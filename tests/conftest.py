"""
Shared fixtures and mocks for Samsara tests.
"""
import pytest
import atexit
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from time import monotonic as _watchdog_monotonic_701
from unittest.mock import Mock, MagicMock, patch

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


_HARD_WATCHDOG_700 = pytest.StashKey()
_PROCESS_WATCHDOG_705 = pytest.StashKey()


def _ci_progress_711(phase, nodeid=''):
    """Progress records, not a liveness pulse: a wedged thread must go stale."""
    directory = os.environ.get('SAMSARA_CI_HEARTBEAT_DIR')
    if not directory:
        return
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f'{os.getpid()}.json'
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(dict(pid=os.getpid(), phase=phase, nodeid=nodeid,
                                       progress=_watchdog_monotonic_701())), encoding='utf-8')
    for attempt in range(50):
        try:
            os.replace(pending, path)
            break
        except PermissionError:
            if attempt == 49:
                raise
            import time
            time.sleep(0.01)


def _ci_dump_listener_711():
    """Cooperative fallback only; the external supervisor owns termination."""
    import faulthandler
    import threading

    directory = os.environ.get('SAMSARA_CI_HEARTBEAT_DIR')
    if not directory:
        return
    root = Path(directory)
    def listen():
        import time
        request = root / f'{os.getpid()}.dump'
        while True:
            if request.exists():
                request.unlink(missing_ok=True)
                with (root / f'faulthandler-{os.getpid()}.txt').open('w') as output:
                    faulthandler.dump_traceback(file=output, all_threads=True)
            time.sleep(0.1)
    threading.Thread(target=listen, name='ci-stack-request', daemon=True).start()


@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname='pytest_runtest_protocol')
def pytest_ci_progress_protocol_711(item):
    _ci_progress_711('test', item.nodeid)
    try:
        yield
    finally:
        _ci_progress_711('between-tests', item.nodeid)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_sessionfinish(session, exitstatus):
    _ci_progress_711('sessionfinish')
    diagnostics = None
    if hasattr(session.config, 'workerinput'):
        # Preserve execnet's real stderr before other finish hooks restore
        # capture streams (fd 2 itself points to NUL on Windows workers).
        diagnostics = os.fdopen(os.dup(sys.stderr.fileno()), 'w', encoding='utf-8',
                                errors='backslashreplace')
    try:
        try:
            outcome = yield
        finally:
            _ci_progress_711('shutdown')
        # This outer wrapper resumes AFTER xdist sends workerfinished.
        # Never truncate its reports or hide an exception from a finish hook.
        if outcome.excinfo is None:
            _worker_exit_guard_714(session.config, exitstatus, stream=diagnostics)
    finally:
        if diagnostics is not None:
            diagnostics.close()


def _worker_exit_guard_714(config, exitstatus, *, timeout=2.0, stream=None):
    """Bound leaked threads only in a worker that has reported its results."""
    if not hasattr(config, 'workerinput'):
        return
    import threading
    import traceback

    stream = sys.stderr if stream is None else stream
    # execnet interrupts the main thread after 5s of gateway shutdown, and
    # xdist kills the process after 10s. Leave time for diagnostics and exit.
    deadline = _watchdog_monotonic_701() + timeout
    registry = sys.modules.get('samsara.runtime.thread_registry')
    if registry is not None:
        def stop_registered():
            try:
                registry.shutdown(timeout=max(0.0, deadline - _watchdog_monotonic_701()))
            except Exception:
                traceback.print_exc(file=stream)
        # Callbacks are application code and can themselves block. They share
        # the join budget, so they must not run on the sessionfinish thread.
        cleanup = threading.Thread(target=stop_registered,
                                   name='ci-worker-cleanup-714', daemon=True)
        cleanup.start()
        cleanup.join(max(0.0, deadline - _watchdog_monotonic_701()))

    def remaining_threads():
        return [thread for thread in threading.enumerate()
                if thread is not threading.current_thread()
                and thread is not threading.main_thread()
                and not thread.daemon and thread.is_alive()]

    for thread in remaining_threads():
        thread.join(max(0.0, deadline - _watchdog_monotonic_701()))
    stragglers = remaining_threads()
    if stragglers:
        print(f'[worker exit guard 714] non-daemon threads remain after {timeout}s; '
              f'exiting with reported status {int(exitstatus)}', file=stream)
    frames = sys._current_frames()
    for thread in stragglers:
        print(f'[worker exit guard 714] {thread.name!r} ident={thread.ident} '
              f'daemon={thread.daemon}', file=stream)
        frame = frames.get(thread.ident)
        if frame is not None:
            traceback.print_stack(frame, file=stream)
    sys.stdout.flush()
    sys.stderr.flush()
    stream.flush()
    # A fully reported worker can otherwise return to execnet's primary-thread
    # wait indefinitely, even with only daemon threads. All sessionfinish hooks
    # (including coverage/result transport) have completed before this point.
    _real_process_exit_701(int(exitstatus))

