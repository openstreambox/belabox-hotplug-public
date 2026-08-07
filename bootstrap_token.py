#!/usr/bin/env python3
"""
Run this ONCE to get a persistent auth token from belaUI, the same way
the browser does when you check "remember me" on the login page.

Usage:
    python3 bootstrap_token.py <belaui_password>

This connects to belaUI over its local websocket, authenticates with the
password, and saves the returned auth_token to /etc/belabox-hotplug/config.json.
The daemon (belabox_hotplug.py) reads that token on every boot instead of
ever needing the plaintext password.

Copyright (c) 2026 Jason Ardon (W3ndees)
Licensed under the MIT License - see LICENSE in this repository.
"""
import asyncio
import json
import sys
import os

CONFIG_DIR = "/etc/belabox-hotplug"
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# belaUI tries these ports in this order at startup and falls back down the
# list if one is already taken (see its own httpListenPorts in belaUI.js).
# ws:// with no port defaults to 80, which silently hangs forever - with no
# error and no output - if belaUI actually landed on 8080 or 81 instead.
# Trying the same order here means this script finds belaUI wherever it
# actually ended up, rather than assuming port 80.
CANDIDATE_PORTS = [80, 8080, 81]
CONNECT_TIMEOUT = 8.0    # per-port TCP connect timeout
AUTH_TIMEOUT = 10.0      # time to wait for belaUI's auth response once connected

try:
    import websockets
except ImportError:
    print("Missing dependency. Install it with:")
    print("  sudo pip3 install websockets pyudev --break-system-packages")
    sys.exit(1)


async def _try_port(port: int, password: str):
    """Attempts the full connect+auth handshake on one port. Returns the auth
    token, or raises on failure/timeout - never hangs indefinitely, since
    that's exactly the failure mode this replaces."""
    uri = f"ws://127.0.0.1:{port}"
    print(f"Trying {uri} ...")
    async with websockets.connect(uri, open_timeout=CONNECT_TIMEOUT) as ws:
        await ws.send(json.dumps({"auth": {"password": password, "persistent_token": True}}))

        # Wait for the auth response specifically; belaUI may send other
        # messages (config, pipelines, status) around the same time.
        async def wait_for_auth():
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if "auth" in msg:
                    return msg["auth"]
                if "notification" in msg:
                    # A WRONG PASSWORD is reported this way, not as an
                    # {"auth": {"success": false}} message - see tryAuth()
                    # in belaUI.js, which calls notificationSend(conn, "auth",
                    # "error", "Invalid password") instead. Without this
                    # check the script waits for an "auth" key that never
                    # arrives and only ever times out with an empty message,
                    # which looks identical to belaUI being unreachable.
                    for n in msg["notification"].get("show", []):
                        if n.get("name") == "auth":
                            raise RuntimeError(n.get("msg") or "Invalid password")

        auth = await asyncio.wait_for(wait_for_auth(), timeout=AUTH_TIMEOUT)
        if auth.get("success") is True and auth.get("auth_token"):
            print(f"Connected on port {port}")
            return auth["auth_token"], port
        raise RuntimeError(f"Authentication failed: {auth}")


async def get_token(password: str) -> str:
    errors = []
    for port in CANDIDATE_PORTS:
        try:
            token, port = await _try_port(port, password)
            return token, port
        except RuntimeError:
            raise  # wrong password - belaUI DID respond, no point trying other ports
        except (OSError, asyncio.TimeoutError) as e:
            errors.append(f"port {port}: {e}")
            continue

    raise RuntimeError(
        "Could not reach belaUI on any of ports "
        f"{CANDIDATE_PORTS} within {CONNECT_TIMEOUT:.0f}s each.\n"
        + "\n".join(f"  {e}" for e in errors) +
        "\n\nCheck belaUI is actually running and see which port it bound to:\n"
        "  systemctl list-units | grep -i bela\n"
        "  sudo journalctl -u <that unit name> | grep -i listen\n"
        "  sudo ss -tlnp | grep -E ':80|:8080|:81'"
    )


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 bootstrap_token.py <belaui_password>")
        sys.exit(1)

    password = sys.argv[1]

    try:
        token, port = asyncio.run(get_token(password))
    except RuntimeError as e:
        print(f"\nError: {e}")
        sys.exit(1)
    print(f"Got auth token: {token}")

    os.makedirs(CONFIG_DIR, exist_ok=True)

    config = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            config = json.load(f)

    config["auth_token"] = token
    # Record whichever port actually worked, so belabox_hotplug.py connects
    # to the same place without repeating this port hunt on every restart.
    config["belaui_ws_url"] = f"ws://127.0.0.1:{port}"
    config.setdefault("hdmi_device", None)  # None = autodetect the rk_hdmirx node
    config.setdefault("hdmi_poll_interval_seconds", 1.0)
    config.setdefault("settle_delay_seconds", 2.0)
    # Ordered highest-priority-first. "network" is a phone (or anything else)
    # streaming into the box over WiFi via belaUI's own RTMP/SRT ingest.
    # The old string form ("hdmi"/"usb") is still honoured if already present.
    config.setdefault("source_priority", ["network", "hdmi", "usb"])
    config.setdefault("network_fps", 30.0)  # picks among the *_25/30/50/60fps presets
    config.setdefault("keepalive_interval_seconds", 5.0)
    # Sierra Wireless modem internal-APN repair (modem-apn-fix.py). Inert
    # unless that script AND /etc/belabox-hotplug/modem-apn.json both exist,
    # so leaving these at their defaults costs nothing on a modem-less rig.
    config.setdefault("modem_apn_fix_enabled", True)
    config.setdefault("modem_apn_fix_on_start", True)
    config.setdefault("modem_apn_config_path", "/etc/belabox-hotplug/modem-apn.json")
    config.setdefault("modem_settle_delay_seconds", 25.0)
    config.setdefault("modem_fix_timeout_seconds", 300.0)

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    os.chmod(CONFIG_PATH, 0o600)  # token is a secret, lock it down

    print(f"Saved config to {CONFIG_PATH}")
    print("No further editing needed for typical setups: USB capture sources and the")
    print("onboard HDMI-in are both auto-detected. See README.md if you need to override")
    print("hdmi_device or source_priority.")


if __name__ == "__main__":
    main()
