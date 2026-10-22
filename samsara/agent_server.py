"""Off-by-default, loopback-only MCP status and speech service.

Dual-era HTTP: 2026-07-28 per-request metadata/discovery and legacy
2025-11-25 initialize. JSON responses only; no sessions or SSE.
Sources: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
and /basic/transports/streamable-http; legacy /2025-11-25/basic/lifecycle.
No request, argument, client identity, token, or exception message is logged.
"""
from __future__ import annotations

import base64
from contextlib import nullcontext
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
from samsara import paste as paste_delivery
from samsara.config_defaults import cfg_get
from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)
PORT = 47832
VERSIONS = ("2026-07-28", "2025-11-25")
MAX_BODY = 16384
# Discard larger refused bodies in small chunks, without growing the body buffer.
MAX_DRAIN = 4 * 1024 * 1024
MAX_TEXT = 400
MAX_FULL_TEXT = 12000
REQUEST_TIMEOUT = 2.0
UI_TIMEOUT = 2.0
SPEAK_INTERVAL = 2.0
MAX_PENDING = 3
PENDING_TIMEOUT = 60.0
MAX_TYPE_TEXT = 2000
TYPE_TIMEOUT = 30.0
SPEAK_QUIET_AFTER_TYPING = 3.0
CONFIRM_ECHO_TAIL = 0.5
UIA_TIMEOUT = 0.35
_UIA_SLOT = threading.BoundedSemaphore(1)
_TERMINAL_PROCESSES = frozenset({
    "putty.exe", "kitty.exe", "mobaxterm.exe", "conemu.exe", "conemu64.exe",
    "tabby.exe", "hyper.exe", "termius.exe", "git-bash.exe",
})
_TERMINAL_CLASSES = frozenset({
    "consolewindowclass", "cascadia_hosting_window_class", "virtualconsoleclass",
    "putty", "kitty", "mintty", "org.wezfurlong.wezterm", "alacritty",
    # Alacritty uses winit's default Win32 class (also used by other winit apps):
    # https://github.com/alacritty/alacritty/blob/master/alacritty/src/display/window.rs
    # https://github.com/rust-windowing/winit/blob/v0.30.12/src/platform_impl/windows/mod.rs
    "tmobaxtermform", "window class",
})
MAX_CONNECTIONS = 8
_TYPE_REASONS = frozenset({"refused_terminal", "refused_elevated", "refused_own_window",
                           "refused_password", "refused_delivery", "refused_unverified", "user_declined", "timeout", "window_changed",
                           "too_long", "invalid_text", "disabled", "busy", "delivered", "unknown"})
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
         "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT},
         "full_text": {"type": "string", "minLength": 1, "maxLength": MAX_FULL_TEXT}},
         "required": ["text"], "additionalProperties": False},
     "annotations": {"readOnlyHint": False, "destructiveHint": False,
                     "idempotentHint": False, "openWorldHint": False}},
]
TYPE_TOOL = {
    "name": "type_text", "description": "Type plain text after the user's spoken approval, without sending.",
    "inputSchema": {"type": "object", "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_TYPE_TEXT}},
        "required": ["text"], "additionalProperties": False},
    "annotations": {"readOnlyHint": False, "destructiveHint": True,
                    "idempotentHint": False, "openWorldHint": True},
}


