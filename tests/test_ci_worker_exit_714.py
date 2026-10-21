"""714: leaked application threads cannot hold a completed xdist worker open."""
import sys
import os
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from tests import conftest as support
from tests.test_ci_hard_watchdog_700 import run_child


@pytest.mark.parametrize('completed_scopes', [0, 3])
def test_replacement_singleton_gets_lookahead_or_shutdown(completed_scopes):
    config = SimpleNamespace(option=SimpleNamespace(dist='loadfile'),
                             getvalue=lambda name: ['popen', 'popen'])
    scheduler = support.pytest_xdist_make_scheduler(config, None)
    node = Mock(shutting_down=False)
    scheduler.add_node(node)
    scheduler.registered_collections[node] = ['last.py::test_last']
    scheduler.workqueue['last.py'] = {'last.py::test_last': False}
    for number in range(completed_scopes):
        scope = f'done{number}.py'
        scheduler.workqueue[scope] = {scope + '::test_done': True}
    scheduler._reschedule(node)
    assert node.send_runtest_some.call_args_list[0].args == ([0],)
    assert not scheduler.workqueue
    node.shutdown.assert_called_once()


@pytest.mark.parametrize('config', [SimpleNamespace(),
                                   SimpleNamespace(option=SimpleNamespace(numprocesses=2))])
def test_guard_never_runs_in_controller_or_plain_pytest(monkeypatch, config):
    shutdown = Mock()
    monkeypatch.setitem(sys.modules, 'samsara.runtime.thread_registry',
                        SimpleNamespace(shutdown=shutdown))
    exit_process = Mock()
    monkeypatch.setattr(support, '_real_process_exit_701', exit_process)
    support._worker_exit_guard_714(config, 1, timeout=0)
    shutdown.assert_not_called()
    exit_process.assert_not_called()


@pytest.mark.timeout(0)  # Simulate session end, when per-test timers are disarmed.
def test_guard_stops_registered_worker_before_considering_force_exit(monkeypatch):
    from samsara.runtime import thread_registry

    stop = threading.Event()
    worker = thread_registry.spawn('cooperative-714', stop.wait, daemon=False)
    monkeypatch.setattr(thread_registry, '_shutdown_callbacks', [stop.set])
    exit_process = Mock()
    monkeypatch.setattr(support, '_real_process_exit_701', exit_process)
    try:
        support._worker_exit_guard_714(SimpleNamespace(workerinput={}), 0, timeout=1)
        assert not worker.is_alive()
        exit_process.assert_called_once_with(0)
    finally:
        stop.set()
        worker.join(1)


@pytest.mark.timeout(0)
def test_hung_registry_cleanup_shares_the_exit_deadline(monkeypatch, capsys):
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name='unit-leak-714', daemon=False)
    worker.start()
    cleanup_started = threading.Event()

    def shutdown(timeout):
        cleanup_started.set()
        stop.wait()

    monkeypatch.setitem(sys.modules, 'samsara.runtime.thread_registry',
                        SimpleNamespace(shutdown=shutdown))
    exit_process = Mock()
    monkeypatch.setattr(support, '_real_process_exit_701', exit_process)
    started = time.monotonic()
    try:
        support._worker_exit_guard_714(SimpleNamespace(workerinput={}), 2, timeout=0.1)
        assert cleanup_started.is_set()
        assert time.monotonic() - started < 2
        exit_process.assert_called_once_with(2)
        output = capsys.readouterr().err
        assert 'unit-leak-714' in output and 'daemon=False' in output
        assert 'threading.py' in output and 'wait' in output
    finally:
        stop.set()
        worker.join(1)


@pytest.mark.parametrize('fails', [False, True])
def test_xdist_reports_results_and_junit_despite_forever_blocked_thread(tmp_path, fails):
    neighbour = tmp_path / 'test_neighbour.py'
    neighbour.write_text('def test_neighbour(): assert True\n', encoding='utf-8')
    junit = tmp_path / 'results.xml'
    result, _ = run_child(tmp_path, f'''
import threading

def test_leaks_thread():
    threading.Thread(target=threading.Event().wait,
                     name='forever-blocked-714', daemon=False).start()
    assert {not fails!r}
''', hard='15', soft='120', extra=(
        '-p', 'xdist.plugin', '-n', '2', '--dist', 'loadfile',
        '--max-worker-restart=0', '--junitxml=' + str(junit), str(neighbour)))
    output = result.stdout + result.stderr
    assert result.returncode == int(fails), output
    assert ('1 failed, 1 passed' if fails else '2 passed') in output, output
    assert "[worker exit guard 714] 'forever-blocked-714'" in output, output
    assert 'daemon=False' in output, output
    assert 'threading.py' in output and 'wait' in output, output
    assert 'node down' not in output and 'crashed while running' not in output, output
    cases = ElementTree.parse(junit).findall('.//testcase')
    assert {case.attrib['name'] for case in cases} == {'test_leaks_thread', 'test_neighbour'}
    assert sum(case.find('failure') is not None for case in cases) == int(fails)


