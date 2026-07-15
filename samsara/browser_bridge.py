"""Local loopback WebSocket bridge to the Samsara Show Numbers browser extension.

Transport for the Brave/Chromium DOM-based Show Numbers vertical slice.
Hosts a `websockets.sync.server` instance bound to 127.0.0.1 only, inside
this already-running Samsara process (thread-based, not asyncio -- matches
the rest of this codebase's plain-threading style and needs no event-loop
bridging code). Nothing outside this module ever touches WebSocket/JSON
directly: `plugins/commands/show_numbers.py` calls only the five methods on
`BrowserBridge` (start/stop/is_connected/request_hints/send_selection/
send_dismiss), so the transport can be swapped later without touching DOM
discovery or command dispatch.

Auth (three layers):
  1. Origin allowlist (`origins=` on `serve()`) -- rejects a mismatched
     `Origin` header during the HTTP handshake itself, before any WS frame
     is exchanged. Enforced by the browser, unspoofable by page content.
     The expected origin is pinned to this extension's stable ID (derived
     from the public key embedded in browser_extension/manifest.json).
  2. A persistent per-profile pairing secret authenticates both peers with
     HMAC-SHA256 challenge/response. This is the layer that stops an
     arbitrary local process from binding the fixed port and impersonating
     Samsara to issue click/focus/select commands. There is deliberately no
     unauthenticated legacy fallback.
  3. A fresh per-connection token authenticates every later
     extension-to-Samsara message after the mutually authenticated hello.

The persistent secret lives in the profile selected by SAMSARA_HOME_DIR.
An authenticated copy of the unpacked extension is installed below that
same profile (`browser_bridge/extension`), avoiding source/frozen path drift
and keeping isolated profiles from sharing pairing state. The old
diagnostics-only pairing.json shape migrates by generating a new secret and
failing closed until the installed extension is loaded/reloaded.

Logging: connection accept/reject and message *types* only. Never the token
value, never message payload bodies (which can carry page text/URLs).
"""

import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import threading
import time

from websockets.sync.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from samsara.paths import samsara_home_dir
from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

PORT = 47831
EXTENSION_ID = "knjkiopjcnpieppomfegojdkndblkaai"
EXPECTED_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
MAX_MESSAGE_SIZE = 8192
HELLO_TIMEOUT_S = 5.0

PAIRING_VERSION = 1
_AUTH_FILE = "pairing-auth.json"
_EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "bridge-auth-core.js",
    "content-core.js",
    "content.js",
    "README.md",
)

_REQUIRED_FIELDS = {
    "hello": {"serverNonce", "clientNonce", "proof"},
    "ready": set(),
    "hints": {"requestId", "hints"},
    "hints_unavailable": {"requestId", "reason"},
    "selection_result": {"requestId", "ok"},
    "dismissed": set(),
}


def _pairing_dir() -> Path:
    return samsara_home_dir() / "browser_bridge"


def _pairing_path() -> Path:
    return _pairing_dir() / "pairing.json"


def _runtime_path() -> Path:
    return _pairing_dir() / "runtime.json"


def _installed_extension_dir() -> Path:
    return _pairing_dir() / "extension"


def _bundled_extension_dir() -> Path:
    # In source this resolves to <repo>/browser_extension. PyInstaller sets
    # module __file__ below its extraction root, yielding the bundled copy.
    return Path(__file__).resolve().parent.parent / "browser_extension"


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, separators=(",", ":"))
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _valid_secret(value) -> bool:
    return isinstance(value, str) and 40 <= len(value) <= 128


def _load_or_create_pairing_secret() -> "str | None":
    path = _pairing_path()
    migrated = False
    try:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = None
            if isinstance(payload, dict) and _valid_secret(payload.get("secret")):
                return payload["secret"]
            # The v0 diagnostics-only file had {port, started_at}. Never
            # accept it as authentication; replace it atomically with a secret.
            migrated = True

        secret = secrets.token_urlsafe(32)
        _atomic_write_json(path, {
            "version": PAIRING_VERSION,
            "secret": secret,
            "created_at": time.time(),
        })
        logger.info(
            "[BROWSER_BRIDGE] %s per-profile browser pairing secret",
            "Migrated" if migrated else "Created",
        )
        return secret
    except OSError:
        logger.exception(
            "[BROWSER_BRIDGE] Cannot persist pairing secret; DOM bridge disabled"
        )
        return None


