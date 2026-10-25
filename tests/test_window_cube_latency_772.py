"""772: fresh process names, bounded executable metadata cache, immediate references."""
import json
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest
import win32api

from plugins.commands import window_cube as cube
from samsara import execution_policy as policy


@pytest.fixture
def processes(monkeypatch, tmp_path):
    cube._file_description.cache_clear()
    cube._alias_names.cache_clear()
    executable = tmp_path / 'editor.exe'
    executable.write_bytes(b'first')
    live = {1: ('editor.exe', str(executable))}
    calls = []

    def process(pid):
        if pid not in live:
            raise psutil.NoSuchProcess(pid)
        name, path = live[pid]
        return SimpleNamespace(name=lambda: name, exe=lambda: path)

    def version(path, key):
        calls.append((path, key))
        return [(1033, 1200)] if key.endswith('Translation') else 'Friendly Editor'

    monkeypatch.setattr(cube, '_get_pid', lambda hwnd: hwnd)
    monkeypatch.setattr(psutil, 'Process', process)
    monkeypatch.setattr(win32api, 'GetFileVersionInfo', version)
    yield live, calls, executable
    cube._file_description.cache_clear()
    cube._alias_names.cache_clear()


def test_same_executable_uses_one_version_lookup(processes):
    live, calls, path = processes
    live[2] = live[1]
    assert cube._friendly_app_name(1) == cube._friendly_app_name(2) == 'Friendly Editor'
    assert len(calls) == 2  # translation table and description, once per file


def test_renamed_process_is_not_a_stale_pid_name(processes):
    live, _, _ = processes
    assert cube._friendly_app_name(1) == 'Friendly Editor'
    live[1] = ('brave.exe', '')
    assert cube._friendly_app_name(1) == 'Brave'


def test_closed_process_and_reused_pid_are_fresh(processes):
    live, _, _ = processes
    assert cube._friendly_app_name(1) == 'Friendly Editor'
    del live[1]
    assert cube._friendly_app_name(1) == 'Window'
    live[1] = ('explorer.exe', '')
    assert cube._friendly_app_name(1) == 'File Explorer'


def test_new_window_gets_a_name(processes):
    live, _, _ = processes
    assert cube._friendly_app_name(2) == 'Window'
    live[2] = ('obsidian.exe', '')
    assert cube._friendly_app_name(2) == 'Obsidian'


def test_replaced_executable_invalidates_description(processes, monkeypatch):
    _, calls, path = processes
    assert cube._friendly_app_name(1) == 'Friendly Editor'
    path.write_bytes(b'replaced executable with new metadata')
    monkeypatch.setattr(win32api, 'GetFileVersionInfo', lambda path, key:
                        [(1033, 1200)] if key.endswith('Translation') else 'New Editor')
    assert cube._friendly_app_name(1) == 'New Editor'


def test_failed_description_lookup_is_not_cached(processes, monkeypatch):
    def denied(*args):
        raise OSError('unavailable')
    monkeypatch.setattr(win32api, 'GetFileVersionInfo', denied)
    assert cube._friendly_app_name(1) == 'Editor'
    monkeypatch.setattr(win32api, 'GetFileVersionInfo', lambda path, key:
                        [(1033, 1200)] if key.endswith('Translation') else 'Recovered Editor')
    assert cube._friendly_app_name(1) == 'Recovered Editor'


def test_alias_inversion_keeps_first_alias(processes, monkeypatch):
    from plugins.commands import windows
    monkeypatch.setattr(windows, 'APP_ALIASES', {'first': 'unique.exe', 'second': 'unique.exe'})
    assert cube._alias_names()['unique.exe'] == 'First'
    assert cube._alias_names() is cube._alias_names()


@pytest.mark.parametrize('phrase', ['show commands', 'hide commands', 'show tutorial',
                                  'samsara command reference', 'samsara settings',
                                  'samsara history', 'samsara quick reference'])
def test_real_builtin_reference_has_no_cancel_window(phrase):
    data = json.loads((Path(__file__).resolve().parents[1] / 'commands.json').read_text(encoding='utf-8'))
    app = SimpleNamespace(config={}, command_executor=SimpleNamespace(commands=data['commands']))
    assert phrase in app.command_executor.commands
    assert policy.classify(phrase, app=app)[0] == policy.RISK_READ
    assert policy.cancel_window_for(phrase, app) == 0


@pytest.mark.parametrize('phrase', ['show cube', 'pin windows', 'pin cube', 'switch', 'refresh cube'])
def test_cube_reference_has_no_cancel_window(phrase, monkeypatch):
    from samsara import plugin_commands
    # The shared test fixture empties the registry before every test.
    plugin_commands._reinstall_module_commands(cube)
    entry = plugin_commands._REGISTRY[phrase]
    monkeypatch.setattr(policy, '_plugin_entry', lambda cid: entry)
    app = SimpleNamespace(config={})
    assert policy.classify(phrase, app=app)[0] == policy.RISK_READ
    assert policy.cancel_window_for(phrase, app) == 0


@pytest.mark.parametrize('method', ['start_recording', 'open_samsara_stress_test',
                                   'open_samsara_updates', 'open_samsara_discord',
                                   'open_samsara_listening_display'])
def test_other_ui_actions_keep_cancel_window(method):
    app = SimpleNamespace(config={}, command_executor=SimpleNamespace(
        commands={'action': {'type': 'method', 'method': method}}))
    assert policy.cancel_window_for('action', app) == 3
