"""Test the process gate without importing dictation or external proof files."""
import sys

if __name__ == '__main__' and sys.argv[1:] == ['--gate-child-836']:
    raise SystemExit(0)

import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'process_guard_836_test', ROOT / 'proofs/gate/process_guard_836.py')
POLICY_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY_MODULE)


@pytest.fixture
def policy():
    return POLICY_MODULE.TestChildPolicy(ROOT)


@pytest.fixture
def guarded(policy):
    # Audit hooks cannot be removed. Disable this test's hook on scope exit.
    active = True

    def audit(event, args):
        if active:
            policy.audit(event, args)

    sys.addaudithook(audit)
    try:
        yield policy
    finally:
        active = False


def test_allowed_test_helper_exits_zero(guarded):
    result = subprocess.run([sys.executable, '-B', __file__, '--gate-child-836'],
                            capture_output=True, timeout=8)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('target', ['dictation.py', 'main.py', 'Samsara.exe'])
def test_app_launch_raises_gate_error_before_creation(guarded, target):
    with pytest.raises(AssertionError, match='gate forbids process launch/signal:'):
        subprocess.Popen([sys.executable, str(ROOT / target)])


def test_exact_watchdog_is_allowed(policy, tmp_path, monkeypatch):
    monkeypatch.setattr(POLICY_MODULE.tempfile, 'tempdir', str(tmp_path))
    argv = [sys.executable, '-c', policy.watchdog, str(os.getpid()),
            str(tmp_path / 'heartbeat.json')]
    assert policy.permits(sys.executable, argv)
    assert policy.permits(sys.executable, subprocess.list2cmdline(argv))
    argv[2] += '\nprint("changed")'
    assert not policy.permits(sys.executable, argv)
    argv[2] = policy.watchdog
    argv[3] = str(os.getpid() + 1)
    assert not policy.permits(sys.executable, argv)


@pytest.mark.parametrize('args', [
    ['-c', 'print("tests/_ProcessWatchdog705")'],
    ['-m', 'dictation'], ['-m', 'samsara'], ['-m', 'tests..dictation'],
    ['-m', 'tests.missing_836'], ['tests/../dictation.py'],
    ['-c', 'from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=1, pipe_handle=2)',
     '--multiprocessing-fork'],
])
def test_unapproved_commands_are_denied(policy, args):
    assert not policy.permits(sys.executable, [sys.executable, *args])


def test_test_module_and_quoted_test_path(policy):
    argv = [sys.executable, '-B', str(Path(__file__).resolve()), '--gate-child-836']
    assert policy.permits(sys.executable, subprocess.list2cmdline(argv))
    assert policy.permits(sys.executable, [sys.executable, '-m', 'tests.test_no_bootstrap_gate_836'])
    assert not policy.permits('cmd.exe', argv)
    assert not policy.permits(sys.executable, ['other-python.exe', *argv[1:]])


def _multiprocessing_child_836():
    pass


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows spawn audit boundary')
def test_multiprocessing_test_target_exits_zero(guarded):
    import multiprocessing

    child = multiprocessing.get_context('spawn').Process(target=_multiprocessing_child_836)
    child.start()
    child.join(timeout=8)
    assert child.exitcode == 0
    child.close()
