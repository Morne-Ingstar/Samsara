"""Exit breadcrumbs and private session summaries; process-ending calls are faked."""
import ast
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from samsara import boot, process_exit

ROOT = Path(__file__).resolve().parents[1]


class RecordingHandler(logging.Handler):
    def __init__(self, events):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.append(record.getMessage())

    def flush(self):
        self.events.append('flushed')


@pytest.fixture
def trace_log():
    events = []
    log = logging.getLogger('test.exit552')
    old = log.handlers[:], log.propagate, log.level
    log.handlers = [RecordingHandler(events)]
    log.propagate = False
    log.setLevel(logging.WARNING)
    yield log, events
    log.handlers, log.propagate, log.level = old


def test_sys_exit_logs_and_flushes_before_raising(trace_log, monkeypatch):
    log, events = trace_log
    monkeypatch.setattr(process_exit.sys, 'exit', lambda code: events.append(('exit', code)))
    process_exit.exit_with_trace(3, 'probe-exit', log=log)
    assert 'reason=probe-exit code=3' in events[0]
    assert events[1:] == ['flushed', ('exit', 3)]


@pytest.mark.parametrize('reason,tag,target_pid', [
    ('ducking-child-tree-kill', 'CHILD-END', 987),
    ('ducking-child-tree-kill', 'CHILD-END', None),
    ('agent-child-tree-kill', 'CHILD-END', 987),
    ('restart-helper-terminate', 'CHILD-END', 987),
    ('model-pull-child-terminate', 'CHILD-END', 987),
    ('worker-system-exit-thread-only', 'THREAD-EXIT', None),
    ('hard-exit', 'PROCESS-END', None),
    ('qt-fatal', 'PROCESS-END', None),
    ('missing-vc-runtime', 'PROCESS-END', None),
    ('existing-instance-pid', 'PROCESS-END', None),
    ('existing-instance-mutex', 'PROCESS-END', None),
    ('existing-instance-file-lock', 'PROCESS-END', None),
    ('update-helper-launched-requesting-quit', 'PROCESS-END', None),
])
def test_tag_names_what_is_ending_and_still_flushes(trace_log, reason, tag, target_pid):
    log, events = trace_log
    process_exit.trace_exit(reason, log=log, target_pid=target_pid)
    target = str(target_pid) if target_pid is not None else 'self'
    assert events == [f'[{tag}] reason={reason} code=none target_pid={target}', 'flushed']


def test_broken_handler_does_not_prevent_other_flushes(trace_log):
    log, events = trace_log
    class Broken(RecordingHandler):
        def emit(self, record):
            raise OSError('closed')
        def flush(self):
            raise OSError('closed')
    log.handlers.insert(0, Broken(events))
    process_exit.trace_exit('probe-exit', log=log)
    assert events == ['flushed']


def test_watchdog_writes_without_handler_flush_or_lock(tmp_path, monkeypatch):
    log = logging.getLogger('Samsara')
    file = tmp_path / 'exit.log'
    handler = logging.FileHandler(file)
    monkeypatch.setattr(log, 'handlers', [handler])
    monkeypatch.setattr(log, 'propagate', False)
    monkeypatch.setattr(handler, 'flush', lambda: pytest.fail('watchdog used handler flush'))
    monkeypatch.setattr(handler, 'acquire', lambda: pytest.fail('watchdog took handler lock'))
    seen = []
    monkeypatch.setattr(process_exit, '_terminate', seen.append)
    process_exit._watchdog_terminate(7)
    assert seen == [7]
    assert 'reason=flush-watchdog code=7' in file.read_text()
    # Restore handler methods before close (which must take its lock).
    monkeypatch.undo()
    handler.close()