# Run stdlib-only code in a separate interpreter: neither the worker's GIL,
# faulthandler's single timer, nor a blocked traceback writer can disarm it.
_PROCESS_WATCHDOG_SOURCE_705 = r'''
import json, os, sys, time
from pathlib import Path

pid, heartbeat = int(sys.argv[1]), Path(sys.argv[2])
if sys.platform == 'win32':
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # Hold the process object, so a recycled PID can never target another app.
    handle = kernel.OpenProcess(0x100001, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    alive = lambda: kernel.WaitForSingleObject(handle, 0) == 258
    def kill():
        if not kernel.TerminateProcess(handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())
else:
    import signal
    alive = lambda: os.getppid() == pid
    def kill():
        os.kill(pid, signal.SIGKILL)

heartbeat.with_suffix('.ready').touch()
try:
    while alive():
        try:
            state = json.loads(heartbeat.read_text(encoding='utf-8'))
        except FileNotFoundError:
            break
        except PermissionError:
            # Windows can briefly deny open while os.replace retires the old
            # file. A hung worker stops replacing it, so reads then succeed.
            time.sleep(0.01)
            continue
        if state and time.monotonic() >= state['deadline'] + 1.0:
            # Terminate BEFORE logging: even a full stderr pipe must not stop us.
            kill()
            os.write(2, ('[process watchdog] killed pid=' + str(pid) + ' ' +
                         state['nodeid'] + '\n').encode('utf-8', 'backslashreplace'))
            break
        time.sleep(0.05)
finally:
    if sys.platform == 'win32':
        kernel.CloseHandle(handle)
'''


class _ProcessWatchdog705:
    def __init__(self, stderr_fd):
        import subprocess

        self.directory = Path(tempfile.mkdtemp(prefix='pytest-watchdog-'))
        self.heartbeat = self.directory / 'heartbeat.json'
        self.write(None)
        # Preserve the inherited silent-audio guard in this child as well.
        self.process = subprocess.Popen(
            [sys.executable, '-c', _PROCESS_WATCHDOG_SOURCE_705,
             str(os.getpid()), str(self.heartbeat)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr_fd,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        deadline = _watchdog_monotonic_701() + 10
        while not self.heartbeat.with_suffix('.ready').exists():
            if self.process.poll() is not None or _watchdog_monotonic_701() >= deadline:
                self.close()
                raise pytest.UsageError('independent test watchdog failed to start')
            import time
            time.sleep(0.01)

    def write(self, state):
        import time

        pending = self.heartbeat.with_suffix('.tmp')
        pending.write_text(json.dumps(state), encoding='utf-8')
        for attempt in range(50):
            try:
                os.replace(pending, self.heartbeat)
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.01)

    def arm(self, nodeid, deadline):
        if self.process.poll() is not None:
            raise pytest.UsageError('independent test watchdog exited unexpectedly')
        self.write(dict(nodeid=nodeid, deadline=deadline))

    def close(self):
        import subprocess

        self.heartbeat.unlink(missing_ok=True)
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        shutil.rmtree(self.directory, ignore_errors=True)


def pytest_sessionstart(session):
    _ci_progress_711('collection')
    _ci_dump_listener_711()
    config = session.config
    if _hard_timeout_seconds_700() == 0:
        return
    if getattr(config.option, 'numprocesses', 0) and not hasattr(config, 'workerinput'):
        return  # xdist controller never executes test items.
    try:
        fd = sys.stderr.fileno()
    except (AttributeError, ValueError):
        fd = sys.__stderr__.fileno()
    watchdog = _ProcessWatchdog705(fd)
    config.stash[_PROCESS_WATCHDOG_705] = watchdog
    config.add_cleanup(watchdog.close)


def _hard_timeout_seconds_700():
    import math

    value = os.environ.get('SAMSARA_TEST_HARD_TIMEOUT', '180')
    try:
        seconds = float(value)
    except ValueError:
        seconds = -1
    if not math.isfinite(seconds) or seconds < 0:
        raise pytest.UsageError('SAMSARA_TEST_HARD_TIMEOUT must be finite and >= 0')
    return seconds


def _rearm_hard_watchdog_700(item):
    import faulthandler
    from time import monotonic
    monotonic = _watchdog_monotonic_701

    state = item.stash.get(_HARD_WATCHDOG_700, None)
    if state is not None:
        fd, deadline = state
        faulthandler.dump_traceback_later(
            max(0.001, deadline - monotonic()), file=fd, exit=True)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    """Cover setup, call, reports and teardown, including other hook wrappers."""
    import faulthandler
    from time import monotonic
    monotonic = _watchdog_monotonic_701

    seconds = _hard_timeout_seconds_700()
    if seconds == 0:
        yield
        return
    deadline = monotonic() + seconds
    watchdog = item.config.stash[_PROCESS_WATCHDOG_705]
    watchdog.arm(item.nodeid, deadline)
    # This hook runs outside per-test capture. On Windows execnet preserves
    # the real stderr in sys.stderr but redirects fd 2 (sys.__stderr__) to NUL.
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, ValueError):
        stderr_fd = sys.__stderr__.fileno()
    fd = os.dup(stderr_fd)
    item.stash[_HARD_WATCHDOG_700] = (fd, deadline)
    try:
        os.write(fd, f'[hard watchdog {seconds:g}s] {item.nodeid}\n'.encode(
            'utf-8', errors='backslashreplace'))
        _rearm_hard_watchdog_700(item)
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        del item.stash[_HARD_WATCHDOG_700]
        os.close(fd)
        watchdog.write(None)


@pytest.hookimpl(trylast=True)
def pytest_runtest_call(item):
    _rearm_hard_watchdog_700(item)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item):
    # Pytest's exception diagnostics (and test-owned timers) can cancel the
    # single faulthandler timer. Restore it without extending this test's budget.
    _rearm_hard_watchdog_700(item)


