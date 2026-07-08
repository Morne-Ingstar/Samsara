"""Visual-proof screenshot tool for the redesigned Dictation History window
(list view, day-grouping, type pills -- see samsara/ui/history_qt.py).

Constructs the window directly (bypassing qt_runtime's thread-marshaling,
since this runs standalone outside the app), seeds a temp SQLite history DB
with ~15 fake entries spanning 3 days, shows it, waits ~500ms for
layout/paint to settle, and saves a PNG. Same pattern as
tools/wizard_screenshots.py.

Usage:
    F:\\envs\\sami\\python.exe tools\\history_screenshots.py

Output: C:\\Temp\\samsara_ui_proof\\*.png (same directory
wizard_screenshots.py uses, so every UI screenshot lands in one place for
review).
"""
from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

OUT_DIR = Path(r"C:\Temp\samsara_ui_proof")
SEED_DB_PATH = OUT_DIR / "history_screenshots_seed.db"


def _settle_and_grab(app: QApplication, widget, out_path: Path, ms: int = 500) -> None:
    widget.show()
    widget.raise_()
    widget.activateWindow()
    end = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    pixmap = widget.grab()
    ok = pixmap.save(str(out_path))
    print(f"{'saved' if ok else 'FAILED to save'}: {out_path}")


def _seed_history(manager, store) -> None:
    """~15 fake entries spanning 3 days -- one Command-type row, one row
    with unicode text. Timestamps are backdated via direct SQL UPDATE since
    HistoryManager.add() always stamps "now" -- same technique used to spot
    -check day-grouping during development."""
    now = datetime.now()

    entries = [
        # (day_offset, hour, entry_type, text)
        (0, 9, "dictation", "Good morning, let's get started on the quarterly report"),
        (0, 9, "dictation", "The meeting is scheduled for three thirty this afternoon"),
        (0, 10, "command", "open chrome"),
        (0, 11, "dictation", "Please remember to follow up with the design team"),
        (0, 13, "wake_command", "jarvis stop listening"),
        (0, 15, "dictation", "unicode smoke test: → café \U0001f3a4"),
        (1, 8, "dictation", "Yesterday's standup notes: blocked on the API review"),
        (1, 9, "dictation", "Draft reply to the customer about the shipping delay"),
        (1, 12, "command", "volume up"),
        (1, 14, "dictation", "Reminder to buy groceries after work today"),
        (1, 18, "failed", ""),
        (3, 10, "dictation", "Three days ago I started outlining this feature"),
        (3, 11, "dictation", "The quick brown fox jumps over the lazy dog"),
        (3, 16, "dictation", "Testing the stress test wizard homophone step there their they're"),
        (3, 17, "dictation", "This is the last seeded entry for the oldest day group"),
    ]

    # recent_windowed() orders by id DESC (id correlates with recency, not
    # the backdated timestamp column) -- insert oldest-first so ids end up
    # monotonically increasing with recency, same as they would in a real
    # running app.
    entries.sort(key=lambda e: (-e[0], e[1]))

    for day_offset, hour, entry_type, text in entries:
        row_id = store.append(entry_type, text)
        ts = (now - timedelta(days=day_offset)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        manager._conn.execute(
            "UPDATE history SET timestamp=? WHERE id=?", (ts.isoformat(), row_id)
        )
    manager._conn.commit()


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if SEED_DB_PATH.exists():
        SEED_DB_PATH.unlink()
    app = QApplication.instance() or QApplication(sys.argv)

    try:
        from samsara.history import HistoryManager
        from samsara.history_store import HistoryStore
        from samsara.ui.history_qt import _HistoryWindow

        manager = HistoryManager(db_path=str(SEED_DB_PATH))
        store = HistoryStore(manager)
        _seed_history(manager, store)

        fake_app = types.SimpleNamespace(
            config={}, history=[], history_store=store, history_db=manager,
        )

        win = _HistoryWindow(fake_app)
        _settle_and_grab(app, win, OUT_DIR / "history_list_default.png")

        # Type filter applied -- shows the Commands-only view.
        win._filter.setCurrentText("Commands")
        _settle_and_grab(app, win, OUT_DIR / "history_list_commands_filter.png")
        win._filter.setCurrentText("All")

        # A row selected -- shows the selected/ACCENT-tinted state.
        if win._list.count() > 1:
            win._list.setCurrentRow(1)
        _settle_and_grab(app, win, OUT_DIR / "history_list_row_selected.png")

        win.close()
        manager.close()
    except Exception:
        import traceback
        print("FAILED: history window")
        traceback.print_exc()

    # ---- Empty state -- a fresh, unseeded DB ------------------------------
    try:
        from samsara.history import HistoryManager
        from samsara.history_store import HistoryStore
        from samsara.ui.history_qt import _HistoryWindow

        empty_db_path = OUT_DIR / "history_screenshots_empty.db"
        if empty_db_path.exists():
            empty_db_path.unlink()
        empty_manager = HistoryManager(db_path=str(empty_db_path))
        empty_store = HistoryStore(empty_manager)
        fake_app_empty = types.SimpleNamespace(
            config={}, history=[], history_store=empty_store, history_db=empty_manager,
        )
        win_empty = _HistoryWindow(fake_app_empty)
        _settle_and_grab(app, win_empty, OUT_DIR / "history_list_empty_state.png")
        win_empty.close()
        empty_manager.close()
    except Exception:
        import traceback
        print("FAILED: history empty-state window")
        traceback.print_exc()

    return 0


if __name__ == "__main__":
    sys.exit(main())
