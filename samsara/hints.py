"""HintManager -- one-shot contextual hint system for Samsara.

Each hint fires at most once per install, at a natural trigger point, then
never again.  Shown history and named counters persist across sessions in
~/.samsara/hints_shown.json.

Public API:
    maybe_show(hint_id, message, delay_s)   show hint if unseen and enabled
    increment(counter_id) -> int            named counter for trigger-after-N
    disable() / enable()                    permanent off/on
    reset()                                 clear history so hints replay
"""

import json
import os
import threading
from pathlib import Path

_DATA_DIR = Path(os.path.expanduser("~")) / ".samsara"


class HintManager:
    _FLUSH_DELAY = 5.0   # seconds to coalesce counter-only writes

    def __init__(self, app) -> None:
        self._app      = app
        self._shown: set  = set()
        self._counters: dict = {}
        self._toast    = None   # active HintToast; None when idle
        self._enabled: bool = app.config.get('hints_enabled', True)

        self._save_lock = threading.Lock()   # guards _pending_save_timer only
        self._pending_save_timer = None

        _DATA_DIR.mkdir(exist_ok=True)
        self._hints_file = _DATA_DIR / "hints_shown.json"
        self._load()

    # ── Public API ──────────────────────────────────────────────────────────────

    def maybe_show(self, hint_id: str, message: str, delay_s: float = 1.5) -> None:
        """Show hint toast if not already shown and hints are enabled.

        Safe to call from any thread.  The toast always appears on the Qt
        main thread after delay_s seconds.  Skipped when recording or TTS
        is actively speaking.
        """
        if not self._enabled or hint_id in self._shown:
            return
        if getattr(self._app, 'recording', False):
            return

        self._shown.add(hint_id)
        self._save()

        def _show():
            if getattr(self._app, 'recording', False):
                return
            _ac = getattr(self._app, 'audio_coordinator', None)
            if _ac and getattr(_ac, 'is_speaking', False):
                return
            if self._toast is not None:
                return
            from samsara.ui.hint_toast import HintToast
            self._toast = HintToast(
                message,
                on_dismiss=self._on_dismiss,
                on_disable=self._on_disable,
            )
            self._toast.show()

        try:
            from PySide6.QtCore import QTimer
            from PySide6.QtWidgets import QApplication
            qt_app = QApplication.instance()
            if qt_app is not None:
                QTimer.singleShot(int(delay_s * 1000), qt_app, _show)
        except Exception:
            pass

    def increment(self, counter_id: str) -> int:
        """Increment a named counter and return the new value.

        The in-memory update is synchronous.  The disk write is deferred and
        coalesced: rapid calls within _FLUSH_DELAY seconds produce at most
        one write instead of one per call.
        """
        self._counters[counter_id] = self._counters.get(counter_id, 0) + 1
        self._schedule_save()
        return self._counters[counter_id]

    def get_counter(self, counter_id: str) -> int:
        return self._counters.get(counter_id, 0)

    def disable(self) -> None:
        """Permanently disable hints (until re-enabled in Settings)."""
        self._enabled = False
        try:
            self._app.update_config_and_save({'hints_enabled': False})
        except Exception:
            pass

    def enable(self) -> None:
        self._enabled = True
        try:
            self._app.update_config_and_save({'hints_enabled': True})
        except Exception:
            pass

    def reset(self) -> None:
        """Clear shown history -- all hints will fire again on next trigger."""
        self._shown.clear()
        self._counters.clear()
        self._save()

    # ── Internal ────────────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        """Cancel any pending debounced write and flush to disk immediately.

        Call during clean shutdown so counters inside the debounce window
        aren't lost.  Safe to call from any thread.
        """
        with self._save_lock:
            if self._pending_save_timer is not None:
                self._pending_save_timer.cancel()
                self._pending_save_timer = None
        self._save()

    def _schedule_save(self) -> None:
        """Cancel any pending save timer and start a fresh debounce timer."""
        with self._save_lock:
            if self._pending_save_timer is not None:
                self._pending_save_timer.cancel()
            t = threading.Timer(self._FLUSH_DELAY, self._save)
            t.daemon = True   # never block process shutdown
            self._pending_save_timer = t
            t.start()

    def _on_dismiss(self) -> None:
        self._toast = None

    def _on_disable(self) -> None:
        self._toast = None
        self.disable()

    def _load(self) -> None:
        try:
            if self._hints_file.exists():
                data = json.loads(self._hints_file.read_text(encoding='utf-8'))
                self._shown    = set(data.get('shown', []))
                self._counters = dict(data.get('counters', {}))
        except Exception:
            pass

    def _save(self) -> None:
        tmp = str(self._hints_file) + '.tmp'
        try:
            data = {'shown': sorted(self._shown), 'counters': self._counters}
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._hints_file)
        except Exception as e:
            print(f"[HINTS] Save failed: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