def _install_paired_extension(secret: str) -> bool:
    source = _bundled_extension_dir()
    destination = _installed_extension_dir()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for name in _EXTENSION_FILES:
            source_file = source / name
            if not source_file.is_file():
                raise FileNotFoundError(source_file)
            shutil.copy2(source_file, destination / name)
        _atomic_write_json(destination / _AUTH_FILE, {
            "version": PAIRING_VERSION,
            "secret": secret,
        })
    except OSError:
        logger.exception(
            "[BROWSER_BRIDGE] Failed to install authenticated extension at %s; "
            "DOM bridge disabled",
            destination,
        )
        return False
    logger.info(
        "[BROWSER_BRIDGE] Authenticated extension ready at %s "
        "(load/reload this directory in Brave)",
        destination,
    )
    return True


def _proof(secret: str, label: str, *parts: str) -> str:
    message = ":".join((label, *parts)).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _validate_message(msg: dict) -> bool:
    """Minimal hand-rolled schema check -- no jsonschema dependency for a
    handful of small, fixed message shapes. Rejects anything not shaped
    like one of the known extension->server message types."""
    if not isinstance(msg, dict):
        return False
    msg_type = msg.get("type")
    if msg_type not in _REQUIRED_FIELDS:
        return False
    required = _REQUIRED_FIELDS[msg_type]
    return required.issubset(msg.keys())


