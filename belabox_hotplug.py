#!/usr/bin/env python3
"""
belabox-hotplug: watches for any external USB video capture device AND the
onboard HDMI-in port coming and going, and tells belaUI (over its local
websocket API) to stop/restart streaming accordingly.

Three independent detection mechanisms are used, because they're
fundamentally different kinds of events:

  - USB sources: real udev add/remove events (the device node appears and
    disappears from the system).
  - The onboard HDMI-in (rk_hdmirx driver): the /dev/videoX node for it is
    always present, cable or no cable, so udev events don't apply. Instead
    this polls the driver's `power_present` V4L2 control, which reflects
    whether the connected source is actually driving power/signal on the
    HDMI line (verified against this board's hardware - the /sys/class/hdmirx
    status file it looked like we could use instead does NOT reflect real
    cable/signal state, it stays "connected" even with nothing plugged in).
  - Network sources (a phone streaming into the box over WiFi): there's no
    device node at all. belaUI runs both ingest servers itself - an
    nginx-rtmp server it scrapes on 127.0.0.1:1936, and an
    `srt-live-transmit srt://:4000 -> udp://127.0.0.1:4001` relay it spawns -
    and it republishes both as entries in its 'sensors' websocket broadcast.
    So this needs no polling of its own: it just reads that broadcast on the
    connection it already has. See BelaUIClient._handle_sensors().

When a source is confirmed active, its supported resolution is probed via
`v4l2-ctl --list-formats-ext` and matched against belaUI's live pipeline list
(cached from the 'pipelines' websocket message) by source-type keyword and
closest resolution, instead of using one hardcoded pipeline.

Separately, and completely independently of the video path, this also watches
for WWAN modems being hotplugged and runs modem-apn-fix.py against them (see
that script's docstring and the README for why that's needed). It's spawned as
a subprocess rather than imported, so nothing it does can stall or crash the
streaming logic.

Run via the accompanying systemd unit (belabox-hotplug.service), which
should be installed to start at boot alongside belaUI.

Copyright (c) 2026 Jason Ardon (W3ndees)
Licensed under the MIT License - see LICENSE in this repository.
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import threading

import pyudev
import websockets

CONFIG_PATH = "/etc/belabox-hotplug/config.json"

# The modem APN repair tool lives next to this file (both get copied into
# /opt/belabox-hotplug). Resolved relative to this script rather than
# hardcoded, so running the daemon straight out of a git checkout works too.
MODEM_APN_FIX_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "modem-apn-fix.py")
MODEM_APN_CONFIG_PATH = "/etc/belabox-hotplug/modem-apn.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("belabox-hotplug")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        log.error(f"No config found at {CONFIG_PATH}. Run bootstrap_token.py first.")
        sys.exit(1)
    with open(CONFIG_PATH) as f:
        return json.load(f)


class BelaUIClient:
    """
    Thin wrapper around the belaUI websocket protocol.
    Keeps a cache of the last-seen config/pipelines/status messages so we
    know what settings to reuse when we issue our own start command.
    """

    def __init__(self, ws_url: str, auth_token: str):
        self.ws_url = ws_url
        self.auth_token = auth_token
        self.ws = None
        self.authenticated = asyncio.Event()

        self.last_config = {}
        self.last_pipelines = {}
        self.is_streaming = False
        # belaUI's current list of audio source names (e.g. "HDMI",
        # the connected USB device's product name, "No audio"), broadcast
        # in 'status' messages and
        # refreshed by belaUI itself on USB audio hotplug. Needed so
        # start_stream() can send the *correct* asrc for whichever source is
        # now active - see pick_asrc().
        self.available_asrcs = []
        # Gets one item pushed onto it on every successful authentication -
        # the first connection AND every reconnect after belaUI restarts or
        # drops the connection. A physically-connected source doesn't emit
        # any USB/HDMI event when belaUI itself goes away and comes back, so
        # this is what lets the daemon notice "belaUI is back but not
        # streaming, and something is still plugged in" instead of only
        # reconciling once at its own startup. See reconcile_on_reconnect().
        self.reconnect_queue = asyncio.Queue()

        # Network ingest state, derived from belaUI's 'sensors' broadcast:
        # None, "rtmp", or "srt". See _handle_sensors() for how belaUI
        # reports this and why it's the detection mechanism for phones
        # streaming in over WiFi.
        self.network_ingest = None
        self._network_candidate = None
        self._network_candidate_count = 0
        # Set by main() so sensor-driven transitions can raise events the
        # same way the udev watcher and the HDMI poller do.
        self.event_queue = None

    async def connect_and_listen(self):
        """Runs forever: connects, authenticates, and processes incoming
        messages, reconnecting automatically if the connection drops."""
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.ws = ws
                    self.authenticated.clear()
                    await ws.send(json.dumps({"auth": {"token": self.auth_token}}))
                    log.info("Connected to belaUI, authenticating...")

                    async for raw in ws:
                        await self._handle_message(json.loads(raw))

            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.warning(f"belaUI connection lost/unavailable ({e}), retrying in 5s...")
                self.ws = None
                self.authenticated.clear()
                await asyncio.sleep(5)

    async def _handle_message(self, msg: dict):
        if "auth" in msg:
            auth = msg["auth"]
            if auth.get("success") is True:
                log.info("Authenticated with belaUI")
                self.authenticated.set()
                self.reconnect_queue.put_nowait(True)
            else:
                log.error(f"belaUI auth failed: {auth}. Re-run bootstrap_token.py")

        if "config" in msg:
            self.last_config = msg["config"]

        if "pipelines" in msg:
            self.last_pipelines = msg["pipelines"]
            log.info(f"Got {len(self.last_pipelines)} pipelines from belaUI")

        if "status" in msg:
            status = msg["status"]
            if "is_streaming" in status:
                self.is_streaming = status["is_streaming"]
                log.info(f"Stream state update: is_streaming={self.is_streaming}")
            if "asrcs" in status:
                self.available_asrcs = status["asrcs"]

        if "sensors" in msg:
            self._handle_sensors(msg["sensors"])

    def _handle_sensors(self, sensors: dict):
        """
        Detects a phone (or anything else) streaming into the box over the
        network, from belaUI's own 'sensors' broadcast.

        belaUI runs both ingest servers itself, so there's nothing to poll at
        the socket level and nothing extra to install:
          - RTMP: it scrapes an nginx-rtmp stats endpoint on 127.0.0.1:1936
            once a second and republishes each live publisher as a sensor
            named "RTMP ingest - <stream name>" with a "<N> Kbps" value. The
            key only exists while a publisher is actually connected.
          - SRT: it spawns `srt-live-transmit ... srt://:4000
            udp://127.0.0.1:4001` at startup and watches that process's own
            output, publishing an "SRT ingest" sensor on "Accepted SRT source
            connection" and clearing it on "SRT source disconnected".

        Both matching belacoder pipelines read from the *local* side of those
        servers (`rtmpsrc location=rtmp://127.0.0.1/publish/live` and
        `udpsrc uri=udp://127.0.0.1:4001`), so the listener exists
        independently of whether we're streaming - which is what makes
        detect-then-start possible here at all.

        SRT deliberately wins when both are somehow live at once: belaUI
        builds this message with
        `Object.assign({'SRT ingest': ...}, sensors)`, which *replaces*
        rather than extends the RTMP entries, so while an SRT source is
        connected the RTMP keys aren't in the message to be seen. Preferring
        RTMP in that state would be unimplementable, not just unwise.
        """
        srt_active = bool(sensors.get("SRT ingest"))
        rtmp_active = any(k.startswith("RTMP ingest - ") for k in sensors)

        if srt_active:
            observed = "srt"
        elif rtmp_active:
            observed = "rtmp"
        else:
            observed = None

        if observed == self.network_ingest:
            self._network_candidate = None
            self._network_candidate_count = 0
            return

        # Require the same new reading twice running before acting on it.
        # These sensor messages arrive about once a second and an ingest can
        # blip (a publisher reconnecting, a stats scrape landing between
        # nginx-rtmp updates); without this, a one-message gap would stop and
        # restart the stream for no reason.
        if observed != self._network_candidate:
            self._network_candidate = observed
            self._network_candidate_count = 1
            return
        self._network_candidate_count += 1
        if self._network_candidate_count < 2:
            return

        previous = self.network_ingest
        self.network_ingest = observed
        self._network_candidate = None
        self._network_candidate_count = 0

        log.info(f"Network ingest change: {previous or 'none'} -> {observed or 'none'}")
        if self.event_queue is not None:
            self.event_queue.put_nowait(("network", "add" if observed else "remove"))

    async def keepalive_loop(self, interval: float = 5.0):
        """
        Sends belaUI's no-op 'keepalive' message on a timer.

        This is not optional politeness - it's required for network ingest
        detection to work at all. belaUI broadcasts the 'sensors' message
        with an activity filter (`broadcastMsg('sensors', ..., getms() -
        ACTIVE_TO)`, ACTIVE_TO = 15000), and only sends it to connections
        whose `lastActive` is within the last 15 seconds. `lastActive` is
        refreshed when belaUI receives *any* valid message from us. This
        daemon otherwise sends nothing at all after authenticating, so
        without this it would never receive a single sensors message and
        would never see a phone connect.

        belaUI has an explicit 'keepalive' case in its message handler that
        does nothing but let that refresh happen, which is exactly what this
        uses.
        """
        while True:
            await asyncio.sleep(interval)
            if self.ws is None or not self.authenticated.is_set():
                continue
            try:
                await self.ws.send(json.dumps({"keepalive": 0}))
            except (websockets.ConnectionClosed, OSError) as e:
                log.debug(f"Keepalive send failed (connection is down): {e}")

    async def wait_ready(self, timeout=15):
        try:
            await asyncio.wait_for(self.authenticated.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("Timed out waiting for belaUI authentication")

    async def stop_stream(self):
        if not self.ws:
            log.warning("Can't stop stream, not connected to belaUI")
            return
        log.info("Sending stop to belaUI")
        await self.ws.send(json.dumps({"stop": 0}))

    async def start_stream(self, pipeline: str, asrc: str = None):
        if not self.ws:
            log.warning("Can't start stream, not connected to belaUI")
            return

        # Reuse the last known settings (bitrate, SRT/SRTLA config, etc.)
        # and only override the pipeline field (and asrc, if given - needed
        # when switching from a source with one audio device to a source
        # with a different one, see pick_asrc()).
        cfg = dict(self.last_config)
        cfg["pipeline"] = pipeline
        if asrc is not None:
            cfg["asrc"] = asrc

        log.info(f"Sending start to belaUI with pipeline={pipeline}"
                 f"{f', asrc={asrc}' if asrc is not None else ''}")
        await self.ws.send(json.dumps({"start": cfg}))


# ---------------------------------------------------------------------------
# USB source detection
# ---------------------------------------------------------------------------

def is_external_usb_video_device(device) -> bool:
    """True if this video4linux device hangs off a USB device (as opposed to
    the onboard HDMI-in, which is a platform device with no USB ancestor).
    Matches any USB capture card/action-cam/dongle, not one specific
    vendor/product."""
    return device.find_parent(subsystem="usb", device_type="usb_device") is not None


def node_supports_capture(device_node: str) -> bool:
    """Some USB capture chips expose 2+ /dev/videoN nodes for one physical
    device (e.g. a real capture stream node plus a metadata-only node that
    errors out on format queries). This is the cheapest reliable way found
    to tell them apart: try to query the current format and see if the
    driver accepts it."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device_node, "--get-fmt-video"],
            capture_output=True, timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