def _safe_name(value, fallback="An AI app", *, client=True):
    if not isinstance(value, str):
        return fallback
    # Compatibility forms and common Cyrillic/Greek look-alikes must undergo
    # the same word check as ASCII; no dependency on a transliteration library.
    value = unicodedata.normalize("NFKC", value)
    value = "".join(c for c in value if unicodedata.category(c) != "Cf")
    lookalikes = str.maketrans("аеорсхуіјѕΑΒΕΖΗΙΚΜΝΟΡΤΥΧ", "aeopcxyijsABEZHIKMNOPTYX")
    value = value.translate(lookalikes).translate(str.maketrans("АЕОРСХУІЈЅ", "AEOPCXYIJS"))
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    # No punctuation, markup, invisible characters or sentence injection.
    value = " ".join("".join(c if c.isalnum() or c == " " else " " for c in value).split())
    spoken = " ".join(value.split()[:3])[:24].strip()
    if client:
        # Check both the full name and what will actually be spoken: the cut
        # can expose a confirmation word at the end of an otherwise safe name.
        for name in (value, spoken):
            words = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])", " ", name)
            words = re.sub(r"\b(?:[a-z]\s+)+[a-z]\b", lambda m: m[0].replace(" ", ""), words.casefold())
            letters = re.sub(r"[^a-z]", "", words)
            if (any(word in letters for word in ("yes", "confirm", "samsara", "say", "allow"))
                    or "no" in words.split()
                    or any(word.startswith(("safe", "ok")) for word in words.split())):
                return fallback
    return spoken or fallback


def _text_reason(args):
    if not isinstance(args, dict) or set(args) != {"text"}:
        return "invalid_text"
    value = args["text"]
    if not isinstance(value, str):
        return "invalid_text"
    if len(value) > MAX_TYPE_TEXT:
        return "too_long"
    if (not value.strip()
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in value)
            or re.search(r"<[^>]*>|```", value)):
        return "invalid_text"
    return None


def _desktop_allows_typing():
    """Fail closed on inaccessible/non-default input desktops (lock/UAC)."""
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenInputDesktop.restype = wintypes.HANDLE
    api.CloseDesktop.argtypes = [wintypes.HANDLE]
    api.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    desktop = api.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
    if not desktop:
        return False
    try:
        name, needed = ctypes.create_unicode_buffer(256), wintypes.DWORD()
        return bool(api.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name),
                                                  ctypes.byref(needed)) and name.value.lower() == "default")
    finally:
        api.CloseDesktop(desktop)


def _focused_control_reason(pid):
    """Bound UIA provider work off the UI thread; never read control text.

    One hung provider occupies the sole daemon slot, so later requests refuse
    rather than spawning unbounded workers. Unknown/missing metadata is unsafe.
    """
    if not _UIA_SLOT.acquire(blocking=False):
        return "refused_unverified"
    done, result = threading.Event(), {}

    def read():
        try:
            import uiautomation as auto
            initializer = getattr(auto, "UIAutomationInitializerInThread", nullcontext)
            with initializer():
                focused = auto.GetFocusedControl()
                if focused is None:
                    result["reason"] = "refused_unverified"
                elif focused.ProcessId != pid:
                    result["reason"] = "window_changed"
                elif "xterm-helper-textarea" in focused.ClassName.casefold():
                    result["reason"] = "refused_terminal"
                elif focused.IsPassword is True:
                    result["reason"] = "refused_password"
                elif focused.IsPassword is not False:
                    result["reason"] = "refused_unverified"
                else:
                    result["reason"] = None
        except Exception as exc:
            logger.debug("[AGENTS] outcome=uia_unverified exception=%s", type(exc).__name__)
            result["reason"] = "refused_unverified"
        finally:
            _UIA_SLOT.release()
            done.set()

    try:
        thread_registry.spawn("agent_server.uia", read, daemon=True)
    except Exception:
        _UIA_SLOT.release()
        return "refused_unverified"
    return result.get("reason", "refused_unverified") if done.wait(UIA_TIMEOUT) else "refused_unverified"


