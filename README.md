# belabox-hotplug

Watches for video sources coming and going - any external USB capture
card/action-cam/dongle, the onboard HDMI-in port, AND a phone streaming into
the box over WiFi (RTMP or SRT) - and tells belaUI to stop/restart the stream
accordingly, picking a matching pipeline automatically instead of one
hardcoded pipeline.

Built for rk3588 BELABOX-based streaming rigs (tested on a Radxa ROCK 5B+),
so you don't have to manually stop/restart the stream every time you swap
cameras or a cable comes loose.

Also included, and usable independently of each other:

- **`modem-apn-fix.py`** - repairs the *internal* LTE attach APN stored in a
  Sierra Wireless modem's own NVRAM, on modem hotplug or on demand. This
  fixes a specific, genuinely hard-to-diagnose failure where a modem sits on
  strong towers forever without ever attaching, and nothing you change in
  NetworkManager or belaUI has any effect.
  [Jump to the full explanation](#sierra-wireless-modem-internal-apn-repair).
- **`fan-lock.sh` / `fan-lock.service`** - a tiny utility that pins the
  board's fan to a fixed cooling state at boot instead of letting the
  kernel's thermal governor ramp it up and down.

## How detection works

**USB sources** (capture dongles, action cams like DJI/GoPro/Insta360,
webcams, etc.) are detected via real udev add/remove events. Any external
USB video4linux device triggers a rescan - there's no vendor/product ID
allowlist, so any USB capture source works out of the box. Some USB capture
chips expose more than one `/dev/videoN` node for the same physical device
(e.g. a real capture stream plus a metadata-only node); on each rescan every
candidate node is tried with `v4l2-ctl --get-fmt-video` and the first one
that actually supports capture is used.

**The onboard HDMI-in** (`rk_hdmirx` driver) is different: its `/dev/videoX`
node always exists, cable or no cable, so udev add/remove events don't apply
to it. Instead, the daemon polls the driver's read-only `power_present` V4L2
control (`v4l2-ctl -d <node> --get-ctrl=power_present`), which reflects
whether the connected source is actually driving power/signal on the HDMI
line right now. This was verified against real hardware:
`/sys/class/hdmirx/hdmirx/status` looked like an obvious candidate but is
**not** reliable - it stayed stuck on `"connected"` for over a minute after
physically unplugging the cable. `power_present` correctly read `0`
immediately and flipped back to `1` as soon as a real signal reappeared.

**Network sources** (a phone streaming to the box over WiFi) are different
again: there is no device node at all, so neither udev nor V4L2 polling
applies. It turns out no polling is needed, because **belaUI runs both ingest
servers itself and already broadcasts their state**:

- **RTMP**: belaUI scrapes an nginx-rtmp stats endpoint on `127.0.0.1:1936`
  once a second and republishes each connected publisher as a sensor named
  `RTMP ingest - <stream name>` with a `<N> Kbps` value. The key exists only
  while a publisher is actually connected.
- **SRT**: belaUI spawns
  `srt-live-transmit -st:yes -stats-report-frequency:500 -statspf:json
  srt://:4000 udp://127.0.0.1:4001` at startup and watches that process's own
  output, publishing an `SRT ingest` sensor on `Accepted SRT source
  connection` and clearing it on `SRT source disconnected`.

Both belacoder presets read the *local* side of those servers -
`rtmpsrc location=rtmp://127.0.0.1/publish/live` and
`udpsrc uri=udp://127.0.0.1:4001` - so the listener exists whether or not
we're streaming. That's what makes detect-then-start possible here; the
pipeline is not itself the listener.

So the daemon just reads belaUI's `sensors` message on the websocket it
already has open. **One catch, and it's easy to miss**: belaUI sends that
broadcast with an activity filter (`broadcastMsg('sensors', ..., getms() -
ACTIVE_TO)`, `ACTIVE_TO = 15000`) and only delivers it to connections that
sent it something in the last 15 seconds. This daemon otherwise says nothing
at all after authenticating, so it sends belaUI's no-op `keepalive` message
every 5s. Without that it would never receive a single `sensors` message and
would never see a phone connect.

A new ingest reading must be seen twice in a row before it's acted on, so a
one-message blip (a publisher reconnecting, a stats scrape landing between
nginx-rtmp updates) doesn't stop and restart the stream for nothing. Network
sources also skip the settle delay: by the time belaUI reports one, the
publisher has completed its handshake and the sensor value *is* a measured
bitrate, so there's nothing left to wait for.

**Point your phone at**: `rtmp://<box-ip>/publish/live` for RTMP, or
`srt://<box-ip>:4000` for SRT.

**Pipeline selection**: once a source is confirmed active, its supported
resolution is probed with `v4l2-ctl --list-formats-ext` and matched against
belaUI's live pipeline list (from its `pipelines` websocket message) by
source-type keyword (`_usb_` vs `_hdmi_` in the pipeline name, e.g.
`rk3588/h265_usb_mjpeg_1080p30` / `rk3588/h265_hdmi_1080p30`) and closest
resolution. Note: belaUI's `pipeline` field in the `start` command is
actually the **sha1 hash of the pipeline's name** (e.g.
`sha1("rk3588/h265_usb_mjpeg_1080p30")`), not the readable string - the
daemon looks this up from the cached pipeline list itself, so you never need
to compute or configure this by hand.

