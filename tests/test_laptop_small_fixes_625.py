"""625: bounded wake follow-ups, passive topmost repair, memory and Qt lifetime."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def followup(monkeypatch):
    from samsara import numbers_followup
    from plugins.commands import show_numbers
    active = [True]
    callbacks = []
    monkeypatch.setattr(show_numbers, 'is_overlay_active', lambda: active[0])
    monkeypatch.setattr(numbers_followup.thread_registry, 'timer',
                        lambda name, delay, fn, **kw: callbacks.append(fn))
    app = SimpleNamespace(app_state='command_window', wake_word_triggered=False,
                          _start_wake_timeout=Mock(), wake_word_timer=Mock(),
                          _indicator_reset=Mock())
    return numbers_followup, app, active, callbacks


def test_overlay_arms_followup_and_dismissal_closes_it(followup):
    module, app, active, callbacks = followup
    assert module.keep_numbers_window(app)
    assert app.wake_word_triggered
    app._start_wake_timeout.assert_called_once_with(30.0)
    callbacks.pop(0)()
    assert app.app_state == 'command_window'
    active[0] = False
    callbacks.pop(0)()
    assert app.app_state == 'asleep'
    assert not app.wake_word_triggered
    app.wake_word_timer.cancel.assert_called_once()


def test_no_overlay_leaves_normal_wake_behavior(followup):
    module, app, active, callbacks = followup
    active[0] = False
    assert not module.keep_numbers_window(app)
    assert not callbacks


def test_old_overlay_callback_does_not_close_new_window(followup):
    module, app, active, callbacks = followup
    module.keep_numbers_window(app)
    stale = callbacks.pop(0)
    module.keep_numbers_window(app)
    stale()
    assert app.wake_word_triggered
    app.app_state = 'long_dictation'
    callbacks.pop(0)()
    assert app.app_state == 'long_dictation'


def test_followup_has_a_hard_deadline(followup, monkeypatch):
    module, app, active, callbacks = followup
    clock = [0.0]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    module.keep_numbers_window(app)
    clock[0] = 31.0
    callbacks.pop(0)()
    assert app.app_state == 'asleep'


@pytest.mark.parametrize('text', ['show numbers', '3', 'double click 3'])
def test_real_wake_dispatch_retains_overlay_window(followup, text):
    # Extract only the method: importing dictation bootstraps the live app.
    module, app, active, callbacks = followup
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_process_wake_command')
    namespace = dict(logger=logging.getLogger('625'), describe_text=lambda value: '<private>',
                     parse_wake_command=lambda value: dict(type='command_text', name='', content=''),
                     current_stage_timing=lambda: None)
    exec(compile(ast.Module(body=[method], type_ignores=[]), 'wake625', 'exec'), namespace)
    outcome = SimpleNamespace(kind='command_executed')
    dispatch = Mock(return_value=outcome)
    app._ensure_session_mode_manager = lambda: SimpleNamespace(dispatch_command_utterance=dispatch)
    app._resolve_command_miss = lambda result, value: result
    app._handle_session_dispatch_outcome = Mock()
    namespace['_process_wake_command'](app, text)
    dispatch.assert_called_once_with(text)
    assert app.wake_word_triggered and app.app_state == 'command_window'


@pytest.mark.parametrize('cloud', [False, True])
def test_unavailable_summary_is_quiet_and_pending_is_preserved(tmp_path, monkeypatch, caplog, cloud):
    from plugins.commands import ask_ollama
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    app = SimpleNamespace(config={'ava_memory': {'mode': 'assistant'},
                                   'ollama': {'model': 'test'},
                                   'ava': {'provider_policy': 'cloud' if cloud else 'local'}})
    app._ava_memory = ask_ollama._build_ava_memory(app)
    post = Mock(side_effect=ConnectionError('fixture server unavailable'))
    monkeypatch.setattr(ask_ollama.ai_transport, 'post_local', post)
    for _ in range(2):
        app._ava_memory.add_user('I like gardening')
        ask_ollama._finish_ava_session(app, wait=True)
    assert post.call_count == (0 if cloud else 1)
    assert ask_ollama._personal_memory(app).pending_sessions()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING and 'AVA MEMORY' in r.message]


def test_deleted_layout_is_safe_to_refresh(qapp):
    from PySide6.QtWidgets import QWidget, QVBoxLayout
    from shiboken6 import delete
    from samsara.ui.settings.calm_layout import RowDividers
    host = QWidget()
    layout = QVBoxLayout(host)
    dividers = RowDividers(layout)
    delete(layout)
    assert dividers.collect() == []
    dividers.refresh()
    host.close()


def test_summary_backoff_recovers_and_disabled_local_is_skipped(monkeypatch):
    from samsara import ava_summary_policy
    clock = [10.0]
    monkeypatch.setattr(ava_summary_policy, 'monotonic', lambda: clock[0])
    app = SimpleNamespace(config={'ollama': {'enabled': True}})
    assert ava_summary_policy.summary_available(app)
    ava_summary_policy.defer_summary(app)
    assert not ava_summary_policy.summary_available(app)
    clock[0] += 301
    assert ava_summary_policy.summary_available(app)
    app.config['ollama']['enabled'] = False
    assert not ava_summary_policy.summary_available(app)


def test_qt_lifetime_exception_is_diagnostic_but_arbitrary_runtime_text_is_private():
    from samsara.log import describe_text
    message = 'Internal C++ object (PySide6.QtWidgets.QVBoxLayout) already deleted.'
    assert message in describe_text(RuntimeError(message))
    assert 'fixture private text' not in describe_text(RuntimeError('fixture private text'))
    assert 'fixture private text' not in describe_text(RuntimeError(message + ' fixture private text'))


@pytest.mark.parametrize('full', [True, False])
def test_both_displays_repair_topmost_and_stop_when_hidden(qapp, monkeypatch, full):
    from samsara.ui import passive_topmost
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    from samsara.live_surface.model import LiveSurfaceModel
    repair = Mock()
    monkeypatch.setattr(passive_topmost, 'restore', repair)
    widget = LiveSurfaceWidget(LiveSurfaceModel()) if full else ListeningIndicator()
    widget.show()
    qapp.processEvents()
    assert widget._topmost_guard.timer.isActive()
    repair.assert_called_with(widget)
    widget.hide()
    assert not widget._topmost_guard.timer.isActive()
    widget.close()


def test_native_topmost_call_has_no_activation_or_geometry_change(monkeypatch):
    from samsara.ui import passive_topmost
    set_pos = Mock()
    monkeypatch.setattr(passive_topmost.sys, 'platform', 'win32')
    monkeypatch.setattr(passive_topmost.ctypes, 'windll',
                        SimpleNamespace(user32=SimpleNamespace(SetWindowPos=set_pos)))
    widget = SimpleNamespace(isVisible=lambda: True, winId=lambda: 2**40)
    passive_topmost.restore(widget)
    set_pos.assert_called_once_with(2**40, -1, 0, 0, 0, 0, 0x213)
    widget.isVisible = lambda: False
    passive_topmost.restore(widget)
    assert set_pos.call_count == 1
