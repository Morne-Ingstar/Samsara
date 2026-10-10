"""Tutorial delivery contract; source extraction never bootstraps dictation."""
import ast
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFontMetrics
from samsara.ui import theme, tutorial_qt
from samsara.ui.widgets.step_indicator import StepIndicator
from PySide6.QtWidgets import QWidget

ROOT = Path(__file__).resolve().parents[1]


def method(file, name):
    tree = ast.parse((ROOT / file).read_text(encoding='utf-8'))
    node = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == name)
    node._tutorial_source = str(ROOT / file)
    return node


def compile_method(node, **names):
    namespace = dict(logger=logging.getLogger(__name__), **names)
    exec(compile(ast.Module(body=[node], type_ignores=[]), node._tutorial_source, 'exec'), namespace)
    return namespace[node.name]


@pytest.fixture(params=[5, 6])
def window(qapp, monkeypatch, request):
    original = tutorial_qt.TutorialWindow._build_step_list
    def steps(self):
        original(self)
        if request.param == 5:
            self._steps = [step for step in self._steps if step[0] != 'try']
        elif not any(key == 'try' for key, _ in self._steps):
            self._steps.insert(-1, ('try', 'Try your settings'))
    monkeypatch.setattr(tutorial_qt.TutorialWindow, '_build_step_list', steps)
    build_page = tutorial_qt.TutorialWindow._build_page
    monkeypatch.setattr(tutorial_qt.TutorialWindow, '_build_page',
                        lambda self, key: QWidget() if key == 'try' else build_page(self, key))
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}})
    win = tutorial_qt.TutorialWindow(app)
    win._step = 1
    win._show_step()
    yield win
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_typed_or_pasted_text_lights_next(window):
    assert not window._next_btn.isEnabled()
    window._dictation_box.setPlainText('   ')
    assert not window._next_btn.isEnabled()
    window._dictation_box.setPlainText('hello world')
    assert window._next_btn.isEnabled()
    assert 'dictation' not in window._app._tutorial_hooks


@pytest.mark.parametrize('scale', [1.0, 1.5])
def test_label_rect_fits_font_metrics(qapp, scale):
    indicator = StepIndicator(['Welcome', 'Dictation', 'Command', 'Ways', 'Try', 'Done'])
    indicator.set_ui_scale(scale)
    indicator.resize(930, indicator.height())
    indicator.show()
    qapp.processEvents()
    fm = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * scale)))
    for index in range(indicator.count()):
        rect = QRectF(indicator.visualItemRect(indicator.item(index)))
        label_top = rect.top() + theme.TYPE_TITLE * scale * 1.6
        assert rect.bottom() - label_top >= fm.height()
        assert rect.width() >= fm.horizontalAdvance(indicator.steps[index])
    indicator.close()


def test_footer_common_height_and_quiet_skips(window, qapp):
    window.show()
    qapp.processEvents()
    buttons = (window._skip_all_btn, window._skip_step_btn, window._back_btn, window._next_btn)
    assert {button.height() for button in buttons} == {theme.HIT_TARGET_MIN}
    assert len({button.mapTo(window, button.rect().topLeft()).y() for button in buttons}) == 1
    window._show_hint('dictation')
    assert window._skip_step_btn.property('class') == 'secondary'
    assert window._skip_all_btn.property('class') == 'secondary'
    assert '💡' not in window._dict_hint.text()


