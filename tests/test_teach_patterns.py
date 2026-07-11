"""Tests for samsara.teach_patterns (parsing/validation/buffer-resolution/
source-grabs/undo-stack) and its dispatch wiring in
plugins/commands/ask_ollama.py's _check_teaching_intent +
_check_teach_pending_gate + handle_ava_confirm.

Layers:
  1. Pure parsing/validation/resolution functions (samsara.teach_patterns)
     -- no app, no I/O (clipboard/pyautogui calls are exercised separately
     via monkeypatch in Layer 1b).
  1b. grab_selection_text / grab_clipboard_text -- mocked clipboard/
     pyautogui (samsara.clipboard and pyautogui are monkeypatched; no real
     clipboard touched).
  2. Dispatch + hot-reload integration -- a REAL VoiceTrainingQt instance
     (not a stub) pointed at an isolated tmp_path config dir, so
     add_vocab_word()/add_correction() and the hot-reload assertions
     (get_initial_prompt()/apply_corrections() reflecting the add with no
     restart) exercise the actual production code path, not a mock of it.
     No Qt event loop / QApplication needed -- VoiceTrainingQt's __init__
     only calls load_training_data(), no widget construction (same
     pattern already used by tools/transcribe_ab.py).
"""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import teach_patterns as tp
from samsara.ui.voice_training_qt import VoiceTrainingQt
import plugins.commands.ask_ollama as ao


# ============================================================================
# Layer 1: pure parsing/validation/resolution
# ============================================================================

class TestParseVocabAdd:
    @pytest.mark.parametrize("text,expected", [
        ("add the word frobnicate to my vocabulary",
         {'kind': 'literal', 'word': 'frobnicate', 'letters': None}),
        ("add frobnicate to my vocabulary",
         {'kind': 'literal', 'word': 'frobnicate', 'letters': None}),
        ("add the word kubernetes to my vocab",
         {'kind': 'literal', 'word': 'kubernetes', 'letters': None}),
        ("add the word data lake to my dictionary",
         {'kind': 'literal', 'word': 'data lake', 'letters': None}),
        ("learn the word gigawatt",
         {'kind': 'literal', 'word': 'gigawatt', 'letters': None}),
        ("add the word morne to my vocabulary spelled M O R N E",
         {'kind': 'literal', 'word': 'morne', 'letters': 'M O R N E'}),
        ("learn the word morne spelled M O R N E",
         {'kind': 'literal', 'word': 'morne', 'letters': 'M O R N E'}),
    ])
    def test_matches_expected(self, text, expected):
        assert tp.parse_vocab_add(text) == expected

    def test_case_insensitive_trigger(self):
        result = tp.parse_vocab_add("ADD THE WORD FOO TO MY VOCABULARY")
        assert result == {'kind': 'literal', 'word': 'FOO', 'letters': None}

    def test_rejects_over_max_words(self):
        assert tp.parse_vocab_add(
            "add the word one two three four five to my vocabulary"
        ) is None

    @pytest.mark.parametrize("text,source", [
        ("add the selection to my vocabulary", 'selection'),
        ("add selection to my vocab", 'selection'),
        ("add selected word to my vocabulary", 'selection'),
        ("add the selected text to my vocab", 'selection'),
        ("add highlighted word to my dictionary", 'selection'),
        ("add the highlighted text to my vocabulary", 'selection'),
        ("add clipboard to my vocabulary", 'clipboard'),
        ("add the clipboard to my vocab", 'clipboard'),
    ])
    def test_source_grab_patterns(self, text, source):
        assert tp.parse_vocab_add(text) == {'kind': 'source', 'source': source}

    @pytest.mark.parametrize("text", [
        "what is the vocabulary of a dictionary",
        "correct flat to hat",
        "remember that db means database",
    ])
    def test_near_misses_do_not_match(self, text):
        assert tp.parse_vocab_add(text) is None


