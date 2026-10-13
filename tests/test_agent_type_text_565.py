"""565: real loopback port-0 transport; all OS, speech and delivery ports fake."""
from contextlib import closing
from http.client import HTTPConnection
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from samsara import agent_server as agents, execution_policy as policy
from samsara.runtime import thread_registry

REAL_PASTE = agents.paste_delivery.paste_preserving_clipboard


def post(service, method='tools/call', params=None, headers=None):
    body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method,
                       'params': params if params is not None else {'name': 'type_text', 'arguments': {'text': 'sample text'}}})
    request_headers = {'Authorization': 'Bearer ' + service._token, 'Content-Type': 'application/json'}
    request_headers.update(headers or {})
    with closing(HTTPConnection('127.0.0.1', service.port, timeout=4)) as connection:
        connection.request('POST', '/mcp', body, request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())


def call(service, text='sample text', *, args=None, headers=None):
    status, response, _ = post(service, params={'name': 'type_text', 'arguments': {'text': text} if args is None else args}, headers=headers)
    assert status == 200
    return response['result']['structuredContent']


@pytest.fixture
def rig(tmp_path, monkeypatch):
    slot = {'op': None}
    def install(app, record):
        old = slot['op']
        slot['op'] = record['op']
        if old is not None and old.approved is None:
            old.cancel(app, 'superseded', chip=False)
    monkeypatch.setattr(policy, '_install_record', install)
    monkeypatch.setattr(policy, 'pending_operation', lambda: slot['op'])
    def release(op):
        if slot['op'] is op:
            slot['op'] = None
    monkeypatch.setattr(policy, '_release_slot', release)
    monkeypatch.setattr(agents, 'TYPE_TIMEOUT', 0.4)
    monkeypatch.setattr(agents, 'CONFIRM_ECHO_TAIL', 0.0)
    target = {'hwnd': 10, 'pid': 20, 'birth': 1.0, 'exe': 'notepad.exe', 'app': 'Notepad'}
    rig = SimpleNamespace(target=target, refusal=None, pastes=[], prompts=[], chips=[],
                          answer='yes', on_question=None, on_speech=None, on_paste=None, probe_count=0)
    app = SimpleNamespace(config={'command_packs': {'agents': True}, 'agent_server': {'allow_typing': True}},
                          _running=True, _ava_cmd_generation=0, _schedule_ui=lambda fn: fn(),
                          save_config=lambda: None)
    def speak(prompt, **kwargs):
        rig.prompts.append((prompt, kwargs))
        if rig.on_speech:
            rig.on_speech(kwargs)
        kwargs['on_done']()
        return SimpleNamespace(utterance_id='fake')
    app.audio_coordinator = SimpleNamespace(speak=speak, is_speaking=False)
    def show_chip(*args):
        rig.chips.append(args)
        if rig.on_question:
            rig.on_question()
        if rig.answer is not None:
            rig.answer_result = policy.answer_pending(app, rig.answer)
    app._show_outcome_chip = show_chip
    def paste(app, text, *, before_paste, feedback, clipboard_only):
        assert clipboard_only is True
        if rig.on_paste:
            rig.on_paste()
        if not before_paste():
            return False
        rig.pastes.append((text, dict(rig.target)))
        return True
    monkeypatch.setattr(agents.paste_delivery, "paste_preserving_clipboard", paste)
    service = agents.AgentServer(app, profile=tmp_path, port=0)
    def probe():
        rig.probe_count += 1
        return dict(rig.target), rig.refusal
    service._target_probe = probe
    assert service.start()
    rig.service, rig.app = service, app
    try:
        yield rig
    finally:
        service.stop()
        for path in tmp_path.rglob('token.json'):
            path.unlink()


def test_second_switch_off_default_and_listing(rig):
    from samsara.config_defaults import cfg_get
    assert cfg_get({}, 'agent_server.allow_typing') is False
    rig.app.config['agent_server']['allow_typing'] = False
    _, data, _ = post(rig.service, 'tools/list', {})
    assert {tool['name'] for tool in data['result']['tools']} == {'get_status', 'speak'}
    assert call(rig.service)['reason'] == 'disabled'
    assert rig.probe_count == 0 and not rig.prompts and not rig.pastes
    rig.app.config['agent_server']['allow_typing'] = True
    _, data, _ = post(rig.service, 'tools/list', {})
    assert {tool['name'] for tool in data['result']['tools']} == {'get_status', 'speak', 'type_text'}
    rig.app.config['command_packs']['agents'] = False
    assert not rig.service.typing_enabled
    assert rig.service.dispatch({'method': 'tools/call', 'id': 1, 'params': {'name': 'type_text', 'arguments': {'text': 'sample'}}}, {})[1]['result']['structuredContent']['reason'] == 'disabled'