def test_dictation_text_inset_matches_instruction(window, qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QLabel
    window.show()
    qapp.processEvents()
    page = window._pages[1]
    card = page.layout().itemAt(0).widget()
    label = card.findChild(QLabel)
    box = window._dictation_box
    text_x = box.viewport().mapTo(window, QPoint(0, 0)).x() + box.cursorRect().x()
    assert text_x == label.mapTo(window, QPoint(0, 0)).x()
    buttons = [window._skip_step_btn, window._back_btn, window._next_btn]
    rects = [(b.mapTo(window, QPoint(0, 0)).x(), b.width()) for b in buttons]
    assert rects[1][0] - sum(rects[0]) == rects[2][0] - sum(rects[1])
    assert window._skip_all_btn.mapTo(window, QPoint(0, 0)).x() == 28
    assert window.width() - sum(rects[2]) == 28

# Reuse 458's production Full display and its exact rest assertions (also
# used by 481); these are state assertions, not a mock cleanup-call check.
from tests.test_hold_display_returns_to_rest_458 import hold
from samsara.live_surface.model import VisibleForm, NoticeKind


def bind_delivery(app, window):
    from types import MethodType
    from samsara import paste
    callback = Mock(wraps=window._app._tutorial_hooks['dictation'])
    window._app._tutorial_hooks['dictation'] = callback
    app._tutorial_hooks = window._app._tutorial_hooks
    app._notify_tutorial = MethodType(compile_method(method('dictation.py', '_notify_tutorial')), app)
    real_paste = Mock(side_effect=AssertionError('real paste must not be called'))
    primer = Mock(side_effect=AssertionError('wake primer must not be called'))
    helpers = SimpleNamespace(paste_preserving_clipboard=real_paste,
        deliver_text_to_focused_editor=primer, show_delivery_result=paste.show_delivery_result)
    wrapper = compile_method(method('dictation.py', '_paste_preserving_clipboard'),
        _paste=helpers, paste_with_preservation=Mock(), type_text_unicode=Mock(),
        _get_foreground_hwnd=Mock())
    app._paste_preserving_clipboard = MethodType(wrapper, app)
    app._deliver_text_to_focused_editor = MethodType(compile_method(
        method('dictation.py', '_deliver_text_to_focused_editor'), _paste=helpers), app)
    return callback, real_paste, primer


def prepare_pending(h, app=None):
    app = app or h.app
    h.partials.stop()
    app.listening_indicator.set_mode('Toggle')
    app.listening_indicator.set_listening(True)
    app.listening_indicator.set_listening(False)
    app._show_hold_pending_chip()
    h.flush()


def assert_delivered(h, app, window, callback, real_paste, expected):
    h.flush()
    assert window._dictation_box.toPlainText() == expected
    assert window._next_btn.isEnabled()
    callback.assert_called_once()
    real_paste.assert_not_called()
    histories = [c for c in app._log_history.call_args_list if c.kwargs.get('status') == 'success']
    assert len(histories) == 1
    assert histories[0].kwargs['display_text'] == expected.strip()
    assert [c.args[0] for c in app.play_sound.call_args_list].count('success') == 1
    assert h.c.view().notice.message == 'typed'
    h.clock[0] = 2.0
    h.c.refresh_expired()
    assert h.c.widget._state.text() == 'Idle'
    assert h.c.view().form is VisibleForm.MARK
    assert h.c.view().notice.kind is NoticeKind.NONE


@pytest.mark.parametrize('parking,direct', [(True, False), (False, False), (False, True)])
def test_streaming_final_completes_ordinary_ending(window, hold, monkeypatch, parking, direct):
    from samsara import streaming
    h = hold
    callback, real_paste, _ = bind_delivery(h.app, window)
    h.session._parking_enabled = parking
    h.session._direct_paste = direct
    h.session._target_hwnd = None  # would park without the tutorial exemption
    h.session._paste_with_retry = lambda text: h.app._paste_preserving_clipboard(text)
    h.partials.partial('Synthetic preview.')
    h.app._schedule_ui(h.app.listening_indicator.set_listening, False)
    h.app._show_hold_pending_chip()
    h.flush()
    h.session.on_final('hello world', raw_text='hello world', duration_s=1)
    h.flush()
    h.app.add_to_history.assert_called_once_with('hello world', is_command=False)
    assert_delivered(h, h.app, window, callback, real_paste, 'hello world')


def test_streaming_partial_never_types_into_waiting_tutorial(window):
    partial = compile_method(method('samsara/streaming.py', '_direct_paste_partial'))
    partial(SimpleNamespace(app=window._app), 'partial words')
    assert window._dictation_box.toPlainText() == ''
    assert not window._next_btn.isEnabled()


def legacy_app(tmp_path, monkeypatch, h, *, command=False, text='hello world'):
    from tests import test_commands_only_key_391 as proof
    # Reuse its synthetic decoder/registry hands, but compile the exact methods
    # against the maintained adapter namespace. Its old default-config eval
    # uses dictation's filename, which the current bootstrap guard forbids.
    namespace = dict(h.app._show_outcome_chip.__func__.__globals__)
    namespace['thread_registry'] = SimpleNamespace(spawn=lambda name, fn, **kw: fn())
    names = ('_stop_recording_impl', '_take_recording_ownership', 'get_transcription_params',
        '_build_hotkey_transcribe_params', '_decode_hotkey_audio', '_resolve_command_miss',
        '_handle_session_dispatch_outcome', '_show_dispatch_outcome_chip',
        '_show_outcome_chip', '_show_hold_pending_chip', '_resolve_hold_chip')
    nodes = [method('dictation.py', name) for name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / 'dictation.py'), 'exec'), namespace)
    defaults = next(node.value for node in method('dictation.py', 'load_config').body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
            and target.id == 'default_config' for target in node.targets))
    config = eval(compile(ast.Expression(defaults), '<tutorial-default-config>', 'eval'), namespace)
    monkeypatch.setattr(proof, '_production', lambda: (namespace, config))
    app, spies = proof._app(tmp_path, monkeypatch, text, command_key=command)
    app.listening_indicator = h.app.listening_indicator
    app.live_surface = h.c
    app._schedule_ui = h.app._schedule_ui
    for name in ('_show_hold_pending_chip', '_show_outcome_chip'):
        setattr(app, name, getattr(h.app, name).__func__.__get__(app))
    # 391 predates 481's final retirement seam; bind its exact production method.
    app._finish_empty_hold_capture = compile_method(method(
        'dictation.py', '_finish_empty_hold_capture')).__get__(app)
    app._empty_hold_chip_seq = 0
    return app, spies


