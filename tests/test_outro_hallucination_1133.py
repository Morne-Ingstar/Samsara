"""1133: invented video-outro tails are dropped only when unsupported by audio."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from tests.test_outro_hallucination_1021 import RATE, decode, seg
from tests.test_silent_hold_387 import make_worker  # noqa: F401

OWNER = "I'll see you next time, and thanks for watching the video!"


def audio(tail):
    # One second of audible speech then two seconds of the given tail level.
    rng = np.random.default_rng(1133)
    noise = (rng.standard_normal(2 * RATE) * tail).astype(np.float32)
    return np.r_[np.full(RATE, .03), noise].astype(np.float32)


@pytest.mark.parametrize('text', [OWNER, 'Please subscribe.', 'See you in the next video!',
                                  'Thanks for watching the video.'])
def test_invented_tail_over_idle_mic_noise_with_low_confidence_is_removed(text):
    real = seg('Send the report to the team.')
    outro = seg(' ' + text, 1., 2.8, avg_logprob=-1.4, no_speech_prob=.6)
    kept, _ = decode([real, outro], audio(.0002))
    assert kept == [real]


def test_invented_tail_over_noise_but_confident_decode_is_kept():
    real = seg('Send the report.')
    outro = seg(' Thanks for watching.', 1., 2.8)  # confident, so not provably invented
    assert decode([real, outro], audio(.0002))[0] == [real, outro]


def test_quiet_but_audible_outro_is_kept_even_when_low_confidence():
    real = seg('Send the report.')
    outro = seg(' Thanks for watching.', 1., 2.8, avg_logprob=-1.8, no_speech_prob=.7)
    assert decode([real, outro], audio(.003))[0] == [real, outro]


def test_deliberate_loud_phrase_is_kept():
    only = seg('Thanks for watching the video.', 0., 1., avg_logprob=-1.5)
    assert decode([only], np.full(RATE, .05, np.float32))[0] == [only]


@pytest.mark.parametrize('text', ['We say thanks for watching at the end of every episode.',
                                  'Thanks for watching and then I left.', 'and thanks for watching'])
def test_mid_sentence_and_extra_words_are_kept(text):
    s = seg(text, 1., 2.8, avg_logprob=-1.5, no_speech_prob=.7)
    assert decode([seg('Intro.'), s], audio(.0001))[0] == [seg('Intro.'), s]


def test_middle_segment_is_never_removed():
    segs = [seg('Intro.'), seg(' Thanks for watching.', 1., 2., avg_logprob=-1.5, no_speech_prob=.7),
            seg(' More talk.', 2., 3.)]
    assert decode(segs, audio(.0001))[0] == segs


def test_silence_only_hold_with_invented_phrase_delivers_nothing(make_worker):
    from tests.test_hold_tail_words_1026 import segment
    worker, session = make_worker([segment(OWNER, 0., 2., avg_logprob=-1.6, no_speech_prob=.8)])
    session._stop_capture.return_value = np.zeros(2 * RATE, np.float32)
    worker._final_pass()
    args = session.on_final.call_args.args
    assert not (args and args[0])
