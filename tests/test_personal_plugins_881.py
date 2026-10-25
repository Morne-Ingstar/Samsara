"""External personal commands stay isolated from bundled commands and profiles."""
import json
import logging
import sys
from types import SimpleNamespace

import pytest

from samsara import plugin_commands as plugins
from samsara.config_schema import SETTINGS_SCHEMA


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.startswith(('samsara_personal_', 'samsara_plugin_')):
            sys.modules.pop(name, None)


def write_plugin(directory, name='example.py', phrase='synthetic greeting', aliases=(), tail=''):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(
        'from samsara.plugin_commands import command\n'
        'IMPORTS = globals().get("IMPORTS", 0) + 1\n'
        f'@command({phrase!r}, aliases={list(aliases)!r})\n'
        'def greet(app, remainder):\n'
        '    return True\n' + tail, encoding='utf-8')
    return path


@pytest.mark.parametrize('exists', [False, True])
def test_absent_or_empty_folder_preserves_registry_exactly(tmp_path, exists):
    plugins.command('bundled fixture', aliases=['bundled alias'])(lambda app, text: True)
    if exists:
        (tmp_path / 'personal_plugins').mkdir()
    registry = dict(plugins._REGISTRY)
    declarations = {name: dict(entries) for name, entries in plugins._MODULE_ENTRIES.items()}
    loaded = dict(plugins._LOADED_MODULES)
    assert plugins.load_extra_plugins() == 0
    assert plugins._REGISTRY == registry
    assert all(plugins._REGISTRY[key] is entry for key, entry in registry.items())
    assert plugins._MODULE_ENTRIES == declarations
    assert plugins._LOADED_MODULES == loaded
    assert (tmp_path / 'personal_plugins').exists() is exists


@pytest.mark.parametrize('configured', ['', 'custom-commands', 'absolute'])
def test_default_and_configured_folder_register_personal_source(tmp_path, configured):
    directory = tmp_path / (configured or 'personal_plugins')
    setting = str(directory) if configured == 'absolute' else configured
    write_plugin(directory, aliases=['synthetic alias'])
    assert plugins.load_extra_plugins({'plugins': {'personal_dir': setting}}) == 1
    entry = plugins._REGISTRY['synthetic greeting']
    assert entry['source'] == 'personal'
    assert plugins._REGISTRY['synthetic alias'] is entry
    assert plugins.list_commands()[0]['source'] == 'personal'
    assert entry['func'](None, '') is True


@pytest.mark.parametrize('error', ['RuntimeError("fixture failure")', 'SystemExit(1)', 'SyntaxError()'])
def test_broken_plugin_is_logged_rolled_back_and_next_file_loads(tmp_path, caplog, error):
    directory = tmp_path / 'personal_plugins'
    write_plugin(directory, name='a_broken.py', phrase='partial fixture', tail=f'raise {error}\n')
    write_plugin(directory, name='b_working.py')
    with caplog.at_level(logging.WARNING, logger=plugins.__name__):
        assert plugins.load_extra_plugins() == 1
    assert 'partial fixture' not in plugins._REGISTRY
    assert all('partial fixture' not in entries for entries in plugins._MODULE_ENTRIES.values())
    assert len(plugins._LOADED_MODULES) == 1
    assert 'synthetic greeting' in plugins._REGISTRY
    assert 'a_broken.py' in caplog.text and 'skipped' in caplog.text


def test_invalid_python_is_skipped(tmp_path, caplog):
    path = write_plugin(tmp_path / 'personal_plugins')
    path.write_text('def invalid(:\n', encoding='utf-8')
    assert plugins.load_extra_plugins() == 0
    assert plugins._REGISTRY == {}
    assert 'SyntaxError' in caplog.text


@pytest.mark.parametrize('primary, aliases', [
    ('bundled fixture', []),
    ('bundled alias', []),
    ('synthetic greeting', ['bundled fixture']),
    ('synthetic greeting', ['bundled alias']),
])
@pytest.mark.parametrize('bundled_plugin', [False, True])
def test_collision_rejects_entire_personal_command(tmp_path, caplog, primary, aliases, bundled_plugin):
    builtins = {'Bundled Fixture': {'aliases': ['Bundled Alias']}}
    if bundled_plugin:
        plugins.command('bundled fixture', aliases=['bundled alias'])(lambda app, text: True)
        builtins = {}
    registry = dict(plugins._REGISTRY)
    write_plugin(tmp_path / 'personal_plugins', phrase=primary, aliases=aliases)
    assert plugins.load_extra_plugins(builtin_commands=builtins) == 1
    assert plugins._REGISTRY == registry
    assert 'collision' in caplog.text


