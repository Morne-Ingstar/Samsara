"""1286: real listener/ring replay and privacy-safe chronology checks.

Synthetic speech decisions isolate the hold state machine. Real voice/model
replays are separate local evidence, not a dependency of this portable test.
"""
import json
import logging
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara.audio_engine import wake_consumer as wake
from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE
from samsara.audio_engine.ring import FrameBus, EMPTY
from samsara.session_modes import SessionMode
from tools.diag.handsfree_deaf_1286 import analyse


def listener(monkeypatch, vad=None, silence=.65):
    clock = NS(now=100.)
    monkeypatch.setattr(wake, 'time', NS(
        time=lambda: clock.now, monotonic=lambda: clock.now,
        perf_counter=lambda: clock.now, perf_counter_ns=lambda: round(clock.now * 1e9)))
    monkeypatch.setattr(wake.flight_recorder, 'record', lambda *a, **k: None)
    bus = FrameBus()
    app = NS(config={'command_mode': {'mode': 'toggle', 'dictate_utterance_silence_s': silence},
                     'hands_free': {'suspend_on_hold': True},
                     'wake_word_config': {'audio': {'speech_threshold': .01088}}},
             command_mode_active=True, _hotkey_recording=False, wake_word_active=True,
             app_state='asleep', wake_word_triggered=False, is_speaking=False,
             silence_start=None, _command_executed_at=None, audio_coordinator=None,
             _tts_last_speaking=0., _vad_available=True, _wake_detector=None,
             _vad_reset=lambda: None,
             _session_mode_manager=NS(mode=SessionMode.DICTATE, buffer_dictate_until_commit=False))
    app._vad_is_speech = vad or (lambda audio, **kw: np.max(np.abs(audio)) > .1)
    consumer = wake.WakeConsumer(NS(register_consumer=bus.new_reader), app)
    captures = []

    def enqueue(frames, *a, **kw):
        # Observe real _flush at its asynchronous dispatch boundary; no threads.
        captures.append((clock.now, np.concatenate(frames)))

    consumer._enqueue_toggle_utterance = enqueue

    def feed(audio):
        audio = np.asarray(audio, dtype=np.float32)
        for start in range(0, len(audio), FRAME_SIZE):
            chunk = audio[start:start + FRAME_SIZE]
            chunk = np.pad(chunk, (0, FRAME_SIZE - len(chunk)))
            clock.now = round(clock.now + .1, 6)
            bus.write(np.clip(chunk * 32767, -32768, 32767).astype(np.int16), clock.now, 0)
            frame = consumer._reader.read_next()
            assert frame is not EMPTY
            consumer._process_frame(frame)

    return NS(consumer=consumer, app=app, clock=clock, feed=feed, captures=captures)


@pytest.mark.parametrize('with_hold', [False, True])
def test_fifteen_second_sentence_is_retained_with_and_without_hold(monkeypatch, with_hold):
    r = listener(monkeypatch)
    r.feed(np.zeros(SAMPLE_RATE))
    r.feed(np.full(SAMPLE_RATE * 2, .2))
    r.feed(np.zeros(SAMPLE_RATE))
    assert len(r.captures) == 1
    if with_hold:
        r.consumer.abort_utterance()  # production hold-start discard boundary
        r.app._hotkey_recording = True
        r.feed(np.full(SAMPLE_RATE * 2, .7))
        assert len(r.captures) == 1
        r.app._hotkey_recording = False
        r.feed(np.zeros(FRAME_SIZE))
    sentence = np.linspace(.21, .25, SAMPLE_RATE * 15, dtype=np.float32)
    r.feed(sentence)
    r.feed(np.zeros(SAMPLE_RATE))
    assert len(r.captures) == 2
    captured = r.captures[-1][1]
    assert np.count_nonzero(captured > .205) == len(sentence)
    assert np.max(captured) < .3  # held speech cannot leak back in pre-roll