@pytest.mark.parametrize('text,reason', [('', 'invalid_text'), (' ', 'invalid_text'),
    (None, 'invalid_text'), (42, 'invalid_text'), ('a' * 2001, 'too_long'),
    ('a\nsecond', 'invalid_text'), ('a\rsecond', 'invalid_text'), ('a\n', 'invalid_text'), ('a\n ', 'invalid_text'), ('a\n\u00a0', 'invalid_text'), ('a\r\n', 'invalid_text'),
    ('a\t b', 'invalid_text'), ('a\x00b', 'invalid_text'), ('a\x1bb', 'invalid_text'),
    ('a\u202eb', 'invalid_text'), ('a\ud800b', 'invalid_text'), ('a\u2028b', 'invalid_text'),
    ('<b>sample</b>', 'invalid_text'), ('```sample```', 'invalid_text')])
def test_validation_precedes_target_and_question(rig, text, reason):
    assert call(rig.service, text)['reason'] == reason
    assert rig.probe_count == 0 and not rig.prompts and not rig.pastes


@pytest.mark.parametrize('args', [{'text': 'sample', 'extra': True}, {}, [], {'text': ['sample']}])
def test_invalid_argument_shapes(rig, args):
    assert call(rig.service, args=args)['reason'] == 'invalid_text'
    assert not rig.prompts and not rig.pastes


@pytest.mark.parametrize('reason', ['refused_terminal', 'refused_elevated', 'refused_own_window',
                                  'refused_password', 'refused_unverified', 'window_changed'])
def test_unsafe_target_refused_without_question(rig, reason):
    rig.refusal = reason
    assert call(rig.service)['reason'] == reason
    assert not rig.prompts and not rig.pastes


@pytest.mark.parametrize('text', ['sample text', 'a' * 2000])
def test_yes_pastes_exactly_once_and_consumes_permission(rig, text):
    result = call(rig.service, text)
    assert result == {'status': 'delivered', 'reason': 'delivered'}
    assert rig.pastes == [(text, rig.target)]
    assert rig.prompts[0][0] == f'An AI app wants to type {len(text.split())} words into Notepad. Say yes to allow.'
    assert rig.prompts[0][1]['category'] == 'confirmation'
    assert policy.pending_operation() is None
    assert policy.answer_pending(rig.app, 'yes') is None
    assert len(rig.pastes) == 1


@pytest.mark.parametrize('field,value', [('hwnd', 11), ('pid', 21), ('birth', 2.0), ('exe', 'word.exe')])
def test_window_or_process_changed_between_yes_and_actual_injection(rig, field, value):
    rig.on_paste = lambda: rig.target.update({field: value})
    assert call(rig.service)['reason'] == 'window_changed'
    assert not rig.pastes


def test_window_changed_while_question_open(rig):
    rig.on_question = lambda: rig.target.update(hwnd=11)
    assert call(rig.service)['reason'] == 'window_changed'
    assert not rig.pastes


@pytest.mark.parametrize('reason', ['refused_terminal', 'refused_elevated', 'refused_password', 'refused_own_window'])
def test_target_becomes_unsafe_after_yes(rig, reason):
    rig.on_paste = lambda: setattr(rig, 'refusal', reason)
    assert call(rig.service)['reason'] == reason
    assert not rig.pastes


@pytest.mark.parametrize('answer,reason', [('no', 'user_declined'), (None, 'timeout'), ('yes but later', 'timeout')])
def test_decline_and_timeout_never_paste(rig, answer, reason):
    rig.answer = answer
    assert call(rig.service)['reason'] == reason
    assert not rig.pastes
    assert policy.pending_operation() is None
    assert policy.answer_pending(rig.app, 'yes') is None


def test_model_sourced_yes_does_not_approve(rig):
    rig.answer = None
    rig.on_question = lambda: policy.answer_pending(rig.app, 'yes', source=policy.ANSWER_MODEL)
    assert call(rig.service)['reason'] == 'timeout'
    assert not rig.pastes