@pytest.hookimpl(optionalhook=True, tryfirst=True)
def pytest_xdist_make_scheduler(config, log):
    if config.option.dist != 'loadfile':
        return None
    from xdist.scheduler.loadfile import LoadFileScheduling

    class HardWatchdogLoadFileScheduling(LoadFileScheduling):
        def _reschedule(self, node):
            super()._reschedule(node)
            # A crash requeues completed file scopes too. Empty assignments
            # cannot satisfy remote.run_one_test's blocking lookahead read.
            while (not node.shutting_down and self.workqueue
                   and self._pending_of(self.assigned_work[node]) < 2):
                super()._reschedule(node)
            if not self.workqueue and not node.shutting_down:
                node.shutdown()

        def remove_node(self, node):
            # xdist 3.8 requeues the entire unfinished file, including the
            # crashed item. Mark that item complete before it reassigns work,
            # but return its id so xdist still reports the crash as a failure.
            for work_unit in self.assigned_work[node].values():
                for nodeid, completed in work_unit.items():
                    if not completed:
                        work_unit[nodeid] = True
                        super().remove_node(node)
                        return nodeid
            return super().remove_node(node)

    return HardWatchdogLoadFileScheduling(config, log)


def _blocked_process_exit_701(code=0):
    # The soft timeout must still kill a wedged worker. Its timer's intentional
    # nonzero exit is separate from application/test code ending the session.
    caller = sys._getframe(1)
    if (caller.f_globals.get('__name__') == 'pytest_timeout'
            and caller.f_code.co_name == 'timeout_timer' and code == 1):
        _real_process_exit_701(code)
    raise RuntimeError('blocked os._exit: tests must not end a pytest worker')


_real_process_exit_701 = os._exit


def _blocked_collection_exit_701(code=None):
    raise RuntimeError('blocked sys.exit during collection: tests must not end a pytest worker')


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_make_collect_report(collector):
    # Main-thread test SystemExit is already a pytest failure. Collection is
    # different: pytest re-raises it and xdist aborts unless we contain it here.
    with pytest.MonkeyPatch.context() as guard:
        guard.setattr(os, '_exit', _blocked_process_exit_701)
        guard.setattr(sys, 'exit', _blocked_collection_exit_701)
        outcome = yield
        if outcome.excinfo is not None and issubclass(outcome.excinfo[0], SystemExit):
            outcome.force_result(pytest.CollectReport(
                collector.nodeid, outcome='failed', result=[],
                longrepr='SystemExit during collection: tests must not end a pytest worker'))


@pytest.hookimpl(hookwrapper=True, tryfirst=True, specname='pytest_runtest_protocol')
def pytest_exit_guard_protocol_701(item):
    with pytest.MonkeyPatch.context() as guard:
        guard.setattr(os, '_exit', _blocked_process_exit_701)
        yield


def pytest_itemcollected(item):
    # Repository-wide UserWarning suppression otherwise hides exits escaping
    # background threads and callbacks. Preserve unrelated warning policies.
    for category in ('PytestUnhandledThreadExceptionWarning', 'PytestUnraisableExceptionWarning'):
        item.add_marker(pytest.mark.filterwarnings(
            f'error:(?s).*(SystemExit|blocked os\\._exit):pytest.{category}'))


@pytest.fixture(autouse=True)
def _speech_presence_unknown_for_decoder_fixtures(request, monkeypatch):
    """Synthetic decoder inputs are not a speech-detector corpus (534)."""
    if request.path.name != 'test_speech_presence_534.py':
        from samsara import speech_presence
        monkeypatch.setattr(speech_presence._presence, 'measure',
                            lambda *args: speech_presence.Presence(False))


# ============================================================================
# Shared fake-app baseline (suite stabilization pass)
# ============================================================================
#
# Dozens of test files build their own lightweight `app` stand-in (a
# SimpleNamespace, a `DictationApp.__new__(DictationApp)`, or a small stub
# class) and bind real dictation.py/wake_consumer.py methods onto it. Every
# time production adds a new cross-cutting attribute those methods read off
# `self` -- a lock, a per-lane token owner, a confidence gate -- every one of
# those ad-hoc fakes goes stale at once. apply_fake_app_defaults() is the
# single place to add the next one.
#
# Call it right after constructing the fake object and BEFORE layering your
# test's own specific attributes on top, e.g.:
#
#     app = DictationApp.__new__(DictationApp)
#     apply_fake_app_defaults(app)
#     app.config = {...}          # your test's own overrides follow
#
# It works on any object that allows attribute assignment (SimpleNamespace,
# a real DictationApp.__new__() instance, or a plain custom class) and never
# overwrites an attribute the object already has, so it's safe to call
# before OR after your own setup.
def apply_fake_app_defaults(app):
    import threading

    from samsara.audio_engine.wake_dispatch import TranscriptionOwners
    from samsara.languages import LanguageConfidenceGate

    defaults = {
        # Per-lane (wake/toggle/ava) exactly-once completion token --
        # dictation.py's _handle_command_mode_utterance/_handle_ava_command_
        # utterance/process_wake_word_buffer all claim/release one.
        '_transcription_owners': TranscriptionOwners,
        # Guards wake-session start/expire/restart bookkeeping --
        # process_wake_word_buffer and the _*_wake_session methods.
        '_wake_session_lock': threading.RLock,
        # Serializes start_recording/stop_recording/cancel_recording against
        # concurrent release/cancel/shutdown.
        '_hold_capture_lifecycle_lock': threading.RLock,
        # Per-app rolling language-confidence expectations used by
        # _filter_dictation_language.
        '_language_confidence_gate': LanguageConfidenceGate,
    }
    for name, factory in defaults.items():
        if not hasattr(app, name):
            setattr(app, name, factory())
    return app

