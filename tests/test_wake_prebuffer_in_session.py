"""Queue 44: an open wake session keeps the pre-onset rewind (clamped at
confirmation + post_wake_guard_ms); the config key off restores the old
discard exactly.

Uses the real WakeConsumer and the real dictation.py policy methods compiled
from source by tests.test_wake_session_policy (dictation is never imported).

Timeline used throughout (fake clock, 100 ms ring frames, t = end of block):
  98.6 .. 100.0  value 1000   before the wake confirmation (pre-wake audio)
  100.0          _start_wake_session -> confirmed_at; guard ends 100.15
  100.1 .. 100.6 value 1500   room tone after confirmation, not speech
  100.7 .. 100.8 value 3000   soft word head Silero does not flag (late onset)
  100.9 .. 101.0 value 8000   Silero onset and word body
"""

import logging

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wake_module
from samsara.audio_engine.frame import FRAME_SIZE, PREBUFFER_FRAMES, SAMPLE_RATE
from samsara.audio_engine.wake_consumer import retain_prebuffer_in_session
from tests.test_wake_session_policy import dictation, rig  # noqa: F401 (rig is a fixture)

PRE_WAKE, ROOM, SOFT_HEAD, WORD = 1000, 1500, 3000, 8000
KEY_OFF = {'wake_word_config': {'session': {'retain_prebuffer_in_session': False}}}


def _level(value):
    return np.float32(value / 32767.0)


def _late_onset(r, *, session=True):
    for i in range(PREBUFFER_FRAMES):
        r.feed(98.6 + i * .1, value=PRE_WAKE, speech=False, process=False)
    r.reader.snap_to_head()
    r.clock.now = 100.0
    if session:
        r.app._start_wake_session(mode='stage_send', send_word='over')
    for end in (100.1, 100.2, 100.3, 100.4, 100.5, 100.6):
        r.feed(end, value=ROOM, speech=False)
    for end in (100.7, 100.8):
        r.feed(end, value=SOFT_HEAD, speech=False)
    r.feed(100.9, value=WORD, speech=True)


def _captured(r):
    return np.concatenate(r.consumer._utterance_frames)


def _policy_events(r):
    return [fields for name, fields in r.events if name == 'wake.capture_policy']


def test_default_is_retain_and_key_reads_both_config_shapes():
    assert retain_prebuffer_in_session({}) is True
    assert retain_prebuffer_in_session(KEY_OFF) is False
    assert retain_prebuffer_in_session({'wake_word': {'session': {'retain_prebuffer_in_session': False}}}) is False
    # Canonical wake_word.session overrides the legacy block, as wake_session_policy does.
    assert retain_prebuffer_in_session({
        'wake_word_config': {'session': {'retain_prebuffer_in_session': False}},
        'wake_word': {'session': {'retain_prebuffer_in_session': True}},
    }) is True
    assert retain_prebuffer_in_session({'wake_word': 'jarvis'}) is True
    # A non-bool value cannot silently flip behaviour.
    assert retain_prebuffer_in_session(
        {'wake_word_config': {'session': {'retain_prebuffer_in_session': 'no'}}}) is True


def test_late_onset_in_open_session_retains_audio_ahead_of_onset(rig):
    r = rig()
    _late_onset(r)
    assert wake_module.wake_capture_session_open(r.app)
    captured = _captured(r)
    # Soft word head that Silero missed is kept.
    assert np.count_nonzero(captured == _level(SOFT_HEAD)) == 2 * FRAME_SIZE
    assert np.count_nonzero(captured == _level(WORD)) == FRAME_SIZE
    # Room tone from confirmation + 150 ms onward: 50 ms of the 100.2 block + 100.3..100.6.
    assert np.count_nonzero(captured == _level(ROOM)) == FRAME_SIZE // 2 + 4 * FRAME_SIZE
    # Nothing from before the wake confirmation or inside the guard.
    assert not np.any(captured == _level(PRE_WAKE))
    assert len(captured) == 750 * SAMPLE_RATE // 1000
    # Buffer is in capture order: room, soft head, word.
    first_soft = int(np.argmax(captured == _level(SOFT_HEAD)))
    first_word = int(np.argmax(captured == _level(WORD)))
    assert np.all(captured[:first_soft] == _level(ROOM))
    assert first_soft < first_word
    [event] = _policy_events(r)
    assert event['policy'] == 'discard' and event['retain_in_session'] is True
    assert event['post_wake_guard_ms'] == 150
    # 15-frame rewind = 1500 ms; 750 ms retained, 750 ms fell before confirmation + guard.
    assert event['discarded_ms'] == pytest.approx(750, abs=.1)


