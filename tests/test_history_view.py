"""Tests for samsara.ui.history_view.HistoryView -- the unified,
day-grouped history list embedded by BOTH the standalone history_qt.py
window and main_window_qt.py's History tab.

Pure-function tests (_pill_for_row, _matches_type_filter) need no Qt.
Construction tests use the session-scoped `qapp` fixture (tests/conftest.py)
and pump the Qt event loop briefly since row loading happens on a
background thread, results marshaled back via Signal.
"""

import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.ui import theme
from samsara.ui.history_view import HistoryView, _pill_for_row, _matches_type_filter


def _pump(app, ms=400):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _make_store(tmp_path, name="history.db"):
    db_path = tmp_path / name
    manager = HistoryManager(db_path=str(db_path))
    return manager, HistoryStore(manager)


# ============================================================================
# Pure helpers -- no Qt
# ============================================================================

class TestPillForRow:
    def test_no_pill_for_plain_dictation(self):
        assert _pill_for_row("dictation", "success") is None

    def test_command_pill(self):
        label, color = _pill_for_row("command", "success")
        assert label == "Command"

    def test_wake_command_pill(self):
        label, color = _pill_for_row("wake_command", "success")
        assert label == "Wake"

    def test_failed_entry_type_pill(self):
        label, color = _pill_for_row("failed", "failed")
        assert label == "Failed"
        assert color == theme.ERROR

    def test_failed_status_with_other_entry_type_still_gets_failed_pill(self):
        """A dictation row that failed must show the Failed pill, not be
        silently treated as plain dictation (the capability restored from
        the old _HistoryPanel)."""
        label, color = _pill_for_row("dictation", "failed")
        assert label == "Failed"
        assert color == theme.ERROR

    def test_failed_entry_type_with_default_success_status_is_still_red(self):
        """Regression: some failure paths set entry_type='failed' but leave
        status at its 'success' default (HistoryManager.add()'s default) --
        this must NOT fall through to the generic accent-colored pill. Caught
        via the history_screenshots.py seed data, which hits exactly this
        combination (store.append('failed', text) never sets status)."""
        label, color = _pill_for_row("failed", "success")
        assert label == "Failed"
        assert color == theme.ERROR


class TestMatchesTypeFilter:
    def test_all_matches_everything(self):
        assert _matches_type_filter({"entry_type": "dictation", "status": "success"}, "All")
        assert _matches_type_filter({"entry_type": "failed", "status": "failed"}, "All")

    def test_commands_matches_command_and_wake_command(self):
        assert _matches_type_filter({"entry_type": "command"}, "Commands")
        assert _matches_type_filter({"entry_type": "wake_command"}, "Commands")
        assert not _matches_type_filter({"entry_type": "dictation"}, "Commands")

    def test_dictation_matches_only_dictation_entry_type(self):
        assert _matches_type_filter({"entry_type": "dictation"}, "Dictation")
        assert not _matches_type_filter({"entry_type": "command"}, "Dictation")

    def test_failed_matches_failed_entry_type_or_failed_status(self):
        assert _matches_type_filter({"entry_type": "failed", "status": "failed"}, "Failed")
        assert _matches_type_filter({"entry_type": "dictation", "status": "failed"}, "Failed")
        assert not _matches_type_filter({"entry_type": "dictation", "status": "success"}, "Failed")


# ============================================================================
# Construction -- no singleton/global assumptions
# ============================================================================

class TestConstruction:
    def test_two_independent_instances_do_not_cross_contaminate(self, qapp, tmp_path):
        """The exact scenario HistoryView must support: the standalone
        window and the main-window tab each construct their own instance
        against their own store -- neither may assume there's only ever
        one HistoryView alive, or read from a shared/global store."""
        mgr1, store1 = _make_store(tmp_path, "store1.db")
        store1.append("dictation", "entry in store one")

        mgr2, store2 = _make_store(tmp_path, "store2.db")
        store2.append("dictation", "entry in store two, first")
        store2.append("command", "entry in store two, second")

        view1 = HistoryView(store1)
        view2 = HistoryView(store2)
        _pump(qapp)

        assert len(view1._rows_by_item_id) == 1
        assert len(view2._rows_by_item_id) == 2

        # Mutating/reloading one must not affect the other.
        store1.append("dictation", "a second entry in store one")
        view1.refresh()
        _pump(qapp)

        assert len(view1._rows_by_item_id) == 2
        assert len(view2._rows_by_item_id) == 2   # unchanged

        mgr1.close()
        mgr2.close()

    def test_construction_with_store_none_and_legacy_history_fn(self, qapp):
        legacy = [("2026-01-01T09:00:00", "legacy entry", False)]
        view = HistoryView(None, legacy_history_fn=lambda: legacy)
        _pump(qapp)

        assert len(view._rows_by_item_id) == 1

    def test_construction_with_no_store_and_no_legacy_shows_empty_state(self, qapp):
        view = HistoryView(None)
        _pump(qapp)

        assert len(view._rows_by_item_id) == 0
        assert view._list.count() >= 1   # the empty-state placeholder item

    def test_failed_filter_end_to_end(self, qapp, tmp_path):
        mgr, store = _make_store(tmp_path)
        store.append("dictation", "a normal entry")
        failed_id = store.append("dictation", "this one failed")
        mgr._conn.execute("UPDATE history SET status=? WHERE id=?", ("failed", failed_id))
        mgr._conn.commit()

        view = HistoryView(store)
        _pump(qapp)
        assert len(view._rows_by_item_id) == 2

        view._filter.setCurrentText("Failed")
        _pump(qapp)
        assert len(view._rows_by_item_id) == 1

        mgr.close()

    def test_detail_pane_shows_and_hides_on_selection(self, qapp, tmp_path):
        mgr, store = _make_store(tmp_path)
        store.append("dictation", "first entry")
        store.append("dictation", "second entry")

        view = HistoryView(store)
        view.show()
        _pump(qapp)

        assert view._detail.isVisible() is False

        view._list.setCurrentRow(1)   # row 0 is the "Today" header
        assert view._detail.isVisible() is True
        assert view._detail.toPlainText() != ""

        view._list.setCurrentRow(-1)
        assert view._detail.isVisible() is False

        mgr.close()