# ============================================================================
# Hermetic collection: force SAMSARA_HOME_DIR before any Samsara import
# ============================================================================
# This MUST be plain module-level code, not a fixture (even autouse) --
# pytest fully imports conftest.py (and every test module's own module-level
# imports, e.g. `import dictation`) during collection, before any fixture
# ever runs. samsara/paths.py's samsara_home_dir() -- and samsara/log.py,
# which binds SAMSARA_LOG_FILE from it at import time -- would otherwise
# read/create the real ~/.samsara on the machine running the suite just from
# `pytest --collect-only`. Force-assign (not setdefault) so a developer's
# real SAMSARA_HOME_DIR, if set in their shell, can't leak into the suite.
_SAMSARA_TEST_HOME = tempfile.mkdtemp(prefix="samsara_test_home_")
os.environ["SAMSARA_HOME_DIR"] = _SAMSARA_TEST_HOME


# BEGIN SILENT AUDIO 446
def _install_silent_audio_446():
    import ctypes
    import importlib.abc
    import importlib.machinery
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    import tempfile
    import threading
    import traceback
    import types

    name = '_samsara_test_audio_446'
    if name in sys.modules:
        return sys.modules[name]
    state = types.ModuleType(name)
    state.calls = []
    state.nodeid = '<collection>'
    state.source = str(Path(__file__).resolve())
    state.real = (os.environ.get('SAMSARA_TEST_AUDIO_ALLOW_REAL') == '1'
                  and os.environ.get('SAMSARA_TEST_REAL_AUDIO') == '1')
    state.originals = {}
    state.lock = threading.RLock()
    sys.modules[name] = state

    def describe(value):
        # Do not persist speech, dictation, raw audio or potentially secret strings.
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if value in ('SystemHand', 'SystemAsterisk', 'SystemExclamation', 'SystemDefault'):
                return value
            return {'type': 'str', 'length': len(value)}
        result = {'type': type(value).__name__}
        if hasattr(value, 'shape'):
            result['shape'] = list(value.shape)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            result['length'] = len(value)
        return result

    def record(call, args=(), kwargs=None, test_id=None):
        frames = traceback.extract_stack(limit=10)[:-2]
        event = {'test': test_id or (state.nodeid if state.nodeid != '<collection>' else
                 os.environ.get('SAMSARA_TEST_AUDIO_NODE', '<collection>')),
                 'pid': os.getpid(), 'call': call,
                 'args': [describe(arg) for arg in args],
                 'kwargs': {key: describe(value) for key, value in (kwargs or {}).items()},
                 'stack': [f'{frame.filename}:{frame.lineno}:{frame.name}' for frame in frames[-5:]],
                 'blocked': True, 'guard': name}
        with state.lock:
            state.calls.append(event)
            path = os.environ.get('SAMSARA_TEST_AUDIO_LOG')
            if path:
                # Separate files avoid interleaving parent/child/thread writes on Windows.
                with open(f'{path}.{os.getpid()}.jsonl', 'a', encoding='utf-8') as handle:
                    handle.write(json.dumps(event) + '\n')
        return event

    state.record = record

    class SilentStream:
        """PortAudio-shaped sink; callbacks run against disposable memory only."""
        def __init__(self, call, *args, **kwargs):
            self._nodeid = record(call, args, kwargs)['test']
            self._call = call
            self.callback = kwargs.get('callback')
            self.samplerate = kwargs.get('samplerate') or 44100
            self.channels = kwargs.get('channels') or 1
            if isinstance(self.channels, (tuple, list)):
                self.channels = self.channels[-1]
            self.blocksize = kwargs.get('blocksize') or 4096
            self.dtype = kwargs.get('dtype', 'float32')
            self.device = kwargs.get('device')
            self.latency = 0
            self.active = False
            self.closed = False
            self._stop = threading.Event()
            self._thread = None

        @property
        def stopped(self):
            return not self.active

        @property
        def write_available(self):
            return self.blocksize

        def start(self):
            record(self._call + '.start')
            self.active = True
            self._stop.clear()
            if self.callback and not self._thread:
                def drain():
                    import numpy as np
                    while not self._stop.wait(self.blocksize / self.samplerate):
                        output = np.zeros((self.blocksize, self.channels), dtype=self.dtype)
                        try:
                            self.callback(output, self.blocksize, None, None)
                            if np.any(output):
                                record(self._call + '.callback', (output,), test_id=self._nodeid)
                        except Exception:
                            self.active = False
                            break
                self._thread = threading.Thread(target=drain, daemon=True)
                self._thread.start()
            return self

        def write(self, data):
            record(self._call + '.write', (data,))
            return False

        def stop(self, *args, **kwargs):
            self.active = False
            self._stop.set()
            thread = self._thread
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
            self._thread = None

        abort = stop

        def close(self, *args, **kwargs):
            self.stop()
            self.closed = True

        def __enter__(self):
            return self.start()

        def __exit__(self, *args):
            self.close()

    state.SilentStream = SilentStream

    def replace(target, attribute, call, stream=False, result=None):
        key = (id(target), attribute)
        if not hasattr(target, attribute):
            return
        original = getattr(target, attribute)
        if getattr(original, '_silent_audio_446', None) is state:
            return
        state.originals[key] = original

        def silent(*args, **kwargs):
            if state.real:
                return original(*args, **kwargs)
            if stream:
                return SilentStream(call, *args, **kwargs)
            record(call, args, kwargs)
            return result

        silent._silent_audio_446 = state
        setattr(target, attribute, silent)

    def patch_module(module):
        if module.__name__ == 'winsound':
            for attribute in ('PlaySound', 'Beep', 'MessageBeep'):
                replace(module, attribute, 'winsound.' + attribute)
        elif module.__name__ == 'sounddevice':
            for attribute in ('play', 'playrec'):
                replace(module, attribute, 'sounddevice.' + attribute)
            for attribute in ('OutputStream', 'RawOutputStream', 'Stream', 'RawStream'):
                replace(module, attribute, 'sounddevice.' + attribute, stream=True)
        elif module.__name__ == 'PySide6.QtWidgets':
            replace(module.QApplication, 'beep', 'QApplication.beep')

    targets = {'winsound', 'sounddevice', 'PySide6.QtWidgets'}

    class Loader:
        def __init__(self, original):
            self.original = original

        def create_module(self, spec):
            return self.original.create_module(spec)

        def exec_module(self, module):
            self.original.exec_module(module)
            patch_module(module)

        def __getattr__(self, attribute):
            return getattr(self.original, attribute)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in targets:
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and spec.loader:
                    spec.loader = Loader(spec.loader)
                return spec
            return None

    sys.meta_path.insert(0, Finder())
    for target in targets:
        module = sys.modules.get(target)
        if module is not None:
            patch_module(module)
    if sys.platform == 'win32':
        replace(ctypes.windll.user32, 'MessageBeep', 'ctypes.windll.user32.MessageBeep', result=1)

    # Python children normally replace PYTHONPATH (436 does). Restore the guard
    # at the spawn boundary, not just in the parent's environment.
    guard_dir = os.environ.get('SAMSARA_TEST_AUDIO_GUARD')
    if not guard_dir:
        guard_dir = tempfile.mkdtemp(prefix='samsara_silent_audio_446_')
        source = Path(__file__).read_text(encoding='utf-8')
        block = source.split('# BEGIN SILENT AUDIO 446\n', 1)[1].split('\n# END SILENT AUDIO 446\n', 1)[0]
        Path(guard_dir, 'sitecustomize.py').write_text(
            block + '\n_install_silent_audio_446()\n', encoding='utf-8')
        os.environ['SAMSARA_TEST_AUDIO_GUARD'] = guard_dir
    state.guard_dir = guard_dir
    os.environ['PYTHONPATH'] = guard_dir + os.pathsep + os.environ.get('PYTHONPATH', '')
    original_popen = subprocess.Popen

    class SilentPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if not state.real:
                supplied_env = kwargs.get('env')
                env = dict(os.environ if supplied_env is None else supplied_env)
                env['SAMSARA_TEST_AUDIO_GUARD'] = guard_dir
                env['SAMSARA_TEST_AUDIO_NODE'] = state.nodeid if state.nodeid != '<collection>' else os.environ.get('SAMSARA_TEST_AUDIO_NODE', '<collection>')
                if os.environ.get('SAMSARA_TEST_AUDIO_LOG'):
                    env['SAMSARA_TEST_AUDIO_LOG'] = os.environ['SAMSARA_TEST_AUDIO_LOG']
                env['PYTHONPATH'] = guard_dir + os.pathsep + env.get('PYTHONPATH', '')
                kwargs['env'] = env
                if isinstance(args, (list, tuple)) and args:
                    executable = Path(str(args[0])).name.lower()
                    if executable.startswith(('python', 'pypy')):
                        flags = list(args[1:])
                        flags = flags[:flags.index('-c')] if '-c' in flags else flags
                        if any(flag in ('-I', '-E', '-S') for flag in flags):
                            raise RuntimeError('silent audio guard requires Python site initialization and PYTHONPATH')
            super().__init__(args, *positional, **kwargs)

    subprocess.Popen = SilentPopen
    return state
