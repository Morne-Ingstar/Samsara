"""Queue 106: the DICTATE context prompt poisons itself.

Two mechanisms, two fixes, both in dictation.py:

  ECHO (output side) -- the decode reproduces the end of the prompt it was
  given, as if it had been spoken. Live, 2026-09-15:
      21:28:53 staged "This changes a thing or two."
      21:28:56 staged "Fuck, no."
      21:29:01 decoded "Changes a thing or two. Fuck, no."   <- both of them
  Refused by _is_context_echo before dispatch_utterance, under a chip.

  STEERING (input side) -- the prompt's trailing tokens bias the next decode
  toward a fragment. perf_artifacts/nd_prompt_contamination.md, 37 recordings
  of a standalone "and": 16/37 correct with no prompt, 33/37 with a clean
  tail, 27/37 with one stray token appended, 3/37 with two, 0/37 with three.
  Cleaned by _sanitise_context_tail before the prompt is handed over.

What is NOT done, and is pinned here: no confidence gate. The owner's genuine
"Fuck, no." carried no_speech_prob 0.646 against the 0.600 threshold, and a
confidence gate would have deleted it.

The five real 200-char tails are loaded from the queue 44 JSON rather than
retyped, so these tests move if that evidence ever does.
"""

import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation
from samsara import transcript_gates
from samsara.session_modes import DispatchOutcome, SessionMode

ROOT = Path(__file__).resolve().parents[1]
TAILS_JSON = ROOT / "perf_artifacts" / "nd_prompt_contamination.json"


@pytest.fixture(scope="module")
def real_tails():
    """The five 200-char prompt tails queue 44 rebuilt from the live log."""
    if not TAILS_JSON.exists():
        pytest.skip(f"queue 44 evidence missing: {TAILS_JSON}")
    return json.loads(TAILS_JSON.read_text(encoding="utf-8"))["tails"]


def _filler(n):
    """n distinct words. Repeated filler would be eaten by the degenerate-run
    trim, which is correct behaviour and a useless fixture."""
    return " ".join(f"w{i}" for i in range(n))


def _seg(text, compression_ratio=1.0, no_speech_prob=0.0):
    return types.SimpleNamespace(
        text=text, compression_ratio=compression_ratio, no_speech_prob=no_speech_prob,
    )


# =============================================================================
# _context_words / _is_context_echo -- pure functions
# =============================================================================

class TestContextWords:

    def test_case_and_outer_punctuation_are_ignored(self):
        assert dictation._context_words('"Fuck, no."') == ["fuck", "no"]
        assert dictation._context_words("Changes a thing or two.") == [
            "changes", "a", "thing", "or", "two"]

    def test_inner_apostrophe_survives_so_dont_stays_one_word(self):
        assert dictation._context_words("I don't know") == ["i", "don't", "know"]
        assert dictation._context_words("I don\u2019t know") == ["i", "don\u2019t", "know"]

    def test_empty_and_pure_punctuation_yield_nothing(self):
        assert dictation._context_words("") == []
        assert dictation._context_words(None) == []
        assert dictation._context_words(" ... -- ") == []


