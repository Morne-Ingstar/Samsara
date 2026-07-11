"""Tests for samsara.teach_patterns (parsing/validation/undo-stack) and its
dispatch wiring in plugins/commands/ask_ollama.py's _check_teaching_intent.

Two layers tested:
  1. Pure parsing/validation functions (samsara.teach_patterns) -- no app,
     no I/O.
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
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import teach_patterns as tp
from samsara.ui.voice_training_qt import VoiceTrainingQt
import plugins.commands.ask_ollama as ao


# ── Layer 1: pure parsing/validation ────────────────────────────────────────

class TestParseVocabAdd:
    @pytest.mark.parametrize("text,expected", [
        ("add the word frobnicate to my vocabulary", "frobnicate"),
        ("add frobnicate to my vocabulary", "frobnicate"),
        ("add the word kubernetes to my vocab", "kubernetes"),
        ("add the word data lake to my dictionary", "data lake"),
        ("learn the word gigawatt", "gigawatt"),
    ])
    def test_matches_expected_word(self, text, expected):
        assert tp.parse_vocab_add(text) == expected

    def test_case_insensitive_trigger(self):
        assert tp.parse_vocab_add("ADD THE WORD FOO TO MY VOCABULARY") == "FOO"

    def test_rejects_over_max_words(self):
        assert tp.parse_vocab_add(
            "add the word one two three four five to my vocabulary"
        ) is None

    @pytest.mark.parametrize("text", [
        "what is the vocabulary of a dictionary",
        "correct flat to hat",
        "remember that db means database",
    ])
    def test_near_misses_do_not_match(self, text):
        assert tp.parse_vocab_add(text) is None


class TestParseCorrectionAdd:
    @pytest.mark.parametrize("text,expected", [
        ("correct flat to hat", ("flat", "hat")),
        ("correct recall to re call", ("recall", "re call")),
        ("when you hear recall write re call", ("recall", "re call")),
        ("when you hear flat type hat", ("flat", "hat")),
        ("when you hear flat use hat", ("flat", "hat")),
    ])
    def test_matches_expected_pair(self, text, expected):
        assert tp.parse_correction_add(text) == expected

    @pytest.mark.parametrize("text", [
        "add the word flat to my vocabulary",
        "when I say flat I mean hat",       # Ava alias phrasing, not correction
        "remember that flat means hat",     # Ava alias phrasing, not correction
    ])
    def test_near_misses_do_not_match(self, text):
        assert tp.parse_correction_add(text) is None


class TestParseUndo:
    def test_matches(self):
        assert tp.parse_undo("undo that") is True
        assert tp.parse_undo("Undo That") is True

    @pytest.mark.parametrize("text", ["undo", "undo this", "forget that"])
    def test_near_misses_do_not_match(self, text):
        assert tp.parse_undo(text) is False


class TestParseForget:
    def test_forget_word(self):
        assert tp.parse_forget("forget the word frobnicate") == ("word", "frobnicate")

    def test_forget_correction(self):
        assert tp.parse_forget("forget the correction flat") == ("correction", "flat")

    def test_near_miss_does_not_match(self):
        assert tp.parse_forget("forget my name") is None


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
        """'forget my name' etc. must keep working via ava_corrections/
        ava_profile -- teach_patterns.parse_forget requires the specific
        'the word'/'the correction' phrasing and must not match it."""
        assert tp.parse_forget("forget my name") is None
        assert tp.parse_forget("forget alias db") is None

    def test_the_one_real_collision_found_during_audit(self):
        """Documents WHY dispatch order matters: ava_corrections'
        generic forget pattern WOULD swallow teach_patterns' more
        specific phrasing if checked first -- this is exactly why
        _check_teaching_intent checks teach_patterns.parse_forget()
        before ava_corrections.parse_forget()."""
        from samsara import ava_corrections as ac
        assert ac.parse_forget("forget the word frobnicate") == "the word frobnicate"
        # ...but teach_patterns' own pattern also matches it, more precisely:
        assert tp.parse_forget("forget the word frobnicate") == ("word", "frobnicate")


class TestValidateCorrectionPair:
    def test_accepts_atomic_pair(self):
        assert tp.validate_correction_pair("flat", "hat") == (True, None)

    def test_rejects_case_only(self):
        ok, reason = tp.validate_correction_pair("Flat", "flat")
        assert ok is False
        assert "case" in reason

    def test_rejects_punctuation_only(self):
        # NOTE: correction_capture._is_punctuation_only deliberately keeps
        # apostrophes (contractions aren't punctuation noise) -- "don't"
        # vs "dont" is NOT punctuation-only under that predicate. Use a
        # pair that actually differs only in stripped punctuation.
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

    def test_does_not_call_extract_corrections_diff_gate(self):
        """Regression lock for the audit finding: extract_corrections()
        itself always rejects a standalone short pair via its whole-text
        rewrite gate -- validate_correction_pair must NOT go through that
        path (verified indirectly: a short pair with zero common words,
        which extract_corrections() would reject as 'looks like a
        rewrite', is accepted here)."""
        ok, reason = tp.validate_correction_pair("frobnicate", "kajigger")
        assert ok is True
        assert reason is None


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


# ── Layer 2: dispatch + hot-reload integration (real VoiceTrainingQt) ──────

class _FakeApp:
    """Minimal app stub: real config_path (isolated tmp dir) so
    VoiceTrainingQt's load/save hit real files, everything else mocked."""

    def __init__(self, tmp_path):
        self.config_path = str(tmp_path / "config.json")
        self.config = {}


@pytest.fixture
def app_with_vt(tmp_path):
    app = _FakeApp(tmp_path)
    app.voice_training_window = VoiceTrainingQt(app)
    app.play_sound = MagicMock()
    return app


@pytest.fixture(autouse=True)
def drain_undo_stack():
    tp.pop_last_action()
    yield
    tp.pop_last_action()


class TestDispatchVocabAdd:
    def test_adds_and_confirms(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))

        handled = ao._check_teaching_intent(app_with_vt, "add the word frobnicate to my vocabulary")

        assert handled is True
        assert "frobnicate" in app_with_vt.voice_training_window.custom_vocab
        assert "Added frobnicate" in spoken[-1]
        app_with_vt.play_sound.assert_called_with("success")

    def test_duplicate_is_reported_not_re_added(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        app_with_vt.voice_training_window.add_vocab_word("frobnicate")

        ao._check_teaching_intent(app_with_vt, "add the word frobnicate to my vocabulary")

        assert app_with_vt.voice_training_window.custom_vocab.count("frobnicate") == 1
        assert "already" in spoken[-1]

    def test_hot_reload_next_prompt_includes_new_word(self, app_with_vt, monkeypatch):
        """The actual live-effect requirement: get_initial_prompt() (what
        every hotkey press calls fresh, per dictation.py's
        _build_hotkey_transcribe_params) must include the word
        immediately after the add, with no restart / no separate reload
        call needed -- because custom_vocab is read directly, not
        cached."""
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        assert "frobnicate" not in (app_with_vt.voice_training_window.get_initial_prompt() or "")

        ao._check_teaching_intent(app_with_vt, "add the word frobnicate to my vocabulary")

        prompt = app_with_vt.voice_training_window.get_initial_prompt()
        assert "frobnicate" in prompt


class TestDispatchCorrectionAdd:
    def test_adds_and_confirms_permanence_plus_undo_hint(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))

        handled = ao._check_teaching_intent(app_with_vt, "correct flat to hat")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict["flat"] == "hat"
        assert "From now on" in spoken[-1]
        assert "undo that" in spoken[-1]

    def test_rejects_non_atomic_pair_and_adds_nothing(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))

        handled = ao._check_teaching_intent(
            app_with_vt, "correct the quick brown fox jumps to over the lazy dog"
        )

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}
        assert "can't save" in spoken[-1]

    def test_hot_reload_next_apply_corrections_uses_new_pair(self, app_with_vt, monkeypatch):
        """Live-effect requirement for corrections: apply_corrections()
        reads a PRE-COMPILED regex (_corrections_pattern) that must be
        rebuilt on add -- unlike vocab, this does NOT happen automatically
        without add_correction() calling _rebuild_corrections_pattern()."""
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        assert app_with_vt.voice_training_window.apply_corrections("that looks flat") == "that looks flat"

        ao._check_teaching_intent(app_with_vt, "correct flat to hat")

        assert app_with_vt.voice_training_window.apply_corrections("that looks flat") == "that looks hat"