def test_native_tail_can_write_after_handler_is_closed(tmp_path, monkeypatch):
    import os
    log = logging.getLogger('Samsara')
    file = tmp_path / 'closed.log'
    handler = logging.FileHandler(file)
    monkeypatch.setattr(log, 'handlers', [handler])
    monkeypatch.setattr(log, 'propagate', False)
    monkeypatch.setattr(process_exit, '_exit_fds', ())
    process_exit._retain_exit_descriptors()
    try:
        handler.close()
        process_exit._raw_exit_trace('terminate-process', 0)
        assert 'reason=terminate-process code=0' in file.read_text()
    finally:
        for fd in process_exit._exit_fds:
            os.close(fd)


def test_hard_exit_records_before_shutdown_and_native_call(monkeypatch):
    events = []
    class Timer:
        def __init__(self, *args, **kwargs):
            self.daemon = False
        def start(self):
            assert self.daemon
            events.append('watchdog')
    monkeypatch.setattr(process_exit, 'threading', SimpleNamespace(Timer=Timer))
    monkeypatch.setattr(process_exit, 'trace_exit', lambda *a: events.append('trace'))
    monkeypatch.setattr(process_exit, 'logging', SimpleNamespace(shutdown=lambda: events.append('shutdown')))
    monkeypatch.setattr(process_exit, 'sys', SimpleNamespace(stdout=None, stderr=None))
    monkeypatch.setattr(process_exit, '_terminate', lambda code: events.append(('terminate', code)))
    process_exit.hard_exit(4)
    assert events == ['watchdog', 'trace', 'shutdown', ('terminate', 4)]


def test_terminate_and_fallback_each_leave_a_breadcrumb(monkeypatch):
    events = []
    def fn(value):
        return lambda *a: events.append(value) or -1
    kernel = SimpleNamespace(GetCurrentProcess=fn('handle'), TerminateProcess=fn('native'))
    monkeypatch.setattr(process_exit.ctypes, 'WinDLL', lambda *a, **k: kernel)
    monkeypatch.setattr(process_exit, '_raw_exit_trace', lambda reason, code: events.append(reason))
    monkeypatch.setattr(process_exit, 'os', SimpleNamespace(_exit=lambda code: events.append('fallback')))
    process_exit._terminate(5)
    assert events == ['terminate-process', 'handle', 'native', 'os-exit-fallback', 'fallback']


def test_qt_fatal_flushes_nonpropagating_logger(trace_log):
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    log, events = trace_log
    prior = qInstallMessageHandler(None)
    try:
        handler = boot.install_qt_message_handler(log)
        handler(QtMsgType.QtFatalMsg, None, 'fixed probe message')
    finally:
        qInstallMessageHandler(prior)
    assert any('reason=qt-fatal' in event for event in events)
    assert events[-1] == 'flushed'


def test_worker_system_exit_is_reported_as_thread_only(trace_log, monkeypatch):
    import threading
    log, events = trace_log
    monkeypatch.setattr(threading, 'excepthook', lambda args: None)
    monkeypatch.setattr(sys, 'unraisablehook', lambda args: None)
    hook, _ = boot.install_exception_hooks(log)
    hook(SimpleNamespace(exc_type=SystemExit, exc_value=SystemExit(2), thread=None))
    assert events[0].startswith('[THREAD-EXIT] reason=worker-system-exit-thread-only')
    assert events[-1] == 'flushed'


@pytest.mark.parametrize('refusal', ['pid', 'mutex'])
def test_single_instance_refusal_trace_precedes_exit(refusal, trace_log, monkeypatch, tmp_path):
    from samsara import single_instance
    log, events = trace_log
    monkeypatch.setattr(boot, 'logger', log)
    if refusal == 'pid':
        marker = tmp_path / 'instance'
        marker.write_text('987')
        monkeypatch.setattr(boot, '_is_samsara_process', lambda pid: True)
        call = lambda: boot._steal_stale_lock_if_any(marker)
    else:
        def acquire():
            raise single_instance.AlreadyRunningError('probe-mutex')
        monkeypatch.setattr(single_instance, 'acquire_single_instance_mutex', acquire)
        monkeypatch.setattr(single_instance, 'bring_existing_forward', lambda name: None, raising=False)
        call = boot._check_single_instance
    with pytest.raises(SystemExit) as result:
        call()
    assert result.value.code == 0
    assert any('reason=existing-instance-' + refusal in event for event in events)
    assert events[-1] == 'flushed'