class TestIsContextEcho:
    """The live 21:27-21:29 family, verbatim."""

    #: The pending buffer as it stood at 21:29:01, rebuilt from the staged
    #: texts in samsara.log (pending_chars 160 at 21:28:56 confirms it).
    BUFFER = ("Oh shit, no way. Oh shit, no way. Baby, baby. You're still there."
              " You're still ere. What? What? Well... Well, well, well."
              " This changes a thing or two. Fuck, no.")

    def test_the_2129_echo_is_refused(self):
        assert len(self.BUFFER) == 160          # matches the logged pending_chars
        assert dictation._is_context_echo(
            "Changes a thing or two. Fuck, no.", self.BUFFER) is True

    def test_it_is_still_an_echo_when_only_the_tail_slice_is_given(self):
        """The decoder saw at most the last 200 chars; the check must work on
        exactly the string that was passed as initial_prompt."""
        assert dictation._is_context_echo(
            "Changes a thing or two. Fuck, no.", self.BUFFER[-200:]) is True

    def test_the_four_word_pair_is_refused(self):
        """21:27:17 / 21:27:23, "Oh shit, no way." twice with nothing between."""
        assert dictation._is_context_echo("Oh shit, no way.", "Oh shit, no way.") is True

    def test_a_genuine_repeat_after_a_gap_is_not_an_echo(self):
        """THE discriminator. Same words, but the user said other things in
        between, so the earlier instance is no longer what the decoder was
        conditioned to continue -- it sits in the middle of the tail."""
        gap = "Oh shit, no way. Baby, baby. What? What? Well, well, well."
        assert dictation._is_context_echo("Oh shit, no way.", gap) is False

    def test_short_real_speech_is_never_an_echo(self):
        """"Fuck, no." is two words and sits at the very end of the tail. It
        is real speech and must survive, which is why the floor exists."""
        assert dictation._is_context_echo("Fuck, no.", self.BUFFER) is False
        assert dictation._is_context_echo("Well, well, well.",
                                          "What? What? Well...") is False

    def test_the_one_and_two_word_fragments_are_left_to_the_prompt_side(self):
        assert dictation._is_context_echo("nd", "yeah I don't know and nd nd nd") is False
        assert dictation._is_context_echo("nd nd", "yeah I don't know and nd nd nd") is False

    def test_echo_plus_new_speech_is_not_refused(self):
        """A partial match contains real words. Refusing the utterance would
        eat them, so only a whole-decode match counts."""
        assert dictation._is_context_echo(
            "Changes a thing or two. Fuck, no. And here is something new",
            self.BUFFER) is False

    def test_a_match_that_is_not_at_the_end_is_not_an_echo(self):
        tail = "this changes a thing or two and then I said something else entirely"
        assert dictation._is_context_echo("Changes a thing or two", tail) is False

    def test_no_tail_means_nothing_to_echo(self):
        assert dictation._is_context_echo("Changes a thing or two. Fuck, no.", "") is False
        assert dictation._is_context_echo("anything at all here", None) is False

    def test_the_floor_is_exactly_four_words(self):
        tail = "alpha bravo charlie delta"
        assert dictation._is_context_echo("bravo charlie delta", tail) is False
        assert dictation._is_context_echo("alpha bravo charlie delta", tail) is True


# =============================================================================
# _sanitise_context_tail -- pure function, against the real tails
# =============================================================================

