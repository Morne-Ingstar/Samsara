"""1229: metadata-only diagnostics, fake streams; no microphone or app bootstrap."""
import ast
import inspect
import logging
import textwrap
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara.audio_engine import engine as mod
from samsara.audio_engine.engine import AudioCaptureEngine
from samsara.audio_engine.ring import FrameBus


def engine():
    result = AudioCaptureEngine(FrameBus())
    result._running = True
    result._device_identity = ('Test input', 'Test API')
    return result


def trigger(e, monkeypatch, origin='portaudio-finished'):
    workers = []
    monkeypatch.setattr(mod.thread_registry, 'spawn', lambda *a, **k: workers.append(a))
    e._on_stream_finished(origin=origin)
    return workers


@pytest.mark.parametrize('origin', ['portaudio-finished', 'callback-error',
                                  'endpoint-removed', 'device-invalidated', 'unknown'])
def test_origins_and_worker_only_line(origin, monkeypatch, caplog):
    e = engine()
    e._last_callback_status = 'input overflow'
    e._last_callback_exception = ValueError if origin == 'callback-error' else None
    e._open_cursor = -3
    e._last_frame_at = 8.0
    monkeypatch.setattr(mod.time, 'perf_counter', lambda: 10.0)
    workers = trigger(e, monkeypatch, origin)
    assert workers[0][0] == 'ace-recovery'
    assert not caplog.records
    # Recovery worker enters with no runnable recovery work, but still logs.
    e._running = False
    with caplog.at_level(logging.INFO):
        workers[0][1]()
    lines = [r.message for r in caplog.records if 'Stream stopped unexpectedly' in r.message]
    assert len(lines) == 1
    line = lines[0]
    for field in (f'origin={origin}', 'callback_status=input overflow',
                  'frames_since_open=3', 'ms_since_last_frame=2000.0',
                  'device_epoch=0', "device='Test input'", "host_api='Test API'",
                  'hold_active=unknown', 'game_mode=unknown'):
        assert field in line
    assert f'callback_exception={"ValueError" if origin == "callback-error" else "none"}' in line


def test_endpoint_entry_and_dedup_preserve_first_snapshot(monkeypatch):
    e = engine()
    workers = []
    monkeypatch.setattr(mod.thread_registry, 'spawn', lambda *a, **k: workers.append(a))
    e.mark_device_lost()
    snapshot = e._loss_diagnostics
    e._on_stream_finished()
    assert e._loss_diagnostics is snapshot
    assert snapshot[0] == 'endpoint-removed'
    assert len(workers) == 1


def test_intentional_stop_does_not_snapshot_or_spawn(monkeypatch):
    e = engine()
    e._running = False
    assert trigger(e, monkeypatch) == []
    assert e._loss_diagnostics is None


def test_app_context_is_read_before_death_handler(monkeypatch, caplog):
    e = engine()
    class App:
        _hotkey_recording = True
        _game_mode = NS(active=True)
        def death(self):
            self._hotkey_recording = False
    app = App()
    e._on_stream_death = app.death
    trigger(e, monkeypatch)
    e._running = False
    e._recovery_loop()
    assert 'hold_active=yes game_mode=on context_at=worker-entry' in caplog.text
    assert app._hotkey_recording is False


def test_callback_metadata_no_log_no_new_container_and_same_pcm(monkeypatch):
    e = engine()
    status = object()
    block = np.linspace(-.25, .25, 1600, dtype=np.float32).reshape(-1, 1)
    reader = e._ring.new_reader('test')
    monkeypatch.setattr(mod, 'logger', NS(**{key: lambda *a, **k: pytest.fail('callback logged')
                                           for key in ('error', 'warning', 'info', 'debug', 'exception')}))
    e._on_audio_block(block, 1600, None, status)
    assert e._last_callback_status is status
    assert e._last_frame_at is not None
    assert e._overflow_count == 1
    frame = reader.read_next()
    np.testing.assert_array_equal(frame.pcm, np.clip(block[:, 0] * 32767., -32768, 32767).astype(np.int16))
    tree = ast.parse(textwrap.dedent(inspect.getsource(AudioCaptureEngine._on_audio_block)))
    wrapper = tree.body[0].body[1]
    assert isinstance(wrapper, ast.Try)
    # Added callback work is two scalar/reference assignments and a class
    # reference on failure. Existing NumPy allocations are deliberately kept.
    diagnostic_stores = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
                         and any(isinstance(t, ast.Attribute) and t.attr.startswith('_last_') for t in n.targets)]
    assert len(diagnostic_stores) == 3
    assert not any(isinstance(n, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp,
                                  ast.SetComp, ast.JoinedStr)) for a in diagnostic_stores for n in ast.walk(a))
    assert {n.func.id for a in diagnostic_stores for n in ast.walk(a)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)} == {'type'}


