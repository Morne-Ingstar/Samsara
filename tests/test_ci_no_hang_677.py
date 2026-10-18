"""677: headless typing refuses promptly; Qt cleanup precedes HTTP-worker GC."""
import gc
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from samsara import agent_server as agents
from samsara.runtime import thread_registry
from tests import test_agent_type_text_565 as typing_tests
from tests.test_agent_type_text_565 import rig  # noqa: F401 -- shared isolated ports


@pytest.mark.parametrize('desktop', [False, True], ids=['no-desktop', 'no-foreground'])
def test_headless_typing_refuses_within_one_second(rig, monkeypatch, desktop):
    import ctypes

    monkeypatch.setattr(agents, '_desktop_allows_typing', lambda: desktop)
    def native_api(*args, **kwargs):
        assert desktop, 'An inaccessible desktop must not reach native window APIs'
        return SimpleNamespace(GetForegroundWindow=lambda: 0,
                               GetWindowThreadProcessId=lambda *a: 0,
                               GetClassNameW=lambda *a: 0)
    monkeypatch.setattr(ctypes, 'WinDLL', native_api)
    rig.service._target_probe = agents._foreground_target
    started = time.monotonic()
    result = typing_tests.call(rig.service)
    assert time.monotonic() - started < 1.0
    assert result == {'status': 'refused',
                      'reason': 'window_changed' if desktop else 'refused_elevated'}
    assert not rig.prompts and not rig.pastes


@pytest.mark.parametrize('exercise', [
    typing_tests.test_switch_ui_persists_and_parent_off_clears_it,
    typing_tests.test_switch_save_failure_restores_state,
    typing_tests.test_approval_style_default_persistence_and_disabled_state,
], ids=['switch', 'save-failure', 'approval-style'])
def test_typing_ui_is_destroyed_before_worker_gc(
        rig, qapp, qt_deferred_delete, monkeypatch, exercise):
    from PySide6.QtCore import Qt
    from shiboken6 import isValid
    from samsara.ui.settings import agents_section as ui

    controls_type = ui.AgentsControls
    controls, destroyed_on = [], []
    main_thread = threading.get_ident()
    def track(*args):
        widget = controls_type(*args)
        controls.append(widget)
        widget.destroyed.connect(lambda: destroyed_on.append(threading.get_ident()),
                                 Qt.ConnectionType.DirectConnection)
        return widget
    monkeypatch.setattr(ui, 'AgentsControls', track)
    monkeypatch.setattr(agents, '_desktop_allows_typing', lambda: False)
    # Hold cyclic wrappers until cleanup, as a busy/coverage-instrumented suite
    # can do. Keeping Python references also proves close() alone is insufficient.
    automatic_gc = gc.isenabled()
    gc.disable()
    try:
        exercise(rig, qapp, qt_deferred_delete)
        assert len(controls) == 1
        assert not isValid(controls[0])
        assert destroyed_on == [main_thread]
        done = threading.Event()
        def collect():
            gc.collect()
            done.set()
        started = time.monotonic()
        worker = thread_registry.spawn('test.headless_gc', collect, daemon=True)
        worker.join(1)
        assert done.is_set() and not worker.is_alive()
        assert time.monotonic() - started < 1.0
        assert destroyed_on == [main_thread]
    finally:
        for widget in controls:
            if isValid(widget):
                widget.deleteLater()
        qt_deferred_delete()
        if automatic_gc:
            gc.enable()


def test_ci_installs_and_enables_windows_thread_timeout():
    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['test']['steps']
    run = next(step['run'] for step in steps if step.get('name') == 'Run tests with coverage')
    assert '--timeout=120' in run.split()
    assert '--timeout-method=thread' in run.split()
    args = run.split()
    assert args[args.index('-n'):args.index('-n') + 2] == ['-n', '2']
    assert '--dist loadfile' in run
    assert '--max-worker-restart=100' in args
    requirements = (root / 'requirements-test.txt').read_text(encoding='utf-8').splitlines()
    assert 'pytest-timeout==2.4.0' in requirements
    assert 'pytest-xdist==3.8.0' in requirements
    assert any('-r requirements-test.txt' in step.get('run', '') for step in steps)