@pytest.mark.parametrize('source', [policy.ANSWER_DIALOG, 'direct'])
def test_only_spoken_yes_can_approve_typing(rig, source):
    rig.answer = None
    rig.on_question = (lambda: policy.pending_operation().approve(app=rig.app)) if source == 'direct' else (
        lambda: policy.answer_pending(rig.app, 'yes', source=source))
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_busy_is_immediate_over_real_http_without_queueing(rig, monkeypatch):
    monkeypatch.setattr(agents, 'TYPE_TIMEOUT', 2.0)
    rig.answer = None
    asked = threading.Event()
    rig.on_question = asked.set
    result = {}
    worker = thread_registry.spawn('test.agent_typing', lambda: result.update(call(rig.service)), daemon=True)
    try:
        assert asked.wait(1)
        started = time.monotonic()
        assert call(rig.service)['reason'] == 'busy'
        assert time.monotonic() - started < 0.8
        assert len(rig.prompts) == 1
        assert policy.answer_pending(rig.app, 'yes') == 'approved'
        worker.join(2)
        assert not worker.is_alive()
        assert result['reason'] == 'delivered' and len(rig.pastes) == 1
    finally:
        rig.service.cancel_typing() if hasattr(rig.service, 'cancel_typing') else None
        worker.join(3)


def test_another_lanes_pending_question_is_not_superseded(rig):
    op = policy.stage_pending(rig.app, policy.Invocation('sample', generation=0), 'existing question')
    assert call(rig.service)['reason'] == 'busy'
    assert policy.pending_operation() is op and op.approved is None
    assert not rig.prompts and not rig.pastes


@pytest.mark.parametrize('change', ['typing_off', 'pack_off', 'rotate', 'stop_generation'])
def test_revocation_while_question_open(rig, change):
    rig.answer = None
    def revoke():
        if change == 'typing_off': rig.app.config['agent_server']['allow_typing'] = False
        elif change == 'pack_off': rig.app.config['command_packs']['agents'] = False
        elif change == 'rotate': assert rig.service.new_token()
        else: rig.app._ava_cmd_generation += 1
        policy.answer_pending(rig.app, 'yes')
    rig.on_question = revoke
    result = call(rig.service)
    assert result['reason'] == 'disabled'
    assert not rig.pastes


def test_stop_generation_after_yes_cancels_before_paste(rig):
    rig.on_paste = lambda: setattr(rig.app, '_ava_cmd_generation', 1)
    assert call(rig.service)['reason'] == 'disabled'
    assert not rig.pastes


def test_wait_does_not_extend_typing_deadline(rig):
    rig.answer = 'wait'
    started = time.monotonic()
    assert call(rig.service)['reason'] == 'timeout'
    assert time.monotonic() - started < 1.2
    assert not rig.pastes


def test_client_name_is_bound_to_its_initialize_not_global(rig):
    _, _, first = post(rig.service, 'initialize', {'clientInfo': {'name': 'Claude Code\n<script>danger</script>\u202e' + 'x' * 60}})
    _, _, second = post(rig.service, 'initialize', {'clientInfo': {'name': 'second client'}})
    call(rig.service, headers={'Mcp-Session-Id': first['Mcp-Session-Id']})
    name = rig.prompts[0][0].split(' wants to type ')[0]
    assert len(name) <= 24 and len(name.split()) <= 3 and all(c.isalnum() or c == ' ' for c in name)
    assert name.startswith('Claude Code')
    call(rig.service, headers={'Mcp-Session-Id': second['Mcp-Session-Id']})
    assert rig.prompts[-1][0].startswith('second client wants')
    call(rig.service)
    assert rig.prompts[-1][0].startswith('An AI app wants')


@pytest.mark.parametrize('name', [None, [], '', '\n\u202e<>'])
def test_unknown_client_name_falls_back(rig, name):
    _, _, headers = post(rig.service, 'initialize', {'clientInfo': {'name': name}})
    assert call(rig.service, headers={'Mcp-Session-Id': headers['Mcp-Session-Id']})['reason'] == 'delivered'
    assert rig.prompts[0][0].startswith('An AI app wants')


def test_log_contains_only_closed_outcomes_and_length_bucket(rig, caplog):
    with caplog.at_level(logging.INFO):
        call(rig.service, 'synthetic private payload')
        rig.answer = 'no'
        call(rig.service, 'other synthetic private payload')
    records = [r.getMessage() for r in caplog.records if r.name == 'samsara.agent_server']
    assert records
    assert not any('synthetic' in r or 'Notepad' in r or 'private payload' in r for r in records)
    typing = [r for r in records if 'length_bucket=' in r]
    assert len(typing) == 2
    assert 'reason=delivered length_bucket=1-100' in typing[0]
    assert 'reason=user_declined length_bucket=1-100' in typing[1]
    requests = [r for r in records if 'method=tools/call tool=type_text' in r]
    assert len(requests) == 2
    assert 'outcome=delivered' in requests[0]
    assert 'outcome=refused' in requests[1]
    assert not any('outcome=accepted' in r for r in requests)


