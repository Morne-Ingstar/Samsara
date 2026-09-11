"""Visual-proof screenshot tool for the Settings card/row layout redesign
(commit dbcd3a3).

Constructs _SettingsWindow directly with a headless stub app (the same
pattern as tests/test_settings.py's _StubApp -- an empty config dict, no
live DictationApp, config.json never touched), switches through every
sidebar tab, and grabs a PNG of the whole window per tab. Any tab whose
content scrolls past the window (QScrollArea.verticalScrollBar().maximum()
> 0) also gets a second grab scrolled to the bottom, so clipped/colliding
content that's off the bottom of the first grab is visible too.

Follows tools/wizard_screenshots.py's qt_runtime discipline: reuses the
existing QApplication instance (or creates the one-and-only one via the
plain constructor, never through samsara.ui.qt_runtime) and never calls
exec() -- events are pumped manually via processEvents() so layout/paint
settles before each grab.

Usage:
    F:\\envs\\sami\\python.exe tools\\settings_screenshots.py

Output: C:\\Users\\Morne\\Documents\\Claude\\ui_proof\\settings\\*.png
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication, QScrollArea

OUT_DIR = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\settings")

# (stack index, display name, filename slug) -- must match _TAB_NAMES /
# self._stack.addWidget(...) order in samsara/ui/settings_qt.py exactly.
# Confirmed directly from source, not assumed:
#   _TAB_NAMES = ["General", "Modes", "Commands", "Sounds", "TTS",
#                 "Ava / Cloud", "Alarms", "Health", "Advanced",
#                 "Help & Support"]
TABS = [
    (0, "General", "general"),
    (1, "Modes", "modes"),
    (2, "Commands", "commands"),
    (3, "Sounds", "sounds"),
    (4, "TTS", "tts"),
    (5, "Ava / Cloud", "ava_cloud"),
    (6, "Alarms", "alarms"),
    (7, "Health", "health"),
    (8, "Advanced", "advanced"),
    (9, "Help & Support", "help_support"),
]


class _StubApp:
    """Minimal app stand-in for headlessly constructing _SettingsWindow --
    identical in shape to tests/test_settings.py's _StubApp. All
    _build_*_tab methods read config via .get(key, default), so an empty
    config dict is enough; this deliberately does not touch config.json or
    require a live DictationApp/Samsara instance."""

    def __init__(self):
        self.config = {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


def _pump(app: QApplication, ms: int) -> None:
    end = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)

    from samsara.ui.settings_qt import _SettingsWindow, _TAB_NAMES

    # Ground-truth check: fail loudly rather than silently screenshotting
    # the wrong tabs if settings_qt.py's tab list ever changes shape.
    expected_names = [name for _, name, _ in TABS]
    if list(_TAB_NAMES) != expected_names:
        print("FAILED: _TAB_NAMES in samsara/ui/settings_qt.py no longer "
              f"matches this script's TABS list.\n  actual:   {_TAB_NAMES}\n"
              f"  expected: {expected_names}")
        return 1

    win = _SettingsWindow(_StubApp())
    win.show()
    win.raise_()
    win.activateWindow()
    _pump(app, 500)

    # row -> stack index map built in __init__; invert it so we can drive
    # the sidebar (not just the stack) the same way a real click would.
    stack_index_to_row = {v: k for k, v in win._sidebar_row_to_stack_index.items()}

    failures = []
    saved = []

    for stack_index, display_name, slug in TABS:
        try:
            row = stack_index_to_row.get(stack_index)
            if row is not None:
                win._sidebar.setCurrentRow(row)
            else:
                win._stack.setCurrentIndex(stack_index)
            _pump(app, 400)

            current = win._stack.currentWidget()
            if isinstance(current, QScrollArea):
                current.verticalScrollBar().setValue(0)
            _pump(app, 150)

            top_path = OUT_DIR / f"{slug}.png"
            pixmap = win.grab()
            ok = pixmap.save(str(top_path))
            print(f"{'saved' if ok else 'FAILED to save'}: {top_path} "
                  f"({pixmap.width()}x{pixmap.height()})")
            if ok:
                saved.append((top_path, pixmap.width(), pixmap.height()))
            else:
                failures.append((display_name, "pixmap.save() returned False"))

            if isinstance(current, QScrollArea):
                vbar = current.verticalScrollBar()
                if vbar.maximum() > 0:
                    vbar.setValue(vbar.maximum())
                    _pump(app, 200)
                    bottom_path = OUT_DIR / f"{slug}_bottom.png"
                    pixmap_bottom = win.grab()
                    ok_b = pixmap_bottom.save(str(bottom_path))
                    print(f"{'saved' if ok_b else 'FAILED to save'}: {bottom_path} "
                          f"({pixmap_bottom.width()}x{pixmap_bottom.height()})")
                    if ok_b:
                        saved.append((bottom_path, pixmap_bottom.width(), pixmap_bottom.height()))
                    else:
                        failures.append((display_name, "bottom pixmap.save() returned False"))
                    vbar.setValue(0)
        except Exception:
            import traceback
            print(f"FAILED: {display_name} tab (index {stack_index})")
            traceback.print_exc()
            failures.append((display_name, "exception during render (see traceback above)"))

    win.close()

    print("\n--- summary ---")
    print(f"{len(saved)} PNG(s) saved to {OUT_DIR}")
    for path, w, h in saved:
        print(f"  {path.name}: {w}x{h}")
    if failures:
        print(f"{len(failures)} tab(s) FAILED:")
        for name, reason in failures:
            print(f"  {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
