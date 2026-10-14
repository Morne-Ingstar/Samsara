"""570b DIRECT: real child stdio + port-0 loopback server, no app bootstrap."""
import ast
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from tools import mcp_relay as relay
from tests.test_agent_server_564 import server, memory_clipboard
from tests.test_connect_ai_app_570 import panel
from samsara.runtime import thread_registry
from samsara.ui.settings import agents_section as ui

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'tools' / 'mcp_relay.py'


def run_child(home, messages=(), url=relay.DEFAULT_URL):
    env = os.environ.copy()
    env['SAMSARA_HOME_DIR'] = str(home)
    body = b''.join(json.dumps(m).encode('utf-8') + b'\n' for m in messages)
    args = [sys.executable, '-u', str(SCRIPT)]
    if url is not None:
        args += ['--url', url]
    return subprocess.run(args,
                          input=body, capture_output=True, env=env, timeout=50)


@pytest.fixture
def token_home(tmp_path):
    home = tmp_path / 'relay_home'
    directory = home / 'agent_server'
    directory.mkdir(parents=True)
    token = secrets.token_urlsafe(32)
    path = directory / 'token.json'
    path.write_text(json.dumps({'version': 1, 'token': token}), encoding='utf-8')
    try:
        yield home, token
    finally:
        path.unlink(missing_ok=True)


def test_real_stdio_roundtrip_preserves_session_and_notification(server, caplog):
    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'Claude Desktop', 'version': 'test'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 'ping', 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
         'params': {'name': 'get_status', 'arguments': {}}},
    ]
    private = secrets.token_hex(16)
    messages.append({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
                     'params': {'name': 'speak', 'arguments': {'text': private}}})
    seen = []
    original = server.dispatch
    def dispatch(request, headers, **kwargs):
        seen.append((request['method'], headers.get('Mcp-Session-Id')))
        return original(request, headers, **kwargs)
    server.dispatch = dispatch
    result = run_child(server._profile, messages, f'http://127.0.0.1:{server.port}/mcp')
    assert result.returncode == 0 and result.stderr == b''
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r['id'] for r in replies] == [1, 'ping', 3, 4, 5]
    assert replies[0]['result']['protocolVersion'] == '2025-11-25'
    assert [t['name'] for t in replies[2]['result']['tools']] == ['get_status', 'speak']
    assert replies[3]['result']['structuredContent']
    assert seen[0][1] is None
    assert bool(all(session in server._clients for _, session in seen[1:]))
    assert bool(server._token.encode() not in result.stdout + result.stderr)
    assert bool(server._token not in caplog.text)
    assert bool(server.app.audio_coordinator.calls[0][0] == private)
    assert bool(private not in caplog.text and private.encode() not in result.stdout + result.stderr)


def test_real_modern_protocol_metadata(server):
    meta = {'io.modelcontextprotocol/protocolVersion': '2026-07-28'}
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover', 'params': {'_meta': meta}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                 'params': {'_meta': meta, 'name': 'get_status', 'arguments': {}}}]
    result = run_child(server._profile, messages, f'http://127.0.0.1:{server.port}/mcp')
    assert result.returncode == 0 and result.stderr == b''
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert all('result' in r for r in replies)
    assert replies[1]['result']['resultType'] == 'complete'


def test_eof_exits_zero_without_contacting_server(token_home):
    result = run_child(token_home[0])
    assert result.returncode == 0 and result.stdout == b'' and result.stderr == b''


def test_not_running_has_one_safe_stderr_line(token_home):
    with socket.socket() as held:
        held.bind(('127.0.0.1', 0))  # Bound, not listening; no port race.
        url = f'http://127.0.0.1:{held.getsockname()[1]}/mcp'
        result = run_child(token_home[0], [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}], url)
    assert result.returncode == 1 and result.stdout == b''
    assert len(result.stderr.splitlines()) == 1 and b'Samsara is not running' in result.stderr
    assert bool(token_home[1].encode() not in result.stderr)


def test_wrong_token_and_rotation_do_not_repair_saved_token(server, token_home):
    path = token_home[0] / 'agent_server' / 'token.json'
    before = path.read_bytes()
    result = run_child(token_home[0], [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}],
                       f'http://127.0.0.1:{server.port}/mcp')
    assert result.returncode == 1 and result.stdout == b''
    assert len(result.stderr.splitlines()) == 1 and b'token was rejected' in result.stderr
    assert bool(path.read_bytes() == before)
    assert bool(token_home[1].encode() not in result.stderr)
    active = relay.Relay(f'http://127.0.0.1:{server.port}/mcp', server._token)
    assert server.new_token()
    with pytest.raises(relay.RelayError, match='token was rejected'):
        active.forward({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}, b'{"jsonrpc":"2.0","id":1,"method":"ping"}')


