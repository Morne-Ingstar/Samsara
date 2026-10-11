"""Destination ownership at capture onset, with fake native window boundaries."""
from samsara.log import describe_text, safe_exception
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_tutorial_dictation_489 import compile_method, method, window, hold
from tests.test_cancelled_final_497 import session_stub
from tests.test_hold_capture_duck import app as capture_app


def test_waiting_hook_cannot_override_ordinary_target_guard():
    from samsara import paste
    receive = Mock()
    transport = Mock(return_value=False)
    app = SimpleNamespace(_tutorial_hooks={'dictation': receive})
    wrapper = compile_method(method('dictation.py', '_paste_preserving_clipboard'),
        _paste=SimpleNamespace(paste_preserving_clipboard=transport,
                              show_delivery_result=Mock()),
        paste_with_preservation=Mock(), type_text_unicode=Mock(),
        _get_foreground_hwnd=Mock())
    app._notify_tutorial = compile_method(method('dictation.py', '_notify_tutorial')).__get__(app)
    guard = Mock(return_value=False)
    assert wrapper(app, 'synthetic sample', before_paste=guard,
                   tutorial_dictation=True) is False
    receive.assert_not_called()
    assert app._tutorial_hooks['dictation'] is receive
    assert transport.call_args.kwargs['before_paste'] is guard


def registration(monkeypatch, *, foreground=True, visible=True, minimized=False):
    from samsara import tutorial_capture as tc
    state = SimpleNamespace(foreground=foreground, visible=visible, minimized=minimized)
    monkeypatch.setattr(tc, 'window_ready', lambda hwnd:
        state.foreground and state.visible and not state.minimized)
    app = SimpleNamespace(_tutorial_hooks={}, config={})
    receive = Mock()
    reg = tc.register(app, 123, receive)
    return tc, app, receive, reg, state


@pytest.mark.parametrize('condition', ['background', 'hidden', 'minimized'])
def test_nonforeground_tutorial_never_owns_capture(monkeypatch, condition):
    tc, app, receive, reg, state = registration(monkeypatch)
    setattr(state, {'background': 'foreground', 'hidden': 'visible',
                    'minimized': 'minimized'}[condition], condition == 'minimized')
    assert tc.begin(app) is None
    assert app._tutorial_hooks['dictation'] is receive


def test_ownership_is_frozen_at_onset_and_next_chunk_owns_separately(monkeypatch):
    tc, app, receive, reg, state = registration(monkeypatch)
    owner = tc.begin(app)
    state.foreground = False
    assert tc.deliver(app, owner, 'synthetic first') is True
    receive.assert_called_once_with('synthetic first')
    assert tc.begin(app) is None
    state.foreground = True
    second = tc.begin(app)
    assert tc.deliver(app, second, 'synthetic second') is True
    assert receive.call_count == 2
    assert app._tutorial_hooks['dictation'] is receive


@pytest.mark.parametrize('action', ['close', 'skip', 'back'])
def test_closed_step_revokes_owner_without_external_paste(window, action):
    from samsara import tutorial_capture as tc
    app = window._app
    owner = tc.begin(app)
    assert owner is not None
    getattr(window, {'close': 'close', 'skip': '_skip_step', 'back': '_go_back'}[action])()
    assert not owner.registration.active
    assert app._tutorial_hooks.get('dictation') is None
    window._step = 1
    window._show_step()  # Returning to the step cannot revive its old capture.
    assert tc.deliver(app, owner, 'synthetic sample') is False
    assert window._dictation_box.toPlainText() == ''
    session = session_stub()
    session._tutorial_owner = owner
    session.app._tutorial_capture_registration = app._tutorial_capture_registration
    session._deliver_final('synthetic sample', None, 1, 0)
    session.app._paste_preserving_clipboard.assert_not_called()
    session.app._log_history.assert_called_once()
    assert session.app._log_history.call_args.kwargs['status'] == 'failed'
    assert session.app._streaming_session is None
    session._overlay.close.assert_called_once()


@pytest.mark.parametrize('lane', ['command_mode_recording', 'ava_mode_recording',
                                  '_memo_recording', '_capture_sink', 'command_mode_active',
                                  'ava_command_session_active'])
def test_other_lanes_never_own_tutorial(monkeypatch, lane):
    tc, app, receive, reg, state = registration(monkeypatch)
    setattr(app, lane, True)
    assert tc.begin(app) is None


