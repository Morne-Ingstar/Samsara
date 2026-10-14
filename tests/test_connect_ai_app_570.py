"""570: copy-only Claude Code setup, fake clipboard and isolated loopback server."""
import secrets
import shlex
import subprocess
import json
from http.client import HTTPConnection
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox, QLabel, QPushButton

from samsara.ui.settings import agents_section as ui
from tests.test_agent_server_564 import server, memory_clipboard, post


@pytest.fixture
def panel(qapp, monkeypatch, memory_clipboard):
    state = SimpleNamespace(status='listening', token=secrets.token_urlsafe(32), port=49123)
    state.connection_details = lambda: (f'http://127.0.0.1:{state.port}/mcp\nAuthorization: Bearer {state.token}'
                                       if state.status == 'listening' else None)
    def rotate():
        state.token = secrets.token_urlsafe(32)
        return True
    state.new_token = rotate
    monkeypatch.setattr(ui, 'for_app', lambda app: state)
    timers = []
    monkeypatch.setattr(ui.QTimer, 'singleShot', lambda ms, fn: timers.append((ms, fn)))
    # Any attempted process or file write by the panel fails the test.
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Process started'))
    monkeypatch.setattr(Path, 'write_text', lambda *a, **k: pytest.fail('File written'))
    monkeypatch.setattr(Path, 'write_bytes', lambda *a, **k: pytest.fail('File written'))
    app = SimpleNamespace(config={'command_packs': {'agents': True}})
    controls = ui.AgentsControls(SimpleNamespace(app=app), QCheckBox())
    try:
        yield controls, state, memory_clipboard, timers
    finally:
        memory_clipboard.clear()
        controls.deleteLater()


def test_live_command_is_masked_and_copy_only_on_press(panel, caplog):
    controls, state, clipboard, timers = panel
    assert clipboard.text() == '' and timers == []
    texts = [w.text() for w in controls.findChildren(QLabel) + controls.findChildren(QPushButton)]
    assert bool(all(state.token not in text for text in texts))
    assert '<token hidden>' in controls.command_preview.text()
    assert controls.desktop.isEnabled()
    assert controls.code_copy.accessibleName()
    controls.code_copy.click()
    words = shlex.split(clipboard.text())
    assert words[:9] == ['claude', 'mcp', 'add', '--transport', 'http', '--scope', 'user', 'samsara',
                         f'http://127.0.0.1:{state.port}/mcp']
    assert words[9] == '--header'
    assert bool(words[10] == 'Authorization: Bearer ' + state.token)
    assert bool(state.token not in caplog.text)
    assert timers[-1][0] == 60_000
    assert clipboard.mimeData().hasFormat(ui._EXCLUDE_HISTORY)


@pytest.mark.parametrize('key', [Qt.Key_Return, Qt.Key_Space])
def test_keyboard_copy_and_cleanup(panel, key):
    controls, state, clipboard, timers = panel
    controls.code_copy.setFocus()
    QTest.keyClick(controls.code_copy, key)
    assert bool(state.token in clipboard.text())
    first = timers[-1][1]
    controls.code_copy.click()
    first()
    assert clipboard.text()
    clipboard.setText('Unrelated clipboard content')
    timers[-1][1]()
    assert clipboard.text() == 'Unrelated clipboard content'
    controls.code_copy.click()
    controls.deleteLater()
    timers[-1][1]()
    assert clipboard.text() == ''


def test_rotate_and_disabled_copy_use_current_state(panel):
    controls, state, clipboard, timers = panel
    old = state.token
    controls.rotate.click()
    assert bool(state.token != old)
    assert clipboard.text() == ''
    controls.code_copy.click()
    assert bool(old not in clipboard.text() and state.token in clipboard.text())
    state.status = 'off'
    controls.refresh()
    assert not controls.code_copy.isEnabled()
    clipboard.clear()
    controls.copy_code_command()
    assert clipboard.text() == ''


