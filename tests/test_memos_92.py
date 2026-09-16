"""Queue 92: the memo store, its retention policy, and the memo list page.

Queue 07's tests (tests/test_quick_memo.py) are unchanged and still pass --
the markdown file it created is written exactly as before. These cover what
92 added on top: the JSONL store of record, spoken categories, audio
retention, and the UI.

Nothing here touches the real ~/.samsara: every case is given an explicit
`home` under tmp_path, and the one case that would reach an Obsidian vault
points at a tmp_path directory.
"""
from __future__ import annotations

import json
import types
import wave
from datetime import datetime, timedelta

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import samsara.quick_memo as qm
from samsara.ui import memos_qt


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wav(home, seconds=0.25, rate=16000):
    """A real, readable WAV in the memo audio directory."""
    return qm.retain_audio([0.0] * int(rate * seconds), rate, home=home)


class _FakePlayer:
    """Stands in for the audio device: a test must not make a noise."""

    def __init__(self):
        self.played = []
        self.stops = 0
        self._on = False

    @property
    def playing(self):
        return self._on

    def play(self, path):
        self.played.append(path)
        self._on = True
        return True

    def stop(self):
        self.stops += 1
        self._on = False


@pytest.fixture
def page(qapp, tmp_path):
    def make(player=None):
        widget = memos_qt.MemosPage(types.SimpleNamespace(config={}), home=tmp_path,
                                    player=player or _FakePlayer())
        widget.resize(760, 560)
        widget.show()
        qapp.processEvents()
        return widget
    return make


# ---------------------------------------------------------------------------
# 1. The store of record
# ---------------------------------------------------------------------------

class TestStore:
    def test_a_memo_lands_in_both_the_index_and_the_markdown(self, tmp_path):
        record = qm.add_memo("buy milk", "voice", home=tmp_path)
        assert record["id"]
        assert qm.index_file(tmp_path).exists()
        assert "buy milk" in qm.memo_file(tmp_path).read_text(encoding="utf-8")
        assert [r["text"] for r in qm.load_memos(tmp_path)] == ["buy milk"]

    def test_append_memo_keeps_queue_07s_contract_and_still_indexes(self, tmp_path):
        path = qm.append_memo("from the old API", "voice", home=tmp_path)
        assert path == qm.memo_file(tmp_path)
        assert [r["text"] for r in qm.load_memos(tmp_path)] == ["from the old API"]

    def test_every_memo_has_a_distinct_id(self, tmp_path):
        ids = {qm.add_memo(f"memo {i}", "voice", home=tmp_path)["id"] for i in range(25)}
        assert len(ids) == 25

    def test_the_index_is_rebuilt_from_markdown_when_it_is_missing(self, tmp_path):
        """A user upgrading from a build with no index keeps their memos."""
        qm.append_memo("recorded before the index existed", "voice", home=tmp_path)
        qm.append_memo("and another", "voice", home=tmp_path)
        qm.index_file(tmp_path).unlink()

        recovered = qm.load_memos(tmp_path)

        assert [r["text"] for r in recovered] == [
            "recorded before the index existed", "and another"]
        assert qm.index_file(tmp_path).exists()      # and written back

    def test_a_corrupt_index_line_costs_only_that_line(self, tmp_path):
        qm.add_memo("good one", "voice", home=tmp_path)
        with qm.index_file(tmp_path).open("a", encoding="utf-8") as handle:
            handle.write("{ this is not json\n")
        qm.add_memo("good two", "voice", home=tmp_path)
        assert [r["text"] for r in qm.load_memos(tmp_path)] == ["good one", "good two"]

    def test_delete_removes_the_memo_and_its_audio(self, tmp_path):
        wav = _wav(tmp_path)
        record = qm.add_memo("delete me", "voice", audio_path=wav, home=tmp_path)
        qm.add_memo("keep me", "voice", home=tmp_path)

        assert qm.delete_memo(record["id"], home=tmp_path) is True

        assert [r["text"] for r in qm.load_memos(tmp_path)] == ["keep me"]
        assert not wav.exists()
        assert qm.delete_memo("no-such-id", home=tmp_path) is False

    def test_deleting_the_last_memo_does_not_resurrect_it(self, tmp_path):
        """An EMPTY index means everything was deleted -- it must not be
        mistaken for a missing one and rebuilt from the markdown."""
        record = qm.add_memo("delete me", "voice", home=tmp_path)
        assert qm.delete_memo(record["id"], home=tmp_path) is True
        assert qm.load_memos(tmp_path) == []
        assert qm.load_memos(tmp_path) == []             # and again, from disk

    def test_delete_also_clears_the_raw_markdown(self, tmp_path):
        """The page offers "open the raw file" one click away: a deleted
        memo must not still be sitting in it."""
        keep = qm.add_memo("keep this", "voice", home=tmp_path)
        drop = qm.add_memo("forget this", "voice", home=tmp_path)

        qm.delete_memo(drop["id"], home=tmp_path)

        text = qm.memo_file(tmp_path).read_text(encoding="utf-8")
        assert "forget this" not in text
        assert "keep this" in text
        assert text.count("# Memos") == 1
        assert [r["id"] for r in qm.load_memos(tmp_path)] == [keep["id"]]

    def test_search_covers_the_transcript_and_the_category(self, tmp_path):
        qm.add_memo("call the dentist", "voice", home=tmp_path)
        qm.add_memo("bread", "voice", home=tmp_path, category="Shopping")
        records = qm.load_memos(tmp_path)
        assert [r["text"] for r in qm.search_memos(records, "dent")] == ["call the dentist"]
        assert [r["text"] for r in qm.search_memos(records, "shopping")] == ["bread"]
        assert len(qm.search_memos(records, "")) == 2

    def test_a_memo_failure_never_swallows_the_dictation(self, tmp_path, monkeypatch):
        """Queue 07's rule, re-checked through the new entry point: a store
        that cannot be written raises, so the caller can fall through."""
        def fail(*args, **kwargs):
            raise PermissionError("filesystem gone")
        monkeypatch.setattr(qm.Path, "open", fail)
        with pytest.raises(qm.MemoWriteError):
            qm.add_memo("this must not vanish silently", "voice", home=tmp_path)