@pytest.mark.parametrize('url', [
    'http://example.org:47832/mcp', 'http://192.168.1.2:47832/mcp',
    'http://127.0.0.1.example.org:47832/mcp', 'http://localhost:47832/mcp',
    'http://127.0.0.2:47832/mcp', 'https://127.0.0.1:47832/mcp',
    'http://user:password@127.0.0.1:47832/mcp', 'http://127.0.0.1:0/mcp',
    'http://127.0.0.1:65536/mcp', 'http://127.0.0.1:47832/other',
    'http://127.0.0.1:47832/mcp?target=elsewhere', 'http://127.0.0.1:47832/mcp#fragment',
])
def test_foreign_or_ambiguous_target_refused_before_token_read(tmp_path, url):
    result = run_child(tmp_path, url=url)
    assert result.returncode == 1 and result.stdout == b''
    assert len(result.stderr.splitlines()) == 1 and b'accepts only' in result.stderr


@pytest.mark.parametrize('saved', [None, [], {}, {'version': 2, 'token': 'invalid'},
                                {'version': 1, 'token': 'invalid'}])
def test_bad_token_never_regenerated(token_home, saved, monkeypatch, capsys):
    path = token_home[0] / 'agent_server' / 'token.json'
    path.write_text(json.dumps(saved), encoding='utf-8')
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(token_home[0]))
    assert relay.main([]) == 1
    assert json.loads(path.read_text()) == saved
    captured = capsys.readouterr()
    assert captured.out == '' and len(captured.err.splitlines()) == 1


@pytest.mark.parametrize('raw', [b'bad json\n', b'[]\n', b'{"jsonrpc":"2.0","method":"bad\\r\\nheader"}\n',
                                b'x' * (relay.MAX_BODY + 1) + b'\n'])
def test_invalid_input_never_reaches_http(raw):
    service = SimpleNamespace(forward=lambda *args: pytest.fail('HTTP attempted'))
    with pytest.raises(relay.RelayError):
        relay.serve(service, io.BytesIO(raw), io.BytesIO())


