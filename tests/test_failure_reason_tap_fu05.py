"""921: tutorial captures reach the real streaming final and both displays."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import failure_reasons, speech_presence, streaming, tutorial_capture


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('duration', [.1, .509, .51, 3.])
@pytest.mark.parametrize('binding,label', [('ctrl+shift', 'Ctrl+Shift'), ('ctrl+alt+k', 'Ctrl+Alt+K')])
def test_tutorial_empty_capture_final_reaches_display_and_home(qapp, monkeypatch, display, duration, binding, label):
    from PySide6.QtWidgets import QMainWindow
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.home_qt import instruction_line
    from samsara.ui.listening_indicator import ListeningIndicator

    window = QMainWindow()
    window.show()
    app = SimpleNamespace(
        config={'hotkey': 'ctrl+shift', 'tts': {'feedback_level': 'brief'},
                'ui': {'live_surface': {'enabled': display == 'Full'}}},
        model_rate=16000, _schedule_ui=lambda fn, *args: fn(*args),
        main_window=SimpleNamespace(_window=window), _log_history=Mock(),
        model=SimpleNamespace(transcribe=Mock(side_effect=AssertionError('empty capture decoded'))))
    controller = None
    if display == 'Full':
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel(clock=lambda: 0.))
        app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        app.listening_indicator = ListeningIndicator()
    registration = tutorial_capture.Registration(1, Mock())
    app._tutorial_capture_registration = registration
    session = streaming.StreamingSession.__new__(streaming.StreamingSession)
    session.app = app
    session._tutorial_owner = tutorial_capture.Owner(registration)
    session.cancel_event = threading.Event()
    session._state_lock = threading.RLock()
    session._state = session.STATE_FINALIZING
    session._direct_paste = False
    session._mark_done = Mock()
    session._overlay = Mock(spec=streaming._LiveSurfaceStreamingOverlay) if display == 'Full' else Mock()
    app._streaming_session = session
    worker = streaming.StreamingWorker.__new__(streaming.StreamingWorker)
    worker._session = session
    worker._snapshot_audio = Mock(return_value=np.zeros(int(duration * 16000), dtype=np.float32))
    measure = Mock(return_value=speech_presence.Presence(True))
    monkeypatch.setattr(speech_presence._presence, 'measure', measure)
    try:
        # 921 FINISH2: a binding changed after capture starts is read at feedback time.
        app.config = dict(app.config, hotkey=binding)
        worker._final_pass()
        expected = failure_reasons.reason_for(
            'short_tap' if duration < .51 else 'near_silent', {'config': app.config})[0]
        if controller:
            controller.drain()
            actual = controller.view().notice.message
            session._overlay.show_empty_result.assert_called_once_with(expected)
        else:
            actual = app.listening_indicator._chip_label
            assert session._overlay.update_text.call_args.args[0] == expected
        assert actual == expected == window.statusBar().currentMessage()
        if duration < .51:
            assert actual == f'Too short - hold {label} while you speak, release when done'
            assert instruction_line(app.config, None) == f'Hold {label} and speak.'
            measure.assert_not_called()
        else:
            measure.assert_called_once()
        app.model.transcribe.assert_not_called()
        registration.callback.assert_not_called()
        assert app._log_history.call_count == (1 if duration > streaming.MIN_PARTIAL_AUDIO_S else 0)
        session._overlay.flash_done_and_fade.assert_called_once()
    finally:
        window.close()
        if controller:
            controller.close()
        else:
            app.listening_indicator.destroy()
            app.listening_indicator.deleteLater()


@pytest.mark.parametrize('cancelled,revoked', [(True, False), (False, True)])
def test_retired_tutorial_result_posts_no_reason(monkeypatch, cancelled, revoked):
    app = SimpleNamespace(config={'hotkey': 'f9'})
    registration = tutorial_capture.Registration(1, Mock(), active=not revoked)
    app._tutorial_capture_registration = registration
    session = SimpleNamespace(app=app, cancel_event=threading.Event(),
                              _tutorial_owner=tutorial_capture.Owner(registration),
                              _deliver_final=Mock(), _empty_capture_kind_891='short_tap')
    if cancelled:
        session.cancel_event.set()
    post = Mock()
    monkeypatch.setattr(failure_reasons, 'post_reason', post)
    failure_reasons.deliver_stream_final(session, None, None, .1, 0)
    post.assert_not_called()


@pytest.mark.parametrize('duration', [0., .1, 3.])
@pytest.mark.parametrize('owner_state', ['active', 'revoked', 'ordinary'])
def test_batch_feedback_and_history_keep_the_same_classification(duration, owner_state):
    app = SimpleNamespace(config={'hotkey': 'f9'}, _log_history=Mock(),
                          _show_outcome_chip=Mock())
    registration = tutorial_capture.Registration(1, Mock(), active=owner_state == 'active')
    app._tutorial_capture_registration = registration
    owner = tutorial_capture.Owner(registration) if owner_state != 'ordinary' else None
    failure_reasons.show_empty_capture(app, duration, tutorial_owner=owner)
    if owner_state == 'active' and duration > 0:
        label = app._show_outcome_chip.call_args.args[0]
        assert label.startswith('Too short' if duration < .51 else 'Nothing heard')
    else:
        app._show_outcome_chip.assert_not_called()
    assert app._log_history.call_count == (1 if duration > .5 else 0)


def test_batch_short_and_both_silence_exits_use_shared_feedback():
    source = (Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    batch = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                 and node.name == 'transcribe' and 'Audio too short' in ast.get_source_segment(source, node))
    calls = [node for node in ast.walk(batch) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == 'show_empty_capture']
    assert len(calls) == 3
    assert all(ast.unparse(node.args[1]) == 'audio_duration' for node in calls)
    assert all(any(kw.arg == 'tutorial_owner' and ast.unparse(kw.value) == 'tutorial_owner'
                   for kw in node.keywords) for node in calls)


@pytest.mark.parametrize('ending', ['short', 'presence', 'legacy', 'command_ghost', 'ava_ghost'])
@pytest.mark.parametrize('tutorial', [False, True])
def test_batch_tutorial_owner_reaches_real_capture_exits(monkeypatch, ending, tutorial):
    from dictation import DictationApp
    import dictation as d

    app = object.__new__(DictationApp)
    app.config = {'mode': 'hold', 'recording_tail_ms': 0, 'hotkey': 'f9'}
    app.model_rate = 16000
    app.recording = app._ace_dictation_active = True
    app.hotkey_pressed = False
    app._last_recording_earcon_ms = 0
    lane = {'command_ghost': 'command_key', 'ava_ghost': 'ava_hold'}.get(ending, 'dictation_hold')
    app._capture_provenance = (object(), lane)
    app._command_mode_ghost_tap = ending == 'command_ghost'
    app._ava_mode_ghost_tap = ending == 'ava_ghost'
    registration = tutorial_capture.Registration(1, Mock())
    app._tutorial_capture_registration = registration
    app._tutorial_capture_token = tutorial_capture.Owner(registration) if tutorial else None
    for name in ('play_sound', '_release_icon_chase', '_update_tray_tooltip',
                 '_finish_empty_hold_capture', '_log_history', '_show_outcome_chip', 'set_app_state'):
        setattr(app, name, Mock())
    app._live_surface_hold_parking_enabled = lambda: False
    app._build_hotkey_transcribe_params = lambda: {}
    app._buffer_should_skip_decode = lambda *_args, **_kwargs: ending == 'legacy'
    duration = .1 if ending == 'short' else 3.
    audio = np.zeros(int(duration * app.model_rate), dtype=np.float32)
    app._dictation_consumer = SimpleNamespace(drain=lambda: audio, drain_after_release=lambda **_: audio)
    app.model = SimpleNamespace(transcribe=Mock())
    workers = []
    monkeypatch.setattr(d, 'is_self_hear_capture', lambda *_: False)
    monkeypatch.setattr(d.thread_registry, 'spawn', lambda name, target, daemon: workers.append(target))
    monkeypatch.setattr(d.diagnostics, 'record', Mock())
    monkeypatch.setattr(speech_presence, 'skip_decode', lambda *_args, **_kwargs: ending == 'presence')
    app._stop_recording_impl()
    for worker in workers:
        worker()
    if tutorial and not ending.endswith('ghost'):
        label = failure_reasons.reason_for(
            'short_tap' if ending == 'short' else 'near_silent', {'config': app.config})[0]
        assert app._show_outcome_chip.call_args.args[0] == label
    else:
        app._show_outcome_chip.assert_not_called()
    if ending.endswith('ghost'):
        assert not workers
    app.model.transcribe.assert_not_called()
    app.play_sound.assert_called_once_with('stop')


@pytest.mark.parametrize('presence', [speech_presence.Presence(False),
                                    speech_presence.Presence(True, max_probability=.9)])
def test_normal_length_speech_or_unknown_detector_still_reaches_decode(monkeypatch, presence):
    app = SimpleNamespace(model_rate=16000, config={})
    session = SimpleNamespace(app=app, on_final=Mock())
    monkeypatch.setattr(speech_presence._presence, 'measure', Mock(return_value=presence))
    assert not failure_reasons.finish_streaming_if_empty(
        SimpleNamespace(_session=session), np.zeros(48000, dtype=np.float32))
    assert session._empty_capture_kind_891 is None
    session.on_final.assert_not_called()


@pytest.mark.parametrize('duration', [None, 0., .1, 3.])
@pytest.mark.parametrize('tutorial', [False, True])
def test_streaming_empty_guards_precede_synchronous_delivery(monkeypatch, duration, tutorial):
    app = SimpleNamespace(model_rate=16000, config={}, _show_outcome_chip=Mock())
    registration = tutorial_capture.Registration(1, Mock())
    app._tutorial_capture_registration = registration
    session = SimpleNamespace(app=app, cancel_event=threading.Event(), _deliver_final=Mock(),
        _tutorial_owner=tutorial_capture.Owner(registration) if tutorial else None)
    session.on_final = lambda text, duration_s=0: failure_reasons.deliver_stream_final(
        session, text, None, duration_s, 0)
    monkeypatch.setattr(speech_presence._presence, 'measure', Mock(return_value=speech_presence.Presence(True)))
    pcm = None if duration is None else np.zeros(int(duration * 16000), dtype=np.float32)
    assert failure_reasons.finish_streaming_if_empty(SimpleNamespace(_session=session), pcm)
    session._deliver_final.assert_called_once()
    if tutorial and duration:
        kind = 'short_tap' if duration < .51 else 'near_silent'
        label = failure_reasons.reason_for(kind, {'config': app.config})[0]
        assert app._show_outcome_chip.call_args.args[0] == label
        assert failure_reasons.stream_empty_message(session) == label
    else:
        app._show_outcome_chip.assert_not_called()
        assert failure_reasons.stream_empty_message(session) == ''