def test_queued_handoff_does_not_complete_a_reopened_step(monkeypatch):
    tc, app, receive, reg, state = registration(monkeypatch)
    owner = tc.begin(app)
    event = reg.event
    tc.release(app)
    tc.register(app, 123, Mock())
    assert tc.accept_event(app, event) is False


def test_owned_streaming_final_reaches_box_once_without_external_paste(window):
    from samsara import paste, tutorial_capture as tc
    session = session_stub()
    session.app._tutorial_capture_registration = window._app._tutorial_capture_registration
    session.app._tutorial_hooks = window._app._tutorial_hooks
    session._tutorial_owner = tc.begin(session.app)
    external = Mock(side_effect=AssertionError('external transport must not run'))
    wrapper = compile_method(method('dictation.py', '_paste_preserving_clipboard'),
        _paste=SimpleNamespace(paste_preserving_clipboard=external,
                              show_delivery_result=paste.show_delivery_result))
    session.app._paste_preserving_clipboard = wrapper.__get__(session.app)
    session._deliver_final('synthetic sample', None, 1, 0)
    session._deliver_final('synthetic sample', None, 1, 0)
    assert window._dictation_box.toPlainText() == 'synthetic sample'
    external.assert_not_called()
    session.app.add_to_history.assert_called_once()


def test_buffer_owner_is_carried_through_queued_decode(monkeypatch):
    tc, app, receive, reg, state = registration(monkeypatch)
    frames = tc.start_frames(app)
    frozen = tc.copy_frames(frames)
    state.foreground = False
    result = tc.run(frozen, lambda: tc.deliver(app, tc.current(), 'synthetic sample'))
    assert result is True
    receive.assert_called_once()
    assert tc.current() is None


@pytest.mark.parametrize('parking', [False, True], ids=['Compact', 'Full'])
def test_background_tutorial_streaming_uses_ordinary_delivery(monkeypatch, parking):
    from samsara import streaming
    tc, app, receive, reg, state = registration(monkeypatch, foreground=False)
    session = session_stub(parking)
    session.app._tutorial_capture_registration = reg
    session.app._tutorial_hooks = app._tutorial_hooks
    session._tutorial_owner = tc.begin(app)
    session._paste_with_retry = Mock()
    monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', lambda: 123)
    session._deliver_final('synthetic sample', None, 1, 0)
    receive.assert_not_called()
    assert app._tutorial_hooks['dictation'] is receive
    if parking:
        session.app._paste_preserving_clipboard.assert_called_once_with('synthetic sample')
    else:
        session._paste_with_retry.assert_called_once_with('synthetic sample')


def test_second_wake_capture_appends_to_real_box_without_paste(window, hold):
    import time
    from samsara import tutorial_capture as tc, config_defaults
    from tests.test_tutorial_dictation_489 import bind_delivery
    app = hold.app
    callback, external, primer = bind_delivery(app, window)
    app.config.update(add_trailing_space=False, smart_corrections={'modes': {'wake': False}})
    app._apply_verbatim_if_active = lambda text: None
    app.process_transcription = lambda text: text
    app._apply_formatting_tokens = lambda text: text
    app.app_state = 'wake_session'
    app._wake_profile_active = True
    app._wake_session_first_chunk = True
    app._wake_session_target_process = 'synthetic tutorial target'
    app._foreground_process_name = lambda: 'synthetic tutorial target'
    output = compile_method(method('dictation.py', '_output_dictation'),
        current_stage_timing=lambda: None, time=time, clean_text=lambda text, **kw: text,
        diagnostics=SimpleNamespace(record=Mock(), DiagRecord=Mock()),
        config_defaults=config_defaults, _languages=SimpleNamespace(describe_diagnostics_language=Mock()))
    for text in ('synthetic first', 'synthetic second'):
        frames = tc.start_frames(app)
        assert tc.run(frames, output, app, text) is True
        hold.flush()
    assert window._dictation_box.toPlainText() == 'synthetic first synthetic second'
    assert callback.call_count == 2
    external.assert_not_called()
    primer.assert_not_called()
    assert window._next_btn.isEnabled()


def test_continuous_consumer_freezes_owner_before_dispatch(monkeypatch):
    import numpy as np
    from samsara.audio_engine import continuous_consumer as cc
    tc, app, receive, reg, state = registration(monkeypatch)
    app.config = {'min_speech_duration': 0.01}
    app.transcribe_continuous_buffer = Mock()
    engine = SimpleNamespace(register_consumer=lambda name: Mock())
    consumer = cc.ContinuousConsumer(engine, app)
    consumer._process_frame(SimpleNamespace(pcm=np.full(1600, 10000, dtype=np.int16)))
    owner = consumer._speech_frames.tutorial_owner
    assert owner is not None
    state.foreground = False
    jobs = []
    monkeypatch.setattr(cc.thread_registry, 'spawn', lambda name, fn, **kw: jobs.append((fn, kw)))
    assert consumer._flush() is True
    frames = jobs[0][1]['args'][0]
    assert frames.tutorial_owner is owner
    tc.run(frames, lambda: tc.deliver(app, tc.current(), 'synthetic sample'))
    receive.assert_called_once()