@pytest.mark.parametrize('status', [302, 500])
def test_no_proxy_redirect_retry_or_diagnostic_body(token_home, status, monkeypatch):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(status)
            self.send_header('Location', 'http://example.org/mcp')
            self.end_headers()
            self.wfile.write(b'not a protocol reply')
    service = HTTPServer(('127.0.0.1', 0), Handler)
    thread = thread_registry.spawn('test.570b.http', service.serve_forever, daemon=True)
    monkeypatch.setenv('HTTP_PROXY', 'http://example.org:8080')
    monkeypatch.setenv('HTTPS_PROXY', 'http://example.org:8080')
    try:
        result = run_child(token_home[0], [{'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}],
                           f'http://127.0.0.1:{service.server_port}/mcp')
        assert result.returncode == 1 and result.stdout == b''
        assert calls == ['/mcp']
        assert len(result.stderr.splitlines()) == 1 and b'not a protocol reply' not in result.stderr
    finally:
        service.shutdown()
        service.server_close()
        thread.join(2)


def test_source_entry_and_installed_portable_paths(monkeypatch):
    monkeypatch.delenv('SAMSARA_HOME_DIR', raising=False)
    source = json.loads(ui.claude_desktop_entry())['mcpServers']['samsara']
    assert source == {'command': sys.executable, 'args': ['-u', str(SCRIPT)]}
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    for directory in ('Program Files/Samsara', 'portable Samsara'):
        executable = ROOT / directory / 'Samsara.exe'
        monkeypatch.setattr(sys, 'executable', str(executable))
        monkeypatch.setattr(sys, '_MEIPASS', str(executable.parent / '_internal'), raising=False)
        entry = json.loads(ui.claude_desktop_entry())
        assert entry == {'mcpServers': {'samsara': {'command': str(executable.with_name('samsara-mcp.exe'))}}}
        assert '_internal' not in entry['mcpServers']['samsara']['command']


@pytest.mark.parametrize('key', [Qt.Key_Return, Qt.Key_Space])
def test_desktop_copy_is_secret_free_copy_only_and_enabled_off(panel, key):
    controls, state, clipboard, timers = panel
    assert controls.desktop.isEnabled() and controls.desktop_copy.isEnabled()
    assert clipboard.text() == '' and timers == []
    controls.desktop_copy.setFocus()
    QTest.keyClick(controls.desktop_copy, key)
    entry = json.loads(clipboard.text())
    assert entry == json.loads(ui.claude_desktop_entry(state.port))
    assert bool(state.token not in clipboard.text())
    assert 'Authorization' not in clipboard.text()
    assert controls.desktop_copy.minimumHeight() >= ui.theme.HIT_TARGET_MIN
    state.status = 'off'
    controls.refresh()
    clipboard.clear()
    controls.desktop_copy.click()
    assert json.loads(clipboard.text()) == json.loads(ui.claude_desktop_entry())


def test_spec_wires_standalone_console_relay_into_shared_internal():
    tree = ast.parse((ROOT / 'scripts' / 'samsara.spec').read_text(encoding='utf-8'))
    names = {'relay_analysis', 'relay_pyz', 'relay_exe', 'coll'}
    nodes = [node for node in tree.body if isinstance(node, ast.Assign)
             and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)]
    calls = {}
    def analysis(*args, **kwargs):
        calls['Analysis'] = (args, kwargs)
        return SimpleNamespace(pure='relay pure', scripts='relay scripts',
                               binaries=['relay DLLs'], datas=['relay data'])
    def capture(name):
        def call(*args, **kwargs):
            calls[name] = (args, kwargs)
            return name
        return call
    context = dict(app_dir=ROOT, Analysis=analysis, PYZ=capture('PYZ'), EXE=capture('EXE'),
                   COLLECT=capture('COLLECT'), exe='GUI exe',
                   a=SimpleNamespace(binaries=['main DLLs'], zipfiles=[], datas=['main data']))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<relay spec contract>', 'exec'), context)
    assert calls['Analysis'][0] == ([str(SCRIPT)],)
    assert calls['Analysis'][1]['runtime_hooks'] == []
    assert calls['EXE'][1]['console'] is True
    assert calls['EXE'][1]['contents_directory'] == '_internal'
    assert calls['EXE'][1]['name'] == 'samsara-mcp'
    assert calls['COLLECT'][0][:2] == ('GUI exe', 'EXE')
    assert ['relay DLLs'] in calls['COLLECT'][0] and ['relay data'] in calls['COLLECT'][0]
    # The standalone executable never imports the app package or GUI bootstrap.
    source = ast.parse(SCRIPT.read_text())
    modules = [n.module for n in ast.walk(source) if isinstance(n, ast.ImportFrom)]
    modules += [alias.name for n in ast.walk(source) if isinstance(n, ast.Import) for alias in n.names]
    assert not any(m and (m.startswith('samsara') or m == 'dictation') for m in modules)


@pytest.mark.parametrize('port', [1, 49123, 65535])
def test_old_entry_reads_custom_port_without_url(server, port, monkeypatch):
    # The actual listener chooses a free port; the edge ports are validated separately.
    home = server._profile
    (home / 'config.json').write_text(json.dumps({'agent_server': {'port': server.port}}))
    result = run_child(home, [{'jsonrpc': '2.0', 'id': 7, 'method': 'ping'}], url=None)
    assert result.returncode == 0 and result.stderr == b''
    assert json.loads(result.stdout)['id'] == 7
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(home))
    (home / 'config.json').write_text(json.dumps({'agent_server': {'port': port}}))
    assert relay.configured_url() == f'http://127.0.0.1:{port}/mcp'


@pytest.mark.parametrize('config', [[], None, {'agent_server': None},
    {'agent_server': {'port': True}}, {'agent_server': {'port': '49123'}},
    {'agent_server': {'port': 0}}, {'agent_server': {'port': 65536}}])
def test_invalid_config_fails_before_token_read(tmp_path, monkeypatch, config):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    (tmp_path / 'config.json').write_text(json.dumps(config))
    monkeypatch.setattr(relay, 'read_token', lambda: pytest.fail('Token read'))
    with pytest.raises(relay.RelayError, match='configured port'):
        relay.configured_url()
    assert relay.main([]) == 1


