"""Samsara boot sequence, extracted from dictation.py (27).

The steps dictation.py runs before DictationApp exists -- faulthandler, the
Visual C++ runtime check, the single-instance lock (with
its stale-PID self-heal), the legacy source-profile migration, the early UI
scale, the splash hand-off -- plus the [BOOT] stage timer DictationApp uses.
dictation.py calls these at exactly the points the inline code used to run,
in the same order; the code itself moved unchanged. Log records go to the
same "Samsara" logger, so every [BOOT-DIAG] line is byte-identical.

Import cost is part of the contract: this module imports only the standard
library at load time (samsara.* and Qt are imported inside the functions
that need them, as they were inline), and it must NEVER import dictation --
boot must not drag the app in. tests/test_boot.py asserts both.

What deliberately stayed in dictation.py (see reports/27_code.md):
  * the None-stdout guard and the ducking-host divert -- they must run
    before ANY samsara import (samsara/__init__.py imports .commands/.ui);
  * the logging bootstrap -- its handler, self-check and rollover code call
    print(), which inside dictation.py resolves to dictation's own logging
    print override; moved here it would silently become the builtin;
  * the taskbar AUMID call -- tests/test_theme_identity.py executes that exact
    module-level block of dictation.py to prove it runs before any Qt object;
  * config load and the MIGRATE steps -- DictationApp methods on
    self.config / _config_lock / save_config's three-way merge.
"""

import logging
import os
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("Samsara")


def _enable_faulthandler(log_dir):
    """Dump every thread's Python stack to <SAMSARA_HOME>/logs/faulthandler.log
    on a native crash (access violation, SIGSEGV/SIGFPE/SIGILL/SIGABRT).

    2026-09-13: the mic setup guide took the process down with no Python
    traceback at all -- the regular log just restarted. faulthandler is the
    only thing that can speak after a native fault, and in a windowless
    build sys.stderr is a null stream, so it gets its own file. enable()
    already covers SIGABRT; faulthandler.register() does not exist on Windows
    and refuses SIGABRT elsewhere, so it is not called. Never raises: a
    diagnostic must not stop boot. Returns the open file (kept alive for the
    process lifetime) or None."""
    import faulthandler as _faulthandler
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / "faulthandler.log", "a", encoding="utf-8")
        fh.write(f"--- Samsara start pid={os.getpid()} ---\n")
        fh.flush()
        _faulthandler.enable(file=fh, all_threads=True)
        return fh
    except Exception as exc:
        try:
            sys.stderr.write(f"[BOOT] faulthandler not enabled: {exc!r}\n")
        except Exception:
            pass
        return None


def check_vc_redistributable():
    """Exit with a dialog when msvcp140.dll is missing (before faster-whisper loads)."""
    if sys.platform == 'win32':
        try:
            import ctypes as _ctypes
            _ctypes.cdll.LoadLibrary("msvcp140.dll")
        except OSError:
            from PySide6.QtWidgets import QApplication as _QApp, QMessageBox as _QMB
            _app = _QApp.instance() or _QApp(sys.argv)
            _QMB.critical(
                None,
                "Missing Dependency",
                "Samsara requires the Visual C++ Redistributable.\n\n"
                "Download it from:\n"
                "https://aka.ms/vs/17/release/vc_redist.x64.exe\n\n"
                "Install it and restart Samsara.",
            )
            sys.exit(1)


# ============================================================================
# Single Instance Check - Prevent multiple instances from running
# ============================================================================

def _is_samsara_process(pid: int) -> bool:
    """True if `pid` is alive AND looks like a Samsara process.

    Liveness alone isn't enough: PIDs get reused by Windows, so a lock file
    naming a PID that's alive right now could belong to a completely
    unrelated process that started after the real Samsara process (which
    wrote that PID) died or was killed. Checks the process image name --
    "Samsara.exe" for a frozen build, or a python*.exe running dictation.py
    for a dev-mode instance.

    Prefers psutil (already a project dependency); falls back to raw
    ctypes OpenProcess + QueryFullProcessImageNameW if psutil isn't
    importable for some reason.
    """
    try:
        import psutil
    except ImportError:
        psutil = None

    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            name = (proc.name() or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        except Exception:
            return False
        if name == "samsara.exe":
            return True
        if name.startswith("python"):
            try:
                cmdline = " ".join(proc.cmdline()).lower()
            except Exception:
                return False
            return "dictation.py" in cmdline
        return False

    if sys.platform != 'win32':
        # Can't verify identity without psutil off Windows -- assume it's
        # real rather than risk stealing a live process's lock.
        return True

    import ctypes
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # No handle -- process is gone (or inaccessible; treat the same,
        # since we can't confirm it's Samsara either way).
        return False
    try:
        buf_len = wt.DWORD(260)
        buf = ctypes.create_unicode_buffer(260)
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(buf_len))
        if not ok:
            return False
        image_name = Path(buf.value).name.lower()
        return image_name == "samsara.exe" or image_name.startswith("python")
    finally:
        kernel32.CloseHandle(handle)