class TestParseCorrectionAdd:
    def test_named_lhs_literal_rhs(self):
        assert tp.parse_correction_add("correct flat to hat") == {
            'lhs_kind': 'named', 'lhs_raw': 'flat',
            'rhs_kind': 'literal', 'rhs': 'hat', 'letters': None,
        }

    def test_that_lhs(self):
        assert tp.parse_correction_add("correct that to hat") == {
            'lhs_kind': 'that', 'lhs_raw': None,
            'rhs_kind': 'literal', 'rhs': 'hat', 'letters': None,
        }

    def test_that_lhs_with_letters(self):
        assert tp.parse_correction_add("correct that to hat spelled H A T") == {
            'lhs_kind': 'that', 'lhs_raw': None,
            'rhs_kind': 'literal', 'rhs': 'hat', 'letters': 'H A T',
        }

    def test_named_lhs_with_letters(self):
        assert tp.parse_correction_add("correct flat to hat spelled H A T") == {
            'lhs_kind': 'named', 'lhs_raw': 'flat',
            'rhs_kind': 'literal', 'rhs': 'hat', 'letters': 'H A T',
        }

    @pytest.mark.parametrize("text,rhs", [
        ("when you hear recall write re call", "re call"),
        ("when you hear flat type hat", "hat"),
        ("when you hear flat use hat", "hat"),
        ("when you write flat use hat", "hat"),
    ])
    def test_when_you_hear_write_variants(self, text, rhs):
        result = tp.parse_correction_add(text)
        assert result['lhs_kind'] == 'named'
        assert result['rhs'] == rhs

    def test_when_you_hear_with_letters(self):
        result = tp.parse_correction_add("when you hear flat use hat spelled H A T")
        assert result['letters'] == 'H A T'

    @pytest.mark.parametrize("text,lhs_kind,rhs_source", [
        ("correct that to the selection", 'that', 'selection'),
        ("correct that to selection", 'that', 'selection'),
        ("correct that to selected word", 'that', 'selection'),
        ("correct that to selected text", 'that', 'selection'),
        ("correct that to clipboard", 'that', 'clipboard'),
        ("correct flat to the selection", 'named', 'selection'),
        ("correct flat to clipboard", 'named', 'clipboard'),
    ])
    def test_rhs_source_patterns(self, text, lhs_kind, rhs_source):
        result = tp.parse_correction_add(text)
        assert result['lhs_kind'] == lhs_kind
        assert result['rhs_kind'] == 'source'
        assert result['rhs_source'] == rhs_source

    @pytest.mark.parametrize("text", [
        "add the word flat to my vocabulary",
        "when I say flat I mean hat",       # Ava alias phrasing, not correction
        "remember that flat means hat",     # Ava alias phrasing, not correction
    ])
    def test_near_misses_do_not_match(self, text):
        assert tp.parse_correction_add(text) is None


class TestParseUndoForgetReject:
    def test_undo_matches(self):
        assert tp.parse_undo("undo that") is True
        assert tp.parse_undo("Undo That") is True

    @pytest.mark.parametrize("text", ["undo", "undo this", "forget that"])
    def test_undo_near_misses(self, text):
        assert tp.parse_undo(text) is False

    def test_forget_word(self):
        assert tp.parse_forget("forget the word frobnicate") == ("word", "frobnicate")

    def test_forget_correction(self):
        assert tp.parse_forget("forget the correction flat") == ("correction", "flat")

    @pytest.mark.parametrize("text", [
        "no", "No", "NOPE", "cancel", "nevermind", "never mind",
    ])
    def test_reject_matches(self, text):
        assert tp.parse_reject(text) is True

    @pytest.mark.parametrize("text", ["yes", "not now", "no thanks", ""])
    def test_reject_near_misses(self, text):
        assert tp.parse_reject(text) is False


class TestValidateCorrectionPair:
    def test_accepts_atomic_pair(self):
        assert tp.validate_correction_pair("flat", "hat") == (True, None)

    def test_rejects_case_only(self):
        ok, reason = tp.validate_correction_pair("Flat", "flat")
        assert ok is False
        assert "case" in reason

    def test_rejects_punctuation_only(self):
        ok, reason = tp.validate_correction_pair("hello.", "hello")
        assert ok is False
        assert "punctuation" in reason

    def test_rejects_too_long(self):
        ok, reason = tp.validate_correction_pair(
            "the quick brown fox jumps", "over the lazy dog",
        )
        assert ok is False
        assert "too long" in reason

    def test_rejects_empty(self):
        assert tp.validate_correction_pair("", "hat")[0] is False
        assert tp.validate_correction_pair("flat", "")[0] is False

    def test_rejects_identical(self):
        assert tp.validate_correction_pair("flat", "flat")[0] is False


class TestIsKnownDictionaryWord:
    def test_common_word_is_known(self):
        assert tp.is_known_dictionary_word("hello") is True

    def test_multi_word_phrase_all_known(self):
        assert tp.is_known_dictionary_word("the quick") is True

    def test_proper_noun_not_known(self):
        assert tp.is_known_dictionary_word("morne") is False

    def test_tech_term_not_known(self):
        assert tp.is_known_dictionary_word("kubernetes") is False

    def test_multi_word_one_unknown_fails_whole_phrase(self):
        assert tp.is_known_dictionary_word("hello morne") is False

    def test_hyphenated_token_fails_closed(self):
        assert tp.is_known_dictionary_word("data-lake") is False

    def test_empty_string(self):
        assert tp.is_known_dictionary_word("") is False


