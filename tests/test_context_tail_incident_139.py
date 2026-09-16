"""Queue 139: prompt artifacts and exhausted decodes never close the loop."""

import types

from samsara import transcript_gates


def _segment(*, logprob=-1.1, compression=1.0):
    return types.SimpleNamespace(
        text="submit", avg_logprob=logprob, compression_ratio=compression,
        no_speech_prob=0.0,
    )


def test_incident_tails_strip_the_artifact_without_losing_submit():
    sanitise = transcript_gates._sanitise_context_tail
    assert sanitise("eva gets hands thing nd submit") == "eva gets hands thing submit"
    assert sanitise("eva gets hands thing nd submit nd submit") == \
        "eva gets hands thing submit"


def test_adjacent_short_token_repeats_are_collapsed_but_separated_words_remain():
    sanitise = transcript_gates._sanitise_context_tail
    assert sanitise("please submit submit now") == "please submit now"
    assert sanitise("submit this then submit that") == "submit this then submit that"


def test_quality_exhausted_marks_an_all_temperature_fallback_unusable_for_context():
    assert transcript_gates._is_quality_exhausted(
        [_segment()], {"log_prob_threshold": -1.0}
    ) is True
