"""Samsara Mobile Companion -- subprocess entry point (Phase 3: PWA + controls).

Run as: F:\\envs\\sami\\python.exe -m samsara.mobile.server_proc

This process runs the LAN-facing HTTP server. It NEVER touches Samsara's
COM-based backends (volume.py, media_keys.py) directly -- it isn't even in
the same OS process as the COM-initialized main app. Every control request
is forwarded as JSON-RPC over a 127.0.0.1 socket to the in-process bridge
(bridge.py), which dispatches it to a handler on the main Samsara side.

Every /api/* and /ping route requires a `token` query parameter matching
--http-token. This is a SEPARATE secret from --ipc-token: the IPC token
authenticates this subprocess to the bridge (loopback only, never sent to a
LAN client); the HTTP token authenticates LAN clients (phone, curl) to this
subprocess, since real system controls are exposed on the LAN interface.

The static PWA (index.html, manifest.json, icon.svg -- served from
plugins/commands/mobile/) is intentionally NOT token-gated: index.html is
how a client gets the token in the first place (it's injected into the page
at serve time, replacing a placeholder). This makes the token a "must load
the page" gate rather than a secret against anyone who can already reach
this LAN server and view-source the page -- the real trust boundary is the
LAN itself.

All I/O (socket connect, HTTP bind) happens inside serve()/main(), never at
import time -- this module is safe to import for inspection or testing
without starting a server.
"""

import argparse
import json
import logging
import os
import socket
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# Env vars the supervisor may set before spawning this process (the
# supervisor currently passes these as argv flags instead; env vars remain
# a supported fallback for manual/direct invocation).
ENV_IPC_PORT = "SAMSARA_MOBILE_IPC_PORT"
ENV_IPC_TOKEN = "SAMSARA_MOBILE_IPC_TOKEN"
ENV_HTTP_PORT = "SAMSARA_MOBILE_HTTP_PORT"
ENV_HTTP_HOST = "SAMSARA_MOBILE_HTTP_HOST"
ENV_HTTP_TOKEN = "SAMSARA_MOBILE_HTTP_TOKEN"

DEFAULT_HTTP_PORT = 8742

# Timeout for the single JSON-RPC round trip made per HTTP request.
IPC_CONNECT_TIMEOUT_SECONDS = 5.0
IPC_HOST = "127.0.0.1"

# Transport actions exposed as their own routes (no request body needed --
# the action is the route itself).
TRANSPORT_ROUTES = ("play", "pause", "toggle", "next", "previous")

# Static PWA assets, served from the existing plugins/commands/mobile/
# directory (unrelated to this package -- kept where the frontend already
# lived rather than duplicated).
STATIC_DIR = Path(__file__).resolve().parents[2] / "plugins" / "commands" / "mobile"
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/manifest.json": "manifest.json",
    "/icon.svg": "icon.svg",
}
# Placeholder in index.html that gets replaced with the real HTTP token at
# serve time, so the page's own fetch() calls can authenticate immediately.
TOKEN_PLACEHOLDER = "__SAMSARA_HTTP_TOKEN__"
_NO_CACHE = "no-cache, no-store, must-revalidate"


def _forward_to_bridge(ipc_port, ipc_token, action, params=None):
    """Send one JSON-RPC request to the in-process bridge, return its reply dict."""
    request = {"token": ipc_token, "action": action, "params": params or {}}
    with socket.create_connection((IPC_HOST, ipc_port), timeout=IPC_CONNECT_TIMEOUT_SECONDS) as sock:
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        sock.settimeout(IPC_CONNECT_TIMEOUT_SECONDS)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    try:
        return json.loads(line.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"malformed bridge reply: {e}"}