class TestDispatchUndo:
    def test_undoes_last_vocab_add(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        ao._check_teaching_intent(app_with_vt, "add the word frobnicate to my vocabulary")
        assert "frobnicate" in app_with_vt.voice_training_window.custom_vocab

        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        handled = ao._check_teaching_intent(app_with_vt, "undo that")

        assert handled is True
        assert "frobnicate" not in app_with_vt.voice_training_window.custom_vocab
        assert "Undone" in spoken[-1]

    def test_undoes_last_correction_add(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        ao._check_teaching_intent(app_with_vt, "correct flat to hat")
        assert app_with_vt.voice_training_window.corrections_dict == {"flat": "hat"}

        handled = ao._check_teaching_intent(app_with_vt, "undo that")

        assert handled is True
        assert app_with_vt.voice_training_window.corrections_dict == {}

    def test_undo_with_nothing_to_undo(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        handled = ao._check_teaching_intent(app_with_vt, "undo that")
        assert handled is True
        assert "Nothing to undo" in spoken[-1]

    def test_undo_only_reverses_the_most_recent_add(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        ao._check_teaching_intent(app_with_vt, "add the word alpha to my vocabulary")
        ao._check_teaching_intent(app_with_vt, "add the word beta to my vocabulary")

        ao._check_teaching_intent(app_with_vt, "undo that")

        assert "beta" not in app_with_vt.voice_training_window.custom_vocab
        assert "alpha" in app_with_vt.voice_training_window.custom_vocab


class TestDispatchForget:
    def test_forget_word_removes_by_phrase(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        app_with_vt.voice_training_window.add_vocab_word("frobnicate")

        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        handled = ao._check_teaching_intent(app_with_vt, "forget the word frobnicate")

        assert handled is True
        assert "frobnicate" not in app_with_vt.voice_training_window.custom_vocab
        assert "Forgotten" in spoken[-1]

    def test_forget_correction_removes_by_phrase(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        app_with_vt.voice_training_window.add_correction("flat", "hat")

        handled = ao._check_teaching_intent(app_with_vt, "forget the correction flat")

        assert handled is True
        assert "flat" not in app_with_vt.voice_training_window.corrections_dict

    def test_forget_nonexistent_reports_absence(self, app_with_vt, monkeypatch):
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        handled = ao._check_teaching_intent(app_with_vt, "forget the word nosuchword")
        assert handled is True
        assert "don't have" in spoken[-1]


class TestDispatchAvaAliasUnaffected:
    """The existing Ava-alias teaching path must keep working byte-for-byte
    through the SAME dispatch function, unshadowed by the new checks
    inserted ahead of it."""

    def test_ava_teaching_still_works(self, app_with_vt, monkeypatch):
        """Mocks ava_corrections.add/get rather than exercising the real
        module -- ava_corrections.json is a real, shared user-data file
        (samsara_home_dir()), not a per-test fixture; this test only needs
        to prove _check_teaching_intent still routes to it correctly, not
        re-test ava_corrections.py itself (that module's own behavior is
        out of scope here and unchanged by this task)."""
        from samsara import ava_corrections as ac
        spoken = []
        monkeypatch.setattr(ao, "speak", lambda a, t: spoken.append(t))
        monkeypatch.setattr(ac, "get", lambda phrase: None)  # no existing alias -- straight add path
        add_calls = []
        monkeypatch.setattr(ac, "add", lambda phrase, expansion: (add_calls.append((phrase, expansion)), ("added", None))[1])

        handled = ao._check_teaching_intent(app_with_vt, "when I say db I mean database")

        assert handled is True
        assert "means" in spoken[-1]
        assert add_calls == [("db", "database")]

    def test_non_teaching_text_falls_through_to_llm(self, app_with_vt, monkeypatch):
        monkeypatch.setattr(ao, "speak", lambda a, t: None)
        assert ao._check_teaching_intent(app_with_vt, "please schedule a meeting for tomorrow") is False