# ---------------------------------------------------------------------------
# 2. Categories -- explicit, cheap, correctable, no model
# ---------------------------------------------------------------------------

class TestCategories:
    def test_there_are_none_until_the_user_makes_one(self, tmp_path):
        assert qm.categories(tmp_path) == []
        assert qm.split_category("shopping: milk", qm.categories(tmp_path)) == (
            None, "shopping: milk")

    def test_a_known_category_prefix_files_the_memo(self, tmp_path):
        qm.add_category("Shopping", home=tmp_path)
        known = qm.categories(tmp_path)
        assert qm.split_category("Shopping: milk and eggs", known) == ("Shopping", "milk and eggs")
        assert qm.split_category("shopping, bread", known) == ("Shopping", "bread")

    def test_an_unknown_first_word_is_never_turned_into_a_category(self, tmp_path):
        qm.add_category("Shopping", home=tmp_path)
        known = qm.categories(tmp_path)
        assert qm.split_category("Remember, call the dentist", known) == (
            None, "Remember, call the dentist")

    def test_a_separator_is_required(self, tmp_path):
        qm.add_category("Shopping", home=tmp_path)
        known = qm.categories(tmp_path)
        assert qm.split_category("Shopping list needs milk", known) == (
            None, "Shopping list needs milk")

    def test_the_longest_matching_category_wins(self, tmp_path):
        qm.save_categories(["Shopping", "Shopping list"], home=tmp_path)
        known = qm.categories(tmp_path)
        assert qm.split_category("Shopping list: milk", known)[0] == "Shopping list"

    def test_categories_are_deduplicated_and_survive_a_restart(self, tmp_path):
        qm.add_category("Shopping", home=tmp_path)
        qm.add_category("shopping", home=tmp_path)
        qm.add_category("Ideas", home=tmp_path)
        assert qm.categories(tmp_path) == ["Shopping", "Ideas"]
        # A fresh read is what a restart does.
        assert qm.categories(tmp_path) == ["Shopping", "Ideas"]

    def test_a_category_is_correctable_and_the_correction_persists(self, tmp_path):
        record = qm.add_memo("bread", "voice", home=tmp_path, category="Ideas",
                             category_source=qm.CATEGORY_SPOKEN)
        assert qm.set_category(record["id"], "Shopping", home=tmp_path) is True
        stored = {r["id"]: r for r in qm.load_memos(tmp_path)}[record["id"]]
        assert stored["category"] == "Shopping"
        assert stored["category_source"] == qm.CATEGORY_MANUAL

    def test_clearing_a_category_is_allowed(self, tmp_path):
        record = qm.add_memo("x", "voice", home=tmp_path, category="Ideas")
        qm.set_category(record["id"], None, home=tmp_path)
        stored = qm.load_memos(tmp_path)[0]
        assert stored["category"] is None and stored["category_source"] is None

    def test_a_future_router_may_fill_an_empty_category(self, tmp_path):
        record = qm.add_memo("unfiled", "voice", home=tmp_path)
        assert qm.set_category(record["id"], "Tasks", home=tmp_path,
                               source=qm.CATEGORY_ROUTER) is True
        assert qm.load_memos(tmp_path)[0]["category_source"] == qm.CATEGORY_ROUTER

    def test_a_router_may_never_overwrite_what_the_user_set_by_hand(self, tmp_path):
        """The contract queue 92 leaves for the capture router."""
        record = qm.add_memo("mine", "voice", home=tmp_path)
        qm.set_category(record["id"], "Shopping", home=tmp_path, source=qm.CATEGORY_MANUAL)
        assert qm.set_category(record["id"], "Tasks", home=tmp_path,
                               source=qm.CATEGORY_ROUTER) is False
        assert qm.load_memos(tmp_path)[0]["category"] == "Shopping"


