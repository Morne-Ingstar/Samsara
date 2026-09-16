"""Queue 164 throwaway runtime probe for HistoryView top-level widgets.

Copies the live history SQLite database before opening it, then exercises the
same HistoryQt window shell and three filter changes.  The application-level
filter records any widget shown while it is top-level, alongside its most
recent parentless-construction event stack.  Output intentionally contains no
history text.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback
import types
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QApplication, QWidget

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.ui.history_qt import _HistoryWindow


OUT = Path(__file__).with_name("history_flash_164.txt")
LIVE_DB = Path(os.environ.get("SAMSARA_HOME_DIR", Path.home() / ".samsara")) / "history.db"


class _TopLevelProbe(QObject):
    """Record only unexpected top-level shows; never record row text."""

    def __init__(self):
        super().__init__()
        self.parentless_stack: dict[int, list[str]] = {}
        self.hits: list[tuple[str, str, list[str]]] = []

    def eventFilter(self, obj, event):  # noqa: N802 - Qt API name
        if not isinstance(obj, QWidget):
            return False
        if event.type() in (QEvent.Type.Polish, QEvent.Type.PolishRequest) and obj.parent() is None:
            self.parentless_stack.setdefault(id(obj), traceback.format_stack(limit=30))
        if event.type() == QEvent.Type.Show and obj.isWindow():
            name = obj.objectName() or "-"
            self.hits.append((type(obj).__name__, name, self.parentless_stack.get(
                id(obj), traceback.format_stack(limit=30),
            )))
        return False


def _pump_until(app: QApplication, predicate, timeout=8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


def _write_result(probe: _TopLevelProbe, initial_count: int, reload_counts: list[int]) -> None:
    lines = [
        "Queue 164 HistoryView top-level runtime probe",
        f"source_db={LIVE_DB}",
        f"initial_rows={initial_count}",
        f"filter_rows={reload_counts}",
        f"unexpected_top_level_show_count={len(probe.hits)}",
        "",
    ]
    for index, (class_name, object_name, stack) in enumerate(probe.hits, 1):
        lines.extend((f"[{index}] class={class_name} object_name={object_name}", "stack:"))
        lines.extend(line.rstrip() for line in stack)
        lines.append("")
    result = "\n".join(lines)
    if os.environ.get("HISTORY_FLASH_164_APPEND"):
        with OUT.open("a", encoding="utf-8") as handle:
            handle.write("\n\n--- post-fix rerun ---\n" + result)
    else:
        OUT.write_text(result, encoding="utf-8")


def main() -> int:
    if not LIVE_DB.exists():
        OUT.write_text(f"Queue 164 probe skipped: live database not found at {LIVE_DB}\n", encoding="utf-8")
        return 2
    temp_dir = Path(tempfile.mkdtemp(prefix="samsara-history-164-"))
    db_copy = temp_dir / "history.db"
    try:
        shutil.copy2(LIVE_DB, db_copy)
        app = QApplication.instance() or QApplication(sys.argv)
        probe = _TopLevelProbe()
        app.installEventFilter(probe)
        manager = HistoryManager(db_path=str(db_copy))
        store = HistoryStore(manager)
        shell = types.SimpleNamespace(config={}, history=[], history_store=store)
        window = _HistoryWindow(shell)
        window.show()
        view = window._view
        if not _pump_until(app, lambda: bool(view._rows_by_item_id)):
            raise RuntimeError("initial HistoryView page did not arrive")
        initial_count = len(view._rows_by_item_id)
        probe.hits.clear()  # the persistent History window is expected; only inspect the view lifecycle.
        counts = []
        for choice in ("Dictation", "Commands", "All"):
            before = view._request_generation
            view._filter.setCurrentText(choice)
            if not _pump_until(app, lambda: view._request_generation > before and not view._relayout_pending):
                raise RuntimeError(f"filter reload did not settle for {choice}")
            # Give queued rows-ready and show events a full pass after the new generation.
            _pump_until(app, lambda: True, timeout=0.05)
            counts.append(len(view._rows_by_item_id))
        _write_result(probe, initial_count, counts)
        window.hide()
        manager.close()
        return 0
    except Exception:
        OUT.write_text("Queue 164 probe failed:\n" + traceback.format_exc(), encoding="utf-8")
        return 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