def test_legacy_final_completes_ordinary_ending(window, hold, tmp_path, monkeypatch):
    from tests.test_commands_only_key_391 import _run
    h = hold
    app, spies = legacy_app(tmp_path, monkeypatch, h)
    callback, real_paste, _ = bind_delivery(app, window)
    app._live_surface_hold_parking_enabled = lambda: True
    app._hold_capture_target_hwnd = None
    spies.namespace['_get_foreground_hwnd'] = lambda: 999
    prepare_pending(h, app)
    timing = Mock(outcome='typed')
    spies.namespace.update(set_current_stage_timing=Mock(), reset_current_stage_timing=Mock())
    app._stop_recording_impl(stage_timing=timing)
    timing.mark.assert_any_call('action_done', outcome='typed')
    timing.complete.assert_called_once_with(outcome='typed')
    assert_delivered(h, app, window, callback, real_paste, window._dictation_box.toPlainText())
    assert 'hello world' in window._dictation_box.toPlainText().lower()
    app.add_to_history.assert_called_once()


def test_continuous_final_completes_ordinary_ending(window, hold):
    import numpy as np
    import time
    h = hold
    app = h.app
    callback, real_paste, _ = bind_delivery(app, window)
    app.model_rate = 16000
    app.capture_rate = 16000
    app.model_lock = threading.Lock()
    app.model = SimpleNamespace(transcribe=lambda *a, **kw:
        ([SimpleNamespace(text='hello world')], SimpleNamespace(language='en')))
    app.get_transcription_params = lambda **kw: {}
    app._filter_dictation_language = lambda text, info: text
    app.voice_training_window = SimpleNamespace(apply_corrections=lambda text: text)
    app.command_executor = SimpleNamespace(process_text=lambda *a: ('', False))
    app.process_transcription = lambda text: text
    app.config.update(add_trailing_space=False)
    prepare_pending(h)
    transcribe = compile_method(method('dictation.py', 'transcribe_continuous_buffer'),
        np=np, time=time, resample_audio=lambda audio, *a: audio,
        clean_text=lambda text, **kw: text)
    transcribe(app, [np.ones(16000, dtype=np.float32)])
    assert_delivered(h, app, window, callback, real_paste, 'hello world')


def test_wake_final_completes_ordinary_ending(window, hold):
    import time
    from samsara import config_defaults
    h = hold
    app = h.app
    callback, real_paste, primer = bind_delivery(app, window)
    app.config.update(add_trailing_space=False, smart_corrections={'modes': {'wake': False}})
    app._apply_verbatim_if_active = lambda text: None
    app.process_transcription = lambda text: text
    app._apply_formatting_tokens = lambda text: text
    app.app_state = 'wake_session'
    app._wake_profile_active = True
    app._wake_session_target_process = 'stale-target'
    app._foreground_process_name = lambda: 'different-target'
    prepare_pending(h)
    timing, pending = Mock(), Mock()
    app._stage_timing_enabled = True
    app._wake_pending_stage_timings = [pending]
    deliver = compile_method(method('dictation.py', '_output_dictation'),
        current_stage_timing=lambda: timing,
        time=time, clean_text=lambda text, **kw: text,
        diagnostics=SimpleNamespace(record=Mock(), DiagRecord=Mock()),
        config_defaults=config_defaults, _languages=SimpleNamespace(describe_diagnostics_language=Mock()))
    assert deliver(app, 'hello world') is True
    timing.mark.assert_any_call('action_done', outcome='typed')
    pending.complete.assert_called_once_with(outcome='typed')
    app.add_to_history.assert_called_once_with('hello world', is_command=False)
    primer.assert_not_called()
    assert app._wake_session_first_chunk is False
    assert_delivered(h, app, window, callback, real_paste, 'hello world')


def test_wake_first_delivery_bypasses_primer_via_wrapper(window):
    app = window._app
    app.play_sound = Mock()
    callback, real_paste, primer = bind_delivery(app, window)
    assert app._deliver_text_to_focused_editor('hello world') is True
    callback.assert_called_once()
    real_paste.assert_not_called()
    primer.assert_not_called()
    app.play_sound.assert_called_once_with('success')