class BrowserBridge:
    """Owns the local WebSocket server and the single active extension
    connection. See module docstring for the auth/transport design."""

    def __init__(
        self,
        port: int = PORT,
        origin: str = EXPECTED_ORIGIN,
        *,
        pairing_secret: "str | None" = None,
    ):
        # port/origin are overridable per-instance (default: the real
        # PORT/EXPECTED_ORIGIN constants) purely so tests can run an
        # isolated bridge on a throwaway port without colliding with a
        # real running Samsara instance or a concurrent test run.
        self._port = port
        self._origin = origin
        self._profile_pairing = (
            port == PORT and origin == EXPECTED_ORIGIN and pairing_secret is None
        )
        self._pairing_secret = pairing_secret
        self._server = None
        self._thread = None
        self._conn_lock = threading.RLock()
        self._active_connection = None
        self._active_token = None
        self._connection_ready = False
        self._pending_lock = threading.Lock()
        self._pending: dict[int, dict] = {}  # requestId -> {"event": Event, "response": dict|None}
        self._request_counter = 0
        self._on_dismissed = None
        # Set by request_hints() alongside a None return, so callers can
        # distinguish "page genuinely has no candidates" (a real, connected
        # DOM response -- do not fall back to UIA, that would just re-show
        # tabs/bookmarks) from "the DOM path itself is unavailable"
        # (disconnected/timeout/no content script -- fall back to UIA).
        self.last_hints_unavailable_reason: "str | None" = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Binds the loopback server and starts its accept-loop thread.
        Returns False (does not raise) if the fixed port is unavailable --
        callers treat that as "bridge disabled for this session, use UIA."
        """
        if self._profile_pairing:
            self._pairing_secret = _load_or_create_pairing_secret()
            if self._pairing_secret is None:
                return False
            if not _install_paired_extension(self._pairing_secret):
                return False
        elif not _valid_secret(self._pairing_secret):
            logger.error(
                "[BROWSER_BRIDGE] No valid pairing secret; refusing to start"
            )
            return False

        try:
            self._server = ws_serve(
                self._handler,
                host="127.0.0.1",
                port=self._port,
                origins=[self._origin],
                max_size=MAX_MESSAGE_SIZE,
            )
        except OSError as e:
            logger.warning(
                "[BROWSER_BRIDGE] Failed to bind loopback port %d -- DOM Show "
                "Numbers unavailable this session: %s",
                self._port, type(e).__name__,
            )
            return False

        self._thread = thread_registry.spawn(
            "browser_bridge.server", self._server.serve_forever, daemon=True
        )
        self._write_runtime_file()
        logger.info("[BROWSER_BRIDGE] Listening on 127.0.0.1:%d", self._port)
        return True

    def stop(self, timeout: float = 2.0) -> None:
        """Explicit, bounded shutdown -- deliberately not relying on
        thread_registry.shutdown()'s generic sweep, which never joins
        daemon threads at all."""
        with self._conn_lock:
            conn = self._active_connection
            self._active_connection = None
            self._connection_ready = False
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._fail_all_pending("bridge_stopping")

        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                logger.exception("[BROWSER_BRIDGE] Error during server.shutdown()")
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                logger.error(
                    "[BROWSER_BRIDGE] Server thread did not exit within %ss", timeout
                )
        self._remove_runtime_file()
        logger.info("[BROWSER_BRIDGE] Stopped")

    def is_connected(self) -> bool:
        with self._conn_lock:
            return self._active_connection is not None and self._connection_ready

    def wait_until_disconnected(self, timeout: float = 2.0) -> bool:
        """Blocks until is_connected() is False, bounded by timeout.

        Disconnection is detected on the connection's own background
        threads (the websockets library's recv-events thread, then this
        bridge's per-connection handler thread reaching _clear_if_active)
        -- so is_connected() can still read True for a brief window right
        after the peer closes its socket, purely from normal thread
        scheduling, not a bug in the flag itself. Production call sites
        only ever treat is_connected() as a cheap best-effort gate (a
        failed send()/timeout after a stale-True read is already handled
        gracefully -- see _send_and_wait), so they have no need for this.
        It exists for callers (tests, diagnostics) that need a
        deterministic point after a close rather than a racy snapshot.
        """
        deadline = time.monotonic() + timeout
        while self.is_connected():
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)
        return True

    def set_on_dismissed(self, callback) -> None:
        """Registers a callback invoked (no args) when the extension reports
        an unsolicited dismissal (in-page Escape), so show_numbers.py can
        clear its own DOM-session state without polling."""
        self._on_dismissed = callback

    # ------------------------------------------------------------------
    # Outbound requests (blocking facade)
    # ------------------------------------------------------------------

    def request_hints(self, timeout: float = 0.8) -> "list[dict] | None":
        self.last_hints_unavailable_reason = None
        response = self._send_and_wait({"type": "show_hints"}, timeout)
        if response is None:
            self.last_hints_unavailable_reason = "timeout_or_disconnected"
            return None
        if response.get("type") == "hints":
            hints = response.get("hints")
            logger.info(
                "[BROWSER_BRIDGE] Page hint count=%d",
                len(hints) if isinstance(hints, list) else -1,
            )
            return hints
        reason = response.get("reason", "unknown")
        logger.info("[BROWSER_BRIDGE] Hints unavailable: %s", reason)
        self.last_hints_unavailable_reason = reason
        return None

    def send_selection(self, number: int, action: str, timeout: float = 0.8, modifiers=None) -> bool:
        response = self._send_and_wait(
            {"type": "select", "number": number, "action": action, "modifiers": modifiers or {}},
            timeout,
        )
        ok = bool(response and response.get("ok"))
        logger.info(
            "[BROWSER_BRIDGE] Selection number=%d action=%s ok=%s", number, action, ok
        )
        return ok

    def send_dismiss(self) -> None:
        with self._conn_lock:
            conn = self._active_connection if self._connection_ready else None
        if conn is None:
            return
        try:
            self._request_counter += 1
            conn.send(json.dumps({"type": "dismiss", "requestId": self._request_counter}))
        except Exception:
            pass

    def _send_and_wait(self, payload: dict, timeout: float) -> "dict | None":
        with self._conn_lock:
            conn = self._active_connection if self._connection_ready else None
        if conn is None:
            return None

        with self._pending_lock:
            self._request_counter += 1
            request_id = self._request_counter
            event = threading.Event()
            self._pending[request_id] = {"event": event, "response": None}

        payload = dict(payload)
        payload["requestId"] = request_id
        try:
            conn.send(json.dumps(payload))
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            return None

        got = event.wait(timeout)
        with self._pending_lock:
            entry = self._pending.pop(request_id, None)
        if not got or entry is None:
            return None
        return entry["response"]

    def _fail_all_pending(self, _reason: str) -> None:
        with self._pending_lock:
            entries = list(self._pending.values())
        for entry in entries:
            entry["event"].set()

    # ------------------------------------------------------------------
    # Connection handling (runs on a per-connection thread spawned by the
    # websockets library itself)
    # ------------------------------------------------------------------

    def _handler(self, connection) -> None:
        # origins= on serve() already rejected any mismatched Origin header
        # during the handshake, before this handler is ever invoked.
        logger.info("[BROWSER_BRIDGE] Connection accepted (origin ok)")

        token = secrets.token_urlsafe(32)
        server_nonce = secrets.token_urlsafe(32)

        # Server chooses the first nonce. Binding the client proof to this
        # fresh challenge prevents a malicious process that temporarily owns
        # the port from capturing a reusable extension hello and replaying it
        # to the real Samsara process later.
        try:
            connection.send(json.dumps({
                "type": "challenge",
                "serverNonce": server_nonce,
            }))
        except ConnectionClosed:
            return

        try:
            first_raw = connection.recv(timeout=HELLO_TIMEOUT_S)
        except (TimeoutError, ConnectionClosed):
            logger.info("[BROWSER_BRIDGE] Connection closed before hello")
            self._clear_if_active(connection)
            return

        try:
            first_msg = json.loads(first_raw)
        except (ValueError, TypeError):
            logger.warning("[BROWSER_BRIDGE] Rejecting connection: malformed hello")
            self._clear_if_active(connection)
            connection.close()
            return

        if not _validate_message(first_msg) or first_msg.get("type") != "hello":
            logger.warning(
                "[BROWSER_BRIDGE] Rejecting connection: first message was not hello"
            )
            self._clear_if_active(connection)
            connection.close()
            return

        client_nonce = first_msg.get("clientNonce")
        claimed_server_nonce = first_msg.get("serverNonce")
        client_proof = first_msg.get("proof")
        if (
            not isinstance(client_nonce, str)
            or not 20 <= len(client_nonce) <= 128
            or claimed_server_nonce != server_nonce
            or not isinstance(client_proof, str)
            or not hmac.compare_digest(
                client_proof,
                _proof(
                    self._pairing_secret,
                    "client-v1",
                    server_nonce,
                    client_nonce,
                ),
            )
        ):
            logger.warning(
                "[BROWSER_BRIDGE] Rejecting connection: pairing proof mismatch"
            )
            connection.close()
            return

        server_proof = _proof(
            self._pairing_secret, "server-v1", server_nonce, client_nonce
        )

        try:
            connection.send(json.dumps({
                "type": "hello_ack",
                "token": token,
                "serverNonce": server_nonce,
                "proof": server_proof,
            }))
        except ConnectionClosed:
            return

        # The extension sends ready only after verifying server_proof. This
        # third leg prevents Samsara from sending a command while the async
        # Web Crypto check in the MV3 worker is still in flight.
        try:
            ready_raw = connection.recv(timeout=HELLO_TIMEOUT_S)
            ready_msg = json.loads(ready_raw)
        except (TimeoutError, ConnectionClosed, ValueError, TypeError):
            logger.warning(
                "[BROWSER_BRIDGE] Rejecting connection: no authenticated ready"
            )
            connection.close()
            return
        if (
            not _validate_message(ready_msg)
            or ready_msg.get("type") != "ready"
            or not hmac.compare_digest(str(ready_msg.get("token", "")), token)
        ):
            logger.warning(
                "[BROWSER_BRIDGE] Rejecting connection: invalid authenticated ready"
            )
            connection.close()
            return

        # Only a peer that authenticated Samsara may replace the active
        # extension. A forged-Origin connection cannot cause a disconnect.
        with self._conn_lock:
            previous = self._active_connection
            self._active_connection = connection
            self._active_token = token
            self._connection_ready = False
        if previous is not None and previous is not connection:
            try:
                previous.close()
            except Exception:
                pass
        # Queue ready_ack while holding the same lock used by outbound request
        # senders. Once is_connected() can observe True, the acknowledgement
        # is guaranteed to be ahead of every command on this WebSocket.
        with self._conn_lock:
            try:
                connection.send(json.dumps({"type": "ready_ack"}))
            except ConnectionClosed:
                if self._active_connection is connection:
                    self._active_connection = None
                    self._active_token = None
                    self._connection_ready = False
                return
            if self._active_connection is connection:
                self._connection_ready = True
        logger.info("[BROWSER_BRIDGE] Mutually authenticated handshake complete")

        try:
            for raw in connection:
                self._handle_message(connection, token, raw)
        except ConnectionClosed:
            pass
        finally:
            self._clear_if_active(connection)
            logger.info("[BROWSER_BRIDGE] Connection closed")

    def _clear_if_active(self, connection) -> None:
        cleared = False
        with self._conn_lock:
            if self._active_connection is connection:
                self._active_connection = None
                self._connection_ready = False
                self._active_token = None
                cleared = True
        if cleared:
            self._fail_all_pending("disconnected")

    def _handle_message(self, connection, expected_token: str, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("[BROWSER_BRIDGE] Rejecting message: malformed JSON")
            connection.close()
            return

        if not _validate_message(msg):
            logger.warning(
                "[BROWSER_BRIDGE] Rejecting message: schema validation failed (type=%r)",
                msg.get("type") if isinstance(msg, dict) else None,
            )
            connection.close()
            return

        if msg.get("token") != expected_token:
            logger.warning("[BROWSER_BRIDGE] Rejecting message: token mismatch")
            connection.close()
            return

        msg_type = msg["type"]
        logger.debug("[BROWSER_BRIDGE] Received message type=%s", msg_type)

        if msg_type == "dismissed":
            if self._on_dismissed is not None:
                try:
                    self._on_dismissed()
                except Exception:
                    logger.exception("[BROWSER_BRIDGE] on_dismissed callback failed")
            return

        request_id = msg.get("requestId")
        if not isinstance(request_id, int):
            return
        with self._pending_lock:
            entry = self._pending.get(request_id)
            if entry is not None:
                entry["response"] = msg
                entry["event"].set()
        # A requestId with no matching pending entry is a stale/late reply
        # to a request the caller already timed out on -- silently dropped
        # by design (see _send_and_wait's pop-on-completion).

    # ------------------------------------------------------------------
    # Runtime diagnostics. The persistent secret is never removed at stop.
    # ------------------------------------------------------------------

    def _write_runtime_file(self) -> None:
        # Only the real default-port/origin bridge (the process-wide
        # singleton from get_bridge()) touches SAMSARA_HOME_DIR -- a test
        # instance constructed with a throwaway port/origin must never
        # clobber real pairing state.
        if not self._profile_pairing:
            return
        try:
            _atomic_write_json(_runtime_path(), {
                "port": self._port,
                "started_at": time.time(),
                "pairing_version": PAIRING_VERSION,
            })
        except OSError:
            logger.exception("[BROWSER_BRIDGE] Failed to write runtime file")

    def _remove_runtime_file(self) -> None:
        if not self._profile_pairing:
            return
        try:
            _runtime_path().unlink(missing_ok=True)
        except OSError:
            pass


_bridge: "BrowserBridge | None" = None


def get_bridge() -> BrowserBridge:
    """Process-wide singleton, mirroring show_numbers.py's own
    module-level-state pattern rather than threading an instance through
    every call site."""
    global _bridge
    if _bridge is None:
        _bridge = BrowserBridge()
    return _bridge
