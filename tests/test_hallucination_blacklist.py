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
    ])
    def test_known_hallucination_strings_are_detected(self, text):
        assert dictation._is_hallucinated_segments([_seg()], text) is True

    def test_case_insensitive_match(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "UNTERTITEL DER AMARA.ORG-COMMUNITY",
        ) is True

    def test_generic_amara_org_substring_matches(self):
        assert dictation._is_hallucinated_segments(
            [_seg()], "Some other Amara.org credit line nobody listed explicitly",
        ) is True

    def test_phrase_embedded_in_longer_text_still_matches(self):
        """Whisper sometimes prepends/appends the hallucination to a real
        or partially-real utterance -- must still be caught."""
        assert dictation._is_hallucinated_segments(
            [_seg()], "hello there Untertitel der Amara.org-Community",
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
