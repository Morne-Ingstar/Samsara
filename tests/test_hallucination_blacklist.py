"""Tests for the multilingual hallucination-string blacklist (Signature E)
in dictation._is_hallucinated_segments.

Pure-function test against a minimal duck-typed segment list -- no audio,
no model load, no full Samsara boot (same import pattern as
test_transcription_params.py: `import dictation` directly).
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dictation


def _seg(compression_ratio=1.0, no_speech_prob=0.0):
    return types.SimpleNamespace(
        compression_ratio=compression_ratio, no_speech_prob=no_speech_prob,
    )


class TestHallucinationStringBlacklist:
    @pytest.mark.parametrize("text", [
        "Untertitel der Amara.org-Community",
        "Sous-titrage ST' 501",
        "ご視聴ありがとうございました",
        "字幕由Amara.org社区提供",
        "Дякую за перегляд",
        "Gracias por ver el video",
        "Thank you for watching!",
        "Thanks for watching",
    ])
    def test_known_hallucination_strings_are_detected(self, text):
        assert dictation._is_hallucinated_segments([_seg()], text) is True

    def test_english_thank_you_for_watching_regression(self):
        """The exact production incident (2026-07-10): an 11.7s blank
        hotkey hold delivered this verbatim. Every non-English variant of
        this staple was already blacklisted -- the English original was
        the gap."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "Thank you for watching!",
        ) is True

    def test_english_thanks_for_watching_variant(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "Thanks for watching",
        ) is True

    def test_case_insensitive_match(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "UNTERTITEL DER AMARA.ORG-COMMUNITY",
        ) is True

    def test_bare_blacklist_phrase_dominates_and_fires(self):
        """100% dominance -- the whole (stripped) transcript IS the
        blacklisted phrase. The clearest possible case."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "Untertitel der Amara.org-Community",
        ) is True

    def test_bare_amara_org_fires(self):
        """The bare 'amara.org' entry is the worst case for false-positive
        risk (see test_generic_amara_org_mention_is_not_flagged below) --
        but on its own, with nothing else in the transcript, it's 100%
        dominant and must still fire."""
        assert dictation._is_hallucinated_segments([_seg()], "amara.org") is True

    def test_generic_amara_org_mention_is_not_flagged(self):
        """DOMINANCE, NOT PRESENCE (regression lock): a legitimate
        utterance that merely MENTIONS amara.org must not have its entire
        transcription discarded -- the coverage ratio here is well under
        the 0.80 threshold. This is the exact scenario the dominance-ratio
        fix exists for; the old substring-presence check used to flag this
        (see git history) -- that was the bug, not this expectation."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "Some other Amara.org credit line nobody listed explicitly",
        ) is False

    def test_task_example_mention_is_not_flagged(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "I was reading about the amara.org community",
        ) is False

    def test_phrase_with_substantial_real_speech_prefix_falls_through(self):
        """Whisper sometimes prepends/appends the hallucination to a real
        or partially-real utterance. Superseded expectation (dominance-
        ratio fix): "hello there" is enough real speech ahead of the
        phrase to drop coverage to ~74%, under the 0.80 threshold -- so
        this must now fall through rather than discard the whole
        (partially real) transcription. Mid-string scrubbing was
        considered and rejected (would corrupt the legitimate "hello
        there" prefix); gate-or-pass on dominance is the only safe move."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "hello there Untertitel der Amara.org-Community",
        ) is False

    def test_phrase_with_thin_noise_still_dominant_still_fires(self):
        """A couple of filler characters around the phrase keep coverage
        at ~90%, still over the 0.80 threshold -- still fires. Contrast
        with test_phrase_with_substantial_real_speech_prefix_falls_through
        above, where a real 2-word prefix pushes coverage under 0.80."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "um, Untertitel der Amara.org-Community",
        ) is True

    def test_real_speech_is_not_flagged(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "please schedule the meeting for tomorrow afternoon",
        ) is False

    def test_existing_repetition_signatures_still_work(self):
        """Signature E is additive -- the existing click-click-style
        repetition detection must be untouched."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "click click click click",
        ) is True

    def test_existing_compression_ratio_signature_still_works(self):
        assert dictation._is_hallucinated_segments(
            [_seg(compression_ratio=5.0)], "some repeated repeated repeated text",
        ) is True