class TestResolveCorrectionTarget:
    BUFFER = [
        "I want to buy a flat today",       # most recent
        "the weather is nice",
        "kubernetes cluster is down",
    ]

    def test_that_returns_literal_most_recent_segment(self):
        assert tp.resolve_correction_target('that', None, self.BUFFER) == self.BUFFER[0]

    def test_that_with_empty_buffer_returns_none(self):
        assert tp.resolve_correction_target('that', None, []) is None

    def test_named_exact_whole_segment_match(self):
        assert tp.resolve_correction_target('named', 'kubernetes cluster is down', self.BUFFER) \
            == "kubernetes cluster is down"

    def test_named_windowed_match_inside_longer_segment(self):
        # "flat" is a single word buried inside a 7-word segment -- must
        # resolve via the windowed comparison, not the whole-segment ratio.
        assert tp.resolve_correction_target('named', 'flat', self.BUFFER) == "flat"

    def test_named_no_match_returns_none(self):
        assert tp.resolve_correction_target('named', 'quantum entanglement', self.BUFFER) is None

    def test_named_with_empty_buffer_returns_none(self):
        assert tp.resolve_correction_target('named', 'flat', []) is None

    def test_named_with_none_raw_returns_none(self):
        assert tp.resolve_correction_target('named', None, self.BUFFER) is None

    def test_never_falls_back_to_lhs_raw_on_no_match(self):
        """The core anti-bootstrap guarantee: an unmatched named target
        must return None, NEVER the raw (untrusted) transcribed text."""
        result = tp.resolve_correction_target('named', 'completely unrelated phrase', self.BUFFER)
        assert result is None
        assert result != 'completely unrelated phrase'

    def test_only_considers_last_window_segments(self):
        long_buffer = [f"segment number {i}" for i in range(20)] + ["target word here"]
        # "target word here" is the OLDEST entry (index 20), outside the
        # default 10-entry window applied to the front of the list --
        # must NOT be found.
        assert tp.resolve_correction_target('named', 'target word here', long_buffer) is None


class TestGetRecentDictatedSegments:
    class _App:
        pass

    def test_returns_most_recent_first_excluding_commands(self):
        app = self._App()
        app.history = [
            ("t1", "first segment", False),
            ("t2", "a command", True),
            ("t3", "second segment", False),
            ("t4", "third segment", False),
        ]
        assert tp.get_recent_dictated_segments(app) == [
            "third segment", "second segment", "first segment",
        ]

    def test_no_history_attribute_returns_empty(self):
        app = self._App()
        assert tp.get_recent_dictated_segments(app) == []

    def test_empty_history_returns_empty(self):
        app = self._App()
        app.history = []
        assert tp.get_recent_dictated_segments(app) == []

    def test_respects_limit(self):
        app = self._App()
        app.history = [(f"t{i}", f"seg{i}", False) for i in range(15)]
        assert len(tp.get_recent_dictated_segments(app, limit=5)) == 5


class TestSanitizeSourceText:
    def test_strips_whitespace_and_punctuation(self):
        assert tp._sanitize_source_text("  Hello, World!  ") == "Hello, World"

    def test_collapses_internal_whitespace(self):
        assert tp._sanitize_source_text("hello    world") == "hello world"

    def test_empty_returns_none(self):
        assert tp._sanitize_source_text("") is None
        assert tp._sanitize_source_text(None) is None
        assert tp._sanitize_source_text("   ") is None

    def test_over_max_words_returns_none(self):
        assert tp._sanitize_source_text("one two three four five") is None

    def test_over_max_chars_returns_none(self):
        assert tp._sanitize_source_text("a" * 100) is None

    def test_within_bounds_accepted(self):
        assert tp._sanitize_source_text("data lake") == "data lake"


class TestReadbackBuilders:
    def test_letters_readback(self):
        assert tp.build_letters_readback("Morne") == "M, O, R, N, E -- Morne"

    def test_letters_readback_with_punctuation(self):
        result = tp.build_letters_readback("Data-Lake")
        assert "hyphen" in result
        assert result.endswith("-- Data-Lake")

    def test_vocab_confirmation_prompt(self):
        prompt = tp.build_vocab_confirmation_prompt("Morne")
        assert "M, O, R, N, E" in prompt
        assert "Save it to your vocabulary?" in prompt

    def test_correction_confirmation_prompt(self):
        prompt = tp.build_correction_confirmation_prompt("flat", "hat")
        assert "Correct 'flat' to" in prompt
        assert "H, A, T -- hat" in prompt


class TestLastActionStack:
    def setup_method(self):
        tp.pop_last_action()  # drain any leftover state between tests

    def test_records_and_pops(self):
        tp.record_last_action('vocab', word='frobnicate')
        assert tp.peek_last_action() == {'kind': 'vocab', 'word': 'frobnicate'}
        popped = tp.pop_last_action()
        assert popped == {'kind': 'vocab', 'word': 'frobnicate'}
        assert tp.pop_last_action() is None  # drained

    def test_new_action_overwrites_previous(self):
        tp.record_last_action('vocab', word='a')
        tp.record_last_action('correction', wrong='b', right='c')
        assert tp.pop_last_action() == {'kind': 'correction', 'wrong': 'b', 'right': 'c'}