class TestSanitiseContextTail:

    def test_the_cap_tracks_the_managers_own_window(self):
        """_CONTEXT_TAIL_CHARS is not a second opinion about how much context
        to use -- the manager still owns that, and dictate_context_tail() is
        still called with no argument. It only tells the sanitiser how wide
        the window is, so it can tell "this slice was cut out of a longer
        buffer" from "this IS the whole buffer". If the manager's default ever
        moved, front alignment would silently stop firing (or start firing on
        untruncated tails) with nothing to notice it."""
        import inspect
        from samsara.session_modes import SessionModeManager
        default = inspect.signature(
            SessionModeManager.dictate_context_tail).parameters["max_chars"].default
        assert default == dictation._CONTEXT_TAIL_CHARS == 200

    def test_every_real_tail_starts_mid_word_and_no_sanitised_one_does(self, real_tails):
        """The defect this half fixes: dictate_context_tail's source[-200:]
        cut every one of the five live tails inside a word, so the prompt's
        own first token was a fragment."""
        buffers = {
            "14:09:20": "t if", "14:11:36": "de", "14:11:40": "ecause",
            "14:11:45": "use", "14:11:55": "u",
        }
        for key, broken_head in buffers.items():
            raw = real_tails[key]
            assert len(raw) == 200
            assert raw.startswith(broken_head)
            clean = dictation._sanitise_context_tail(raw)
            assert not clean.startswith(broken_head.split()[0] + " ") or " " in broken_head
            # the real assertion: the sanitised head is a word of the raw text
            assert raw.split(clean.split()[0], 1)[0].endswith(" ")

    def test_the_degenerate_run_that_cost_37_of_37_is_removed(self, real_tails):
        """tail_14:11:55 ends "...I don't know and nd nd nd" and decoded "nd"
        37 times out of 37. Sanitised, it ends where tail_14:11:40 ends --
        the 27-of-37 row."""
        clean = dictation._sanitise_context_tail(real_tails["14:11:55"])
        assert clean.endswith("yeah I don't know and")
        assert "nd nd" not in clean

    def test_the_run_is_removed_entirely_not_collapsed_to_one(self):
        """Leaving one behind lands on the tail_14:11:45 row (3 of 37);
        removing all three lands on tail_14:11:40 (27 of 37)."""
        clean = dictation._sanitise_context_tail("yeah I don't know and nd nd nd")
        assert clean == "yeah I don't know and"

    def test_a_single_stray_token_is_left_alone(self, real_tails):
        """Honest limitation, recorded so it is not mistaken for a pass: one
        "nd" is not a repeat run and cannot be told from a real short word,
        so tail_14:11:45 survives sanitisation nearly unchanged."""
        clean = dictation._sanitise_context_tail(real_tails["14:11:45"])
        assert clean.endswith("I don't know and nd")

    def test_trailing_runs_are_trimmed_to_a_fixed_point(self):
        """Removing one run can expose the run that seeded it."""
        assert dictation._sanitise_context_tail("real words here ha ha ha ho ho ho") == \
            "real words here"

    def test_trailing_punctuation_garbage_is_trimmed_too(self):
        assert dictation._sanitise_context_tail("the " + "_" * 20) == "the"

    def test_two_repeats_are_emphasis_not_degeneracy(self):
        """Two was tried and rejected -- see _TAIL_REPEAT_RUN. The trim runs
        to a fixed point, so at two it cascades through consecutive doubled
        phrases and can cut a tail back far enough to MANUFACTURE the
        end-anchored match _is_context_echo then refuses as an echo. The
        cascade is pinned by test_a_genuine_repeat_after_a_gap_is_still_staged
        above; this is the local half."""
        assert dictation._TAIL_REPEAT_RUN == 3
        text = "I said no no"
        assert dictation._sanitise_context_tail(text) == text
        cascade = "Oh shit, no way. Baby, baby. What? What?"
        assert dictation._sanitise_context_tail(cascade) == cascade

    def test_the_five_real_tails_sanitise_identically_at_two_and_three(self, real_tails):
        """Pins the claim made in _TAIL_REPEAT_RUN's comment: no arm in
        nd_prompt_contamination_106.md separates the two thresholds, so the
        measured table stands whichever is chosen."""
        import unittest.mock
        # Queue 128: _sanitise_context_tail now lives in
        # samsara.transcript_gates and reads _TAIL_REPEAT_RUN from that
        # module's globals. dictation re-exports the constant, but patching
        # the re-export would leave the function reading the real 3 and this
        # comparison would pass without ever having tried 2.
        out = {}
        for value in (2, 3):
            with unittest.mock.patch.object(transcript_gates, "_TAIL_REPEAT_RUN", value):
                out[value] = {k: dictation._sanitise_context_tail(v)
                              for k, v in real_tails.items()}
        assert out[2] == out[3]

    def test_a_short_tail_is_not_front_trimmed(self):
        """Nothing was cut off a buffer shorter than the cap, so there is no
        cut to repair and the first word must survive."""
        assert dictation._sanitise_context_tail("I went to the") == "I went to the"

    def test_a_sentence_boundary_is_preferred_when_it_leaves_a_usable_prompt(self):
        """The cut landed inside "fragment"; the prompt starts at the next
        sentence rather than at the broken word."""
        raw = "agment. " + _filler(50)
        clean = dictation._sanitise_context_tail(raw, max_chars=len(raw))
        assert clean == _filler(50)

    def test_a_sentence_boundary_too_near_the_end_falls_back_to_a_word_boundary(self):
        """Starting at the only boundary here would throw away the whole
        prompt to gain a clean start, so the word boundary wins and just the
        cut fragment goes."""
        raw = "agment " + _filler(50) + " tail. end"
        clean = dictation._sanitise_context_tail(raw, max_chars=len(raw))
        assert clean == _filler(50) + " tail. end"

    def test_empty_and_degenerate_inputs_give_an_empty_prompt(self):
        assert dictation._sanitise_context_tail("") == ""
        assert dictation._sanitise_context_tail(None) == ""
        assert dictation._sanitise_context_tail("nd nd nd nd") == ""
        assert dictation._sanitise_context_tail("anything", max_chars=0) == ""

    def test_the_output_never_exceeds_the_cap(self, real_tails):
        for raw in real_tails.values():
            assert len(dictation._sanitise_context_tail(raw)) <= 200


# =============================================================================
# _handle_command_mode_utterance -- the loop, end to end
# =============================================================================