@pytest.mark.parametrize('failure', [ValueError, MemoryError, SystemExit])
def test_callback_preserves_exception_identity_and_no_traceback_reference(failure, monkeypatch):
    e = engine()
    exc = failure('not recorded')
    def fail(*a):
        raise exc
    monkeypatch.setattr(e._ring, 'write', fail)
    with pytest.raises(failure) as caught:
        e._on_audio_block(np.zeros((1600, 1), dtype=np.float32), 1600, None, False)
    assert caught.value is exc
    assert e._last_callback_exception is failure
    assert e._last_frame_at is None
    trigger(e, monkeypatch)
    assert e._loss_diagnostics[0] == 'callback-error'


@pytest.mark.parametrize('arrived', [False, True])
def test_gap_reports_audio_evidence_and_resets(arrived, monkeypatch, caplog):
    e = engine()
    now = [5.0]
    monkeypatch.setattr(mod.time, 'perf_counter', lambda: now[0])
    trigger(e, monkeypatch)
    now[0] = 7.5
    if arrived:
        e._on_audio_block(np.zeros((1600, 1), dtype=np.float32), 1600, None, False)
    with caplog.at_level(logging.INFO):
        e._log_recovery_gap()
        e._log_recovery_gap()
    lines = [r.message for r in caplog.records if 'Stream recovered gap_ms' in r.message]
    assert len(lines) == 1
    assert f'gap_ms=2500.000 audio_arrived_during_gap={"yes" if arrived else "no"}' in lines[0]
    assert e._loss_diagnostics is None


@pytest.mark.parametrize('fail_first', [False, True])
def test_recovery_original_call_and_state_sequence(fail_first, monkeypatch):
    e = engine()
    events = []
    class Stream:
        def stop(self): events.append('stop')
        def close(self): events.append('close')
    e._stream = Stream()
    e._on_stream_death = lambda: events.append(('death', e._recovering, e.device_lost))
    e._on_recovery_success = lambda: events.append(('success', e._recovering, e.device_lost))
    e._reconnect_now = NS(wait=lambda timeout: events.append(('wait', timeout)), clear=lambda: events.append('clear'))
    e._resolve_device = lambda: (events.append('resolve') or (4, ''))
    attempts = []
    def opened(device):
        events.append(('open', device))
        attempts.append(device)
        if fail_first and len(attempts) == 1:
            raise RuntimeError('fake missing device')
        e._stream = Stream()
    e._open_stream = opened
    e.bump_device_epoch = lambda: events.append('epoch')
    trigger(e, monkeypatch)
    e._recovery_loop()
    expected = [('death', True, True), ('wait', 2.0), 'clear', 'stop', 'close', 'resolve', ('open', 4)]
    if fail_first:
        expected += [('wait', 2.0), 'clear', 'resolve', ('open', 4)]
    expected += ['epoch', ('success', False, False)]
    assert events == expected
    assert e._running is True
    assert e._recovering is False
    assert e.device_lost is False


def test_failed_refresh_is_unknown_not_invented_native_invalidation(monkeypatch):
    e = engine()
    monkeypatch.setattr(e, '_try_reopen_locked', lambda **kw: False)
    monkeypatch.setattr(mod.thread_registry, 'spawn', lambda *a, **k: None)
    assert e.refresh({}) is False
    assert e._loss_diagnostics[0] == 'unknown'
