"""700: the hard timeout survives native GIL holds and covers fixture teardown."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from tests import conftest as support


NATIVE_BLOCK = '''
import ctypes
import os
import sys
import time

def native_block(seconds):
    # Unlike WinDLL/CDLL, PyDLL keeps the GIL throughout the foreign call.
    if sys.platform == 'win32':
        sleep = ctypes.PyDLL('kernel32').Sleep
        sleep.argtypes = [ctypes.c_ulong]
        sleep.restype = None
        duration = int(seconds * 1000)
    else:
        sleep = ctypes.PyDLL(None).sleep
        sleep.argtypes = [ctypes.c_uint]
        sleep.restype = ctypes.c_uint
        duration = int(seconds)
    os.write(2, f'NATIVE_ENTER {time.monotonic()}\\n'.encode())
    sleep(duration)
'''


def run_child(tmp_path, source, *, hard='5', soft='1', extra=(), conftest_extra='',
              step_watchdog=False):
    """Use the real shared hooks in an isolated, bounded child pytest process."""
    shutil.copyfile(Path(__file__).with_name('conftest.py'), tmp_path / 'conftest.py')
    with (tmp_path / 'conftest.py').open('a', encoding='utf-8') as handle:
        handle.write(textwrap.dedent('''

            @pytest.fixture(autouse=True)
            def _speech_presence_unknown_for_decoder_fixtures():
                # Watchdog probes have no decoders. Keep cold numpy imports
                # out of their sub-second budgets, including replacement workers.
                pass
        '''))
        handle.write('\n' + textwrap.dedent(conftest_extra))
    (tmp_path / 'test_probe.py').write_text(textwrap.dedent(source), encoding='utf-8')
    (tmp_path / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    home = tmp_path / 'home'
    home.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith('SAMSARA_TEST_SHARD_')
           and key not in ('SAMSARA_TEST_HARD_TIMEOUT', 'SAMSARA_CI_HEARTBEAT_DIR')}
    env.update(SAMSARA_TEST_HARD_TIMEOUT=hard,
               PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PYTEST_ADDOPTS='', PYTEST_PLUGINS='',
               PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
    for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA',
                'LOCALAPPDATA', 'TMP', 'TEMP', 'TMPDIR'):
        env[key] = str(home)
    env['PYTHONPATH'] = os.pathsep.join((str(tmp_path), str(Path(__file__).resolve().parents[1]),
                                       env.get('PYTHONPATH', '')))
    command = [sys.executable, '-m', 'pytest', '-c', str(tmp_path / 'pytest.ini'),
               '--confcutdir', str(tmp_path), '-p', 'no:cacheprovider',
               '-p', 'pytest_timeout', '--timeout=' + soft, '--timeout-method=thread',
               '--basetemp', str(tmp_path / 'pytest-temp'), '-q', '-s', *extra,
               str(tmp_path / 'test_probe.py')]
    if step_watchdog:
        command = [sys.executable, '-u', str(Path(__file__).resolve().parents[1] /
                   'tools/ci_hang_reporter.py'), '--state-dir', str(tmp_path / 'step'),
                   '--idle-seconds', '2', '--report-seconds', '0.5', '--', *command]
    started = time.monotonic()
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=30)
    finished = time.monotonic()
    (tmp_path / 'child.stdout.txt').write_text(result.stdout, encoding='utf-8')
    (tmp_path / 'child.stderr.txt').write_text(result.stderr, encoding='utf-8')
    (tmp_path / 'child.result.txt').write_text(
        f'exit={result.returncode}; elapsed={finished - started:.3f}s\n', encoding='utf-8')
    return result, finished


def test_child_environment_isolates_shards_and_watchdog(tmp_path, monkeypatch):
    for key, value in {'SAMSARA_TEST_SHARD_INDEX': '3', 'SAMSARA_TEST_SHARD_TOTAL': '4',
                       'SAMSARA_TEST_SHARD_FUTURE': 'parent',
                       'SAMSARA_TEST_HARD_TIMEOUT': '999',
                       'SAMSARA_TEST_AUDIO_LOG': 'preserved-audio-log'}.items():
        monkeypatch.setenv(key, value)
    captured = {}

    def run(command, **kwargs):
        captured.update(kwargs['env'])
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(subprocess, 'run', run)
    run_child(tmp_path, 'def test_probe(): pass', hard='7')
    assert not any(key.startswith('SAMSARA_TEST_SHARD_') for key in captured)
    assert captured['SAMSARA_TEST_HARD_TIMEOUT'] == '7'
    assert captured['SAMSARA_TEST_AUDIO_LOG'] == 'preserved-audio-log'


def test_native_gil_hold_defeats_the_soft_timeout(tmp_path):
    result, finished = run_child(tmp_path, NATIVE_BLOCK + '''
def test_native_gil():
    native_block(3)
    time.sleep(2)  # Give the overdue pytest-timeout thread the GIL.
''', hard='0')
    assert result.returncode != 0, result.stdout + result.stderr
    entered = float(re.search(r'NATIVE_ENTER (\d+\.\d+)', result.stderr).group(1))
    assert finished - entered >= 2.8, result.stderr
    assert '+++ Timeout +++' in result.stdout + result.stderr
    assert '[hard watchdog' not in result.stderr


def test_hard_watchdog_exits_while_native_code_holds_gil(tmp_path):
    result, finished = run_child(tmp_path, NATIVE_BLOCK + '''
def test_native_gil():
    native_block(60)
''')
    assert result.returncode != 0, result.stdout + result.stderr
    entered = float(re.search(r'NATIVE_ENTER (\d+\.\d+)', result.stderr).group(1))
    assert 3 <= finished - entered < 10, result.stderr
    assert '[hard watchdog 5s] test_probe.py::test_native_gil' in result.stderr
    assert 'Timeout (' in result.stderr and 'native_block' in result.stderr
    assert '+++ Timeout +++' not in result.stdout + result.stderr


def test_fast_tests_rearm_and_cancel_before_session_finish(tmp_path):
    # CI descheduling can turn the 0.2s sleep into >0.5s. Keep the same
    # ratios: the combined tests and session finish each exceed one budget.
    budget = 2.0 if os.environ.get('CI') else 0.5
    result, _ = run_child(tmp_path, '''
import time
import pytest

@pytest.mark.parametrize('number', range(3))
def test_fast(number):
    time.sleep(TEST_DELAY)
'''.replace('TEST_DELAY', str(budget * 0.4)), hard=str(budget), soft='0', conftest_extra='''
def pytest_sessionfinish(session, exitstatus):
    import time
    time.sleep(FINISH_DELAY)  # Longer than a test's budget, after its timer was cancelled.
    print('SESSION_FINISHED')
'''.replace('FINISH_DELAY', str(budget + 0.2)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert '3 passed' in result.stdout and 'SESSION_FINISHED' in result.stdout
    assert result.stderr.count(f'[hard watchdog {budget:g}s] test_probe.py::test_fast') == 3
    assert 'Timeout (' not in result.stderr


@pytest.mark.parametrize('phase', ['setup', 'teardown', 'failed_teardown'])
def test_watchdog_covers_fixture_lifecycle(tmp_path, phase):
    source = NATIVE_BLOCK + '''
import pytest

@pytest.fixture
def lifecycle():
    SETUP
    yield
    TEARDOWN

def test_lifecycle(lifecycle):
    BODY
'''
    source = source.replace('SETUP', 'native_block(60)' if phase == 'setup' else 'pass')
    source = source.replace('TEARDOWN', 'pass' if phase == 'setup' else 'native_block(60)')
    source = source.replace('BODY', 'assert False' if phase == 'failed_teardown' else 'pass')
    result, _ = run_child(tmp_path, source, hard='0.5', soft='0')
    assert result.returncode != 0, result.stdout + result.stderr
    assert '[hard watchdog 0.5s] test_probe.py::test_lifecycle' in result.stderr
    assert 'Timeout (' in result.stderr and 'native_block' in result.stderr


def test_xdist_reports_crash_and_restarts_for_next_test(tmp_path):
    result, _ = run_child(tmp_path, NATIVE_BLOCK + '''
def test_native_gil():
    native_block(60)

def test_after_crash():
    assert 'samsara.speech_presence' not in sys.modules
    assert 'numpy' not in sys.modules
''', hard='0.5', soft='0', extra=(
        '-p', 'xdist.plugin', '-n', '1', '--dist', 'loadfile', '--max-worker-restart=2'))
    assert result.returncode == 1, result.stdout + result.stderr
    assert '1 failed, 1 passed' in result.stdout
    assert 'crashed while running' in result.stdout and 'test_native_gil' in result.stdout
    assert '[hard watchdog 0.5s] test_probe.py::test_native_gil' in result.stderr
    assert 'Timeout (' in result.stderr


@pytest.mark.parametrize('sabotage', ['cancel', 'disable', 'replace'])
def test_process_watchdog_survives_disarmed_faulthandler(tmp_path, sabotage):
    result, finished = run_child(tmp_path, NATIVE_BLOCK + f'''
import faulthandler

def test_disarmed():
    if {sabotage!r} == 'disable':
        faulthandler.disable()
    if {sabotage!r} == 'replace':
        faulthandler.dump_traceback_later(60, file=sys.stderr, exit=True)
    else:
        faulthandler.cancel_dump_traceback_later()
    native_block(60)
''', hard='1', soft='0')
    assert result.returncode != 0, result.stdout + result.stderr
    entered = float(re.search(r'NATIVE_ENTER (\d+\.\d+)', result.stderr).group(1))
    assert 1 <= finished - entered < 10, result.stderr
    assert '[process watchdog] killed pid=' in result.stderr
    assert 'test_probe.py::test_disarmed' in result.stderr


def test_process_watchdog_restarts_xdist_and_names_the_crashed_item(tmp_path):
    result, _ = run_child(tmp_path, NATIVE_BLOCK + '''
import faulthandler

def test_disarmed():
    faulthandler.disable()
    faulthandler.cancel_dump_traceback_later()
    native_block(60)

def test_after_crash():
    assert True
''', hard='1', soft='0', extra=(
        '-p', 'xdist.plugin', '-n', '2', '--dist', 'loadfile', '--max-worker-restart=2'))
    assert result.returncode == 1, result.stdout + result.stderr
    assert '1 failed, 1 passed' in result.stdout
    assert '[process watchdog] killed pid=' in result.stderr
    assert 'test_probe.py::test_disarmed' in result.stderr


def test_process_watchdog_covers_outer_protocol_teardown(tmp_path):
    result, _ = run_child(tmp_path, '''
def test_passes_before_outer_teardown():
    assert True
''', hard='1', soft='0', conftest_extra=NATIVE_BLOCK + '''
import faulthandler

@pytest.hookimpl(hookwrapper=True, trylast=True, specname='pytest_runtest_protocol')
def pytest_outer_teardown_probe(item):
    yield
    faulthandler.cancel_dump_traceback_later()
    native_block(60)
''')
    assert result.returncode != 0, result.stdout + result.stderr
    assert '[process watchdog] killed pid=' in result.stderr
    assert 'test_probe.py::test_passes_before_outer_teardown' in result.stderr


def test_default_exceeds_ci_soft_timeout(monkeypatch):
    monkeypatch.delenv('SAMSARA_TEST_HARD_TIMEOUT', raising=False)
    assert support._hard_timeout_seconds_700() == 180


def test_process_watchdog_close_retries_windows_reader(monkeypatch, tmp_path):
    from unittest.mock import Mock
    watchdog = object.__new__(support._ProcessWatchdog705)
    watchdog.directory = tmp_path / 'watchdog'
    watchdog.directory.mkdir()
    watchdog.heartbeat = Mock()
    watchdog.heartbeat.unlink.side_effect = [PermissionError('reader open'), None]
    watchdog.process = Mock()
    pause = Mock()
    monkeypatch.setattr(time, 'sleep', pause)
    watchdog.close()
    assert watchdog.heartbeat.unlink.call_count == 2
    pause.assert_called_once_with(0.01)
    watchdog.process.wait.assert_called_once_with(timeout=2)
    watchdog.process.kill.assert_not_called()
    assert not watchdog.directory.exists()


@pytest.mark.parametrize('phase', ['collection', 'sessionfinish', 'atexit', 'native'])
def test_step_supervisor_covers_gaps_outside_item_watchdogs(tmp_path, phase):
    hook = NATIVE_BLOCK + '''
import atexit
import faulthandler
'''
    if phase == 'collection':
        hook += '\nnative_block(60)\n'
    elif phase == 'sessionfinish':
        hook += '\ndef pytest_sessionfinish(session): native_block(60)\n'
    elif phase == 'atexit':
        hook += '\natexit.register(native_block, 60)\n'
    result, _ = run_child(tmp_path, NATIVE_BLOCK + '''
def test_probe():
    BODY
'''.replace('BODY', 'native_block(60)' if phase == 'native' else 'pass'),
        hard='0', soft='0', conftest_extra=hook, step_watchdog=True)
    assert result.returncode == 124, result.stdout + result.stderr
    assert 'deadline exceeded' in result.stdout
    assert 'killed owned process tree' in result.stdout
    assert 'cpu_seconds' in result.stdout


def test_step_supervisor_preserves_normal_exit_and_diagnostics(tmp_path):
    result, _ = run_child(tmp_path, 'def test_probe(): pass', hard='0', soft='0',
                          step_watchdog=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'pytest exited 0' in result.stdout
    states = list((tmp_path / 'step').glob('*.json'))
    assert len(states) == 1
    import json
    assert json.loads(states[0].read_text())['phase'] == 'shutdown'


@pytest.mark.parametrize('probe', ['dump_stacks', 'describe'])
def test_stack_probe_failure_still_terminates_owned_tree(tmp_path, monkeypatch, probe):
    from tools import ci_hang_reporter as reporter
    from unittest.mock import Mock
    root = Mock(pid=123)
    root.children.return_value = []
    child = Mock(pid=123, returncode=1)
    running = [True]
    child.poll.side_effect = lambda: None if running[0] else 1
    root.kill.side_effect = lambda: running.__setitem__(0, False)
    monkeypatch.setattr(reporter.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(reporter.psutil, 'Process', lambda pid: root)
    monkeypatch.setattr(reporter, 'describe', lambda processes: [])
    monkeypatch.setattr(reporter, probe, Mock(side_effect=OSError('probe failed')))
    ticks = iter([0, 2])
    monkeypatch.setattr(reporter.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(OSError, match='probe failed'):
        reporter.supervise(['pytest'], tmp_path, idle_seconds=1)
    root.kill.assert_called_once()


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf', '-inf', 'invalid', ''])
def test_invalid_timeout_is_rejected(monkeypatch, value):
    monkeypatch.setenv('SAMSARA_TEST_HARD_TIMEOUT', value)
    with pytest.raises(pytest.UsageError, match='SAMSARA_TEST_HARD_TIMEOUT'):
        support._hard_timeout_seconds_700()