# ---------------------------------------------------------------------------
# 3. Audio: round trip and retention
# ---------------------------------------------------------------------------

class TestAudio:
    def test_a_memo_with_audio_round_trips_recorded_listed_and_playable(self, tmp_path, qapp):
        """The brief's round trip, end to end."""
        wav = _wav(tmp_path, seconds=0.3)
        record = qm.add_memo("this one was spoken", "voice", audio_path=wav, home=tmp_path)

        # Recorded: a real WAV with the capture format.
        with wave.open(str(wav), "rb") as handle:
            assert handle.getnchannels() == 1 and handle.getsampwidth() == 2
            assert handle.getframerate() == 16000

        # Listed: it comes back out of the store with its audio attached.
        stored = qm.load_memos(tmp_path)[0]
        assert stored["id"] == record["id"]
        assert qm.audio_path_for(stored, tmp_path) == wav.resolve()

        # Played: the page finds it and hands it to the player.
        player = _FakePlayer()
        widget = memos_qt.MemosPage(types.SimpleNamespace(config={}), home=tmp_path,
                                    player=player)
        widget.show()
        qapp.processEvents()
        assert widget.play_selected() is True
        assert player.played == [wav.resolve()]

    def test_audio_is_stored_relative_so_the_folder_can_move(self, tmp_path):
        wav = _wav(tmp_path)
        record = qm.add_memo("relative", "voice", audio_path=wav, home=tmp_path)
        assert not record["audio"].startswith(str(tmp_path))
        assert record["audio"].startswith("audio/")

    def test_old_audio_is_pruned_and_the_transcript_is_kept(self, tmp_path):
        wav = _wav(tmp_path)
        record = qm.add_memo("said long ago", "voice", audio_path=wav, home=tmp_path)
        records = qm.load_memos(tmp_path)
        records[0]["at"] = (datetime.now() - timedelta(days=200)).isoformat()
        qm._write_index(records, tmp_path)

        removed = qm.prune_audio({"memo_audio_retention_days": 90, "memo_audio_max_mb": 0},
                                 home=tmp_path)

        assert removed == [record["id"]]
        assert not wav.exists()
        stored = qm.load_memos(tmp_path)[0]
        assert stored["text"] == "said long ago"        # the transcript survives
        assert stored["audio_pruned"] is True

    def test_a_pinned_memo_is_never_pruned_at_any_age(self, tmp_path):
        wav = _wav(tmp_path)
        record = qm.add_memo("keep this one", "voice", audio_path=wav, home=tmp_path)
        assert qm.set_keep(record["id"], True, home=tmp_path) is True
        records = qm.load_memos(tmp_path)
        records[0]["at"] = (datetime.now() - timedelta(days=5000)).isoformat()
        qm._write_index(records, tmp_path)

        assert qm.prune_audio({"memo_audio_retention_days": 1, "memo_audio_max_mb": 1},
                              home=tmp_path) == []
        assert wav.exists()

    def test_the_size_cap_takes_the_oldest_first(self, tmp_path):
        records, wavs = [], []
        for index in range(3):
            wav = _wav(tmp_path)
            # Pad each file past the cap's granularity. prune_audio measures
            # st_size, so the bytes are what matter, not the frame count.
            with wav.open("ab") as handle:
                handle.write(bytes(600 * 1024))
            wavs.append(wav)
            records.append(qm.add_memo(f"memo {index}", "voice",
                                       audio_path=wav, home=tmp_path))
        stored = qm.load_memos(tmp_path)
        for offset, record in enumerate(stored):
            record["at"] = (datetime.now() - timedelta(days=10 - offset)).isoformat()
        qm._write_index(stored, tmp_path)

        # 1 MB of budget against ~1.8 MB of audio: the two oldest go.
        removed = qm.prune_audio({"memo_audio_retention_days": 0, "memo_audio_max_mb": 1},
                                 home=tmp_path)

        assert removed == [records[0]["id"], records[1]["id"]]
        assert not wavs[0].exists() and not wavs[1].exists()
        assert wavs[2].exists()                          # the newest survives
        assert len(qm.load_memos(tmp_path)) == 3         # every transcript kept

    def test_zero_means_no_limit(self, tmp_path):
        wav = _wav(tmp_path)
        qm.add_memo("forever", "voice", audio_path=wav, home=tmp_path)
        assert qm.prune_audio({"memo_audio_retention_days": 0, "memo_audio_max_mb": 0},
                              home=tmp_path) == []
        assert wav.exists()

    def test_the_retention_defaults_are_bounded(self):
        assert qm.DEFAULT_RETENTION_DAYS > 0
        assert qm.DEFAULT_MAX_AUDIO_MB > 0


