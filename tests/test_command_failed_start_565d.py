"""Failed Command-key starts cannot claim a later ordinary hold.

Run with the maintained no-bootstrap adapter; audio and delivery are fake.
"""
import sys
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.hotkeys import HotkeyCluster
from samsara import speech_presence


@pytest.fixture
def capture(monkeypatch):
    host = sys.modules['dictation']
    cls = host.DictationApp
    app = SimpleNamespace(
        config={'hotkey': 'f8', 'command_hotkey': 'f9', 'mode': 'hold',
                'recording_tail_ms': 0},
        _running=True, model_loaded=True, recording=False, snoozed=False,
        hotkey_pressed=False, command_mode_recording=False,
        command_mode_active=False, toggle_active=False, continuous_active=False,
        _stop_in_flight=False, _streaming_session=None, _wake_consumer=None,
        _hold_capture_lifecycle_lock=threading.RLock(),
        current_keys=set(), key_press_times={}, _hold_down_key=False,
        _dictation_consumer=SimpleNamespace(activate=Mock(return_value=True),
            cancel=Mock(), stop_streaming=Mock()),
    )
    for name in ('start_recording', '_start_recording_impl', 'cancel_recording',
                 '_cancel_recording_impl', '_take_recording_ownership',
                 '_stop_recording_impl', '_finish_empty_hold_capture'):
        setattr(app, name, MethodType(getattr(cls, name), app))
    for name in ('play_sound', '_duck_audio', '_restore_audio',
                 '_open_hold_capture_duck', '_close_hold_capture_duck',
                 '_request_icon_chase', '_release_icon_chase', '_update_tray_tooltip',
                 '_set_microphone_state', '_park_hold_feedback'):
        setattr(app, name, Mock())
    app._live_surface_hold_parking_enabled = lambda: False
    app.set_app_state = lambda **kw: app.__dict__.update(kw)
    app.get_key_name = lambda key: key
    app.parse_hotkey = MethodType(HotkeyCluster.parse_hotkey, app)
    app.check_hotkey_state = lambda combo: combo in app.current_keys
    app._check_command_mode_key = Mock()
    app._hands_free_dictation_commit_available = lambda: False
    app._hotkey_state_text = lambda: 'synthetic capture'
    monkeypatch.setattr(speech_presence, 'play_hold_start', Mock())
    monkeypatch.setattr(host.flight_recorder, 'record', Mock())
    return app


@pytest.mark.parametrize('failure', [
    'mic_lost', 'consumer_missing', 'engine_restarting', 'activate_false',
    'not_running', 'model_unloaded', 'surface_finalizing', 'streaming_owned',
    'activate_raises',
])
@pytest.mark.parametrize('source', ['command_hotkey', 'command_hold'])
def test_failed_command_start_then_ordinary_hold(capture, monkeypatch, failure, source):
    app = capture
    consumer = app._dictation_consumer
    if failure == 'mic_lost':
        app._ace_engine = SimpleNamespace(running=False, device_lost=True)
    elif failure == 'consumer_missing':
        app._dictation_consumer = None
    elif failure == 'engine_restarting':
        app._ace_engine = SimpleNamespace(running=False, device_lost=False)
    elif failure == 'activate_false':
        consumer.activate.return_value = False
    elif failure == 'activate_raises':
        consumer.activate.side_effect = RuntimeError('synthetic activation failure')
    elif failure == 'not_running':
        app._running = False
    elif failure == 'model_unloaded':
        app.model_loaded = False
    elif failure == 'surface_finalizing':
        app._live_surface_hold_parking_enabled = lambda: True
        app._hold_park_finalizing = True
    elif failure == 'streaming_owned':
        app._streaming_session = object()
    def start():
        if source == 'command_hotkey':
            HotkeyCluster.on_key_press(app, 'f9')
        else:
            app.command_mode_recording = True
            app.start_recording(streaming=False, play_earcon=False)
    if failure == 'activate_raises':
        with pytest.raises(RuntimeError, match='synthetic activation failure'):
            start()
    else:
        start()
    assert not app.recording
    assert not app.command_mode_recording
    assert not app.hotkey_pressed

    # Recover the microphone/start guards and press the ordinary dictation key.
    app.current_keys.clear()
    app._running = app.model_loaded = True
    app._ace_engine = None
    app._streaming_session = None
    app._hold_park_finalizing = False
    app._live_surface_hold_parking_enabled = lambda: False
    app._dictation_consumer = consumer
    consumer.activate.side_effect = None
    consumer.activate.return_value = True
    HotkeyCluster.on_key_press(app, 'f8')
    assert app.recording
    assert not app.command_mode_recording
    _assert_capture_route(app, monkeypatch, command=False)


def test_successful_command_start_keeps_command_provenance(capture, monkeypatch):
    HotkeyCluster.on_key_press(capture, 'f9')
    assert capture.recording and capture.command_mode_recording
    _assert_capture_route(capture, monkeypatch, command=True)


def test_duplicate_start_preserves_existing_command_capture(capture):
    HotkeyCluster.on_key_press(capture, 'f9')
    capture.start_recording(streaming=False, play_earcon=False)
    assert capture.recording and capture.command_mode_recording


def _assert_capture_route(app, monkeypatch, *, command):
    host = sys.modules['dictation']
    audio = np.ones(16000, dtype=np.float32)
    app.model_rate = 16000
    app._dictation_consumer.drain = lambda: audio
    app._dictation_consumer.drain_after_release = lambda **kw: audio
    app._last_recording_earcon_ms = 0
    app._build_hotkey_transcribe_params = lambda: {}
    app._buffer_should_skip_decode = lambda *a, **kw: False
    decode = Mock(return_value=SimpleNamespace(text='synthetic words', low_confidence=False,
        seg_list=[], detected_lang='en', diag_path='synthetic'))
    app._decode_hotkey_audio = decode
    app.voice_training_window = SimpleNamespace(apply_corrections=lambda text: text)
    monkeypatch.setattr(speech_presence, 'skip_decode', lambda *a, **kw: False)
    monkeypatch.setattr(host, '_apply_retry_on_suspected_loss',
                        lambda result, *a: (result, False, False))
    monkeypatch.setattr(host, 'is_signoff_hallucination', lambda *a: (False, None))
    monkeypatch.setattr(host.thread_registry, 'spawn', lambda name, fn, **kw: fn())
    # Stop immediately at the first delivery seam; unexpected routing fails.
    class Routed(BaseException):
        pass
    paths = []
    def dispatch(text, owner, **kw):
        paths.append(kw['capture_path'])
        raise Routed()
    app.command_executor = SimpleNamespace(process_text=dispatch)
    def dictation(text):
        paths.append('dictation_hold')
        raise Routed()
    app._apply_verbatim_if_active = dictation
    with pytest.raises(Routed):
        app._stop_recording_impl()
    path = 'command_key' if command else 'dictation_hold'
    assert paths == [path]
    assert decode.call_args.kwargs['free_form'] is (not command)
    assert not app.command_mode_recording
