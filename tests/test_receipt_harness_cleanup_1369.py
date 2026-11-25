import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools.build_receipts import common, daily, runner


class FakeProcessTree:
    pid = 100

    def __init__(self):
        self.owned = {self.pid: 'Samsara.exe', 101: 'worker.exe'}
        self.unrelated = {999: 'Samsara.exe'}
        self.returncode = None
        self.waits = []
        self.terminated = []
        self.closed = False

    def poll(self):
        return self.returncode

    def empty(self):
        return not self.owned

    def wait(self, *, timeout):
        self.waits.append(timeout)
        if self.owned:
            raise subprocess.TimeoutExpired('fake tree', timeout)

    def exit(self, code):
        self.returncode = code
        self.owned.clear()

    def terminate_tree(self):
        self.terminated.extend(self.owned)
        self.exit(1)

    def close(self):
        assert not self.closed
        self.closed = True


@pytest.fixture
def owned_harness(tmp_path):
    harness = runner.Harness(tmp_path, tmp_path, tmp_path / 'report', {}, root=tmp_path)
    tree = FakeProcessTree()
    harness.process = tree
    harness.process_verified = True
    harness.channel = runner.Channel(tmp_path, tree)
    return harness, tree


def test_normal_quit_records_exit_without_termination(owned_harness):
    harness, tree = owned_harness

    def quit_request(operation, *, timeout):
        assert (operation, timeout) == ('quit', 10)
        tree.exit(0)
        return {'quit_requested': True}

    harness.channel.call = quit_request
    harness.quit()
    assert harness.shutdown_verified
    assert harness.lifecycle[-1]['cleanup_outcome'] == 'exited'
    assert tree.waits == [45] and tree.terminated == [] and tree.closed


@pytest.mark.parametrize('publish_fails', [False, True])
def test_ignored_or_unpublished_quit_terminates_only_the_owned_tree(owned_harness, monkeypatch, publish_fails):
    harness, tree = owned_harness
    expected = 'TimeoutExpired'
    if publish_fails:
        def fail_publish(*args):
            raise PermissionError(13, 'fake mailbox sharing violation')

        monkeypatch.setattr(runner, 'publish', fail_publish)
        expected = 'PermissionError'
    else:
        harness.channel.call = lambda *args, **kwargs: {'quit_requested': True}

    with pytest.raises(runner.ReceiptFailure, match=expected) as failure:
        harness.quit()
    if publish_fails:
        assert failure.value.diagnostic['action'] == 'publish_request'
    harness.quit()  # An outer finally can call quit again after a failed intermediate restart.
    assert not harness.shutdown_verified
    assert harness.lifecycle[-1]['cleanup_outcome'] == 'terminated'
    assert len(harness.lifecycle) == 1
    assert tree.waits == [45, 5] and tree.closed and tree.empty()
    assert tree.terminated == [tree.pid, 101]
    assert tree.unrelated == {999: 'Samsara.exe'}


def test_already_gone_records_exit_without_publishing(owned_harness):
    harness, tree = owned_harness
    tree.exit(1)
    with pytest.raises(runner.ReceiptFailure, match='ProbeExited'):
        harness.quit()
    assert harness.lifecycle[-1]['cleanup_outcome'] == 'gone'
    assert tree.closed and not tree.waits and not tree.terminated
    assert not list(harness.home.glob('request-*.json'))


def test_exited_root_with_live_descendant_still_requires_cleanup(owned_harness):
    harness, tree = owned_harness
    del tree.owned[tree.pid]
    tree.returncode = 1
    with pytest.raises(runner.ReceiptFailure, match='ProbeExited'):
        harness.quit()
    assert harness.lifecycle[-1]['cleanup_outcome'] == 'terminated'
    assert tree.terminated == [101]


def test_failed_explicit_termination_still_closes_the_job(owned_harness):
    harness, tree = owned_harness
    harness.channel.call = lambda *args, **kwargs: {'quit_requested': True}

    def fail_termination():
        raise OSError('fake termination failure')

    tree.terminate_tree = fail_termination
    with pytest.raises(runner.ReceiptFailure, match='OSError') as failure:
        harness.quit()
    assert failure.value.diagnostic['action'] == 'cleanup_tree'
    assert tree.closed and harness.cleanup_outcome == 'unverified'


