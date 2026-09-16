"""Queue 122: dictation history has a way out.

Everything the user has ever said lives in ~/.samsara/history.db, and until
now there was no way to read it anywhere else -- history_store.py's own
docstring said "there is currently no history export/import feature at all".

These tests use a REAL HistoryManager against a tmp_path database, so the
columns, the ordering and the UTF-8 behaviour are the store's own, not a
fixture's idea of them. The UI tests build a real HistoryView against that
store; Qt's own save dialog is never opened (it is modal and would hang),
and what is asserted instead is that the control reaching it is focusable,
44 px and in the tab order.

Two things they exist to keep honest:

  * the JSON must round-trip, because a lossy export cannot be re-imported
    and an importer is the next prompt, not this one;
  * nothing may be written until a path is chosen -- an export is every word
    the user has ever dictated.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import history_export as hx
from samsara.history import HistoryManager
from samsara.history_store import HistoryStore

#: Non-ASCII on purpose: history_store.py carries a cp1252 audit note, and
#: this module is the first file in the history path that opens a file.
ACCENTED = "voilà, naïve café — 90% déjà vu"
CYRILLIC = "привет мир"
EMOJI = "shipped it \U0001F680"


@pytest.fixture
def store(tmp_path):
    mgr = HistoryManager(db_path=tmp_path / "history.db")
    return HistoryStore(mgr), mgr


def add(mgr, text, *, entry_type="dictation", when=None, **kw):
    """Insert a row, optionally forcing its timestamp (HistoryManager.add
    always stamps 'now', and a date-range test needs older rows)."""
    row_id = mgr.add(raw_text=text, display_text=text, entry_type=entry_type, **kw)
    if when:
        with mgr._lock:
            mgr._conn.execute("UPDATE history SET timestamp = ? WHERE id = ?",
                              (when, row_id))
            mgr._conn.commit()
    return row_id


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------

class TestJsonExport:
    def test_every_entry_is_exported_and_reloads(self, store, tmp_path):
        s, mgr = store
        for i in range(5):
            add(mgr, f"entry {i}")
        rows = hx.collect_all(s)
        out = hx.export(rows, tmp_path / "h.json")

        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["schema"] == "samsara.history"
        assert doc["schema_version"] == hx.SCHEMA_VERSION
        assert doc["count"] == 5 == len(doc["entries"])
        assert {e["display_text"] for e in doc["entries"]} == {f"entry {i}" for i in range(5)}

    def test_every_column_is_present_on_every_entry(self, store, tmp_path):
        """A lossy export cannot be re-imported. Nulls are written as nulls
        so an importer can tell a missing field from an empty one."""
        s, mgr = store
        add(mgr, "one", app_context="notepad.exe", duration_ms=1234, mode="toggle",
            status="success", log_prob=-0.25, matched_command="scroll down")
        doc = json.loads(hx.export(hx.collect_all(s), tmp_path / "h.json")
                         .read_text(encoding="utf-8"))
        entry = doc["entries"][0]
        assert set(entry) == set(hx.COLUMNS)
        assert doc["columns"] == list(hx.COLUMNS)
        assert entry["app_context"] == "notepad.exe"
        assert entry["duration_ms"] == 1234
        assert entry["matched_command"] == "scroll down"
        assert entry["audio_path"] is None          # a null, not a missing key

    def test_the_row_id_survives_so_an_importer_has_a_key(self, store, tmp_path):
        s, mgr = store
        ids = [add(mgr, f"e{i}") for i in range(3)]
        doc = json.loads(hx.export(hx.collect_all(s), tmp_path / "h.json")
                         .read_text(encoding="utf-8"))
        assert sorted(e["id"] for e in doc["entries"]) == sorted(ids)

    def test_the_scope_is_recorded_so_a_partial_export_says_so(self, store, tmp_path):
        s, mgr = store
        add(mgr, "one")
        doc = json.loads(hx.export(hx.collect_all(s), tmp_path / "h.json",
                                   scope='last 7 days, search: "cat"')
                         .read_text(encoding="utf-8"))
        assert doc["scope"] == 'last 7 days, search: "cat"'

    def test_an_empty_history_is_a_valid_document_not_an_error(self, store, tmp_path):
        s, _mgr = store
        out = hx.export(hx.collect_all(s), tmp_path / "empty.json")
        doc = json.loads(out.read_text(encoding="utf-8"))
        assert doc["count"] == 0
        assert doc["entries"] == []
        assert doc["schema_version"] == hx.SCHEMA_VERSION
        assert doc["columns"] == list(hx.COLUMNS)


# ---------------------------------------------------------------------------
# Readable text
# ---------------------------------------------------------------------------

class TestTextExport:
    def test_it_is_chronological_oldest_first(self, store, tmp_path):
        s, mgr = store
        add(mgr, "the first thing", when="2026-01-01T09:00:00")
        add(mgr, "the last thing", when="2026-03-05T17:30:00")
        add(mgr, "the middle thing", when="2026-02-02T12:00:00")
        text = hx.export(hx.collect_all(s), tmp_path / "h.txt").read_text(encoding="utf-8")
        assert (text.index("the first thing") < text.index("the middle thing")
                < text.index("the last thing"))

    def test_it_carries_dates_and_times(self, store, tmp_path):
        s, mgr = store
        add(mgr, "hello there", when="2026-02-02T12:34:56")
        text = hx.export(hx.collect_all(s), tmp_path / "h.txt").read_text(encoding="utf-8")
        assert "2026-02-02" in text
        assert "12:34:56" in text
        assert "hello there" in text

    def test_a_command_entry_is_marked_so_it_is_not_read_as_dictation(self, store, tmp_path):
        s, mgr = store
        add(mgr, "scroll down", entry_type="command")
        add(mgr, "some dictated words")
        text = hx.export(hx.collect_all(s), tmp_path / "h.txt").read_text(encoding="utf-8")
        assert "[command]" in text
        assert "[dictation]" not in text, "the ordinary case needs no tag"

    def test_markdown_quotes_the_words_so_they_cannot_become_markup(self, store, tmp_path):
        s, mgr = store
        add(mgr, "# not a heading and - not a list")
        text = hx.export(hx.collect_all(s), tmp_path / "h.md").read_text(encoding="utf-8")
        assert "> # not a heading and - not a list" in text

    def test_an_empty_history_says_so(self, store, tmp_path):
        s, _mgr = store
        text = hx.export(hx.collect_all(s), tmp_path / "h.txt").read_text(encoding="utf-8")
        assert "No entries." in text
        assert "Samsara dictation history" in text

    def test_the_format_follows_the_chosen_extension(self, store, tmp_path):
        s, mgr = store
        add(mgr, "words")
        assert hx.format_for_path("x.json") == "json"
        assert hx.format_for_path("x.md") == "markdown"
        assert hx.format_for_path("x.txt") == "text"
        assert hx.format_for_path("x") == "text"
        md = hx.export(hx.collect_all(s), tmp_path / "a.md").read_text(encoding="utf-8")
        assert md.startswith("# Samsara dictation history")


# ---------------------------------------------------------------------------
# Date range
# ---------------------------------------------------------------------------

class TestDateRange:
    @pytest.fixture
    def spread(self, store):
        s, mgr = store
        for day in ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"):
            add(mgr, f"on {day}", when=f"{day}T12:00:00")
        return s, mgr

    def test_both_boundaries_are_included(self, spread):
        s, _mgr = spread
        rows = hx.in_date_range(hx.collect_all(s), "2026-01-02", "2026-01-04")
        assert sorted(r["display_text"] for r in rows) == [
            "on 2026-01-02", "on 2026-01-03", "on 2026-01-04"]

    def test_the_whole_of_the_last_day_counts(self, store):
        """A user asking for the 3rd to the 5th means all of the 5th, not up
        to midnight at its start."""
        s, mgr = store
        add(mgr, "late on the fifth", when="2026-01-05T23:59:58")
        rows = hx.in_date_range(hx.collect_all(s), "2026-01-03", "2026-01-05")
        assert [r["display_text"] for r in rows] == ["late on the fifth"]

    def test_an_open_ended_range_works_in_both_directions(self, spread):
        s, _mgr = spread
        assert len(hx.in_date_range(hx.collect_all(s), "2026-01-04", None)) == 2
        assert len(hx.in_date_range(hx.collect_all(s), None, "2026-01-02")) == 2

    def test_no_range_is_everything(self, spread):
        s, _mgr = spread
        assert len(hx.in_date_range(hx.collect_all(s), None, None)) == 5

    def test_the_range_export_contains_only_those_entries(self, spread, tmp_path):
        s, _mgr = spread
        rows = hx.in_date_range(hx.collect_all(s), "2026-01-02", "2026-01-03")
        doc = json.loads(hx.export(rows, tmp_path / "r.json", scope="2026-01-02 to 2026-01-03")
                         .read_text(encoding="utf-8"))
        assert doc["count"] == 2
        assert all("2026-01-0" in e["timestamp"] for e in doc["entries"])
        assert {e["display_text"] for e in doc["entries"]} == {"on 2026-01-02", "on 2026-01-03"}


# ---------------------------------------------------------------------------
# UTF-8
# ---------------------------------------------------------------------------

class TestNonAsciiSurvives:
    @pytest.mark.parametrize("text", [ACCENTED, CYRILLIC, EMOJI])
    @pytest.mark.parametrize("name", ["h.json", "h.txt", "h.md"])
    def test_it_round_trips_through_both_formats(self, store, tmp_path, text, name):
        s, mgr = store
        add(mgr, text)
        out = hx.export(hx.collect_all(s), tmp_path / name)
        assert text in out.read_text(encoding="utf-8")

    def test_json_stores_the_characters_not_escapes(self, store, tmp_path):
        """ensure_ascii=False. A file full of \\u00e0 is technically valid
        and useless to the person who dictated it."""
        s, mgr = store
        add(mgr, ACCENTED)
        raw = (hx.export(hx.collect_all(s), tmp_path / "h.json")).read_bytes()
        assert ACCENTED.encode("utf-8") in raw

    def test_the_file_is_utf8_whatever_the_platform_default_is(self, store, tmp_path):
        """The cp1252 audit that history_store.py documents. Decoding as
        cp1252 must FAIL or differ -- that is what proves the bytes are
        UTF-8 rather than the platform's idea of text."""
        s, mgr = store
        add(mgr, CYRILLIC)
        raw = (hx.export(hx.collect_all(s), tmp_path / "h.txt")).read_bytes()
        assert CYRILLIC.encode("utf-8") in raw
        try:
            assert CYRILLIC not in raw.decode("cp1252")
        except UnicodeDecodeError:
            pass          # even better: cp1252 cannot read it at all