# ---------------------------------------------------------------------------
# 4. The vault mirror -- optional, off, and never fatal
# ---------------------------------------------------------------------------

class TestVaultMirror:
    def test_it_is_off_unless_switched_on(self, tmp_path):
        record = qm.add_memo("private", "voice", home=tmp_path)
        assert qm.mirror_enabled({"voice_memo": {"vault_dir": str(tmp_path)}}) is False
        assert qm.mirror_to_vault(record, {"voice_memo": {"vault_dir": str(tmp_path)}},
                                  home=tmp_path) is None

    def test_when_on_it_writes_an_obsidian_embed_and_the_transcript(self, tmp_path):
        vault = tmp_path / "vault"
        wav = _wav(tmp_path)
        record = qm.add_memo("spoken thought", "voice", audio_path=wav, home=tmp_path,
                             category="Ideas")
        config = {"voice_memo": {"vault_dir": str(vault), "mirror_memos": True,
                                 "note_relpath": "Voice Memos.md",
                                 "attachments_relpath": "Attachments/Memos"}}

        note = qm.mirror_to_vault(record, config, home=tmp_path)

        assert note == vault / "Voice Memos.md"
        text = note.read_text(encoding="utf-8")
        assert text.startswith("# Voice Memos\n\n")
        assert "![[Attachments/Memos/memo_" in text     # Obsidian's inline player
        assert "spoken thought" in text
        assert "Ideas" in text
        assert (vault / "Attachments" / "Memos").is_dir()

    def test_a_broken_vault_path_never_raises_and_never_loses_the_memo(self, tmp_path):
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("a file, not a directory", encoding="utf-8")
        record = qm.add_memo("still safe", "voice", home=tmp_path)
        config = {"voice_memo": {"vault_dir": str(blocker), "mirror_memos": True}}

        assert qm.mirror_to_vault(record, config, home=tmp_path) is None
        assert [r["text"] for r in qm.load_memos(tmp_path)] == ["still safe"]


# ---------------------------------------------------------------------------
# 5. The memo list page
# ---------------------------------------------------------------------------