def find_working_usb_node():
    """Scans every external USB video4linux device currently present,
    grouped by physical USB device, and returns the device node of the
    first one that actually supports capture (not just the first node that
    happens to exist). Returns None if no external USB source is usable
    right now."""
    context = pyudev.Context()
    groups = {}
    for device in context.list_devices(subsystem="video4linux"):
        if not is_external_usb_video_device(device):
            continue
        usb_parent = device.find_parent(subsystem="usb", device_type="usb_device")
        groups.setdefault(usb_parent.sys_path, []).append(device.device_node)

    for usb_path, nodes in groups.items():
        for node in sorted(nodes):
            if node_supports_capture(node):
                return node
    return None


def udev_watcher_thread(loop: asyncio.AbstractEventLoop, event_queue: asyncio.Queue):
    """Runs in a background thread (pyudev's observer is not asyncio-native)
    and pushes add/remove events for any external USB video4linux device
    into the asyncio queue. Doesn't care which node fired - handle_events
    rescans all candidates once settled, so it naturally copes with a
    device that shows up as several /dev/videoN nodes at once."""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="video4linux")

    def device_event(device):
        action = device.action  # 'add' or 'remove'
        if action not in ("add", "remove"):
            return
        if not is_external_usb_video_device(device):
            return  # this is the onboard HDMI-in node, handled separately

        log.info(f"udev event: {action} for {device.device_node} (external USB video device)")
        loop.call_soon_threadsafe(event_queue.put_nowait, ("usb", action))

    observer = pyudev.MonitorObserver(monitor, callback=device_event)
    observer.start()
    log.info("udev watcher started (watching for any external USB video4linux device)")

    # Keep the thread alive; the observer runs its own internal thread too,
    # but we block here so this function (and thus this thread) doesn't exit.
    threading.Event().wait()