# ============================================================================
# Layer 1b: selection / clipboard grabs -- mocked clipboard + pyautogui
# ============================================================================

class TestGrabClipboardText:
    def test_returns_sanitized_clipboard_text(self, monkeypatch):
        import pyperclip
        monkeypatch.setattr(pyperclip, "paste", lambda: "  hello world  ")
        assert tp.grab_clipboard_text() == "hello world"

    def test_empty_clipboard_returns_none(self, monkeypatch):
        import pyperclip
        monkeypatch.setattr(pyperclip, "paste", lambda: "")
        assert tp.grab_clipboard_text() is None

    def test_oversized_clipboard_returns_none(self, monkeypatch):
        import pyperclip
        monkeypatch.setattr(pyperclip, "paste", lambda: "one two three four five six")
        assert tp.grab_clipboard_text() is None

    def test_pyperclip_exception_returns_none(self, monkeypatch):
        import pyperclip
        def _raise():
            raise RuntimeError("no clipboard access")
        monkeypatch.setattr(pyperclip, "paste", _raise)
        assert tp.grab_clipboard_text() is None


class TestGrabSelectionText:
    """Mocks samsara.clipboard's module-level functions and pyautogui --
    no real clipboard or keyboard event touched. Exercises the exact
    algorithm commit 4ca1ad8 was built for and left unwired: snapshot ->
    Ctrl+C -> poll sequence number -> read-or-refuse -> ALWAYS restore."""

    def test_seq_advances_reads_and_restores(self, monkeypatch):
        from samsara import clipboard as clipboard_module
        import pyautogui
        import pyperclip

        pre_snapshot = clipboard_module.ClipboardSnapshot({1: b"original"})
        monkeypatch.setattr(clipboard_module, "save_clipboard", lambda: pre_snapshot)

        seq_values = iter([100, 100, 101])  # before, one no-change poll, then advanced
        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number", lambda: next(seq_values))

        hotkey_calls = []
        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: hotkey_calls.append(a))

        monkeypatch.setattr(pyperclip, "paste", lambda: "selected text")

        restore_calls = []
        monkeypatch.setattr(clipboard_module, "restore_clipboard", lambda snap: restore_calls.append(snap) or True)

        result = tp.grab_selection_text(timeout_s=1.0)

        assert result == "selected text"
        assert hotkey_calls == [('ctrl', 'c')]
        assert restore_calls == [pre_snapshot]

    def test_seq_never_advances_returns_none_but_still_restores(self, monkeypatch):
        from samsara import clipboard as clipboard_module
        import pyautogui

        pre_snapshot = clipboard_module.ClipboardSnapshot({1: b"original"})
        monkeypatch.setattr(clipboard_module, "save_clipboard", lambda: pre_snapshot)
        # Sequence number never changes -- "nothing was selected".
        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number", lambda: 100)
        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: None)

        restore_calls = []
        monkeypatch.setattr(clipboard_module, "restore_clipboard", lambda snap: restore_calls.append(snap) or True)

        result = tp.grab_selection_text(timeout_s=0.1)

        assert result is None
        assert restore_calls == [pre_snapshot], "clipboard must be restored even on the empty-selection path"

    def test_pre_snapshot_seq_left_unset_so_restore_is_not_self_blocked(self, monkeypatch):
        """Regression lock for the documented design decision: the
        pre-Ctrl+C snapshot must NOT have .seq set to seq_before, or
        restore_clipboard's own seq-guard (commit 4ca1ad8) would see
        "clipboard changed since snapshot" (true -- our own Ctrl+C did
        that) and silently skip the restore, permanently clobbering the
        user's real clipboard with whatever was just selected."""
        from samsara import clipboard as clipboard_module
        import pyautogui
        import pyperclip

        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number",
                             iter([100, 101]).__next__)
        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: None)
        monkeypatch.setattr(pyperclip, "paste", lambda: "grabbed")

        captured_snapshot = {}

        def fake_save():
            snap = clipboard_module.ClipboardSnapshot({1: b"orig"})
            captured_snapshot['snap'] = snap
            return snap

        monkeypatch.setattr(clipboard_module, "save_clipboard", fake_save)

        real_restore_calls = []

        def fake_restore(snap):
            real_restore_calls.append(snap)
            return True

        monkeypatch.setattr(clipboard_module, "restore_clipboard", fake_restore)

        tp.grab_selection_text(timeout_s=1.0)

        assert captured_snapshot['snap'].seq is None, \
            "pre-Ctrl+C snapshot's .seq must stay None -- see grab_selection_text's docstring"
        assert real_restore_calls == [captured_snapshot['snap']]

    def test_restores_even_if_read_raises(self, monkeypatch):
        """finally-block guarantee: an exception reading pyperclip after a
        successful Ctrl+C must not skip the restore."""
        from samsara import clipboard as clipboard_module
        import pyautogui
        import pyperclip

        pre_snapshot = clipboard_module.ClipboardSnapshot({1: b"original"})
        monkeypatch.setattr(clipboard_module, "save_clipboard", lambda: pre_snapshot)
        monkeypatch.setattr(clipboard_module, "get_clipboard_sequence_number",
                             iter([100, 101]).__next__)
        monkeypatch.setattr(pyautogui, "hotkey", lambda *a, **kw: None)

        def _raise():
            raise RuntimeError("clipboard access denied")
        monkeypatch.setattr(pyperclip, "paste", _raise)

        restore_calls = []
        monkeypatch.setattr(clipboard_module, "restore_clipboard", lambda snap: restore_calls.append(snap) or True)

        result = tp.grab_selection_text(timeout_s=1.0)

        assert result is None
        assert restore_calls == [pre_snapshot]