@pytest.mark.parametrize('assignment_fails', [False, True])
def test_windows_launch_assigns_the_original_handle_before_resume(monkeypatch, assignment_fails):
    actions = []
    job = SimpleNamespace(Close=lambda: actions.append('close job'))
    process = SimpleNamespace(Close=lambda: actions.append('close process'))
    thread = SimpleNamespace(Close=lambda: actions.append('close thread'))

    def configure(target, info, limits):
        assert target is job and info == 9
        assert limits['BasicLimitInformation']['LimitFlags'] == 8192
        actions.append('kill on close')

    def create(exe, command, security, thread_security, inherit, flags, env, cwd, info):
        assert exe == 'fake app.exe' and command == '"fake app.exe"'
        assert not inherit and flags == 0x08000404
        assert (info.dwFlags, info.wShowWindow, info.dwX, info.dwY) == (5, 0, 1920, 20)
        actions.append('create suspended')
        return process, thread, 100, 200

    def assign(target, handle):
        assert target is job and handle is process
        actions.append('assign')
        if assignment_fails:
            raise PermissionError('nested assignment refused')

    def resume(handle):
        assert handle is thread
        actions.append('resume')

    def terminate(handle, code):
        assert handle is process and code == 1
        actions.append('terminate suspended process')

    def terminate_job(target, code):
        assert target is job and code == 1
        actions.append('terminate job')

    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(TerminateProcess=terminate))
    monkeypatch.setitem(sys.modules, 'win32job', SimpleNamespace(
        CreateJobObject=lambda security, name: job if security is None and name == '' else None,
        QueryInformationJobObject=lambda *_: {'BasicLimitInformation': {'LimitFlags': 0}},
        SetInformationJobObject=configure, AssignProcessToJobObject=assign,
        TerminateJobObject=terminate_job,
        JobObjectExtendedLimitInformation=9, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192))
    monkeypatch.setitem(sys.modules, 'win32process', SimpleNamespace(
        STARTUPINFO=SimpleNamespace, CreateProcess=create, ResumeThread=resume,
        CREATE_SUSPENDED=4, CREATE_UNICODE_ENVIRONMENT=1024))
    monkeypatch.setitem(sys.modules, 'pywintypes', SimpleNamespace(error=type('NativeError', (Exception,), {})))
    info = SimpleNamespace(dwFlags=5, wShowWindow=0, dwX=1920, dwY=20)
    args = ['fake app.exe']
    options = dict(cwd='fake cwd', env={}, startupinfo=info, creationflags=0x08000000)
    if assignment_fails:
        with pytest.raises(PermissionError, match='nested assignment refused'):
            runner.OwnedProcessTree.launch(args, **options)
        assert actions == ['kill on close', 'create suspended', 'assign', 'terminate suspended process',
                           'close process', 'close thread', 'close job']
    else:
        tree = runner.OwnedProcessTree.launch(args, **options)
        assert (tree.job, tree.handle, tree.pid) == (job, process, 100)
        assert actions == ['kill on close', 'create suspended', 'assign', 'resume', 'close thread']
        tree.terminate_tree()
        tree.close()
        assert actions[-3:] == ['terminate job', 'close job', 'close process']


@pytest.mark.parametrize('failure_phase', ['crash', 'interrupt_run', 'interrupt_quit'])
def test_daily_finally_closes_tree_and_writes_outcome(tmp_path, monkeypatch, failure_phase):
    root = tmp_path / 'scratch'
    app = root / 'app'
    app.mkdir(parents=True)
    (app / 'Samsara.exe').touch()
    take = tmp_path / 'fake.wav'
    take.touch()
    report_root = tmp_path / 'reports'
    tree = FakeProcessTree()
    monkeypatch.setattr(common, 'build_identity', lambda _: {'commit': 'abcdef0', 'build_ts': None})
    monkeypatch.setattr(daily, 'local_model_name', lambda _: 'base')
    monkeypatch.setattr(daily.replay_input, 'choose_take', lambda _: take)
    monkeypatch.setattr(daily, 'process_context', lambda: {})
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        EnumDisplayMonitors=lambda: [(1,)],
        GetMonitorInfo=lambda _: {'Device': daily.desktop.DEVICE, 'Work': (0, 0, 1000, 1000)}))

    def request(operation, *, timeout):
        if failure_phase == 'interrupt_quit':
            raise KeyboardInterrupt
        return {'quit_requested': True}

    def run(harness):
        harness.process = tree
        harness.process_verified = failure_phase != 'crash'
        harness.channel = SimpleNamespace(call=request)
        if failure_phase == 'crash':
            raise RuntimeError('fake startup failure before process verification')
        if failure_phase == 'interrupt_run':
            raise KeyboardInterrupt

    monkeypatch.setattr(daily.DailyHarness, 'run', run)
    args = ['--scratch-root', str(root), '--corpus', str(tmp_path), '--model', str(tmp_path),
            '--report-root', str(report_root)]
    if failure_phase == 'crash':
        assert daily.main(args) == 1
    else:
        with pytest.raises(KeyboardInterrupt):
            daily.main(args)
    assert tree.closed and tree.waits == [45, 5] and tree.terminated == [100, 101]
    completion = json.loads(next(report_root.glob('*/completion.json')).read_text())
    failure = json.loads(next(report_root.rglob('run-failure.json')).read_text())
    assert completion['cleanup_outcome'] == failure['cleanup_outcome'] == 'terminated'
    assert not completion['normal_shutdown_verified']
    assert not list(report_root.glob('*/RUNNING.lock'))
