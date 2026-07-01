"""Verification for Phase 3 of the mobile companion subsystem: PWA + app-targeted transport.

Exercises the static file serving (with server-side token injection) and the
app-targeted transport route end-to-end -- no GUI, no full Samsara boot.

Non-invasive by construction: the app-targeted transport check uses a
made-up process name that cannot match any real running app or window, so
it exercises the routing/fallback logic without ever focusing a window or
sending a real keystroke/media command.

Checks:
  1. GET / serves index.html with the real HTTP token injected (no auth
     required -- this is how a client is meant to bootstrap the token).
  2. GET /manifest.json and /icon.svg serve with no auth required.
  3. POST /api/app/toggle with a nonexistent process name returns ok:false
     without crashing (no real window focus/keystroke, no real SMTC call
     matches).
  4. POST /api/app/<bad action> is rejected (unknown transport action).
  5. samsara.mobile.qr.generate_png() returns real PNG bytes for a URL.

Run with: F:\\envs\\sami\\python.exe tools\\mobile_phase3_test.py
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara.mobile.qr import generate_png
from samsara.mobile.supervisor import Supervisor

HTTP_PORT = 18746
HTTP_REQUEST_TIMEOUT_SECONDS = 5.0
FAKE_PROCESS = "totally-fake-app-xyz.exe"


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=HTTP_REQUEST_TIMEOUT_SECONDS) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


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

    try:
        # --- 1: index.html served with token injected, no auth needed ---
        status, content_type, body = _get(HTTP_PORT, "/")
        assert status == 200, f"expected 200 for /, got {status}"
        assert "text/html" in (content_type or ""), f"expected text/html, got {content_type}"
        text = body.decode("utf-8")
        assert "__SAMSARA_HTTP_TOKEN__" not in text, "token placeholder was not substituted"
        assert token in text, "real HTTP token was not injected into index.html"
        print("  [1] PASS: / served index.html with the real HTTP token injected, no auth required")

        # --- 2: other static assets, no auth needed ---
        status, content_type, body = _get(HTTP_PORT, "/manifest.json")
        assert status == 200, f"expected 200 for /manifest.json, got {status}"
        json.loads(body.decode("utf-8"))  # must be valid JSON
        status, content_type, body = _get(HTTP_PORT, "/icon.svg")
        assert status == 200, f"expected 200 for /icon.svg, got {status}"
        assert "svg" in (content_type or ""), f"expected image/svg+xml, got {content_type}"
        print("  [2] PASS: /manifest.json and /icon.svg served without auth")

        # --- 3: app-targeted transport routes safely for a nonexistent app ---
        status, reply = _post(HTTP_PORT, token, "/api/app/toggle", {"app": FAKE_PROCESS})
        assert status == 200, f"expected 200, got {status}: {reply}"
        assert reply.get("ok") is False, f"expected ok:false for a nonexistent app, got {reply}"
        assert reply.get("app") == FAKE_PROCESS, f"expected app echoed back, got {reply}"
        print(f"  [3] PASS: /api/app/toggle routed safely for a nonexistent app: {reply}")

        # --- 4: invalid action on the app-targeted route is rejected ---
        status, reply = _post(HTTP_PORT, token, "/api/app/rewind", {"app": FAKE_PROCESS})
        assert status == 404, f"expected 404 for an unrouted action, got {status}: {reply}"
        print(f"  [4] PASS: /api/app/rewind (not a real route) rejected: {reply}")
    finally:
        supervisor.stop()

    # --- 5: local QR generation produces real PNG bytes ---
    png = generate_png("http://192.168.1.50:8742/")
    assert png is not None, "qrcode package should be installed -- generate_png returned None"
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "generate_png did not return valid PNG data"
    print(f"  [5] PASS: local QR generation produced {len(png)} bytes of real PNG data")


def main():
    try:
        run_checks()
    except AssertionError as e:
        print(f"FAIL: {e}")
        return 1
    print()
    print("RESULT: all Phase 3 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