If no pipeline matches the source-type keyword at all, the daemon logs a
warning and falls back to the closest resolution match across all known
**non-network** pipelines rather than failing silently. Network pipelines are
excluded from that pool deliberately: their names carry no resolution, so on
a board with no `_usb_` pipeline a USB camera would otherwise be matched to
an RTMP pipeline and belaUI would happily "stream" an `rtmpsrc` that no phone
is publishing to. A local capture source must never end up on a network
pipeline; if nothing local is left, the daemon refuses to start instead.

The network presets are matched differently, on **fps only** - their names
are `..._30fps`, with no resolution, because the incoming stream is whatever
the phone decides to send and the pipeline just re-encodes it. There is no
device to probe with `v4l2-ctl` and nothing in belaUI's ingest stats reports
the publisher's frame rate (only bitrate, and RTT for SRT), so the target
comes from `network_fps` in config.json (default 30).

**Audio for network sources** is handled by belaUI itself and the daemon
sends no `asrc` for them. Each pipeline in belaUI's list carries an `asrc`
*boolean* - belaUI sets it by regex-testing whether the pipeline file
contains an `alsasrc device=... ! ... mux.` block. It's false for all eight
network presets, whose audio comes out of `flvdemux`/`decodebin` rather than
an ALSA card, and when it's false belaUI ignores the field entirely (it
won't validate it, store it, or call `asrcProbe()`). The daemon reads that
boolean rather than guessing from the source type.

**Priority when several sources are live at once**: `network > hdmi > usb` by
default. The reasoning: a phone publishing to the box is a deliberate act -
it only appears because someone chose to start streaming - whereas HDMI and
USB sources are typically just left plugged in. HDMI still beats USB on the
assumption that a camera on the onboard HDMI-in is the primary,
higher-quality path with a USB dongle/action-cam as the backup. Set
`"source_priority"` to any ordering of `["network", "hdmi", "usb"]` to
change it; any source you leave out is appended at lowest priority rather
than disabled. The old string form (`"hdmi"` / `"usb"`) still works. Whenever
more than one source is present, the daemon logs which won and why.

Sources degrade gracefully in both directions - stop publishing from the
phone and the stream falls back to HDMI, unplug that and it falls back to
USB, all without touching the UI.

**If both RTMP and SRT are publishing at once**, SRT wins. That's not a
preference, it's forced: belaUI builds the sensors message with
`Object.assign({'SRT ingest': ...}, sensors)`, which *replaces* rather than
extends the RTMP entries, so while an SRT source is connected the RTMP keys
aren't in the message to be seen at all. Preferring RTMP in that state would
be unimplementable, not just unwise.

**Recovering from a belaUI restart/crash**: a physically-connected source
produces no USB/HDMI event when belaUI itself goes away and comes back (a
software update, a crash, a manual restart), so the daemon re-checks both
sources from scratch on every (re)connection to belaUI, not just its own
startup - otherwise a source could stay plugged in the whole time and the
stream would just stay off until some unrelated device event happened to
come along. Look for `Reconnected to belaUI: rescanning USB/HDMI sources...`
in the logs to confirm this fired.

