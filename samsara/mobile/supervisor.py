"""Samsara Mobile Companion -- supervisor (Phase 1).

Owned by the main app (DictationApp). start()/stop() are the only entry
points that do I/O -- nothing in this module binds a socket or spawns a
process at import time.

start() brings up the in-process bridge (loopback IPC listener), then
launches server_proc.py as a separate subprocess that owns the LAN-facing
HTTP server. If either the IPC bind or the HTTP subprocess fails to come up
(e.g. a port already in use), the whole feature disables itself and start()
returns False -- it never raises into app startup.
"""

import logging
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from .bridge import Bridge, make_ping_handler
from . import server_proc

logger = logging.getLogger(__name__)

# The sami env interpreter this project standardizes on. Used as a fallback
# when the running interpreter isn't already that one -- e.g. Samsara itself
# was launched some other way and a bare `python` on PATH would resolve to
# an unrelated venv (this machine has a Hermes venv shadowing PATH).
SAMI_PYTHON = r"F:\envs\sami\python.exe"

DEFAULT_HTTP_PORT = server_proc.DEFAULT_HTTP_PORT

# How long start() waits after spawning the subprocess to see if it exits
# immediately (e.g. HTTP port already in use) before declaring success.
SPAWN_SETTLE_SECONDS = 2.0
_SPAWN_POLL_INTERVAL_SECONDS = 0.1

# How long to wait for the subprocess to exit cleanly on stop() before kill().
TERMINATE_TIMEOUT_SECONDS = 3.0

# How often the watchdog thread polls subprocess liveness.
WATCHDOG_POLL_SECONDS = 1.0

# Bounded restart-with-backoff after an unexpected subprocess death. A dead
# subprocess must never take the host down with it -- these bounds exist so
# a persistently-failing subprocess degrades to "feature off" instead of
# looping forever.
RESTART_BACKOFF_SECONDS = 5.0
MAX_CONSECUTIVE_RESTARTS = 3


def _resolve_interpreter():
    """Return the interpreter to launch server_proc with.

    Prefers sys.executable when the running process is already the sami env
    (the common case, since Samsara itself is normally run with
    F:\\envs\\sami\\python.exe); otherwise falls back to the hardcoded sami
    path.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and "sami" in str(exe).lower():
        return sys.executable
    return SAMI_PYTHON


def _get_lan_ip():
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


class Supervisor:
    """Owns the bridge + subprocess lifecycle for the mobile companion feature.

    http_host defaults to the auto-detected LAN IP; tests may pass "127.0.0.1"
    explicitly to exercise the whole path without depending on the machine's
    network configuration.
    """

    def __init__(self, http_port=DEFAULT_HTTP_PORT, http_host=None):
        self._http_port = http_port
        self._http_host = http_host
        self._bridge = None
        self._proc = None
        self._token = None
        self._watchdog_thread = None
        self._stop_event = threading.Event()
        self._enabled = False
        self._restart_count = 0
        self.interpreter_used = None

    @property
    def enabled(self):
        """True once start() has successfully brought the feature up."""
        return self._enabled

    @property
    def process(self):
        """The current subprocess handle (or None). Exposed for tests/diagnostics."""
        return self._proc

    @property
    def bridge(self):
        """The in-process bridge (or None). Exposed for tests/diagnostics."""
        return self._bridge

    def start(self):
        """Bring up the bridge + subprocess. Returns True on success.

        Never raises -- any failure (port in use, spawn failure) logs and
        leaves the supervisor disabled in its pre-start state.
        """
        if self._enabled:
            return True

        self._token = secrets.token_hex(16)
        self._bridge = Bridge(self._token)
        self._bridge.register("ping", make_ping_handler())

        try:
            ipc_port = self._bridge.start()
        except OSError as e:
            logger.warning("[MOBILE] IPC bridge bind failed, feature disabled: %s", e)
            self._bridge = None
            return False

        http_host = self._http_host or _get_lan_ip()

        try:
            self._proc = self._spawn_subprocess(ipc_port, http_host)
        except OSError as e:
            logger.warning("[MOBILE] Subprocess spawn failed, feature disabled: %s", e)
            self._bridge.stop()
            self._bridge = None
            return False

        # Give the subprocess a moment to either bind its HTTP port or die
        # trying (e.g. port-in-use) before declaring success.
        if self._wait_for_early_exit(self._proc, SPAWN_SETTLE_SECONDS):
            logger.warning(
                "[MOBILE] Subprocess exited immediately (exit code %s, likely port %s in use), feature disabled",
                self._proc.returncode, self._http_port,
            )
            self._bridge.stop()
            self._bridge = None
            self._proc = None
            return False

        self._enabled = True
        self._restart_count = 0
        self._stop_event.clear()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="samsara-mobile-supervisor"
        )
        self._watchdog_thread.start()
        logger.info(
            "[MOBILE] Supervisor started: HTTP subprocess pid=%s on %s:%s, IPC on 127.0.0.1:%s",
            self._proc.pid, http_host, self._http_port, ipc_port,
        )
        return True

    def stop(self):
        """Tear down the subprocess and bridge. Idempotent, never raises."""
        self._stop_event.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=2.0)
            self._watchdog_thread = None

        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=TERMINATE_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    logger.error("[MOBILE] Subprocess did not die after kill()")
            except Exception:
                logger.exception("[MOBILE] Error terminating subprocess")
            self._proc = None

        if self._bridge is not None:
            try:
                self._bridge.stop()
            except Exception:
                logger.exception("[MOBILE] Error stopping bridge")
            self._bridge = None

        self._enabled = False
        logger.info("[MOBILE] Supervisor stopped")

    # -- internals -----------------------------------------------------------

    def _spawn_subprocess(self, ipc_port, http_host):
        interpreter = _resolve_interpreter()
        self.interpreter_used = interpreter
        cmd = [
            interpreter, "-m", "samsara.mobile.server_proc",
            "--ipc-port", str(ipc_port),
            "--ipc-token", self._token,
            "--http-port", str(self._http_port),
            "--http-host", http_host,
        ]
        repo_root = Path(__file__).resolve().parents[2]
        return subprocess.Popen(cmd, cwd=str(repo_root))

    def _wait_for_early_exit(self, proc, seconds):
        """Poll until proc exits or `seconds` elapses. Returns True if it exited."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(_SPAWN_POLL_INTERVAL_SECONDS)
        return proc.poll() is not None

    def _watchdog_loop(self):
        while not self._stop_event.wait(WATCHDOG_POLL_SECONDS):
            if self._proc is None or self._proc.poll() is None:
                continue  # still alive (or already torn down by stop())

            logger.warning("[MOBILE] Subprocess died (exit code %s)", self._proc.returncode)
            self._enabled = False

            if self._restart_count >= MAX_CONSECUTIVE_RESTARTS:
                logger.warning(
                    "[MOBILE] Restart limit reached; leaving feature off. Host remains fully functional without it."
                )
                return

            self._restart_count += 1
            time.sleep(RESTART_BACKOFF_SECONDS)
            if self._stop_event.is_set():
                return

            try:
                http_host = self._http_host or _get_lan_ip()
                self._proc = self._spawn_subprocess(self._bridge.port, http_host)
                self._enabled = True
                logger.info(
                    "[MOBILE] Subprocess restarted (attempt %s/%s)",
                    self._restart_count, MAX_CONSECUTIVE_RESTARTS,
                )
            except OSError:
                logger.exception("[MOBILE] Restart failed")
                return