def test_malformed_config_and_absent_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    assert relay.configured_url() == relay.DEFAULT_URL
    for config in ({}, {'agent_server': {}}, {'unrelated': 49123}):
        (tmp_path / 'config.json').write_text(json.dumps(config))
        assert relay.configured_url() == relay.DEFAULT_URL
    (tmp_path / 'config.json').write_text('{broken')
    with pytest.raises(relay.RelayError, match='configured port'):
        relay.configured_url()


def test_explicit_url_overrides_bad_saved_config(server):
    (server._profile / 'config.json').write_text('{broken')
    result = run_child(server._profile, [{'jsonrpc': '2.0', 'id': 8, 'method': 'ping'}],
                       f'http://127.0.0.1:{server.port}/mcp')
    assert result.returncode == 0 and json.loads(result.stdout)['id'] == 8


def test_notifications_with_error_replies_never_write_stdout(server):
    messages = [{'jsonrpc': '2.0', 'method': 'notifications/roots/list_changed'},
                {'jsonrpc': '2.0', 'id': None, 'method': 'ping'},
                {'jsonrpc': '2.0', 'id': 0, 'method': 'ping'}]
    result = run_child(server._profile, messages, f'http://127.0.0.1:{server.port}/mcp')
    assert result.returncode == 0 and result.stderr == b''
    assert [json.loads(line)['id'] for line in result.stdout.splitlines()] == [None, 0]


@pytest.mark.parametrize('status, expected', [(403, 'refused this connection'),
    (404, 'endpoint was not found'), (415, 'message format'), (503, 'service is unavailable')])
def test_plain_http_errors_are_accurate_body_free_and_notification_safe(status, expected, monkeypatch):
    class Response:
        def read(self, limit):
            return b'private HTTP error body'
    response = Response()
    response.status = status
    class Connection:
        def request(self, *args):
            pass
        def getresponse(self):
            return response
        def close(self):
            pass
    monkeypatch.setattr(relay, 'HTTPConnection', lambda *a, **k: Connection())
    service = relay.Relay(relay.DEFAULT_URL, '')
    outgoing = io.BytesIO()
    with pytest.raises(relay.RelayError, match=expected) as error:
        relay.serve(service, io.BytesIO(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'), outgoing)
    assert str(status) in str(error.value)
    assert 'private' not in str(error.value) and outgoing.getvalue() == b''


def test_help_needs_no_token_config_or_network(tmp_path):
    env = os.environ.copy()
    env['SAMSARA_HOME_DIR'] = str(tmp_path)
    (tmp_path / 'config.json').write_text('{broken')
    result = subprocess.run([sys.executable, str(SCRIPT), '--help'], env=env,
                            capture_output=True, timeout=10)
    assert result.returncode == 0 and b'--url' in result.stdout and result.stderr == b''
    assert not (tmp_path / 'agent_server').exists()


def test_release_paths_require_relay_existence_and_help_success():
    workflow = (ROOT / '.github' / 'workflows' / 'release-build.yml').read_text()
    batch = (ROOT / 'build_release.bat').read_text()
    block = workflow.split('- name: Verify build output', 1)[1].split('- name:', 1)[0]
    assert 'Test-Path "dist\\Samsara\\samsara-mcp.exe"' in block
    assert '& "dist\\Samsara\\samsara-mcp.exe" --help' in block
    assert 'if ($LASTEXITCODE -ne 0)' in block
    assert 'exit 1' in block.split('if ($LASTEXITCODE -ne 0)', 1)[1]
    assert 'if not exist "dist\\Samsara\\samsara-mcp.exe"' in batch
    after = batch.split('"dist\\Samsara\\samsara-mcp.exe" --help >nul', 1)[1]
    assert after.lstrip().startswith('if errorlevel 1 (')
    assert 'exit /b 1' in after.split('echo Build successful:', 1)[0]


def test_desktop_docs_match_panel_and_explain_restart_and_errors():
    doc = (ROOT / 'Docs' / 'agents' / 'samsara' / 'SKILL.md').read_text()
    assert 'support is coming' not in doc
    for text in ('Copy Claude Desktop JSON', 'Settings > Developer > Edit Config',
                 'claude_desktop_config.json', 'mcpServers', 'restart Claude Desktop',
                 'New token', 'restart the Claude Desktop connection',
                 'SAMSARA_HOME_DIR', 'agent_server.port', '--url',
                 'HTTP 403', 'HTTP 404', 'HTTP 415', 'HTTP 503'):
        assert text in doc