def test_switch_ui_persists_and_parent_off_clears_it(rig, qapp):
    from PySide6.QtWidgets import QCheckBox
    from samsara.ui.settings.agents_section import AgentsControls
    rig.app._agent_server = rig.service
    saved = []
    rig.app.save_config = lambda: saved.append(json.loads(json.dumps(rig.app.config)))
    owner = SimpleNamespace(app=rig.app)
    enabled = QCheckBox(); enabled.setChecked(True)
    controls = AgentsControls(owner, enabled)
    try:
        assert controls.typing.isChecked()
        controls.typing.setChecked(False)
        assert not rig.service.typing_enabled
        controls.typing.setChecked(True)
        assert rig.service.typing_enabled
        enabled.setChecked(False)
        assert not controls.typing.isEnabled() and not controls.typing.isChecked()
        assert saved[-1]['agent_server']['allow_typing'] is False
        assert not rig.service.typing_enabled and rig.service.status == 'off'
    finally:
        controls.close()


def test_switch_save_failure_restores_state(rig, qapp):
    from PySide6.QtWidgets import QCheckBox
    from samsara.ui.settings.agents_section import AgentsControls
    rig.app._agent_server = rig.service
    def fail(): raise OSError('synthetic save failure')
    rig.app.save_config = fail
    enabled = QCheckBox(); enabled.setChecked(True)
    controls = AgentsControls(SimpleNamespace(app=rig.app), enabled)
    try:
        controls.typing.setChecked(False)
        assert rig.service.typing_enabled and controls.typing.isChecked()
        enabled.setChecked(False)
        assert rig.service.enabled and rig.service.typing_enabled and enabled.isChecked()
    finally:
        controls.close()


@pytest.mark.parametrize('exe,reason', [
    ('warp.exe', 'refused_terminal'), ('windowsterminal.exe', 'refused_terminal'),
    ('conhost.exe', 'refused_terminal'), ('cmd.exe', 'refused_terminal'),
    ('powershell.exe', 'refused_terminal'), ('pwsh.exe', 'refused_terminal'),
    ('wezterm-gui.exe', 'refused_terminal'), ('alacritty.exe', 'refused_terminal'),
    ('mintty.exe', 'refused_terminal'),
    *[(exe, 'refused_terminal') for exe in (
        'putty.exe', 'kitty.exe', 'mobaxterm.exe', 'conemu.exe', 'conemu64.exe',
        'tabby.exe', 'hyper.exe', 'termius.exe', 'git-bash.exe')],
    ('samsara.exe', 'refused_own_window'),
    ('lockapp.exe', 'refused_elevated'), ('logonui.exe', 'refused_elevated'),
    ('consent.exe', 'refused_elevated'), ('notepad.exe', None)])
def test_native_target_probe_with_fake_windows_and_processes(monkeypatch, exe, reason):
    import ctypes
    import psutil
    from samsara import injection_safety
    # Importing paste does not call delivery; every native operation below is fake.
    import samsara.paste
    monkeypatch.setattr(agents, '_desktop_allows_typing', lambda: True)
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(name=lambda: exe, create_time=lambda: 2.0))
    monkeypatch.setattr(injection_safety, 'window_integrity', lambda hwnd: SimpleNamespace(confirmed_ok=True))
    monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=43210, IsPassword=False, ClassName='Edit')))
    def process(hwnd, pid): pid._obj.value = 43210; return 1
    def classname(hwnd, buffer, length): buffer.value = 'Editor'; return 6
    api = SimpleNamespace(GetForegroundWindow=lambda: 123, GetWindowThreadProcessId=process, GetClassNameW=classname)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: api)
    target, actual = agents._foreground_target()
    assert actual == reason and target['hwnd'] == 123 and target['pid'] == 43210


def test_secure_desktop_is_refused_without_native_window_probe(monkeypatch):
    monkeypatch.setattr(agents, '_desktop_allows_typing', lambda: False)
    assert agents._foreground_target() == ({}, 'refused_elevated')


