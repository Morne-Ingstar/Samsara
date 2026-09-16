"""Measure bound production hold helpers without importing/starting dictation.

Default backend is the existing fake IPC child (no audio devices). --live
uses real session-volume control, restores in finally, and never opens a mic.
"""
import argparse
import ast
import json
import logging
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from types import MethodType, ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--exclude-pid', type=int, action='append', default=[])
    args = parser.parse_args()
    # Avoid the package's UI/command bootstrap and isolate diagnostic output.
    package = ModuleType('samsara')
    package.__path__ = [str(ROOT / 'samsara')]
    sys.modules['samsara'] = package
    os.environ['SAMSARA_HOME_DIR'] = str(ROOT / '.pytest_cache' / 'hold_duck_latency_home')
    sys.path.insert(0, str(ROOT))
    from samsara import audio_ducking
    from samsara.runtime import thread_registry

    if not args.live:
        def spawn():
            return subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'tests' / '_fake_ducking_child.py'), 'normal'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        audio_ducking._transport = audio_ducking.DuckingHostTransport(spawn_fn=spawn)
    events = []
    ns = dict(audio_ducking=audio_ducking, thread_registry=thread_registry, time=time,
              logger=logging.getLogger('hold_capture_duck_latency'),
              flight_recorder=SimpleNamespace(record=lambda event, **kw: events.append((event, kw))),
              _HANDS_FREE_CAPTURE_DUCK_RESTORE_DELAY_S=0.5)
    names = ('_open_hold_capture_duck', '_close_hold_capture_duck',
             '_open_hands_free_capture_duck', '_close_hands_free_capture_duck',
             '_restore_hands_free_capture_duck_now')
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    methods = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    exec(compile(ast.Module(body=methods, type_ignores=[]), 'dictation.py', 'exec'), ns)
    app = SimpleNamespace(
        config=json.loads((ROOT / 'config.json').read_text(encoding='utf-8')),
        _running=True, _dictation_consumer=None,
        _hold_capture_duck_seq=0, _hold_capture_duck_token=None,
        _hold_capture_duck_confirmed_at=None,
        _hands_free_duck_lock=threading.Lock(), _hands_free_capture_ducker=None,
        _hands_free_capture_duck_owners=set(), _hands_free_capture_duck_owner_seq=0,
        _hands_free_capture_duck_starting=False, _hands_free_capture_duck_start_generation=0,
        _hands_free_duck_restore_timer=None, _hands_free_capture_duck_restore_generation=0,
        _hands_free_capture_duck_restore_token=object(),
        _hands_free_duck_excludes=lambda: set(args.exclude_pid),
        _bump_wake_gate_freeze=lambda: None, _log_duck_result=lambda *a: None,
    )
    for name in names:
        setattr(app, name, MethodType(ns[name], app))
    status, session_counts = 'ok', None
    try:
        for _ in range(21):
            app._open_hold_capture_duck()
            ducker = app._hands_free_capture_ducker
            if ducker is None:
                raise RuntimeError('Capture duck disabled in current config')
            session_counts = dict(seen=ducker.sessions_seen, ducked=ducker.sessions_ducked,
                                  failed=ducker.sessions_failed, last_error=ducker.last_error)
            app._close_hold_capture_duck()
    except Exception as exc:
        status = str(exc)
    finally:
        try:
            app._close_hold_capture_duck(immediate=True)
        finally:
            audio_ducking.shutdown_host()
    samples = [fields for event, fields in events if event == 'hold_capture_duck.engage']
    reused = [s['elapsed_ms'] for s in samples[1:] if s['confirmed']]
    result = dict(
        date='2026-09-10', backend='live WASAPI child' if args.live else 'fake IPC child',
        status=status, factor_key='ducking.hands_free_level',
        factor=app.config.get('ducking', {}).get('hands_free_level', 0.15),
        excluded_pids=args.exclude_pid, sessions=session_counts,
        first_engage_ms=samples[0]['elapsed_ms'] if samples else None,
        reused_median_ms=statistics.median(reused) if reused else None,
        reused_max_ms=max(reused) if reused else None,
        open_owners_after=len(app._hands_free_capture_duck_owners), samples=samples,
        limitation='Measures volume acknowledgement and ownership, not acoustic settling or microphone capture.',
    )
    suffix = 'live' if args.live else 'ipc'
    out = ROOT / 'perf_artifacts' / f'hold_capture_duck_latency_{suffix}.json'
    out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'samples'}, indent=2))
    return 0 if status == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