def test_next_start_keeps_only_timestamp_and_leading_tag(tmp_path, trace_log):
    log, events = trace_log
    (tmp_path / boot.SESSION_MARKER).write_text(json.dumps({'pid': 987, 'started': '2026-10-12 08:24:35'}))
    (tmp_path / 'samsara.log.1').write_text(
        '2026-10-12 08:24:35,010 - INFO - Samsara starting at 2026-10-12 08:24:35\n'
        '2026-10-12 08:49:43,123 - WARNING - [QT] message body excluded\n')
    (tmp_path / 'samsara.log').write_text(
        '2026-10-12 09:01:00,001 - INFO - Samsara starting at 2026-10-12 09:01:00\n'
        '2026-10-12 09:01:00,010 - INFO - [BOOT] current startup\n')
    previous = boot.begin_session(tmp_path, log)
    message = '\n'.join(events)
    assert previous['pid'] == 987
    assert '08:49:43,123 tag=[QT]' in message
    assert '08:24:35' in message
    assert 'message body excluded' not in message
    assert 'current startup' not in message


@pytest.mark.parametrize('content,expected', [
    ('2026-10-12 08:49:43,000 - INFO - no tag [PRIVATE]\n', 'untagged'),
    ('traceback continuation [PRIVATE]\n', 'unknown'),
    ('2026-10-11 08:49:43,000 - INFO - [OLD] old run\n', 'unknown'),
])
def test_tail_never_uses_brackets_inside_message(tmp_path, content, expected):
    (tmp_path / 'samsara.log').write_text(content)
    assert boot._previous_log_tail(tmp_path, '2026-10-12 08:24:35')[1] == expected


@pytest.mark.parametrize('marker', ['[]', 'null', 'broken'])
def test_bad_marker_still_starts_new_session(tmp_path, trace_log, marker):
    (tmp_path / boot.SESSION_MARKER).write_text(marker)
    boot.begin_session(tmp_path, trace_log[0])
    assert isinstance(json.loads((tmp_path / boot.SESSION_MARKER).read_text()), dict)


def extracted(path, name, **scope):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8-sig'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'), scope)
    return scope[name]


@pytest.mark.parametrize('path,name', [
    ('samsara/audio_ducking.py', '_kill_process_tree'),
    ('samsara/agent_channel/cli_agents.py', '_kill_tree'),
])
def test_child_tree_kill_records_before_fake_kill(path, name, monkeypatch, trace_log):
    log, events = trace_log
    monkeypatch.setattr(process_exit, 'logging', SimpleNamespace(getLogger=lambda *a: log))
    child = SimpleNamespace(pid=987, poll=lambda: None, kill=lambda: events.append('kill'), wait=lambda **k: None)
    psutil = SimpleNamespace(Process=lambda pid: SimpleNamespace(children=lambda **k: [], kill=child.kill), wait_procs=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, 'psutil', psutil)
    function = extracted(path, name, sys=SimpleNamespace(platform='linux'))
    function(child)
    assert events[0].startswith('[CHILD-END] reason=')
    assert events[0].endswith('target_pid=987')
    assert events[1:] == ['flushed', 'kill']


