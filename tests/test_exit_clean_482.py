"""Final-exit regression tests; never import dictation or create Qt objects."""

import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_helper():
    spec = importlib.util.spec_from_file_location("exit_helper_482", ROOT / "samsara/process_exit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Exited(BaseException):
    pass


@pytest.fixture
def harness(monkeypatch):
    module = load_helper()
    events = []
    timers = []

    class Timer:
        def __init__(self, interval, callback, args):
            self.interval, self.callback, self.args = interval, callback, args
            self.daemon = False
            timers.append(self)

        def start(self):
            assert self.daemon
            events.append("watchdog")

    def fallback(code):
        events.append(("fallback", code))
        raise Exited

    def function(name, result):
        def call(*args):
            events.append((name, *args))
            return result
        return call

    kernel = SimpleNamespace(GetCurrentProcess=function("handle", -1),
                             TerminateProcess=function("terminate", 0))
    monkeypatch.setattr(module, "threading", SimpleNamespace(Timer=Timer))
    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32",
        stdout=SimpleNamespace(flush=lambda: events.append("stdout")),
        stderr=SimpleNamespace(flush=lambda: events.append("stderr"))))
    monkeypatch.setattr(module, "logging", SimpleNamespace(shutdown=lambda: events.append("logging")))
    monkeypatch.setattr(module, "os", SimpleNamespace(_exit=fallback))
    monkeypatch.setattr(module.ctypes, "WinDLL", lambda *a, **k: kernel, raising=False)
    return module, events, timers, kernel


def test_flush_order_and_returning_terminate_fallback(harness):
    module, events, timers, kernel = harness
    with pytest.raises(Exited):
        module.hard_exit(7)
    assert events == ["watchdog", "logging", "stdout", "stderr", ("handle",),
                      ("terminate", -1, 7), ("fallback", 7)]
    assert kernel.GetCurrentProcess.restype is module.ctypes.c_void_p
    assert kernel.TerminateProcess.argtypes == (module.ctypes.c_void_p, module.ctypes.c_uint)


def test_non_windows_and_absent_streams(harness):
    module, events, _, _ = harness
    module.sys.platform = "linux"
    module.sys.stdout = module.sys.stderr = None
    with pytest.raises(Exited):
        module.hard_exit()
    assert events == ["watchdog", "logging", ("fallback", 0)]


def test_failed_flushes_do_not_prevent_termination(harness):
    module, events, _, _ = harness

    def broken():
        raise OSError("closed")

    module.logging.shutdown = broken
    module.sys.stdout.flush = broken
    with pytest.raises(Exited):
        module.hard_exit()
    assert "stderr" in events
    assert events[-1] == ("fallback", 0)


def test_native_api_error_still_falls_back(harness):
    module, events, _, kernel = harness

    def broken(*args):
        raise OSError("unavailable")

    kernel.TerminateProcess = broken
    with pytest.raises(Exited):
        module.hard_exit(4)
    assert events[-1] == ("fallback", 4)


def test_watchdog_terminates_without_reentering_flush(harness):
    module, events, timers, _ = harness
    with pytest.raises(Exited):
        module.hard_exit(3)
    events.clear()
    assert timers[0].interval == 3.0
    with pytest.raises(Exited):
        timers[0].callback(*timers[0].args)
    assert events == [("handle",), ("terminate", -1, 3), ("fallback", 3)]


def test_quit_tail_cleans_host_and_always_hard_exits():
    tree = ast.parse((ROOT / "dictation.py").read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "quit_app")
    tail = ast.Module(body=method.body[-4:], type_ignores=[])
    events = []

    def call(name):
        return lambda *args: events.append(name)

    def fail_host():
        events.append("host")
        raise OSError("host cleanup failure")

    fake = SimpleNamespace(hard_exit=call("exit"))
    # Remove just the local import: execute the actual tail with a fake helper.
    tail.body = [node for node in tail.body if not isinstance(node, ast.ImportFrom)]
    for host in (call("host"), fail_host):
        events.clear()
        scope = dict(logger=SimpleNamespace(info=call("goodbye"), debug=call("debug")),
                     _samsara_boot=SimpleNamespace(end_session=call("marker")), LOG_DIR="logs",
                     audio_ducking=SimpleNamespace(shutdown_host=host), process_exit=fake,
                     thread_registry=SimpleNamespace(shutdown=call("threads")),
                     os=os,
                     exit_reason="user_quit",
                     finish_exit=lambda *args, **kwargs: call("exit")())
        try:
            exec(compile(tail, "quit-tail", "exec"), scope)
        except OSError:
            pass
        assert events == ["threads", "goodbye", "marker", "host", "exit"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows mutex lifetime")
@pytest.mark.skipif("gate_no_bootstrap" in sys.modules, reason="Gate forbids child processes; checked by isolated process tool")
def test_hard_exit_releases_isolated_mutex_and_leaves_clean_marker(tmp_path):
    script = '''
import importlib.util, logging, pathlib, sys
root, home = map(pathlib.Path, sys.argv[1:])
def load(name):
    spec = importlib.util.spec_from_file_location(name, root / 'samsara' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
boot, instance, exit = (load(name) for name in ('boot', 'single_instance', 'process_exit'))
lock = instance.acquire_single_instance_mutex(home)
assert boot.begin_session(home) is None
logging.basicConfig(filename=home / 'exit.log', level=logging.INFO)
logging.info('persisted before hard exit')
boot.end_session(home)
print('CLEAN')
exit.hard_exit()
'''
    for _ in range(2):
        result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT), str(tmp_path)],
                                capture_output=True, text=True, timeout=8)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "CLEAN"
        assert not (tmp_path / "session.running").exists()
    assert (tmp_path / "exit.log").read_text().count("persisted before hard exit") == 2


@pytest.mark.skipif("gate_no_bootstrap" in sys.modules, reason="Gate forbids child processes; checked by isolated process tool")
def test_watchdog_bounds_real_blocked_logging_flush():
    script = '''
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location('exit482', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.logging.shutdown = lambda: time.sleep(120)
module.hard_exit(7)
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "samsara/process_exit.py")],
                            capture_output=True, timeout=8)
    assert result.returncode == 7


@pytest.mark.skipif("gate_no_bootstrap" in sys.modules, reason="Gate forbids child processes; checked by isolated process tool")
def test_watchdog_survives_registry_shutdown_during_blocked_flush():
    script = '''
import sys, time
from samsara import process_exit
from samsara.runtime import thread_registry
def blocked_flush():
    thread_registry.shutdown(timeout=0)
    time.sleep(120)
process_exit.logging.shutdown = blocked_flush
process_exit.hard_exit(7)
'''
    result = subprocess.run([sys.executable, "-B", "-c", script],
                            cwd=ROOT, capture_output=True, timeout=8)
    assert result.returncode == 7