@pytest.mark.parametrize('visible,minimized,foreground,expected', [
    (True, False, 123, True), (False, False, 123, False),
    (True, True, 123, False), (True, False, 999, False)])
def test_native_visibility_minimize_and_foreground_checks(monkeypatch, visible, minimized, foreground, expected):
    from samsara import tutorial_capture as tc
    user32 = SimpleNamespace(IsWindowVisible=Mock(return_value=visible),
        IsIconic=Mock(return_value=minimized), GetForegroundWindow=Mock(return_value=foreground))
    monkeypatch.setattr(tc.ctypes, 'windll', SimpleNamespace(user32=user32))
    assert tc.window_ready(123) is expected


def test_hands_free_chunks_and_ordinary_pending_draft_have_separate_owners(monkeypatch):
    from samsara.session_modes import SessionMode, UtteranceSignals
    tc, app, receive, reg, state = registration(monkeypatch)
    external = Mock(return_value=True)
    def inject(text, guard=None):
        owner = tc.current()
        return tc.deliver(app, owner, text) if owner is not None else external(text)
    from samsara.session_modes import SessionModeManager
    manager = tc.install(SessionModeManager(abort_phrases=['cancel'],
        foreground_exe_resolver=lambda: 'synthetic.exe', foreground_hwnd_resolver=lambda: 123,
        inject_fn=inject, remove_chars_fn=Mock(), command_dispatch_fn=Mock(),
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True))
    manager.reset(SessionMode.DICTATE)
    signals = UtteranceSignals(has_contiguous_speech=True, transcript_confident=True,
                               compression_ratios=(1.0,))
    state.foreground = False
    ordinary = tc.start_frames(app)
    assert tc.run(ordinary, manager.dispatch_utterance, 'synthetic ordinary.', signals).kind == 'dictate_staged'
    old_draft = manager.dictate_pending_buffer
    state.foreground = True
    for text in ('synthetic tutorial first.', 'synthetic tutorial second.'):
        owned = tc.start_frames(app)
        assert tc.run(owned, manager.dispatch_utterance, text, signals).kind == 'dictate_injected'
    assert receive.call_count == 2
    external.assert_not_called()
    assert manager.dictate_pending_buffer == old_draft
    control = tc.start_frames(app)
    assert tc.run(control, manager.commit_pending_dictation).kind == 'dictate_committed'
    assert receive.call_count == 2
    external.assert_called_once_with(old_draft)


