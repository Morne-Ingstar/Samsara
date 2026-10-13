"""Off-by-default, loopback-only MCP status and speech service.

Dual-era HTTP: 2026-07-28 per-request metadata/discovery and legacy
2025-11-25 initialize. JSON responses only; no sessions or SSE.
Sources: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
and /basic/transports/streamable-http; legacy /2025-11-25/basic/lifecycle.
No request, argument, client identity, token, or exception message is logged.
"""
from __future__ import annotations

import base64
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import threading
import time
import unicodedata

from samsara import __version__
from samsara.config_defaults import cfg_get
from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)
PORT = 47832
VERSIONS = ("2026-07-28", "2025-11-25")
MAX_BODY = 16384
MAX_TEXT = 400
REQUEST_TIMEOUT = 2.0
UI_TIMEOUT = 2.0
SPEAK_INTERVAL = 2.0
MAX_PENDING = 3
PENDING_TIMEOUT = 60.0
_METHODS = frozenset({"initialize", "notifications/initialized", "ping",
                      "notifications/cancelled", "server/discover", "tools/list", "tools/call"})
_INFO = {"name": "samsara", "version": __version__}
_CAPABILITIES = {"tools": {"listChanged": False}}
_SERVERS_LOCK = threading.Lock()
TOOLS = [
    {"name": "get_status", "description": "Read Samsara's public operating state.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "speak", "description": "Speak a short plain-text reply at the user's feedback level.",
     "inputSchema": {"type": "object", "properties": {
         "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT}},
         "required": ["text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False}},
]


def _error(code, message):
    return {"code": code, "message": message}


class _HTTPServer(HTTPServer):
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT)
        with self.agent._state_lock:
            self.agent._connection = connection
        return connection, address

    def close_request(self, request):
        with self.agent._state_lock:
            if self.agent._connection is request:
                self.agent._connection = None
        super().close_request(request)

    def handle_error(self, request, client_address):
        logger.warning("[AGENTS] outcome=request_failed")


class _DeadlineReader(io.RawIOBase):
    """One deadline for the entire request, even when bytes keep arriving."""

    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.deadline = time.monotonic() + REQUEST_TIMEOUT

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request deadline")
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class _Handler(BaseHTTPRequestHandler):
    # Close each response: no idle keep-alive connection can monopolize the server.
    protocol_version = "HTTP/1.0"

    def setup(self):
        super().setup()
        self.rfile.close()
        self.rfile = io.BufferedReader(_DeadlineReader(self.connection))

    def log_message(self, *_args):
        pass  # Base handler logs raw paths and headers; deliberately disabled.

    def send_error(self, code, message=None, explain=None):
        self._reply(code)

    def _reply(self, status, payload=None):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if status == 405:
            self.send_header("Allow", "POST")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.close_connection = True
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _guard(self):
        agent = self.server.agent
        if self.headers.get_all("Origin") is not None:
            return 403
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in {
                f"127.0.0.1:{agent.port}", f"localhost:{agent.port}"}:
            return 403
        auth = self.headers.get_all("Authorization", [])
        candidate = auth[0][7:] if len(auth) == 1 and auth[0].startswith("Bearer ") else ""
        with agent._state_lock:
            token = agent._token
        if not token or not hmac.compare_digest(candidate.encode("utf-8"), token.encode("utf-8")):
            return 401
        if not agent.enabled or not getattr(agent.app, "_running", True) or agent._stopping.is_set():
            return 503
        return None

    def _unsupported(self):
        self._reply(self._guard() or 405)

    do_GET = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_HEAD = _unsupported

    def do_POST(self):
        started = time.monotonic()
        method = tool = "none"
        outcome = "refused"
        try:
            denied = self._guard()
            if denied:
                self._reply(denied)
                return
            if self.path != "/mcp":
                self._reply(404)
                return
            if self.headers.get_content_type() != "application/json":
                self._reply(415)
                return
            lengths = self.headers.get_all("Content-Length", [])
            if self.headers.get_all("Transfer-Encoding") or len(lengths) != 1 or not lengths[0].isdigit():
                self._reply(400)
                return
            length = int(lengths[0])
            if length > MAX_BODY:
                self._reply(413)
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._reply(400)
                return
            try:
                request = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeError, RecursionError):
                self._reply(400, {"jsonrpc": "2.0", "id": None, "error": _error(-32700, "Parse error")})
                return
            if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                    or not isinstance(request.get("method"), str)
                    or ("id" in request and (type(request["id"]) not in (str, int)))):
                self._reply(400, {"jsonrpc": "2.0", "id": None, "error": _error(-32600, "Invalid request")})
                return
            method = request["method"] if request["method"] in _METHODS else "unknown"
            params = request.get("params", {})
            if isinstance(params, dict) and params.get("name") in ("speak", "get_status"):
                tool = params["name"]
            status, response = self.server.agent.dispatch(request, self.headers)
            outcome = "error" if response and "error" in response else "accepted"
            if response and "result" in response:
                tool_outcome = response["result"].get("structuredContent", {}).get("status")
                if tool_outcome in {"spoken", "queued", "suppressed"}:
                    outcome = tool_outcome
            self._reply(status, response)
        except (TimeoutError, OSError):
            outcome = "connection_closed"
        except Exception:
            # No traceback: exception messages/locals can contain speech or tokens.
            outcome = "internal_error"
            self._reply(500, {"jsonrpc": "2.0", "id": None, "error": _error(-32603, "Internal error")})
        finally:
            logger.info("[AGENTS] method=%s tool=%s outcome=%s duration_ms=%d",
                        method, tool, outcome, int((time.monotonic() - started) * 1000))


class AgentServer:
    def __init__(self, app, *, profile: Path | None = None, port: int | None = None,
                 clock=None):
        self.app = app
        self._profile = profile
        self._requested_port = port
        self.port = PORT
        self.status = "off"
        self._token = None
        self._http = self._thread = self._connection = None
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stopping = threading.Event()
        self._last_speak = float("-inf")
        self._pending = {}
        self._clock = clock if clock is not None else time.monotonic
        self._generation = 0

    @property
    def enabled(self):
        return (self.app.config.get("command_packs") or {}).get("agents") is True

    def _token_path(self):
        return (self._profile if self._profile is not None else samsara_home_dir()) / "agent_server" / "token.json"

    def _save_token(self, token):
        path = self._token_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = None, path.with_name("token-" + secrets.token_hex(8) + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = None
                json.dump({"version": 1, "token": token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass  # Match the browser bridge's best-effort chmod on Windows.
        finally:
            if fd is not None:
                os.close(fd)
            temporary.unlink(missing_ok=True)

    def _load_token(self):
        path = self._token_path()
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            token = saved.get("token")
            if (saved.get("version") != 1 or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{40,128}", token)):
                raise ValueError("Invalid saved token")
        except (FileNotFoundError, ValueError, AttributeError):
            # Repair missing/damaged data; transient read errors must not unpair clients.
            token = secrets.token_urlsafe(32)
            self._save_token(token)
        self._token = token

    def new_token(self):
        with self._lifecycle_lock, self._state_lock:
            try:
                token = secrets.token_urlsafe(32)
                self._save_token(token)
                self._token = token
                self._generation += 1  # Invalidate any UI work queued with the old token.
                return True
            except (OSError, ValueError):
                logger.warning("[AGENTS] outcome=token_save_failed")
                return False

    def connection_details(self):
        with self._state_lock:
            if self.status != "listening" or not self._token:
                return None
            return f"http://127.0.0.1:{self.port}/mcp\nAuthorization: Bearer {self._token}"

    def start(self):
        with self._lifecycle_lock:
            if not self.enabled:
                return False
            if self._thread is not None and self._thread.is_alive():
                return True
            try:
                port = self._requested_port if self._requested_port is not None else cfg_get(self.app.config, "agent_server.port")
                if type(port) is not int or not 0 <= port <= 65535 or (port == 0 and self._requested_port is None):
                    raise ValueError("Invalid port")
                self._load_token()
                self._http = _HTTPServer(("127.0.0.1", port), _Handler)
                self._http.agent = self
                self.port = self._http.server_port
                self._stopping.clear()
                self._thread = thread_registry.spawn("agent_server.http", self._http.serve_forever,
                                                      kwargs={"poll_interval": 0.05}, daemon=True)
                self.status = "listening"
                return True
            except (OSError, ValueError, TypeError, AttributeError):
                self.status = "unavailable"
                logger.warning("[AGENTS] outcome=start_unavailable")
                if self._http is not None:
                    self._http.server_close()
                self._http = None
                return False

    def stop(self):
        with self._lifecycle_lock:
            with self._state_lock:
                self._stopping.set()
                self._generation += 1
                self._pending.clear()
                self._last_speak = float("-inf")
                connection = self._connection
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if self._http is not None:
                self._http.shutdown()
                self._http.server_close()
            if self._thread is not None:
                self._thread.join(REQUEST_TIMEOUT + UI_TIMEOUT + 0.5)
            self._http = self._thread = None
            self.status = "off"

    def sync(self):
        if self.enabled:
            return self.start()
        self.stop()
        return False

    def _on_ui(self, callback):
        done = threading.Event()
        gate = threading.Lock()
        result = {}
        generation = self._generation
        deadline = time.monotonic() + UI_TIMEOUT

        def run():
            with gate:
                if (result.get("cancelled") or generation != self._generation
                        or self._stopping.is_set() or time.monotonic() >= deadline):
                    done.set()
                    return
                if not self.enabled or not getattr(self.app, "_running", True):
                    result["cancelled"] = True
                    done.set()
                    return
            try:
                value = callback()
                with gate:
                    if not result.get("cancelled"):
                        result["value"] = value
            except Exception:
                with gate:
                    result["cancelled"] = True
            finally:
                done.set()

        self.app._schedule_ui(run)
        if not done.wait(UI_TIMEOUT):
            with gate:
                result["cancelled"] = True
            raise TimeoutError("UI unavailable")
        if "value" not in result:
            raise TimeoutError("UI unavailable")
        return result["value"]

    def _get_status(self):
        config = self.app.config
        tts = config.get("tts") or {}
        mode = getattr(getattr(self.app, "_session_mode_manager", None), "mode", None)
        mode = getattr(mode, "value", mode)
        # Closed vocabularies prevent a malformed state/config field leaking text.
        mode = mode if mode in {"dictate", "command", "ava"} else (
            "ava" if getattr(self.app, "ava_mode_active", False) else
            "command" if getattr(self.app, "command_mode_active", False) else
            "asleep" if getattr(self.app, "app_state", None) == "asleep" else "dictate")
        feedback = tts.get("feedback_level", "brief")
        feedback = feedback if feedback in {"silent", "brief", "full"} else "brief"
        route = (config.get("ava") or {}).get("provider_policy", "off")
        return {"app_version": __version__,
                "listening": bool(getattr(self.app, "recording", False)
                                  or getattr(self.app, "wake_word_active", False)
                                  or getattr(self.app, "continuous_active", False)),
                "current_mode": mode, "feedback_level": feedback,
                "speech_muted": bool((config.get("accessibility") or {}).get("ava_mute_audio", False)
                                     or tts.get("enabled") is False or feedback == "silent"),
                "ava_route": route if route in {"off", "local", "cloud"} else "off"}

    def _speak(self, text):
        state = self._get_status()
        if state["feedback_level"] == "silent":
            return {"status": "suppressed", "reason": "silent"}
        if state["speech_muted"]:
            return {"status": "suppressed", "reason": "muted"}
        coordinator = getattr(self.app, "audio_coordinator", None)
        if coordinator is None:
            return {"status": "suppressed", "reason": "voice_unavailable"}
        with self._state_lock:
            now = self._clock()
            self._pending = {slot: expiry for slot, expiry in self._pending.items() if expiry > now}
            if len(self._pending) >= MAX_PENDING or now - self._last_speak < SPEAK_INTERVAL:
                return {"status": "suppressed", "reason": "rate_limited"}
            self._last_speak = now
            slot = object()
            self._pending[slot] = now + PENDING_TIMEOUT
        completed = False

        def done():
            nonlocal completed
            with self._state_lock:
                if not completed:
                    completed = True
                    self._pending.pop(slot, None)

        try:
            handle = coordinator.speak(text, category="conversation", interruptible=True, on_done=done)
        except Exception:
            done()
            return {"status": "suppressed", "reason": "voice_unavailable"}
        identity = str(getattr(handle, "utterance_id", ""))
        if identity.startswith("noop-"):
            done()
            return {"status": "suppressed", "reason": {
                "noop-silent": "silent", "noop-ava-muted": "muted",
                "noop-tts-disabled": "voice_unavailable"}.get(identity, "coordinator_suppressed")}
        return {"status": "spoken" if completed else "queued",
                "reason": "completed" if completed else "coordinator"}

    def dispatch(self, request, headers):
        method, params, identity = request["method"], request.get("params", {}), request.get("id")
        response = {"jsonrpc": "2.0", "id": identity}

        def fail(code, message, status=400):
            return status, {**response, "error": _error(code, message)}

        if not isinstance(params, dict):
            return fail(-32602, "Invalid params")
        version = headers.get("MCP-Protocol-Version")
        modern = version == VERSIONS[0]
        if version is not None and version not in VERSIONS:
            error = _error(-32022, "Unsupported protocol version")
            error["data"] = {"requested": version, "supported": list(VERSIONS)}
            return 400, {**response, "error": error}
        if modern:
            meta = params.get("_meta", {})
            if (not isinstance(meta, dict) or meta.get("io.modelcontextprotocol/protocolVersion") != version
                    or headers.get("Mcp-Method") != method):
                return fail(-32020, "Header mismatch")
            if method == "tools/call":
                name = headers.get("Mcp-Name", "")
                if name.startswith("=?base64?") and name.endswith("?="):
                    try:
                        name = base64.b64decode(name[9:-2], validate=True).decode("utf-8")
                    except (ValueError, UnicodeError):
                        return fail(-32020, "Header mismatch")
                if name != params.get("name"):
                    return fail(-32020, "Header mismatch")
        if method in {"notifications/initialized", "notifications/cancelled"} and "id" not in request:
            return 202, None
        if "id" not in request:
            return fail(-32600, "Invalid request")
        if method == "initialize" and not modern:
            result = {"protocolVersion": VERSIONS[1], "capabilities": _CAPABILITIES, "serverInfo": _INFO}
        elif method == "server/discover" and modern:
            result = {"supportedVersions": list(VERSIONS), "capabilities": _CAPABILITIES,
                      "_meta": {"io.modelcontextprotocol/serverInfo": _INFO}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(args, dict):
                return fail(-32602, "Invalid arguments")
            if name == "get_status" and not args:
                callback = self._get_status
            elif name == "speak" and set(args) == {"text"}:
                text = args["text"]
                if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or not text.strip()
                        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text)
                        or re.search(r"<[^>]*>|```", text)):
                    return fail(-32602, "Expected plain text of 1 to 400 characters")
                callback = lambda: self._speak(text)
            else:
                return fail(-32602, "Unknown tool or invalid arguments")
            try:
                value = self._on_ui(callback)
                failed = False
            except TimeoutError:
                value, failed = {"status": "suppressed", "reason": "ui_unavailable"}, True
            result = {"content": [{"type": "text", "text": json.dumps(value)}],
                      "structuredContent": value, "isError": failed}
        else:
            return fail(-32601, "Method not found", 404 if modern else 200)
        if modern:
            result = {**result, "resultType": "complete", "_meta": {
                **result.get("_meta", {}), "io.modelcontextprotocol/serverInfo": _INFO}}
        return 200, {**response, "result": result}


def for_app(app):
    """One server per app, created by boot service or Settings; never on import."""
    with _SERVERS_LOCK:
        server = getattr(app, "_agent_server", None)
        if server is None:
            server = AgentServer(app)
            app._agent_server = server
            thread_registry.on_shutdown(server.stop)
        return server