def _foreground_target():
    """Identity and safety only: never read a title or a control's text."""
    import ctypes
    from ctypes import wintypes
    import psutil
    from samsara import injection_safety
    from samsara.paste import _UNDO_TERMINAL_PROCESSES
    try:
        if not _desktop_allows_typing():
            return {}, "refused_elevated"
        api = ctypes.WinDLL("user32", use_last_error=True)
        api.GetForegroundWindow.restype = wintypes.HWND
        api.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        api.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        hwnd, pid = api.GetForegroundWindow(), wintypes.DWORD()
        if not hwnd or not api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
            return {}, "window_changed"
        process = psutil.Process(pid.value)
        exe = process.name().lower()
        target = {"hwnd": int(hwnd), "pid": pid.value, "birth": process.create_time(),
                  "exe": exe, "app": _safe_name(Path(exe).stem, "the app", client=False)}
        if pid.value == os.getpid() or exe == "samsara.exe":
            return target, "refused_own_window"
        classname = ctypes.create_unicode_buffer(256)
        api.GetClassNameW(hwnd, classname, len(classname))
        if exe in _UNDO_TERMINAL_PROCESSES | _TERMINAL_PROCESSES or classname.value.casefold() in _TERMINAL_CLASSES:
            return target, "refused_terminal"
        verdict = injection_safety.window_integrity(int(hwnd))
        if not verdict.confirmed_ok or exe in {"lockapp.exe", "logonui.exe", "consent.exe"}:
            return target, "refused_elevated"
        reason = _focused_control_reason(pid.value)
        if reason:
            return target, reason
        if api.GetForegroundWindow() != hwnd:
            return target, "window_changed"
        return target, None
    except Exception:
        return {}, "window_changed"


def _error(code, message):
    return {"code": code, "message": message}