# ---------------------------------------------------------------------------
# HDMI source detection
# ---------------------------------------------------------------------------

def find_hdmi_device():
    """Locates the onboard HDMI-in device node by walking up from each
    video4linux device to find one bound to the rk_hdmirx platform driver,
    rather than assuming it's always /dev/video0."""
    context = pyudev.Context()
    for device in context.list_devices(subsystem="video4linux"):
        parent = device.find_parent(subsystem="platform")
        if parent is not None and parent.driver == "rk_hdmirx":
            return device.device_node
    return None


def get_hdmi_power_present(hdmi_device: str) -> bool:
    """Polls the rk_hdmirx driver's read-only `power_present` V4L2 control,
    which reflects whether the connected source is actually driving
    power/signal on the HDMI line right now. This was verified against this
    board's hardware to be reliable where the /sys/class/hdmirx/hdmirx/status
    sysfs file was NOT: that file stayed stuck on "connected" for over a
    minute after physically unplugging the cable, while power_present
    correctly read 0 immediately and flipped back to 1 once a live signal
    was actually present again."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", hdmi_device, "--get-ctrl=power_present"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode != 0:
            return False
        value = result.stdout.strip().split(":")[-1].strip()
        return value == "1"
    except Exception as e:
        log.warning(f"Failed to read power_present from {hdmi_device}: {e}")
        return False


async def poll_hdmi(hdmi_device: str, poll_interval: float, event_queue: asyncio.Queue,
                     initial_present: bool):
    """Background asyncio task: polls power_present at a fixed interval and
    pushes an 'add'/'remove' event whenever it changes state. Takes the
    already-known startup state so it doesn't log/emit a spurious
    transition for state that was already established before this task
    started."""
    last_present = initial_present
    while True:
        present = await asyncio.to_thread(get_hdmi_power_present, hdmi_device)
        if present != last_present:
            action = "add" if present else "remove"
            log.info(f"HDMI signal {'detected' if present else 'lost'} on {hdmi_device}")
            await event_queue.put(("hdmi", action))
            last_present = present
        await asyncio.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Format probing + pipeline matching
# ---------------------------------------------------------------------------

def probe_source_resolution(device_node: str):
    """Runs `v4l2-ctl --list-formats-ext` on the given node and returns the
    (width, height, fps) of its first listed format. On this hardware both
    the USB card and the HDMI-in report their best/current format first
    (USB: highest native resolution; HDMI: the live-negotiated signal
    format), so this doesn't need special-casing per source type."""
    try:
        result = subprocess.run(
            ["v4l2-ctl", "-d", device_node, "--list-formats-ext"],
            capture_output=True, text=True, timeout=3,
        )
    except Exception as e:
        log.warning(f"Failed to probe formats on {device_node}: {e}")
        return None, None, None

    size_match = re.search(r"Size:\s*Discrete\s*(\d+)x(\d+)", result.stdout)
    if not size_match:
        return None, None, None
    width, height = int(size_match.group(1)), int(size_match.group(2))

    fps_match = re.search(r"Discrete\s*[\d.]+s\s*\(([\d.]+)\s*fps\)", result.stdout)
    fps = float(fps_match.group(1)) if fps_match else None

    return width, height, fps


def parse_pipeline_resolution(name: str):
    """Extracts (height, fps) from a pipeline name like
    'rk3588/h265_usb_mjpeg_1080p30' -> (1080, 30.0). Returns (None, None)
    for bare pipelines with no resolution suffix, e.g. 'rk3588/h265_hdmi'."""
    m = re.search(r"(\d{3,4})p(\d+(?:\.\d+)?)?", name)
    if not m:
        return None, None
    height = int(m.group(1))
    fps = float(m.group(2)) if m.group(2) else None
    return height, fps


