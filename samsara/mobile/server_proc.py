"""Samsara Mobile Companion -- subprocess entry point (Phase 1).

Run as: F:\\envs\\sami\\python.exe -m samsara.mobile.server_proc

This process runs the LAN-facing HTTP server. It NEVER touches Samsara's
COM-based backends (volume.py, music.py) directly -- it isn't even in the
same OS process as the COM-initialized main app. Every control request is
forwarded as JSON-RPC over a 127.0.0.1 socket to the in-process bridge
(bridge.py), which dispatches it to a handler on the main Samsara side.

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

logger = logging.getLogger(__name__)

# Env vars the supervisor may set before spawning this process (the
# supervisor currently passes these as argv flags instead; env vars remain
# a supported fallback for manual/direct invocation).
ENV_IPC_PORT = "SAMSARA_MOBILE_IPC_PORT"
ENV_IPC_TOKEN = "SAMSARA_MOBILE_IPC_TOKEN"
ENV_HTTP_PORT = "SAMSARA_MOBILE_HTTP_PORT"
ENV_HTTP_HOST = "SAMSARA_MOBILE_HTTP_HOST"

DEFAULT_HTTP_PORT = 8742

# Timeout for the single JSON-RPC round trip made per HTTP request.
IPC_CONNECT_TIMEOUT_SECONDS = 5.0
IPC_HOST = "127.0.0.1"


def _forward_to_bridge(ipc_port, token, action, params=None):
    """Send one JSON-RPC request to the in-process bridge, return its reply dict."""
    request = {"token": token, "action": action, "params": params or {}}
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


def make_handler(ipc_port, token):
    """Build a BaseHTTPRequestHandler class closed over the bridge connection info."""

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

        def do_GET(self):
            if self.path.rstrip("/") == "/ping":
                try:
                    reply = _forward_to_bridge(ipc_port, token, "ping")
                except OSError as e:
                    self._send_json({"ok": False, "error": f"bridge unreachable: {e}"}, 502)
                    return
                self._send_json(reply)
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    return MobileRequestHandler


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
    return parser.parse_args(argv)


def resolve_config(argv=None):
    """Merge argv flags (preferred) with env var fallbacks into a concrete config."""
    args = parse_args(argv)
    ipc_port = args.ipc_port or int(os.environ.get(ENV_IPC_PORT, "0"))
    token = args.ipc_token or os.environ.get(ENV_IPC_TOKEN, "")
    http_port = args.http_port or int(os.environ.get(ENV_HTTP_PORT, str(DEFAULT_HTTP_PORT)))
    http_host = args.http_host or os.environ.get(ENV_HTTP_HOST) or get_lan_ip()
    if not ipc_port or not token:
        raise SystemExit("server_proc requires --ipc-port and --ipc-token (or matching env vars)")
    return ipc_port, token, http_host, http_port


def serve(ipc_port, token, http_host, http_port):
    handler_cls = make_handler(ipc_port, token)
    httpd = HTTPServer((http_host, http_port), handler_cls)
    logger.info(
        "[MOBILE-PROC] Serving on %s:%s, forwarding to bridge at 127.0.0.1:%s",
        http_host, http_port, ipc_port,
    )
    print(f"[MOBILE-PROC] Listening on http://{http_host}:{http_port}", flush=True)
    httpd.serve_forever()


def main(argv=None):
    logging.basicConfig(level=logging.INFO)
    ipc_port, token, http_host, http_port = resolve_config(argv)
    serve(ipc_port, token, http_host, http_port)


if __name__ == "__main__":
    main()
