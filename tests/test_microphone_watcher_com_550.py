"""Real COM regression: run directly, outside the bootstrap gate's subprocess ban.

Only standalone Python children and Windows notification registration; no Samsara
process, PortAudio import, device stream, speech, or owner configuration.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def child(mode):
    import importlib.abc
    import threading
    import time
    from types import ModuleType, SimpleNamespace

    calls = []
    audio = ModuleType('sounddevice')
    def refuse(*args, **kwargs):
        calls.append('sounddevice')
        raise AssertionError('This test must never use audio')
    for name in ('InputStream', 'OutputStream', 'RawInputStream', 'RawOutputStream',
                 'Stream', 'RawStream', 'query_devices', 'query_hostapis', 'play',
                 'rec', 'wait', 'stop', '_initialize', '_terminate'):
        setattr(audio, name, refuse)
    audio.__getattr__ = lambda name: refuse
    sys.modules['sounddevice'] = audio
    class NoBootstrap(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'dictation':
                raise AssertionError('No desktop bootstrap in a COM test')
    sys.meta_path.insert(0, NoBootstrap())
    sys.path.insert(0, str(ROOT))
    from samsara import microphone_recovery as recovery
    assert 'comtypes' not in sys.modules
    if mode == 'main-first':
        import comtypes
        assert comtypes.__file__  # Real installed dependency, never a stand-in.
    before = 'comtypes' in sys.modules
    stop = threading.Event()
    app = SimpleNamespace(_output_watcher_stop=stop, _no_microphone=True,
                          output_device=None)
    # Model a runner with no endpoints. Registration itself uses real pycaw/COM.
    app._microphone_changes = recovery.DeviceChanges(app, endpoints=lambda: frozenset())
    errors, passes = [], []
    real_tick = app._microphone_changes.tick
    def tick():
        passes.append(1)
        real_tick()
    app._microphone_changes.tick = tick
    def run():
        try:
            recovery.watch(app, lambda: None)
        except BaseException as exc:
            errors.append(type(exc).__name__ + ': ' + str(exc))
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    time.sleep(1.2)
    alive_before_stop = worker.is_alive()
    stop.set()
    worker.join(5)
    result = dict(case=mode, com_imported_before_thread=before,
                  real_comtypes=bool(getattr(sys.modules.get('comtypes'), '__file__', None)),
                  alive_before_stop=alive_before_stop, ended_by_stop=not worker.is_alive(),
                  exceptions=errors, passes=len(passes),
                  mode='notifications' if app._microphone_changes.notifications else 'comparing',
                  audio_calls=calls, real_sounddevice_loaded=hasattr(audio, '__file__')
                  and isinstance(getattr(audio, '__file__'), str))
    print(json.dumps(result), flush=True)


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows COM regression')
@pytest.mark.parametrize('mode', ['watcher-first', 'main-first'])
def test_real_comtypes_watcher_stops_only_when_requested(tmp_path, mode):
    env = dict(os.environ)
    for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA',
                'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR'):
        env[key] = str(tmp_path)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--watch-child', mode],
                            cwd=ROOT, env=env, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    print(json.dumps(evidence))
    assert evidence['com_imported_before_thread'] == (mode == 'main-first')
    assert evidence['real_comtypes'] is True
    assert evidence['alive_before_stop'] is True
    assert evidence['ended_by_stop'] is True
    assert evidence['exceptions'] == []
    assert evidence['passes'] >= 1
    assert evidence['mode'] in ('notifications', 'comparing')
    assert evidence['audio_calls'] == []
    assert evidence['real_sounddevice_loaded'] is False


if __name__ == '__main__':
    assert sys.argv[1] == '--watch-child'
    child(sys.argv[2])
