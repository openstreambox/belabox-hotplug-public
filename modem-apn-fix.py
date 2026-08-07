#!/usr/bin/env python3
"""
modem-apn-fix: repairs the *internal* LTE attach APN stored inside a Sierra
Wireless modem's own NVRAM, on hotplug or on demand.

WHY THIS EXISTS
---------------
On Qualcomm-based Sierra Wireless modems (EM7455 and relatives), the LTE
*initial attach* uses a PDP context stored in the modem's own non-volatile
memory - AT+CGDCONT profile 1 - and NOT the APN configured in
NetworkManager / belaUI. The modem sends that internally-stored APN in the
very first ATTACH REQUEST, long before any host-side connection manager is
consulted.

If that internal context is stale, blank, or belongs to a different carrier
(extremely common on used/secondhand modems that carried a previous owner's
config), the network rejects the attach with an ESM failure. The visible
symptom is maddeningly generic: the modem finds real towers with good
signal (strong RSRP/SINR, correct band, correct PLMN in the scan) and then
sits in "searching" or "limited service" forever. Nothing you change on the
NetworkManager/belaUI side has any effect, because that layer is never even
reached. It looks identical to a bad SIM, a bad antenna, a carrier
provisioning problem, or a band-lock issue.

The fix, confirmed working on two separate EM7455 units, is to rewrite
profile 1 over the modem's AT command port and cycle the radio:

    AT!ENTERCND="A710"                     <- unlock Sierra's protected commands
    AT+CGDCONT=1,"IP","<correct_apn>"      <- rewrite the attach profile in NVRAM
    AT+CFUN=0                              <- radio off
    AT+CFUN=1                              <- radio on, re-attach with the new APN

then verify with AT!GSTATUS?, looking for `EMM state: Registered` and
`PS state: Attached`.

WHAT THIS SCRIPT DOES
---------------------
1. Discovers every connected Sierra Wireless modem and each one's AT port
   dynamically (via ModemManager, falling back to probing tty candidates).
   Port numbering is NOT stable - ttyUSB2 on a single-modem box becomes
   ttyUSB5 once a second modem is plugged into a USB-to-M.2 adapter, and the
   order flips depending on what enumerates first - so nothing here is
   hardcoded.
2. Reads the active SIM's ICCID/IMSI (mmcli, then qmicli, then the AT port)
   and looks up the expected APN in a small user-maintained JSON map, so
   adding a new SIM is a config edit rather than a code edit.
3. Reads the modem's currently stored internal context (AT+CGDCONT?) and
   compares it to the expected APN. **If they already match, it does
   nothing.** This runs on every hotplug event and at boot, so it must not
   needlessly cycle a modem that is already correct - cycling the radio
   drops the link, which on a live streaming rig is not free.
4. Only on mismatch: applies the sequence above, then polls AT!GSTATUS?
   until the modem is Registered + Attached, or until a timeout.
5. Logs every step with enough detail to debug blind from a journal dump:
   which modem, which SIM, what APN was found vs. expected, whether the fix
   was applied, and whether registration actually came back.

GETTING THE AT PORT AWAY FROM MODEMMANAGER
------------------------------------------
ModemManager owns the AT port, so a naive `open("/dev/ttyUSB2")` fights it.
There are two ways around that and this script supports both:

  * `mmcli -m N --command='AT...'` - but it fails with "operation only
    allowed in debug mode" unless the daemon's log level is DEBUG. That can
    be flipped at runtime with `mmcli --set-logging=DEBUG` (no restart
    needed) and flipped back afterwards. Non-invasive, good for read-only
    checks, but unreliable *across* the AT+CFUN=0/1 cycle: MM reacts to the
    radio going down by tearing its own port handling down, and the
    follow-up AT+CFUN=1 can land on a closed port.

  * `mmcli -m N --inhibit` - tells MM to let go of that ONE device (and
    keep managing every other modem) for as long as the inhibit process
    lives. The tty stays enumerated and is now free for direct access. This
    is the default for the actual fix, because the whole write + verify
    window happens inside a single inhibit and MM never gets a chance to
    interfere mid-sequence.

Deliberately NOT the default: `systemctl stop ModemManager`. On a bonded
multi-modem rig that would drop the *other* modem's link too, i.e. take the
stream down to fix a modem that isn't carrying it. Available via
`"port_release": "stop-modemmanager"` in the config if you really want it.

USAGE
-----
    python3 modem-apn-fix.py --check        # report only, never writes
    python3 modem-apn-fix.py --fix          # apply where mismatched
    python3 modem-apn-fix.py --list         # dump what was discovered
    python3 modem-apn-fix.py --fix --modem 1 --verbose

Exit codes (so this is usable from scripts and from the hotplug daemon):
    0  - nothing to do, or every attempted fix verified successfully
    10 - at least one modem needs fixing (--check mode; no changes made)
    20 - a fix was applied but the modem did not register before the timeout
    30 - operational error (no modems, missing tools, bad config)

Called automatically by belabox_hotplug.py on modem hotplug and at daemon
startup. Invoked as a subprocess rather than imported, so that a failure
in here can never take down the video/stream daemon.

Copyright (c) 2026 Jason Ardon (W3ndees)
Licensed under the MIT License - see LICENSE in this repository.
"""
import argparse
import contextlib
import json
import logging
import os
import re
import select
import subprocess
import sys
import time

# termios is Linux/POSIX only. Imported defensively so the file still parses
# and --help still works if someone opens it on a non-POSIX box; the direct
# serial transport raises a clear error if it's actually needed and missing.
try:
    import termios
except ImportError:  # pragma: no cover - non-POSIX
    termios = None

CONFIG_PATH = "/etc/belabox-hotplug/modem-apn.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("modem-apn-fix")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Everything here is overridable from the JSON config. The defaults are the
# values that were actually used on the confirmed-working EM7455 units.
DEFAULT_CONFIG = {
    # SIM -> APN mapping. See modem-apn.example.json and the README.
    "apn_by_iccid_prefix": {},
    "apn_by_imsi_plmn": {},
    # Used when nothing matches. null means "log it and leave the modem
    # alone" - deliberately not a guess, since writing a wrong attach APN is
    # exactly the failure mode this script exists to repair.
    "default_apn": None,

    # Shipped table of major-carrier APNs, keyed by carrier name, matched on
    # the SIM's home PLMN (MCC+MNC). See modem-apn.example.json for the
    # populated version and the schema.
    #
    # !!! THIS TABLE GOES STALE. Carriers do change APNs, retire networks and
    # get acquired. Treat it as a good starting point, not as authoritative -
    # spot-check against your carrier's own current documentation
    # periodically, and re-check any entry the moment a SIM won't attach.
    # `_apn_table_reviewed` in the config records when it was last checked;
    # the script warns when that date is over a year old.
    "carriers": {},

    # Resolution order, first match wins. Deliberately ends at "carrier":
    # your own explicit maps beat the shipped table, because the table is
    # keyed on the *host network's* PLMN and an MVNO SIM shares that PLMN
    # while needing a completely different APN. See resolve_expected_apn().
    "match_order": ["iccid", "imsi_plmn", "carrier", "default"],

    # Which field of a carrier entry to use, first present wins. "apn_5g" is
    # deliberately absent: an EM7455 is LTE-only Cat-6 hardware and a 5G APN
    # on it is at best pointless. Add it here if you're running 5G-capable
    # modems (EM9191 etc.).
    "apn_variant_order": ["apn", "apn_alt", "apn_hotspot"],

    # Informational: when the carrier table above was last verified.
    "_apn_table_reviewed": None,

    "pdp_type": "IP",          # profile 1 PDP type; "IP" = IPv4-only
    "cgdcont_profile": 1,      # the attach profile. Effectively always 1.
    "enforce_pdp_type": True,  # treat a PDP-type difference as a mismatch too
    "entercnd_password": "A710",  # Sierra's protected-command unlock

    # "auto" | "serial" | "mmcli"
    #   auto   - mmcli for read-only checks (least invasive), serial for the
    #            write+verify sequence (survives the CFUN cycle).
    #   serial - always talk to the tty directly.
    #   mmcli  - always go through `mmcli --command`.
    "at_method": "auto",

    # How to get MM off the port when using the serial transport:
    # "inhibit" | "stop-modemmanager" | "none"
    "port_release": "inhibit",
    # Restored after `mmcli --set-logging=DEBUG`. MM has no "get current
    # level" call, so this is what we set it *back* to, not what it was.
    "modemmanager_restore_log_level": "INFO",

    "at_timeout_seconds": 20.0,       # per-AT-command response deadline
    "settle_seconds": 5.0,            # pause after CFUN=1 before polling
    "verify_timeout_seconds": 90.0,   # total budget for reaching Attached
    "verify_poll_seconds": 5.0,       # AT!GSTATUS? polling interval

    # Sierra rebrands its modules to OEMs, so match on more than one vendor
    # ID. 1199 = Sierra Wireless, 413c = Dell (DW5811e/DW5816e), 03f0 = HP.
    "usb_vendor_ids": ["1199", "413c", "03f0"],
    # Set true to operate on non-Sierra modems too. Off by default because
    # AT!ENTERCND / AT!GSTATUS? are Sierra-specific and will just ERROR out
    # on anything else.
    "allow_non_sierra": False,
}

