"""Regression coverage for History row widgets never becoming top-level."""

import time
from datetime import datetime, timedelta

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.ui import history_view as hv


def _rows(count):
    now = datetime.now()
    return [
        {
            "id": index + 1,
            "timestamp": (now - timedelta(minutes=index)).isoformat(timespec="seconds"),
            "entry_type": "dictation",
            "status": "success",
            "display_text": f"history entry {index}",
            "raw_text": f"history entry {index}",
            "mode": "wake",
        }
        for index in range(count)
    ]


def _pump_until(app, condition, timeout_seconds=5):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_large_history_population_never_constructs_a_top_level_row_widget(qapp, monkeypatch):
    """Sample Qt's real top-level list immediately before item reparenting.

    A first page can contain 200 rows, and loading another page repeats the
    construction path.  Four hundred rows make a regression visible without
    relying on a human noticing a sub-millisecond flash.
    """
    observed_top_levels = []
    original = hv.QListWidget.setItemWidget

    def sample_before_reparenting(list_widget, item, widget):
        if widget in qapp.topLevelWidgets():
            observed_top_levels.append(widget)
        return original(list_widget, item, widget)

    monkeypatch.setattr(hv.QListWidget, "setItemWidget", sample_before_reparenting)
    view = hv.HistoryView(None)
    view._has_more = True  # Exercise the Load-older row too.

    view._render_rows(_rows(400), append=False)

    assert observed_top_levels == []


def test_first_open_builds_one_eager_page_then_offers_paging(qapp, tmp_path):
    """Opening a large history renders the first page, not the whole store."""
    manager = HistoryManager(db_path=str(tmp_path / "history.db"))
    store = HistoryStore(manager)
    for index in range(hv._PAGE_SIZE * 2):
        store.append("dictation", f"history entry {index}")

    view = hv.HistoryView(store)
    assert _pump_until(qapp, lambda: len(view._rows_by_item_id) == hv._PAGE_SIZE)
    assert view._load_older_item is not None
    assert len(view._rows_by_item_id) == hv._PAGE_SIZE
    manager.close()
