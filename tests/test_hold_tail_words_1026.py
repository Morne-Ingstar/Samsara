"""1026: deterministic decoder outputs through real hold-final callers.

PCM fixtures exercise acoustic policy, not native recognition accuracy. No
owner audio, model loads, network, desktop bootstrap or external files.
"""
import logging
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import decode_recovery
from samsara.hallucination_filter import transcribe_without_outro
from samsara.hold_final_policy import FinalAudit, current_audit, preserve_preview_tail
from samsara.transcript_gates import _apply_segment_quality_gates
from tests.test_silent_hold_387 import make_worker

RATE = 16000
PARAMS = dict(log_prob_threshold=-1., no_speech_threshold=.6)


def segment(text, start=0., end=1., **changes):
    return NS(**(dict(text=text, start=start, end=end, avg_logprob=-.2,
                     no_speech_prob=.1, compression_ratio=1.1, temperature=0.) | changes))


def audio(level=.03):
    t = np.arange(RATE) / RATE
    # Amplitude-modulated tone plus 250 ms silence; deterministic energy only.
    voiced = level * np.sin(2 * np.pi * 190 * t) * (.7 + .3 * np.sin(2 * np.pi * 4 * t))
    return np.r_[voiced, voiced, np.zeros(RATE // 4)].astype(np.float32)


@pytest.mark.parametrize('level', [.03, .002])
@pytest.mark.parametrize('no_speech', [.1, .95])
@pytest.mark.parametrize('compression', [1.1, None])
def test_batch_low_confidence_made_up_tail_survives_with_energy(level, no_speech, compression):
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Include the flibberquax.', 1., 2., avg_logprob=-1.8,
                   no_speech_prob=no_speech, temperature=.2, compression_ratio=compression)
    text, low = _apply_segment_quality_gates([prefix, tail], PARAMS, 2.25, audio=audio(level))
    assert text == prefix.text + tail.text
    assert low is True


def test_batch_missing_pcm_does_not_authorize_score_only_tail_loss():
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Include the flibberquax.', 1., 2., avg_logprob=-1.8)
    assert _apply_segment_quality_gates([prefix, tail], PARAMS, 2.25) == (
        prefix.text + tail.text, True)


def test_batch_silent_low_confidence_tail_still_drops():
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Uncertain trailing output.', 1., 2., avg_logprob=-1.8)
    pcm = audio()
    pcm[RATE:] = 0
    assert _apply_segment_quality_gates([prefix, tail], PARAMS, 2.25, audio=pcm) == (prefix.text, False)


def test_real_streaming_final_keeps_mixed_confidence_tail_and_counts_segments(make_worker, caplog):
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Include the flibberquax.', 1., 2., avg_logprob=-1.8, temperature=.2)
    worker, session = make_worker([prefix, tail])
    session._stop_capture.return_value = audio()
    with caplog.at_level(logging.INFO):
        worker._final_pass()
    assert session.on_final.call_args.args == (prefix.text + tail.text,)
    lines = [r.getMessage() for r in caplog.records if '[HOLD_FINAL]' in r.getMessage()]
    assert len(lines) == 1
    assert 'segments_decoded=2 segments_dropped=0' in lines[0]
    assert f'chars_delivered={len(prefix.text + tail.text)}' in lines[0]
    assert 'flibberquax' not in lines[0]


def test_real_batch_final_passes_pcm_to_quality_gate(make_worker, caplog):
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Include the flibberquax.', 1., 2., avg_logprob=-1.8)
    _, session = make_worker([prefix, tail])
    session.app._filter_dictation_language = lambda text, info: True
    with caplog.at_level(logging.INFO):
        result = decode_recovery.decode_hold(session.app, audio(), PARAMS, 2.25)
    assert result.text == prefix.text + tail.text
    assert result.low_confidence is True
    assert len([r for r in caplog.records if '[HOLD_FINAL]' in r.getMessage()]) == 1


def test_shorter_final_appends_guarded_preview_suffix_without_replacing_prefix(make_worker, caplog):
    final = segment('Keep the complete project notes.')
    worker, session = make_worker([final])
    session._last_partial = 'keep the complete project notes including the flibberquax.'
    session._stop_capture.return_value = audio()
    with caplog.at_level(logging.INFO):
        worker._final_pass()
    expected = 'Keep the complete project notes including the flibberquax.'
    assert session.on_final.call_args.args == (expected,)
    assert session.app.model.transcribe.call_count == 1
    line = next(r.getMessage() for r in caplog.records if '[HOLD_FINAL]' in r.getMessage())
    assert 'preview_tail_words=3' in line
    assert f'chars_delivered={len(expected)}' in line


@pytest.mark.parametrize('preview', [
    'Keep a different project notes including the flibberquax.',
    'Keep the complete project.',
    'Keep the complete project notes. Thanks for watching.',
    'Keep the complete project notes. loop loop loop loop',
    'Keep the complete project notes. _____',
])
def test_conflicting_shorter_or_hallucinated_preview_never_replaces_final(preview):
    final = 'Keep the complete project notes.'
    assert preserve_preview_tail(NS(_last_partial=preview), final) == final


def test_removed_outro_cannot_be_resurrected_from_preview(make_worker, caplog):
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Subscribe.', 1., 2.)
    worker, session = make_worker([prefix, tail])
    session._last_partial = prefix.text + tail.text
    pcm = audio()
    pcm[RATE:] = 1.e-5
    session._stop_capture.return_value = pcm
    with caplog.at_level(logging.INFO):
        worker._final_pass()
    assert session.on_final.call_args.args == (prefix.text,)
    line = next(r.getMessage() for r in caplog.records if '[HOLD_FINAL]' in r.getMessage())
    assert 'segments_decoded=2 segments_dropped=1' in line
    assert "'near_silent_outro': 1" in line


def test_quiet_spoken_outro_is_not_dropped_for_confidence_alone(make_worker):
    prefix = segment('Keep the complete project notes.')
    tail = segment(' Subscribe.', 1., 2., avg_logprob=-1.8, no_speech_prob=.7)
    worker, session = make_worker([prefix, tail])
    session._stop_capture.return_value = audio(.002)
    worker._final_pass()
    assert session.on_final.call_args.args == (prefix.text + tail.text,)


def test_fallback_exhausted_still_rejects_and_uses_only_accepted_preview(make_worker, caplog):
    failed = segment('Untrusted decoder output.', avg_logprob=-1.27, temperature=.2)
    worker, session = make_worker([failed])
    session._last_partial = 'Accepted preview words.'
    session._stop_capture.return_value = audio()
    with caplog.at_level(logging.INFO):
        worker._final_pass()
    assert session.on_final.call_args.args == (session._last_partial,)
    assert session.app.model.transcribe.call_count == 1
    line = next(r.getMessage() for r in caplog.records if '[HOLD_FINAL]' in r.getMessage())
    assert "'fallback_exhausted': 1" in line
    assert failed.text not in line


def test_audit_isolated_between_recordings(make_worker):
    worker, session = make_worker([segment('Keep the complete project notes.')])
    session._stop_capture.return_value = audio()
    worker._final_pass()
    first = session._hold_final_audit
    worker._final_pass()
    assert session._hold_final_audit is not first
    assert first.decoded == session._hold_final_audit.decoded == 1
    assert current_audit.get() is None


@pytest.mark.parametrize('enabled', [True, False])
def test_lazy_final_filter_records_only_consumed_segments(enabled):
    audit = FinalAudit()
    segments = [segment('Keep the complete project notes.'), segment(' A final flibberquax.', 1., 2.)]
    model = NS(transcribe=lambda *args, **kwargs: (iter(segments), None))
    token = current_audit.set(audit)
    try:
        result, _ = transcribe_without_outro(model, audio(), PARAMS, enabled=enabled)
        assert audit.decoded == 0
        assert list(result) == segments
        assert audit.decoded == 2 and not audit.dropped
    finally:
        current_audit.reset(token)