def test_rewind_never_reaches_before_confirmation_plus_guard(rig):
    r = rig()
    for i in range(PREBUFFER_FRAMES):
        r.feed(98.6 + i * .1, value=PRE_WAKE, speech=False, process=False)
    r.reader.snap_to_head()
    r.clock.now = 100.0
    r.app._start_wake_session(mode='stage_send', send_word='over')
    r.feed(100.1, value=WORD, speech=True)   # wholly inside the guard: dropped
    assert r.consumer._utterance_frames == []
    r.feed(100.2, value=WORD, speech=True)   # onset; only the last 50 ms is admissible
    captured = _captured(r)
    assert len(captured) == FRAME_SIZE // 2
    assert np.all(captured == _level(WORD))


def test_same_capture_while_asleep_is_unchanged_by_the_key(rig):
    """Asleep there is no post-wake admission: the full rewind (including audio
    the user said before any wake) was always kept, and the key must not touch it."""
    captures = []
    for config in ({}, KEY_OFF):
        r = rig(config)
        _late_onset(r, session=False)
        assert r.app.app_state == 'asleep'
        assert not wake_module.wake_capture_session_open(r.app)
        captures.append(_captured(r))
        assert _policy_events(r) == []
    on, off = captures
    np.testing.assert_array_equal(on, off)
    assert len(on) == PREBUFFER_FRAMES * FRAME_SIZE
    assert np.any(on == _level(PRE_WAKE))


def test_key_off_is_the_old_discard_exactly(rig, monkeypatch, caplog):
    r = rig(KEY_OFF)
    rewind_calls = []
    real_rewind = type(r.reader).rewind
    monkeypatch.setattr(type(r.reader), 'rewind',
                        lambda self, n: (rewind_calls.append(n), real_rewind(self, n)))
    caplog.set_level(logging.DEBUG, logger=wake_module.logger.name)
    _late_onset(r)
    captured = _captured(r)
    # Old behaviour: only the onset frame; the soft head is lost.
    assert rewind_calls == []
    assert len(r.consumer._utterance_frames) == 1
    assert np.all(captured == _level(WORD)) and len(captured) == FRAME_SIZE
    [event] = _policy_events(r)
    assert event['retain_in_session'] is False
    # Old formula: full rewind + guard-trimmed samples (100.1 block + 50 ms of 100.2).
    assert event['discarded_ms'] == pytest.approx(1650, abs=.1)
    [log] = [rec for rec in caplog.records if '[WAKE-POLICY]' in rec.message]
    assert 'discarded_ms=1650.000' in log.message and 'retain_in_session=False' in log.message


def _decode_as_whisper_would(r, buffer, monkeypatch):
    """Run the real _decode_wake_word_buffer. The model is a stand-in, not
    Whisper: it hears "over" only when the word's head reached it and "ver"
    when the buffer starts at the late onset -- the first-syllable loss the
    offline replay measured on the owner's voice
    (perf_artifacts/wake_prebuffer_onsets.md)."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    import threading

    heard = []

    def transcribe(audio, **kwargs):
        text = 'Over.' if np.any(np.isclose(audio, _level(SOFT_HEAD))) else 'ver.'
        heard.append(text)
        return [SimpleNamespace(text=text)], SimpleNamespace(language='en')

    app = r.app
    app.model = SimpleNamespace(transcribe=transcribe)
    app.model_rate = SAMPLE_RATE
    app.model_lock = threading.Lock()
    app._wake_audio_is_below_gate = Mock(return_value=False)
    app.get_transcription_params = Mock(return_value={})
    app._filter_dictation_language = lambda text, info: text
    app.voice_training_window = SimpleNamespace(apply_corrections=lambda text: text)
    app._emit_wake_trace = Mock()
    app._log_history = Mock()
    monkeypatch.setattr(dictation, 'resample_audio', lambda audio, *args: audio, raising=False)
    dictation.DictationApp._decode_wake_word_buffer(app, buffer, SAMPLE_RATE)
    return heard


def _speak_soft_over_and_flush(r):
    _late_onset(r)
    r.feed(101.0, value=WORD, speech=True)
    end = 101.0
    while not r.app.process_wake_word_buffer.called:
        end = round(end + .1, 1)
        assert end < 104.0, 'utterance never flushed'
        r.feed(end, value=0, speech=False)
    return r.app.process_wake_word_buffer.call_args.args[0]


def test_soft_onset_end_word_transcribes_and_ends_the_session(rig, monkeypatch):
    r = rig()
    buffer = _speak_soft_over_and_flush(r)
    assert r.app.app_state == 'wake_session'
    heard = _decode_as_whisper_would(r, buffer, monkeypatch)
    assert heard == ['Over.']
    assert r.app.app_state == 'asleep'
    r.app._output_dictation.assert_not_called()


def test_key_off_soft_onset_end_word_loses_its_head_and_session_stays_open(rig, monkeypatch):
    r = rig(KEY_OFF)
    buffer = _speak_soft_over_and_flush(r)
    heard = _decode_as_whisper_would(r, buffer, monkeypatch)
    assert heard == ['ver.']
    assert r.app.app_state == 'wake_session'
    r.app._output_dictation.assert_called_once_with('ver.')
