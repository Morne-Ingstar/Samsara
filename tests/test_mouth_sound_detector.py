"""Synthetic DSP and real WakeConsumer admission contracts; no devices/models."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.audio_engine import mouth_sound_detector as mouth
from samsara.audio_engine.frame import Frame, FRAME_SIZE, SAMPLE_RATE
from samsara.audio_engine.ring import EMPTY
from samsara.audio_engine.wake_consumer import WakeConsumer
from samsara.config_schema import SETTINGS_SCHEMA


def signal(onsets=(.1, .35), seconds=1.5):
    """Repeat a deterministic 10-ms high-band noise burst, not a voice sample."""
    rng = np.random.default_rng(657)
    spectrum = np.fft.rfft(rng.standard_normal(160))
    freq = np.fft.rfftfreq(160, 1 / SAMPLE_RATE)
    spectrum[(freq < 2200) | (freq > 5800)] = 0
    pulse = np.fft.irfft(spectrum)
    pulse *= .15 / np.sqrt(np.mean(pulse ** 2))
    audio = np.zeros(round(seconds * SAMPLE_RATE))
    for onset in onsets:
        index = round(onset * SAMPLE_RATE)
        audio[index:index + pulse.size] += pulse
    return np.round(np.clip(audio, -1, 1) * 32767).astype(np.int16)


def negatives(seconds=3):
    """Procedural surrogates only: cannot establish real-world false-fire rates."""
    rng = np.random.default_rng(658)
    t = np.arange(round(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    noise = rng.standard_normal(t.size)
    speech = sum(.02 / k * np.sin(2 * np.pi * 150 * k * t) for k in range(1, 12))
    speech *= .5 + .5 * np.sin(2 * np.pi * 3 * t)
    breath = .025 * noise * np.sin(np.pi * np.minimum(t % 1, .8) / .8) ** 2
    tv = speech + .012 * noise + .02 * np.sin(2 * np.pi * 60 * t)
    typing = np.zeros_like(t)
    mouse = np.zeros_like(t)
    for onset in np.arange(.1, seconds - .1, .25):
        start = round(onset * SAMPLE_RATE)
        n = 480
        typing[start:start+n] += .2 * np.sin(2*np.pi*900*np.arange(n)/SAMPLE_RATE) * np.exp(-np.arange(n)/120)
        mouse[start:start+24] += .25 * noise[start:start+24]
    return {name: np.round(np.clip(data, -1, 1) * 32767).astype(np.int16)
            for name, data in dict(typing=typing, mouse=mouse, speech=speech, breath=breath, tv=tv).items()}


def frames(audio, base=100, epoch=0):
    for seq, offset in enumerate(range(0, len(audio) - FRAME_SIZE + 1, FRAME_SIZE)):
        yield Frame(seq, base + (offset + FRAME_SIZE) / SAMPLE_RATE,
                    audio[offset:offset + FRAME_SIZE], epoch)


def replay(audio, detector=None):
    detector = detector or mouth.MouthSoundDetector()
    return [event for frame in frames(audio)
            for event in detector.feed(frame.pcm, frame.t_capture - .1)]


@pytest.mark.parametrize('gap, expected', [(145, 0), (150, 1), (155, 1), (250, 1), (345, 1), (350, 1), (355, 0)])
def test_inclusive_pair_window(gap, expected):
    events = replay(signal((.1, .1 + gap / 1000)))
    assert len(events) == expected
    if events:
        assert events[0]['gap_ms'] == gap
        assert events[0]['reason'] == 'pair'
        assert 0 < events[0]['score'] <= 1


def test_pair_window_is_calibratable():
    assert len(replay(signal((.1, .5)), mouth.MouthSoundDetector((300, 450)))) == 1


def test_single_and_refractory():
    assert not replay(signal((.1,)))
    events = replay(signal((.1, .35, .6, .85, 1.2, 1.45), seconds=2))
    assert len(events) == 2
    assert [event['ts_first'] for event in events] == pytest.approx([100.1, 101.2])


@pytest.mark.parametrize('offset', [.095, .098, .099, .101])
def test_frame_splits(offset):
    events = replay(signal((offset, offset + .25)))
    assert len(events) == 1
    assert events[0]['ts_first'] == pytest.approx(100 + offset, abs=.005)


@pytest.mark.parametrize('kind', ['typing', 'mouse', 'speech', 'breath', 'tv'])
def test_negative_surrogates(kind):
    assert not replay(negatives()[kind])


def make_consumer(enabled=True):
    app = SimpleNamespace(
        config={'mouth_sounds': {'experiment_shadow': enabled}},
        app_state='wake_session', _hotkey_recording=False,
        _ava_cmd_generation=1, _wake_capture_admission=None,
        _hands_free_capture_duck_token=None, command_mode_active=False,
        ava_command_session_active=False, is_speaking=False, silence_start=None,
        wake_word_triggered=True, _command_executed_at=None, _tts_last_speaking=0.,
        _dictation_silence_timeout=10000.,
        audio_coordinator=SimpleNamespace(is_speaking=False),
        _vad_available=True, _vad_is_speech=Mock(return_value=False),
        _expire_wake_session=Mock(), _wake_detector=None,
        _open_hands_free_capture_duck=Mock(side_effect=OSError('synthetic failure')),
    )
    reader = SimpleNamespace(read_next=lambda: EMPTY)
    consumer = WakeConsumer(SimpleNamespace(register_consumer=lambda name: reader), app)
    records = []
    if enabled:
        consumer._mouth_shadow.sink = SimpleNamespace(write=lambda reason, **data: records.append(dict(reason=reason, **data)))
    return consumer, app, records


def drive(consumer, audio, base=100):
    for frame in frames(audio, base):
        consumer._process_frame(frame)


def pairs(records):
    return [record for record in records if record['reason'] == 'pair']


def test_real_hook_before_vad_and_no_action_or_timer_touch(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    before_vad = []
    def vad(*args, **kwargs):
        before_vad.append(len(pairs(records)))
        return False
    app._vad_is_speech.side_effect = vad
    drive(consumer, signal())
    assert len(pairs(records)) == 1
    assert before_vad[3] == 1
    app._expire_wake_session.assert_called()  # Existing expiry remains serviced.
    app._open_hands_free_capture_duck.assert_not_called()
    assert app.app_state == 'wake_session' and app._ava_cmd_generation == 1
    assert consumer._utterance_frames == []


def test_rewind_is_not_replayed(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    stream = list(frames(signal()))
    for frame in stream[:5] + stream[:5] + stream[5:]:
        consumer._process_frame(frame)
    assert len(pairs(records)) == 1


@pytest.mark.parametrize('change', ['epoch', 'gap', 'generation', 'session', 'duck'])
def test_pair_cannot_span_discontinuity(monkeypatch, change):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    for frame in frames(signal()):
        if frame.seq == 2:
            if change == 'generation':
                app._ava_cmd_generation += 1
            if change == 'session':
                app.app_state = 'quick_dictation'
            if change == 'duck':
                consumer._hands_free_capture_duck_token = 8
        if change == 'epoch' and frame.seq >= 2:
            frame.device_epoch = 1
        if change == 'gap' and frame.seq == 2:
            continue
        consumer._process_frame(frame)
    assert not pairs(records)


@pytest.mark.parametrize('block', ['hold', 'tts', 'asleep'])
def test_unconditional_sound_veto_even_during_capture(monkeypatch, block):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    app.is_speaking = True  # Ordinary speech capture's TTS exemption cannot admit sounds.
    if block == 'hold':
        app._hotkey_recording = True
        app.command_mode_active = True
        app.config.update(command_mode={'mode': 'toggle'}, hands_free={'suspend_on_hold': False})
    elif block == 'tts':
        app.audio_coordinator.is_speaking = True
    else:
        app.app_state = 'asleep'
    drive(consumer, signal())
    assert not pairs(records)


def test_tts_tail_and_suppressed_rewind(monkeypatch):
    now = [100.]
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: now[0])
    consumer, app, records = make_consumer()
    stream = list(frames(signal()))
    app.audio_coordinator.is_speaking = True
    consumer._process_frame(stream[0])
    app.audio_coordinator.is_speaking = False
    for frame in stream[1:5]:
        now[0] = frame.t_capture - .1
        consumer._process_frame(frame)
    for frame in stream[:5]:
        consumer._process_frame(frame)
    assert not pairs(records)


def test_timestamp_admission_before_detector_even_keep_policy(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    app._wake_capture_admission = (100.2, 'keep', 100.)
    drive(consumer, signal())
    assert not pairs(records)  # First click predates confirmation + guard.


def test_generation_changed_during_scoring_drops_delivery(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    original = consumer._mouth_shadow.detector.feed
    def race(*args):
        events = original(*args)
        if events:
            app._ava_cmd_generation += 1
        return events
    consumer._mouth_shadow.detector.feed = race
    drive(consumer, signal())
    assert not pairs(records)


def test_duck_failure_never_opens_an_action_path(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    assert consumer._open_hands_free_duck_safe(app) is None
    app._open_hands_free_capture_duck.reset_mock()
    drive(consumer, signal())
    assert len(pairs(records)) == 1  # Shadow can inspect unducked idle audio.
    app._open_hands_free_capture_duck.assert_not_called()
    assert app.app_state == 'wake_session'


def test_flag_off_no_construction_dsp_or_log(monkeypatch):
    monkeypatch.setattr(mouth, 'MouthSoundShadow', Mock(side_effect=AssertionError('must be inert')))
    consumer, app, records = make_consumer(False)
    drive(consumer, signal())
    assert consumer._mouth_shadow is None and not records
    mouth.MouthSoundShadow.assert_not_called()
    assert SETTINGS_SCHEMA['mouth_sounds.experiment_shadow']['default'] is False
    assert all(value['settings_ui'] is False for key, value in SETTINGS_SCHEMA.items() if key.startswith('mouth_sounds.'))


@pytest.mark.parametrize('failure', ['dsp', 'log', 'config'])
def test_error_disables_once_for_session(monkeypatch, caplog, failure):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    shadow = consumer._mouth_shadow
    if failure == 'dsp':
        shadow.detector.feed = Mock(side_effect=RuntimeError('private details'))
    elif failure == 'log':
        shadow.sink.write = Mock(side_effect=OSError('private details'))
    else:
        shadow = mouth.MouthSoundShadow({'mouth_sounds': {'pair_window_ms': [500, 100]}})
        consumer._mouth_shadow = shadow
    drive(consumer, signal())
    shadow.invalidate()
    drive(consumer, signal(), base=102)
    assert shadow.disabled and not pairs(records)
    messages = [record.message for record in caplog.records if 'detector_error' in record.message]
    assert len(messages) == 1 and 'private details' not in messages[0]


def test_shadow_log_is_numeric_and_rotates(tmp_path):
    path = tmp_path / 'logs' / 'mouth_sounds_shadow.jsonl'
    sink = mouth.ShadowLog(path)
    sink.write('pair', ts_first=1., ts_second=1.25, gap_ms=250., score=.8)
    record = json.loads(path.read_text())
    assert record == dict(reason='pair', count=1, ts_first=1., ts_second=1.25, gap_ms=250., score=.8)
    path.write_bytes(b' ' * (1024 * 1024))
    sink.write('pair', gap_ms=200.)
    assert path.with_suffix('.jsonl.1').stat().st_size == 1024 * 1024
    assert json.loads(path.read_text())['count'] == 2


def test_oww_barge_in_is_not_sound_permission(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    consumer._is_latched_ava_mode = lambda app: True
    consumer._ava_tts_half_duplex_enabled = lambda app: True
    consumer._wake_barge_in_during_ava_tts = Mock(return_value=True)
    app.audio_coordinator.is_speaking = True
    drive(consumer, signal())
    consumer._wake_barge_in_during_ava_tts.assert_called()
    assert not pairs(records)


def test_suppressed_frame_does_not_join_later_pair(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    for frame in frames(signal()):
        app._hotkey_recording = frame.seq == 2
        consumer._process_frame(frame)
    assert not pairs(records)


def test_reset_and_malformed_pcm_fail_closed(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    consumer._mouth_shadow.observe(consumer, next(frames(signal())))
    frame = Frame(1, 100.2, np.zeros(FRAME_SIZE, dtype=np.float32), 0)
    consumer._mouth_shadow.consume(consumer, frame, None)
    assert consumer._mouth_shadow.disabled
    assert not pairs(records)


def test_active_toggle_uses_session_identity_even_when_app_state_asleep(monkeypatch):
    monkeypatch.setattr(mouth.time, 'perf_counter', lambda: 99.)
    consumer, app, records = make_consumer()
    app.app_state = 'asleep'
    app.command_mode_active = True
    app.config['command_mode'] = {'mode': 'toggle'}
    drive(consumer, signal())
    assert len(pairs(records)) == 1
    assert app.app_state == 'asleep' and app.command_mode_active
