"""Samsara Mobile Companion -- in-process IPC bridge (Phase 1).

Owns the loopback listener that the mobile subprocess (server_proc.py)
forwards JSON-RPC requests to, and dispatches them to handlers registered by
the main Samsara side. This module performs NO socket or thread work at
import time -- everything happens inside Bridge.start(), called explicitly
by supervisor.py.
"""

import json
import logging
import queue
import socket
import threading
import time

logger = logging.getLogger(__name__)

# Loopback-only: the bridge NEVER listens on a LAN-facing interface. The
# LAN-facing HTTP server lives in the subprocess (server_proc.py); this
# process only talks to that subprocess over 127.0.0.1.
IPC_HOST = "127.0.0.1"

# Newline-delimited JSON framing over TCP: one JSON object per line, in both
# directions. Simple and adequate for the low request volume of a
# remote-control bridge.
_ENCODING = "utf-8"
_MAX_LINE_BYTES = 65536  # cap against a runaway/garbage client

# Socket accept/recv timeouts so listener and connection threads notice a
# stop() request promptly instead of blocking forever.
_ACCEPT_POLL_SECONDS = 0.5
_RECV_TIMEOUT_SECONDS = 5.0

# How long the dispatch worker's queue.get() waits before re-checking the
# stop flag. This is a poll interval, not a blind sleep -- stop() is noticed
# within one tick even with zero traffic.
_DISPATCH_POLL_SECONDS = 0.5


class Bridge:
    """In-process side of the mobile IPC channel.

    Registered handlers execute on a single dedicated dispatch thread
    (`_dispatch_loop`) that lives for the bridge's whole lifetime, separate
    from the accept thread and from any per-connection thread. Phase 1's
    stub "ping" handler doesn't need this, but it is the seam Phase 2
    depends on: COM apartments are thread-affine, so every COM-calling
    handler (volume.py, music.py) must run on the *same* thread every time.
    Routing all dispatch through one persistent worker thread now means
    Phase 2 can CoInitialize that thread once and reuse it, instead of
    re-plumbing dispatch.
    """

    def __init__(self, token):
        self._token = token
        self._handlers = {}
        self._listener_sock = None
        self._accept_thread = None
        self._dispatch_thread = None
        self._request_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._port = None

    def register(self, action, handler):
        """Register a handler: handler(params: dict) -> JSON-serializable dict."""
        self._handlers[action] = handler

    def start(self, host=IPC_HOST, port=0):
        """Bind the loopback listener and start the accept + dispatch threads.

        Returns the bound port. Raises OSError on bind failure -- callers
        (supervisor.py) are responsible for catching that and disabling the
        feature gracefully rather than letting it propagate into app startup.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(8)
        sock.settimeout(_ACCEPT_POLL_SECONDS)
        self._listener_sock = sock
        self._port = sock.getsockname()[1]

        self._stop_event.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True, name="samsara-mobile-bridge-dispatch"
        )
        self._dispatch_thread.start()

        self._accept_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="samsara-mobile-bridge-accept"
        )
        self._accept_thread.start()

        logger.info("[MOBILE-BRIDGE] Listening on %s:%s", host, self._port)
        return self._port

    @property
    def port(self):
        return self._port

    def is_alive(self):
        """True if the dispatch worker -- the thread future COM calls will use -- is running."""
        return bool(self._dispatch_thread and self._dispatch_thread.is_alive())

    def stop(self):
        """Close the listener and stop both threads. Idempotent."""
        self._stop_event.set()
        if self._listener_sock is not None:
            try:
                self._listener_sock.close()
            except OSError:
                pass
            self._listener_sock = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        # Unblock a dispatch loop currently waiting on an empty queue.
        self._request_queue.put(None)
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=2.0)
            self._dispatch_thread = None
        logger.info("[MOBILE-BRIDGE] Stopped")

    # -- internals -----------------------------------------------------------

    def _accept_loop(self):
        while not self._stop_event.is_set():
            try:
                conn, _addr = self._listener_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break  # listener socket closed under us during stop()
            threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True,
                name="samsara-mobile-bridge-conn",
            ).start()

    def _handle_connection(self, conn):
        conn.settimeout(_RECV_TIMEOUT_SECONDS)
        try:
            raw = self._read_line(conn)
            if raw is None:
                return
            response = self._handle_request_line(raw)
            conn.sendall((json.dumps(response) + "\n").encode(_ENCODING))
        except (OSError, socket.timeout) as e:
            logger.debug("[MOBILE-BRIDGE] Connection error: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _read_line(self, conn):
        buf = b""
        while b"\n" not in buf:
            if len(buf) > _MAX_LINE_BYTES:
                return None
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        return buf.split(b"\n", 1)[0]

    def _handle_request_line(self, raw):
        try:
            request = json.loads(raw.decode(_ENCODING))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return {"ok": False, "error": f"malformed request: {e}"}

        if not isinstance(request, dict):
            return {"ok": False, "error": "request must be a JSON object"}

        if request.get("token") != self._token:
            return {"ok": False, "error": "invalid token"}

        action = request.get("action")
        if not action:
            return {"ok": False, "error": "missing 'action'"}

        reply_queue = queue.Queue(maxsize=1)
        self._request_queue.put((action, request.get("params") or {}, reply_queue))
        try:
            return reply_queue.get(timeout=_RECV_TIMEOUT_SECONDS)
        except queue.Empty:
            return {"ok": False, "error": "dispatch timed out"}

    def _dispatch_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._request_queue.get(timeout=_DISPATCH_POLL_SECONDS)
            except queue.Empty:
                continue
            if item is None:  # stop() sentinel
                continue
            action, params, reply_queue = item
            handler = self._handlers.get(action)
            if handler is None:
                reply_queue.put({"ok": False, "error": f"unknown action: {action}"})
                continue
            try:
                result = handler(params)
                reply_queue.put(result)
            except Exception as e:
                # A handler exception must never kill this thread -- it is
                # the thread Phase 2 will pin COM to, and it must outlive
                # any single bad request.
                logger.exception("[MOBILE-BRIDGE] Handler '%s' failed", action)
                reply_queue.put({"ok": False, "error": f"handler exception: {e}"})


def make_ping_handler():
    """Stub handler for Phase 1: proves the round trip without touching COM."""
    def _ping(params):
        return {"ok": True, "pong": time.time()}
    return _ping
