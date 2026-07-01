"""Verification for Phase 2 of the mobile companion subsystem: real controls.

Exercises samsara/mobile/handlers.py + the HTTP token layer end-to-end
against the REAL system backends (volume.py's Core Audio singleton,
media_keys.py's foreground SMTC routing) -- no GUI, no full Samsara boot.

Deliberately non-invasive: every check either reads real state, round-trips
a value back to what it already was, or exercises a validation error path
that returns before touching the real backend. It never toggles mute, never
sends a real play/pause/next/previous to whatever is in the foreground, and
never leaves the system volume different from how it started.

Checks:
  1. GET /api/status returns real volume/mute/foreground-app state.
  2. GET /api/status without a token, and with the wrong token, both 401.
  3. POST /api/volume round-trips the CURRENT level back to itself (no
     audible change) -- proves the real Core Audio write path works.
  4. POST /api/volume with a missing 'level' is rejected before touching audio.
  5. POST /api/mute with an invalid action is rejected before touching audio.
  6. The IPC token and HTTP token are different secrets: presenting the HTTP
     token directly to the bridge's IPC port is refused.
  7. The transport handler's action validation is proven directly against
     the bridge (bypassing real media control) -- an unknown action is
     rejected before _run_transport() ever runs. Real play/pause/next/
     previous against the live foreground app is intentionally NOT
     exercised here (see note in report).

Run with: F:\\envs\\sami\\python.exe tools\\mobile_phase2_test.py
"""

import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara.mobile.bridge import Bridge
from samsara.mobile.handlers import make_transport_handler
from samsara.mobile.supervisor import Supervisor

HTTP_PORT = 18745
HTTP_REQUEST_TIMEOUT_SECONDS = 5.0


def _get(port, token, path):
    url = f"http://127.0.0.1:{port}{path}?token={token}" if token is not None else f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _post(port, token, path, body):
    data = json.dumps(body).encode("utf-8")
    url = f"http://127.0.0.1:{port}{path}?token={token}"
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def run_checks():
    supervisor = Supervisor(http_port=HTTP_PORT, http_host="127.0.0.1")
    started = supervisor.start()
    assert started, "supervisor.start() should succeed with a free port"
    token = supervisor.http_token
    ipc_port = supervisor.bridge.port

    try:
        # --- 1: real status read ---
        status, reply = _get(HTTP_PORT, token, "/api/status")
        assert status == 200, f"expected 200, got {status}: {reply}"
        assert reply.get("ok") is True, f"expected ok:true, got {reply}"
        assert reply.get("volume") is None or 0 <= reply["volume"] <= 100, f"bad volume: {reply}"
        assert reply.get("muted") is None or isinstance(reply["muted"], bool), f"bad muted: {reply}"
        print(f"  [1] PASS: /api/status real read -> {reply}")

        # --- 2: auth required ---
        status, reply = _get(HTTP_PORT, None, "/api/status")
        assert status == 401, f"expected 401 with no token, got {status}: {reply}"
        status, reply = _get(HTTP_PORT, "wrong-token", "/api/status")
        assert status == 401, f"expected 401 with wrong token, got {status}: {reply}"
        print("  [2] PASS: /api/status requires the correct HTTP token")

        # --- 3: volume round-trip to its own current value (no audible change) ---
        current_level = _get(HTTP_PORT, token, "/api/status")[1].get("volume")
        if current_level is None:
            print("  [3] SKIP: could not read current volume (no default render device?)")
        else:
            status, set_reply = _post(HTTP_PORT, token, "/api/volume", {"level": current_level})
            assert status == 200 and set_reply.get("ok") is True, f"volume set failed: {set_reply}"
            assert set_reply.get("volume") == current_level, (
                f"round-trip changed volume: was {current_level}, now {set_reply.get('volume')}"
            )
            print(f"  [3] PASS: volume round-tripped to its own value ({current_level}) via real Core Audio write")

        # --- 4: missing level rejected before touching audio ---
        status, reply = _post(HTTP_PORT, token, "/api/volume", {})
        assert reply.get("ok") is False, f"expected ok:false for missing level, got {reply}"
        print(f"  [4] PASS: missing 'level' rejected: {reply}")

        # --- 5: invalid mute action rejected before touching audio ---
        status, reply = _post(HTTP_PORT, token, "/api/mute", {"action": "banana"})
        assert reply.get("ok") is False, f"expected ok:false for invalid mute action, got {reply}"
        print(f"  [5] PASS: invalid mute action rejected: {reply}")

        # --- 6: IPC token and HTTP token are different secrets ---
        request = {"token": token, "action": "ping", "params": {}}
        with socket.create_connection(("127.0.0.1", ipc_port), timeout=5) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.settimeout(5)
            raw = sock.recv(4096)
        ipc_reply = json.loads(raw.decode("utf-8"))
        assert ipc_reply.get("ok") is False, (
            f"HTTP token must NOT authenticate against the IPC bridge, got {ipc_reply}"
        )
        print(f"  [6] PASS: HTTP token rejected by the IPC bridge (separate secrets): {ipc_reply}")
    finally:
        supervisor.stop()

    # --- 7: transport action validation, direct against the bridge (no real media control) ---
    bridge_token = "phase2-test-token"
    bridge = Bridge(bridge_token)
    bridge.register("transport", make_transport_handler())
    try:
        bridge_port = bridge.start(port=0)
        request = {"token": bridge_token, "action": "transport", "params": {"action": "rewind"}}
        with socket.create_connection(("127.0.0.1", bridge_port), timeout=5) as sock:
            sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
            sock.settimeout(5)
            raw = sock.recv(4096)
        reply = json.loads(raw.decode("utf-8"))
        assert reply.get("ok") is False, f"expected ok:false for unknown transport action, got {reply}"
        assert bridge.is_alive(), "bridge dispatch thread must survive an invalid transport action"
        print(f"  [7] PASS: unknown transport action rejected before touching real media control: {reply}")
    finally:
        bridge.stop()


def main():
    try:
        run_checks()
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    print()
    print("RESULT: all Phase 2 checks passed")
    print(
        "NOTE: real play/pause/toggle/next/previous against the live foreground app were "
        "NOT exercised automatically, to avoid disrupting whatever is currently on screen. "
        "Verify manually with, e.g., a POST to /api/toggle once running."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
