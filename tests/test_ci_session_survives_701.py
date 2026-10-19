"""701: clock fakes and accidental exits fail a test without aborting xdist."""
import pytest

from tests.test_ci_hard_watchdog_700 import run_child


def child_session(tmp_path, source, *, collecting=False):
    # loadfile keeps the failing test and its successors in one worker, while
    # this second file exercises an independent worker in the same session.
    neighbour = tmp_path / 'test_neighbour.py'
    neighbour.write_text('def test_neighbour_survives():\n    assert True\n', encoding='utf-8')
    extra = ['-p', 'xdist.plugin', '-n', '2', '--dist', 'loadfile', str(neighbour)]
    if collecting:
        extra.append('--continue-on-collection-errors')
    result, _ = run_child(tmp_path, source, hard='15', soft='10', extra=extra)
    output = result.stdout + result.stderr
    assert 'INTERNALERROR' not in output, output
    assert 'node down' not in output, output
    assert result.returncode == 1, output
    return output


def test_exhausted_shared_clock_fails_and_next_test_runs(tmp_path):
    output = child_session(tmp_path, '''
import time

def test_exhaust_clock(monkeypatch):
    ticks = iter([10.0])
    monkeypatch.setattr(time, 'monotonic', lambda: next(ticks))
    time.monotonic()
    time.monotonic()  # The original hold-capture fixture's failure path.

def test_after_clock_failure():
    assert time.monotonic() > 10.0
''')
    assert 'StopIteration' in output and 'test_exhaust_clock' in output
    assert '1 failed, 2 passed' in output


@pytest.mark.parametrize('exit_path', [
    'sys.exit(0)',
    'raise SystemExit(0)',
    'os._exit(0)',
    'thread = threading.Thread(target=lambda: sys.exit(0)); thread.start(); thread.join()',
    'thread = threading.Thread(target=lambda: os._exit(0)); thread.start(); thread.join()',
])
def test_accidental_exit_fails_and_next_test_runs(tmp_path, exit_path):
    output = child_session(tmp_path, f'''
import os
import sys
import threading

def test_accidental_exit():
    {exit_path}

def test_after_exit():
    assert True
''')
    assert 'test_accidental_exit' in output
    assert '1 failed, 2 passed' in output


@pytest.mark.parametrize('exit_path', ['sys.exit(0)', 'os._exit(0)', 'raise SystemExit(0)'])
def test_collection_exit_is_reported_without_internalerror(tmp_path, exit_path):
    output = child_session(tmp_path, f'''
import os
import sys
{exit_path}
''', collecting=True)
    # xdist may retain a separate traceback from each collecting worker.
    assert 'ERROR collecting test_probe.py' in output
    assert '1 passed,' in output and 'error' in output
    assert 'tests must not end a pytest worker' in output