# END SILENT AUDIO 446


# Install before test collection can bind output aliases. The one autouse
# fixture below provides the per-test recorder and explicit hardware policy.
_silent_audio_446 = _install_silent_audio_446()


def pytest_configure(config):
    config.addinivalue_line('markers', 'real_audio: requires actual audio hardware; opt in with SAMSARA_TEST_REAL_AUDIO=1')


def pytest_collection_modifyitems(config, items):
    if os.environ.get('SAMSARA_TEST_REAL_AUDIO') != '1':
        skip = pytest.mark.skip(reason='real audio disabled; set SAMSARA_TEST_REAL_AUDIO=1 to opt in')
        for item in items:
            if item.get_closest_marker('real_audio'):
                item.add_marker(skip)

    # CRC32 gave 7527/3496/4701/4134 tests on the real collection. Pack largest
    # files first instead; relative POSIX paths break ties on every CI runner.
    index = os.environ.get('SAMSARA_TEST_SHARD_INDEX')
    total = os.environ.get('SAMSARA_TEST_SHARD_TOTAL')
    if index is None or total is None:
        return
    # Copied hooks in child-pytest probes must not shard their tiny sessions.
    root = Path(__file__).resolve().parents[1]
    if config.rootpath.resolve() != root:
        return
    try:
        index, total = int(index), int(total)
    except ValueError:
        raise pytest.UsageError('test shard index and total must be integers') from None
    if total < 1 or not 0 <= index < total:
        raise pytest.UsageError('test shard requires total > 0 and 0 <= index < total')
    shard_items = [item for item in items if item.path.is_relative_to(root / 'tests')]
    counts = {}
    for item in shard_items:
        path = item.path.relative_to(config.rootpath).as_posix()
        counts[path] = counts.get(path, 0) + 1
    loads, assignments = [0] * total, {}
    for path in sorted(counts, key=lambda path: (-counts[path], path)):
        target = min(range(total), key=lambda shard: loads[shard])
        assignments[path] = target
        loads[target] += counts[path]
    kept, deselected = [], []
    for item in items:
        if not item.path.is_relative_to(root / 'tests'):
            kept.append(item)
            continue
        path = item.path.relative_to(config.rootpath).as_posix()
        (kept if assignments[path] == index else deselected).append(item)
    items[:] = kept
    if deselected:
        config.hook.pytest_deselected(items=deselected)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _silent_audio_446.calls.clear()
    _silent_audio_446.nodeid = item.nodeid