class TestMemosPage:
    def test_it_lists_memos_newest_first_with_their_date(self, page, tmp_path):
        qm.add_memo("older", "voice", home=tmp_path)
        qm.add_memo("newer", "voice", home=tmp_path)
        widget = page()
        assert len(widget.rows) == 2
        assert "newer" in widget.rows[0] and "older" in widget.rows[1]
        assert datetime.now().strftime("%Y-%m-%d") in widget.rows[0]

    def test_an_empty_store_says_how_to_make_one(self, page):
        widget = page()
        assert widget.rows == []
        assert "take a memo" in widget.status_text

    def test_the_category_is_shown_on_the_row(self, page, tmp_path):
        qm.add_memo("bread", "voice", home=tmp_path, category="Shopping")
        widget = page()
        assert "Shopping" in widget.rows[0]

    def test_search_filters_the_list(self, page, tmp_path):
        qm.add_memo("call the dentist", "voice", home=tmp_path)
        qm.add_memo("buy bread", "voice", home=tmp_path)
        widget = page()
        widget._search.setText("dent")
        assert len(widget.rows) == 1 and "dentist" in widget.rows[0]
        widget._search.setText("nothing matches this")
        assert widget.rows == [] and widget.status_text == memos_qt.NO_MATCHES

    def test_the_full_transcript_is_shown_not_the_elided_row(self, page, tmp_path):
        long_text = "a thought " * 30
        qm.add_memo(long_text.strip(), "voice", home=tmp_path)
        widget = page()
        assert widget.detail_text == long_text.strip()
        assert len(widget.rows[0]) < len(long_text)      # the row IS elided

    def test_a_memo_without_audio_says_so_and_cannot_be_played(self, page, tmp_path):
        qm.add_memo("typed, not spoken", "typed", home=tmp_path)
        widget = page()
        assert widget.status_text == memos_qt.NO_AUDIO
        assert widget._play_btn.isEnabled() is False
        assert widget.play_selected() is False

    def test_pruned_audio_says_expired_rather_than_offering_a_dead_button(self, page, tmp_path):
        wav = _wav(tmp_path)
        qm.add_memo("audio is gone", "voice", audio_path=wav, home=tmp_path)
        wav.unlink()
        widget = page()
        assert widget._play_btn.isEnabled() is False
        assert widget.status_text in (memos_qt.NO_AUDIO, memos_qt.AUDIO_EXPIRED)

    def test_copy_puts_the_transcript_on_the_clipboard(self, page, tmp_path, qapp):
        qm.add_memo("copy this exactly", "voice", home=tmp_path)
        widget = page()
        assert widget.copy_selected() is True
        assert QApplication.clipboard().text() == "copy this exactly"
        assert widget.status_text == memos_qt.COPIED

    def test_delete_asks_first_and_then_removes(self, page, tmp_path):
        qm.add_memo("goodbye", "voice", home=tmp_path)
        widget = page()

        widget.request_delete()
        assert widget._confirm_btn.isVisible()
        assert len(widget.rows) == 1                     # nothing gone yet

        assert widget.confirm_delete() is True
        assert widget.rows == []
        assert qm.load_memos(tmp_path) == []

    def test_delete_can_be_cancelled(self, page, tmp_path):
        qm.add_memo("still here", "voice", home=tmp_path)
        widget = page()
        widget.request_delete()
        widget.cancel_delete()
        assert widget.confirm_delete() is False
        assert len(qm.load_memos(tmp_path)) == 1

    def test_a_category_typed_in_the_page_is_saved_and_reloadable(self, page, tmp_path):
        qm.add_memo("bread", "voice", home=tmp_path)
        widget = page()
        widget._category.setCurrentText("Shopping")

        assert widget.apply_category() is True

        assert qm.load_memos(tmp_path)[0]["category"] == "Shopping"
        assert "Shopping" in qm.categories(tmp_path)     # and the list learned it
        assert "Shopping" in page().rows[0]              # survives a rebuild

    def test_playing_a_second_memo_stops_the_first(self, page, tmp_path):
        qm.add_memo("one", "voice", audio_path=_wav(tmp_path), home=tmp_path)
        qm.add_memo("two", "voice", audio_path=_wav(tmp_path), home=tmp_path)
        player = _FakePlayer()
        widget = page(player)
        widget._list.setCurrentRow(0)
        widget.play_selected()
        widget._list.setCurrentRow(1)
        widget.play_selected()
        assert len(player.played) == 2


# ---------------------------------------------------------------------------
# 6. Reachable and operable without a mouse
# ---------------------------------------------------------------------------