def _steal_stale_lock_if_any(lock_file_path) -> None:
    """If lock_file_path exists and names a dead or non-Samsara PID, delete
    it. If it names a live Samsara process, log and exit(0) -- never hang;
    this whole check is non-blocking liveness/identity inspection, no wait.

    Runs before the OS-level lock acquisition below, which still does the
    actual atomic locking -- this only turns "some stale file is sitting
    there from a hard kill" into a clean steal instead of a false
    already-running refusal.
    """
    if not lock_file_path.exists():
        return
    try:
        recorded_pid = int(lock_file_path.read_text().strip())
    except (OSError, ValueError):
        # Unreadable/empty/corrupt -- can't belong to a live instance we'd
        # recognize; treat as stale.
        logger.info("[LOCK] lock file unreadable, stealing")
        try:
            lock_file_path.unlink()
        except OSError as e:
            logger.debug(f"[LOCK] could not remove unreadable lock file: {e}")
        return

    if _is_samsara_process(recorded_pid):
        logger.warning(f"[WARN] Samsara is already running (PID: {recorded_pid})")
        sys.exit(0)

    logger.info(f"[LOCK] stale lock from PID {recorded_pid}, stealing")
    try:
        lock_file_path.unlink()
    except OSError as e:
        logger.debug(f"[LOCK] could not remove stale lock file: {e}")


def _check_single_instance():
    """
    Ensure only one instance of Samsara is running.
    Windows uses a process-lifetime named mutex; Unix-like systems retain the
    existing file lock. Returns the retained handle or exits if another
    instance owns the same profile identity.

    The normal profile uses a fixed identity. When SAMSARA_HOME_DIR is
    explicitly set (temp-profile tooling, the tray's "Preview First-Run"
    dev action), the identity is derived from that path, so a preview
    instance never collides with the primary instance.
    """
    if sys.platform == 'win32':
        from samsara.single_instance import (
            AlreadyRunningError,
            acquire_single_instance_mutex,
        )
        try:
            return acquire_single_instance_mutex()
        except AlreadyRunningError:
            # Keep this exact marker: frozen smoke tooling recognizes it as
            # the expected fast refusal when a real instance is already up.
            logger.warning("[WARN] Samsara is already running")
            sys.exit(0)
        except Exception as e:
            # Preserve the existing fail-open startup policy. A broken
            # single-instance check must not make an accessibility app
            # impossible to launch.
            logger.warning(f"[WARN] Could not check for existing instance: {e}")
            return None

    from pathlib import Path
    import tempfile

    home_override = os.environ.get("SAMSARA_HOME_DIR")
    if home_override:
        import hashlib
        # normcase + realpath so equivalent paths (different case, trailing
        # slash, relative vs absolute, 8.3 vs long form) hash identically --
        # otherwise two preview launches pointed at "the same" dir by a
        # human typing it two different ways would get two different locks
        # and could run concurrently against one profile.
        normalized = os.path.normcase(os.path.realpath(home_override))
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        lock_name = f"samsara-{digest}.lock"
    else:
        lock_name = "samsara.lock"
    lock_file_path = Path(tempfile.gettempdir()) / lock_name
    _steal_stale_lock_if_any(lock_file_path)

    try:
        # Open/create lock file
        if sys.platform == 'win32':
            import msvcrt
            # Open in write mode, create if doesn't exist
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                # Try to read the other instance's PID
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
                sys.exit(0)
        else:
            # Unix-like systems (macOS, Linux)
            import fcntl
            lock_file = open(lock_file_path, 'w')
            try:
                # Try to get exclusive lock (non-blocking)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Write our PID
                lock_file.write(str(os.getpid()))
                lock_file.flush()
                return lock_file  # Keep file open to maintain lock
            except (IOError, OSError):
                # Another instance has the lock
                lock_file.close()
                try:
                    with open(lock_file_path, 'r') as f:
                        other_pid = f.read().strip()
                    logger.warning(f"[WARN] Samsara is already running (PID: {other_pid})")
                except Exception as e:
                    logger.debug(f"Could not read other instance PID: {e}")
                    logger.warning("[WARN] Samsara is already running")
                sys.exit(0)
    except Exception as e:
        # If locking fails for any reason, log but continue
        # (better to have duplicate instances than no instances)
        logger.warning(f"[WARN] Could not check for existing instance: {e}")
        return None