@pytest.mark.parametrize('case,reason', [('password', 'refused_password'),
    ('unknown_integrity', 'refused_elevated'), ('own_pid', 'refused_own_window'),
    ('console_class', 'refused_terminal'), ('uia_changed_process', 'window_changed'),
    ('uia_unavailable', 'refused_unverified')])
def test_native_safety_properties_with_fakes(monkeypatch, case, reason):
    import ctypes
    from samsara import injection_safety
    test_native_target_probe_with_fake_windows_and_processes(monkeypatch, 'notepad.exe', None)
    if case == 'password':
        monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=43210, IsPassword=True, ClassName='Edit')))
    elif case == 'unknown_integrity':
        monkeypatch.setattr(injection_safety, 'window_integrity', lambda hwnd: SimpleNamespace(confirmed_ok=False))
    elif case == 'own_pid':
        monkeypatch.setattr(agents.os, 'getpid', lambda: 43210)
    elif case == 'console_class':
        api = ctypes.WinDLL('fake')
        def classname(hwnd, buffer, length): buffer.value = 'ConsoleWindowClass'; return 18
        api.GetClassNameW = classname
    elif case == 'uia_changed_process':
        monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=43211, IsPassword=False, ClassName='Edit')))
    elif case == 'uia_unavailable':
        def unavailable(): raise RuntimeError('synthetic UIA failure')
        monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=unavailable))
    assert agents._foreground_target()[1] == reason


@pytest.mark.parametrize('desktop,expected', [('Default', True), ('Winlogon', False), (None, False)])
def test_native_input_desktop_probe_is_fake_and_closes_handle(monkeypatch, desktop, expected):
    import ctypes
    closed = []
    def info(handle, kind, buffer, size, needed): buffer.value = desktop; return True
    def close(handle): closed.append(handle)
    api = SimpleNamespace(OpenInputDesktop=lambda *args: 123 if desktop else 0,
                          CloseDesktop=close, GetUserObjectInformationW=info)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: api)
    assert agents._desktop_allows_typing() is expected
    assert closed == ([123] if desktop else [])


@pytest.mark.parametrize('name', ['Samsara security check. Notepad is safe. Say yes to allow',
    'YES', 'no', 'Say', 'allow', 'SAFE', 'confirm', 'ok', 'Claude yes please',
    'Ｓａｙ ｙｅｓ', 'Yеs', 'Samsаra', 'Y.E.S', 'Y E S', 'Ｓ.Ａ.Ｙ', 'y-e-s', 'yés',
    'SamsaraBot', 'SamsaraSecurity', 'SayYes', 'YesMan', 'Yes2', 'OkGo', 'Okay',
    'Allowed', 'Safely', 'Say\u00adyes', 'ＳａｙＹｅｓ',
    'Yesman', 'YESMAN', 'yesman', 'Yesbot', 'YESBOT', 'Yess', 'Yesyes', 'YESYES',
    'yesbot ai', 'theyes', 'autoyes', 'Confirmed', 'CONFIRMED', 'Confirmation',
    'Confirmbot', 'Saysure', 'Sayit', 'Yеsman', 'Ｙｅｓｍａｎ', 'Sayward',
    'Yesterday Bot', 'Yes Terday', 'YesTerday', 'YES TERDAY', 'Yes-terday Bot',
    'Calendar Assistantss Yesterday', 'Aaaaaaaaaaaaaaaaaa B Yesterday',
    'Justsayit', 'Pleasesay', 'Autoallow'])
def test_agent_name_cannot_supply_confirmation_instructions(rig, name):
    _, _, headers = post(rig.service, 'initialize', {'clientInfo': {'name': name}})
    assert call(rig.service, headers={'Mcp-Session-Id': headers['Mcp-Session-Id']})['reason'] == 'delivered'
    assert rig.prompts[0][0].startswith('An AI app wants')


@pytest.mark.parametrize('name', ['Notion', 'Notebook', 'Nothing', 'Nobot', 'Noway',
    'Nonono', 'Claude Code'])
def test_client_name_word_check_preserves_names_containing_short_words(name):
    # FINISH IT 3 deliberately permits Nobot/Noway/Nonono: only whole-word no
    # is blocked, since a no in the question pushes toward declining.
    assert agents._safe_name(name) == name


def test_client_name_filter_checks_truncated_spoken_string():
    name = 'Calendar Assistantsss Nobody'
    assert len('Calendar Assistantsss No') == 24
    assert agents._safe_name(name) == 'An AI app'
    assert agents._safe_name(name, client=False) == 'Calendar Assistantsss No'


