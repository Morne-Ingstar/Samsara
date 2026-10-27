"""944: a dead worker cannot expire the replacement shard's deadline."""
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

from tests import test_ci_hard_watchdog_700 as watchdog_tests
from tools import ci_hang_reporter as reporter


def test_watchdog_retry_probe_keeps_background_listener_sleep_real(tmp_path, monkeypatch):
    real_sleep = time.sleep
    watchdog_tests.test_process_watchdog_close_retries_windows_reader(monkeypatch, tmp_path)
    assert time.sleep is real_sleep


def test_supervisor_survives_expired_dead_worker_while_replacement_runs(tmp_path, monkeypatch):
    dead = dict(pid=7, phase='test', nodeid='dead::crashed', progress=0, deadline=1)
    live = dict(pid=8, phase='test', nodeid='replacement::running', progress=2, deadline=20)
    for state in (dead, live):
        (tmp_path / f'{state["pid"]}.json').write_text(json.dumps(state), encoding='utf-8')
    root, replacement = Mock(pid=1), Mock(pid=8)
    root.children.return_value = [replacement]
    root.cpu_times.return_value = replacement.cpu_times.return_value = Mock(user=0, system=0)
    root.name.return_value = replacement.name.return_value = 'python.exe'
    child = Mock(pid=1, returncode=0)
    child.poll.side_effect = [None, 0, 0]
    monkeypatch.setattr(reporter.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(reporter.psutil, 'Process', lambda pid: root)
    ticks = iter([0, 2])
    monkeypatch.setattr(reporter, 'time', SimpleNamespace(
        monotonic=lambda: next(ticks), sleep=lambda seconds: None, strftime=time.strftime))
    dump = Mock()
    monkeypatch.setattr(reporter, 'dump_stacks', dump)
    assert reporter.supervise(['pytest'], tmp_path, report_seconds=1) == 0
    dump.assert_not_called()
    root.kill.assert_not_called()
    replacement.kill.assert_not_called()
    assert reporter.expired_items(reporter.heartbeat_state(tmp_path, pids={8}), 20) == ['replacement::running']
    assert '7.json' in reporter.heartbeat_state(tmp_path)  # Keep forensic evidence.


def test_interrupted_junit_excludes_an_already_completed_item(tmp_path):
    path = tmp_path / 'junit.xml'
    states = {'1.json': dict(pid=1, phase='between-tests', nodeid='completed::passed'),
              '2.json': dict(pid=2, phase='test', nodeid='running::interrupted')}
    reporter.report_interrupted_session(path, states, 1, lambda message: None)
    cases = ElementTree.parse(path).findall('.//testcase')
    assert [case.get('name') for case in cases] == ['running::interrupted']
    assert 'completed::passed' in cases[0].find('failure').text