class _LoopManager:
    """A manager whose context tail is driven by what actually got staged.

    dictate_context_tail mirrors SessionModeManager's real implementation
    exactly (samsara/session_modes.py: `return source[-max_chars:]`), and the
    buffer only grows through dispatch_utterance -- which is the real
    manager's only writer of _dictate_pending_buffer / _stage_buffer too.
    That is what makes "refused before dispatch" and "never reaches the next
    prompt" the same statement, and lets these tests run the feedback loop
    the defect lives in rather than one utterance at a time.
    """

    def __init__(self, mode=SessionMode.DICTATE):
        self.mode = mode
        self.buffer = ""
        self.staged = []
        self.dispatch_utterance = Mock(side_effect=self._dispatch)

    def _dispatch(self, text, signals):
        self.staged.append(text)
        self.buffer += (" " if self.buffer else "") + text
        return DispatchOutcome(kind="dictate_staged",
                               detail={"text": text, "pending_chars": len(self.buffer)})

    def dictate_context_tail(self, max_chars=200):
        return self.buffer[-max_chars:]


def _make_loop_app(decodes, *, mode=SessionMode.DICTATE, nsp=0.0):
    """DictationApp stand-in that returns `decodes` one per utterance."""
    from dictation import DictationApp
    from tests.conftest import apply_fake_app_defaults

    queue = list(decodes)
    app = DictationApp.__new__(DictationApp)
    apply_fake_app_defaults(app)
    app._wake_transcription_in_progress = False
    app.config = {}
    app.model_rate = 16000
    app.model_lock = Mock()
    app.model_lock.__enter__ = Mock(return_value=None)
    app.model_lock.__exit__ = Mock(return_value=False)
    app.model = Mock()
    app.prompts = []

    def transcribe(_audio, **params):
        app.prompts.append(params.get("initial_prompt"))
        text = queue.pop(0)
        return ([_seg(text, no_speech_prob=nsp)], types.SimpleNamespace())

    app.model.transcribe = Mock(side_effect=transcribe)
    app.get_transcription_params = Mock(return_value={})
    app.voice_training_window = Mock()
    app.voice_training_window.apply_corrections = Mock(side_effect=lambda t: t)
    app._command_mode_ghost_tap = False
    app._compute_switch_gate_signals = Mock(return_value=Mock())
    app._handle_session_dispatch_outcome = Mock()
    app._filter_dictation_language = Mock(side_effect=lambda t, info, **kw: t)
    app._intent_shadow_observe = Mock()
    app._dictate_preview = None
    app.play_sound = Mock()
    app._vad_reset = Mock()
    app._show_outcome_chip = Mock()

    manager = _LoopManager(mode)
    app._ensure_session_mode_manager = Mock(return_value=manager)
    return app, manager


def _speak(app, duration_s=1.0):
    dictation.DictationApp._handle_command_mode_utterance(
        app, [np.zeros(int(duration_s * 16000), dtype=np.float32)], 16000)


class TestTheLiveIncident:
    """2026-09-15 21:28:53 -> 21:29:01, in order."""

    SEQUENCE = ["This changes a thing or two.", "Fuck, no.",
                "Changes a thing or two. Fuck, no."]

    def test_the_third_decode_is_refused_chipped_and_not_staged(self):
        app, manager = _make_loop_app(self.SEQUENCE)

        _speak(app, 3.0)          # 21:28:53
        _speak(app, 1.8)          # 21:28:56
        _speak(app, 2.7)          # 21:29:01

        assert manager.staged == ["This changes a thing or two.", "Fuck, no."]
        assert manager.dispatch_utterance.call_count == 2
        assert "Changes a thing or two. Fuck, no." not in manager.buffer

        app._show_outcome_chip.assert_called_once_with(
            dictation._CONTEXT_ECHO_CHIP, "warning")

    def test_the_chip_says_what_happened_and_why(self):
        assert dictation._CONTEXT_ECHO_CHIP == "echo of your draft - not staged"

    def test_the_refusal_is_logged_with_a_fixed_reason_token(self, caplog):
        app, _manager = _make_loop_app(self.SEQUENCE)
        with caplog.at_level("INFO", logger="Samsara"):
            for d in (3.0, 1.8, 2.7):
                _speak(app, d)
        assert "dropped reason=context_echo" in caplog.text
        assert "Refused context echo" in caplog.text

    def test_the_short_exclamation_before_it_is_staged_unchanged(self):
        """"Fuck, no." is the utterance a confidence gate would have eaten:
        no_speech_prob 0.646 against the 0.600 threshold. It is staged."""
        assert 0.646 > dictation._NO_SPEECH_THRESHOLD
        app, manager = _make_loop_app(["This changes a thing or two.", "Fuck, no."],
                                      nsp=0.646)
        _speak(app, 3.0)
        _speak(app, 1.8)
        assert manager.staged == ["This changes a thing or two.", "Fuck, no."]

    def test_a_genuine_repeat_after_a_gap_is_still_staged(self):
        """Same words as an echo, but with speech in between, so the earlier
        instance is no longer the end of the prompt."""
        app, manager = _make_loop_app([
            "Oh shit, no way.", "Baby, baby.", "What? What?", "Oh shit, no way.",
        ])
        for _ in range(4):
            _speak(app, 1.4)
        assert manager.staged[-1] == "Oh shit, no way."
        assert manager.dispatch_utterance.call_count == 4
        app._show_outcome_chip.assert_not_called()