@pytest.mark.parametrize('lane', ['task', 'memo', 'command', 'ava', 'voice_memo'])
def test_other_capture_lanes_do_not_consume_dictation(window, hold, tmp_path, monkeypatch, lane):
    from tests.test_commands_only_key_391 import _run
    app, spies = legacy_app(tmp_path, monkeypatch, hold,
        command=lane == 'command', text='scroll down' if lane == 'command' else 'hello world')
    callback, real_paste, _ = bind_delivery(app, window)
    if lane in ('task', 'memo'):
        app._memo_recording = True
        app._capture_sink = 'task' if lane == 'task' else None
        if lane == 'task':
            from samsara import capture_sinks
            destination = Mock(return_value=True)
            monkeypatch.setattr(capture_sinks, 'deliver_task', destination)
        else:
            destination = Mock(return_value={})
            app.config['memo_retain_audio'] = False
            spies.namespace['quick_memo'] = SimpleNamespace(categories=lambda *a, **kw: [],
                split_category=lambda text, known: (None, text), add_memo=destination,
                SOURCE_VOICE='voice', CATEGORY_SPOKEN='spoken', mirror_to_vault=Mock(), prune_audio=Mock())
    elif lane == 'voice_memo':
        destination = Mock(return_value=True)
        spies.namespace['voice_memo'] = SimpleNamespace(is_armed=lambda cfg: True, capture=destination)
    elif lane == 'ava':
        app.ava_mode_recording = True
        app._route_to_ava = Mock()
    _run(app)
    callback.assert_not_called()
    real_paste.assert_not_called()
    assert app._tutorial_hooks.get('dictation') is callback
    assert window._dictation_box.toPlainText() == ''
    if lane in ('task', 'memo', 'voice_memo'):
        destination.assert_called_once()


def test_command_only_completes_command_step(window, hold, tmp_path, monkeypatch):
    from tests.test_commands_only_key_391 import _run
    window._step = 2
    window._show_step()
    app, _ = legacy_app(tmp_path, monkeypatch, hold, command=True, text='scroll down')
    callback = Mock(wraps=window._app._tutorial_hooks['command'])
    app._tutorial_hooks = {'command': callback}
    app._notify_tutorial = compile_method(method('dictation.py', '_notify_tutorial')).__get__(app)
    _run(app)
    hold.flush()
    callback.assert_called_once()
    assert window._next_btn.isEnabled()
    app._paste_preserving_clipboard.assert_not_called()


@pytest.mark.parametrize('feedback', [True, False, 'silent'])
@pytest.mark.parametrize('confirmation', [False, True])
def test_no_hook_forwards_exact_paste_arguments(feedback, confirmation):
    paste, inject, type_unicode, foreground, guard = (Mock() for _ in range(5))
    result = object()
    paste.return_value = result
    show = Mock()
    wrapper = compile_method(method('dictation.py', '_paste_preserving_clipboard'),
        _paste=SimpleNamespace(paste_preserving_clipboard=paste, show_delivery_result=show),
        paste_with_preservation=inject, type_text_unicode=type_unicode, _get_foreground_hwnd=foreground)
    app = SimpleNamespace()
    assert wrapper(app, 'hello world', before_paste=guard,
                   return_delivery_confirmation=confirmation, feedback=feedback) is result
    paste.assert_called_once_with(app, 'hello world', before_paste=guard,
        return_delivery_confirmation=confirmation, feedback=feedback,
        paste_with_preservation_fn=inject, type_text_unicode_fn=type_unicode,
        get_foreground_hwnd_fn=foreground)
    show.assert_not_called()


def test_snippet_or_ava_edit_paste_cannot_consume_waiting_dictation(window):
    app = window._app
    callback, real_paste, _ = bind_delivery(app, window)
    real_paste.side_effect = None
    real_paste.return_value = True
    assert app._paste_preserving_clipboard('Synthetic snippet.') is True
    callback.assert_not_called()
    real_paste.assert_called_once()
    assert app._tutorial_hooks['dictation'] is callback
    assert window._dictation_box.toPlainText() == ''


def test_hands_free_delivery_confirmation_shape(window):
    app = window._app
    callback, real_paste, _ = bind_delivery(app, window)
    assert app._paste_preserving_clipboard('hello world', tutorial_dictation=True,
        return_delivery_confirmation=True, before_paste=Mock(return_value=False)) == (True, True)
    callback.assert_called_once()
    real_paste.assert_not_called()
    assert window._dictation_box.toPlainText() == 'hello world'