_SIERRA_HINT_RE = re.compile(r"sierra|em7[45]\d\d|em9\d\d\d|mc7\d\d\d", re.I)


def load_config(path: str) -> dict:
    """Loads the APN map, layering it over DEFAULT_CONFIG. A missing file is
    not fatal for --list (you may just want to see what's connected), but
    without a map nothing can be checked or fixed, so that's reported by the
    caller rather than silently treated as 'all good'."""
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(path):
        log.warning(f"No APN map found at {path} - see modem-apn.example.json")
        return cfg
    try:
        with open(path) as f:
            user = json.load(f)
    except (OSError, ValueError) as e:
        log.error(f"Could not read/parse {path}: {e}")
        return cfg

    unknown = [k for k in user if k not in DEFAULT_CONFIG and not k.startswith("_")]
    if unknown:
        # Typo'd key names are otherwise completely silent, which is a nasty
        # thing to debug in the field.
        log.warning(f"Ignoring unrecognized key(s) in {path}: {', '.join(sorted(unknown))}")

    for k in DEFAULT_CONFIG:
        if k in user:
            cfg[k] = user[k]
    return cfg


def has_apn_map(cfg: dict) -> bool:
    return bool(cfg["apn_by_iccid_prefix"] or cfg["apn_by_imsi_plmn"]
                or cfg["carriers"] or cfg["default_apn"])


# ---------------------------------------------------------------------------
# Small subprocess helper (same shape as the v4l2-ctl calls in the daemon)
# ---------------------------------------------------------------------------

def run(cmd, timeout=20.0):
    """Runs a command and returns (returncode, stdout, stderr). Never raises
    for a missing binary or a timeout - returns a non-zero code instead, so
    every call site can treat 'tool unavailable' the same as 'tool said no'."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(cmd)}: timed out after {timeout}s"
    except Exception as e:  # pragma: no cover - defensive
        return 1, "", f"{' '.join(cmd)}: {e}"


def _clean(value):
    """mmcli's JSON uses "--" for 'not available'. Normalize that (and empty
    strings) to None so callers can just check truthiness."""
    if value in (None, "", "--"):
        return None
    return value


def _dig(obj, *keys):
    """Tolerant nested dict lookup - mmcli has moved fields between releases,
    so every read of its JSON goes through this instead of chained [] access."""
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return _clean(obj)


def mmcli_json(args, timeout=20.0):
    """Runs mmcli with --output-json and returns the parsed dict, or None.
    Requires ModemManager >= 1.10 (Ubuntu 22.04 ships 1.18)."""
    rc, out, err = run(["mmcli", "-J"] + args, timeout=timeout)
    if rc != 0:
        log.debug(f"mmcli {' '.join(args)} failed (rc={rc}): {err.strip() or out.strip()}")
        return None
    try:
        return json.loads(out)
    except ValueError:
        log.debug(f"mmcli {' '.join(args)} returned non-JSON output")
        return None


# ---------------------------------------------------------------------------
# AT transports
# ---------------------------------------------------------------------------

class ATError(Exception):
    """Raised when an AT command can't be delivered at all (port gone, no
    response, transport unusable). A modem replying 'ERROR' is NOT this -
    that's a normal response and comes back as an ATResponse."""


class ATResponse:
    """One AT command's reply, split into the informational lines and the
    final result code."""

    def __init__(self, raw: str):
        self.raw = raw
        lines = [ln.strip() for ln in raw.replace("\r", "\n").split("\n")]
        self.lines = [ln for ln in lines if ln]
        self.final = self.lines[-1] if self.lines else ""

    @property
    def ok(self) -> bool:
        return self.final.upper() == "OK"

    def __str__(self):
        return " | ".join(self.lines)


_FINAL_CODES = ("OK", "ERROR", "NO CARRIER", "NO DIALTONE", "BUSY", "NO ANSWER")


def _is_final(line: str) -> bool:
    u = line.strip().upper()
    return u in _FINAL_CODES or u.startswith("+CME ERROR:") or u.startswith("+CMS ERROR:")


class SerialATTransport:
    """Talks to the modem's AT tty directly using stdlib termios - no pyserial
    dependency, matching the rest of this repo's "only pyudev + websockets"
    footprint.

    Assumes ModemManager has already been told to let go of the port (see
    modemmanager_inhibited / modemmanager_stopped). Opening it while MM still
    holds it usually "works" but the two of you interleave commands and
    responses, which produces garbage that is very hard to recognize as
    garbage."""

    def __init__(self, port: str, timeout: float = 20.0):
        self.port = port
        self.timeout = timeout
        self.fd = None

    def open(self):
        if termios is None:
            raise ATError("termios unavailable - the serial transport is POSIX-only")
        try:
            fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise ATError(f"cannot open {self.port}: {e}")

        try:
            attrs = termios.tcgetattr(fd)
            # Raw 8N1. Baud is meaningless for a USB CDC/option port but
            # termios still wants a valid value, and CLOCAL matters: without
            # it, opening a port with no carrier can block forever.
            attrs[0] = termios.IGNPAR                                   # iflag
            attrs[1] = 0                                                # oflag
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL     # cflag
            attrs[3] = 0                                                # lflag
            attrs[4] = termios.B115200                                  # ispeed
            attrs[5] = termios.B115200                                  # ospeed
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception as e:
            os.close(fd)
            raise ATError(f"cannot configure {self.port}: {e}")

        self.fd = fd
        return self

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def command(self, cmd: str, timeout: float = None) -> ATResponse:
        if self.fd is None:
            raise ATError("port not open")
        timeout = self.timeout if timeout is None else timeout

        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)  # drop unsolicited leftovers
            os.write(self.fd, (cmd + "\r").encode("ascii", "ignore"))
        except OSError as e:
            raise ATError(f"write to {self.port} failed: {e}")

        deadline = time.monotonic() + timeout
        buf = ""
        while time.monotonic() < deadline:
            r, _, _ = select.select([self.fd], [], [], min(0.5, max(0.05, deadline - time.monotonic())))
            if not r:
                continue
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                continue
            except OSError as e:
                # The port disappearing mid-read is a real case (a USB adapter
                # that resets), and it's worth distinguishing from a timeout.
                raise ATError(f"read from {self.port} failed: {e}")
            if not chunk:
                continue
            buf += chunk.decode("utf-8", "replace")
            for line in buf.replace("\r", "\n").split("\n"):
                if _is_final(line):
                    return ATResponse(buf)

        raise ATError(f"no final result code from {self.port} for '{cmd}' within {timeout}s")