# ---------------------------------------------------------------------------
# Nothing is written until the user says so
# ---------------------------------------------------------------------------

class TestNothingIsWrittenUnasked:
    def test_collecting_rows_writes_no_file(self, store, tmp_path, monkeypatch):
        s, mgr = store
        add(mgr, "private words")
        monkeypatch.setattr(hx, "samsara_home_dir", lambda: tmp_path / "home")
        before = set(tmp_path.rglob("*"))
        rows = hx.collect_all(s)
        hx.build_document(rows)
        hx.render_text(rows)
        assert set(tmp_path.rglob("*")) == before, "reading history must touch no file"

    def test_the_default_directory_is_the_apps_own_not_documents(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hx, "samsara_home_dir", lambda: tmp_path / ".samsara")
        d = hx.default_export_dir()
        assert d == tmp_path / ".samsara" / "exports"
        assert not d.exists(), "naming the default must not create it"
        lowered = str(d).lower()
        for synced in ("onedrive", "dropbox", "google drive", "icloud"):
            assert synced not in lowered

    def test_the_default_filename_is_dated_and_typed(self):
        import datetime
        when = datetime.datetime(2026, 3, 4, 17, 5)
        assert hx.default_filename("json", when) == "samsara-history_2026-03-04_1705.json"
        assert hx.default_filename("text", when).endswith(".txt")
        assert hx.default_filename("markdown", when).endswith(".md")

    def test_an_unknown_format_is_refused_rather_than_guessed(self, tmp_path):
        with pytest.raises(ValueError):
            hx.export([], tmp_path / "x.csv", "csv")