# Network-ingest pipelines: belacoder's rk3588 set has four of each, e.g.
# 'rk3588/h265_rtmp_localhost_publish_live_30fps' (rtmpsrc reading the local
# nginx-rtmp server) and 'rk3588/h265_srt_port_4000_30fps' (udpsrc reading
# what belaUI's srt-live-transmit relays to 127.0.0.1:4001).
_NETWORK_KEYWORDS = {"rtmp": "_rtmp_", "srt": "_srt_"}


def is_network_pipeline(name: str) -> bool:
    return any(kw in name for kw in _NETWORK_KEYWORDS.values())


def parse_pipeline_fps(name: str):
    """Extracts the fps from a network pipeline name, which encodes *only*
    fps and no resolution: '..._publish_live_30fps' -> 30.0.

    Local-capture pipelines say '1080p30'; the network ones say '30fps',
    because the incoming stream's resolution is whatever the phone decides to
    send and the pipeline just re-encodes it. That's also why picking one of
    these can't be resolution-matched the way a USB or HDMI source is - there
    is no device to probe with v4l2-ctl."""
    m = re.search(r"_(\d+(?:\.\d+)?)fps\b", name)
    return float(m.group(1)) if m else None


def pick_network_pipeline(ingest_type: str, pipelines: dict, target_fps: float):
    """Picks the belacoder pipeline for an active RTMP or SRT ingest.

    Matched on transport keyword plus closest fps. `target_fps` comes from
    config rather than from the stream, because nothing in belaUI's ingest
    stats reports the publisher's frame rate - only bitrate (and RTT for
    SRT). 30 is the sane default for a phone; set network_fps in config.json
    if yours sends 60."""
    keyword = _NETWORK_KEYWORDS.get(ingest_type)
    candidates = {pid: p for pid, p in pipelines.items()
                  if keyword and keyword in p["name"]}
    if not candidates:
        log.error(f"No '{ingest_type}' pipeline found in belaUI's list. This board's "
                  f"belacoder build may not ship the network-ingest pipelines; "
                  f"expected a name containing '{keyword}'.")
        return None, None

    def distance(item):
        _, p = item
        fps = parse_pipeline_fps(p["name"])
        # An fps-less network pipeline (none ship today) sorts last rather
        # than being treated as a perfect match.
        return abs((fps if fps is not None else 0.0) - target_fps)

    best_id, best = min(candidates.items(), key=distance)
    log.info(f"Matched {ingest_type.upper()} ingest (target {target_fps:g}fps) "
             f"to pipeline '{best['name']}'")
    return best_id, best["name"]


def pick_pipeline(source_type: str, device_node: str, pipelines: dict):
    """
    Picks the belaUI pipeline that best matches the given active source.

    `pipelines` is the {id: {name, asrc, acodec}} dict cached from belaUI's
    'pipelines' websocket message (BelaUIClient.last_pipelines). Matching is
    keyword-based on source type ('_usb_' vs '_hdmi_' in the pipeline name,
    e.g. "rk3588/h265_usb_mjpeg_1080p30" / "rk3588/h265_hdmi_1080p30") plus
    closest resolution - this is a simple heuristic for the MVP, not a
    format-accurate match (e.g. it won't distinguish MJPEG-only USB cards
    from H264 UVC action cams by codec, only by the "_usb_"/"_hdmi_" keyword
    belacoder's own pipeline names happen to use).

    Returns (pipeline_id, pipeline_name), or (None, None) if nothing usable
    was found at all (e.g. belaUI hasn't sent its pipeline list yet).
    """
    if not pipelines:
        log.warning("No pipeline list cached from belaUI yet, can't pick a pipeline")
        return None, None

    width, height, fps = probe_source_resolution(device_node)
    if height is None:
        log.warning(f"Could not determine resolution for {device_node}; "
                    f"matching on source type only")

    keyword = "_usb_" if source_type == "usb" else "_hdmi_"
    candidates = {pid: p for pid, p in pipelines.items() if keyword in p["name"]}

    if not candidates:
        # The fallback pool deliberately excludes the RTMP/SRT network-ingest
        # pipelines. They have no resolution in their names, so they'd score
        # (inf, inf) and normally sort last - but if they were the only
        # pipelines left, a USB camera would be matched to one and belaUI
        # would happily "stream" an rtmpsrc that no phone is publishing to.
        # A local capture source must never end up on a network pipeline.
        candidates = {pid: p for pid, p in pipelines.items()
                      if not is_network_pipeline(p["name"])}
        log.warning(f"No pipelines matched keyword '{keyword}' for {source_type} source; "
                    f"falling back to closest-resolution match across all "
                    f"non-network pipelines rather than failing silently")
        if not candidates:
            log.error(f"No local-capture pipelines exist at all for a {source_type} "
                      f"source; refusing to start on a network-ingest pipeline")
            return None, None

    if height is None:
        pid, p = next(iter(candidates.items()))
        log.warning(f"No resolution to match on either; falling back to first "
                    f"available candidate pipeline: {p['name']}")
        return pid, p["name"]

    def distance(item):
        _, p = item
        p_height, p_fps = parse_pipeline_resolution(p["name"])
        if p_height is None:
            return (float("inf"), float("inf"))  # rank bare/no-resolution pipelines last
        fps_diff = abs((p_fps or 30.0) - (fps or 30.0))
        return (abs(p_height - height), fps_diff)

    best_id, best = min(candidates.items(), key=distance)
    log.info(f"Matched {source_type} source ({width}x{height}@{fps}) "
             f"to pipeline '{best['name']}'")
    return best_id, best["name"]


# belaUI's fixed onboard audio source names (see its own audioSrcAliases /
# addAudioCardById in belaUI.js) - anything else in the asrcs list is a
# hotplugged external device (e.g. a USB action cam's own audio interface).
_ONBOARD_ASRC_NAMES = {"HDMI", "Analog in", "No audio", "Pipeline default"}