def test_analyser_counts_strict_runs_and_does_not_copy_words(tmp_path):
    logs, flight = tmp_path / 'logs', tmp_path / 'flight'
    logs.mkdir()
    flight.mkdir()
    rows = ['2026-11-19 09:00:00,000 - DEBUG - [HOTKEY] Main hotkey (mouse) pressed']
    for minute in (0, 3):
        for second in (10, 12, 14):
            rows.append(f'2026-11-19 09:{minute:02}:{second:02},000 - INFO - '
                        '[GATE] speech_presence path=handsfree duration_s=1.400 peak=0.1 verdict=reject')
        rows.append(f'2026-11-19 09:{minute:02}:16,000 - INFO - '
                    '[GATE] speech_presence path=handsfree duration_s=4.000 peak=0.9 verdict=pass')
    rows.append('2026-11-19 09:04:00,000 - DEBUG - arbitrary private words must be omitted')
    (logs / 'samsara.log').write_text('\n'.join(rows))
    result = analyse(logs, flight)
    assert result['stretch_count'] == 2
    assert result['follow_hold_within_60s'] == 1
    assert result['not_follow_hold_within_60s'] == 1
    assert all(run['next_nonmatching_capture']['duration_s'] == 4 for run in result['stretches'])
    assert 'arbitrary private words' not in json.dumps(result)


def test_analyser_does_not_count_unknown_flight_closes(tmp_path):
    root = tmp_path
    (root / 'events-20261119.jsonl').write_text('\n'.join(json.dumps({
        'event': 'wake.session_close', 'reason': 'silence_flush', 'speech_s': 1.4,
        't_wall': 100 + i, 'thread': 'wake'}) for i in range(5)))
    result = analyse(root, root)
    assert result['flight_silence_closes'] == 5
    assert result['stretch_count'] == 0


@pytest.mark.parametrize('vad_available', [True, False])
def test_silence_close_logs_numeric_evidence_once(monkeypatch, caplog, vad_available):
    r = listener(monkeypatch)
    r.app._vad_available = vad_available
    calls = []
    r.app._vad_lock = threading.Lock()
    def probabilities(audio):
        calls.append(audio.copy())
        return np.array([.02], dtype=np.float32)
    r.app._vad_probabilities = probabilities
    r.app._hotkey_recording = True
    r.feed(np.zeros(FRAME_SIZE))
    r.app._hotkey_recording = False
    with caplog.at_level(logging.INFO, logger=wake.logger.name):
        r.feed(np.zeros(FRAME_SIZE))
        r.feed(np.full(SAMPLE_RATE * 2, .2))
        r.feed(np.zeros(SAMPLE_RATE))
    rows = [row.message for row in caplog.records if '[HANDSFREE-SILENCE]' in row.message]
    assert len(rows) == 1
    fields = dict(pair.split('=') for pair in rows[0].split()[1:])
    assert float(fields['silence_s']) == pytest.approx(.65)
    assert float(fields['since_release_s']) == pytest.approx(2.8)
    assert float(fields['rms']) == 0
    assert float(fields['vad_used']) == int(vad_available)
    assert float(fields['vad_peak']) == pytest.approx(.02 if vad_available else -1)
    assert float(fields['threshold']) == pytest.approx(.5 if vad_available else .01)
    assert len(calls) == int(vad_available)


def test_telemetry_failure_cannot_drop_a_capture(monkeypatch, caplog):
    r = listener(monkeypatch)
    r.app._vad_lock = threading.Lock()
    def broken(audio):
        raise RuntimeError('telemetry unavailable')
    r.app._vad_probabilities = broken
    with caplog.at_level(logging.INFO, logger=wake.logger.name):
        r.feed(np.zeros(SAMPLE_RATE))
        r.feed(np.full(SAMPLE_RATE * 2, .2))
        r.feed(np.zeros(SAMPLE_RATE))
    assert len(r.captures) == 1
    rows = [row.message for row in caplog.records if '[HANDSFREE-SILENCE]' in row.message]
    assert len(rows) == 1
    assert 'vad_peak=-1.000000' in rows[0] and 'since_release_s=-1.000' in rows[0]