class MmcliATTransport:
    """Sends AT commands through `mmcli -m N --command=...`.

    This needs ModemManager's log level to be DEBUG, otherwise every call
    comes back as "operation only allowed in debug mode". The level is
    flipped at runtime by the caller (see modemmanager_debug_logging) rather
    than by restarting the daemon with -d, which would drop every modem."""

    _DEBUG_MODE_HINT = "debug mode"

    def __init__(self, mm_index: int, timeout: float = 20.0):
        self.mm_index = mm_index
        self.timeout = timeout

    def open(self):
        return self

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def command(self, cmd: str, timeout: float = None) -> ATResponse:
        timeout = self.timeout if timeout is None else timeout
        # mmcli's own --timeout is in whole seconds and must be >= the time we
        # are willing to wait, or it aborts the command out from under us.
        mm_timeout = max(2, int(timeout))
        rc, out, err = run(
            ["mmcli", "-m", str(self.mm_index),
             f"--command={cmd}", f"--timeout={mm_timeout}"],
            timeout=timeout + 5,
        )
        blob = (out + err)
        if rc != 0:
            if self._DEBUG_MODE_HINT in blob.lower():
                raise ATError("mmcli refused: ModemManager is not in debug mode")
            raise ATError(f"mmcli --command failed (rc={rc}): {blob.strip()[:200]}")

        # Output looks like:  response: '+CGDCONT: 1,"IP","wholesale",...'
        text = out.strip()
        m = re.search(r"response:\s*(.*)", text, re.S)
        if m:
            text = m.group(1).strip()
        if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
            text = text[1:-1]
        # mmcli strips the trailing OK, so synthesize one - callers check
        # .ok and the modem did in fact succeed if mmcli returned rc 0.
        if not any(_is_final(ln) for ln in text.replace("\r", "\n").split("\n")):
            text = text + "\nOK"
        return ATResponse(text)


# ---------------------------------------------------------------------------
# Getting ModemManager out of the way
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def modemmanager_debug_logging(cfg: dict, enabled: bool = True):
    """Temporarily raises MM's log level to DEBUG so `mmcli --command` is
    permitted, and lowers it again on the way out.

    Caveat worth knowing: MM exposes no "read current log level" call, so
    this restores to `modemmanager_restore_log_level` (default INFO), not to
    whatever was set before. If you deliberately run MM with -d, set that
    config key to "DEBUG"."""
    if not enabled:
        yield False
        return

    rc, _, err = run(["mmcli", "--set-logging=DEBUG"], timeout=10)
    if rc != 0:
        log.warning(f"Could not set ModemManager logging to DEBUG: {err.strip()[:200]}")
        yield False
        return

    log.info("ModemManager log level temporarily set to DEBUG (needed for mmcli --command)")
    try:
        yield True
    finally:
        level = cfg["modemmanager_restore_log_level"]
        rc, _, err = run(["mmcli", f"--set-logging={level}"], timeout=10)
        if rc != 0:
            log.warning(f"Failed to restore ModemManager log level to {level}: "
                        f"{err.strip()[:200]} - it will stay verbose until MM restarts")
        else:
            log.info(f"ModemManager log level restored to {level}")


def _wait_for_inhibit(proc, grace: float) -> bool:
    """Decides whether `mmcli --inhibit` took hold.

    The reliable signal is the process *staying alive*: mmcli blocks forever
    once it's holding the inhibition and exits immediately if it couldn't get
    it. Its confirmation line is only a bonus - mmcli's stdout is fully
    buffered when it's a pipe rather than a tty, so that line often doesn't
    flush until the process exits, and waiting for it would mean waiting
    forever on success."""
    deadline = time.monotonic() + grace
    seen = ""
    fd = proc.stdout.fileno()
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            # Exited early == it failed; whatever it printed is the reason.
            try:
                seen += (proc.stdout.read() or b"").decode("utf-8", "replace")
            except (OSError, ValueError):
                pass
            log.warning(f"mmcli --inhibit exited immediately: {seen.strip()[:200] or '(no output)'}")
            return False

        r, _, _ = select.select([fd], [], [], 0.25)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                chunk = b""
            if chunk:
                seen += chunk.decode("utf-8", "replace")
                if "inhibit" in seen.lower() and "error" not in seen.lower():
                    return True

    # Still running after the grace period with no error: it's holding it.
    return True


