"""688: worker isolation contract and bounded, owned ghost-display cleanup."""
import configparser
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest
import yaml

from tests.test_ghost_tap_rest_481 import _close_display, _run_callbacks


ROOT = Path(__file__).resolve().parents[1]


def _steps():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    return workflow['jobs']['test']['steps']


def test_ci_uses_restartable_file_scoped_workers_and_windows_timeout():
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    job = workflow['jobs']['test']
    assert job['strategy'] == {'fail-fast': False, 'matrix': {'shard': [0, 1, 2, 3]}}
    assert job['timeout-minutes'] == 120
    main = next(step for step in job['steps'] if step.get('name') == 'Run tests with coverage')
    assert 'if' not in main and not main.get('continue-on-error', False)
    assert main['timeout-minutes'] == 100
    assert main['env']['SAMSARA_TEST_SHARD_INDEX'] == '${{ matrix.shard }}'
    assert str(main['env']['SAMSARA_TEST_SHARD_TOTAL']) == '4'
    # Every shard inherits conftest's hard watchdog; CI must not disable it.
    for scope in (workflow, job, main):
        assert 'SAMSARA_TEST_HARD_TIMEOUT' not in scope.get('env', {})
    for name in ('Verify embedded version identity', 'Check thread discipline',
                 'Report plugin imports'):
        checks = [step for step in job['steps'] if step.get('name') == name]
        assert len(checks) == 1 and checks[0]['if'] == 'matrix.shard == 0'
        assert not checks[0].get('continue-on-error', False)
    args = shlex.split(main['run'])
    assert args[:9] == ['python', '-u', 'tools/ci_hang_reporter.py',
                        '--state-dir', 'ci-hang-report',
                        '--junit-path', 'junit.xml', '--', 'pytest']
    # Check pytest's own arguments, after the supervisor's separator.
    args = args[9:]
    assert args[args.index('-n') + 1] == '2'
    assert args[args.index('--dist') + 1] == 'loadfile'
    assert int(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--max-worker-restart='))) >= 10
    assert '--timeout=120' in args and '--timeout-method=thread' in args
    ini = configparser.ConfigParser()
    ini.read(ROOT / 'pytest.ini', encoding='utf-8')
    options = args + shlex.split(ini['pytest']['addopts'])
    assert '-x' not in options and '--exitfirst' not in options
    assert not any(arg.startswith('--maxfail') for arg in options)


def test_ci_installs_pinned_worker_and_timeout_plugins():
    requirements = (ROOT / 'requirements-test.txt').read_text(encoding='utf-8').splitlines()
    assert 'pytest-xdist==3.8.0' in requirements
    assert 'pytest-timeout==2.4.0' in requirements
    assert any('-r requirements-test.txt' in step.get('run', '') for step in _steps())


def test_ci_routes_desktop_tests_to_a_bounded_native_step():
    steps = _steps()
    main = next(step for step in steps if step.get('name') == 'Run tests with coverage')
    args = shlex.split(main['run'])
    assert main['env']['QT_QPA_PLATFORM'] == 'offscreen'
    assert args[args.index('-m', 3) + 1] == 'not requires_desktop'

    desktop = steps[steps.index(main) + 1]
    assert desktop['name'] == 'Run native desktop tests'
    assert desktop['shell'] == 'pwsh' and desktop['if'] == 'always() && matrix.shard == 0'
    assert desktop['continue-on-error'] is True
    assert desktop['timeout-minutes'] == 15
    assert 'QT_QPA_PLATFORM' not in desktop.get('env', {})
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    for scope in (workflow, workflow['jobs']['test'], desktop):
        assert 'SAMSARA_TEST_SHARD_INDEX' not in scope.get('env', {})
        assert 'SAMSARA_TEST_SHARD_TOTAL' not in scope.get('env', {})
    # 906: 899 explicitly selects windows so collection cannot restore offscreen.
    unset, platform, command = desktop['run'].strip().splitlines()
    assert unset == 'Remove-Item Env:\\QT_QPA_PLATFORM -ErrorAction SilentlyContinue'
    assert platform.split('#', 1)[0].strip() == "$env:QT_QPA_PLATFORM = 'windows'"
    assert shlex.split(command) == [
        'python', '-m', 'pytest', '-m', 'requires_desktop', '-p', 'no:cacheprovider',
        '--timeout=120', '--timeout-method=thread', '-v',
    ]


def test_ci_preserves_coverage_and_uploads_junit_even_on_failure():
    steps = _steps()
    args = shlex.split(next(step['run'] for step in steps if step.get('name') == 'Run tests with coverage'))
    assert {'--cov=.', '--cov-report=xml', '--cov-report=term-missing', '--junitxml=junit.xml'} <= set(args)
    upload = next(step for step in steps if step.get('name') == 'Upload test reports')
    assert upload['if'] == 'always()'
    assert upload['uses'].startswith('actions/upload-artifact@')
    assert upload['with']['name'] == 'test-reports-${{ matrix.shard }}'
    assert set(upload['with']['path'].splitlines()) == {'junit.xml', 'coverage.xml'}
    codecov = next(step for step in steps if step.get('name') == 'Upload coverage report')
    assert codecov['if'] == 'always()' and codecov['with']['files'] == './coverage.xml'
    assert codecov['with']['name'] == 'test-coverage-${{ matrix.shard }}'
    assert codecov['with']['flags'] == 'shard-${{ matrix.shard }}'


def test_capture_callbacks_drain_followup_work_in_order():
    calls, callbacks = [], []
    def first():
        calls.append('first')
        callbacks.append((calls.append, ('followup',)))
    callbacks.append((first, ()))
    _run_callbacks(callbacks)
    assert calls == ['first', 'followup'] and not callbacks


def test_capture_callbacks_fail_if_they_keep_rescheduling():
    callbacks = []
    def repeat():
        callbacks.append((repeat, ()))
    repeat()
    with pytest.raises(AssertionError, match='did not settle'):
        _run_callbacks(callbacks)


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_ghost_cleanup_stops_activity_and_deletes_only_its_display(
        qapp, qt_deferred_delete, monkeypatch, display):
    from PySide6.QtCore import QAbstractAnimation, QPropertyAnimation, QTimer
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import LiveSurfaceWidget

    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': display == 'Full'}}})
    controller = LiveSurfaceController(app,
        widget_factory=lambda model: LiveSurfaceWidget(model, reduced_motion=True))
    if display == 'Full':
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
        widget = controller.widget
        owner, method = controller, 'close'
    else:
        widget = indicator = ListeningIndicator()
        owner, method = indicator, 'destroy'
    unrelated = QWidget()
    unrelated.deleteLater()
    timer = QTimer(widget)
    timer.start(0)
    animation = QPropertyAnimation(widget, b'geometry', widget)
    animation.setDuration(10000)
    animation.setStartValue(widget.geometry())
    animation.setEndValue(widget.geometry().translated(1, 0))
    animation.start()
    closed = []
    original_close = getattr(owner, method)
    def close():
        assert all(not child.isActive() for child in widget.findChildren(QTimer))
        assert all(child.state() == QAbstractAnimation.State.Stopped
                   for child in widget.findChildren(QAbstractAnimation))
        closed.append(True)
        original_close()
    monkeypatch.setattr(owner, method, close)
    try:
        assert timer.isActive() and animation.state() == QAbstractAnimation.State.Running
        _close_display(controller, indicator, qt_deferred_delete)
        assert closed == [True] and not isValid(widget)
        assert isValid(unrelated)
    finally:
        if isValid(widget):
            for child in widget.findChildren(QTimer):
                child.stop()
            for child in widget.findChildren(QAbstractAnimation):
                child.stop()
            original_close()
            widget.deleteLater()
            qt_deferred_delete(widget)
        if isValid(unrelated):
            qt_deferred_delete(unrelated)
