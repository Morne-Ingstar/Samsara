"""Visual-proof screenshot tool for the unified Dictation History view
(list view, day-grouping, type pills -- see samsara/ui/history_view.py,
embedded by BOTH samsara/ui/history_qt.py's standalone window and
samsara/ui/main_window_qt.py's History tab).

Constructs windows directly (bypassing qt_runtime's thread-marshaling,
since this runs standalone outside the app), seeds a temp SQLite history DB
with ~15 fake entries spanning 3 days (including a failed row), shows each
window, waits ~500ms for layout/paint AND the background row-loading
thread to settle, and saves a PNG. Same pattern as
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


def _pump(app: QApplication, ms: int = 400) -> None:
    """Process events without showing/grabbing -- lets an async row-load
    (background thread + Signal) finish before the next interaction, e.g.
    after changing the filter dropdown. Skipping this before selecting a
    row is exactly the bug this tool caught: HistoryView's _render_rows()
    clears and rebuilds the QListWidgetItems on every reload, so selecting
    a row and then letting an in-flight reload land wipes the selection
    out from under you."""
    end = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _settle_and_grab(app: QApplication, widget, out_path: Path, ms: int = 500) -> None:
    widget.show()
    widget.raise_()
    widget.activateWindow()
    _pump(app, ms)
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
        # HistoryView's row loading runs on a background thread (results
        # marshaled back via Signal) -- give it a beat before the first grab.
        _settle_and_grab(app, win, OUT_DIR / "history_list_default.png")

        # Type filter applied -- Commands-only view.
        win._view._filter.setCurrentText("Commands")
        _settle_and_grab(app, win, OUT_DIR / "history_list_commands_filter.png")

        # Failed-only view -- the seeded failed row's red pill + red-tinted
        # text (the capability restored from the old _HistoryPanel).
        win._view._filter.setCurrentText("Failed")
        _settle_and_grab(app, win, OUT_DIR / "history_list_failed_filter.png")
        win._view._filter.setCurrentText("All")
        _pump(app)   # let the "All" reload land BEFORE selecting a row --
                      # _render_rows() clears/rebuilds items on every
                      # reload, so selecting first and letting an in-flight
                      # reload land afterward silently wipes the selection.

        # A row selected -- shows the selected/ACCENT-tinted state AND the
        # restored collapsible detail pane.
        if win._view._list.count() > 1:
            win._view._list.setCurrentRow(1)
        _settle_and_grab(app, win, OUT_DIR / "history_list_row_selected.png")

        win.close()
        manager.close()
    except Exception:
        import traceback
        print("FAILED: history window")
        traceback.print_exc()

    # ---- Full main window, History tab active (sidebar + status bar +
    # embedded HistoryView) -- the actual unification this tool proves. ----
    try:
        from samsara.history import HistoryManager
        from samsara.history_store import HistoryStore
        from samsara.ui.main_window_qt import _MainWindow

        main_db_path = OUT_DIR / "history_screenshots_mainwindow.db"
        if main_db_path.exists():
            main_db_path.unlink()
        main_manager = HistoryManager(db_path=str(main_db_path))
        main_store = HistoryStore(main_manager)
        _seed_history(main_manager, main_store)

        fake_main_app = types.SimpleNamespace(
            config={
                'mode': 'hold', 'wake_word_enabled': True,
                'wake_word_config': {'phrase': 'jarvis'}, 'microphone': None,
            },
            available_mics=[], recording=False, continuous_active=False,
            wake_word_active=True, snoozed=False,
            history=[], history_store=main_store, history_db=main_manager,
        )

        main_win = _MainWindow(fake_main_app)
        main_win._activate("History")
        _settle_and_grab(app, main_win, OUT_DIR / "main_window_history_tab.png")

        main_win.close()
        main_manager.close()
    except Exception:
        import traceback
        print("FAILED: main window (History tab)")
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