@pytest.fixture(autouse=True)
def silent_audio(request, monkeypatch):
    """Record output requests without devices, including inherited Python children."""
    real = request.node.get_closest_marker('real_audio') is not None
    if real and os.environ.get('SAMSARA_TEST_REAL_AUDIO') != '1':
        pytest.skip('real audio disabled; set SAMSARA_TEST_REAL_AUDIO=1 to opt in')
    _silent_audio_446.real = real
    monkeypatch.setenv('SAMSARA_TEST_AUDIO_NODE', request.node.nodeid)
    if real:
        monkeypatch.setenv('SAMSARA_TEST_AUDIO_ALLOW_REAL', '1')
    else:
        monkeypatch.delenv('SAMSARA_TEST_AUDIO_ALLOW_REAL', raising=False)
    try:
        yield _silent_audio_446
    finally:
        _silent_audio_446.real = False


def _cleanup_samsara_test_home():
    """Close tagged Samsara log handlers before removing the temp home dir --
    Windows refuses to delete a file that still has an open handle, and the
    RotatingFileHandler samsara/log.py's fallback config (or dictation.py's
    own bootstrap) attaches to the root logger keeps samsara.log open for the
    life of the process."""
    import logging
    from samsara.log import SAMSARA_LOG_HANDLER_TAG

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, SAMSARA_LOG_HANDLER_TAG, False):
            handler.close()
            root.removeHandler(handler)

    shutil.rmtree(_SAMSARA_TEST_HOME, ignore_errors=True)


atexit.register(_cleanup_samsara_test_home)


@pytest.fixture(scope="session", autouse=True)
def _shutdown_live_tts_engines():
    """Retire surviving native speech engines without importing/starting TTS."""
    yield
    try:
        module = sys.modules.get('samsara.tts.winrt_engine')
        engines = tuple(getattr(module, '_live_engines', ()))
    except BaseException:
        return
    for engine in engines:
        try:
            engine.shutdown()
        except BaseException:
            pass


@pytest.fixture(autouse=True)
def _isolate_cli_shutdown():
    """A registry shutdown in one test must not close later CLI conversations."""
    module = sys.modules.get('samsara.agent_channel.cli_agents')
    if module is not None:
        module._closing.clear()
    yield
    module = sys.modules.get('samsara.agent_channel.cli_agents')
    if module is not None:
        module._closing.clear()


@pytest.fixture(autouse=True)
def _isolate_plugin_registry(tmp_path, monkeypatch):
    """Keep the real plugins/ directory from leaking into tests.

    Tests that want plugin behavior can register via the @command decorator
    after this fixture clears the global registry.
    """
    from copy import deepcopy
    from samsara import plugin_commands as _plugin_commands
    saved = dict(_plugin_commands._REGISTRY)
    saved_modules = {name: dict(entries)
                     for name, entries in _plugin_commands._MODULE_ENTRIES.items()}
    entries = {id(entry): entry for entry in saved.values()}
    for module_entries in saved_modules.values():
        entries.update((id(entry), entry) for entry in module_entries.values())
    saved_entries = [(entry, deepcopy(entry)) for entry in entries.values()]
    saved_loaded = dict(_plugin_commands._LOADED_MODULES)
    saved_started = set(_plugin_commands._STARTED_SERVICES)
    saved_matcher = _plugin_commands._shared_matcher
    _plugin_commands._REGISTRY.clear()
    # A sentinel app in a dispatch test must not start hooks discovered by an
    # earlier test (including the runtime Claude alias handoff).
    _plugin_commands._LOADED_MODULES.clear()
    _plugin_commands._STARTED_SERVICES.clear()
    _plugin_commands._shared_matcher = None

    original_load = _plugin_commands.load_plugins

    def _scoped_load(plugins_dir):
        plugins_dir = Path(plugins_dir)
        project_plugins = Path(__file__).parent.parent / "plugins" / "commands"
        if plugins_dir.resolve() == project_plugins.resolve():
            return 0
        return original_load(plugins_dir)

    monkeypatch.setattr(_plugin_commands, "load_plugins", _scoped_load)
    try:
        yield
    finally:
        # Restore entry contents as well as mappings: service hooks can replace
        # alias lists or mutate nested metadata on these shared dictionaries.
        for entry, contents in saved_entries:
            entry.clear()
            entry.update(contents)
        _plugin_commands._REGISTRY.clear()
        _plugin_commands._REGISTRY.update(saved)
        _plugin_commands._LOADED_MODULES.clear()
        _plugin_commands._LOADED_MODULES.update(saved_loaded)
        _plugin_commands._STARTED_SERVICES.clear()
        _plugin_commands._STARTED_SERVICES.update(saved_started)
        _plugin_commands._shared_matcher = saved_matcher
        # Discovery reuses imported modules. Keep newly imported shipped
        # declarations, but retire test decorators and temporary plugins from
        # the cache used by raw_phrase_claims() and command reinstallation.
        for name in list(_plugin_commands._MODULE_ENTRIES):
            if name not in saved_modules and not name.startswith('plugins.commands.'):
                del _plugin_commands._MODULE_ENTRIES[name]
        _plugin_commands._MODULE_ENTRIES.update(saved_modules)


