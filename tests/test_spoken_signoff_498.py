"""498: requested short speech survives; silent/weak sign-offs do not."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.hallucination_filter import SignoffContext, is_signoff_hallucination, signoff_failure_reason
from tests.test_phantom_signoff_492 import _batch_boundary
from tests.test_silent_hold_387 import make_worker, _segment


def context(*, held, duration=.8, no_speech=.01, logprob=-.05, end=.8):
    return SignoffContext([dict(no_speech_prob=no_speech, avg_logprob=logprob,
                               start=0., end=end)], duration, user_held_key=held)


@pytest.mark.parametrize('text', ['Thank you', 'Thanks.', 'Bye'])
def test_confident_short_hold_kept_same_hands_free_dropped(text):
    assert is_signoff_hallucination(text, context(held=True)) == (False, 'audio_not_corroborated')
    assert is_signoff_hallucination(text, context(held=False)) == (True, 'audio<1.5s')
    assert is_signoff_hallucination(text, SignoffContext([{}], .8)) == (True, 'audio<1.5s')


@pytest.mark.parametrize('changes,reason', [
    ({'no_speech': .7}, 'all_no_speech>=0.5'),
    ({'logprob': -1.0}, 'avg_logprob<=-1.0'),
    ({'duration': 3., 'end': .8}, 'tail>speech'),
])
def test_hold_keeps_existing_silence_confidence_and_tail_rules(changes, reason):
    assert is_signoff_hallucination('Thanks.', context(held=True, **changes)) == (True, reason)


def test_failure_reason_propagates_capture_intent_and_toggle():
    segments = context(held=True).segments
    assert signoff_failure_reason('Thank you', segments, .8, {}, user_held_key=True) is None
    assert signoff_failure_reason('Thank you', segments, .8, {}) == 'signoff:audio<1.5s'
    assert signoff_failure_reason('Thanks.', segments, .8, {'dictation': {'signoff_filter': False}}) is None


@pytest.mark.parametrize('lane,kept', [('hold', True), ('hands-free', False)])
def test_actual_batch_caller_uses_its_capture_lane(lane, kept):
    app = SimpleNamespace(config={}, _log_cmd_utt_dropped=Mock(), _show_outcome_chip=Mock())
    segment = _segment(text='Thanks.', no_speech_prob=.01, avg_logprob=-.05, start=0., end=.8)
    assert bool(_batch_boundary(lane)(app, 'Thanks.', [segment], .8)) is kept


@pytest.mark.parametrize('no_speech,kept', [(.01, True), (.7, False)])
def test_actual_streaming_hold_short_final(make_worker, no_speech, kept):
    worker, session = make_worker([_segment(text='Thank you', no_speech_prob=no_speech,
                                          avg_logprob=-.05, start=0., end=.8)])
    session._stop_capture.return_value = np.zeros(12800, dtype=np.float32)
    worker._final_pass()
    assert bool(session.on_final.call_args.args[0]) is kept
    if not kept:
        session.app.process_transcription.assert_not_called()