@pytest.mark.parametrize('classname', [
    'consolewindowclass', 'cascadia_hosting_window_class', 'virtualconsoleclass',
    'putty', 'kitty', 'mintty', 'org.wezfurlong.wezterm', 'alacritty',
    'TMobaXtermForm', 'Window Class'])
def test_terminal_window_classes_survive_a_renamed_executable(monkeypatch, classname):
    import ctypes
    test_native_target_probe_with_fake_windows_and_processes(monkeypatch, 'editor.exe', None)
    api = ctypes.WinDLL('fake')
    def set_class(hwnd, buffer, length):
        buffer.value = classname
        return len(classname)
    api.GetClassNameW = set_class
    assert agents._foreground_target()[1] == 'refused_terminal'


@pytest.mark.parametrize('case,reason', [('xterm', 'refused_terminal'), ('missing', 'refused_unverified'),
    ('xterm_extra', 'refused_terminal'), ('xterm_prefix', 'refused_terminal'),
    ('no_control', 'refused_unverified'), ('no_password', 'refused_unverified'),
    ('no_class', 'refused_unverified'), ('unknown_password', 'refused_unverified')])
def test_focused_control_verification_fails_closed(monkeypatch, case, reason):
    test_native_target_probe_with_fake_windows_and_processes(monkeypatch, 'code.exe', None)
    focused = SimpleNamespace(ProcessId=43210, IsPassword=False, ClassName='Edit')
    if case == 'xterm': focused.ClassName = 'xterm-helper-textarea'
    elif case == 'xterm_extra': focused.ClassName = 'xterm-helper-textarea extra'
    elif case == 'xterm_prefix': focused.ClassName = 'editor XTERM-HELPER-TEXTAREA-focused extra'
    elif case == 'no_control': focused = None
    elif case == 'no_password': del focused.IsPassword
    elif case == 'no_class': del focused.ClassName
    elif case == 'unknown_password': focused.IsPassword = None
    if case == 'missing': monkeypatch.setitem(sys.modules, 'uiautomation', None)
    else: monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=lambda: focused))
    assert agents._foreground_target()[1] == reason