# ============================================================================
# Mock Classes
# ============================================================================

class MockWhisperModel:
    """Mock for faster_whisper.WhisperModel"""

    def __init__(self, model_size="base", device="cpu", compute_type="int8"):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type

    def transcribe(self, audio, language="en", beam_size=5, vad_filter=True, initial_prompt=None):
        """Return mock transcription segments"""
        mock_segment = Mock()
        mock_segment.text = "hello world"
        mock_info = Mock()
        mock_info.language = language
        return [mock_segment], mock_info


class MockAudioStream:
    """Mock for sounddevice.InputStream"""

    def __init__(self, **kwargs):
        self.callback = kwargs.get('callback')
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def close(self):
        self.running = False


class MockKeyboardListener:
    """Mock for pynput.keyboard.Listener"""

    def __init__(self, on_press=None, on_release=None):
        self.on_press = on_press
        self.on_release = on_release
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class MockTrayIcon:
    """Mock for pystray.Icon"""

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title
        self.menu = menu
        self.visible = False

    def run(self):
        self.visible = True

    def stop(self):
        self.visible = False


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def sample_commands():
    """Sample commands for testing"""
    return {
        "commands": {
            "open chrome": {
                "type": "launch",
                "target": "chrome.exe",
                "description": "Open Google Chrome"
            },
            "close window": {
                "type": "hotkey",
                "keys": ["alt", "f4"],
                "description": "Close active window"
            },
            "copy": {
                "type": "hotkey",
                "keys": ["ctrl", "c"],
                "description": "Copy selection"
            },
            "period": {
                "type": "text",
                "text": ".",
                "description": "Insert period"
            },
            "new line": {
                "type": "press",
                "key": "enter",
                "description": "Insert new line"
            },
            "double click": {
                "type": "mouse",
                "action": "double_click",
                "description": "Double click mouse"
            },
            "hold shift": {
                "type": "key_down",
                "key": "shift",
                "description": "Hold Shift key"
            },
            "release shift": {
                "type": "key_up",
                "key": "shift",
                "description": "Release Shift key"
            },
            "release all": {
                "type": "release_all",
                "description": "Release all held keys"
            }
        }
    }


@pytest.fixture
def sample_config():
    """Sample configuration for testing"""
    return {
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "cancel_hotkey": "escape",
        "mode": "hold",
        "model_size": "base",
        "language": "en",
        "auto_paste": True,
        "add_trailing_space": True,
        "auto_capitalize": True,
        "format_numbers": True,
        "device": "auto",
        "microphone": None,
        "silence_threshold": 2.0,
        "min_speech_duration": 0.3,
        "command_mode_enabled": True,
        "wake_word": "hey samsara",
        "wake_word_timeout": 5.0,
        "audio_feedback": True,
        "sound_volume": 0.5
    }


@pytest.fixture
def temp_commands_file(tmp_path, sample_commands):
    """Create a temporary commands.json file"""
    commands_file = tmp_path / "commands.json"
    with open(commands_file, 'w') as f:
        json.dump(sample_commands, f)
    return commands_file


@pytest.fixture
def temp_config_file(tmp_path, sample_config):
    """Create a temporary config.json file"""
    config_file = tmp_path / "config.json"
    with open(config_file, 'w') as f:
        json.dump(sample_config, f)
    return config_file


@pytest.fixture
def mock_whisper():
    """Mock the WhisperModel"""
    with patch('faster_whisper.WhisperModel', MockWhisperModel):
        yield MockWhisperModel


@pytest.fixture
def mock_audio():
    """Mock sounddevice"""
    with patch('sounddevice.InputStream', MockAudioStream):
        with patch('sounddevice.query_devices', return_value=[
            {'name': 'Test Microphone', 'max_input_channels': 2, 'index': 0},
            {'name': 'Another Mic', 'max_input_channels': 1, 'index': 1},
        ]):
            yield


@pytest.fixture
def mock_keyboard():
    """Mock pynput keyboard"""
    with patch('pynput.keyboard.Listener', MockKeyboardListener):
        with patch('pynput.keyboard.Controller'):
            yield


@pytest.fixture
def mock_tray():
    """Mock pystray"""
    with patch('pystray.Icon', MockTrayIcon):
        yield


@pytest.fixture
def mock_pyautogui():
    """Mock pyautogui"""
    with patch('pyautogui.hotkey') as mock_hotkey:
        with patch('pyautogui.press') as mock_press:
            with patch('pyautogui.click') as mock_click:
                with patch('pyautogui.doubleClick') as mock_double:
                    with patch('pyautogui.keyDown') as mock_keydown:
                        with patch('pyautogui.keyUp') as mock_keyup:
                            yield {
                                'hotkey': mock_hotkey,
                                'press': mock_press,
                                'click': mock_click,
                                'doubleClick': mock_double,
                                'keyDown': mock_keydown,
                                'keyUp': mock_keyup
                            }


@pytest.fixture
def mock_pyperclip():
    """Mock pyperclip"""
    clipboard = {'content': ''}

    def mock_copy(text):
        clipboard['content'] = text

    def mock_paste():
        return clipboard['content']

    with patch('pyperclip.copy', mock_copy):
        with patch('pyperclip.paste', mock_paste):
            yield clipboard


