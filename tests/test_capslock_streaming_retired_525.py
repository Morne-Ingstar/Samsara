"""Retired capture stays off; shared dictation previews remain available."""
import ast
import copy
import json
from pathlib import Path
import threading
import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.config_schema import SETTINGS_SCHEMA, migrate_removed_settings, retire_streaming_mode
from samsara import hotkeys

ROOT = Path(__file__).resolve().parents[1]


def bind_method(name, app):
    """Bind an unchanged production method without importing the bootstrap."""
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == name)
    helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_three_way_merge']
    namespace = dict(copy=copy, logger=Mock(), _MISSING=object())
    exec(compile(ast.Module(body=helpers + [method], type_ignores=[]), 'dictation.py', 'exec'), namespace)
    return MethodType(namespace[name], app)


@pytest.mark.parametrize('stored', [True, False])
def test_saved_flag_is_off_before_capslock_install(tmp_path, monkeypatch, stored):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'streaming_mode': stored, 'mode': 'hold'}), encoding='utf-8')
    config = json.loads(path.read_text(encoding='utf-8'))
    migrate_removed_settings(config)
    assert config['streaming_mode'] is False
    hook = Mock()
    monkeypatch.setattr(hotkeys.keyboard, 'hook_key', hook)
    app = SimpleNamespace(config=config, _capslock_hook=None)
    hotkeys.HotkeyCluster._install_capslock_hook(app)
    hook.assert_not_called()
    assert app._capslock_hook is None
    assert retire_streaming_mode(config) is False


def test_external_reload_cannot_restore_saved_true():
    app = SimpleNamespace(config={'streaming_mode': False}, _config_lock=threading.Lock(),
                          _config_last_disk_snapshot={'streaming_mode': False})
    reload_config = bind_method('_apply_disk_config', app)
    assert reload_config({'streaming_mode': True}) == 0
    assert app.config['streaming_mode'] is False
    assert reload_config({'streaming_mode': True}) == 0


def test_legacy_setter_cannot_enable_capture():
    app = SimpleNamespace(config={'streaming_mode': False}, _config_lock=threading.Lock(),
                          _capslock_lifecycle_lock=threading.Lock(), _streaming_session=None,
                          _install_capslock_hook=Mock(), _uninstall_capslock_hook=Mock(), save_config=Mock())
    bind_method('set_streaming_mode', app)(True)
    assert app.config['streaming_mode'] is False
    app._install_capslock_hook.assert_not_called()
    app.save_config.assert_not_called()


def test_capslock_events_do_not_start_workers_after_normalization(monkeypatch):
    config = {'streaming_mode': True}
    retire_streaming_mode(config)
    app = SimpleNamespace(config=config, snoozed=False, model_loaded=True, _capslock_held=False,
                          _capslock_lifecycle_lock=threading.Lock(), start_recording=Mock())
    spawn = Mock()
    monkeypatch.setattr(hotkeys.thread_registry, 'spawn', spawn)
    monkeypatch.setattr(hotkeys.hold_key_rules, 'route_capture', lambda *args: False)
    for event_type in (hotkeys.keyboard.KEY_DOWN, hotkeys.keyboard.KEY_UP):
        hotkeys.HotkeyCluster._on_capslock_event(app, SimpleNamespace(event_type=event_type))
    hotkeys.HotkeyCluster._capslock_start_streaming(app)
    spawn.assert_not_called()
    app.start_recording.assert_not_called()


def test_retired_key_has_explicit_schema_coverage():
    spec = SETTINGS_SCHEMA['streaming_mode']
    assert spec['default'] is False
    assert spec['settings_ui'] is False
    assert spec['ui_less_reason'].strip()


@pytest.mark.parametrize('query', ['Streaming capture', 'streaming_mode', 'streaming_key_display', 'CapsLock'])
def test_settings_has_no_retired_control_or_search_result(qapp, monkeypatch, tmp_path, query):
    from tests.test_settings_search_312 import _window
    window = _window(monkeypatch, tmp_path)
    try:
        assert not {'streaming_mode', 'streaming_key_display'} & window._widgets.keys()
        assert not any(key == 'streaming_mode' for key, _, _ in window.config_row_inventory())
        window.show_tab('General')
        window._pages['General'].advanced.reveal()
        window._search_edit.setText(query)
        window._apply_search_filter()
        qapp.processEvents()
        assert not any(query.casefold() in (title + ' ' + help_text).casefold()
                       for title, help_text, *_ in window._search_rows)
    finally:
        window.deleteLater()


def test_ordinary_full_hold_still_uses_shared_live_capture(monkeypatch):
    from tests.test_dictation_surface_recovery import capture_app
    import samsara.streaming as streaming
    # Reuse the existing production-AST fixture; no desktop module is imported.
    app, _, _ = capture_app.__wrapped__()
    from samsara import listening_display
    namespace = app._start_recording_impl.__func__.__globals__
    monkeypatch.setitem(namespace, 'listening_display', listening_display)
    monkeypatch.setitem(namespace, 'time', time)
    app.config['streaming_mode'] = False
    app.config['ui']['listening_display'] = 'full'
    app.live_surface = object()
    app._dictation_consumer.activate_streaming = Mock(return_value=True)
    session = Mock()
    monkeypatch.setattr(streaming, 'StreamingSession', Mock(return_value=session))
    assert app._start_recording_impl(streaming=False, play_earcon=False)
    app._dictation_consumer.activate_streaming.assert_called_once()
    session.start.assert_called_once()


def test_initial_load_normalizes_before_the_hook_is_installed():
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'DictationApp')
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '__init__')
    load = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'load_config')
    calls = {node.func.attr: node.lineno for node in ast.walk(init)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr in {'load_config', '_install_capslock_hook'}}
    assert calls['load_config'] < calls['_install_capslock_hook']
    # Loaded settings retire through migrate_loaded_config's current migration chain.
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == 'migrate_loaded_config' for node in ast.walk(load))