def pick_asrc(source_type: str, available_asrcs: list, pipeline: dict = None):
    """
    Picks the belaUI audio source name ("asrc") to send in the start
    command for the given active source type. Returns None when belaUI
    doesn't want an asrc at all for this pipeline.

    `pipeline` is the entry from belaUI's cached pipeline list. Its "asrc"
    field is a *boolean*, not a name: belaUI sets it by regex-testing whether
    the pipeline file contains an `alsasrc device=... ! ... mux.` block
    (pipelineGetAudioProps() in belaUI.js). When it's false, belaUI ignores
    the asrc field completely - it won't validate it, won't store it, and
    won't call asrcProbe() - because the audio is already inside the source
    the pipeline reads from.

    That's the case for every RTMP/SRT network-ingest pipeline: their audio
    comes out of `flvdemux`/`decodebin`, not from an ALSA card, so there is
    no audio device to select and nothing to probe.

    This exists because belaUI's own "start" handler will hang indefinitely
    probing for the *previous* asrc if it's stale (verified on this board:
    switching from HDMI to a USB action cam while blindly reusing the old
    cached "asrc": "HDMI" made belaUI's startStream() call
    `asrcProbe("HDMI")` and just never return, since no HDMI audio device
    exists anymore - the stream silently never actually started even though
    belaUI already reported is_streaming=True).

    `available_asrcs` is belaUI's current list of audio source names (from
    its 'status' message, BelaUIClient.available_asrcs) - it's refreshed by
    belaUI itself whenever a USB audio device is plugged/unplugged.

    Returns an asrc name, or None if no confident choice could be made (in
    which case the caller should fall back to "No audio" rather than send
    nothing and risk reusing a stale value).
    """
    # Ask the pipeline itself before guessing from the source type. This is
    # both more accurate than a keyword guess and the only correct answer for
    # network pipelines, where sending an asrc is meaningless.
    if pipeline is not None and not pipeline.get("asrc", True):
        log.info(f"Pipeline '{pipeline.get('name')}' carries its own audio "
                 f"(no alsasrc); sending no asrc")
        return None

    if source_type == "hdmi":
        if "HDMI" in available_asrcs:
            return "HDMI"
        log.warning("'HDMI' not in belaUI's current asrcs list; "
                    f"available: {available_asrcs}")
        return None

    # source_type == "usb": look for whichever asrc isn't one of the fixed
    # onboard names - that's the hotplugged device's own audio interface.
    external = [a for a in available_asrcs if a not in _ONBOARD_ASRC_NAMES]
    if len(external) == 1:
        return external[0]
    if len(external) > 1:
        log.warning(f"Multiple external audio sources present ({external}); "
                    f"picking '{external[0]}' - ambiguous with more than one "
                    f"USB audio device connected at once")
        return external[0]

    log.warning(f"No external audio source found in belaUI's asrcs list "
                f"({available_asrcs}) for the active USB video source; "
                f"falling back to 'No audio' rather than risk hanging on a "
                f"stale asrc")
    return "No audio" if "No audio" in available_asrcs else None


# ---------------------------------------------------------------------------
# Source arbitration
# ---------------------------------------------------------------------------

class SourceState:
    def __init__(self, hdmi_device: str):
        self.hdmi_device = hdmi_device
        self.hdmi_present = False
        self.usb_node = None
        # "rtmp", "srt", or None - mirrors BelaUIClient.network_ingest.
        self.network_ingest = None
        # None, "hdmi", "usb", "network:rtmp" or "network:srt". The network
        # transport is part of the key so that a phone switching from RTMP to
        # SRT is correctly seen as a change of source, not as "still network".
        self.active_source = None


async def evaluate_and_apply(state: SourceState, client: BelaUIClient, priority: list,
                               network_fps: float = 30.0):
    """
    Decides which source (if any) should be streaming and only issues a
    start/stop to belaUI if that decision actually changed - this runs
    after every USB, HDMI or network-ingest event, so it needs to be a no-op
    when e.g. the USB metadata node disappears but the real capture node (and
    thus the USB source as a whole) is still present.

    `priority` is an ordered list of source kinds, highest first.
    """
    available = {}
    if state.network_ingest:
        available["network"] = f"network:{state.network_ingest}"
    if state.hdmi_present:
        available["hdmi"] = "hdmi"
    if state.usb_node:
        available["usb"] = "usb"

    chosen_kind = next((kind for kind in priority if kind in available), None)
    chosen = available.get(chosen_kind)

    if len(available) > 1:
        log.info(f"Multiple sources present ({', '.join(sorted(available))}); "
                 f"priority {' > '.join(priority)} selects '{chosen}'")

    if chosen == state.active_source:
        return

    # belaUI's own "start" websocket handler is a no-op whenever it already
    # considers itself streaming (see belaUI.js: `if (isStreaming ...) return;`)
    # - including while it's internally erroring/auto-retrying the *previous*
    # pipeline after the old source disappeared (verified on this board: losing
    # HDMI mid-stream makes belacoder throw repeated "gstreamer error from
    # v4l2src0/alsasrc0", and belaUI just keeps relaunching the same stale
    # HDMI pipeline in a retry loop of its own, completely ignoring a "start"
    # sent with a new pipeline). So switching from one already-active source
    # to another must explicitly stop and wait for confirmation first - just
    # sending "start" with the new pipeline is silently ignored otherwise.
    if client.is_streaming:
        log.info(f"Stopping current stream before switching "
                  f"({state.active_source} -> {chosen})")
        await client.stop_stream()
        if not await wait_for_stream_stopped(client):
            log.warning("belaUI did not confirm the stream stopped in time; "
                        "proceeding to start anyway")

    if chosen is None:
        state.active_source = None
        return

    if chosen_kind == "network":
        # Nothing local to probe: the "device" is whatever the phone is
        # publishing into belaUI's own RTMP server or SRT relay.
        device_node = None
        pipeline_id, pipeline_name = pick_network_pipeline(
            state.network_ingest, client.last_pipelines, network_fps)
        where = f"{state.network_ingest.upper()} ingest"
    else:
        device_node = state.hdmi_device if chosen == "hdmi" else state.usb_node
        pipeline_id, pipeline_name = pick_pipeline(chosen, device_node,
                                                   client.last_pipelines)
        where = device_node

    if pipeline_id is None:
        log.error(f"No usable pipeline found for {chosen} source at {where}, "
                  f"not starting stream")
        state.active_source = None
        return

    asrc = pick_asrc(chosen_kind, client.available_asrcs,
                     client.last_pipelines.get(pipeline_id))

    log.info(f"Switching active source to {chosen} ({where}), "
             f"pipeline={pipeline_name}, asrc={asrc}")
    await client.start_stream(pipeline_id, asrc=asrc)
    state.active_source = chosen


