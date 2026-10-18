"""Device-boundary and inherited-child regressions for the suite's audio guard."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_all_output_boundaries_record_without_devices(silent_audio, request):
    import ctypes
    import sounddevice as sd
    import winsound
    from PySide6.QtWidgets import QApplication

    reference = Path(__file__).with_name('conftest.py').read_text(encoding='utf-8')
    block = reference.split('# BEGIN SILENT AUDIO 446\n', 1)[1].split('\n# END SILENT AUDIO 446\n', 1)[0]
    # xdist workers inherit the exact guard via sitecustomize, before conftest.
    assert block in Path(silent_audio.source).read_text(encoding='utf-8')
    assert silent_audio.nodeid == request.node.nodeid

    winsound.PlaySound('SystemHand', winsound.SND_ALIAS)
    winsound.Beep(440, 1)
    winsound.MessageBeep()
    if sys.platform == 'win32':
        assert ctypes.windll.user32.MessageBeep(0) == 1
    QApplication.beep()
    sd.play([0.5], 44100)
    for name in ('OutputStream', 'RawOutputStream', 'Stream', 'RawStream'):
        stream = getattr(sd, name)(samplerate=44100, channels=1)
        assert isinstance(stream, silent_audio.SilentStream)
        stream.start()
        assert stream.active
        assert stream.write(b'\0\0') is False
        stream.close()
        assert stream.closed and not stream.active
    calls = {event['call'] for event in silent_audio.calls}
    assert {'winsound.PlaySound', 'winsound.Beep', 'winsound.MessageBeep',
            'QApplication.beep', 'sounddevice.play'} <= calls
    if sys.platform == 'win32':
        assert 'ctypes.windll.user32.MessageBeep' in calls
    for name in ('OutputStream', 'RawOutputStream', 'Stream', 'RawStream'):
        assert {'sounddevice.' + name, 'sounddevice.' + name + '.start',
                'sounddevice.' + name + '.write'} <= calls
    assert all(event['blocked'] for event in silent_audio.calls)


def test_child_uses_guard_when_it_replaces_pythonpath(tmp_path, silent_audio):
    # Same executable/-X/-c/env-copy/overwritten-PYTHONPATH pattern as 436.
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    script = '''
import json, sys, winsound, sounddevice as sd
from PySide6.QtWidgets import QApplication
guard = sys.modules['_samsara_test_audio_446']
winsound.Beep(440, 1)
QApplication.beep()
stream = sd.OutputStream(samplerate=44100, channels=1)
stream.start()
stream.write(b'\\0\\0')
stream.close()
print(json.dumps([event['call'] for event in guard.calls]))
'''
    result = subprocess.run([sys.executable, '-X', 'faulthandler', '-c', script],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    calls = json.loads(result.stdout.strip())
    assert {'winsound.Beep', 'QApplication.beep', 'sounddevice.OutputStream',
            'sounddevice.OutputStream.start', 'sounddevice.OutputStream.write'} <= set(calls)


def test_installation_never_calls_original_output_functions(tmp_path):
    script = r'''
import ctypes, json, runpy, sys, types
from pathlib import Path
source = Path(sys.argv[1]).read_text(encoding='utf-8')
block = source.split('# BEGIN SILENT AUDIO 446\n', 1)[1].split('\n# END SILENT AUDIO 446\n', 1)[0]
def forbidden(*args, **kwargs):
    raise AssertionError('original device entry point reached')
for name, functions in [('winsound', ['Beep', 'PlaySound', 'MessageBeep']),
                        ('sounddevice', ['play', 'playrec', 'OutputStream', 'RawOutputStream', 'Stream', 'RawStream'])]:
    module = types.ModuleType(name)
    for function in functions:
        setattr(module, function, forbidden)
    sys.modules[name] = module
qt = types.ModuleType('PySide6.QtWidgets')
qt.QApplication = type('QApplication', (), {'beep': staticmethod(forbidden)})
sys.modules[qt.__name__] = qt
if sys.platform == 'win32':
    ctypes.windll.user32 = types.SimpleNamespace(MessageBeep=forbidden)
del sys.modules['_samsara_test_audio_446']
namespace = {'__file__': sys.argv[1]}
exec(block, namespace)
guard = namespace['_install_silent_audio_446']()
import winsound, sounddevice as sd
winsound.Beep(440, 1)
winsound.PlaySound('SystemHand', 0)
winsound.MessageBeep()
qt.QApplication.beep()
if sys.platform == 'win32':
    ctypes.windll.user32.MessageBeep(0)
sd.play([1], 44100)
sd.playrec([1], 44100)
for name in ('OutputStream', 'RawOutputStream', 'Stream', 'RawStream'):
    stream = getattr(sd, name)()
    stream.start()
    stream.write(b'\0\0')
    stream.close()
assert all(event['blocked'] for event in guard.calls)
print('ORIGINALS_UNREACHED')
'''
    result = subprocess.run([sys.executable, '-c', script, str(Path(__file__).with_name('conftest.py'))],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ORIGINALS_UNREACHED' in result.stdout


@pytest.mark.parametrize('enabled', [False, True])
def test_real_audio_marker_requires_explicit_opt_in(tmp_path, enabled):
    # Exercise real pytest marker evaluation without a second pytest run.
    # The opt-in case only asserts policy state; it never requests sound.
    script = r'''
import runpy, sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from _pytest.skipping import evaluate_skip_marks
namespace = runpy.run_path(sys.argv[1])
enabled = sys.argv[2] == 'True'
config = pytest.Config.fromdictargs({}, ['-p', 'no:cacheprovider'])
namespace['pytest_configure'](config)
session = pytest.Session.from_config(config)
config.hook.pytest_sessionstart(session=session)
Path('test_policy.py').write_text('def test_policy(): pass\n', encoding='utf-8')
module = pytest.Module.from_parent(session, path=Path.cwd() / 'test_policy.py')
item = pytest.Function.from_parent(module, name='test_policy', callobj=lambda: None)
item.add_marker('real_audio')
namespace['pytest_collection_modifyitems'](config, [item])
assert (evaluate_skip_marks(item) is None) is enabled
patch = pytest.MonkeyPatch()
fixture = namespace['silent_audio'].__wrapped__(SimpleNamespace(node=item), patch)
if enabled:
    guard = next(fixture)
    assert guard.real
    try:
        next(fixture)
    except StopIteration:
        pass
    assert not guard.real
else:
    try:
        next(fixture)
    except pytest.skip.Exception:
        pass
    else:
        raise AssertionError('unapproved hardware fixture was not skipped')
patch.undo()
print('POLICY_OK')
'''
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get('PYTHONPATH', '')
    if enabled:
        env['SAMSARA_TEST_REAL_AUDIO'] = '1'
    else:
        env.pop('SAMSARA_TEST_REAL_AUDIO', None)
    result = subprocess.run([sys.executable, '-c', script,
                             str(Path(__file__).with_name('conftest.py')), str(enabled)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'POLICY_OK' in result.stdout


def test_python_child_cannot_bypass_guard(silent_audio):
    for flag in ('-I', '-E', '-S'):
        with pytest.raises(RuntimeError, match='silent audio guard requires'):
            subprocess.run([sys.executable, flag, '-c', 'pass'], check=True)


def test_callback_audio_is_drained_into_memory_only(silent_audio):
    import threading
    import sounddevice as sd

    called = threading.Event()

    def callback(output, frames, time_info, status):
        output.fill(0.5)
        called.set()

    stream = sd.OutputStream(samplerate=8000, channels=1, blocksize=64, callback=callback)
    assert isinstance(stream, silent_audio.SilentStream)
    stream.start()
    assert called.wait(2)
    stream.close()
    assert any(event['call'] == 'sounddevice.OutputStream.callback' for event in silent_audio.calls)


def test_winrt_and_edge_playback_use_recorded_streams(silent_audio):
    # Test the production playback entries without synthesis/native devices.
    import collections
    import threading
    import numpy as np
    from samsara.tts.winrt_engine import WinRTEngine, _Utterance
    from samsara.tts.edge_tts_engine import EdgeTTSEngine
    from samsara.tts.engine_base import SpeechHandle

    winrt = object.__new__(WinRTEngine)
    winrt._output_device = None
    winrt._output_device_resolver = None
    winrt._output_sample_rate = 44100
    winrt._stream_finalizer = None
    winrt._tts_stream = None
    winrt._tts_buffer = collections.deque()
    winrt._tts_buffer_lock = threading.Lock()
    winrt._tts_callback_progress = [None]
    winrt._open_persistent_stream()
    assert isinstance(winrt._tts_stream, silent_audio.SilentStream)
    winrt._close_persistent_stream()
    winrt._stream_pcm_ephemeral(np.zeros(8, dtype=np.float32), _Utterance(SpeechHandle(utterance_id='silent'), None))
    edge = object.__new__(EdgeTTSEngine)
    edge._output_device = None
    edge._output_device_resolver = None
    edge._lock = threading.Lock()
    edge._cancelled = False
    edge._play_pcm(np.zeros(8, dtype=np.int16).tobytes(), sample_rate=44100)
    assert sum(event['call'] == 'sounddevice.OutputStream.write' for event in silent_audio.calls) == 2