class TestAvaAliasPatternsUnshadowed:
    """The critical no-shadowing regression: existing Ava-alias phrasings
    must NOT be captured by the new patterns, in either direction."""

    @pytest.mark.parametrize("text", [
        "when I say db I mean database",
        "remember that db means database",
        "remember db means database",
        "let's call this project the moonshot",
        "from now on db is database",
        "db means database",
    ])
    def test_ava_teaching_phrases_do_not_match_new_patterns(self, text):
        assert tp.parse_vocab_add(text) is None
        assert tp.parse_correction_add(text) is None

    def test_ava_forget_generic_phrase_untouched_by_new_forget_pattern(self):
        assert tp.parse_forget("forget my name") is None
        assert tp.parse_forget("forget alias db") is None

    def test_the_one_real_collision_found_during_audit(self):
        from samsara import ava_corrections as ac
        assert ac.parse_forget("forget the word frobnicate") == "the word frobnicate"
        assert tp.parse_forget("forget the word frobnicate") == ("word", "frobnicate")


# ============================================================================
# Layer 2: dispatch + hot-reload integration (real VoiceTrainingQt)
# ============================================================================

class _FakeApp:
    """Minimal app stub: real config_path (isolated tmp dir) so
    VoiceTrainingQt's load/save hit real files, everything else mocked."""

    def __init__(self, tmp_path):
        self.config_path = str(tmp_path / "config.json")
        self.config = {}
        self.history = []  # session buffer -- (timestamp, text, is_command) tuples


@pytest.fixture
def app_with_vt(tmp_path):
    app = _FakeApp(tmp_path)
    app.voice_training_window = VoiceTrainingQt(app)
    app.play_sound = MagicMock()
    return app


def _dictate(app, text):
    """Append a plain-dictation segment to the session buffer, mirroring
    dictation.py's add_to_history(text, is_command=False)."""
    app.history.append((str(time.time()), text, False))


@pytest.fixture(autouse=True)
def drain_module_state():
    """Both teach_patterns._last_action and ask_ollama._pending_action are
    module-level globals -- drain before AND after every test so no test
    can leak state into the next one (matches the established pattern in
    this file for _last_action; extended here to also cover
    _pending_action, which the confirmation-flow tests below rely on)."""
    tp.pop_last_action()
    ao._pending_action = None
    yield
    tp.pop_last_action()
    ao._pending_action = None


def _speak_capture(monkeypatch):
    spoken = []
    monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
    return spoken


def _expire_speak_window():
    """Force any currently-pending confirmation's self-transcription
    discard window to be already over, as if Ava had finished speaking --
    tests exercising the REPLY to a prompt need this, tests exercising the
    discard-window behavior itself deliberately do NOT call this."""
    if ao._pending_action is not None:
        ao._pending_action['speak_until'] = 0


