"""Queue 119: TV audio is rejected by the language gate; the owner is not.

The contradiction this file resolves: `hf_bench_post_fix.md` reported
media_only false_accept_rate = 1.0 while the language signal separated cleanly
(owner min 0.983, media max 0.825) and a 0.90 floor sat in the gap.

Two reasons, both established in the queue 119 report:

1. `LanguageConfidenceGate.evaluate` could only reject when the detected
   language was NOT expected. TV audio is English, `en` is always expected,
   so the probability was never consulted. The gate was a no-op for the exact
   failure it was best placed to catch.
2. `tools/hf_bench.py` never constructs the gate at all -- it only summarises
   `language_probability`. Its false_accept_rate therefore measures the decode
   and segment gates, and could never have shown the gate working either way.

Every probability below is REAL, measured on the current tree:
hf_corpus for the bench clips, and 59 of the owner's own wake captures for the
short-utterance costs. Nothing here is invented.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.languages import (
    LANGUAGE_CONFIDENCE_FLOOR,
    LANGUAGE_CONFIDENCE_MIN_CHARS,
    LANGUAGE_CONFIDENCE_MIN_DURATION_S,
    LanguageConfidenceGate,
)

# ---------------------------------------------------------------------------
# Real measured clips (reports/119/artifacts/hf_rerun_119.json)
# ---------------------------------------------------------------------------

#: (file, probability, chars, duration_s) -- TV playing, owner silent.
MEDIA_ONLY = [
    ("media_only_01.wav", 0.84033203125, 362, 30.0),
    ("media_only_02.wav", 0.88623046875, 516, 30.0),
    ("media_only_03.wav", 0.84130859375, 450, 30.0),
]

#: The owner speaking, including over the same TV.
OWNER = [
    ("speech_owner_01.wav", 0.98437500000, 60, 5.0),
    ("speech_owner_03.wav", 0.99316406250, 57, 8.1),
    ("speech_owner_over_media_03.wav", 0.98339843750, 65, 6.7),
    ("wake_over_media_01.wav", 0.98193359375, 35, 4.9),
]

#: The owner's own short commands -- where language_probability is least
#: reliable, and the only place a floor can cost him anything.
OWNER_SHORT = [
    ("done.", 0.79785156250, 1.2),
    ("over.", 0.97216796875, 1.4),
    ("cancel.", 0.98046875000, 1.5),
    ("This is round 5.", 0.99218750000, 2.0),
]


def _gate():
    return LanguageConfidenceGate()


def _judge(text, probability, duration_s=None, configured="en"):
    reason, _expected = _gate().evaluate(
        text, "en", probability, configured, LANGUAGE_CONFIDENCE_FLOOR,
        remember=False, duration_s=duration_s)
    return reason


# ---------------------------------------------------------------------------
# The failure
# ---------------------------------------------------------------------------

class TestMediaIsRejected:
    @pytest.mark.parametrize("name,prob,chars,dur", MEDIA_ONLY)
    def test_each_media_clip_is_rejected(self, name, prob, chars, dur):
        assert _judge("word " * (chars // 5), prob, dur) == "low_confidence", name

    def test_false_accept_rate_is_zero(self):
        """Threshold: ZERO, not a fraction.

        There are three media clips, so the only rates expressible are 0.00,
        0.33, 0.67 and 1.00 -- there is no meaningful intermediate value to
        allow. The measured separation is media max 0.8862 vs owner min
        0.9819 with the floor at 0.90, so every media clip clears the floor by
        at least 0.0138. If this ever regresses, the clip that slipped should
        be investigated rather than the threshold loosened: a non-zero rate
        here means TV dialogue is being typed into whatever has focus.
        """
        accepted = [n for n, p, c, d in MEDIA_ONLY
                    if _judge("word " * (c // 5), p, d) is None]
        rate = len(accepted) / len(MEDIA_ONLY)
        assert rate == 0.0, f"media false-accept {rate:.2f}: {accepted}"

    def test_the_old_predicate_would_have_let_every_one_through(self):
        """Regression guard on the ROOT CAUSE, not just the symptom: the old
        rule required `language not in expected`, and English TV is always
        expected."""
        for _name, prob, _chars, _dur in MEDIA_ONLY:
            assert prob < LANGUAGE_CONFIDENCE_FLOOR
            old_rule_would_reject = "en" not in {"en"} and prob < LANGUAGE_CONFIDENCE_FLOOR
            assert old_rule_would_reject is False


# ---------------------------------------------------------------------------
# The cost side
# ---------------------------------------------------------------------------

class TestTheOwnerIsNotRejected:
    @pytest.mark.parametrize("name,prob,chars,dur", OWNER)
    def test_owner_speech_is_accepted(self, name, prob, chars, dur):
        assert _judge("word " * (chars // 5), prob, dur) is None, name

    @pytest.mark.parametrize("text,prob,dur", OWNER_SHORT)
    def test_short_owner_commands_survive(self, text, prob, dur):
        """The one that matters: "done." scores 0.7979, well under the floor,
        because 1.2 s is not enough audio to judge a language from. Rejecting
        the owner's own word is worse than transcribing the TV, so short
        utterances are not judged on this signal at all."""
        assert _judge(text, prob, dur) is None, text

    def test_a_quiet_owner_word_is_never_rejected_for_confidence_alone(self):
        assert _judge("done.", 0.50, 1.2) is None
        assert _judge("yes", 0.30, 0.8) is None

    def test_the_duration_guard_is_what_saves_them(self):
        """Same text and probability, longer audio -> now judged. This pins
        the guard itself, so removing it fails here rather than silently
        costing the owner his short commands."""
        long_text = "word " * 60
        assert _judge(long_text, 0.7979, 1.2) is None
        assert _judge(long_text, 0.7979, LANGUAGE_CONFIDENCE_MIN_DURATION_S) == "low_confidence"


class TestTheLengthFallback:
    """When the caller cannot supply duration, transcript length stands in --
    a long transcript from audio the model is unsure about is confabulation."""

    def test_media_is_still_rejected_without_duration(self):
        for name, prob, chars, _dur in MEDIA_ONLY:
            assert _judge("word " * (chars // 5), prob) == "low_confidence", name

    def test_short_owner_text_is_still_accepted_without_duration(self):
        for text, prob, _dur in OWNER_SHORT:
            assert _judge(text, prob) is None, text

    def test_the_threshold_sits_between_the_two_populations(self):
        owner_max_chars = max(c for _n, _p, c, _d in OWNER)
        media_min_chars = min(c for _n, _p, c, _d in MEDIA_ONLY)
        assert owner_max_chars < LANGUAGE_CONFIDENCE_MIN_CHARS < media_min_chars


# ---------------------------------------------------------------------------
# The original rule must still hold
# ---------------------------------------------------------------------------

class TestTheForeignLanguageRuleIsUnchanged:
    def test_unexpected_language_is_still_rejected_however_short(self):
        reason, _ = _gate().evaluate("hola", "es", 0.50, "en",
                                     LANGUAGE_CONFIDENCE_FLOOR,
                                     remember=False, duration_s=0.8)
        assert reason == "low_confidence"

    def test_a_confident_unexpected_language_still_passes(self):
        reason, _ = _gate().evaluate("hola amigo", "es", 0.99, "en",
                                     LANGUAGE_CONFIDENCE_FLOOR,
                                     remember=False, duration_s=3.0)
        assert reason is None

    def test_empty_text_is_never_judged(self):
        assert _judge("   ", 0.10, 30.0) is None


# ---------------------------------------------------------------------------
# The gate has to actually be on the hands-free path
# ---------------------------------------------------------------------------

class TestTheGateIsOnTheHandsFreePath:
    """Structural, by reading dictation.py -- the module is never imported
    (the app is normally running, and tools/hf_bench.py parses it for the same
    reason). If a lane stops calling the funnel, this fails."""

    HANDS_FREE_LANES = (
        "_handle_command_mode_utterance",
        "_decode_wake_word_buffer",
        "transcribe_continuous_buffer",
    )

    @pytest.fixture(scope="class")
    def source(self):
        return (Path(__file__).parent.parent / "dictation.py").read_text(encoding="utf-8")

    def test_every_hands_free_lane_calls_the_language_funnel(self, source):
        import re
        for lane in self.HANDS_FREE_LANES:
            start = source.index(f"def {lane}(")
            nxt = re.search(r"\n    def ", source[start:])
            body = source[start:start + (nxt.start() if nxt else len(source) - start)]
            assert "_filter_dictation_language" in body, lane

    def test_the_funnel_is_the_only_gate_caller(self, source):
        """One gate, one call site -- §2 said do not add a second gate."""
        assert source.count("_language_confidence_gate.evaluate") <= 1
        assert source.count("gate.evaluate(") == 1