async def wait_for_stream_stopped(client: BelaUIClient, timeout: float = 10.0,
                                    poll_interval: float = 0.2) -> bool:
    """Polls BelaUIClient.is_streaming (updated from belaUI's 'status'
    messages) until it goes False, since belaUI ignores "start" while it
    still considers itself streaming."""
    elapsed = 0.0
    while client.is_streaming and elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return not client.is_streaming


async def wait_for_pipelines(client: BelaUIClient, timeout: float = 5.0,
                               poll_interval: float = 0.2) -> bool:
    """Waits for belaUI's pipeline list to arrive after (re)connecting,
    since it's sent as a follow-up message after auth succeeds, not
    atomically with it."""
    elapsed = 0.0
    while not client.last_pipelines and elapsed < timeout:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return bool(client.last_pipelines)


async def reconcile_now(state: SourceState, client: BelaUIClient, priority: list,
                          network_fps: float = 30.0, label: str = "Reconciling"):
    """Rescans every source from scratch and applies whatever the result
    should be. Used both at daemon startup and on every belaUI reconnect
    (see reconcile_on_reconnect) - state.active_source is deliberately
    invalidated first so evaluate_and_apply() can't skip the decision as
    "unchanged" based on stale memory of what it last set, when what
    actually matters is whether belaUI itself is currently streaming."""
    log.info(f"{label}: rescanning USB/HDMI/network sources...")
    state.active_source = "unknown"
    state.usb_node = await asyncio.to_thread(find_working_usb_node)
    state.hdmi_present = await asyncio.to_thread(get_hdmi_power_present, state.hdmi_device)
    # Network ingest isn't rescannable on demand - it's whatever belaUI last
    # told us in a 'sensors' broadcast, so just resync from the client.
    state.network_ingest = client.network_ingest
    await evaluate_and_apply(state, client, priority, network_fps)


async def reconcile_on_reconnect(client: BelaUIClient, state: SourceState, priority: list,
                                   network_fps: float = 30.0):
    """Runs once per successful (re)connection to belaUI *after* the very
    first one (which the daemon's startup sequence in main() already
    consumes and handles directly). A physically-connected source produces
    no USB/HDMI event when belaUI itself restarts or drops the connection,
    so without this, a belaUI crash/restart could leave the stream off
    indefinitely even though nothing about the actual video source
    changed."""
    while True:
        await client.reconnect_queue.get()
        if not await wait_for_pipelines(client):
            log.warning("Reconnected to belaUI but never got a pipeline list; "
                        "skipping reconcile")
            continue
        # A belaUI restart also restarts its srt-live-transmit relay and
        # resets its RTMP stats, so drop any remembered ingest state and wait
        # to be told again rather than acting on a pre-restart reading.
        client.network_ingest = None
        client._network_candidate = None
        client._network_candidate_count = 0
        await reconcile_now(state, client, priority, network_fps,
                            label="Reconnected to belaUI")


async def handle_events(event_queue: asyncio.Queue, client: BelaUIClient,
                          state: SourceState, settle_delay: float, priority: list,
                          network_fps: float = 30.0):
    while True:
        source_type, action = await event_queue.get()

        # Network ingest is exempt from the settle delay: by the time belaUI
        # reports it, the publisher has already completed its handshake and is
        # pushing data (the sensor value *is* a measured bitrate), so there's
        # nothing left to wait for. The delay exists for local devices, whose
        # nodes appear before they're ready to be probed.
        if action == "add" and source_type != "network":
            log.info(f"Waiting {settle_delay}s for {source_type} source to settle...")
            await asyncio.sleep(settle_delay)

        if source_type == "usb":
            state.usb_node = await asyncio.to_thread(find_working_usb_node)
            if action == "add" and state.usb_node is None:
                log.warning("USB add event fired, but no working capture node found "
                            "among the candidates")
        elif source_type == "hdmi":
            state.hdmi_present = await asyncio.to_thread(get_hdmi_power_present, state.hdmi_device)
        elif source_type == "network":
            state.network_ingest = client.network_ingest

        await evaluate_and_apply(state, client, priority, network_fps)


# ---------------------------------------------------------------------------
# WWAN modem hotplug -> internal APN repair
#
# Completely independent of everything above: this doesn't touch belaUI, the
# stream, or the video source state machine. It exists because a Sierra
# Wireless modem's LTE *attach* APN lives in the modem's own NVRAM
# (AT+CGDCONT profile 1), not in NetworkManager - so a modem carrying a stale
# APN from a previous owner will camp on towers with great signal and never
# attach, and nothing on the host side can fix it. See modem-apn-fix.py's
# docstring and the README for the full story.
#
# modem-apn-fix.py is run as a subprocess, not imported: it blocks for up to
# ~2 minutes cycling a modem's radio and waiting for re-registration, and a
# crash or hang in there must not be able to take the streaming daemon with
# it. Its stdout/stderr are relayed into this daemon's log so everything ends
# up in one journal.
# ---------------------------------------------------------------------------

