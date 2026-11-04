"""Held-final benchmark regression: real delivery method, synthetic PCM only."""
import ast
from pathlib import Path
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock
import wave

import numpy as np
import pytest

from samsara import benchmark_store as store, tutorial_capture, decode_recovery
from samsara.runtime import thread_registry

_REAL_SPAWN = thread_registry.spawn


@pytest.fixture
def capture(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    app = NS(config={'benchmark': {'collect_samples': True, 'max_samples': 200},
                     'model_size': 'base', 'auto_paste': False}, model_rate=16000)
    session = NS(app=app, cancel_event=threading.Event(), _state_lock=threading.Lock(),
                 _decode_audio=np.linspace(-.2, .2, 1600, dtype=np.float32),
                 _parking_enabled=False, _direct_paste=False, _overlay=Mock(), _mark_done=Mock(),
                 _hold_final_audit=True, STATE_PASTING='pasting')
    jobs = []
    monkeypatch.setattr(thread_registry, 'spawn', lambda name, fn, **kw: jobs.append(fn))
    monkeypatch.setattr(tutorial_capture, 'final_current', lambda *a, **kw: True)
    monkeypatch.setattr(tutorial_capture, 'record_final', lambda *a, **kw: True)
    monkeypatch.setattr(decode_recovery, 'deliver_failure', lambda *a: False)
    # Execute the actual production method without bootstrapping dictation/Qt.
    source = Path(__file__).resolve().parents[1] / 'samsara' / 'streaming.py'
    cls = next(n for n in ast.parse(source.read_text(encoding='utf-8')).body
               if isinstance(n, ast.ClassDef) and n.name == 'StreamingSession')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_deliver_final')
    namespace = {'logger': Mock(), 'StreamingOverlayQt': NS(STATE_DONE='done'),
                 '_LiveSurfaceStreamingOverlay': type('Overlay', (), {}),
                 'MIN_PARTIAL_AUDIO_S': .5}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), 'exec'), namespace)
    def deliver(text='Final corrected text.', raw='raw decoder text'):
        namespace['_deliver_final'](session, text, raw, .1, 10)
    return session, jobs, deliver


def test_accepted_final_saves_once_with_exact_decoder_pcm(capture):
    session, jobs, deliver = capture
    expected = store._to_int16_pcm(session._decode_audio).tobytes()
    deliver()
    deliver()
    assert len(jobs) == 1
    assert store.list_samples() == []  # delivery does no disk I/O
    session._decode_audio[:] = 0
    jobs[0]()
    row, = store.list_samples()
    assert (row['raw_transcript'], row['final_text'], row['model']) == (
        'raw decoder text', 'Final corrected text.', 'base')
    with wave.open(str(store.audio_path(row)), 'rb') as wav:
        assert wav.getframerate() == 16000
        assert wav.readframes(wav.getnframes()) == expected
    assert session._overlay.flash_done_and_fade.call_count == 2


def test_disabled_does_not_queue_or_create_files(capture):
    session, jobs, deliver = capture
    session.app.config['benchmark']['collect_samples'] = False
    deliver()
    assert not jobs and not store._benchmark_dir().exists()
    session._overlay.flash_done_and_fade.assert_called_once()


def test_disabled_after_queue_does_not_write(capture):
    session, jobs, deliver = capture
    deliver()
    session.app.config['benchmark']['collect_samples'] = False
    jobs[0]()
    assert not store._benchmark_dir().exists()


@pytest.mark.parametrize('failure', ['spawn', 'disk', 'unexpected_writer'])
def test_collection_failure_never_breaks_delivery(capture, monkeypatch, failure):
    session, jobs, deliver = capture
    def fail(*a, **kw):
        raise OSError('synthetic write failure')
    if failure == 'spawn':
        monkeypatch.setattr(thread_registry, 'spawn', fail)
    elif failure == 'disk':
        monkeypatch.setattr(store, '_write_rows', fail)
    else:
        monkeypatch.setattr(store, 'append_sample', fail)
    deliver()
    for job in jobs:
        job()
    session._overlay.flash_done_and_fade.assert_called_once()
    assert store.list_samples() == []


def test_retry_uses_original_decoder_take_not_release_command(capture):
    session, jobs, deliver = capture
    session._stopped_audio_1042 = np.zeros(800, np.float32)
    expected = store._to_int16_pcm(session._decode_audio).tobytes()
    deliver('Recovered original words.', 'original words')
    jobs[0]()
    row, = store.list_samples()
    with wave.open(str(store.audio_path(row)), 'rb') as wav:
        assert wav.readframes(wav.getnframes()) == expected


@pytest.mark.parametrize('reason', ['cancelled', 'empty', 'missing_audio', 'superseded'])
def test_unaccepted_final_never_collects(capture, monkeypatch, reason):
    session, jobs, deliver = capture
    if reason == 'cancelled':
        session.cancel_event.set()
        store.capture_held_final(session, 'text', 'raw')
    elif reason == 'empty':
        deliver('')
    elif reason == 'missing_audio':
        del session._decode_audio
        deliver()
    else:
        monkeypatch.setattr(tutorial_capture, 'final_current', lambda *a, **k: False)
        monkeypatch.setattr(tutorial_capture, 'retire_final', Mock())
        deliver()
    assert not jobs and not store._benchmark_dir().exists()


def test_overlapping_holds_preserve_rows_and_sample_cap(capture):
    session, jobs, deliver = capture
    session.app.config['benchmark']['max_samples'] = 2
    deliver()
    # Restore actual workers; every append shares the same serialization lock.
    threads = [threading.Thread(target=store.append_sample,
               args=(session.app, session._decode_audio, 16000, str(i), str(i), 'base'))
               for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()
    jobs[0]()
    assert len(store.list_samples()) == 2
    assert len(list(store.audio_dir().glob('*.wav'))) == 2


def test_capture_writer_runs_on_background_thread(capture, monkeypatch):
    session, jobs, deliver = capture
    # Use the production thread registry rather than the deterministic queue.
    monkeypatch.setattr(thread_registry, 'spawn', _REAL_SPAWN)
    called = threading.Event()
    caller = threading.get_ident()
    observed = []
    def write(*a):
        observed.append(threading.get_ident())
        called.set()
    monkeypatch.setattr(store, 'append_sample', write)
    store.capture_held_final(session, 'final', 'raw')
    assert called.wait(2)
    assert observed[0] != caller
