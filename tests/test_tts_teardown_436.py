"""Native TTS lifetime regressions; failures stay in disposable subprocesses."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

_ROOT = Path(__file__).resolve().parents[1]

_NATIVE = r'''
import atexit
import gc
import io
import time
import wave
import weakref
from pathlib import Path
import sys

case = sys.argv[1]
closed = []
completed = []
# Exercise the ordering where weakref's global exit hook predates PortAudio.
class Sentinel:
    pass
sentinel = Sentinel()
weakref.finalize(sentinel, lambda: None)

def exit_check():
    print('EXIT_CLOSED=' + str(bool(closed)), flush=True)
    print('EXIT_COMPLETED=' + str(bool(completed)), flush=True)

if case.startswith('exit'):
    atexit.register(exit_check)

from samsara.tts.winrt_engine import WinRTEngine

def watch(stream):
    original_close = stream.close
    def tracked_close():
        original_close()
        closed.append(True)
    stream.close = tracked_close

def make_engine():
    engine = WinRTEngine()
    if engine._tts_stream is None:
        print('NATIVE_UNAVAILABLE: no output stream', flush=True)
        raise SystemExit(0)
    assert engine._tts_stream.active
    watch(engine._tts_stream)
    return engine

def collect():
    for _ in range(5):
        gc.collect()
        time.sleep(0.1)  # let native callbacks run after collection

def start_utterance(engine, seconds):
    # Silent, deterministic synthesis; the real stream and worker still run.
    raw = io.BytesIO()
    with wave.open(raw, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b'\0\0' * int(seconds * 44100))
    payload = raw.getvalue()
    engine._synthesize = lambda *args: payload
    handle = engine.speak('teardown check', on_done=lambda: completed.append(True))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine._tts_buffer_lock:
            buffered = bool(engine._tts_buffer)
        if handle._state == 'playing' and buffered:
            return handle
        time.sleep(0.005)
    raise AssertionError('utterance never reached native playback')

if case in ('two_forward', 'two_reverse'):
    engines = [make_engine(), make_engine()]
    refs = [weakref.ref(engine) for engine in engines]
    for count, index in enumerate((0, 1) if case == 'two_forward' else (1, 0), 1):
        engines[index] = None
        collect()
        assert refs[index]() is None
        assert len(closed) == count
elif case == 'idle':
    engine = make_engine()
    reference = weakref.ref(engine)
    del engine
    collect()
    assert reference() is None
    assert len(closed) == 1
elif case == 'speaking':
    engine = make_engine()
    handle = start_utterance(engine, 0.4)
    reference = weakref.ref(engine)
    del engine  # worker retains its owner until playback finishes
    deadline = time.monotonic() + 5
    while handle._state != 'done' and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle._state == 'done'
    collect()
    assert reference() is None
    assert len(closed) == 1
    assert completed == [True]
elif case == 'shutdown_twice':
    engine = make_engine()
    stream = engine._tts_stream
    engine.shutdown()
    engine.shutdown()
    assert stream.closed
    assert len(closed) == 1
    del engine, stream
    collect()
    assert len(closed) == 1
elif case in ('exit_alive', 'exit_speaking'):
    engine = make_engine()  # keep alive: interpreter-exit finalizer must close it
    if case == 'exit_speaking':
        start_utterance(engine, 2.0)
else:
    raise AssertionError(case)
print('CASE_OK=' + case, flush=True)
'''

_FAKE = r'''
import gc
import sys
import weakref
from unittest.mock import patch
import numpy as np
import sounddevice as sd
from samsara.tts import winrt_engine as module
from samsara.tts.engine_base import SpeechHandle

case = sys.argv[1]
streams = []
class Stream:
    def __init__(self, **kwargs):
        self.callback = kwargs['callback']
        self.aborts = self.closes = 0
        self.closed = False
        streams.append(self)
    def start(self):
        if case == 'start_failure' and len(streams) == 1:
            raise RuntimeError('start failed after opening handle')
    def abort(self):
        self.aborts += 1
        if case == 'abort_failure':
            raise RuntimeError('abort failed')
    def close(self):
        self.closes += 1
        self.closed = True

with patch.object(module, '_import_winsdk', return_value=(object, object, None, None)), \
     patch.object(module, 'get_helper', return_value=object()), \
     patch.object(module.WinRTEngine, '_build_voice_list', return_value=[]), \
     patch.object(sd, 'OutputStream', Stream), \
     patch.object(module, 'output_sample_rate', return_value=44100):
    engine = module.WinRTEngine(output_device=7 if case == 'start_failure' else None)
    if case == 'start_failure':
        assert len(streams) == 2
        assert streams[0].closed and streams[0].closes == 1
        assert not streams[1].closed
    elif case in ('cancel', 'cancel_worker'):
        handle = SpeechHandle(utterance_id='cancelled')
        utterance = module._Utterance(handle, None)
        engine._active[handle.utterance_id] = utterance
        buffer = engine._tts_buffer
        buffer.extend([(np.array([1, 2], dtype=np.float32), 'cancelled'),
                       (np.array([3, 4], dtype=np.float32), 'retained')])
        if case == 'cancel':
            engine.cancel(handle)
        else:
            utterance.cancelled.set()
            engine._push_chunks(np.zeros(2, dtype=np.float32), utterance, 'cancelled')
        assert engine._tts_buffer is buffer
        output = np.empty((4, 1), dtype=np.float32)
        streams[-1].callback(output, 4, None, None)
        np.testing.assert_array_equal(output[:, 0], [3, 4, 0, 0])
    elif case == 'restart':
        engine.restart_stream()
        assert len(streams) == 2
        assert streams[0].closed and not streams[1].closed
    elif case != 'abort_failure':
        raise AssertionError(case)
    reference = weakref.ref(engine)
    if case in ('abort_failure', 'start_failure'):
        engine.shutdown()
        engine.shutdown()
    del engine
    gc.collect()
    # Retaining callback-bearing stream handles must not retain their engine.
    assert reference() is None
    assert all(stream.closed and stream.closes == 1 for stream in streams)
    assert not module._live_engines
print('CASE_OK=' + case, flush=True)
'''

_FIXTURE = r'''
import runpy
import sys
from pathlib import Path
from types import ModuleType
import weakref
namespace = runpy.run_path(str(Path.cwd() / 'tests' / 'conftest.py'))
fixture = namespace['_shutdown_live_tts_engines'].__wrapped__
name = 'samsara.tts.winrt_engine'
assert name not in sys.modules
empty = fixture()
next(empty)
try:
    next(empty)
except StopIteration:
    pass
assert name not in sys.modules  # empty suite must not import/construct TTS
calls = []
class Engine:
    def __init__(self, label, fails=False):
        self.label, self.fails = label, fails
    def shutdown(self):
        calls.append(self.label)
        if self.fails:
            raise RuntimeError('cleanup failure')
module = ModuleType(name)
module._live_engines = weakref.WeakSet()
good, bad = Engine('good'), Engine('bad', True)
module._live_engines.update((good, bad))
sys.modules[name] = module
safety = fixture()
next(safety)
try:
    next(safety)
except StopIteration:
    pass
assert sorted(calls) == ['bad', 'good']
print('CASE_OK=safety_fixture', flush=True)
'''


def _run_child(tmp_path, script, case):
    home = tmp_path / 'home'
    home.mkdir()
    env = os.environ.copy()
    for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA',
                'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR'):
        env[key] = str(home)
    env['PYTHONPATH'] = str(_ROOT)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script, case],
                            cwd=_ROOT, env=env, capture_output=True, text=True,
                            errors='replace', timeout=20)
    output = result.stdout + result.stderr
    assert result.returncode == 0, f'child exit={result.returncode}\n{output}'
    if 'NATIVE_UNAVAILABLE:' in output:
        pytest.skip(output.strip())
    assert 'CASE_OK=' + case in output, output
    return output


@pytest.mark.real_audio
@pytest.mark.skipif(sys.platform != 'win32', reason='WinRT native lifetime requires Windows')
@pytest.mark.parametrize('case', ['idle', 'speaking', 'two_forward', 'two_reverse',
                                 'shutdown_twice', 'exit_alive', 'exit_speaking'])
def test_native_teardown_in_subprocess(tmp_path, case):
    output = _run_child(tmp_path, _NATIVE, case)
    if case.startswith('exit'):
        assert 'EXIT_CLOSED=True' in output, output
        assert 'EXIT_COMPLETED=False' in output, output


@pytest.mark.parametrize('case', ['start_failure', 'abort_failure', 'cancel',
                                 'cancel_worker', 'restart'])
def test_stream_failure_and_callback_ownership_in_subprocess(tmp_path, case):
    _run_child(tmp_path, _FAKE, case)


def test_session_safety_net_in_subprocess(tmp_path):
    _run_child(tmp_path, _FIXTURE, 'safety_fixture')