class _HTTPServer(HTTPServer):
    allow_reuse_address = False

    def __init__(self, *args):
        super().__init__(*args)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._workers = set()

    def process_request(self, request, client_address):
        # A confirmation must not block tools/list or a second call's busy reply.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return

        def run():
            try:
                self.finish_request(request, client_address)
            except Exception:
                self.handle_error(request, client_address)
            finally:
                self.shutdown_request(request)
                self._slots.release()
                with self.agent._state_lock:
                    self._workers.discard(threading.current_thread())

        try:
            with self.agent._state_lock:
                worker = thread_registry.spawn("agent_server.request", run, daemon=True)
                self._workers.add(worker)
        except Exception:
            self.shutdown_request(request)
            self._slots.release()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT)
        with self.agent._state_lock:
            self.agent._connections.add(connection)
        return connection, address

    def close_request(self, request):
        with self.agent._state_lock:
            self.agent._connections.discard(request)
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

    def _reply(self, status, payload=None, client_id=None):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        if client_id is not None:
            self.send_header("Mcp-Session-Id", client_id)
        if status == 405:
            self.send_header("Allow", "POST")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.close_connection = True
        if data and self.command != "HEAD":
            self.wfile.write(data)
        if status >= 400:
            self._linger_close()

    def _linger_close(self):
        """Deliver refusals before closing with unread bytes (a reset on Windows)."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            remaining = MAX_DRAIN
            # read1 includes BufferedReader's prefetched bytes; its raw reader
            # retains the original whole-request deadline, even for a drip feed.
            while remaining > 0:
                discarded = self.rfile.read1(min(MAX_BODY, remaining))
                if not discarded:
                    break
                remaining -= len(discarded)
        except (TimeoutError, OSError):
            pass  # The byte budget/deadline and disconnected peers bound the close.

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
            self.agent_generation = agent._generation
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
            if isinstance(params, dict) and params.get("name") in ("speak", "get_status", "type_text"):
                tool = params["name"]
            status, response = self.server.agent.dispatch(request, self.headers,
                                                          generation=self.agent_generation)
            outcome = "error" if response and "error" in response else "accepted"
            if response and "result" in response:
                tool_outcome = response["result"].get("structuredContent", {}).get("status")
                if tool_outcome in {"spoken", "queued", "suppressed", "delivered", "refused", "busy", "unknown"}:
                    outcome = tool_outcome
            client_id = None
            if method == "initialize" and response and "result" in response:
                client_id = self.server.agent._remember_client(params.get("clientInfo", {}))
            self._reply(status, response, client_id)
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
        self._http = self._thread = None
        self._connections = set()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._stopping = threading.Event()
        self._last_speak = float("-inf")
        self._pending = {}
        self._clock = clock if clock is not None else time.monotonic
        self._generation = 0
        self._clients = {}
        self._typing_lock = threading.Lock()
        self._typing_op = None
        self._typing_quiet_until = float("-inf")
        self._target_probe = _foreground_target

    @property
    def enabled(self):
        return (self.app.config.get("command_packs") or {}).get("agents") is True

    @property
    def _connection(self):
        """Compatibility view for the existing bounded-shutdown diagnostic."""
        with self._state_lock:
            return next(iter(self._connections), None)

    @property
    def typing_enabled(self):
        return self.enabled and (self.app.config.get("agent_server") or {}).get("allow_typing") is True

    def _remember_client(self, info):
        name = _safe_name(info.get("name") if isinstance(info, dict) else None)
        identity = secrets.token_urlsafe(24)
        with self._state_lock:
            now = self._clock()
            self._clients = {key: value for key, value in self._clients.items() if value[1] > now}
            if len(self._clients) >= 64:
                self._clients.pop(next(iter(self._clients)))
            self._clients[identity] = (name, now + 3600)
        return identity

    def _client_name(self, params, headers):
        if headers.get("MCP-Protocol-Version") == VERSIONS[0]:
            info = params.get("_meta", {}).get("io.modelcontextprotocol/clientInfo", {})
            return _safe_name(info.get("name") if isinstance(info, dict) else None)
        with self._state_lock:
            name, expiry = self._clients.get(headers.get("Mcp-Session-Id"), ("An AI app", 0))
            return name if expiry > self._clock() else "An AI app"

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
                self._clients.clear()
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
                connections = tuple(self._connections)
                self._clients.clear()
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            if self._http is not None:
                self._http.shutdown()
                self._http.server_close()
                with self._state_lock:
                    workers = tuple(self._http._workers)
                deadline = time.monotonic() + UI_TIMEOUT + 0.5
                for worker in workers:
                    worker.join(max(0, deadline - time.monotonic()))
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

    def _speak(self, text, *, caption_text=None, allow_silent=False):
        with self._state_lock:
            if self._typing_lock.locked() or self._clock() < self._typing_quiet_until:
                return {"status": "busy", "reason": "busy"}
        state = self._get_status()
        if state["feedback_level"] == "silent" and not allow_silent:
            return {"status": "suppressed", "reason": "silent"}
        if state["speech_muted"] and state["feedback_level"] != "silent":
            return {"status": "suppressed", "reason": "muted"}
        coordinator = getattr(self.app, "audio_coordinator", None)
        if coordinator is None:
            return {"status": "suppressed", "reason": "voice_unavailable"}
        with self._state_lock:
            if self._typing_lock.locked() or self._clock() < self._typing_quiet_until:
                return {"status": "busy", "reason": "busy"}
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
            if state["feedback_level"] == "silent":
                # The coordinator's Silent decision displays a caption without audio.
                coordinator.speak(caption_text or text, category="conversation", interruptible=True)
                done()
                return {"status": "suppressed", "reason": "silent"}
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

    def cancel_typing(self):
        """Disable the pending permission without altering another lane's slot."""
        from samsara import execution_policy as policy
        with self._state_lock:
            op = self._typing_op
        if op is not None:
            policy._release_slot(op)
            op.cancel(self.app, "disabled", chip=False)

    def _type_text(self, args, client_name, generation):
        text = args.get("text") if isinstance(args, dict) else None
        length = len(text) if isinstance(text, str) else 0
        bucket = "0" if length == 0 else "1-100" if length <= 100 else "101-500" if length <= 500 else "501-2000" if length <= 2000 else "over-2000"

        def finish(reason):
            reason = reason if reason in _TYPE_REASONS else "window_changed"
            status = reason if reason in {"delivered", "unknown"} else "refused"
            logger.info("[AGENTS] tool=type_text outcome=%s reason=%s length_bucket=%s", status, reason, bucket)
            return {"status": status, "reason": reason}

        if not self.typing_enabled or generation != self._generation or self._stopping.is_set():
            return finish("disabled")
        if reason := _text_reason(args):
            return finish(reason)
        with self._state_lock:
            self._pending = {slot: expiry for slot, expiry in self._pending.items() if expiry > self._clock()}
            if self._pending or not self._typing_lock.acquire(blocking=False):
                return finish("busy")
        from samsara import execution_policy as policy
        policy_generation = policy.capture_generation(self.app)
        op = None
        ready = threading.Event()
        approval = {}
        refused = {}
        abandoned = threading.Event()
        staged_record = {}
        speech_done = threading.Event()
        question = {}
        delivery_state = {"started": False, "done": False, "release": False}
        deadline = time.monotonic() + TYPE_TIMEOUT

        def eligible():
            return (not abandoned.is_set() and self.typing_enabled and generation == self._generation
                    and not self._stopping.is_set() and getattr(self.app, "_running", True)
                    and policy.is_fresh(self.app, policy_generation)
                    and time.monotonic() < deadline)

        def probe():
            target, reason = self._target_probe()
            return target, reason if reason in _TYPE_REASONS else "window_changed" if reason else None

        try:
            def speak_question():
                pending = policy.pending_operation()
                if pending is not None and pending.approved is None and not pending.is_expired():
                    return "busy"
                if not eligible():
                    return "disabled"
                target, reason = probe()
                if reason or not target:
                    return reason or "window_changed"
                coordinator = getattr(self.app, "audio_coordinator", None)
                if coordinator is None:
                    return "timeout"
                if getattr(coordinator, "is_speaking", False):
                    return "busy"
                # STAND-IN COPY (565c)
                question["command_key_only"] = (self.app.config.get("agent_server") or {}).get("command_key_only", False) is True
                instruction = "Hold your Command key and say yes to allow." if question["command_key_only"] else "Say yes to allow."
                prompt = f"{client_name} wants to type {len(text.split())} words into {target['app']}. {instruction}"
                question.update(target=target, prompt=prompt)
                question["capture_start"] = getattr(self.app, "_hold_capture_start_ns", None)
                question["capture_active"] = any(bool(getattr(self.app, flag, False)) for flag in
                    ("recording", "_hotkey_recording", "_stop_in_flight", "_streaming_session"))

                def spoken():
                    question["listen_after"] = time.monotonic() + CONFIRM_ECHO_TAIL
                    speech_done.set()

                handle = coordinator.speak(prompt, category="confirmation", interruptible=True, on_done=spoken)
                if str(getattr(handle, "utterance_id", "")).startswith("noop-"):
                    return "timeout"
                return None

            reason = self._on_ui(speak_question)
            if reason:
                return finish(reason)
            # No pending yes exists during playback or its echo tail. Waiting is
            # on the HTTP worker, never on the UI thread. Cancelled/noop speech
            # cannot open an answer window if its completion callback is absent.
            while (not speech_done.is_set()
                   or time.monotonic() < question.get("listen_after", float("inf"))):
                if not eligible():
                    return finish("disabled" if time.monotonic() < deadline else "timeout")
                abandoned.wait(0.01)
            listen_start_ns = time.perf_counter_ns()
            question["capture_active_at_listen"] = any(bool(getattr(self.app, flag, False)) for flag in
                ("recording", "_hotkey_recording", "_stop_in_flight", "_streaming_session"))

            def ask():
                pending = policy.pending_operation()
                if pending is not None and pending.approved is None and not pending.is_expired():
                    return None, "busy"
                if not eligible():
                    return None, "disabled"
                target, prompt = question["target"], question["prompt"]
                current, why = probe()
                if why or current != target:
                    return None, why or "window_changed"

                def guard():
                    if not eligible():
                        refused["reason"] = "disabled" if time.monotonic() < deadline else "timeout"
                    elif getattr(self.app.audio_coordinator, "is_speaking", False):
                        refused["reason"] = "user_declined"
                    elif question["capture_active_at_listen"]:
                        refused["reason"] = "user_declined"
                    else:
                        # A held-key capture begun during the question cannot
                        # become a delayed spoken yes after playback finishes.
                        capture_start = getattr(self.app, "_hold_capture_start_ns", None)
                        if capture_start is None:
                            capture_start = question["capture_start"]
                        # Without a transcript-finalised marker an old timestamp
                        # cannot be distinguished from an answer still in flight.
                        if (question["capture_active"]
                                or (capture_start is not None and capture_start < listen_start_ns)):
                            refused["reason"] = "user_declined"
                            return refused["reason"]
                        current, why = probe()
                        if why or current != target:
                            refused["reason"] = why or "window_changed"
                    return refused.get("reason")

                def approved(_op):
                    # Capture the target at the user's yes, then recheck at delivery.
                    try:
                        if (question["command_key_only"]
                                and getattr(_op, "answer_capture_path", None) != "command_key"):
                            refused["reason"] = "user_declined"
                            return
                        if (getattr(_op, "answer_source", None) != policy.ANSWER_VOICE
                                or policy.classify_reply(getattr(_op, "reply_text", "")) != policy.REPLY_YES):
                            refused["reason"] = "user_declined"
                            return
                        current, why = probe()
                        if why or current != target:
                            refused["reason"] = why or "window_changed"
                        else:
                            approval["target"] = current
                    except Exception:
                        refused["reason"] = "window_changed"
                    finally:
                        ready.set()

                inv = policy.Invocation("agent type_text", route=policy.Route.MODEL,
                                        generation=policy_generation)
                staged = policy.stage_pending(self.app, inv, prompt, on_approve=approved,
                                               on_reject=lambda _op: ready.set(),
                                               targets={}, target_probe=None,
                                               refusal_guard=guard, ttl_s=TYPE_TIMEOUT)
                staged.command_key_only = question["command_key_only"]
                def filter_reply(reply, source, capture_path):
                    if (staged.command_key_only and reply == policy.REPLY_YES
                            and (source != policy.ANSWER_VOICE or capture_path != "command_key")):
                        return "refused:command key required"
                    staged.answer_capture_path = capture_path
                    return None
                staged.voice_reply_filter = filter_reply
                with self._state_lock:
                    self._typing_op = staged
                    staged_record["op"] = staged
                if not eligible():
                    policy._release_slot(staged)
                    staged.cancel(self.app, "expired", chip=False)
                    return staged, "timeout"
                if policy.pending_operation() is not staged:
                    staged.cancel(self.app, "unavailable", chip=False)
                    return staged, "timeout"
                try:
                    if callable(chip := getattr(self.app, "_show_outcome_chip", None)):
                        chip(prompt, "pending", int(TYPE_TIMEOUT * 1000))
                    coordinator = getattr(self.app, "audio_coordinator", None)
                    if coordinator is None:
                        return staged, "timeout"
                except Exception:
                    return staged, "timeout"
                return staged, None

            op, reason = self._on_ui(ask)
            if reason:
                return finish(reason)
            while not ready.wait(min(0.05, max(0, deadline - time.monotonic()))):
                if not eligible():
                    return finish("disabled" if time.monotonic() < deadline else "timeout")
            if refused:
                return finish(refused["reason"])
            if not op.approved or "target" not in approval:
                return finish("disabled" if not self.typing_enabled or generation != self._generation
                              or not policy.is_fresh(self.app, policy_generation)
                              else "timeout" if time.monotonic() >= deadline else "user_declined")
            if not eligible():
                return finish("disabled" if time.monotonic() < deadline else "timeout")

            def perform_delivery():
                def final_guard():
                    if not eligible():
                        refused["reason"] = "disabled" if time.monotonic() < deadline else "timeout"
                        return False
                    current, why = probe()
                    if why or current != approval["target"]:
                        refused["reason"] = why or "window_changed"
                        return False
                    return True

                if not final_guard():
                    return refused["reason"]
                try:
                    pasted = paste_delivery.paste_preserving_clipboard(
                        self.app, text, before_paste=final_guard,
                        feedback="silent", clipboard_only=True)
                except Exception:
                    pasted = False
                return "delivered" if pasted else refused.get("reason", "refused_delivery")

            def deliver():
                with self._state_lock:
                    delivery_state["started"] = True
                try:
                    return perform_delivery()
                finally:
                    with self._state_lock:
                        delivery_state["done"] = True
                        if delivery_state["release"]:
                            self._typing_quiet_until = self._clock() + SPEAK_QUIET_AFTER_TYPING
                            self._typing_lock.release()

            try:
                return finish(self._on_ui(deliver))
            except TimeoutError:
                # The UI callback may already have sent Ctrl+V. Its cancellation
                # cannot undo that side effect; never claim refusal or retry.
                return finish("unknown")
        except TimeoutError:
            return finish("timeout")
        except Exception:
            return finish("window_changed")
        finally:
            abandoned.set()
            op = op or staged_record.get("op")
            if op is not None:
                policy._release_slot(op)
                if op.approved is None:
                    op.cancel(self.app, "expired", chip=False)
            with self._state_lock:
                if self._typing_op is op:
                    self._typing_op = None
                if question:
                    self._typing_quiet_until = self._clock() + SPEAK_QUIET_AFTER_TYPING
                if delivery_state["started"] and not delivery_state["done"]:
                    delivery_state["release"] = True
                else:
                    self._typing_lock.release()

    def dispatch(self, request, headers, *, generation=None):
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
            result = {"tools": TOOLS + ([TYPE_TOOL] if self.typing_enabled else [])}
        elif method == "tools/call":
            name, args = params.get("name"), params.get("arguments", {})
            if not isinstance(args, dict) and name != "type_text":
                return fail(-32602, "Invalid arguments")
            if name == "type_text":
                value = self._type_text(args, self._client_name(params, headers),
                                        self._generation if generation is None else generation)
                result = {"content": [{"type": "text", "text": json.dumps(value)}],
                          "structuredContent": value, "isError": value["status"] != "delivered"}
                if modern:
                    result = {**result, "resultType": "complete", "_meta": {
                        "io.modelcontextprotocol/serverInfo": _INFO}}
                return 200, {**response, "result": result}
            if name == "get_status" and not args:
                callback = self._get_status
            elif name == "speak" and set(args).issubset({"text", "full_text"}) and "text" in args:
                text = args["text"]
                full_text = args.get("full_text")
                if (not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT or not text.strip()
                        or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in text)
                        or re.search(r"<[^>]*>|```", text)):
                    return fail(-32602, "Expected plain text of 1 to 400 characters")
                if full_text is not None and (not isinstance(full_text, str)
                        or not 1 <= len(full_text) <= MAX_FULL_TEXT or not full_text.strip()
                        or any(unicodedata.category(c) in {"Cf", "Cs"}
                               or (unicodedata.category(c) == "Cc" and c not in "\n\r\t")
                               for c in full_text)):
                    return fail(-32602, f"Expected full_text to contain 1 to {MAX_FULL_TEXT} printable characters")
                spoken = text
                words = spoken.split()
                if len(words) > 60:
                    spoken = " ".join(words[:60])
                    logger.warning("[AGENTS] speak text capped at 60 words client=%s",
                                   self._client_name(params, headers))

                def speak_agent_reply():
                    result = self._speak(spoken, caption_text=full_text, allow_silent=full_text is not None)
                    if full_text is None:
                        return result
                    import time as _time
                    if full_text is not None:
                        rest = full_text
                        if full_text.startswith(spoken):
                            rest = full_text[len(spoken):].lstrip()
                        self.app._cli_agent_voice_state = {
                            "agent": "mcp", "last": spoken, "rest": rest,
                            "expires": _time.monotonic() + 60,
                        }
                    return {"spoken": result.get("status") in {"spoken", "queued"},
                            "full_text_kept": full_text is not None,
                            "follow_ups": ["repeat that", "read the rest"]}

                callback = speak_agent_reply
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
