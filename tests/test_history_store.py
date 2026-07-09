"""Tests for the persistent history backend:
- samsara.history_store.HistoryStore (the task-shaped façade)
- samsara.history.HistoryManager.recent_windowed (the SQL-side windowing/
  search/type-filter method backing it)
- samsara.ui.history_view.day_label (the pure day-grouping label function --
  moved here from history_qt.py when the list/toolbar UI was extracted into
  a reusable HistoryView shared with main_window_qt.py's History tab)

All DB tests use a tmp_path-backed SQLite file -- never the real
~/.samsara/history.db. Includes a non-cp1252 unicode round-trip
("→ café 🎤") through append+query, guarding against the known
UnicodeEncodeError failure mode on Windows' default locale encoding.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.ui.history_view import day_label


UNICODE_TEXT = "→ café 🎤"


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "history_test.db"
    manager = HistoryManager(db_path=str(db_path))
    s = HistoryStore(manager)
    yield s
    manager.close()


# ============================================================================
# HistoryStore -- append / query / search / type-filter / windowing / delete / clear
# ============================================================================

class TestAppendAndQuery:
    def test_append_returns_row_id(self, store):
        row_id = store.append("dictation", "hello world")
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_query_returns_appended_entries_newest_first(self, store):
        store.append("dictation", "first")
        store.append("dictation", "second")
        store.append("dictation", "third")

        rows = store.query(limit=10)

        assert [r["display_text"] for r in rows] == ["third", "second", "first"]

    def test_query_respects_limit(self, store):
        for i in range(10):
            store.append("dictation", f"entry {i}")

        rows = store.query(limit=3)

        assert len(rows) == 3
        assert rows[0]["display_text"] == "entry 9"

    def test_query_empty_store_returns_empty_list(self, store):
        assert store.query() == []


class TestSearch:
    def test_search_matches_substring(self, store):
        store.append("dictation", "the quick brown fox")
        store.append("dictation", "totally unrelated text")

        rows = store.query(search="quick")

        assert len(rows) == 1
        assert "quick" in rows[0]["display_text"]

    def test_search_case_insensitive(self, store):
        store.append("dictation", "Hello World")

        rows = store.query(search="hello")

        assert len(rows) == 1

    def test_search_no_match_returns_empty(self, store):
        store.append("dictation", "something")

        rows = store.query(search="nonexistent")

        assert rows == []


class TestTypeFilter:
    def test_filters_by_entry_type(self, store):
        store.append("dictation", "a dictation entry")
        store.append("command", "open chrome")
        store.append("wake_command", "jarvis stop")

        rows = store.query(type_filter="command")

        assert len(rows) == 1
        assert rows[0]["entry_type"] == "command"

    def test_no_filter_returns_all_types(self, store):
        store.append("dictation", "a")
        store.append("command", "b")

        rows = store.query()

        assert len(rows) == 2

    def test_search_and_type_filter_combined(self, store):
        store.append("dictation", "open the pod bay doors")
        store.append("command", "open chrome")

        rows = store.query(search="open", type_filter="command")

        assert len(rows) == 1
        assert rows[0]["entry_type"] == "command"


class TestWindowing:
    def test_before_id_fetches_older_page(self, store):
        ids = [store.append("dictation", f"entry {i}") for i in range(5)]

        first_page = store.query(limit=2)
        assert [r["id"] for r in first_page] == [ids[4], ids[3]]

        second_page = store.query(limit=2, before_id=first_page[-1]["id"])
        assert [r["id"] for r in second_page] == [ids[2], ids[1]]

        third_page = store.query(limit=2, before_id=second_page[-1]["id"])
        assert [r["id"] for r in third_page] == [ids[0]]

    def test_before_id_beyond_oldest_returns_empty(self, store):
        row_id = store.append("dictation", "only entry")

        rows = store.query(before_id=row_id)

        assert rows == []

    def test_windowing_combined_with_search(self, store):
        ids = [store.append("dictation", "matching entry") for _ in range(3)]
        store.append("dictation", "completely unrelated text")

        first_page = store.query(search="matching", limit=2)
        assert len(first_page) == 2

        second_page = store.query(search="matching", limit=2, before_id=first_page[-1]["id"])
        assert len(second_page) == 1
        assert second_page[0]["display_text"] == "matching entry"


class TestDeleteAndClear:
    def test_delete_single_id(self, store):
        id1 = store.append("dictation", "keep me")
        id2 = store.append("dictation", "delete me")

        store.delete([id2])

        rows = store.query()
        assert [r["id"] for r in rows] == [id1]

    def test_delete_multiple_ids(self, store):
        ids = [store.append("dictation", f"entry {i}") for i in range(4)]

        store.delete([ids[0], ids[2]])

        remaining = {r["id"] for r in store.query()}
        assert remaining == {ids[1], ids[3]}

    def test_clear_empties_everything(self, store):
        for i in range(5):
            store.append("dictation", f"entry {i}")

        store.clear()

        assert store.query() == []


class TestUnavailableStore:
    """A HistoryStore wrapping None (history_db failed to open at boot)
    must degrade gracefully -- never raise."""

    def test_append_returns_none(self):
        s = HistoryStore(None)
        assert s.append("dictation", "text") is None

    def test_query_returns_empty_list(self):
        s = HistoryStore(None)
        assert s.query() == []

    def test_delete_does_not_raise(self):
        s = HistoryStore(None)
        s.delete([1, 2, 3])

    def test_clear_does_not_raise(self):
        s = HistoryStore(None)
        s.clear()


# ============================================================================
# Unicode round-trip -- the cp1252 UnicodeEncodeError audit
# ============================================================================

class TestUnicodeRoundTrip:
    def test_unicode_text_round_trips_through_append_and_query(self, store):
        row_id = store.append("dictation", UNICODE_TEXT)

        rows = store.query(limit=10)

        assert row_id is not None
        matching = [r for r in rows if r["id"] == row_id]
        assert len(matching) == 1
        assert matching[0]["display_text"] == UNICODE_TEXT
        assert matching[0]["raw_text"] == UNICODE_TEXT

    def test_unicode_text_searchable(self, store):
        store.append("dictation", UNICODE_TEXT)

        rows = store.query(search="café")

        assert len(rows) == 1
        assert rows[0]["display_text"] == UNICODE_TEXT

    def test_unicode_text_survives_db_reopen(self, tmp_path):
        db_path = tmp_path / "unicode_history.db"
        mgr1 = HistoryManager(db_path=str(db_path))
        HistoryStore(mgr1).append("dictation", UNICODE_TEXT)
        mgr1.close()

        mgr2 = HistoryManager(db_path=str(db_path))
        rows = HistoryStore(mgr2).query(limit=10)
        mgr2.close()

        assert len(rows) == 1
        assert rows[0]["display_text"] == UNICODE_TEXT


# ============================================================================
# day_label -- pure day-grouping function (today/yesterday/this-year/other-year)
# ============================================================================

class TestDayLabel:
    NOW = datetime(2026, 7, 8, 15, 30, 0)

    def test_today(self):
        assert day_label(datetime(2026, 7, 8, 9, 0), self.NOW) == "Today"

    def test_today_late_night(self):
        assert day_label(datetime(2026, 7, 8, 23, 59), self.NOW) == "Today"

    def test_yesterday(self):
        assert day_label(datetime(2026, 7, 7, 12, 0), self.NOW) == "Yesterday"

    def test_this_year_shows_weekday_and_date(self):
        result = day_label(datetime(2026, 7, 6, 12, 0), self.NOW)
        assert result == "Mon, Jul 6"

    def test_this_year_no_leading_zero_on_single_digit_day(self):
        result = day_label(datetime(2026, 7, 1, 12, 0), self.NOW)
        assert result == "Wed, Jul 1"

    def test_other_year_shows_full_date(self):
        result = day_label(datetime(2025, 7, 6, 12, 0), self.NOW)
        assert result == "Jul 6, 2025"

    def test_other_year_far_past(self):
        result = day_label(datetime(2019, 12, 25, 8, 0), self.NOW)
        assert result == "Dec 25, 2019"

    def test_boundary_new_years_eve_vs_day_across_year(self):
        now = datetime(2026, 1, 1, 10, 0)
        assert day_label(datetime(2025, 12, 31, 23, 0), now) == "Yesterday"

    def test_default_now_does_not_raise(self):
        # now=None must fall back to datetime.now() internally without error.
        result = day_label(datetime(2020, 1, 1))
        assert isinstance(result, str)
