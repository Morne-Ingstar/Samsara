"""Portable launch resolution: no app or subprocess is started."""
import json
from pathlib import Path
import pytest
from samsara import launch_targets
from samsara.handlers import LaunchHandler

ROOT = Path(__file__).resolve().parents[1]
PHRASES = ('run claude code', 'open obsidian', 'open warp', 'open discord', 'open spotify')

@pytest.mark.parametrize('phrase', PHRASES)
def test_each_launch_uses_fake_account(tmp_path, monkeypatch, phrase):
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    target = json.loads((ROOT / 'commands.json').read_text())['commands'][phrase]['target']
    assert target.startswith(('%APPDATA%', '%LOCALAPPDATA%'))
    expanded = launch_targets.os.path.expandvars(target)
    path = Path(expanded)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('fake installed target')
    monkeypatch.setattr(launch_targets, '_app_path', lambda _: pytest.fail('fallback unnecessary'))
    assert launch_targets.resolve_target(target) == str(path)
    assert str(tmp_path) in str(path)

@pytest.mark.parametrize('phrase', PHRASES)
def test_missing_app_returns_failed_without_starting(tmp_path, monkeypatch, phrase):
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(launch_targets, '_app_path', lambda _: None)
    monkeypatch.setattr(launch_targets.shutil, 'which', lambda _: None)
    monkeypatch.setattr('samsara.handlers.subprocess.Popen', lambda *a, **k: pytest.fail('must not launch'))
    cmd = json.loads((ROOT / 'commands.json').read_text())['commands'][phrase]
    assert LaunchHandler().execute(cmd, None) is False


def test_app_paths_precedes_path(monkeypatch):
    monkeypatch.setattr(launch_targets.os.path, 'isfile', lambda _: False)
    monkeypatch.setattr(launch_targets, '_app_path', lambda name: 'registered/' + name)
    monkeypatch.setattr(launch_targets.shutil, 'which', lambda _: pytest.fail('registry wins'))
    assert launch_targets.resolve_target(r'%LOCALAPPDATA%\Programs\Warp\Warp.exe') == 'registered/Warp.exe'


def test_path_fallback_and_shortcut_executable(monkeypatch):
    monkeypatch.setattr(launch_targets.os.path, 'isfile', lambda _: False)
    monkeypatch.setattr(launch_targets, '_app_path', lambda _: None)
    monkeypatch.setattr(launch_targets.shutil, 'which', lambda name: 'PATH/' + name)
    assert launch_targets.resolve_target(r'%APPDATA%\Discord.lnk') == 'PATH/Discord.exe'


def test_packaged_dispatch_does_not_claim_ava(monkeypatch):
    from plugins.commands import dispatch_voice
    monkeypatch.setattr(dispatch_voice, '_local', lambda: None)
    assert dispatch_voice.route_ava_utterance(None, 'send prompt') is False
    assert dispatch_voice.is_dispatch_shaped('send 123 to sol') is False


def test_native_claude_on_fake_path(tmp_path, monkeypatch):
    monkeypatch.setenv('APPDATA', str(tmp_path / 'Roaming'))
    monkeypatch.setenv('PATH', str(tmp_path / 'bin'))
    monkeypatch.setenv('PATHEXT', '.EXE;.CMD')
    executable = tmp_path / 'bin' / 'claude.exe'
    executable.parent.mkdir()
    executable.write_text('fake native executable')
    monkeypatch.setattr(launch_targets, '_app_path', lambda _: None)
    resolved = launch_targets.resolve_target(r'%APPDATA%\npm\claude.cmd')
    assert resolved is not None
    assert launch_targets.os.path.normcase(resolved) == launch_targets.os.path.normcase(str(executable))