def make_handler(ipc_port, ipc_token, http_token):
    """Build a BaseHTTPRequestHandler class closed over the bridge/auth info."""

    class MobileRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            logger.debug("[MOBILE-PROC] " + fmt % args)

        def _send_json(self, data, status=200):
            body = json.dumps(data).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self, query):
            return (query.get("token") or [None])[0] == http_token

        def _forward(self, action, params=None):
            try:
                reply = _forward_to_bridge(ipc_port, ipc_token, action, params)
            except OSError as e:
                self._send_json({"ok": False, "error": f"bridge unreachable: {e}"}, 502)
                return
            self._send_json(reply)

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            try:
                return json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return {}

        def _serve_static(self, filename):
            file_path = STATIC_DIR / filename
            try:
                data = file_path.read_bytes()
            except (OSError, FileNotFoundError):
                self.send_error(404)
                return
            if filename == "index.html":
                data = data.replace(TOKEN_PLACEHOLDER.encode("utf-8"), http_token.encode("utf-8"))
            content_type = _guess_mime(file_path.suffix)
            cache_control = _NO_CACHE if file_path.suffix == ".html" else "public, max-age=3600"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            # Static assets are unauthenticated -- see module docstring for why.
            if path in STATIC_FILES:
                self._serve_static(STATIC_FILES[path])
                return

            if not self._authorized(query):
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return

            if path == "/ping":
                self._forward("ping")
                return
            if path == "/api/status":
                self._forward("status")
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if not self._authorized(query):
                self._send_json({"ok": False, "error": "unauthorized"}, 401)
                return

            if path == "/api/volume":
                body = self._read_json_body()
                self._forward("volume_set", {"level": body.get("level")})
                return
            if path == "/api/mute":
                body = self._read_json_body()
                self._forward("mute_set", {"action": body.get("action", "toggle")})
                return
            if path.startswith("/api/app/"):
                action = path[len("/api/app/"):]
                if action not in TRANSPORT_ROUTES:
                    self._send_json({"ok": False, "error": "unknown endpoint"}, 404)
                    return
                body = self._read_json_body()
                params = {"action": action}
                if body.get("app"):
                    params["app"] = body["app"]
                self._forward("transport_app", params)
                return
            if path.startswith("/api/") and path[len("/api/"):] in TRANSPORT_ROUTES:
                action = path[len("/api/"):]
                self._forward("transport", {"action": action})
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    return MobileRequestHandler


def _guess_mime(suffix):
    return {
        ".html": "text/html; charset=utf-8",
        ".json": "application/manifest+json",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
    }.get(suffix, "application/octet-stream")


def get_lan_ip():
    """Best-effort LAN IP via a connect-less UDP socket (no data sent on the wire)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.1)
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Samsara mobile companion subprocess")
    parser.add_argument("--ipc-port", type=int, default=None)
    parser.add_argument("--ipc-token", type=str, default=None)
    parser.add_argument("--http-port", type=int, default=None)
    parser.add_argument("--http-host", type=str, default=None)
    parser.add_argument("--http-token", type=str, default=None)
    return parser.parse_args(argv)


def resolve_config(argv=None):
    """Merge argv flags (preferred) with env var fallbacks into a concrete config."""
    args = parse_args(argv)
    ipc_port = args.ipc_port or int(os.environ.get(ENV_IPC_PORT, "0"))
    ipc_token = args.ipc_token or os.environ.get(ENV_IPC_TOKEN, "")
    http_port = args.http_port or int(os.environ.get(ENV_HTTP_PORT, str(DEFAULT_HTTP_PORT)))
    http_host = args.http_host or os.environ.get(ENV_HTTP_HOST) or get_lan_ip()
    http_token = args.http_token or os.environ.get(ENV_HTTP_TOKEN, "")
    if not ipc_port or not ipc_token:
        raise SystemExit("server_proc requires --ipc-port and --ipc-token (or matching env vars)")
    if not http_token:
        raise SystemExit("server_proc requires --http-token (or matching env var)")
    return ipc_port, ipc_token, http_host, http_port, http_token


def serve(ipc_port, ipc_token, http_host, http_port, http_token):
    handler_cls = make_handler(ipc_port, ipc_token, http_token)
    httpd = HTTPServer((http_host, http_port), handler_cls)
    logger.info(
        "[MOBILE-PROC] Serving on %s:%s, forwarding to bridge at 127.0.0.1:%s",
        http_host, http_port, ipc_port,
    )
    print(f"[MOBILE-PROC] Listening on http://{http_host}:{http_port}", flush=True)
    httpd.serve_forever()


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    ipc_port, ipc_token, http_host, http_port, http_token = resolve_config(argv)
    serve(ipc_port, ipc_token, http_host, http_port, http_token)


if __name__ == "__main__":
    main()
