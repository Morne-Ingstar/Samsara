"""LP05: real shared composition, isolated ports, no app bootstrap/processes."""
import ast
import builtins
import importlib.util
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

from samsara import headless

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_profile(monkeypatch, tmp_path):
    profile = tmp_path / 'profile'
    for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA', 'LOCALAPPDATA'):
        monkeypatch.setenv(key, str(profile))
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    return profile


def test_package_import_is_lazy_and_keeps_export_names():
    spec = importlib.util.spec_from_file_location('_lp05_package', ROOT / 'samsara' / '__init__.py')
    package = importlib.util.module_from_spec(spec)
    before = set(sys.modules)
    spec.loader.exec_module(package)
    assert package.__all__ == ['CommandExecutor', 'SplashScreen']
    assert all(name in dir(package) for name in package.__all__)
    assert 'CommandExecutor' not in package.__dict__
    assert 'SplashScreen' not in package.__dict__
    assert set(sys.modules) == before
    with pytest.raises(AttributeError):
        package.missing_export


def test_public_exports_resolve_and_required_import_failure_propagates(monkeypatch):
    import samsara
    from samsara.commands import CommandExecutor
    assert samsara.CommandExecutor is CommandExecutor
    sentinel = object()
    monkeypatch.setitem(sys.modules, 'samsara.ui.splash_qt', SimpleNamespace(SplashScreenQt=sentinel))
    assert samsara.SplashScreen is sentinel
    original = builtins.__import__

    def fail_commands(name, *args, **kwargs):
        if name == 'commands':
            raise RuntimeError('required import failed')
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fail_commands)
    with pytest.raises(RuntimeError, match='required import failed'):
        samsara.__getattr__('CommandExecutor')


def test_actual_graph_uses_canonical_settings_handlers_and_matcher(isolated_profile):
    from samsara.commands import CommandExecutor
    from samsara.config_defaults import DEFAULTS
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.handlers import build_handler_registry
    app = SimpleNamespace(config={})
    before = set(threading.enumerate())
    with headless.deny_effects() as attempts:
        graph = headless.compose_services(app, resources=headless.ResourcePorts())
        entry, _ = graph.command_executor._matcher.match('copy')
    assert not attempts
    assert isinstance(graph.command_executor, CommandExecutor)
    assert graph.command_executor._app is app
    assert graph.settings_schema is SETTINGS_SCHEMA
    assert graph.settings_defaults is DEFAULTS
    assert graph.handlers == build_handler_registry()
    assert entry is not None and entry.source == 'builtin'
    assert set(threading.enumerate()) == before
    assert not isolated_profile.exists()


def test_injected_registry_is_composed_by_the_production_matcher(monkeypatch):
    from samsara import plugin_commands
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES', {})

    @plugin_commands.command('lp05 fixture command', pack='core')
    def fixture_command(app, remainder):
        return True

    ports = headless.ResourcePorts({'plugin_registry': headless.ResourcePort(plugin_commands._REGISTRY)})
    graph = headless.compose_services(SimpleNamespace(config={}), resources=ports)
    entry, _ = graph.command_executor._matcher.match('lp05 fixture command')
    assert entry is not None and entry.source == 'plugin'
    assert fixture_command is plugin_commands._REGISTRY['lp05 fixture command']['func']


def test_denied_controllers_cannot_report_fake_success():
    graph = headless.compose_services(SimpleNamespace(config={}), resources=headless.ResourcePorts())
    with pytest.raises(headless.ResourceUnavailable):
        graph.command_executor.keyboard_controller.press('a')
    with pytest.raises(headless.ResourceUnavailable):
        graph.command_executor.mouse_controller.click('left')


def test_desktop_factory_and_patch_seams_keep_delegation(monkeypatch):
    from samsara import commands
    calls = []
    keyboard = SimpleNamespace(press=lambda key: calls.append(('press', key)))
    mouse = SimpleNamespace(click=lambda button: calls.append(('click', button)))
    monkeypatch.setattr(commands, 'KeyboardController', lambda: calls.append('keyboard') or keyboard)
    monkeypatch.setattr(commands, 'MouseController', lambda: calls.append('mouse') or mouse)
    monkeypatch.setattr(commands, 'get_enabled_packs', lambda config: {'core'})
    monkeypatch.setattr(commands._plugin_commands, 'load_plugins', lambda path: calls.append('load'))
    monkeypatch.setattr(commands._plugin_commands, 'start_plugin_services', lambda app: calls.append('services'))
    monkeypatch.setattr(commands._plugin_commands, 'set_shared_matcher', lambda matcher: calls.append('matcher'))
    graph = headless.compose_services(SimpleNamespace(config={}), executor_factory=commands.CommandExecutor)
    assert calls == ['load', 'matcher', 'services', 'matcher']
    graph.command_executor.keyboard_controller.press('a')
    graph.command_executor.mouse_controller.click('left')
    assert calls[-4:] == ['keyboard', ('press', 'a'), 'mouse', ('click', 'left')]