class TestDispatchVocabAddDictionaryWordFastPath:
    def test_adds_and_confirms_instantly(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")

        assert handled is True
        assert "hello" in app_with_vt.voice_training_window.custom_vocab
        assert "Added hello" in spoken[-1]
        assert ao._pending_action is None  # no confirmation gate for a known word
        app_with_vt.play_sound.assert_called_with("success")

    def test_duplicate_is_reported_not_re_added(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        app_with_vt.voice_training_window.add_vocab_word("hello")

        ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")

        assert app_with_vt.voice_training_window.custom_vocab.count("hello") == 1
        assert "already" in spoken[-1]

    def test_hot_reload_next_prompt_includes_new_word(self, app_with_vt, monkeypatch):
        """STEP 1(c) hot-reload assertion: get_initial_prompt() (what
        dictation.py calls fresh before every transcription) must include
        the word immediately after the add, no restart / no separate
        reload call -- because custom_vocab is read directly, not
        cached."""
        _speak_capture(monkeypatch)
        assert "hello" not in (app_with_vt.voice_training_window.get_initial_prompt() or "")

        ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")

        prompt = app_with_vt.voice_training_window.get_initial_prompt()
        assert "hello" in prompt


class TestDispatchVocabAddSpellingFlow:
    def test_non_dictionary_word_without_spelled_opens_spelling_wait(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")

        assert handled is True
        assert app_with_vt.voice_training_window.custom_vocab == []
        assert ao._pending_action['type'] == 'vocab_spelling_wait'
        assert "Spell that for me" in spoken[-1]

    def test_spelling_wait_then_valid_letters_opens_confirmation(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()

        handled = ao._check_teaching_intent(app_with_vt, "M O R N E")

        assert handled is True
        assert ao._pending_action['type'] == 'vocab_confirm'
        assert ao._pending_action['word'] == 'Morne'
        assert "M, O, R, N, E" in spoken[-1]
        assert app_with_vt.voice_training_window.custom_vocab == []  # not persisted yet

    def test_spelling_wait_then_garbled_letters_reprompts_without_losing_state(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()

        handled = ao._check_teaching_intent(app_with_vt, "the quick brown fox")

        assert handled is True
        assert ao._pending_action['type'] == 'vocab_spelling_wait'  # still waiting
        assert "didn't catch" in spoken[-1]

    def test_confirmation_then_yes_persists(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()
        ao._check_teaching_intent(app_with_vt, "M O R N E")
        _expire_speak_window()

        ao.handle_ava_confirm(app_with_vt)

        assert "Morne" in app_with_vt.voice_training_window.custom_vocab
        assert ao._pending_action is None
        assert "Added Morne" in spoken[-1]

    def test_confirmation_then_no_rejects_without_persisting(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()
        ao._check_teaching_intent(app_with_vt, "M O R N E")
        _expire_speak_window()

        handled = ao._check_teaching_intent(app_with_vt, "no")

        assert handled is True
        assert app_with_vt.voice_training_window.custom_vocab == []
        assert ao._pending_action is None
        assert "not saved" in spoken[-1]

    def test_confirmation_then_fresh_letters_respells(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()
        ao._check_teaching_intent(app_with_vt, "M O R M E")  # deliberately wrong
        _expire_speak_window()

        handled = ao._check_teaching_intent(app_with_vt, "M O R N E")  # corrected

        assert handled is True
        assert ao._pending_action['type'] == 'vocab_confirm'
        assert ao._pending_action['word'] == 'Morne'
        assert app_with_vt.voice_training_window.custom_vocab == []  # still not persisted

    def test_spelled_inline_skips_spelling_wait_goes_straight_to_confirm(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary spelled M O R N E")

        assert handled is True
        assert ao._pending_action['type'] == 'vocab_confirm'
        assert ao._pending_action['word'] == 'Morne'

    def test_spelled_inline_garbled_refuses_without_opening_confirmation(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(
            app_with_vt, "add the word morne to my vocabulary spelled xyz123")

        assert handled is True
        assert ao._pending_action is None
        assert "spell it again" in spoken[-1]


class TestDispatchVocabAddSourceGrab:
    def test_selection_advances_and_persists_via_short_confirm(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        monkeypatch.setattr(tp, "grab_selection_text", lambda: "widget name")

        handled = ao._check_teaching_intent(app_with_vt, "add the selection to my vocabulary")

        assert handled is True
        assert "widget name" in app_with_vt.voice_training_window.custom_vocab
        assert ao._pending_action is None  # text sources skip the readback gate
        assert "Added widget name" in spoken[-1]

    def test_empty_selection_refuses(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        monkeypatch.setattr(tp, "grab_selection_text", lambda: None)

        handled = ao._check_teaching_intent(app_with_vt, "add the selection to my vocabulary")

        assert handled is True
        assert app_with_vt.voice_training_window.custom_vocab == []
        assert "Nothing is selected" in spoken[-1]

    def test_clipboard_source(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        monkeypatch.setattr(tp, "grab_clipboard_text", lambda: "clipped word")

        handled = ao._check_teaching_intent(app_with_vt, "add clipboard to my vocabulary")

        assert handled is True
        assert "clipped word" in app_with_vt.voice_training_window.custom_vocab

    def test_empty_clipboard_refuses(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        monkeypatch.setattr(tp, "grab_clipboard_text", lambda: None)

        handled = ao._check_teaching_intent(app_with_vt, "add clipboard to my vocabulary")

        assert handled is True
        assert app_with_vt.voice_training_window.custom_vocab == []
        assert "clipboard is empty" in spoken[-1]


class TestDispatchCorrectionAddBufferSourced:
    def test_named_lhs_dictionary_rhs_instant_persist(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want to buy a flat today")

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to hat")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {"flat": "hat"}
        assert "From now on" in spoken[-1]
        assert "undo that" in spoken[-1]
        assert ao._pending_action is None  # dictionary RHS -- no gate

    def test_lhs_stored_is_buffer_literal_not_teaching_utterance_transcription(self, app_with_vt, monkeypatch):
        """The core anti-bootstrap assertion for corrections: the
        RESOLUTION always routes through the buffer, never the teaching
        utterance's own X token, verified directly against
        resolve_correction_target (the exact function dispatch calls)."""
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "the kubernetes cluster is down")

        assert tp.resolve_correction_target('named', 'kubernetes', ["the kubernetes cluster is down"]) \
            == "kubernetes"

        ao._check_teaching_intent(app_with_vt, "correct kubernetes to online")
        assert "kubernetes" in app_with_vt.voice_training_window.corrections_dict

    def test_that_lhs_resolves_to_last_segment(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "the weather is nice")

        ao._check_teaching_intent(app_with_vt, "correct that to sunny")

        assert app_with_vt.voice_training_window.corrections_dict == {"the weather is nice": "sunny"}

    def test_no_buffer_match_refuses_and_persists_nothing(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        _dictate(app_with_vt, "the weather is nice")

        handled = ao._check_teaching_intent(app_with_vt, "correct xyzzy to hat")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "recently wrote" in spoken[-1]

    def test_empty_buffer_that_refuses(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "correct that to hat")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "recently wrote" in spoken[-1]

    def test_empty_buffer_named_refuses(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to hat")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "recently wrote" in spoken[-1]

    def test_rejects_non_atomic_pair_and_adds_nothing(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        _dictate(app_with_vt, "the quick brown fox jumps")

        handled = ao._check_teaching_intent(
            app_with_vt, "correct the quick brown fox jumps to over the lazy dog"
        )

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "can't save" in spoken[-1]

    def test_hot_reload_next_apply_corrections_uses_new_pair(self, app_with_vt, monkeypatch):
        """STEP 1(c) hot-reload assertion for corrections: apply_corrections()
        reads a pre-compiled regex that must be rebuilt on add -- proven
        end-to-end through the real dispatch path, no restart."""
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "that looks flat")
        assert app_with_vt.voice_training_window.apply_corrections("that looks flat") == "that looks flat"

        ao._check_teaching_intent(app_with_vt, "correct flat to hat")

        assert app_with_vt.voice_training_window.apply_corrections("that looks flat") == "that looks hat"


class TestDispatchCorrectionAddSpellingAndSourceFlows:
    def test_spelled_rhs_opens_confirmation(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to hat spelled H A T")

        assert handled is True
        assert ao._pending_action['type'] == 'correction_confirm'
        assert ao._pending_action['wrong'] == 'flat'
        assert ao._pending_action['right'] == 'Hat'
        assert app_with_vt.voice_training_window.corrections_dict == {}

    def test_non_dictionary_rhs_without_spelled_opens_spelling_wait(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to morne")

        assert handled is True
        assert ao._pending_action['type'] == 'correction_spelling_wait'
        assert ao._pending_action['wrong'] == 'flat'
        assert "Spell that for me" in spoken[-1]

    def test_spelling_wait_then_letters_opens_confirmation(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")
        ao._check_teaching_intent(app_with_vt, "correct flat to morne")
        _expire_speak_window()

        handled = ao._check_teaching_intent(app_with_vt, "M O R N E")

        assert handled is True
        assert ao._pending_action['type'] == 'correction_confirm'
        assert ao._pending_action['wrong'] == 'flat'
        assert ao._pending_action['right'] == 'Morne'

    def test_confirmation_yes_persists_correction(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")
        ao._check_teaching_intent(app_with_vt, "correct flat to hat spelled H A T")
        _expire_speak_window()

        ao.handle_ava_confirm(app_with_vt)

        assert app_with_vt.voice_training_window.corrections_dict == {"flat": "Hat"}
        assert ao._pending_action is None

    def test_rhs_selection_source_skips_readback(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")
        monkeypatch.setattr(tp, "grab_selection_text", lambda: "penthouse")

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to the selection")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {"flat": "penthouse"}
        assert ao._pending_action is None

    def test_rhs_selection_empty_refuses(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")
        monkeypatch.setattr(tp, "grab_selection_text", lambda: None)

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to the selection")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "Nothing is selected" in spoken[-1]


class TestSelfTranscriptionDiscardWindow:
    """STEP 3F scoped mitigation: an utterance arriving before the
    estimated end of Ava's own readback must be silently discarded, not
    misread as a reply."""

    def test_reply_within_speak_window_is_discarded(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        assert ao._pending_action['speak_until'] > time.time()
        spoken.clear()

        handled = ao._check_teaching_intent(app_with_vt, "M O R N E")

        assert handled is True
        assert spoken == []  # nothing spoken -- discarded silently
        assert ao._pending_action['type'] == 'vocab_spelling_wait'  # state untouched

    def test_reply_after_speak_window_is_processed(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()
        spoken.clear()

        handled = ao._check_teaching_intent(app_with_vt, "M O R N E")

        assert handled is True
        assert spoken != []
        assert ao._pending_action['type'] == 'vocab_confirm'


class TestPendingConfirmationExpiry:
    def test_expired_pending_is_cleared_and_utterance_processed_fresh(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        ao._pending_action['expires'] = time.time() - 1  # force expiry
        spoken = _speak_capture(monkeypatch)

        handled = ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")

        assert handled is True
        assert "hello" in app_with_vt.voice_training_window.custom_vocab
        assert ao._pending_action is None


class TestYesDuringSpellingWaitGivesFeedback:
    def test_yes_during_spelling_wait_does_not_silently_noop(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word morne to my vocabulary")
        _expire_speak_window()
        spoken = _speak_capture(monkeypatch)

        ao.handle_ava_confirm(app_with_vt)

        assert spoken != []
        assert "still waiting" in spoken[-1]
        assert ao._pending_action['type'] == 'vocab_spelling_wait'  # untouched


class TestDispatchUndo:
    def test_undoes_last_vocab_add(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")
        assert "hello" in app_with_vt.voice_training_window.custom_vocab

        spoken = _speak_capture(monkeypatch)
        handled = ao._check_teaching_intent(app_with_vt, "undo that")

        assert handled is True
        assert "hello" not in app_with_vt.voice_training_window.custom_vocab
        assert "Undone" in spoken[-1]

    def test_undoes_last_correction_add(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        _dictate(app_with_vt, "I want a flat")
        ao._check_teaching_intent(app_with_vt, "correct flat to hat")
        assert app_with_vt.voice_training_window.corrections_dict == {"flat": "hat"}

        handled = ao._check_teaching_intent(app_with_vt, "undo that")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}

    def test_undo_with_nothing_to_undo(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        handled = ao._check_teaching_intent(app_with_vt, "undo that")
        assert handled is True
        assert "Nothing to undo" in spoken[-1]

    def test_undo_only_reverses_the_most_recent_add(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        ao._check_teaching_intent(app_with_vt, "add the word hello to my vocabulary")
        ao._check_teaching_intent(app_with_vt, "add the word world to my vocabulary")

        ao._check_teaching_intent(app_with_vt, "undo that")

        assert "world" not in app_with_vt.voice_training_window.custom_vocab
        assert "hello" in app_with_vt.voice_training_window.custom_vocab


class TestDispatchForget:
    def test_forget_word_removes_by_phrase(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        app_with_vt.voice_training_window.add_vocab_word("hello")

        spoken = _speak_capture(monkeypatch)
        handled = ao._check_teaching_intent(app_with_vt, "forget the word hello")

        assert handled is True
        assert "hello" not in app_with_vt.voice_training_window.custom_vocab
        assert "Forgotten" in spoken[-1]

    def test_forget_correction_removes_by_phrase(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        app_with_vt.voice_training_window.add_correction("flat", "hat")

        handled = ao._check_teaching_intent(app_with_vt, "forget the correction flat")

        assert handled is True
        assert "flat" not in app_with_vt.voice_training_window.corrections_dict

    def test_forget_nonexistent_reports_absence(self, app_with_vt, monkeypatch):
        spoken = _speak_capture(monkeypatch)
        handled = ao._check_teaching_intent(app_with_vt, "forget the word nosuchword")
        assert handled is True
        assert "don't have" in spoken[-1]


class TestDispatchAvaAliasUnaffected:
    """The existing Ava-alias teaching path must keep working byte-for-byte
    through the SAME dispatch function, unshadowed by the new checks
    inserted ahead of it."""

    def test_ava_teaching_still_works(self, app_with_vt, monkeypatch):
        from samsara import ava_corrections as ac
        spoken = _speak_capture(monkeypatch)
        monkeypatch.setattr(ac, "get", lambda phrase: None)
        add_calls = []
        monkeypatch.setattr(ac, "add", lambda phrase, expansion: (add_calls.append((phrase, expansion)), ("added", None))[1])

        handled = ao._check_teaching_intent(app_with_vt, "when I say db I mean database")

        assert handled is True
        assert "means" in spoken[-1]
        assert add_calls == [("db", "database")]

    def test_non_teaching_text_falls_through_to_llm(self, app_with_vt, monkeypatch):
        _speak_capture(monkeypatch)
        assert ao._check_teaching_intent(app_with_vt, "please schedule a meeting for tomorrow") is False

    def test_ava_alias_unaffected_while_no_teach_confirmation_pending(self, app_with_vt, monkeypatch):
        """Even with the new pending-confirmation gate inserted at the top
        of _check_teaching_intent, an Ava-alias phrase with NO teach
        confirmation open must reach ava_corrections exactly as before."""
        from samsara import ava_corrections as ac
        _speak_capture(monkeypatch)
        assert ao._pending_action is None
        monkeypatch.setattr(ac, "get", lambda phrase: None)
        monkeypatch.setattr(ac, "add", lambda phrase, expansion: ("added", None))

        handled = ao._check_teaching_intent(app_with_vt, "remember that db means database")

        assert handled is True