@contextlib.contextmanager
def modemmanager_released(cfg: dict, mm_index, log_prefix=""):
    """Frees the modem's AT port for direct access, using whichever method
    the config asks for. Yields True if a release actually took effect.

    "inhibit" is the default and only affects the one modem, which matters on
    a bonded multi-modem rig: stopping ModemManager outright would knock the
    *other* modem's link down too."""
    method = cfg["port_release"]

    if method == "none" or mm_index is None:
        yield False
        return

    if method == "stop-modemmanager":
        log.warning(f"{log_prefix}Stopping ModemManager entirely - this drops "
                    f"EVERY modem on this box, not just this one")
        rc, _, err = run(["systemctl", "stop", "ModemManager"], timeout=30)
        if rc != 0:
            log.warning(f"{log_prefix}Could not stop ModemManager: {err.strip()[:200]}")
            yield False
            return
        time.sleep(2.0)  # let it actually let go of the ttys
        try:
            yield True
        finally:
            rc, _, err = run(["systemctl", "start", "ModemManager"], timeout=30)
            if rc != 0:
                log.error(f"{log_prefix}FAILED to restart ModemManager: "
                          f"{err.strip()[:200]} - run 'systemctl start ModemManager' manually")
            else:
                log.info(f"{log_prefix}ModemManager restarted")
        return

    # Default: per-device inhibition.
    try:
        proc = subprocess.Popen(
            ["mmcli", "-m", str(mm_index), "--inhibit"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        )
    except FileNotFoundError:
        log.warning(f"{log_prefix}mmcli not found; accessing the AT port without "
                    f"inhibiting ModemManager")
        yield False
        return

    ok = _wait_for_inhibit(proc, grace=3.0)
    if ok:
        log.info(f"{log_prefix}ModemManager inhibited for this device; AT port is ours")
    else:
        log.warning(f"{log_prefix}Could not inhibit ModemManager; continuing anyway, but "
                    f"MM may interleave its own commands on this port")
    try:
        yield ok
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
            proc.wait(timeout=5)
        except Exception:  # pragma: no cover - defensive
            pass
        try:
            proc.stdout.close()
        except Exception:  # pragma: no cover - defensive
            pass
        log.info(f"{log_prefix}Released ModemManager inhibition; MM will re-probe the modem")


# ---------------------------------------------------------------------------
# Modem discovery
# ---------------------------------------------------------------------------

class Modem:
    """One physical modem, plus everything we managed to learn about it."""

    def __init__(self, mm_index=None):
        self.mm_index = mm_index
        self.manufacturer = None
        self.model = None
        self.revision = None
        self.imei = None
        self.sysfs = None
        self.at_ports = []      # candidate AT ttys, best guess first
        self.qmi_port = None    # /dev/cdc-wdmN, for the qmicli fallback
        self.sim_index = None
        self.iccid = None
        self.imsi = None
        # The SIM's HOME network (MCC+MNC), read from the SIM itself. This is
        # what APN selection must key on.
        self.sim_operator_code = None
        # The network the modem is currently REGISTERED on. Deliberately kept
        # separate: when roaming - which is the entire point of a BICS-style
        # global SIM - the serving network is not the home network, and
        # matching an APN against it would pick the wrong carrier's APN.
        self.registered_operator_code = None
        self.operator_name = None
        # MNC length (2 or 3) from the SIM's EF_AD. Without it, splitting an
        # IMSI into MCC+MNC is genuinely ambiguous - "310260..." could be
        # MCC 310 / MNC 26 or MCC 310 / MNC 260.
        self.mnc_length = None
        self.mm_state = None
        self.mm_reg_state = None
        self.vendor_id = None

    @property
    def label(self) -> str:
        """Stable-ish human name for logs. The mmcli index shifts around on
        hotplug, so the IMEI is included - that's what actually identifies a
        unit across replug and across reboots."""
        bits = [f"modem {self.mm_index}" if self.mm_index is not None else "modem ?"]
        if self.model:
            bits.append(self.model)
        if self.imei:
            bits.append(f"IMEI {self.imei}")
        if self.at_ports:
            bits.append(self.at_ports[0])
        return " / ".join(bits)

    @property
    def is_sierra(self) -> bool:
        blob = " ".join(filter(None, [self.manufacturer, self.model, self.revision]))
        return bool(_SIERRA_HINT_RE.search(blob))


_PORT_RE = re.compile(r"^(\S+)\s+\((\w+)\)$")


def _parse_ports(port_list):
    """mmcli reports ports as ['ttyUSB2 (at)', 'cdc-wdm0 (qmi)', 'wwan0 (net)'].
    Returns (at_ports, qmi_port) as full /dev paths."""
    at_ports, qmi_port = [], None
    for entry in port_list or []:
        m = _PORT_RE.match(str(entry).strip())
        if not m:
            continue
        name, kind = m.group(1), m.group(2).lower()
        if kind == "at":
            at_ports.append(f"/dev/{name}")
        elif kind in ("qmi", "mbim") and qmi_port is None:
            qmi_port = f"/dev/{name}"
    return at_ports, qmi_port


def _read_sysfs_vendor(sysfs_path):
    """Walks up from MM's reported sysfs path looking for a USB device node
    with an idVendor, so rebranded modules (Dell/HP) can still be identified
    even if their manufacturer string doesn't say Sierra."""
    if not sysfs_path:
        return None
    path = sysfs_path
    for _ in range(6):
        candidate = os.path.join(path, "idVendor")
        if os.path.exists(candidate):
            try:
                with open(candidate) as f:
                    return f.read().strip().lower()
            except OSError:
                return None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def discover_modems(cfg: dict):
    """Enumerates every modem ModemManager knows about, resolving each one's
    AT port and SIM details. Returns a list of Modem objects (Sierra ones
    first, since those are the ones this script can act on)."""
    listing = mmcli_json(["-L"])
    if listing is None:
        log.error("Could not talk to ModemManager (`mmcli -L` failed). "
                  "Is the ModemManager service running?")
        return []

    paths = listing.get("modem-list") or []
    if not paths:
        log.info("ModemManager reports no modems connected")
        return []

    modems = []
    for path in paths:
        idx_match = re.search(r"/(\d+)$", str(path))
        if not idx_match:
            continue
        modem = _describe_modem(int(idx_match.group(1)), cfg)
        if modem:
            modems.append(modem)

    modems.sort(key=lambda m: (not m.is_sierra, m.mm_index if m.mm_index is not None else 0))
    return modems


def _describe_modem(index: int, cfg: dict):
    data = mmcli_json(["-m", str(index)])
    if data is None:
        log.warning(f"Could not read details for modem {index}; skipping it")
        return None

    m = Modem(mm_index=index)
    m.manufacturer = _dig(data, "modem", "generic", "manufacturer")
    m.model = _dig(data, "modem", "generic", "model")
    m.revision = _dig(data, "modem", "generic", "revision")
    m.sysfs = _dig(data, "modem", "generic", "device")
    m.mm_state = _dig(data, "modem", "generic", "state")
    m.imei = (_dig(data, "modem", "3gpp", "imei")
              or _dig(data, "modem", "generic", "equipment-identifier"))
    m.mm_reg_state = _dig(data, "modem", "3gpp", "registration-state")
    m.registered_operator_code = _dig(data, "modem", "3gpp", "operator-code")
    m.operator_name = _dig(data, "modem", "3gpp", "operator-name")

    ports = _dig(data, "modem", "generic", "ports") or []
    m.at_ports, m.qmi_port = _parse_ports(ports)
    m.vendor_id = _read_sysfs_vendor(m.sysfs)

    # Sierra rebrands: trust the USB vendor ID as well as the model string.
    if not m.is_sierra and m.vendor_id in [v.lower() for v in cfg["usb_vendor_ids"]]:
        log.debug(f"modem {index}: matched by USB vendor id {m.vendor_id}")
        m.manufacturer = m.manufacturer or f"(USB vendor {m.vendor_id})"

    sim_path = _dig(data, "modem", "generic", "sim")
    if sim_path:
        sim_idx = re.search(r"/(\d+)$", str(sim_path))
        if sim_idx:
            m.sim_index = int(sim_idx.group(1))
    _load_sim_details(m)
    return m


def _load_sim_details(m: Modem):
    """Fills in ICCID/IMSI, trying mmcli, then qmicli, then the AT port.

    Three tiers because each one has a real failure case seen on this
    hardware: mmcli can report "--" for a SIM it hasn't finished probing;
    qmicli needs the QMI port that MBIM-mode firmware doesn't expose; and the
    AT port is sometimes the only thing that answers on a modem stuck in a
    half-initialized state - which is exactly the state this script is for."""
    if m.sim_index is not None:
        sim = mmcli_json(["-m", str(m.mm_index), f"--sim={m.sim_index}"])
        if sim:
            m.iccid = _dig(sim, "sim", "properties", "iccid") or _dig(sim, "sim", "iccid")
            m.imsi = _dig(sim, "sim", "properties", "imsi") or _dig(sim, "sim", "imsi")
            # This one comes from the SIM, so it IS the home PLMN, already
            # split correctly by ModemManager - the best source we have.
            m.sim_operator_code = (_dig(sim, "sim", "properties", "operator-code")
                                   or _dig(sim, "sim", "operator-code"))
            m.operator_name = (_dig(sim, "sim", "properties", "operator-name")
                               or m.operator_name)

    if m.iccid and m.imsi and m.sim_operator_code:
        return

    if m.qmi_port:
        _load_sim_details_qmi(m)


def _bcd_swap(hex_bytes):
    """Decodes the nibble-swapped BCD that SIM elementary files use.
    0x89 0x01 0x24 -> "981042..." reversed per byte -> "98" "10" "42" ->
    digits are (low nibble, high nibble) of each byte. Trailing 'f' padding
    is stripped."""
    digits = []
    for b in hex_bytes:
        digits.append(f"{b & 0x0F:X}")
        digits.append(f"{b >> 4:X}")
    text = "".join(digits).lower()
    return text.rstrip("f")


def _qmi_read_transparent(port: str, spec: str):
    """`qmicli --uim-read-transparent=<path>` dumps 'Read result:' followed by
    colon-separated hex bytes. Returns them as a list of ints, or None."""
    rc, out, err = run(
        ["qmicli", "-d", port, "-p", f"--uim-read-transparent={spec}"], timeout=20
    )
    if rc != 0:
        log.debug(f"qmicli --uim-read-transparent={spec} failed: {err.strip()[:200]}")
        return None
    m = re.search(r"Read result:\s*(.+)", out, re.S)
    if not m:
        return None
    hex_text = re.findall(r"\b[0-9A-Fa-f]{2}\b", m.group(1))
    try:
        return [int(h, 16) for h in hex_text]
    except ValueError:
        return None


def _load_sim_details_qmi(m: Modem):
    """qmicli fallback. Checks the card is actually present first, then reads
    EF_ICCID (3F00/2FE2) and EF_IMSI (3F00/7FFF/6F07) straight off the SIM."""
    rc, out, _ = run(["qmicli", "-d", m.qmi_port, "-p", "--uim-get-card-status"], timeout=20)
    if rc == 0 and "present" not in out.lower():
        log.warning(f"{m.label}: qmicli reports no SIM card present")
        return

    if not m.iccid:
        raw = _qmi_read_transparent(m.qmi_port, "0x3F00,0x2FE2")
        if raw:
            m.iccid = _bcd_swap(raw)
            log.debug(f"{m.label}: ICCID via qmicli = {m.iccid}")

    if not m.imsi:
        raw = _qmi_read_transparent(m.qmi_port, "0x3F00,0x7FFF,0x6F07")
        # EF_IMSI is a length byte followed by 8 packed bytes. In the first
        # packed byte the low nibble is a parity/odd-even flag, not a digit,
        # so it's dropped - only the high nibble is the leading IMSI digit.
        if raw and len(raw) >= 2:
            m.imsi = _bcd_swap(raw[1:])[1:]
            log.debug(f"{m.label}: IMSI via qmicli = {m.imsi}")

    if m.mnc_length is None:
        # EF_AD (administrative data). Byte 4 - when present - is the length
        # of the MNC in the home PLMN, which is the only authoritative way to
        # split an IMSI into MCC+MNC. Both 2 and 3 digit MNCs exist under
        # MCC 310, so without this the split is a guess.
        raw = _qmi_read_transparent(m.qmi_port, "0x3F00,0x7FFF,0x6FAD")
        if raw and len(raw) >= 4 and raw[3] in (2, 3):
            m.mnc_length = raw[3]
            log.debug(f"{m.label}: MNC length via EF_AD = {m.mnc_length}")


def load_sim_details_via_at(m: Modem, transport):
    """Last-resort SIM read over the AT port, used when neither mmcli nor
    qmicli produced an ICCID/IMSI. Only called once a transport is already
    open, so it costs nothing extra."""
    if not m.imsi:
        try:
            resp = transport.command("AT+CIMI")
            for line in resp.lines:
                if re.fullmatch(r"\d{14,15}", line):
                    m.imsi = line
                    log.info(f"{m.label}: IMSI read over AT port = {m.imsi}")
                    break
        except ATError as e:
            log.debug(f"{m.label}: AT+CIMI failed: {e}")

    if not m.iccid:
        for cmd in ("AT+ICCID", "AT!ICCID?", "AT+CCID"):
            try:
                resp = transport.command(cmd)
            except ATError:
                continue
            if not resp.ok:
                continue
            for line in resp.lines:
                digits = re.sub(r"[^0-9]", "", line)
                if len(digits) >= 18:
                    m.iccid = digits
                    log.info(f"{m.label}: ICCID read over AT port ({cmd}) = {m.iccid}")
                    return


# ---------------------------------------------------------------------------
# SIM -> APN resolution
# ---------------------------------------------------------------------------

def _carrier_variant(carrier_entry: dict, cfg: dict, want=None):
    """Picks which APN field of a carrier table entry to use.

    Carrier entries carry several: `apn` (the normal one), `apn_5g`,
    `apn_hotspot` (AT&T's "Broadband" for tablets and standalone modems),
    `apn_alt` (Cricket's ndo/endo pair). `want` names one explicitly;
    otherwise the first field present in apn_variant_order wins.

    Returns (apn, variant_name) or (None, None)."""
    if want:
        value = carrier_entry.get(want)
        return (value, want) if value else (None, None)
    for variant in cfg["apn_variant_order"]:
        value = carrier_entry.get(variant)
        if value:
            return value, variant
    return None, None


def _entry_apn(entry, cfg: dict = None):
    """Normalizes one map value into {'apn': ..., ...}.

    Three accepted forms:
      "america.bics"                              - a bare APN string
      {"apn": "wholesale", "label": "Tello"}      - explicit
      {"carrier": "AT&T", "variant": "apn_hotspot"}
                                                  - points into the carrier
                                                    table, so an MVNO or a
                                                    non-default variant can
                                                    reuse the shipped values
                                                    instead of copying them.
    """
    if isinstance(entry, str):
        return {"apn": entry}
    if not isinstance(entry, dict):
        return None

    if entry.get("apn") is not None:
        return dict(entry)

    carrier_name = entry.get("carrier")
    if carrier_name and cfg:
        carrier = (cfg["carriers"] or {}).get(carrier_name)
        if not carrier:
            log.warning(f"Config references unknown carrier '{carrier_name}'; "
                        f"known carriers: {', '.join(sorted(cfg['carriers'] or {}))}")
            return None
        apn, variant = _carrier_variant(carrier, cfg, entry.get("variant"))
        if not apn:
            log.warning(f"Carrier '{carrier_name}' has no usable APN field "
                        f"(wanted {entry.get('variant') or cfg['apn_variant_order']})")
            return None
        out = {"apn": apn,
               "label": entry.get("label") or f"{carrier_name} ({variant})"}
        if entry.get("pdp_type"):
            out["pdp_type"] = entry["pdp_type"]
        return out

    return None


def _plmn_candidates(m):
    """Home-PLMN readings for this SIM, most trustworthy first.

    Prefers ModemManager's SIM operator-code (read from the SIM and already
    split correctly). Falls back to slicing the IMSI - exactly, if EF_AD gave
    us the MNC length, otherwise trying the 3-digit reading before the
    2-digit one since a 5-char key is a prefix of a 6-char one."""
    out = []
    if m.sim_operator_code:
        out.append((m.sim_operator_code, "SIM operator-code"))
    if m.imsi:
        if m.mnc_length in (2, 3) and len(m.imsi) >= 3 + m.mnc_length:
            out.append((m.imsi[:3 + m.mnc_length], f"IMSI + EF_AD MNC len {m.mnc_length}"))
        else:
            for length in (6, 5):
                if len(m.imsi) >= length:
                    out.append((m.imsi[:length], f"IMSI first {length}"))

    seen, unique = set(), []
    for plmn, how in out:
        if plmn not in seen:
            seen.add(plmn)
            unique.append((plmn, how))
    return unique


def resolve_expected_apn(cfg: dict, m):
    """Works out which APN this SIM should be using.

    Order comes from `match_order`, default:
        iccid -> imsi_plmn -> carrier -> default

    ICCID deliberately outranks the carrier table, and that ordering is load
    bearing rather than stylistic. The table is keyed on the *host network's*
    PLMN, but an MVNO SIM broadcasts the host's PLMN while needing a
    different APN: a Tello SIM reports 310260 (T-Mobile) and would match the
    T-Mobile row, yet its actual APN is `wholesale`, not
    `fast.t-mobile.com`. Cricket is the same story on AT&T's PLMNs - it needs
    `ndo`/`endo`, not `NXTGENPHONE`. If the carrier table were consulted
    first, every MVNO would match its host carrier's row and the per-ICCID
    override would never be reached for precisely the SIMs it exists to
    serve. Reorder `match_order` if you disagree, but that's the trade.

    Matching keys on the SIM's HOME PLMN, never on the network it's currently
    registered to - a roaming SIM (a BICS-style global SIM is always roaming)
    would otherwise match whichever local carrier it happened to camp on.

    Returns (entry_dict, matched_by) or (None, None)."""
    steps = cfg["match_order"] or ["iccid", "imsi_plmn", "carrier", "default"]

    for step in steps:
        if step == "iccid" and m.iccid:
            prefixes = cfg["apn_by_iccid_prefix"] or {}
            # Longest prefix wins, so one specific SIM can override a rule
            # written for a whole batch.
            for prefix in sorted(prefixes, key=len, reverse=True):
                if m.iccid.startswith(str(prefix)):
                    entry = _entry_apn(prefixes[prefix], cfg)
                    if entry:
                        return entry, f"ICCID prefix {prefix}"

        elif step == "imsi_plmn":
            plmns = cfg["apn_by_imsi_plmn"] or {}
            for plmn, how in _plmn_candidates(m):
                if plmn in plmns:
                    entry = _entry_apn(plmns[plmn], cfg)
                    if entry:
                        return entry, f"home PLMN {plmn} via {how}"

        elif step == "carrier":
            carriers = cfg["carriers"] or {}
            for plmn, how in _plmn_candidates(m):
                for name, carrier in carriers.items():
                    if not isinstance(carrier, dict):
                        continue
                    if plmn not in (carrier.get("mcc_mnc_prefixes") or []):
                        continue
                    apn, variant = _carrier_variant(carrier, cfg)
                    if not apn:
                        log.warning(f"Carrier table entry '{name}' matched PLMN {plmn} "
                                    f"but has no field from {cfg['apn_variant_order']}")
                        continue
                    if carrier.get("notes"):
                        log.info(f"Carrier note for {name}: {carrier['notes']}")
                    return ({"apn": apn, "label": f"{name} ({variant})"},
                            f"carrier table, home PLMN {plmn} via {how}")

        elif step == "default" and cfg["default_apn"]:
            entry = _entry_apn(cfg["default_apn"], cfg)
            if entry:
                return entry, "default_apn"

    return None, None


def warn_if_table_stale(cfg: dict):
    """Carrier APNs change. This nags rather than trusting the table forever.

    Only a warning: a stale table is still far better than no table, and this
    must never be a reason to refuse to fix a modem in the field."""
    reviewed = cfg.get("_apn_table_reviewed")
    if not cfg["carriers"]:
        return
    if not reviewed:
        log.info("Carrier APN table has no _apn_table_reviewed date - consider "
                 "spot-checking it against your carriers' current documentation")
        return
    m = re.match(r"(\d{4})-(\d{2})", str(reviewed))
    if not m:
        log.warning(f"_apn_table_reviewed ('{reviewed}') is not YYYY-MM; ignoring")
        return
    months = ((time.gmtime().tm_year - int(m.group(1))) * 12
              + (time.gmtime().tm_mon - int(m.group(2))))
    if months >= 12:
        log.warning(f"Carrier APN table was last reviewed {reviewed} ({months} months "
                    f"ago). Carrier APNs change, networks get retired and MVNOs get "
                    f"acquired - spot-check it before trusting a failure to it.")


# ---------------------------------------------------------------------------
# AT command parsing
# ---------------------------------------------------------------------------

_CGDCONT_RE = re.compile(r'\+CGDCONT:\s*(\d+)\s*,\s*"([^"]*)"\s*,\s*"([^"]*)"')


def parse_cgdcont(text: str, profile: int):
    """Pulls (pdp_type, apn) for the given profile out of an AT+CGDCONT?
    reply. Returns (None, None) if the profile isn't listed at all - which is
    itself a finding, not an error: an unset attach profile is one of the
    exact failure states this script repairs."""
    for m in _CGDCONT_RE.finditer(text):
        if int(m.group(1)) == profile:
            return m.group(2).strip(), m.group(3).strip()
    return None, None


def parse_gstatus(text: str) -> dict:
    """Extracts the fields that matter from AT!GSTATUS?.

    Real EM7455 output packs two fields per line and pads with runs of
    spaces, e.g.
        System mode:   LTE               PS state:    Attached
        EMM state:     Registered        Normal Service
    so each field is picked out individually rather than parsed line by
    line."""
    out = {"emm_state": None, "emm_substate": None, "ps_state": None,
           "system_mode": None, "mode": None}

    m = re.search(r"EMM state:\s*(.+)", text)
    if m:
        parts = re.sub(r"\s+", " ", m.group(1)).strip().split(" ", 1)
        out["emm_state"] = parts[0]
        out["emm_substate"] = parts[1].strip() if len(parts) > 1 else None

    for key, pattern in (("ps_state", r"PS state:\s*(\S+)"),
                         ("system_mode", r"System mode:\s*(\S+)"),
                         ("mode", r"\bMode:\s*(\S+)")):
        m = re.search(pattern, text)
        if m:
            out[key] = m.group(1).strip()
    return out


def is_attached(status: dict) -> bool:
    """The pair that actually proves the attach succeeded. EMM 'Registered'
    on its own is not enough - a modem can be Registered with a Limited
    Service substate and no PS bearer at all, which is precisely what a
    rejected attach looks like."""
    return ((status.get("emm_state") or "").lower() == "registered"
            and (status.get("ps_state") or "").lower() == "attached")


# ---------------------------------------------------------------------------
# The fix itself
# ---------------------------------------------------------------------------

def read_current_apn(transport, profile: int):
    """Returns (pdp_type, apn) for the given profile, either of which may be
    None if the modem doesn't have that profile defined at all."""
    resp = transport.command("AT+CGDCONT?")
    if not resp.ok:
        raise ATError(f"AT+CGDCONT? did not succeed: {resp}")
    # The full reply is worth having in -v output: seeing every profile at
    # once is often what makes an odd modem's behaviour click.
    log.debug(f"AT+CGDCONT? -> {' | '.join(resp.lines)}")
    return parse_cgdcont(resp.raw, profile)


def read_gstatus(transport) -> dict:
    resp = transport.command("AT!GSTATUS?")
    if not resp.ok:
        raise ATError(f"AT!GSTATUS? did not succeed: {resp}")
    return parse_gstatus(resp.raw)


def apply_apn_fix(transport, cfg: dict, apn: str, pdp_type: str, prefix=""):
    """Runs the confirmed-working write sequence. Returns True if the modem
    accepted the CGDCONT write.

    Command order is exactly the sequence verified on two EM7455 units;
    it's kept as-is rather than "improved", because attach-profile behaviour
    across Sierra firmware revisions is not something to guess at."""
    profile = cfg["cgdcont_profile"]
    password = cfg["entercnd_password"]

    # 1. Unlock Sierra's protected command set.
    try:
        resp = transport.command(f'AT!ENTERCND="{password}"')
        if not resp.ok:
            # Some firmware revisions don't gate CGDCONT behind ENTERCND and
            # answer ERROR here. That's not fatal - the write below may still
            # be accepted - so this warns and carries on rather than aborting.
            log.warning(f"{prefix}AT!ENTERCND returned '{resp.final}' - continuing anyway "
                        f"(harmless if this firmware doesn't require it)")
        else:
            log.info(f"{prefix}Protected command set unlocked")
    except ATError as e:
        log.warning(f"{prefix}AT!ENTERCND failed to send ({e}) - continuing anyway")

    # 2. Rewrite the attach profile in NVRAM. This is the actual fix; the
    #    radio cycle below only makes the modem re-attach using it.
    cmd = f'AT+CGDCONT={profile},"{pdp_type}","{apn}"'
    log.info(f"{prefix}Writing attach profile: {cmd}")
    try:
        resp = transport.command(cmd)
    except ATError as e:
        log.error(f"{prefix}Could not send the CGDCONT write: {e}")
        return False
    if not resp.ok:
        log.error(f"{prefix}Modem rejected the CGDCONT write: {resp}")
        return False
    log.info(f"{prefix}Attach profile {profile} written: {pdp_type} / {apn}")

    # 3. Radio off. The modem frequently drops the AT response here as it
    #    tears the stack down, so a missing reply is expected and not treated
    #    as a failure - what matters is whether CFUN=1 brings it back.
    log.info(f"{prefix}Cycling the radio (AT+CFUN=0 -> AT+CFUN=1)")
    try:
        resp = transport.command("AT+CFUN=0")
        if not resp.ok:
            log.warning(f"{prefix}AT+CFUN=0 returned '{resp.final}'")
    except ATError as e:
        log.warning(f"{prefix}AT+CFUN=0 gave no clean reply ({e}) - expected on some "
                    f"firmware, continuing")

    time.sleep(2.0)

    # 4. Radio on, re-attaching with the freshly written APN.
    try:
        resp = transport.command("AT+CFUN=1")
        if not resp.ok:
            log.warning(f"{prefix}AT+CFUN=1 returned '{resp.final}'")
    except ATError as e:
        log.error(f"{prefix}AT+CFUN=1 failed: {e}")
        # Leave the modem in a sane state rather than radio-off: one more
        # attempt after a pause, because a modem parked at CFUN=0 is worse
        # than the problem we came to fix.
        time.sleep(3.0)
        try:
            transport.command("AT+CFUN=1")
            log.info(f"{prefix}AT+CFUN=1 succeeded on retry")
        except ATError as e2:
            log.error(f"{prefix}Retry of AT+CFUN=1 also failed ({e2}). The modem may be "
                      f"left with its radio OFF - power-cycle it or run "
                      f"'AT+CFUN=1' manually")
            return False

    return True


def wait_for_registration(transport, cfg: dict, prefix="") -> tuple:
    """Polls AT!GSTATUS? until the modem is Registered + Attached, or the
    budget runs out. Returns (success, last_status_dict).

    Bounded by design - never loops forever. If it times out, the caller logs
    that clearly and moves on; the modem is left with its radio on and the
    corrected profile written, so it may well attach a few seconds later on
    its own."""
    timeout = cfg["verify_timeout_seconds"]
    interval = cfg["verify_poll_seconds"]

    log.info(f"{prefix}Waiting up to {timeout:.0f}s for the modem to re-attach...")
    time.sleep(cfg["settle_seconds"])

    deadline = time.monotonic() + timeout
    status = {}
    while time.monotonic() < deadline:
        try:
            status = read_gstatus(transport)
        except ATError as e:
            # Normal during the re-attach window - the AT port can be busy or
            # briefly unresponsive while the stack comes back up.
            log.debug(f"{prefix}GSTATUS poll failed (will retry): {e}")
            time.sleep(interval)
            continue

        log.info(f"{prefix}  EMM state={status.get('emm_state')} "
                 f"({status.get('emm_substate')}), PS state={status.get('ps_state')}, "
                 f"system mode={status.get('system_mode')}")

        if is_attached(status):
            substate = (status.get("emm_substate") or "")
            if "limited" in substate.lower():
                log.warning(f"{prefix}Registered and attached, but the EMM substate is "
                            f"'{substate}' - data may still be restricted")
            return True, status

        time.sleep(interval)

    return False, status


# ---------------------------------------------------------------------------
# Per-modem orchestration
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def open_transport(m: Modem, cfg: dict, want_write: bool, prefix=""):
    """Yields an AT transport for this modem, choosing between mmcli and
    direct serial per the `at_method` config, and doing whatever
    ModemManager-wrangling that choice requires.

    On "auto": read-only checks go through mmcli (nothing is disturbed, no
    inhibit, no port stolen), while the write path uses direct serial under
    an inhibit, because `mmcli --command` is not reliable across the
    AT+CFUN=0/1 cycle - MM tears its port handling down when the radio drops
    and the follow-up CFUN=1 lands on a closed port."""
    method = cfg["at_method"]
    if method == "auto":
        method = "serial" if want_write else "mmcli"

    if method == "mmcli":
        if m.mm_index is None:
            log.warning(f"{prefix}No ModemManager index; falling back to the serial transport")
        else:
            with modemmanager_debug_logging(cfg) as debug_on:
                if not debug_on:
                    log.warning(f"{prefix}Could not enable ModemManager debug mode; "
                                f"falling back to the serial transport")
                else:
                    transport = MmcliATTransport(m.mm_index, timeout=cfg["at_timeout_seconds"])
                    # Cheap liveness probe: if this modem doesn't answer a
                    # bare AT, fall through to serial rather than misreporting
                    # the failure as "no APN configured". The probe's
                    # try/except deliberately does NOT wrap the yield below -
                    # an ATError raised by the *caller's* body would otherwise
                    # be swallowed here and this generator would yield a
                    # second time, which contextlib turns into a confusing
                    # RuntimeError.
                    usable = True
                    try:
                        transport.command("AT", timeout=5)
                    except ATError as e:
                        usable = False
                        log.warning(f"{prefix}mmcli transport unusable ({e}); "
                                    f"falling back to the serial transport")
                    if usable:
                        yield transport
                        return

    # Serial path.
    if not m.at_ports:
        raise ATError("no AT port was discovered for this modem")

    with modemmanager_released(cfg, m.mm_index, log_prefix=prefix):
        last_error = None
        # A Sierra modem can expose more than one port typed 'at'; probe each
        # with a bare AT rather than assuming the first one listed is the
        # command port.
        for port in m.at_ports:
            transport = SerialATTransport(port, timeout=cfg["at_timeout_seconds"])
            try:
                transport.open()
                transport.command("AT", timeout=5)
            except ATError as e:
                last_error = e
                log.debug(f"{prefix}{port} did not answer a bare AT: {e}")
                transport.close()
                continue

            log.info(f"{prefix}Using AT port {port}")
            try:
                yield transport
            finally:
                transport.close()
            return

        raise ATError(f"none of {', '.join(m.at_ports)} answered an AT command "
                      f"(last error: {last_error})")


def process_modem(m: Modem, cfg: dict, apply_fix: bool, force: bool) -> dict:
    """Checks (and optionally fixes) one modem. Never raises - every failure
    path returns a result dict, because one bad modem must not stop the other
    one from being handled."""
    prefix = f"[{m.label}] "
    result = {
        "modem": m.mm_index,
        "model": m.model,
        "imei": m.imei,
        "at_port": m.at_ports[0] if m.at_ports else None,
        "iccid": m.iccid,
        "imsi": m.imsi,
        "sim_label": None,
        "expected_apn": None,
        "current_apn": None,
        "matched_by": None,
        "action": "none",
        "registered": None,
        "ok": True,
        "detail": "",
    }

    log.info(f"{prefix}--- checking ---")
    log.info(f"{prefix}manufacturer={m.manufacturer}, model={m.model}, "
             f"revision={m.revision}, MM state={m.mm_state}, "
             f"registration={m.mm_reg_state}")

    if not m.is_sierra and not cfg["allow_non_sierra"]:
        log.info(f"{prefix}Not a Sierra Wireless modem - skipping "
                 f"(AT!ENTERCND/AT!GSTATUS? are Sierra-specific). "
                 f"Set \"allow_non_sierra\": true to override.")
        result["action"] = "skipped-not-sierra"
        return result

    if not m.at_ports:
        log.error(f"{prefix}No AT port found. ModemManager reported no port of type "
                  f"'at' for this device - check `mmcli -m {m.mm_index}` output.")
        result["ok"] = False
        result["action"] = "error"
        result["detail"] = "no AT port"
        return result

    try:
        # Read-only pass first: identify the SIM and read the stored profile.
        with open_transport(m, cfg, want_write=False, prefix=prefix) as transport:
            if not m.iccid or not m.imsi:
                load_sim_details_via_at(m, transport)
                result["iccid"], result["imsi"] = m.iccid, m.imsi

            entry, matched_by = resolve_expected_apn(cfg, m)
            home = ", ".join(f"{p} ({how})" for p, how in _plmn_candidates(m)) or "unknown"
            log.info(f"{prefix}SIM: ICCID={m.iccid or 'unknown'}, "
                     f"IMSI={m.imsi or 'unknown'}, "
                     f"operator={m.operator_name or 'unknown'}")
            log.info(f"{prefix}Home PLMN candidates: {home}; "
                     f"currently registered on {m.registered_operator_code or 'nothing'}")

            if not entry:
                log.error(f"{prefix}No APN mapping matched this SIM, and no carrier in "
                          f"the table claims its home PLMN. Add its ICCID prefix (or the "
                          f"PLMN to a carrier's mcc_mnc_prefixes) in {CONFIG_PATH} - "
                          f"refusing to guess, since writing a wrong attach APN causes "
                          f"exactly the failure this script repairs.")
                result["ok"] = False
                result["action"] = "no-mapping"
                result["detail"] = "SIM not present in the APN map"
                return result

            expected_apn = str(entry["apn"])
            expected_pdp = str(entry.get("pdp_type") or cfg["pdp_type"])
            result["expected_apn"] = expected_apn
            result["sim_label"] = entry.get("label")
            result["matched_by"] = matched_by
            log.info(f"{prefix}Expected APN: '{expected_apn}' ({expected_pdp}) "
                     f"for '{entry.get('label') or 'unlabelled'}' [matched by {matched_by}]")

            profile = cfg["cgdcont_profile"]
            current_pdp, current_apn = read_current_apn(transport, profile)
            result["current_apn"] = current_apn
            log.info(f"{prefix}Modem's stored attach profile {profile}: "
                     f"{current_pdp or 'unset'} / "
                     f"{repr(current_apn) if current_apn is not None else 'unset'}")

            apn_mismatch = (current_apn or "").strip().lower() != expected_apn.strip().lower()
            pdp_mismatch = (cfg["enforce_pdp_type"]
                            and (current_pdp or "").strip().lower() != expected_pdp.strip().lower())

            if not apn_mismatch and not pdp_mismatch and not force:
                log.info(f"{prefix}Internal APN already correct - no action taken")
                result["action"] = "already-correct"
                return result

            if force and not apn_mismatch and not pdp_mismatch:
                log.warning(f"{prefix}Profile already correct, but --force was given; "
                            f"rewriting and cycling the radio anyway")
            else:
                reasons = []
                if apn_mismatch:
                    reasons.append(f"APN {repr(current_apn)} != {repr(expected_apn)}")
                if pdp_mismatch:
                    reasons.append(f"PDP type {repr(current_pdp)} != {repr(expected_pdp)}")
                log.warning(f"{prefix}MISMATCH: {'; '.join(reasons)}")

            if not apply_fix:
                log.info(f"{prefix}Check-only mode - not applying the fix. "
                         f"Re-run with --fix to correct it.")
                result["action"] = "needs-fix"
                return result

        # Write pass. Deliberately a separate transport: on "auto" the check
        # above ran through mmcli, and the write needs direct serial under an
        # inhibit. The read transport is fully closed (and MM's debug logging
        # restored) before this opens.
        with open_transport(m, cfg, want_write=True, prefix=prefix) as transport:
            applied = apply_apn_fix(transport, cfg, expected_apn, expected_pdp, prefix=prefix)
            if not applied:
                result["ok"] = False
                result["action"] = "fix-failed"
                result["detail"] = "the modem did not accept the write sequence"
                return result

            result["action"] = "fixed"
            registered, status = wait_for_registration(transport, cfg, prefix=prefix)
            result["registered"] = registered
            if registered:
                log.info(f"{prefix}SUCCESS: registered and attached "
                         f"(EMM={status.get('emm_state')}/{status.get('emm_substate')}, "
                         f"PS={status.get('ps_state')}, mode={status.get('system_mode')})")
            else:
                result["ok"] = False
                result["detail"] = (f"not attached within "
                                    f"{cfg['verify_timeout_seconds']:.0f}s "
                                    f"(EMM={status.get('emm_state')}, "
                                    f"PS={status.get('ps_state')})")
                log.error(f"{prefix}FAILED to attach within "
                          f"{cfg['verify_timeout_seconds']:.0f}s. Last state: "
                          f"EMM={status.get('emm_state')} "
                          f"({status.get('emm_substate')}), "
                          f"PS={status.get('ps_state')}, "
                          f"system mode={status.get('system_mode')}. "
                          f"The corrected APN IS written and the radio IS on, so it may "
                          f"still attach shortly; if not, verify the APN value itself, "
                          f"the SIM's activation status, and antenna/signal.")

    except ATError as e:
        log.error(f"{prefix}AT communication failed: {e}")
        result["ok"] = False
        result["action"] = "error"
        result["detail"] = str(e)
    except Exception as e:  # pragma: no cover - defensive
        log.exception(f"{prefix}Unexpected error: {e}")
        result["ok"] = False
        result["action"] = "error"
        result["detail"] = str(e)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_NEEDS_FIX = 10
EXIT_FIX_FAILED = 20
EXIT_ERROR = 30


def print_modem_listing(modems):
    for m in modems:
        print(f"modem {m.mm_index}: {m.manufacturer or '?'} {m.model or '?'} "
              f"(rev {m.revision or '?'})")
        print(f"    sierra:     {m.is_sierra} (usb vendor {m.vendor_id or '?'})")
        print(f"    imei:       {m.imei or '?'}")
        print(f"    at ports:   {', '.join(m.at_ports) or 'NONE FOUND'}")
        print(f"    qmi port:   {m.qmi_port or '-'}")
        print(f"    iccid:      {m.iccid or '?'}")
        print(f"    imsi:       {m.imsi or '?'}")
        print(f"    home plmn:  {', '.join(f'{p} ({how})' for p, how in _plmn_candidates(m)) or '?'}")
        print(f"    registered: {m.operator_name or '?'} ({m.registered_operator_code or '?'})")
        print(f"    mm state:   {m.mm_state or '?'} / reg {m.mm_reg_state or '?'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="modem-apn-fix.py",
        description="Check and repair the internal LTE attach APN stored in "
                    "Sierra Wireless modem NVRAM (AT+CGDCONT profile 1).",
        epilog="Exit codes: 0 ok, 10 needs fixing (--check), "
               "20 fix applied but did not register, 30 error.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                      help="report mismatches without changing anything (default)")
    mode.add_argument("--fix", action="store_true",
                      help="apply the fix where the stored APN is wrong")
    mode.add_argument("--list", action="store_true",
                      help="just show what was discovered, then exit")
    parser.add_argument("--modem", type=int, metavar="INDEX",
                        help="operate on a single ModemManager index only")
    parser.add_argument("--config", default=CONFIG_PATH, metavar="PATH",
                        help=f"APN map location (default: {CONFIG_PATH})")
    parser.add_argument("--force", action="store_true",
                        help="rewrite and cycle even if the stored APN already matches")
    parser.add_argument("--json", action="store_true",
                        help="print a machine-readable result summary on stdout")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="debug-level logging")
    args = parser.parse_args(argv)

    if args.verbose:
        log.setLevel(logging.DEBUG)

    cfg = load_config(args.config)
    warn_if_table_stale(cfg)
    modems = discover_modems(cfg)

    if args.modem is not None:
        modems = [m for m in modems if m.mm_index == args.modem]
        if not modems:
            log.error(f"No modem with ModemManager index {args.modem}")
            return EXIT_ERROR

    if args.list:
        if not modems:
            print("No modems found.")
            return EXIT_ERROR
        print_modem_listing(modems)
        return EXIT_OK

    if not modems:
        log.error("No modems found - nothing to do")
        return EXIT_ERROR

    if not has_apn_map(cfg):
        log.error(f"No APN mappings configured in {args.config}. Copy "
                  f"modem-apn.example.json there and fill in your SIMs.")
        return EXIT_ERROR

    log.info(f"Found {len(modems)} modem(s); "
             f"{sum(1 for m in modems if m.is_sierra)} Sierra Wireless")

    results = [process_modem(m, cfg, apply_fix=args.fix, force=args.force)
               for m in modems]

    # Summary block - this is what you want in the journal when debugging
    # after the fact, without having to reconstruct it from the step logs.
    log.info("=== summary ===")
    for r in results:
        log.info(f"modem {r['modem']} ({r['model'] or '?'}): action={r['action']}, "
                 f"sim={r['sim_label'] or r['iccid'] or 'unknown'}, "
                 f"expected={r['expected_apn'] or '-'}, found={r['current_apn'] or '-'}, "
                 f"registered={r['registered']}"
                 f"{', ' + r['detail'] if r['detail'] else ''}")

    if args.json:
        print(json.dumps({"modems": results}, indent=2))

    if any(r["action"] == "fixed" and not r["ok"] for r in results):
        return EXIT_FIX_FAILED
    if any(r["action"] in ("error", "no-mapping", "fix-failed") for r in results):
        return EXIT_ERROR
    if any(r["action"] == "needs-fix" for r in results):
        return EXIT_NEEDS_FIX
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