def test_crash_replacement_finishes_remaining_file_and_leaking_neighbour(tmp_path):
    neighbour = tmp_path / 'test_neighbour.py'
    neighbour.write_text('''import threading
def test_neighbour():
    threading.Thread(target=threading.Event().wait,
                     name='surviving-worker-leak-714', daemon=False).start()
def test_neighbour_after(): assert True
''', encoding='utf-8')
    junit = tmp_path / 'results.xml'
    result, _ = run_child(tmp_path, '''
from conftest import _real_process_exit_701 as real_exit

def test_before_crash(): assert True
def test_crash(): real_exit(1)
def test_after_crash(): assert True
''', hard='15', soft='120', extra=(
        '-v', '-p', 'xdist.plugin', '-n', '2', '--dist', 'loadfile',
        '--max-worker-restart=100', '--junitxml=' + str(junit), str(neighbour)))
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert '1 failed, 4 passed' in output, output
    assert 'replacing crashed worker' in output, output
    assert "[worker exit guard 714] 'surviving-worker-leak-714'" in output, output
    cases = ElementTree.parse(junit).findall('.//testcase')
    assert {case.attrib['name'] for case in cases} == {
        'test_before_crash', 'test_crash', 'test_after_crash',
        'test_neighbour', 'test_neighbour_after'}
    # pytest's JUnit writer classifies xdist's abrupt worker loss as an error.
    assert sum(case.find('error') is not None for case in cases) == 1
    assert next(case for case in cases if case.get('name') == 'test_crash').find('error') is not None


@pytest.mark.parametrize('previous', [None, 'valid', 'partial'])
def test_reporter_junit_preserves_results_and_identifies_last_items(tmp_path, previous):
    from tools.ci_hang_reporter import report_interrupted_session

    path = tmp_path / 'junit.xml'
    if previous == 'valid':
        path.write_text('<testsuites tests="1" failures="0"><testsuite tests="1" '
                        'failures="0"><testcase name="completed"/></testsuite></testsuites>')
    elif previous == 'partial':
        path.write_text('<testsuites>partial')
    states = {'7.json': {'pid': 7, 'phase': 'test', 'nodeid': 'tests/test_crashed.py::test_one'},
              '8.json': {'pid': 8, 'phase': 'shutdown', 'nodeid': ''}}
    messages = []
    report_interrupted_session(path, states, 12.5, messages.append)
    root = ElementTree.parse(path).getroot()
    failures = root.findall('.//testcase[failure]')
    assert len(failures) == 1
    assert failures[0].get('name') == 'tests/test_crashed.py::test_one'
    assert 'last observed' in failures[0].find('failure').text
    assert 'shutdown' in failures[0].find('failure').text
    assert any('1 failed (interrupted session)' in message for message in messages)
    if previous == 'valid':
        assert root.find('.//testcase[@name="completed"]') is not None
        assert root.get('tests') == '2' and root.get('failures') == '1'
    if previous == 'partial':
        assert path.with_suffix('.xml.interrupted').read_text() == '<testsuites>partial'


def test_reporter_ends_stalled_session_with_summary_and_junit(tmp_path):
    reporter = Path(__file__).resolve().parents[1] / 'tools/ci_hang_reporter.py'
    junit = tmp_path / 'junit.xml'
    source = '''import json, os, time
from pathlib import Path
root = Path(os.environ['SAMSARA_CI_HEARTBEAT_DIR'])
(root / (str(os.getpid()) + '.json')).write_text(json.dumps({
    'pid': os.getpid(), 'phase': 'test', 'nodeid': 'tests/test_stalled.py::test_stalled'}))
time.sleep(60)
'''
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('SAMSARA_TEST_SHARD_')
           and key != 'SAMSARA_CI_HEARTBEAT_DIR'}
    # Avoid external stack-dumper dependencies; exercise the cooperative fallback.
    env['PATH'] = ''
    result = subprocess.run([
        sys.executable, '-u', str(reporter), '--state-dir', str(tmp_path / 'state'),
        '--idle-seconds', '1', '--report-seconds', '1', '--max-seconds', '10',
        '--junit-path', str(junit), '--', sys.executable, '-c', source],
        env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20)
    output = result.stdout + result.stderr
    assert result.returncode == 124, output
    assert '1 failed (interrupted session)' in output, output
    assert 'killed owned process tree' in output, output
    case = ElementTree.parse(junit).find('.//testcase')
    assert case.get('name') == 'tests/test_stalled.py::test_stalled'
    assert case.find('failure').get('type') == 'CIHang'


def test_winrt_event_loop_is_daemon_and_can_stop():
    from samsara.tts.winrt_helper import WinRTHelper

    helper = WinRTHelper()
    try:
        assert helper._thread.daemon
    finally:
        helper.shutdown()
    assert not helper._thread.is_alive()
    helper._loop.close()


def test_readiness_monitor_uses_daemon_registry_thread(monkeypatch):
    from samsara import ava_readiness
    from samsara.runtime import thread_registry

    monkeypatch.setattr(ava_readiness, '_monitor', None)
    monkeypatch.setattr(ava_readiness, 'probe_configured_provider', lambda app: None)
    workers = []

    def spawn(name, target):
        worker = thread_registry.spawn(name, target)
        workers.append(worker)
        return worker

    monitor = ava_readiness.start_monitor(SimpleNamespace(), spawn)
    try:
        assert len(workers) == 1 and workers[0].daemon
    finally:
        monitor.stop()
        workers[0].join(1)
    assert not workers[0].is_alive()