@pytest.mark.parametrize('action', ['deliver', 'close', 'skip', 'back'])
def test_real_start_stop_freezes_owner_and_releases_capture(qapp, capture_app, monkeypatch, action):
    from samsara import streaming, tutorial_capture as tc
    from samsara.ui.tutorial_qt import TutorialWindow
    app = capture_app
    app.config.update(auto_paste=True, ui={'live_surface': {'enabled': False}})
    app._stop_recording_impl = app.stop_impl
    app._schedule_ui = lambda callback, *args: callback(*args)
    app.add_to_history, app._log_history, app._notify_main_window = Mock(), Mock(), Mock()
    app._on_streaming_session_finished = compile_method(method(
        'dictation.py', '_on_streaming_session_finished')).__get__(app)
    external = Mock(side_effect=AssertionError('tutorial must not paste externally'))
    from samsara import paste
    app._paste_preserving_clipboard = compile_method(method('dictation.py', '_paste_preserving_clipboard'),
        _paste=SimpleNamespace(paste_preserving_clipboard=external,
                              show_delivery_result=paste.show_delivery_result)).__get__(app)

    class Overlay:
        STATE_DONE = 'done'
        def __init__(self, **kw):
            self.show, self.close, self.update_text = Mock(), Mock(), Mock()
        def flash_done_and_fade(self, finish):
            finish()

    monkeypatch.setattr(streaming, 'StreamingOverlayQt', Overlay)
    monkeypatch.setattr(streaming, 'StreamingWorker', Mock())
    monkeypatch.setattr(streaming.thread_registry, 'register', Mock())
    win = TutorialWindow(app)
    win._step = 1
    win._show_step()
    win.show()
    hwnd = int(win.winId())
    native = SimpleNamespace(GetForegroundWindow=Mock(return_value=hwnd),
        IsWindowVisible=Mock(side_effect=lambda handle: win.isVisible()),
        IsIconic=Mock(side_effect=lambda handle: win.isMinimized()))
    monkeypatch.setattr(tc.ctypes, 'windll', SimpleNamespace(user32=native))
    monkeypatch.setattr(streaming._user32, 'GetForegroundWindow', native.GetForegroundWindow)
    try:
        app.start_recording(streaming=True, play_earcon=False)
        session = app._streaming_session
        assert session._tutorial_owner is not None
        native.GetForegroundWindow.return_value = 999  # onset ownership stays frozen
        app.stop_recording()
        assert session._state == session.STATE_FINALIZING
        if action != 'deliver':
            getattr(win, {'close': 'close', 'skip': '_skip_step', 'back': '_go_back'}[action])()
        session.on_final('synthetic capture', duration_s=1)
        qapp.processEvents()
        assert app._streaming_session is None
        external.assert_not_called()
        assert win._dictation_box.toPlainText() == ('synthetic capture' if action == 'deliver' else '')
        assert app._log_history.call_args.kwargs['status'] == ('success' if action == 'deliver' else 'failed')
        app.start_recording(streaming=True, play_earcon=False)
        assert app.recording and app._streaming_session is not session
        app.stop_recording()
        app._streaming_session.cancel()
    finally:
        win.close()
        win.deleteLater()
        qapp.processEvents()


def test_revoked_batch_final_stays_in_failed_history(window, hold, tmp_path, monkeypatch):
    from tests.test_tutorial_dictation_489 import legacy_app, bind_delivery
    app, spies = legacy_app(tmp_path, monkeypatch, hold)
    callback, external, _ = bind_delivery(app, window)
    window._skip_step()
    app._stop_recording_impl()
    app.add_to_history.assert_called_once()
    app._log_history.assert_called_once()
    assert app._log_history.call_args.kwargs['status'] == 'failed'
    external.assert_not_called()
    callback.assert_not_called()


def test_tutorial_only_quick_chunk_expires_without_late_external_delivery(monkeypatch):
    import threading
    tc, app, receive, reg, state = registration(monkeypatch)
    app.wake_dictation_mode = 'quick'
    app.wake_dictation_buffer = []
    app._dictation_require_end = False
    app.app_state = 'wake_session'
    app._dictation_finalize_lock = threading.Lock()
    external = Mock(return_value=True)
    app._output_dictation = lambda text: tc.deliver(app, tc.current(), text) if tc.current() else external(text)
    def reset():
        app.wake_dictation_mode = None
        app.app_state = 'asleep'
    app._reset_wake_dictation = Mock(side_effect=reset)
    tc.run(tc.start_frames(app), tc.append_wake, app, 'synthetic practice')
    tc.release(app)
    finalize = compile_method(method('dictation.py', '_finalize_dictation_timeout'))
    finalize(app)
    assert app.wake_dictation_mode is None
    app._reset_wake_dictation.assert_called_once()
    receive.assert_called_once_with('synthetic practice')
    external.assert_not_called()


def test_extracted_wake_branch_resolves_capture_module_without_global():
    import ast
    import copy
    from samsara import config_defaults
    decoder = method('dictation.py', '_decode_wake_word_buffer')
    branch = next(node for node in ast.walk(decoder) if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Attribute)
        and node.test.left.attr == 'app_state'
        and any(isinstance(value, ast.Tuple) for value in node.test.comparators))
    route = ast.parse("def route(self, text):\n text_lower = text.lower()\n ww_config = self.config.get('wake_word_config', {})\n stage_timing = None\n").body[0]
    route.body.append(copy.deepcopy(branch))
    namespace = dict(describe_text=describe_text, safe_exception=safe_exception,
                     config_defaults=config_defaults, logger=Mock())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])),
                 '<tutorial-wake-route>', 'exec'), namespace)
    app = SimpleNamespace(app_state='quick_dictation', config={'wake_word_config': {}},
        wake_dictation_buffer=['synthetic ordinary'], _output_dictation=Mock(return_value=True),
        _reset_wake_dictation=Mock(), _emit_wake_trace=Mock())
    namespace['route'](app, 'over')
    app._output_dictation.assert_called_once_with('synthetic ordinary')
    app._reset_wake_dictation.assert_called_once()
