"""Tests for samsara.ui.history_view.HistoryView -- the unified,
day-grouped history list embedded by BOTH the standalone history_qt.py
window and main_window_qt.py's History tab.

Pure-function tests (row_outcome, _matches_type_filter) need no Qt.
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
from samsara.ui import history_view as hv
from samsara.ui.history_view import (
    HistoryView, row_outcome, _matches_type_filter, _is_empty_wake_attempt,
    _SCOPE_LAST_7_DAYS, _SCOPE_ALL,
)


def _pump(app, ms=400):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def _pump_until(app, condition, timeout_ms=3000):
    """Poll until `condition()` is true or timeout_ms elapses. Row loading
    runs on a background thread (SQLite query + Signal marshal back to the
    Qt thread) -- a fixed short sleep is flaky under full-suite CPU
    contention, so wait for the actual result instead of guessing a
    duration."""
    end = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def _make_store(tmp_path, name="history.db"):
    db_path = tmp_path / name
    manager = HistoryManager(db_path=str(db_path))
    return manager, HistoryStore(manager)


# ============================================================================
# Pure helpers -- no Qt
# ============================================================================

class TestRowOutcome:
    """Queue 79: the pill is the entry's outcome, derived from recorded
    columns. Plain typed dictation carries no pill."""

    def test_no_pill_for_plain_dictation(self):
        assert row_outcome({"entry_type": "dictation", "status": "success"}) is None

    def test_command_pill(self):
        outcome = row_outcome({"entry_type": "command", "status": "success"})
        assert outcome.label == "Command"

    def test_wake_command_pill(self):
        outcome = row_outcome({"entry_type": "wake_command", "status": "success", "display_text": "stop listening"})
        assert outcome.label == "Command"

    def test_failed_entry_type_pill(self):
        outcome = row_outcome({"entry_type": "failed", "status": "failed", "display_text": "[FAILED] x"})
        assert outcome.label == "Failed"
        assert hv._pill_colours()[outcome.kind][0] == theme.ERROR

    def test_failed_dictation_is_not_typed_not_generic_failure(self):
        """A dictation whose paste failed still has its words -- the pill
        says what happened to them (the entry the user most needs back)."""
        outcome = row_outcome({"entry_type": "dictation", "status": "failed", "display_text": "hi"})
        assert outcome.label == "Not typed"
        assert hv._pill_colours()[outcome.kind][0] == theme.WARNING

    def test_failed_entry_type_with_default_success_status_is_still_failed(self):
        """Some failure paths set entry_type='failed' but leave status at its
        'success' default -- still a failure, never the accent pill."""
        outcome = row_outcome({"entry_type": "failed", "status": "success", "display_text": "[FAILED] x"})
        assert outcome.label == "Failed"
        assert hv._pill_colours()[outcome.kind][0] == theme.ERROR


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

    def test_not_typed_matches_failed_entry_type_or_failed_status(self):
        # "Failed" is the pre-79 name, kept as an alias of "Not typed".
        for name in ("Failed", "Not typed"):
            assert _matches_type_filter({"entry_type": "failed", "status": "failed"}, name)
            assert _matches_type_filter({"entry_type": "dictation", "status": "failed"}, name)
            assert not _matches_type_filter({"entry_type": "dictation", "status": "success"}, name)


class TestEmptyWakeAttempts:
    def test_hotkey_no_speech_rows_are_hidden_too(self):
        """Queue 79: the recorder writes no-speech rows from the hotkey path
        as well (mode 'hold', status 'empty'); pre-79 they showed as red
        Failed entries."""
        assert _is_empty_wake_attempt({
            "mode": "hold", "entry_type": "failed", "status": "empty",
            "display_text": "(no speech detected)",
        })

    def test_only_empty_wake_rows_are_hidden(self):
        assert _is_empty_wake_attempt({
            "mode": "wake", "entry_type": "failed", "display_text": "(no speech detected)",
        })
        assert _is_empty_wake_attempt({
            "mode": "wake", "entry_type": "failed", "display_text": "  ",
        })
        assert not _is_empty_wake_attempt({
            "mode": "wake", "entry_type": "failed", "display_text": "real command",
        })
        assert not _is_empty_wake_attempt({
            "mode": "dictate", "entry_type": "dictation", "display_text": "",
        })


# ============================================================================
# Construction -- no singleton/global assumptions
# ============================================================================

class TestConstruction:
    def test_default_scope_and_empty_wake_toggle(self, qapp, tmp_path, monkeypatch):
        class FakeSettings:
            values = {}

            def __init__(self, *_args):
                pass

            def value(self, key, default=None, type=None):
                return self.values.get(key, default)

            def setValue(self, key, value):
                self.values[key] = value

        monkeypatch.setattr("samsara.ui.history_view.QSettings", FakeSettings)
        mgr, store = _make_store(tmp_path)
        recent_id = mgr.add("spoken", display_text="spoken", mode="wake", entry_type="dictation")
        empty_id = mgr.add(
            "", display_text="(no speech detected)", mode="wake",
            status="empty", entry_type="failed",
        )
        old_id = mgr.add("old", display_text="old", entry_type="dictation")
        mgr._conn.execute(
            "UPDATE history SET timestamp=? WHERE id=?",
            ("2000-01-01T00:00:00", old_id),
        )
        mgr._conn.commit()

        view = HistoryView(store)

        assert view._scope.currentText() == _SCOPE_LAST_7_DAYS
        assert view._show_empty_wake.isChecked() is False
        default_rows = view._fetch_rows("", "All", None)
        assert {row["id"] for row in default_rows} == {recent_id}
        all_rows = view._fetch_rows("", "All", None, _SCOPE_ALL, True)
        assert {row["id"] for row in all_rows} == {recent_id, empty_id, old_id}
        mgr.close()

    def test_saved_scope_and_toggle_are_restored(self, qapp, monkeypatch):
        class FakeSettings:
            values = {
                "history/date_scope": _SCOPE_ALL,
                "history/show_empty_wake_attempts": True,
            }

            def __init__(self, *_args):
                pass

            def value(self, key, default=None, type=None):
                return self.values.get(key, default)

            def setValue(self, key, value):
                self.values[key] = value

        monkeypatch.setattr("samsara.ui.history_view.QSettings", FakeSettings)
        view = HistoryView(None)
        assert view._scope.currentText() == _SCOPE_ALL
        assert view._show_empty_wake.isChecked() is True
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
        _pump_until(qapp, lambda: len(view1._rows_by_item_id) >= 1 and len(view2._rows_by_item_id) >= 2)

        assert len(view1._rows_by_item_id) == 1
        assert len(view2._rows_by_item_id) == 2

        # Mutating/reloading one must not affect the other.
        store1.append("dictation", "a second entry in store one")
        view1.refresh()
        _pump_until(qapp, lambda: len(view1._rows_by_item_id) >= 2)

        assert len(view1._rows_by_item_id) == 2
        assert len(view2._rows_by_item_id) == 2   # unchanged

        mgr1.close()
        mgr2.close()

    def test_construction_with_store_none_and_legacy_history_fn(self, qapp):
        legacy = [("2026-01-01T09:00:00", "legacy entry", False)]
        view = HistoryView(None, legacy_history_fn=lambda: legacy)
        _pump_until(qapp, lambda: len(view._rows_by_item_id) >= 1)

        assert len(view._rows_by_item_id) == 1

    def test_construction_with_no_store_and_no_legacy_shows_empty_state(self, qapp):
        view = HistoryView(None)
        _pump_until(qapp, lambda: view._list.count() >= 1)

        assert len(view._rows_by_item_id) == 0
        assert view._list.count() >= 1   # the empty-state placeholder item

    def test_failed_filter_end_to_end(self, qapp, tmp_path):
        mgr, store = _make_store(tmp_path)
        store.append("dictation", "a normal entry")
        failed_id = store.append("dictation", "this one failed")
        mgr._conn.execute("UPDATE history SET status=? WHERE id=?", ("failed", failed_id))
        mgr._conn.commit()

        view = HistoryView(store)
        _pump_until(qapp, lambda: len(view._rows_by_item_id) >= 2)
        assert len(view._rows_by_item_id) == 2

        view._filter.setCurrentText("Failed")
        _pump_until(qapp, lambda: len(view._rows_by_item_id) == 1)
        assert len(view._rows_by_item_id) == 1

        mgr.close()

    def test_detail_pane_shows_and_hides_on_selection(self, qapp, tmp_path):
        mgr, store = _make_store(tmp_path)
        store.append("dictation", "first entry")
        store.append("dictation", "second entry")

        view = HistoryView(store)
        view.show()
        _pump_until(qapp, lambda: len(view._rows_by_item_id) >= 2)

        assert view._detail.isVisible() is False

        view._list.setCurrentRow(1)   # row 0 is the "Today" header
        assert view._detail.isVisible() is True
        assert view._detail.toPlainText() != ""

        view._list.setCurrentRow(-1)
        assert view._detail.isVisible() is False

        mgr.close()