class TestOnlyCleanTextEntersTheTail:

    def test_guard_suppressed_text_never_reaches_the_next_prompt(self):
        """The hallucination guard returns before dispatch_utterance, which
        is the tail's only writer -- so what it catches cannot become the
        next utterance's context. Run through the real loop, not asserted
        from the source."""
        app, manager = _make_loop_app([
            "the real first sentence", "Thank you for watching!", "and now the third",
        ])
        _speak(app)
        _speak(app)
        _speak(app)

        assert "Thank you for watching" not in manager.buffer
        assert manager.staged == ["the real first sentence", "and now the third"]
        assert app.prompts[2] is not None
        assert "Thank you for watching" not in app.prompts[2]
        assert "the real first sentence" in app.prompts[2]

    def test_trailing_garbage_is_trimmed_before_it_can_enter_the_tail(self):
        app, manager = _make_loop_app(["ready for " + "_" * 200, "next one"])
        _speak(app)
        _speak(app)
        assert manager.staged[0] == "ready for"
        assert "_" not in app.prompts[1]

    def test_a_refused_echo_never_reaches_the_next_prompt_either(self):
        """The property that closes the loop: the third decode is refused,
        so the fourth utterance is conditioned on a buffer that never saw
        it."""
        app, manager = _make_loop_app([
            "This changes a thing or two.", "Fuck, no.",
            "Changes a thing or two. Fuck, no.", "something else entirely now",
        ])
        for d in (3.0, 1.8, 2.7, 1.5):
            _speak(app, d)
        assert app.prompts[3] == "This changes a thing or two. Fuck, no."
        assert manager.staged[-1] == "something else entirely now"


class TestTheSanitisedTailIsWhatTheDecoderSees:

    def test_the_prompt_handed_to_whisper_is_the_sanitised_tail(self):
        app, manager = _make_loop_app(["first", "second"])
        _speak(app)
        manager.buffer = "yeah I don't know and nd nd nd"
        _speak(app)
        assert app.prompts[1] == "yeah I don't know and"

    def test_an_untruncated_tail_is_passed_through_whole(self):
        """Pins the existing queue-44-era contract: a short buffer keeps its
        first word (test_command_mode_utterance_hallucination.py asserts the
        same string)."""
        app, manager = _make_loop_app(["first", "second"])
        _speak(app)
        manager.buffer = "I went to the"
        _speak(app)
        assert app.prompts[1] == "I went to the"

    def test_a_tail_that_sanitises_to_nothing_sends_no_prompt(self):
        app, manager = _make_loop_app(["first", "second"])
        _speak(app)
        manager.buffer = "nd nd nd nd"
        _speak(app)
        assert app.prompts[1] is None

    def test_the_command_lane_is_untouched(self):
        app, manager = _make_loop_app(["show me the numbers"], mode=SessionMode.COMMAND)
        _speak(app)
        assert app.prompts[0] is None
        assert manager.dispatch_utterance.call_count == 1


class TestControlPhrasesAreNeverRefused:

    def test_bring_back_my_draft_survives_a_perfect_echo_match(self):
        """Four words, exactly the floor, and the phrase the owner reaches
        for when the draft is already in a mess. Refusing it would be this
        fix eating the way out of the problem it exists to fix."""
        app = Mock()
        app._is_dictate_context_echo = dictation.DictationApp._is_dictate_context_echo
        tail = "some earlier prose bring back my draft"
        assert dictation._is_context_echo("bring back my draft", tail) is True
        assert app._is_dictate_context_echo(app, "bring back my draft", tail) is False

    def test_ordinary_prose_is_still_refused_through_the_same_method(self):
        app = Mock()
        assert dictation.DictationApp._is_dictate_context_echo(
            app, "Changes a thing or two. Fuck, no.",
            "This changes a thing or two. Fuck, no.") is True