def test_native_key_map_resolves_original_members_on_demand(monkeypatch):
    from samsara import commands
    names = ('ctrl shift alt cmd enter esc space tab backspace delete home end '
             'page_up page_down up down left right f1 f2 f3 f4 f5 f6 f7 f8 f9 f10 f11 f12').split()
    keys = SimpleNamespace(**{name: object() for name in names})
    monkeypatch.setattr(commands, 'Key', keys)
    assert commands.CommandExecutor.KEY_MAP['win'] is keys.cmd
    assert commands.CommandExecutor.KEY_MAP['pageup'] is keys.page_up
    assert commands.CommandExecutor.KEY_MAP['f12'] is keys.f12


def test_pack_probe_is_injected_without_changing_default_desktop_policy(monkeypatch):
    from samsara.command_packs import get_enabled_packs
    calls = []
    monkeypatch.setitem(sys.modules, 'samsara.agent_channel.hermes', SimpleNamespace(
        permitted=lambda config: calls.append(('permitted', config)) or True,
        installed=lambda: calls.append('installed') or True))
    config = {'command_packs': {'hermes': True}}
    assert 'hermes' in get_enabled_packs(config)
    assert calls == [('permitted', config), 'installed']
    calls.clear()
    assert 'hermes' not in get_enabled_packs(config, hermes_available=lambda config: False)
    assert not calls


@pytest.mark.parametrize('effect', ['file', 'thread', 'network', 'model'])
def test_effect_guard_denies_acquisition_and_restores_it(effect, tmp_path):
    original_open = builtins.open
    original_start = threading.Thread.start
    with headless.deny_effects() as attempts:
        with pytest.raises(headless.HeadlessEffectError):
            if effect == 'file':
                (tmp_path / 'forbidden').write_text('fixture', encoding='utf-8')
            elif effect == 'thread':
                threading.Thread(target=lambda: None).start()
            elif effect == 'network':
                with socket.socket() as connection:
                    connection.connect(('127.0.0.1', 9))
            else:
                # Exercise the import guard directly even if a test adapter has
                # already supplied a model module; no backend is imported.
                sys.meta_path[0].find_spec('faster_whisper')
    assert len(attempts) == 1
    assert not (tmp_path / 'forbidden').exists()
    assert builtins.open is original_open and threading.Thread.start is original_start


def test_required_data_failure_is_reported_not_swallowed(tmp_path):
    missing = headless.check(commands_path=tmp_path / 'missing.json')
    assert missing['ok'] is False
    assert missing['services']['command_registry']['state'] == 'unavailable'
    assert 'FileNotFoundError' in missing['services']['command_registry']['reason']


def test_main_reports_core_and_resource_states_without_effects(capsys, isolated_profile):
    before = set(threading.enumerate())
    assert headless.main(['--headless-check']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['scope'] == 'import/composition only'
    assert result['services']['command_registry']['entries'] > 0
    assert result['services']['settings_model']['entries'] > 0
    assert result['services']['command_handlers']['state'] == 'ready'
    for name in headless.RESOURCE_NAMES:
        assert result['services'][name]['state'] == 'unavailable'
        assert result['services'][name]['reason']
    assert not isolated_profile.exists()
    assert set(threading.enumerate()) == before


def test_main_returns_nonzero_for_composition_failure(monkeypatch, capsys):
    def failed_factory(*args, **kwargs):
        raise ImportError('fixture failure')
    monkeypatch.setattr(headless, 'compose_services', failed_factory)
    assert headless.main(['--headless-check']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False
    assert 'ImportError' in result['services']['command_registry']['reason']
    assert headless.main(['--headless-check', '--fetch-components']) == 2


def test_entry_dispatch_and_shared_composition_are_early_source_seams():
    source = (ROOT / 'dictation.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_dispatch_startup_argument')
    assert '--headless-check' in ast.unparse(dispatch)
    assert any(isinstance(node, ast.ImportFrom) and node.module == 'samsara.headless'
               for node in ast.walk(dispatch))
    call = source.index('_dispatch_startup_argument(sys.argv[1:])')
    for later in ('_enable_faulthandler(', '_previous_early_startup()', 'import sounddevice',
                  'import samsara.torch_guard', 'class DictationApp'):
        assert call < source.index(later)
    assert 'compose_services(self, commands_path, executor_factory=CommandExecutor)' in source
    assert not any(isinstance(node, ast.Import) and any(alias.name in {'keyboard', 'pyautogui'}
                   for alias in node.names) or isinstance(node, ast.ImportFrom)
                   and (node.module or '').startswith('pynput') for node in tree.body)