# ---------------------------------------------------------------------------
# The view: the export control, and the rows it would write
# ---------------------------------------------------------------------------

@pytest.fixture
def view(qapp, store, monkeypatch, tmp_path):
    from PySide6.QtCore import QSettings
    from samsara.ui import history_view as hv

    class _FakeSettings:
        values = {}

        def __init__(self, *a):
            pass

        def value(self, key, default=None, type=None):
            return self.values.get(key, default)

        def setValue(self, key, value):
            self.values[key] = value

    monkeypatch.setattr(hv, "QSettings", _FakeSettings)
    # The view loads on a background thread; the tests below call the query
    # helpers directly, so nothing has to be waited on.
    s, mgr = store
    v = hv.HistoryView(s)
    v.resize(900, 700)
    v.show()
    qapp.processEvents()
    yield v, s, mgr
    v.close()


class TestTheExportControl:
    def test_it_exists_and_says_what_it_is(self, view):
        v, _s, _mgr = view
        from samsara.ui import history_view as hv
        assert v._export_btn.text() == hv.EXPORT_LABEL
        assert v._export_btn.accessibleName() == hv.EXPORT_LABEL

    def test_it_is_a_44px_target(self, view, qapp):
        v, _s, _mgr = view
        qapp.processEvents()
        assert v._export_btn.minimumHeight() >= 44
        assert v._export_btn.height() >= 44
        assert v._export_btn.width() >= 44

    def test_it_is_keyboard_reachable(self, view, qapp):
        from PySide6.QtCore import Qt
        v, _s, _mgr = view
        assert v._export_btn.focusPolicy() == Qt.FocusPolicy.StrongFocus
        v._export_btn.setFocus()
        qapp.processEvents()
        assert v._export_btn.hasFocus()

    def test_it_is_in_the_tab_order_before_the_destructive_button(self, view, qapp):
        """Tab from Export must land on Clear, not the other way round: the
        control you reach by accident should not be the one that deletes."""
        from PySide6.QtWidgets import QWidget
        v, _s, _mgr = view
        v._export_btn.setFocus()
        qapp.processEvents()
        nxt = v._export_btn.nextInFocusChain()
        seen, guard = [], 0
        while guard < 40 and isinstance(nxt, QWidget):
            guard += 1
            if nxt is v._clear_btn:
                break
            if nxt is v._export_btn:
                break
            seen.append(nxt)
            nxt = nxt.nextInFocusChain()
        assert nxt is v._clear_btn, f"Clear did not follow Export; saw {seen[:4]}"