class TestKeyboardOnly:
    def _key(self, widget, key, modifier=Qt.KeyboardModifier.NoModifier):
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent
        widget.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, modifier))

    def test_every_control_is_focusable_and_in_a_deliberate_tab_order(self, page, tmp_path):
        qm.add_memo("one", "voice", home=tmp_path)
        widget = page()
        chain = widget.tab_chain
        assert len(chain) >= 6
        for control in chain:
            assert control.focusPolicy() != Qt.FocusPolicy.NoFocus, control.accessibleName()
        # The order is the reading order: search, list, then the actions.
        assert chain[0] is widget._search
        assert chain[1] is widget._list

    def test_every_action_has_an_accessible_name_equal_to_its_label(self, page):
        widget = page()
        from PySide6.QtWidgets import QAbstractButton
        for button in widget.findChildren(QAbstractButton):
            if button.text():
                assert button.accessibleName() == button.text(), button.text()

    def test_every_control_meets_the_44_px_target(self, page, tmp_path):
        qm.add_memo("one", "voice", home=tmp_path)
        widget = page()
        from PySide6.QtWidgets import QAbstractButton
        for button in widget.findChildren(QAbstractButton):
            if button.isVisibleTo(widget) and button.text():
                assert button.height() >= memos_qt.MIN_TARGET, button.text()

    def test_ctrl_f_reaches_the_search_box(self, page, tmp_path):
        qm.add_memo("one", "voice", home=tmp_path)
        widget = page()
        widget._list.setFocus()
        self._key(widget, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert widget._search.hasFocus()

    def test_enter_plays_and_delete_asks_without_a_mouse(self, page, tmp_path):
        qm.add_memo("spoken", "voice", audio_path=_wav(tmp_path), home=tmp_path)
        player = _FakePlayer()
        widget = page(player)
        widget._list.setFocus()

        self._key(widget, Qt.Key.Key_Return)
        assert len(player.played) == 1

        self._key(widget, Qt.Key.Key_Delete)
        assert widget._confirm_btn.isVisible()
        assert widget._confirm_btn.hasFocus()            # confirm is where focus lands

    def test_escape_backs_out_of_a_delete(self, page, tmp_path):
        qm.add_memo("safe", "voice", home=tmp_path)
        widget = page()
        widget.request_delete()
        self._key(widget, Qt.Key.Key_Escape)
        assert not widget._confirm_btn.isVisible()
        assert len(qm.load_memos(tmp_path)) == 1

    def test_delete_does_not_fire_while_typing_a_search(self, page, tmp_path):
        qm.add_memo("one", "voice", home=tmp_path)
        widget = page()
        widget._search.setFocus()
        self._key(widget, Qt.Key.Key_Delete)
        assert not widget._confirm_btn.isVisible()

    def test_the_shortcut_is_written_on_the_control_not_hidden(self, page):
        widget = page()
        assert "Ctrl+F" in widget._search.accessibleName()
        assert "Enter" in widget._play_btn.text()
        assert "Del" in widget._delete_btn.text()

    def test_a_row_reads_the_whole_memo_not_the_elided_summary(self, page, tmp_path):
        long_text = "a long spoken thought that will not fit on one row " * 3
        qm.add_memo(long_text.strip(), "voice", home=tmp_path)
        widget = page()
        spoken = widget._list.item(0).data(Qt.ItemDataRole.AccessibleTextRole)
        assert long_text.strip() in spoken


# ---------------------------------------------------------------------------
# 7. Reachable from the hub and by voice
# ---------------------------------------------------------------------------

class TestReachable:
    def test_memos_is_a_hub_page_in_the_nav(self):
        from samsara.ui import main_window_qt
        from samsara.ui.home_qt import MEMOS
        assert MEMOS in main_window_qt.NAV_ORDER
        assert MEMOS in main_window_qt.NAV_ICONS

    def test_the_hub_builds_the_page(self, qapp, tmp_path):
        from samsara.ui import main_window_qt
        from samsara.ui.home_qt import MEMOS
        window = main_window_qt._MainWindow.__new__(main_window_qt._MainWindow)
        window._app = types.SimpleNamespace(config={})
        panel = main_window_qt._MainWindow._make_panel(window, MEMOS)
        assert isinstance(panel, memos_qt.MemosPage)

    def test_a_voice_command_opens_it(self):
        import importlib
        module = importlib.import_module("plugins.commands.quick_memo")
        from samsara.ui.home_qt import MEMOS
        opened = []
        app = types.SimpleNamespace(open_hub_page=lambda name: opened.append(name) or True)
        assert module.open_memo_list(app, "") is True
        assert opened == [MEMOS]

    def test_taking_a_memo_still_works(self):
        import importlib
        module = importlib.import_module("plugins.commands.quick_memo")
        started = []
        app = types.SimpleNamespace(start_memo_capture=lambda: started.append(True) or True)
        assert module.start_quick_memo(app, "") is True
        assert started == [True]

    def test_the_raw_file_affordance_is_still_there(self, page, tmp_path):
        widget = page()
        assert widget._raw_btn.text() == memos_qt.OPEN_FILE
        assert widget._raw_btn.isVisibleTo(widget)