# Sierra rebrands its modules to OEMs, so this matches more than one vendor:
# 1199 = Sierra Wireless, 413c = Dell (DW5811e/DW5816e), 03f0 = HP.
# Kept in sync with "usb_vendor_ids" in modem-apn.json - this list only
# decides whether to *wake up* the fix script, which does its own, stricter
# Sierra check before touching anything.
MODEM_USB_VENDOR_IDS = {"1199", "413c", "03f0"}


def is_wwan_modem(device) -> bool:
    """True if this USB device looks like one of the WWAN modems we care
    about. Checked against both the udev property and the raw sysfs
    attribute, because ID_VENDOR_ID is set by udev's usb_id builtin and isn't
    guaranteed to be populated on every event."""
    vendor = device.get("ID_VENDOR_ID")
    if not vendor:
        try:
            vendor = device.attributes.asstring("idVendor")
        except (KeyError, UnicodeDecodeError, AttributeError):
            vendor = None
    return bool(vendor) and vendor.lower() in MODEM_USB_VENDOR_IDS


def modem_udev_watcher_thread(loop: asyncio.AbstractEventLoop, modem_queue: asyncio.Queue):
    """Second udev watcher, on the usb subsystem this time, filtered to whole
    USB devices (device_type='usb_device') so one physical modem produces one
    event rather than one per USB interface.

    Only 'add' is interesting: a modem being unplugged needs no action, and
    'change' events fire constantly for unrelated reasons."""
    context = pyudev.Context()
    monitor = pyudev.Monitor.from_netlink(context)
    monitor.filter_by(subsystem="usb", device_type="usb_device")

    def device_event(device):
        if device.action != "add":
            return
        if not is_wwan_modem(device):
            return

        name = device.get("ID_MODEL") or device.sys_name
        log.info(f"udev event: add for WWAN modem {name} "
                 f"(vendor {device.get('ID_VENDOR_ID')})")
        loop.call_soon_threadsafe(modem_queue.put_nowait, f"hotplug: {name}")

    observer = pyudev.MonitorObserver(monitor, callback=device_event)
    observer.start()
    log.info("udev watcher started (watching for WWAN modem hotplug)")

    threading.Event().wait()


async def run_modem_apn_fix(reason: str, apn_config_path: str, timeout: float):
    """Runs modem-apn-fix.py --fix once and relays its output into this log.

    Hard-bounded by `timeout`: the script has its own internal registration
    timeout, but if it ever wedged (a serial read on a modem that stopped
    responding mid-USB-reset, say), this daemon must not wedge with it."""
    cmd = [sys.executable, MODEM_APN_FIX_SCRIPT, "--fix", "--config", apn_config_path]
    log.info(f"Running modem APN check ({reason})")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        log.error(f"Could not launch {MODEM_APN_FIX_SCRIPT}: {e}")
        return

    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        log.error(f"modem-apn-fix.py exceeded {timeout}s; killing it. The modem is "
                  f"left as-is - check it manually with "
                  f"'python3 {MODEM_APN_FIX_SCRIPT} --list'")
        proc.kill()
        await proc.wait()
        return

    for line in stdout.decode("utf-8", "replace").splitlines():
        if line.strip():
            log.info(f"[modem-apn-fix] {line}")

    # Exit codes are defined at the top of modem-apn-fix.py.
    if proc.returncode == 0:
        log.info("Modem APN check finished: nothing to do, or the fix verified OK")
    elif proc.returncode == 20:
        log.error("Modem APN fix was applied but the modem did not re-register in time")
    elif proc.returncode == 30:
        log.error("Modem APN check could not run (no modems, missing tools, or "
                  "an unmapped SIM) - see the relayed output above")
    else:
        log.warning(f"Modem APN check exited with code {proc.returncode}")


async def handle_modem_events(modem_queue: asyncio.Queue, settle_delay: float,
                                timeout: float, apn_config_path: str):
    """Serializes modem fixes: one at a time, never concurrently, since two
    fixes racing would both be trying to inhibit ModemManager and cycle
    radios at once.

    The settle delay is much longer than the video one - a modem takes tens of
    seconds to enumerate all its interfaces and for ModemManager to finish
    probing it and read the SIM. Any further events that arrive during the
    wait are folded into this same run rather than queueing up another pass,
    since the script always checks every modem present anyway."""
    while True:
        reason = await modem_queue.get()

        log.info(f"Waiting {settle_delay}s for the modem to enumerate and for "
                 f"ModemManager to probe it...")
        await asyncio.sleep(settle_delay)

        coalesced = 0
        while not modem_queue.empty():
            modem_queue.get_nowait()
            coalesced += 1
        if coalesced:
            reason += f" (+{coalesced} further event(s) coalesced)"

        await run_modem_apn_fix(reason, apn_config_path, timeout)