@pytest.fixture
def mock_subprocess():
    """Mock subprocess for launch commands"""
    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = Mock()
        yield mock_popen


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication for tests that instantiate Qt widgets."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _qt_windows_717():
    # Do not import Qt or create an application for tests that never use it.
    widgets = sys.modules.get('PySide6.QtWidgets')
    if widgets is None:
        return ()
    inventory = getattr(widgets.QApplication, 'topLevelWidgets', None)
    if not callable(inventory):  # Some tests replace the class with a stand-in.
        return ()
    from PySide6.QtCore import QCoreApplication
    if not isinstance(widgets.QApplication.instance(), QCoreApplication):
        return ()
    return tuple(inventory())


def _qt_cached_widgets_722(owners):
    """Find existing widget references without invoking lazy cache lookups."""
    from types import ModuleType
    from PySide6.QtCore import QObject
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid

    pending, seen, widgets = list(owners), set(), []
    while pending:
        owner = pending.pop()
        if isinstance(owner, (str, bytes, int, float, bool, type(None), type, ModuleType)):
            continue
        if id(owner) in seen or callable(owner):
            continue
        seen.add(id(owner))
        if isinstance(owner, QObject):
            if not isValid(owner):
                continue
            if isinstance(owner, QWidget):
                widgets.append(owner)
            pending.extend(QObject.children(owner))
        # Settings' dict/list subclasses materialize pages from values()/iter().
        # Inspect their stored contents only; teardown must never build UI.
        if isinstance(owner, dict):
            pending.extend(dict.keys(owner))
            pending.extend(dict.values(owner))
        elif isinstance(owner, list):
            pending.extend(list.__iter__(owner))
        elif isinstance(owner, (tuple, set, frozenset)):
            pending.extend(owner)
        try:
            attributes = object.__getattribute__(owner, '__dict__')
        except AttributeError:
            continue
        pending.extend(dict.values(attributes))
    return widgets


def _qt_cleanup_windows_717(previous, *, owners=()):
    """Retire this test's windows without delivering another owner's events."""
    if not _qt_windows_717():
        return
    from PySide6.QtCore import QAbstractAnimation, QCoreApplication, QEvent, QTimer
    from PySide6.QtWidgets import QWidget
    from shiboken6 import isValid

    # Wider-scoped fixtures may cache new parentless controls during the test.
    # A Python reference from their live owner is as real as a Qt parent link.
    retained = {id(widget) for widget in _qt_cached_widgets_722((*previous, *owners))}
    def retained_owner(widget):
        while widget is not None:
            if id(widget) in retained:
                return True
            widget = widget.parentWidget()
        return False

    # A close handler may create another window. Bound those follow-up rounds;
    # never pump the event loop (which would dispatch unrelated timer callbacks).
    for _ in range(8):
        windows = [widget for widget in _qt_windows_717()
                   if isValid(widget) and not retained_owner(widget)]
        if not windows:
            return
        # Tool windows also appear in topLevelWidgets(). Retire their owner
        # first: its close/hide handlers may still access those children.
        def depth(widget):
            count = 0
            parent = widget.parentWidget()
            while parent is not None:
                count += 1
                parent = parent.parentWidget()
            return count
        windows.sort(key=depth)
        for widget in windows:
            if not isValid(widget):  # An earlier parent can have deleted it.
                continue
            for activity in (*widget.findChildren(QAbstractAnimation), *widget.findChildren(QTimer)):
                if isValid(activity):
                    activity.stop()
            try:
                widget.close()
            finally:
                if isValid(widget):
                    QWidget.hide(widget)
                    widget.deleteLater()
                    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
    raise RuntimeError('Qt teardown kept creating top-level windows after 8 rounds')


@pytest.fixture(autouse=True)
def _qt_test_windows_717(monkeypatch, request):
    # Wider-scoped fixtures have already been set up. Keep their windows alive;
    # delete function-owned windows before this test's monkeypatch is undone.
    previous = _qt_windows_717()
    try:
        yield
    finally:
        owners = [fixture.cached_result[0] for fixture in request._fixture_defs.values()
                  if fixture.scope != 'function' and fixture.cached_result is not None
                  and fixture.cached_result[2] is None]
        _qt_cleanup_windows_717(previous, owners=owners)


@pytest.fixture(scope="session")
def qt_deferred_delete(qapp):
    """Deliver owned deletes, or pump global deletes within a finite budget."""
    from time import monotonic
    from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop

    def drain(receiver=None):
        # A fixture with an owner must not delete another fixture's objects.
        # Do not dispatch unrelated timers while the owner is being destroyed.
        if receiver is not None:
            QCoreApplication.sendPostedEvents(receiver, QEvent.Type.DeferredDelete)
            return
        deadline = monotonic() + 0.050
        for _ in range(8):
            if monotonic() >= deadline:
                break
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            remaining_ms = int((deadline - monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            qapp.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, min(5, remaining_ms))
    return drain


@pytest.fixture
def qt_widget_scope(qapp, qt_deferred_delete):
    """Destroy an owned widget before patches expire; close may retire its controller."""
    from contextlib import contextmanager
    from PySide6.QtCore import QAbstractAnimation, QTimer
    from shiboken6 import isValid

    @contextmanager
    def owned(widget, *, close=None):
        try:
            yield widget
        finally:
            if isValid(widget):
                for animation in widget.findChildren(QAbstractAnimation):
                    animation.stop()
                for timer in widget.findChildren(QTimer):
                    timer.stop()
                try:
                    (close or widget.close)()
                finally:
                    if isValid(widget):
                        widget.hide()
                        widget.deleteLater()
                        qt_deferred_delete(widget)

    return owned