def test_same_filename_uses_separate_module_and_repeat_load_reuses_it(tmp_path):
    bundled = tmp_path / 'bundled'
    write_plugin(bundled, phrase='bundled fixture')
    plugins.load_plugins(bundled)
    original = plugins._REGISTRY['bundled fixture']
    write_plugin(tmp_path / 'personal_plugins')
    assert plugins.load_extra_plugins() == 1
    personal = plugins._REGISTRY['synthetic greeting']
    module = sys.modules[personal['func'].__module__]
    assert plugins.load_extra_plugins() == 1
    assert module.IMPORTS == 1
    assert plugins._REGISTRY['bundled fixture'] is original
    assert plugins._REGISTRY['synthetic greeting'] is personal
    plugins._REGISTRY.clear()
    assert plugins.load_extra_plugins() == 1
    assert plugins._REGISTRY['synthetic greeting'] is personal
    assert module.IMPORTS == 1


def test_frozen_external_source_import_does_not_need_package_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path / 'frozen-bundle'), raising=False)
    path = write_plugin(tmp_path / 'personal_plugins')

    def reject_package_discovery(path):
        pytest.fail('external source must not depend on frozen package discovery')

    monkeypatch.setattr(plugins, '_canonical_module_name', reject_package_discovery)
    assert plugins.load_extra_plugins() == 1
    entry = plugins._REGISTRY['synthetic greeting']
    module = sys.modules[entry['func'].__module__]
    assert module.__file__ == str(path)
    assert module.__spec__.loader.__class__.__name__ == 'SourceFileLoader'
    assert plugins.execute_command('synthetic greeting') == ('synthetic greeting', True)


def test_executor_loads_after_bundled_plugins_and_preserves_builtin(tmp_path):
    from samsara.commands import CommandExecutor

    bundled = tmp_path / 'bundled'
    write_plugin(bundled, name='core.py', phrase='bundled fixture')
    directory = tmp_path / 'custom-commands'
    write_plugin(directory, name='a_collision.py', phrase='builtin fixture')
    write_plugin(directory, name='b_collision.py', phrase='bundled fixture')
    write_plugin(directory, name='c_working.py')
    commands = tmp_path / 'commands.json'
    commands.write_text(json.dumps({'commands': {'builtin fixture': {'type': 'hotkey', 'keys': 'x'}}}))
    app = SimpleNamespace(config={'plugins': {'personal_dir': 'custom-commands'}})
    executor = CommandExecutor(commands, app=app, plugins_dir=bundled)
    assert 'builtin fixture' not in plugins._REGISTRY
    assert plugins._REGISTRY['bundled fixture']['source'] != 'personal'
    assert executor._matcher.match('builtin fixture')[0].source == 'builtin'
    assert executor._matcher.match('synthetic greeting')[0].handler(None, '') is True


def test_schema_keeps_personal_folder_config_file_editable():
    setting = SETTINGS_SCHEMA['plugins.personal_dir']
    assert setting['type'] == 'str' and setting['default'] == ''
    assert setting['settings_ui'] is False and setting['ui_less_reason']


@pytest.mark.parametrize('remainder, route_name, expected', [
    ('', 'EXACT', 'FAILED'),
    ('ordinary prose', 'EXACT', 'MISS'),
    ('ordinary prose', 'MODEL', 'FAILED'),
])
def test_personal_declines_keep_remainder_guard_without_private_names(
        tmp_path, monkeypatch, remainder, route_name, expected):
    from samsara import commands
    from samsara.command_registry import DispatchState
    from samsara.execution_policy import Route

    path = write_plugin(tmp_path / 'personal_plugins')
    path.write_text(path.read_text(encoding='utf-8').replace('return True', 'return False'),
                    encoding='utf-8')
    command_file = tmp_path / 'commands.json'
    command_file.write_text('{"commands": {}}', encoding='utf-8')
    monkeypatch.setattr(commands.execution_policy, 'authorize',
                        lambda *args, **kwargs: commands.execution_policy.Allowed())
    executor = commands.CommandExecutor(command_file, plugins_dir=tmp_path / 'absent')
    result = executor.execute_canonical(
        'synthetic greeting', SimpleNamespace(config={}),
        route=getattr(Route, route_name), args={'remainder': remainder},
        source_text='synthetic greeting ' + remainder)
    assert result.state is getattr(DispatchState, expected)