async def main():
    config = load_config()

    ws_url = config.get("belaui_ws_url", "ws://127.0.0.1")
    auth_token = config["auth_token"]
    settle_delay = config.get("settle_delay_seconds", 2.0)
    hdmi_poll_interval = config.get("hdmi_poll_interval_seconds", 1.0)

    # Which source wins when more than one is active at once, highest first.
    # Network (a phone streaming in over WiFi via RTMP or SRT) leads by
    # default because it's the deliberate act - a phone only appears when
    # someone chose to start publishing to the box, whereas HDMI and USB
    # sources are typically just left plugged in. HDMI still beats USB on the
    # assumption that a camera on the onboard HDMI-in is the primary,
    # higher-quality path and a USB dongle/action-cam is the backup.
    #
    # The old string form ("hdmi" / "usb", meaning only which of those two
    # wins) is still accepted so existing config.json files keep working.
    priority = config.get("source_priority", ["network", "hdmi", "usb"])
    if isinstance(priority, str):
        priority = (["network", "usb", "hdmi"] if priority == "usb"
                    else ["network", "hdmi", "usb"])
        log.info(f"Config uses the old string source_priority; treating it as "
                 f"{priority}")
    valid = ["network", "hdmi", "usb"]
    priority = [s for s in priority if s in valid]
    # Anything left out of an explicit list is appended rather than dropped,
    # so a config naming only some sources can't silently disable the rest.
    missing = [s for s in valid if s not in priority]
    if missing:
        log.warning(f"source_priority did not list {missing}; appending at lowest "
                    f"priority rather than ignoring those sources")
        priority += missing
    log.info(f"Source priority: {' > '.join(priority)}")

    # Frame rate used to choose among the RTMP/SRT ingest pipelines, whose
    # names encode fps only (e.g. '..._30fps'). Nothing in belaUI's ingest
    # stats reports the publisher's actual frame rate, so this is a config
    # value rather than something probed.
    network_fps = float(config.get("network_fps", 30.0))

    # Modem APN repair (see the section above and modem-apn-fix.py). Enabled
    # by default, but silently and harmlessly inert unless BOTH the script and
    # an APN map config are actually present - so existing installs that never
    # copied those files across behave exactly as they did before.
    modem_fix_enabled = config.get("modem_apn_fix_enabled", True)
    modem_apn_config = config.get("modem_apn_config_path", MODEM_APN_CONFIG_PATH)
    modem_settle_delay = config.get("modem_settle_delay_seconds", 25.0)
    modem_fix_timeout = config.get("modem_fix_timeout_seconds", 300.0)
    modem_fix_on_start = config.get("modem_apn_fix_on_start", True)

    if modem_fix_enabled:
        missing = [p for p in (MODEM_APN_FIX_SCRIPT, modem_apn_config)
                   if not os.path.exists(p)]
        if missing:
            log.info(f"Modem APN repair not configured ({', '.join(missing)} missing) - "
                     f"skipping it. See README if you have a Sierra Wireless modem.")
            modem_fix_enabled = False

    hdmi_device = config.get("hdmi_device") or find_hdmi_device()
    if not hdmi_device:
        log.error("Could not find the onboard HDMI-in device (rk_hdmirx driver). "
                  "Set 'hdmi_device' explicitly in config.json if autodetection is wrong.")
        sys.exit(1)
    log.info(f"Using {hdmi_device} as the onboard HDMI-in device")

    client = BelaUIClient(ws_url, auth_token)
    loop = asyncio.get_event_loop()
    event_queue = asyncio.Queue()
    modem_queue = asyncio.Queue()
    state = SourceState(hdmi_device)
    # Lets the client raise network-ingest events from belaUI's 'sensors'
    # broadcast into the same queue the udev watcher and HDMI poller use.
    client.event_queue = event_queue

    # Connect first and wait for belaUI's initial 'pipelines' message to
    # arrive before evaluating any state, otherwise a source that's already
    # plugged in at startup would race pick_pipeline() against an empty
    # pipeline cache and fail to start.
    connect_task = asyncio.create_task(client.connect_and_listen())
    await client.wait_ready()
    await wait_for_pipelines(client)

    # Consume the reconnect-queue item that corresponds to this same first
    # connection ourselves, and reconcile synchronously here - before the
    # watcher thread below is started. find_working_usb_node() creates its
    # own pyudev.Context(), and doing that at the exact same moment the
    # watcher thread is initializing its own Context()/Monitor was found
    # (via testing on this board) to intermittently deadlock somewhere in
    # the underlying libudev bindings. Once the watcher thread is already up
    # and just blocked listening for events, creating additional
    # short-lived Contexts alongside it is fine - which is why every
    # *subsequent* reconnect (belaUI restarting/crashing later, handled by
    # reconcile_on_reconnect below) doesn't need this same care.
    await client.reconnect_queue.get()
    await reconcile_now(state, client, priority, network_fps, label="Startup")

    # udev watching happens in a background thread since pyudev's observer
    # isn't asyncio-native; it pushes events into our asyncio queue safely.
    watcher_thread = threading.Thread(
        target=udev_watcher_thread,
        args=(loop, event_queue),
        daemon=True,
    )
    watcher_thread.start()

    tasks = [
        connect_task,
        # Mandatory for network ingest detection, not just hygiene: belaUI
        # only sends its 'sensors' broadcast to connections that have been
        # active in the last 15s. See BelaUIClient.keepalive_loop().
        client.keepalive_loop(config.get("keepalive_interval_seconds", 5.0)),
        poll_hdmi(hdmi_device, hdmi_poll_interval, event_queue, state.hdmi_present),
        handle_events(event_queue, client, state, settle_delay, priority, network_fps),
        reconcile_on_reconnect(client, state, priority, network_fps),
    ]

    if modem_fix_enabled:
        # Started after the video watcher rather than alongside it, for the
        # same libudev reason documented above: two threads racing to build
        # their first pyudev.Context() was found to intermittently deadlock.
        # By the time this runs, the first watcher is already up and just
        # blocked listening.
        modem_watcher_thread = threading.Thread(
            target=modem_udev_watcher_thread,
            args=(loop, modem_queue),
            daemon=True,
        )
        modem_watcher_thread.start()
        tasks.append(handle_modem_events(modem_queue, modem_settle_delay,
                                         modem_fix_timeout, modem_apn_config))

        if modem_fix_on_start:
            # A modem that was already plugged in at boot never produces a
            # hotplug event, so without this a stale internal APN would only
            # ever be noticed if you happened to replug the modem - which is
            # the whole reason this problem takes hours to find. Queued rather
            # than awaited so a slow modem check can't delay the daemon
            # reaching its normal running state.
            modem_queue.put_nowait("daemon startup")

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
