"""972: sustained gain warnings exclude transients; all PCM is synthetic."""
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import failure_reasons as reasons


RATE = 16000


def host(level='full', rate=RATE):
    return SimpleNamespace(model_rate=rate, config={'tts': {'feedback_level': level}},
                           _schedule_ui=lambda fn, *args: fn(*args),
                           listening_indicator=SimpleNamespace(show_outcome=Mock()),
                           _speak_session_notice=Mock())


def speech(seconds, rate=RATE, amplitude=.2):
    return amplitude * np.sin(2 * np.pi * 200 * np.arange(round(seconds * rate)) / rate)


def burst(audio, start, duration, rate=RATE):
    audio[round(start * rate):round((start + duration) * rate)] = 1.
    return audio


def hot(seconds=6, rate=RATE):
    return np.clip(speech(seconds, rate, amplitude=1.5), -1., 1.)


@pytest.mark.parametrize('rate', [8000, 16000, 48000])
@pytest.mark.parametrize('case', ['normal', 'cough', 'fit', 'hot', 'hot_with_silence'])
def test_synthetic_capture_verdicts(rate, case):
    audio = speech(10 if case == 'fit' else 6, rate)
    if case == 'cough':
        burst(audio, 2.1, .15, rate)
    elif case == 'fit':
        for start in (3., 3.5, 4.):
            burst(audio, start, .15, rate)
    elif case == 'hot':
        audio = hot(rate=rate)
    elif case == 'hot_with_silence':
        audio = np.concatenate([np.zeros(8 * rate), hot(3, rate), np.zeros(8 * rate)])
    app = host(rate=rate)
    reasons.post_clipping(app, audio)
    expected = case.startswith('hot')
    assert app.listening_indicator.show_outcome.called is expected
    assert app._speak_session_notice.called is expected


def test_short_fit_is_ignored_even_without_other_voiced_audio():
    audio = np.zeros(10 * RATE)
    for start in (3., 3.5, 4.):
        burst(audio, start, .15)
    assert not reasons.sustained_clipping(audio, RATE)


def test_widespread_but_rare_transients_do_not_warn():
    audio = speech(10)
    for start in (1., 4., 7.):
        burst(audio, start, .02)
    assert not reasons.sustained_clipping(audio, RATE)


@pytest.mark.parametrize('audio', [None, np.array([]), np.zeros(RATE), np.ones(2)])
def test_empty_silent_and_tiny_captures_do_not_warn(audio):
    assert not reasons.sustained_clipping(audio, RATE)


def test_peak_helper_retains_its_original_contract():
    assert reasons.capture_peak(None) == reasons.capture_peak([]) == 0.
    assert reasons.capture_peak(np.array([.2, -1.])) == 1.
    assert reasons.reason_for('clipping', {'peak': 1.}) == ('', '')


def test_screen_cooldown_boundary_and_speech_once_per_app(monkeypatch):
    now = [0.]
    monkeypatch.setattr(reasons, 'monotonic', lambda: now[0])
    app = host()
    audio = hot()
    reasons.post_clipping(app, audio)
    for instant in (1., 599.999):
        now[0] = instant
        reasons.post_delivery_clipping(app, audio, None)
    assert app.listening_indicator.show_outcome.call_count == 1
    now[0] = 600.
    reasons.post_clipping(app, audio)
    assert app.listening_indicator.show_outcome.call_count == 2
    assert app._speak_session_notice.call_count == 1
    label, severity, _ = app.listening_indicator.show_outcome.call_args.args
    assert label == 'Captured\nMic is too loud - turn the mic level down'
    assert severity == 'accent'
    other = host()
    reasons.post_clipping(other, audio)
    other._speak_session_notice.assert_called_once()


def test_brief_notice_does_not_consume_first_full_speech(monkeypatch):
    now = [0.]
    monkeypatch.setattr(reasons, 'monotonic', lambda: now[0])
    app = host('brief')
    reasons.post_clipping(app, hot())
    app._speak_session_notice.assert_not_called()
    app.config['tts']['feedback_level'] = 'full'
    now[0] = 600.
    reasons.post_clipping(app, hot())
    app._speak_session_notice.assert_called_once()


@pytest.mark.parametrize('clipped', [False, True])
def test_streaming_carries_verdict_before_synchronous_final(monkeypatch, clipped):
    monkeypatch.setattr('samsara.tutorial_capture.final_current', lambda _: True)
    app = host()
    session = SimpleNamespace(app=app, cancel_event=SimpleNamespace(is_set=lambda: False),
                              _deliver_final=Mock())
    worker = SimpleNamespace(_session=session)
    def finish(*_):
        assert session._capture_clipping_972 is clipped
        reasons.deliver_stream_final(session, 'Synthetic speech', None, 6., 20)
        return False
    monkeypatch.setattr('samsara.speech_presence.finish_streaming_if_empty', finish)
    audio = hot() if clipped else burst(speech(6), 2.1, .15)
    assert not reasons.finish_streaming_if_empty(worker, audio)
    session._deliver_final.assert_called_once()
    assert app.listening_indicator.show_outcome.called is clipped
    assert not hasattr(session, '_capture_peak_891')


def test_stream_and_batch_share_cooldown(monkeypatch):
    monkeypatch.setattr('samsara.tutorial_capture.final_current', lambda _: True)
    app = host()
    reasons.post_clipping(app, hot())
    session = SimpleNamespace(app=app, cancel_event=SimpleNamespace(is_set=lambda: False),
                              _deliver_final=Mock(), _capture_clipping_972=True)
    reasons.deliver_stream_final(session, 'Synthetic speech', None, 6., 20)
    assert app.listening_indicator.show_outcome.call_count == 1
    assert app._speak_session_notice.call_count == 1


def test_transient_and_failed_delivery_do_not_consume_cooldown():
    app = host()
    reasons.post_clipping(app, burst(speech(6), 2., .15))
    reasons.post_delivery_clipping(app, hot(), False)
    assert not hasattr(app, '_clipping_notice_at_972')
    reasons.post_delivery_clipping(app, hot(), None)
    app.listening_indicator.show_outcome.assert_called_once()