def test_uia_runs_on_worker_and_hung_provider_has_bounded_wait(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    caller = threading.get_ident()
    seen = []
    def focused():
        seen.append(threading.get_ident())
        started.set()
        release.wait(2)
        finished.set()
        return SimpleNamespace(ProcessId=20, IsPassword=False, ClassName='Edit')
    monkeypatch.setitem(sys.modules, 'uiautomation', SimpleNamespace(GetFocusedControl=focused))
    monkeypatch.setattr(agents, 'UIA_TIMEOUT', 0.03)
    try:
        before = time.monotonic()
        assert agents._focused_control_reason(20) == 'refused_unverified'
        assert started.is_set() and seen == [seen[0]] and seen[0] != caller
        assert time.monotonic() - before < 0.25
        assert agents._focused_control_reason(20) == 'refused_unverified'
        assert len(seen) == 1  # A hung provider cannot accumulate workers.
    finally:
        release.set()
        assert finished.wait(1)
        deadline = time.monotonic() + 1
        while not agents._UIA_SLOT.acquire(blocking=False):
            assert time.monotonic() < deadline
            time.sleep(0.005)
        agents._UIA_SLOT.release()


def speak_call(service):
    return post(service, params={'name': 'speak', 'arguments': {'text': 'Yes.'}})[1]['result']['structuredContent']


@pytest.mark.parametrize('answer,reason', [('yes', 'delivered'), ('no', 'user_declined'), (None, 'timeout')])
def test_agent_cannot_speak_while_question_open_or_for_three_seconds_after(rig, answer, reason):
    clock = [10.0]
    rig.service._clock = lambda: clock[0]
    rig.answer = answer
    def question():
        assert speak_call(rig.service) == {'status': 'busy', 'reason': 'busy'}
        assert len(rig.prompts) == 1
    rig.on_question = question
    assert call(rig.service)['reason'] == reason
    for elapsed in (0, 2.999):
        clock[0] = 10 + elapsed
        assert speak_call(rig.service) == {'status': 'busy', 'reason': 'busy'}
        assert len(rig.prompts) == 1
    clock[0] = 13.0
    assert speak_call(rig.service)['status'] == 'spoken'


def test_question_echo_cannot_answer_before_playback_completes(rig, monkeypatch):
    early = []
    monkeypatch.setattr(agents, 'CONFIRM_ECHO_TAIL', 0.02)
    def own_speech(kwargs):
        assert policy.pending_operation() is None
        early.append(policy.answer_pending(rig.app, 'Yes.'))
        assert speak_call(rig.service)['reason'] == 'busy'
    rig.on_speech = own_speech
    assert call(rig.service)['reason'] == 'delivered'
    assert early == [None] and len(rig.pastes) == 1


@pytest.mark.parametrize('kind', ['noop', 'never_completed'])
def test_unspoken_or_unfinished_question_never_opens_answer_window(rig, kind):
    def suppressed(prompt, **kwargs):
        assert policy.pending_operation() is None
        assert policy.answer_pending(rig.app, 'yes') is None
        if kind == 'noop': kwargs['on_done']()
        return SimpleNamespace(utterance_id='noop-silent' if kind == 'noop' else 'fake')
    rig.app.audio_coordinator.speak = suppressed
    assert call(rig.service)['reason'] == 'timeout'
    assert not rig.pastes and policy.pending_operation() is None


def test_capture_begun_during_question_cannot_be_delayed_yes(rig):
    rig.app._hold_capture_start_ns = time.perf_counter_ns()
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_capture_started_during_speech_cannot_supply_delayed_yes(rig):
    rig.on_speech = lambda kwargs: setattr(rig.app, '_hold_capture_start_ns', time.perf_counter_ns())
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_stale_held_capture_remains_fail_closed_without_finalised_marker(rig):
    rig.app._hold_capture_start_ns = time.perf_counter_ns() - 10_000_000_000
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


@pytest.mark.parametrize('flag', ['recording', '_stop_in_flight', '_hotkey_recording', '_streaming_session'])
def test_capture_live_or_in_flight_before_question_cannot_supply_yes(rig, flag):
    rig.app._hold_capture_start_ns = time.perf_counter_ns() - 10_000_000_000
    setattr(rig.app, flag, True)
    def fresh_answer():
        setattr(rig.app, flag, False)
        rig.app._hold_capture_start_ns = time.perf_counter_ns()
    rig.on_question = fresh_answer
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_hotkey_active_before_timestamp_and_recording_cannot_supply_yes(rig):
    rig.app._hotkey_recording = True
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


@pytest.mark.parametrize('flag', ['recording', '_hotkey_recording', '_stop_in_flight', '_streaming_session'])
def test_capture_active_when_listening_starts_cannot_supply_prebuffered_yes(rig, flag):
    rig.on_speech = lambda kwargs: setattr(rig.app, flag, True)
    def late_timestamp_answer():
        setattr(rig.app, flag, False)
        rig.app._hold_capture_start_ns = time.perf_counter_ns()
    rig.on_question = late_timestamp_answer
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_capture_already_active_before_question_cannot_supply_yes(rig):
    rig.app._hold_capture_start_ns = time.perf_counter_ns() - 10_000_000_000
    rig.app.recording = True
    assert call(rig.service)['reason'] == 'user_declined'
    assert not rig.pastes


def test_fresh_command_only_capture_can_answer(rig):
    rig.on_question = lambda: setattr(rig.app, '_hold_capture_start_ns', time.perf_counter_ns())
    assert call(rig.service)['reason'] == 'delivered'


def test_already_queued_agent_speech_prevents_question(rig):
    rig.app.audio_coordinator.speak = lambda *a, **k: SimpleNamespace(utterance_id='fake')
    assert speak_call(rig.service)['status'] == 'queued'
    assert call(rig.service)['reason'] == 'busy'
    assert not rig.prompts and not rig.pastes


@pytest.mark.parametrize('pasted', [True, False])
def test_late_delivery_returns_unknown_without_retry_and_stays_busy(rig, monkeypatch, caplog, pasted):
    monkeypatch.setattr(agents, 'UI_TIMEOUT', 0.03)
    release, finished = threading.Event(), threading.Event()
    attempts, workers = [], []
    scheduled = []
    def schedule(fn):
        scheduled.append(fn)
        if len(scheduled) <= 2:
            fn()
        else:
            def run():
                try:
                    fn()
                finally:
                    finished.set()
            workers.append(thread_registry.spawn('test.late_delivery', run, daemon=True))
    rig.app._schedule_ui = schedule
    def slow_paste(app, text, *, before_paste, **kwargs):
        assert before_paste()
        attempts.append(text)
        if pasted:
            rig.pastes.append((text, dict(rig.target)))
        assert release.wait(2)
        return pasted
    monkeypatch.setattr(agents.paste_delivery, 'paste_preserving_clipboard', slow_paste)
    try:
        with caplog.at_level(logging.INFO):
            assert call(rig.service) == {'status': 'unknown', 'reason': 'unknown'}
        assert attempts == ['sample text']
        assert len(rig.pastes) == int(pasted)
        assert call(rig.service)['reason'] == 'busy'
        assert policy.answer_pending(rig.app, 'yes') is None
        messages = [r.getMessage() for r in caplog.records if r.name == 'samsara.agent_server']
        assert any('outcome=unknown reason=unknown' in message for message in messages)
        assert any('tool=type_text outcome=unknown' in message for message in messages)
    finally:
        release.set()
        assert finished.wait(1)
        for worker in workers:
            worker.join(1)
    assert not rig.service._typing_lock.locked()
    assert len(attempts) == 1


def test_delivery_queued_past_ui_timeout_cannot_paste_later(rig, monkeypatch):
    monkeypatch.setattr(agents, 'UI_TIMEOUT', 0.03)
    queued, count = [], [0]
    def schedule(fn):
        count[0] += 1
        if count[0] <= 2:
            fn()
        else:
            queued.append(fn)
    rig.app._schedule_ui = schedule
    assert call(rig.service) == {'status': 'unknown', 'reason': 'unknown'}
    assert len(queued) == 1 and not rig.pastes
    queued[0]()
    assert not rig.pastes
    assert not rig.service._typing_lock.locked()


@pytest.mark.parametrize('case', ['typed_allowlist', 'incomplete_snapshot', 'clipboard_timeout', 'clipboard_copy_failed'])
def test_real_clipboard_path_has_no_unicode_injection_or_fallback(rig, monkeypatch, case):
    from samsara import clipboard, injection_safety
    copied, keys, restored, typed = [], [], [], []
    snapshot = clipboard.ClipboardSnapshot()
    if case == 'incomplete_snapshot': snapshot.lost[1] = 'fake unavailable format'
    monkeypatch.setattr(injection_safety, 'window_integrity', lambda *a: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(clipboard, 'HAS_PYPERCLIP', True)
    def copy(text):
        if case == 'clipboard_copy_failed': raise RuntimeError('fake failure')
        copied.append(text)
    monkeypatch.setattr(clipboard, 'pyperclip', SimpleNamespace(copy=copy))
    monkeypatch.setattr(clipboard, 'save_clipboard', lambda: snapshot)
    monkeypatch.setattr(clipboard, 'restore_clipboard', lambda saved: restored.append(saved) or True)
    monkeypatch.setattr(clipboard, 'get_clipboard_sequence_number', lambda: 10)
    monkeypatch.setattr(clipboard, 'wait_for_clipboard_ready', lambda *a: 'timeout' if case == 'clipboard_timeout' else 'ready')
    monkeypatch.setattr(clipboard, '_paste_target', lambda: (10, 20))
    monkeypatch.setattr(clipboard, '_paste_target_matches', lambda target: True)
    monkeypatch.setattr(clipboard, 'message_paste_to_focused_control', lambda: clipboard.DeliveryOutcome.SENT)
    monkeypatch.setitem(sys.modules, 'pyautogui', SimpleNamespace(hotkey=lambda *args: keys.append(args)))
    rig.app._foreground_wants_typed_injection = lambda: True
    rig.app._flight_foreground_process_name = lambda: 'chrome.exe'
    rig.app._record_undoable_paste = lambda *a, **k: None
    rig.app.adaptive_learner = SimpleNamespace(record_transcription=lambda *a: None)
    monkeypatch.setattr(agents.paste_delivery.flight_recorder, 'record', lambda *a, **k: None)
    monkeypatch.setattr(agents.paste_delivery, '_arm_undo_after_paste', lambda *a: None)
    def actual(app, text, **kwargs):
        return REAL_PASTE(app, text, **kwargs,
            paste_with_preservation_fn=clipboard.paste_with_preservation,
            type_text_unicode_fn=lambda *a, **k: typed.append(a) or True,
            get_foreground_hwnd_fn=lambda: 10)
    monkeypatch.setattr(agents.paste_delivery, 'paste_preserving_clipboard', actual)
    result = call(rig.service, 'hello from agent')
    assert not typed
    if case == 'typed_allowlist':
        assert result['reason'] == 'delivered'
        assert copied == ['hello from agent'] and keys == [('ctrl', 'v')] and restored == [snapshot]
    else:
        assert result['reason'] == 'refused_delivery' and not keys
        if case == 'incomplete_snapshot': assert not copied and not restored