def test_model_pull_termination_trace(monkeypatch):
    events = []
    monkeypatch.setattr(process_exit, 'trace_exit', lambda *a, **k: events.append('trace'))
    from unittest.mock import Mock
    from PySide6.QtWidgets import QLineEdit
    owner = SimpleNamespace(_generation=1, _pages=[], _picker=None,
                            _ollama_recheck_timer=SimpleNamespace(stop=lambda: events.append('stop-timer')),
                            _pull_proc=SimpleNamespace(pid=987, terminate=lambda: events.append('terminate')),
                            _consent_check=Mock(), _api_key=Mock(), _show_key=Mock(),
                            hide=lambda: events.append('hide'))
    function = extracted('samsara/ui/ava_guide_qt.py', 'closeEvent',
                         logger=logging.getLogger(), QLineEdit=QLineEdit)
    function(owner, SimpleNamespace(ignore=lambda: events.append('ignore')))
    # The guide is now reusable: closing invalidates its callbacks and hides
    # it, without killing the model download or reporting a process death.
    assert events == ['stop-timer', 'hide', 'ignore']
    assert owner._closed and owner._generation == 2
    owner._consent_check.setChecked.assert_called_once_with(False)
    owner._api_key.setEchoMode.assert_called_once_with(QLineEdit.EchoMode.Password)


def test_update_request_trace_after_helper_before_quit(monkeypatch):
    events = []
    monkeypatch.setattr(process_exit, 'trace_exit', lambda *a, **k: events.append('trace'))
    owner = SimpleNamespace(_closed=False, app=SimpleNamespace(
        _resolve_parked_draft_before_quit=lambda: True,
        quit_app=lambda **k: events.append('quit')),
        _status=SimpleNamespace(setText=lambda value: None),
        _primary=SimpleNamespace(setEnabled=lambda value: None),
        _beta_choice=SimpleNamespace(setEnabled=lambda value: None))  # 540's checkbox
    function = extracted('samsara/ui/update_qt.py', '_on_prepare_finished',
        channel_token=lambda app: (False, 0),  # 540: the choice did not change
        launch_prepared_update=lambda *a, **k: events.append('helper'),
        os=SimpleNamespace(getpid=lambda: 987), logger=logging.getLogger(),
        QTimer=SimpleNamespace(singleShot=lambda delay, cb: cb()))
    function(owner, object(), '')
    assert events == ['helper', 'trace', 'quit']


def test_restart_helper_failure_trace(monkeypatch, tmp_path):
    from samsara import app_restart
    events = []
    monkeypatch.setattr(process_exit, 'trace_exit', lambda *a, **k: events.append('trace'))
    helper = SimpleNamespace(pid=987, poll=lambda: 2, returncode=2,
                             terminate=lambda: events.append('terminate'))
    monkeypatch.setattr(app_restart.subprocess, 'Popen', lambda *a, **k: helper)
    monkeypatch.setattr(app_restart.tempfile, 'gettempdir', lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match='exited early'):
        app_restart.start_handoff(['probe'], cwd=str(tmp_path), helper_argv=['probe'])
    assert events == ['trace', 'terminate']


def test_missing_runtime_exit_trace(trace_log, monkeypatch):
    import ctypes
    import PySide6.QtWidgets as widgets
    log, events = trace_log
    monkeypatch.setattr(boot, 'logger', log)
    def missing(*args):
        raise OSError('missing')
    monkeypatch.setattr(ctypes.cdll, 'LoadLibrary', missing)
    monkeypatch.setattr(widgets, 'QApplication', SimpleNamespace(instance=lambda: object()))
    monkeypatch.setattr(widgets, 'QMessageBox', SimpleNamespace(critical=lambda *a: None))
    with pytest.raises(SystemExit):
        boot.check_vc_redistributable()
    assert 'reason=missing-vc-runtime' in events[0]
    assert events[-1] == 'flushed'


def test_file_lock_refusal_trace(trace_log, monkeypatch, tmp_path):
    log, events = trace_log
    monkeypatch.setattr(boot, 'logger', log)
    # Replace boot's sys object, never the interpreter-wide platform flag.
    monkeypatch.setattr(boot, 'sys', SimpleNamespace(platform='linux'))
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(boot, '_steal_stale_lock_if_any', lambda path: None)
    def refused(*args):
        raise OSError('locked')
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=refused, LOCK_EX=1, LOCK_NB=2))
    with pytest.raises(SystemExit):
        boot._check_single_instance()
    assert any('reason=existing-instance-file-lock' in event for event in events)
    assert events[-1] == 'flushed'
