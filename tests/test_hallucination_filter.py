"""Task 281's pure final-layer sign-off guard; no audio or app import."""

from samsara.hallucination_filter import (
    SIGNOFF_PHRASES,
    SignoffContext,
    is_signoff_hallucination,
)
from samsara.config_defaults import cfg_get


def _context(*, no_speech=0.1, logprob=-0.2, duration=2.0):
    return SignoffContext(
        [{"no_speech_prob": no_speech, "avg_logprob": logprob,
          "start": 0.0, "end": 1.0}],
        audio_duration_s=duration,
    )


def test_each_curated_phrase_drops_when_audio_is_weak():
    for phrase in SIGNOFF_PHRASES:
        dropped, reason = is_signoff_hallucination(phrase, _context(no_speech=0.6))
        assert dropped, (phrase, reason)
        assert reason == "all_no_speech>=0.5"


def test_each_phrase_is_kept_with_clean_long_audio():
    for phrase in SIGNOFF_PHRASES:
        dropped, reason = is_signoff_hallucination(phrase, _context())
        assert not dropped, (phrase, reason)
        assert reason == "audio_not_corroborated"


def test_extra_words_keep_a_signoff_phrase_as_real_speech():
    assert is_signoff_hallucination(
        "Thanks for watching the demo", _context(no_speech=0.9),
    ) == (False, "not_signoff")


def test_real_sentence_containing_bye_is_kept():
    assert is_signoff_hallucination(
        "I said bye before the meeting started", _context(no_speech=0.9),
    ) == (False, "not_signoff")


def test_phrase_concatenation_is_a_whole_signoff():
    assert is_signoff_hallucination(
        "Thank you. Bye.", _context(logprob=-1.1),
    ) == (True, "avg_logprob<=-1.0")


def test_short_audio_is_a_corrobator_even_with_clean_segment_scores():
    assert is_signoff_hallucination(
        "Subscribe", _context(duration=1.49),
    ) == (True, "audio<1.5s")


def test_long_post_speech_hold_is_a_corrobator():
    context = SignoffContext(
        [{"no_speech_prob": 0.1, "avg_logprob": -0.2, "start": 0.0, "end": 1.0}],
        audio_duration_s=2.1,
    )
    assert is_signoff_hallucination("Goodbye", context) == (True, "tail>speech")


def test_missing_segment_telemetry_fails_open():
    assert is_signoff_hallucination(
        "Thank you", SignoffContext([{}], audio_duration_s=3.0),
    ) == (False, "audio_not_corroborated")


def test_filter_off_drops_nothing():
    config = {"dictation": {"signoff_filter": False}}
    context = SignoffContext(
        _context(no_speech=0.9).segments, audio_duration_s=2.0,
        enabled=cfg_get(config, "dictation.signoff_filter"),
    )
    dropped, reason = is_signoff_hallucination("Thank you", context)
    assert cfg_get(config, "dictation.signoff_filter") is False
    assert (dropped, reason) == (False, "disabled")


def test_filter_default_is_on():
    assert cfg_get({}, "dictation.signoff_filter") is True


# Measured `nd_prompt_contamination_wavs` fixture signals, transcribed with
# the owner's medium/CUDA lane parameters.  No test decodes a WAV.
def test_measured_owner_signoff_fixture_drops():
    context = SignoffContext(
        [{"no_speech_prob": 0.712, "avg_logprob": -0.925,
          "start": 0.0, "end": 2.0}],
        audio_duration_s=0.740,
    )
    assert is_signoff_hallucination("Thanks for watching!", context) == (
        True, "all_no_speech>=0.5",
    )


def test_measured_owner_mixed_output_fixture_is_kept():
    context = SignoffContext(
        [{"no_speech_prob": 0.553, "avg_logprob": -0.756,
          "start": 0.0, "end": 1.02}],
        audio_duration_s=0.820,
    )
    assert is_signoff_hallucination(
        "and I'll see you in the next one. Bye.", context,
    ) == (False, "not_signoff")