def test_rotated_connection_rejects_old_session(server):
    with closing(HTTPConnection('127.0.0.1', server.port, timeout=4)) as connection:
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                           'params': {'protocolVersion': '2025-11-25',
                                      'clientInfo': {'name': 'Claude Code', 'version': '1'}}})
        connection.request('POST', '/mcp', body, {'Authorization': 'Bearer ' + server._token,
                                                'Content-Type': 'application/json'})
        response = connection.getresponse()
        session = response.getheader('Mcp-Session-Id')
        response.read()
        assert response.status == 200 and session
    old = server._token
    command = ui.claude_code_command(server.connection_details())
    assert bool(old in command)
    assert server.new_token()
    assert post(server, headers={'Authorization': 'Bearer ' + old,
                                'Mcp-Session-Id': session})[0] == 401
    assert post(server)[0] == 200
    assert bool(old not in ui.claude_code_command(server.connection_details()))


@pytest.mark.parametrize('details', [None, '', 'foreign endpoint',
    'http://127.0.0.1:0/mcp\nAuthorization: Bearer invalid',
    'http://example.org:47832/mcp\nAuthorization: Bearer invalid'])
def test_invalid_details_never_generate_a_command(details):
    assert ui.claude_code_command(details) is None


def test_rotation_hint_explains_replacement_and_local_shadow(panel):
    controls, state, clipboard, timers = panel
    labels = [label.text() for label in controls.findChildren(QLabel)]
    assert any('claude mcp remove samsara --scope user' in text for text in labels)
    assert any('claude mcp remove samsara in that project' in text for text in labels)
    assert ui.theme.TEXT_PRIMARY in controls.desktop.styleSheet()


def test_unavailable_preview_does_not_ask_to_enable_again(panel):
    controls, state, clipboard, timers = panel
    state.status = 'unavailable'
    controls.refresh()
    assert 'server is not running' in controls.command_preview.text()
    assert 'Turn on' not in controls.command_preview.text()
    assert not controls.code_copy.isEnabled()
    state.status = 'off'
    controls.refresh()
    assert 'Turn on Let AI apps use Samsara' in controls.command_preview.text()


def test_connection_and_approval_controls_coexist(panel):
    controls, state, clipboard, timers = panel
    assert controls.code_copy.isEnabled()
    assert controls.approval_style.count() == 2
    assert controls.approval_style.itemData(0) is False
    assert controls.approval_style.itemData(1) is True
    controls.owner.app.config['agent_server'] = {'allow_typing': True, 'command_key_only': True}
    controls.refresh()
    assert controls.approval_style.isEnabled()
    assert controls.approval_style.currentIndex() == 1
    assert controls.code_copy.isEnabled()


@pytest.mark.parametrize('frozen', [False, True])
@pytest.mark.parametrize('port', [47832, 49123])
@pytest.mark.parametrize('profile', [None, 'alternate home'])
def test_desktop_entry_uses_port_and_only_explicit_home(monkeypatch, frozen, port, profile, tmp_path):
    import sys
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    if profile is None:
        monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    else:
        monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / profile))
    entry = json.loads(ui.claude_desktop_entry(port))['mcpServers']['samsara']
    args = entry.get('args', [])
    if port != 47832:
        assert args[-2:] == ['--url', f'http://127.0.0.1:{port}/mcp']
    else:
        assert '--url' not in args
    if profile is None:
        assert 'env' not in entry
    else:
        assert entry['env'] == {'SAMSARA_HOME_DIR': str(tmp_path / profile)}
    assert 'Authorization' not in json.dumps(entry)


def test_desktop_copy_tracks_live_port_and_off_configuration(panel):
    controls, state, clipboard, timers = panel
    controls.desktop_copy.click()
    assert json.loads(clipboard.text()) == json.loads(ui.claude_desktop_entry(state.port))
    state.port = 49124
    controls.desktop_copy.click()
    assert json.loads(clipboard.text()) == json.loads(ui.claude_desktop_entry(state.port))
    state.status = 'off'
    controls.owner.app.config['agent_server'] = {'port': 49125}
    controls.refresh()
    controls.desktop_copy.click()
    assert json.loads(clipboard.text()) == json.loads(ui.claude_desktop_entry(49125))


@pytest.mark.parametrize('port', [True, None, '49123', 0, 65536])
def test_invalid_desktop_port_never_generates_entry(port):
    assert ui.claude_desktop_entry(port) is None