# Single-instance lock is only meaningful when this file is run as the main
# program. Acquiring it at import time blocks pytest (any import of dictation
# triggers sys.exit(0) from _check_single_instance when a prior import already
# holds the lock). The lock is acquired from the __main__ block below via
# _acquire_instance_lock() -- the module-level name is kept so tests and
# helpers can reason about it without triggering the check.
_instance_lock = None


def _acquire_instance_lock():
    """Acquire the single-instance lock. Called from __main__ only."""
    global _instance_lock
    _instance_lock = _check_single_instance()
    return _instance_lock


def lock_single_instance():
    """__main__: acquire the single-instance lock, with its [BOOT-DIAG] timing."""
    _t = time.perf_counter()
    _acquire_instance_lock()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] instance lock (_check_single_instance): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: instance lock {_dt:.0f}ms")


def migrate_legacy_source_profile(source_dir):
    """__main__: carry a legacy config.json from source_dir (dictation.py's folder)
    into the per-user profile once. No-op in a frozen build."""
    from samsara.paths import migrate_legacy_source_config
    if not getattr(sys, "frozen", False):
        try:
            if migrate_legacy_source_config(Path(source_dir) / "config.json"):
                logger.info("[CONFIG] Migrated legacy source profile to the per-user profile")
        except Exception as _config_migration_exc:
            logger.warning(
                "[CONFIG] Could not migrate legacy source profile: %s",
                _config_migration_exc,
            )


def apply_early_interface_scale():
    """__main__: set QT_SCALE_FACTOR from the saved UI scale before Qt starts."""
    from samsara.paths import samsara_config_path
    try:
        from samsara.ui_scale import apply_early_ui_scale
        _early_config_path = samsara_config_path()
        _early_scale = apply_early_ui_scale(_early_config_path)
        logger.info(f"[UI] Early interface scale: {_early_scale:g}x")
    except Exception as _scale_exc:
        logger.warning(f"[UI] Could not apply interface scale: {_scale_exc}")


def create_splash():
    """__main__: build the splash (on this, the main thread, as before) and return it."""
    _t = time.perf_counter()
    from samsara.ui.splash_qt import SplashScreenQt
    splash = SplashScreenQt()
    _dt = (time.perf_counter() - _t) * 1000
    logger.debug(f"[BOOT-DIAG] splash init (SplashScreenQt): {_dt:.0f}ms")
    if _dt > 5000:
        logger.debug(f"[BOOT-DIAG] SLOW STEP: splash init {_dt:.0f}ms")
    splash.set_status("Initializing...")
    return splash


def show_startup_failure(splash, e):
    """__main__: put a synchronous DictationApp() failure on the splash.

    Keeps the shared Qt runtime and splash alive so the failure is visible;
    the caller re-raises."""
    try:
        splash.set_status("Startup could not finish")
        if hasattr(splash, "set_detail"):
            splash.set_detail(str(e))
        if hasattr(splash, "set_error"):
            splash.set_error("Startup could not finish", str(e))
    except Exception as _splash_error:
        logger.debug(f"Could not show synchronous startup error: {_splash_error}")


class _BootStageTimer:
    """[BOOT] stage timer with one "last mark" per thread.

    The old closure shared a single last-timestamp between the boot thread
    and the model thread, so each lane's step durations absorbed the other
    lane's work (perf_artifacts/boot_profile.md section 4: "ACE audio engine
    start: 828ms" was really 13,371 ms, "Silero VAD load: 12547ms" was 212 ms).
    Each thread's first mark measures from begin_thread() if that thread
    called it, else from timer creation. "total" is always since creation.
    """

    def __init__(self, log=None):
        self._t0 = time.monotonic()
        self._last: dict[int, float] = {}
        self._lock = threading.Lock()
        self._log = log or logger.info

    def begin_thread(self) -> None:
        with self._lock:
            self._last[threading.get_ident()] = time.monotonic()

    def __call__(self, label: str) -> None:
        now = time.monotonic()
        tid = threading.get_ident()
        with self._lock:
            last = self._last.get(tid, self._t0)
            self._last[tid] = now
        self._log(
            f"[BOOT] {label}: {(now - last) * 1000:.0f}ms  "
            f"(total {(now - self._t0) * 1000:.0f}ms, thread={threading.current_thread().name})"
        )
