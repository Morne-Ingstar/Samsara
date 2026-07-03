"""
Config file watcher for Samsara.

Monitors config.json for external changes (manual edits while the app
is running) and fires a callback when a valid change is detected.

Uses watchdog if it is installed, otherwise falls back to a 2-second
polling thread. Both paths share the same debounce and JSON-validation
logic.
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler as _WatchdogBase
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False

_DEBOUNCE_DEFAULT_MS = 500


class ConfigWatcher:
    """Watch a JSON config file and fire a callback on valid external changes.

    The callback receives the newly parsed config dict and is called from a
    background daemon thread.  The caller is responsible for thread-safety
    of anything it does inside the callback.

    Debounce: rapid successive file-system events (e.g. editor
    write-then-rename) are collapsed into one callback fired debounce_ms
    after the last event.
    """

    def __init__(
        self,
        config_path: Path,
        callback: Callable[[dict], None],
        debounce_ms: int = _DEBOUNCE_DEFAULT_MS,
    ):
        self._path = Path(config_path).resolve()
        self._callback = callback
        self._debounce = debounce_ms / 1000.0
        self._timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self._stopped = False
        self._observer = None
        self._poll_thread: Optional[threading.Thread] = None

        # Snapshot the mtime so the initial file presence is not treated as
        # a change on the first poll tick.
        try:
            self._last_mtime: Optional[float] = self._path.stat().st_mtime
        except OSError:
            self._last_mtime = None

    def start(self):
        if _HAS_WATCHDOG:
            self._start_watchdog()
        else:
            self._start_polling()

    def stop(self):
        self._stopped = True
        with self._timer_lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2)
            except Exception as e:
                logger.debug(f"stop: {e}")

    # ── internals ─────────────────────────────────────────────────────────────

    def _schedule_fire(self):
        """Reset the debounce timer on each event."""
        with self._timer_lock:
            if self._stopped:
                return
            if self._timer is not None:
                self._timer.cancel()
            self._timer = thread_registry.timer(
                "config_watch.debounce", self._debounce, self._fire, daemon=True)

    def _fire(self):
        with self._timer_lock:
            self._timer = None
        if self._stopped:
            return

        try:
            new_mtime = self._path.stat().st_mtime
        except OSError:
            return

        if new_mtime == self._last_mtime:
            return  # spurious event — nothing actually changed

        self._last_mtime = new_mtime

        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                new_config = json.load(f)
        except json.JSONDecodeError as exc:
            logger.warning("[CONFIG] External edit ignored — invalid JSON: %s", exc)
            return
        except OSError:
            return

        try:
            self._callback(new_config)
        except Exception as exc:
            logger.error("[CONFIG] Reload callback raised: %s", exc)

    def _start_watchdog(self):
        path_str = str(self._path)

        class _Handler(_WatchdogBase):
            def __init__(self_, schedule_fn):
                super().__init__()
                self_._schedule = schedule_fn

            def on_modified(self_, event):
                if not event.is_directory and Path(event.src_path).resolve() == self._path:
                    self_._schedule()

            def on_created(self_, event):
                if not event.is_directory and Path(event.src_path).resolve() == self._path:
                    self_._schedule()

        handler = _Handler(self._schedule_fire)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._path.parent), recursive=False)
        self._observer.start()
        logger.debug("[CONFIG] Watcher started (watchdog)")

    def _start_polling(self):
        def _poll():
            while not self._stopped:
                time.sleep(2.0)
                if self._stopped:
                    break
                try:
                    mtime = self._path.stat().st_mtime
                    if mtime != self._last_mtime:
                        self._schedule_fire()
                except OSError as e:
                    logger.debug(f"_poll: {e}")

        self._poll_thread = thread_registry.spawn("config-watcher", _poll, daemon=True)
        logger.debug("[CONFIG] Watcher started (polling)")