**What's not handled**: changing a camera's resolution/fps *without*
unplugging it (e.g. via the camera's own menu) isn't detected - neither USB
nor HDMI monitoring notices an in-place format change on an
already-connected source, only connect/disconnect. The daemon keeps running
the old pipeline, which will start erroring since its caps no longer match
the real source, until the device is unplugged/replugged or the daemon is
restarted.

## 1. Copy files to the board

Either clone directly on the board (simplest - this is what the rest of
these instructions assume):

```bash
# SSH in first: ssh belabox ("belabox" is a placeholder for your board's
# actual hostname/IP, or an SSH config alias if you've set one up)
git clone <this repo's URL> ~/belabox-hotplug-public
```

or copy a local checkout over with `scp` if the board has no outbound git
access:

```bash
scp -r belabox-hotplug-public belabox:~/belabox-hotplug-public
```

Either way, everything below assumes the checkout lives at
`~/belabox-hotplug-public` on the board - adjust the paths if yours is
somewhere else.

## 2. Install on the board (SSH in first: `ssh belabox`)

```bash
sudo apt update
sudo apt install -y python3-pip v4l-utils
sudo pip3 install websockets pyudev

sudo mkdir -p /opt/belabox-hotplug
sudo cp ~/belabox-hotplug-public/belabox_hotplug.py /opt/belabox-hotplug/
sudo cp ~/belabox-hotplug-public/bootstrap_token.py /opt/belabox-hotplug/
sudo cp ~/belabox-hotplug-public/LICENSE /opt/belabox-hotplug/

# Only if you have a Sierra Wireless modem - see the section further down.
# Without both of these files the daemon just skips the modem logic entirely.
sudo cp ~/belabox-hotplug-public/modem-apn-fix.py /opt/belabox-hotplug/
```

(`--break-system-packages` is only needed on newer pip/Ubuntu releases with
PEP 668 externally-managed environments - not required on Ubuntu 22.04.)

`v4l-utils` provides `v4l2-ctl`, which the daemon's USB/HDMI detection shells
out to constantly - without it, every HDMI poll tick fails with
`Failed to read power_present ... 'v4l2-ctl'` and the daemon can never see
either video source. It's normally already present on a stock BELABOX
image; only missing on a bare/custom install.

## 3. Get an auth token (one-time, run manually)

```bash
cd /opt/belabox-hotplug
sudo python3 bootstrap_token.py YOUR_BELAUI_PASSWORD
```

This creates `/etc/belabox-hotplug/config.json` with your saved token. No
further editing is required for a typical setup - USB and HDMI sources are
both auto-detected.

**Security note**: passing the password as a command-line argument means it
will briefly be visible to anything else on the box that can read the
process list (`ps aux`), and it'll land in your shell history unless you
prefix the command with a space (bash: `HISTCONTROL=ignorespace`, or just
edit it out of `~/.bash_history` afterward). This only matters for the one
command above - after that, only the derived token is stored on disk, never
the plaintext password.

## 4. Config reference

```json
{
  "auth_token": "...",
  "belaui_ws_url": "ws://127.0.0.1",
  "hdmi_device": null,
  "hdmi_poll_interval_seconds": 1.0,
  "settle_delay_seconds": 2.0,
  "source_priority": ["network", "hdmi", "usb"],
  "network_fps": 30.0,
  "keepalive_interval_seconds": 5.0,

  "modem_apn_fix_enabled": true,
  "modem_apn_fix_on_start": true,
  "modem_apn_config_path": "/etc/belabox-hotplug/modem-apn.json",
  "modem_settle_delay_seconds": 25.0,
  "modem_fix_timeout_seconds": 300.0
}
```

- `hdmi_device`: leave as `null` to auto-detect the `rk_hdmirx` device node.
  Set explicitly (e.g. `"/dev/video0"`) only if autodetection picks the
  wrong node.
- `hdmi_poll_interval_seconds`: how often to check `power_present`.
- `settle_delay_seconds`: how long to wait after a source appears before
  probing its format and starting the stream, to let it finish
  initializing.
- `source_priority`: ordered list, highest first, from `"network"`, `"hdmi"`
  and `"usb"`. Any source you omit is appended at lowest priority rather than
  disabled. The old string form (`"hdmi"` / `"usb"`) is still accepted.
- `network_fps`: which of the `_25fps` / `_30fps` / `_50fps` / `_60fps`
  RTMP/SRT ingest presets to use. Nothing reports the publisher's real frame
  rate, so this is a setting rather than something probed. 30 suits most
  phones; set 60 if yours publishes 60.
- `keepalive_interval_seconds`: how often to send belaUI's no-op `keepalive`.
  Must stay comfortably under belaUI's 15s activity window or network ingest
  detection stops working entirely - don't raise this above ~10.

The `modem_*` keys control the Sierra Wireless APN repair described below.
They're inert unless both `modem-apn-fix.py` and the file named by
`modem_apn_config_path` exist, so leaving them at their defaults costs
nothing on a rig with no modem.

- `modem_apn_fix_enabled`: master switch for the modem logic.
- `modem_apn_fix_on_start`: also run one check when the daemon starts. A
  modem that was already plugged in at boot never fires a hotplug event, so
  without this a stale APN would only be caught by replugging the modem.
- `modem_settle_delay_seconds`: how long to wait after a modem appears
  before checking it. Much longer than the video settle delay - a modem
  takes tens of seconds to enumerate all its interfaces and for
  ModemManager to finish probing it and read the SIM.
- `modem_fix_timeout_seconds`: hard ceiling on one `modem-apn-fix.py` run.
  The script has its own internal timeouts; this is the daemon's backstop so
  a wedged modem can't wedge the daemon.

There is no `capture_vendor_id`/`capture_product_id` - any external USB
video capture device is detected automatically.

## 5. Install and start the service

```bash
sudo cp ~/belabox-hotplug-public/belabox-hotplug.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable belabox-hotplug
sudo systemctl start belabox-hotplug
```

## 6. Watch it work

```bash
sudo journalctl -u belabox-hotplug -f
```

- Nothing plugged into USB or HDMI: no stream starts.
- Plug in a USB capture source: it's detected, the correct pipeline is
  picked based on its resolution, and streaming starts.
- Unplug it: streaming stops.
- Plug a source into the onboard HDMI port: detected via `power_present`
  (not just device existence), matching pipeline picked, streaming starts.
- Unplug HDMI: streaming stops.
- Start publishing from a phone to `rtmp://<box-ip>/publish/live` or
  `srt://<box-ip>:4000`: detected from belaUI's ingest sensors, matching
  preset picked, streaming starts. Stop publishing and it falls back to
  whatever is still physically connected.
- Several sources at once: the `source_priority` rule is applied and logged.

## Troubleshooting

- **No udev events showing up for a USB device**: confirm it actually shows
  up under `/sys/bus/usb/devices/*/idVendor` - if it doesn't enumerate as a
  USB device at all, this daemon can't see it either.
- **HDMI never seems to detect a source**: check
  `v4l2-ctl -d /dev/video0 --get-ctrl=power_present` manually. If it's stuck
  at `0`, the issue is at the hardware/cable/source level (verified on this
  board: the source device itself must be powered on and actively
  outputting - a cable plugged into an unpowered source will not flip this).
- **"Can't stop/start stream, not connected to belaUI"**: check belaUI's
  actual service name with `systemctl list-units | grep -i bela` and fix
  the `After=`/`Wants=` lines in the `.service` file if it's not literally
  called `belaUI.service`.
- **Auth fails on every reconnect**: the token may have been invalidated
  (e.g. password changed in belaUI). Re-run `bootstrap_token.py`.
- **Pipeline picked seems wrong**: check the journalctl log line
  `Matched <type> source (<w>x<h>@<fps>) to pipeline '<name>'` - matching is
  keyword + closest-resolution only, not a full format match.
- **A phone streaming in is never detected**: first confirm belaUI itself
  sees it - the ingest should appear in belaUI's own sensors panel as
  `RTMP ingest - live` or `SRT ingest`. If belaUI doesn't see it, the problem
  is upstream of this daemon (check the phone's URL, and that
  `srt-live-transmit` is installed - belaUI spawns it and silently does
  nothing if it's missing). If belaUI *does* see it but the daemon doesn't,
  look for `Network ingest change:` in the journal; if that line never
  appears, the keepalive isn't reaching belaUI and the daemon is being
  filtered out of the sensors broadcast.
- **Stream flaps between a phone and a camera**: the phone is winning by
  default. Reorder `source_priority` to put `"hdmi"` or `"usb"` first.
- **`No 'rtmp' pipeline found in belaUI's list`**: this board's belacoder
  build doesn't ship the network-ingest presets. Check
  `ls /usr/share/belacoder/pipeline/rk3588/ | grep -E 'rtmp|srt'`.

## Sierra Wireless modem internal APN repair

`modem-apn-fix.py`. Read this section before deciding you don't need it -
the failure it fixes is invisible from every layer you'd normally look at.

### The root cause

On Qualcomm-based Sierra Wireless modems (EM7455 and relatives), the LTE
**initial attach** uses a PDP context stored in the modem's own NVRAM -
`AT+CGDCONT` profile 1 - and **not** the APN configured in NetworkManager or
belaUI. The modem puts that internally-stored APN into the very first ATTACH
REQUEST, before any host-side connection manager is consulted at all.

If that internal context is stale, blank, or belongs to some other carrier -
very common on used/secondhand modems that carried a previous owner's
config - the network rejects the attach with an **ESM failure**. What you
see is:

- The modem scans and finds real towers.
- Signal is good: strong RSRP/SINR, correct band, correct PLMN.
- And it never attaches. `EMM state: Deregistered / Searching`, or
  `Registered` with a `Limited Service` substate, forever.

That looks identical to a dozen unrelated problems - a bad SIM, an
unactivated line, antennas on the wrong ports, a band-lock, a carrier
provisioning issue - and no amount of changing the APN in NetworkManager or
belaUI has any effect, because **that layer is never reached**. This one
cost hours to find.

The fix, confirmed working on two separate EM7455 units, is to rewrite
profile 1 over the modem's AT command port and cycle the radio:

```
AT!ENTERCND="A710"                     # unlock Sierra's protected commands
AT+CGDCONT=1,"IP","<correct_apn>"      # rewrite the attach profile in NVRAM
AT+CFUN=0                              # radio off
AT+CFUN=1                              # radio on, re-attach with the new APN
```

then verify with `AT!GSTATUS?`, looking for `EMM state: Registered` **and**
`PS state: Attached`. Both matter: `Registered` alone can coexist with a
`Limited Service` substate and no PS bearer, which is exactly what a
rejected attach looks like.

### The second, separate bug: belaUI never reads any of the above

Getting the modem to attach is necessary but **not sufficient** for belaUI
to actually connect it. belaUI's own data connection runs through a
completely different mechanism: a NetworkManager `gsm` connection profile
that belaUI creates and manages itself, with its **own**, independent `apn`
field. This isn't a guess - it's read straight out of belaUI's own source
(`getGsmConns()` / `modemGetConfig()` / `handleModemConfig()` in
`belaUI.js`):

- The moment belaUI sees a new modem+SIM (matched by ModemManager's
  `device-identifier` + the SIM's ICCID, **not** the IMEI), it auto-creates
  a NetworkManager connection for it - on a hardcoded 1-second polling loop
  that runs from belaUI startup, not gated on the UI being open.
- That connection defaults to `autoconfig: true`. If this NetworkManager
  build supports it, `autoconfig` **blanks the APN** and hands the guess to
  NetworkManager's own carrier database (MBPI) instead. If it doesn't
  support that property at all, belaUI just writes the literal string
  `"internet"`. Either way, for an MVNO (Tello, Cricket, most non-US
  carriers) that's very likely wrong - and it's wrong independently of
  whatever `AT+CGDCONT` profile 1 says, because belaUI never looks at
  profile 1.
- belaUI only *dials* that connection once ModemManager reports the modem
  as `registered`/`enabled`. A modem stuck on a bad attach APN never reaches
  that state, so belaUI never even attempts to connect it - meaning the
  CGDCONT fix above is a **precondition**, not a substitute, for this part.

So there are two independent APN mismatches, in two different subsystems,
configured through two different channels, and **both have to be correct**
before belaUI can establish a working data connection. Fixing only the
modem's internal profile gets it registered on the network and then stuck
exactly where it was before - belaUI still can't connect it, for a
completely different reason that looks, from the outside, identical to the
first problem.

`modem-apn-fix.py` fixes both, using the same resolved APN for each, and
verifies both independently: `AT!GSTATUS?` for the attach, and polling
NetworkManager's own connection state for the data bearer.

### What the script does

1. **Finds every connected Sierra modem and each one's AT port dynamically**,
   via ModemManager (`mmcli -J`), falling back to probing each candidate tty
   with a bare `AT`. Nothing is hardcoded to `ttyUSB2`: with an onboard M.2
   modem *and* a second one on a USB-to-M.2 adapter, port numbers shift
   depending on what's plugged in and in what order. Sierra's OEM rebrands
   (Dell `413c`, HP `03f0`) are recognized as well as Sierra's own `1199`.
2. **Reads the active SIM's ICCID/IMSI** - `mmcli`, then `qmicli`
   (`--uim-get-card-status` for presence, then reading EF_ICCID `3F00/2FE2`
   and EF_IMSI `3F00/7FFF/6F07` directly), then `AT+CIMI`/`AT+ICCID` as a
   last resort. Three tiers because each has a real failure case: mmcli
   reports `--` for a SIM it hasn't finished probing, qmicli needs a QMI port
   that MBIM-mode firmware doesn't expose, and the AT port is sometimes the
   only thing that answers on a half-initialized modem - which is precisely
   the state this script exists for.
3. **Looks up the expected APN** in `/etc/belabox-hotplug/modem-apn.json` -
   your own ICCID/PLMN entries plus a shipped table of major US carrier APNs
   (see [The APN map](#the-apn-map) below). Adding a SIM is a config edit,
   not a code edit. If nothing matches, the script logs that and **refuses to
   guess** - writing a wrong attach APN causes exactly the failure it's here
   to repair.
4. **Compares that against the modem's stored profile** (`AT+CGDCONT?`) and
   does nothing if they already match. This is important: it runs on every
   hotplug event and at every boot, and cycling a radio that's already
   correct drops a working link for no reason.
5. **Only on mismatch**, applies the sequence above, then polls `AT!GSTATUS?`
   every 5s for up to 90s until the modem is Registered + Attached.
6. **Then does the same idempotent check/fix for belaUI's NetworkManager
   connection** - the second, separate layer described above. Finds it by
   `gsm.device-id` + `gsm.sim-id`, exactly how belaUI itself matches it, and
   deliberately never creates one itself - only patches an existing
   belaUI-created connection, so it can never race or duplicate belaUI's own
   connection lifecycle. Patches `gsm.apn` and turns off `gsm.auto-config`
   only if what it finds is actually wrong, tears the connection down so
   belaUI redials fresh, and polls NetworkManager's own connection state for
   `activated`. Runs regardless of what the CGDCONT step found - a modem can
   be fine on one layer and wrong on the other.
7. **Logs every step** - which modem, which SIM, expected vs. found APN on
   *both* layers, whether either fix ran, whether registration and
   activation actually came back - plus a summary block at the end, because
   this is not something you can debug blind from a journal that only says
   "failed".
8. **Fails gracefully.** Every timeout is bounded and no loop is unbounded.
   If registration or activation doesn't come back it says so and stops,
   leaving the modem with its radio **on** and both corrected APNs written,
   rather than parked in a half-fixed, worse-than-before state. One bad
   modem never stops another from being handled, and a caller (this script's
   own exit code, or the daemon reading it) can never mistake a real failure
   for success - `ok: false` on any layer always surfaces as a non-zero exit.

### Getting the AT port away from ModemManager

ModemManager owns the AT port, so a naive `open("/dev/ttyUSB2")` fights it
for responses. There are two ways around that and the script supports both,
selectable with `"at_method"`:

- **`mmcli -m N --command='AT...'`** fails with *"operation only allowed in
  debug mode"* unless ModemManager's log level is DEBUG. That can be flipped
  at runtime with `mmcli --set-logging=DEBUG` - no daemon restart, no modems
  dropped - and the script flips it back afterwards. Non-invasive, good for
  read-only checks. But it is *not* reliable across the `AT+CFUN=0/1` cycle:
  MM tears its port handling down when the radio drops, and the follow-up
  `AT+CFUN=1` can land on a closed port.
- **`mmcli -m N --inhibit`** tells MM to let go of that **one** device - and
  keep managing every other modem - for as long as the inhibit process
  lives. The tty stays enumerated and is now genuinely free.

So the default (`"at_method": "auto"`) is: **mmcli for the read-only check**
(nothing is disturbed on a modem that turns out to be fine) and **direct
serial under an inhibit for the write**, so the entire write-and-verify
window happens inside one inhibit and MM never gets a chance to interfere
mid-sequence.

Note on restoring the log level: ModemManager has no "read current level"
call, so the script restores to `modemmanager_restore_log_level` (default
`INFO`), not to whatever was set before. If you deliberately run MM with
`-d`, set that key to `"DEBUG"`.

Deliberately **not** the default: `systemctl stop ModemManager`. On a bonded
multi-modem rig that drops the *other* modem's link too - taking the stream
down to fix a modem that wasn't even carrying it. It's available via
`"port_release": "stop-modemmanager"` if you want it.

The serial transport uses stdlib `termios` directly, so there's no `pyserial`
dependency to install.

### Install

```bash
sudo mkdir -p /etc/belabox-hotplug
sudo cp ~/belabox-hotplug-public/modem-apn-fix.py /opt/belabox-hotplug/
sudo chmod 755 /opt/belabox-hotplug/modem-apn-fix.py
sudo cp ~/belabox-hotplug-public/modem-apn.example.json /etc/belabox-hotplug/modem-apn.json
sudo chmod 600 /etc/belabox-hotplug/modem-apn.json   # ICCIDs are identifying
sudo nano /etc/belabox-hotplug/modem-apn.json        # put your real SIMs in
```

`nmcli`/NetworkManager is required for the NetworkManager-layer fix below -
it's already present on any board running belaUI, since belaUI itself uses
it extensively, so this isn't an extra install.

Requires `modemmanager` (for `mmcli`); `libqmi-utils` (for `qmicli`) is
optional and only used as an ICCID/IMSI fallback. ModemManager 1.10+ is
needed for `mmcli -J`; Ubuntu 22.04 ships 1.18.

Restart the daemon afterwards so it picks the modem logic up:

```bash
sudo systemctl restart belabox-hotplug
```

### Manual use

```bash
# See what's connected, including ICCID/IMSI and the resolved AT port.
# Start here when adding a new SIM to the map.
sudo python3 /opt/belabox-hotplug/modem-apn-fix.py --list

# Report mismatches without changing anything. Safe on a live rig.
sudo python3 /opt/belabox-hotplug/modem-apn-fix.py --check

# Apply the fix where the stored APN is wrong.
sudo python3 /opt/belabox-hotplug/modem-apn-fix.py --fix

# One modem only, with debug logging.
sudo python3 /opt/belabox-hotplug/modem-apn-fix.py --fix --modem 1 --verbose

# Troubleshoot the CGDCONT layer in isolation, without touching NetworkManager.
sudo python3 /opt/belabox-hotplug/modem-apn-fix.py --fix --no-networkmanager
```

Exit codes, for scripting: `0` nothing to do or both layers verified, `10` a
mismatch was found (on either layer) in `--check` mode, `20` a fix was
applied but didn't fully verify - the modem didn't re-register, or
NetworkManager didn't activate the corrected connection (the relayed log
lines say which), `30` operational error (no modems, missing tools, unmapped
SIM). `--json` prints a machine-readable summary with both layers' results
per modem (`action`/`registered` for CGDCONT, `nm_action`/`nm_activated` for
NetworkManager).

### The APN map

See [modem-apn.example.json](modem-apn.example.json) for the full annotated
format. It ships pre-populated with the major US carriers, so a plain retail
SIM usually needs no configuration at all:

```json
{
  "carriers": {
    "AT&T": {
      "mcc_mnc_prefixes": ["310410", "310380", "310170", "310560", "310980"],
      "apn": "NXTGENPHONE",
      "apn_5g": "ENHANCEDPHONE",
      "apn_hotspot": "Broadband",
      "notes": "..."
    },
    "T-Mobile":  { "mcc_mnc_prefixes": ["310260", "..."], "apn": "fast.t-mobile.com" },
    "Verizon":   { "mcc_mnc_prefixes": ["311480", "..."], "apn": "vzwinternet" }
  },
  "apn_by_iccid_prefix": {
    "8901240": { "apn": "wholesale",    "label": "Tello (T-Mobile US MVNO)" },
    "8934300": { "apn": "america.bics", "label": "eiot / BICS" }
  },
  "default_apn": null
}
```

> **This table goes stale.** Carrier APNs change, networks get retired and
> MVNOs get acquired. Treat it as a good starting point, **not** as
> permanently authoritative - spot-check it against each carrier's own
> current documentation periodically, and re-check any entry the moment a
> SIM won't attach. Bump `_apn_table_reviewed` (`"YYYY-MM"`) when you do; the
> script logs a warning once it's over a year old. The shipped values were
> transcribed from a supplied list current as of mid-2026 and have not been
> independently verified against carrier documentation.

**Match precedence** (`match_order`, first match wins):

```
iccid  ->  imsi_plmn  ->  carrier  ->  default
```

Your own per-ICCID entries deliberately outrank the shipped carrier table,
and **that ordering is load-bearing rather than stylistic**. The table is
keyed on the *host network's* PLMN, but an MVNO SIM broadcasts the host's
PLMN while needing a different APN. A Tello SIM reports `310260` (T-Mobile)
and would match the T-Mobile row, yet its real APN is `wholesale`, not
`fast.t-mobile.com`. Cricket is the same story on AT&T's PLMNs - it needs
`ndo`/`endo`, not `NXTGENPHONE`. If the carrier table were consulted first,
every MVNO would match its host carrier's row and the per-ICCID override
would never be reached for precisely the SIMs it exists to serve. Reorder
`match_order` if you want it the other way, but that's the trade.

**Matching keys on the SIM's home PLMN, never the serving network.** The
home PLMN comes from ModemManager's SIM `operator-code` (read off the SIM
and already split correctly), falling back to slicing the IMSI - exactly, if
the SIM's `EF_AD` gave us the MNC length, otherwise trying the 3-digit
reading before the 2-digit one. That matters because "310260..." is
genuinely ambiguous between MCC 310 / MNC 26 and MCC 310 / MNC 260. Using
the *registered* operator instead would be wrong for any roaming SIM - and a
BICS-style global SIM is always roaming, so it would match whichever local
carrier it happened to camp on.

**MVNOs with no `mcc_mnc_prefixes`** (Cricket, Consumer Cellular, Boost)
can't be matched automatically - they ride their host carrier's PLMN, so
there's nothing unique to match on. They're reference data; point an ICCID
prefix at one to use it:

```json
"apn_by_iccid_prefix": {
  "8901360": { "carrier": "Cricket Wireless (AT&T MVNO)" },
  "8901361": { "carrier": "Cricket Wireless (AT&T MVNO)", "variant": "apn_alt" },
  "8901490": { "carrier": "AT&T", "variant": "apn_hotspot", "label": "AT&T data-only" }
}
```

**Which APN field gets used is decided per modem, from what the hardware can
actually do:**

| Modem reports | Order used | Default |
|---|---|---|
| LTE only | `apn_variant_order` | `["apn", "apn_alt", "apn_hotspot"]` |
| 5GNR capable | `apn_variant_order_5g` | `["apn_5g", "apn", "apn_alt", "apn_hotspot"]` |

This matters because handing AT&T's `ENHANCEDPHONE` to an EM7455 - LTE-only
Cat-6 silicon that will never see a 5G cell - would be writing a knowingly
wrong attach APN, which is the failure this whole script exists to repair.
So the same AT&T SIM resolves differently depending on which modem it's in:

```
EM7455 (LTE-only)        -> NXTGENPHONE     [AT&T (apn)]
EM9191 (5GNR capable)    -> ENHANCEDPHONE   [AT&T (apn_5g)]
5G modem, LTE-locked     -> NXTGENPHONE     [AT&T (apn)]
```

Detection keys on ModemManager's **current** capabilities and access
technologies, not supported ones - a modem that supports 5GNR but is
configured LTE-only will make its initial attach over LTE, so the LTE APN is
what belongs in profile 1. That third line above is exactly that case, and
it's logged rather than silently treated as LTE-only hardware. Set
`assume_5g` to `true`/`false` to override; `null` (default) autodetects.
`--list` shows each modem's capabilities and which order it will use.

Where a carrier uses one APN for both (T-Mobile, Verizon), `apn_5g` is set
to the same value on purpose rather than omitted - it records that it was
checked. Deleting it is harmless; the order falls through to `apn`.

Worth knowing for AT&T: their own guidance is `Broadband` (`apn_hotspot`)
for tablets, hotspots and standalone modem devices rather than
`NXTGENPHONE`. A BELABOX modem is arguably one of those, but it depends on
the plan the SIM is on, so the default still prefers `apn` - override
per-SIM with `{"carrier": "AT&T", "variant": "apn_hotspot"}` if
`NXTGENPHONE` doesn't attach. An explicit `variant` always wins over the
capability-based choice.

Map values may be a bare APN string, an object with `apn`/`label`/
`pdp_type`, or a `{"carrier": ..., "variant": ...}` reference into the
table. In `apn_by_iccid_prefix` a short prefix covers a whole batch of SIMs
and a full 19-20 digit ICCID pins one specific SIM, overriding the short
rule because it's longer. `apn_by_imsi_plmn` lets you override a carrier
table row without editing the table. `default_apn: null` means "log it and
leave the modem alone" rather than guessing.

### belaUI's NetworkManager connection (the second layer)

Controlled by a separate set of config keys, all inert-safe defaults:

```json
{
  "configure_networkmanager": true,
  "nm_disable_autoconfig": true,
  "nm_apply_timeout_seconds": 20.0,
  "nm_verify_timeout_seconds": 30.0,
  "nm_verify_poll_seconds": 2.0,
  "nm_wait_for_connection_seconds": 20.0,
  "nm_wait_for_connection_poll_seconds": 2.0
}
```

- `configure_networkmanager`: master switch. Turn off to only ever touch the
  modem's own NVRAM, never NetworkManager - useful if something else is
  managing that connection and you don't want this script anywhere near it.
- `nm_disable_autoconfig`: turns off `gsm.auto-config` on the matched
  connection when it's on, so belaUI's own default doesn't blank the APN
  back out again the next time it touches the connection. Silently skipped
  on a NetworkManager build that doesn't have that property at all.
- `nm_wait_for_connection_seconds`: how long to wait for belaUI to
  auto-create its connection before giving up on this modem's NetworkManager
  fix. Exists for one specific race: this daemon and belaUI can both start
  at boot in either order, and if this daemon's one-shot startup check runs
  before belaUI has created the connection, there's no later trigger to
  catch it except another hotplug event - which may never come for a modem
  that's simply left plugged in. This bounds that wait instead of assuming
  an ordering.
- `nm_verify_timeout_seconds`: how long to wait for NetworkManager to report
  the connection `activated` after the APN is corrected. This script never
  calls `nmcli connection up` itself - belaUI's own 1-second polling loop is
  what actually dials it, once ModemManager reports the modem `registered`.
  This setting only verifies that outcome; it can't force it.

**How it finds the right connection**: by `gsm.device-id` (ModemManager's
`device-identifier` - a computed hash, **not** the IMEI) plus `gsm.sim-id`
(the SIM's ICCID) - the exact pair belaUI itself matches on. It **never**
creates a connection of its own; if none exists yet (after the wait above),
it logs that clearly and moves on, rather than risking a duplicate that
fights belaUI's own connection lifecycle.

**What a fix actually does**, mirroring `nmcli`'s own semantics for exactly
what a manual edit in belaUI's own modem settings panel does: sets
`gsm.apn` (and `gsm.auto-config no`, if enabled), then `nmcli connection
down` so belaUI's next ~1-second poll tick redials fresh with the corrected
APN rather than leaving a bearer that may already be up, however badly, with
the old one.

`--list` shows the matched connection (or its absence) for every modem:

```
    nm dev-id:  aabbccdd...
    nm conn:    3f9a2b7e-... (apn='wholesale', auto-config=False, state=activated)
```

### Hotplug integration

`belabox_hotplug.py` runs a second udev watcher on the `usb` subsystem,
filtered to whole USB devices so one physical modem produces one event
rather than one per interface. On `add` for a matching vendor ID it waits
`modem_settle_delay_seconds` (default 25s, for enumeration + ModemManager
probing), folds any further events that arrived during the wait into the
same run, and then spawns `modem-apn-fix.py --fix` as a **subprocess**.

Subprocess rather than an import, deliberately: the script blocks for up to
~2 minutes cycling a radio and waiting for re-registration, and a hang or
crash in there must not be able to take the streaming daemon with it. Its
output is relayed into the daemon's log prefixed with `[modem-apn-fix]`, so
everything lands in one journal. Runs are serialized - never two at once,
since two fixes racing would both be trying to inhibit ModemManager and
cycle radios simultaneously.

### Modem troubleshooting

- **"No APN mapping matched this SIM"**: run `--list`, take the ICCID and
  home PLMN it prints, and either add an ICCID entry or add that PLMN to the
  right carrier's `mcc_mnc_prefixes`.
- **A major-carrier SIM got the wrong APN**: it's probably an MVNO matching
  its host carrier's row in the shipped table. Add a per-ICCID entry - that
  outranks the table. Also spot-check the table itself against the carrier's
  current documentation; the shipped values do go out of date.
- **"No AT port found"**: check `mmcli -m 0` and look for a port of type
  `(at)`. Some firmware compositions expose none - switch the modem's USB
  composition (Sierra: `AT!USBCOMP=...`) or use MBIM/QMI mode with the AT
  port enabled.
- **"mmcli refused: ModemManager is not in debug mode"** and the serial
  fallback also fails: something else is holding the port. Check with
  `sudo fuser -v /dev/ttyUSB*`.
- **Fix applied but never attaches**: the APN value itself is the next thing
  to doubt - confirm it with the carrier. Then check the SIM is activated,
  and that the antennas are on the right ports (an EM7455 with only the
  diversity antenna connected registers but performs terribly).
- **ModemManager left noisy after a run**: the log level restore failed;
  `sudo mmcli --set-logging=INFO` fixes it, and it resets on the next MM
  restart anyway.
- **It cycles the modem on every boot**: that means the write isn't
  sticking. Check `--check` right after a `--fix` - if the APN reads back
  wrong, `AT!ENTERCND` likely didn't unlock this firmware and needs a
  different password in `entercnd_password`.
- **The modem registers fine (GSTATUS shows Attached) but belaUI still
  shows it as disconnected/failed**: this is the second layer, not the
  first - check `--list`'s `nm conn:` line, or run
  `nmcli -g gsm.device-id,gsm.sim-id,gsm.apn,gsm.auto-config connection
  show` directly. If the APN there doesn't match what you expect, that's
  belaUI's NetworkManager connection, not the modem's internal profile -
  re-run `--fix` (not `--fix --no-networkmanager`) to correct it.
- **`nm dev-id: ?`**: ModemManager's `device-identifier` field came back
  empty. The NetworkManager fix can't run without it - check
  `mmcli -m <N> -K | grep device-identifier` directly; if it's genuinely not
  reported by this ModemManager version, `configure_networkmanager: false`
  disables that layer and you'll need to set the APN through belaUI's own
  modem settings UI manually instead.
- **"No NetworkManager gsm connection found... after waiting Ns"**: belaUI
  itself likely isn't running, or hasn't gotten to this modem yet. Confirm
  with `systemctl status belaUI` (or whatever belaUI's actual unit is
  called) and that it's actually up.
- **NetworkManager APN corrected but never shows `activated`**: this is
  usually just sequencing, not a real failure - belaUI only dials once
  ModemManager reports the modem `registered`, so if the CGDCONT/GSTATUS
  result above didn't succeed either, that's the thing to fix first. If the
  modem *is* registered and it still won't activate, try
  `nmcli connection up <uuid>` manually and read its actual error.

## fan-lock

A small, independent utility - pins the fan to a fixed cooling state at
boot rather than letting the kernel's thermal governor ramp it up/down.

```bash
sudo cp fan-lock.sh /usr/local/bin/fan-lock.sh
sudo chmod 755 /usr/local/bin/fan-lock.sh
sudo cp fan-lock.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now fan-lock
```

`cur_state` in `fan-lock.sh` (currently `2`) is board/cooling-device
specific - check `/sys/class/thermal/cooling_device0/` on your own hardware
before assuming this value applies.

## License

MIT - see [LICENSE](LICENSE). Copyright (c) 2026 Jason Ardon (W3ndees).