class TestExportingWhatTheViewShows:
    def test_it_matches_the_rows_the_view_fetched(self, view):
        v, _s, mgr = view
        for i in range(4):
            add(mgr, f"visible {i}")
        shown = v._fetch_rows(*v._current_query()[:2], None, *v._current_query()[2:])
        exported = v.export_rows()
        assert [r["id"] for r in exported] == [r["id"] for r in shown]

    def test_the_search_box_narrows_the_export(self, view, qapp):
        v, _s, mgr = view
        add(mgr, "the quick brown fox")
        add(mgr, "something else entirely")
        v._search.setText("brown")
        qapp.processEvents()
        rows = v.export_rows()
        assert [r["display_text"] for r in rows] == ["the quick brown fox"]

    def test_scope_all_ignores_the_toolbar(self, view, qapp):
        v, s, mgr = view
        add(mgr, "matches the search")
        add(mgr, "does not")
        v._search.setText("matches")
        qapp.processEvents()
        assert len(v.export_rows()) == 1
        assert len(v.export_rows(scope_all=True)) == 2

    def test_the_scope_label_describes_what_was_exported(self, view, qapp):
        v, _s, _mgr = view
        v._search.setText("cat")
        qapp.processEvents()
        label = v._export_scope_label(False)
        assert 'search: "cat"' in label
        assert v._export_scope_label(True) == "all history"

    def test_an_empty_store_exports_nothing_without_raising(self, view, tmp_path):
        v, _s, _mgr = view
        rows = v.export_rows()
        assert rows == []
        doc = json.loads(hx.export(rows, tmp_path / "e.json").read_text(encoding="utf-8"))
        assert doc["count"] == 0
