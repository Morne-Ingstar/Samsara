"""Recovery regressions derived from 556's probes; fake audio, real owners.

No desktop bootstrap or live device is imported. Source extraction preserves
the complete methods and lets this file run in a plain repository export.
"""
import ast
import logging
from pathlib import Path
import sys
import threading
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from samsara import microphone_recovery as recovery
from samsara.audio_engine.engine import AudioCaptureEngine
from samsara.portaudio_guard import install, RescanDeferred

ROOT = Path(__file__).resolve().parents[1]
MIC = 'Microphone (Jabra EVOLVE2 65 USB Headset)'


def method(name, **extra):
    tree = ast.parse((ROOT / 'dictation.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(logger=logging.getLogger('558'), np=np, time=recovery.time,
                     current_stage_timing=lambda: SimpleNamespace(
                         feedback_started=Mock(), feedback_finished=Mock()),
                     speech_presence=SimpleNamespace(note_hold_earcon=Mock()), **extra)
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'dictation.py', 'exec'), namespace)
    return namespace[name]


def device(name):
    return dict(name=name, hostapi=0, max_input_channels=1, default_samplerate=16000)


class Audio:
    def __init__(self, devices=()):
        self.devices = list(devices)
        self.pending = None
        self.default = SimpleNamespace(device=(0 if devices else -1, -1))
        self.events, self.created, self.opened = [], [], []
        self.fail_outputs = set()
        self.fail_input = False
        audio = self

        class Base:
            def __init__(self, **kwargs):
                self.kwargs, self.active, self.closed = kwargs, False, False
                audio.created.append(self)

            def start(self):
                if ((isinstance(self, Output) and self.kwargs.get('device') in audio.fail_outputs)
                        or (isinstance(self, Input) and audio.fail_input)):
                    raise OSError('synthetic removal between open and start')
                self.active = True
                if isinstance(self, Input):
                    audio.opened.append(self.kwargs['device'])

            def stop(self):
                self.active = False

            abort = stop

            def close(self):
                self.active, self.closed = False, True

            def __enter__(self):
                self.start()
                return self

            def __exit__(self, *args):
                try:
                    self.stop()
                finally:
                    self.close()

        class Input(Base):
            pass

        class Output(Base):
            pass

        self._StreamBase, self.InputStream, self.OutputStream = Base, Input, Output
        install(self)

    def query_devices(self, index=None, kind=None):
        if index is None and kind is None:
            return self.devices
        if kind == 'output':
            return dict(default_samplerate=16000)
        if index is None:
            index = self.default.device[0]
        if not 0 <= index < len(self.devices):
            raise OSError('no capture endpoint')
        return self.devices[index]

    def query_hostapis(self):
        return [dict(name='MME')]

    def _terminate(self):
        self.events.append('terminate')

    def _initialize(self):
        self.events.append('initialize')
        if self.pending is not None:
            self.devices, self.pending = self.pending, None
            self.default.device = (0 if self.devices else -1, -1)


def owner(monkeypatch, audio, now):
    monkeypatch.setitem(sys.modules, 'sounddevice', audio)
    monkeypatch.setattr(recovery.time, 'monotonic', lambda: now[0])
    app = SimpleNamespace(_audio_refresh_lock=threading.RLock(),
        _buffer_lock=threading.Lock(), _playback_buffer=np.zeros((0, 1), dtype=np.float32),
        _sound_stream=None, _sound_stream_sr=16000, _sound_stream_device=None,
        _no_microphone=True, _using_default_mic=False, _ace_engine=None,
        output_device=None, config={'microphone': 9, 'microphone_name': MIC,
            'audio_feedback': True}, available_mics=[], capture_rate=16000,
        _rescan_idle_since=float('-inf'), _load_sound_cache=Mock(),
        _run_calibration_if_auto=Mock(),
        _sound_cache={'error': np.ones((16, 1), dtype=np.float32)},
        _resolve_playback_output=lambda: None)
    for name in ('_start_sound_stream', '_sound_stream_callback', 'play_sound', 'stop_sound_stream'):
        setattr(app, name, MethodType(method(name, sd=audio), app))
    app.get_available_microphones = lambda: [dict(id=i, **d) for i, d in enumerate(audio.devices)]
    app._prepare_audio_rescan = lambda: recovery.prepare_rescan(app, audio)
    def start():
        ring = SimpleNamespace(write=Mock(), bump_device_epoch=lambda: 1)
        app._ace_engine = AudioCaptureEngine(ring, config=app.config,
            before_reopen=app._prepare_audio_rescan)
        app._ace_engine.start()
        recovery.reconcile(app, audio)
        recovery.set_state(app, False)
    app._start_ace_engine = start
    return app


def test_f1_cue_without_output_does_not_block_real_headset_rescan(monkeypatch):
    now, audio = [0.0], Audio()
    app = owner(monkeypatch, audio, now)
    app.play_sound('error')
    # A cue emitted after the only headset disappeared is never queued.
    assert len(app._playback_buffer) == 0
    audio.pending = [device(MIC)]
    recovery.refresh(app)
    assert audio.events == ['terminate', 'initialize']
    assert app._ace_engine.running
    assert audio.opened == [0]
    app._ace_engine.stop()
    app.stop_sound_stream()


@pytest.mark.parametrize('active,queued', [(False, True), (True, True), (False, False)],
                         ids=['output-lost', 'callback-stalled', 'empty-lost-output-tail'])
def test_f1_stranded_queue_recovers_within_progress_bound(monkeypatch, active, queued):
    now, audio = [0.0], Audio()
    app = owner(monkeypatch, audio, now)
    app._start_sound_stream()
    app.play_sound('error')
    app._sound_stream.active = active
    if not queued:
        app._playback_buffer = app._playback_buffer[:0]
    app._audio_output_idle_after = 1000  # Stale predicted end must not block.
    audio.pending = [device(MIC)]
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: frozenset())
    changes.request_input()
    for instant in (0, .5, 1, 1.5):
        now[0] = instant
        changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert app._ace_engine.running
    assert len(app._playback_buffer) == 0
    app._ace_engine.stop()
    app.stop_sound_stream()


def test_f1_live_cue_callbacks_and_speech_never_get_cancelled(monkeypatch):
    now, audio = [0.0], Audio()
    app = owner(monkeypatch, audio, now)
    app._start_sound_stream()
    app._sound_cache['error'] = np.ones((16000 * 40, 1), dtype=np.float32)
    app.play_sound('error')
    stream = app._sound_stream
    for second in range(35):
        now[0] = second
        app._sound_stream_callback(np.zeros((16000, 1)), 16000, None, None)
        with pytest.raises(RescanDeferred):
            with recovery.rescan_transaction(app, audio):
                pytest.fail('active cue admitted')
        assert app._sound_stream is stream and not stream.closed
    assert len(app._playback_buffer) == 16000 * 5
    app.tts_engine = SimpleNamespace(is_speaking=lambda: True, cancel_all=Mock())
    now[0] = 300
    with pytest.raises(RescanDeferred):
        with recovery.rescan_transaction(app, audio):
            pytest.fail('speech admitted')
    app.tts_engine.cancel_all.assert_not_called()
    app.stop_sound_stream()


@pytest.mark.parametrize('selected,default_fails', [(None, True), (7, True), (7, False)])
def test_f2_failed_output_start_leaves_no_registered_orphan(monkeypatch, selected, default_fails):
    audio = Audio()
    audio.fail_outputs = {selected} | ({None} if default_fails else set())
    app = owner(monkeypatch, audio, [0.0])
    app._resolve_playback_output = lambda: selected
    app._start_sound_stream()
    failed = [s for s in audio.created if s.kwargs.get('device') in audio.fail_outputs]
    assert failed and all(s.closed for s in failed)
    live = [s for s in audio.created if not s.closed]
    assert list(audio._samsara_stream_guard.streams.values()) == live
    with audio._samsara_stream_guard.reserve((app._sound_stream,)):
        pass
    app.stop_sound_stream()
    assert not audio._samsara_stream_guard.streams


@pytest.mark.parametrize('kind', ['InputStream', 'OutputStream'])
@pytest.mark.parametrize('entry', [False, True], ids=['explicit-start', 'context-entry'])
def test_f2_common_guard_cleans_every_stream_start_failure(kind, entry):
    audio = Audio()
    audio.fail_outputs, audio.fail_input = {None}, True
    stream = getattr(audio, kind)()
    with pytest.raises(OSError):
        if entry:
            with stream:
                pytest.fail('failed start entered context')
        else:
            stream.start()
    assert stream.closed
    assert not audio._samsara_stream_guard.streams


def test_f3_comparing_fallback_returns_to_chosen_mic_with_one_real_rescan(monkeypatch):
    now, audio = [0.0], Audio([device('Microphone (Realtek(R) Audio)')])
    app = owner(monkeypatch, audio, now)
    app._start_ace_engine()
    assert app._using_default_mic
    endpoints = [frozenset({('built-in', audio.devices[0]['name'])})]
    reads = Mock(side_effect=lambda: endpoints[0])
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=reads)
    changes.notifications = False
    changes.prime()
    for instant in (0, .5, 1, 4.9, 5):
        now[0] = instant
        changes.tick()
    assert reads.call_count == 3  # prime, t=0, t=5, no extra tick probes
    assert audio.events == []
    endpoints[0] |= {('usb', MIC)}
    audio.pending = [*audio.devices, device(MIC)]
    now[0] = 10
    changes.tick()
    assert audio.events == ['terminate', 'initialize']
    assert audio.opened == [0, 1]
    assert app._ace_engine.capture_name == MIC
    assert not app._using_default_mic
    assert app.config['microphone'] == 9
    for instant in (15, 30, 300):
        now[0] = instant
        changes.tick()
    assert audio.events == ['terminate', 'initialize']
    app._ace_engine.stop()
    app.stop_sound_stream()


def test_f4_unique_mme_prefix_marks_real_capture_lost(monkeypatch):
    audio = Audio([device(MIC[:31])])
    app = owner(monkeypatch, audio, [0.0])
    app.config['microphone_name'] = MIC[:31]
    app._start_ace_engine()
    from samsara.audio_engine import engine
    queued = []
    monkeypatch.setattr(engine.thread_registry, 'spawn', lambda name, fn, **kw: queued.append(fn))
    endpoints = [frozenset({('usb', MIC), ('other', 'Microphone (Realtek(R) Audio)')})]
    changes = recovery.DeviceChanges(app, endpoints=lambda: endpoints[0])
    changes.prime()
    endpoints[0] -= {('usb', MIC)}
    changes._observe()
    assert changes._input_pending
    assert app._ace_engine.device_lost
    assert len(queued) == 1
    app._ace_engine.stop()


def test_f4_ambiguous_mme_prefix_warns_once_without_refresh(monkeypatch, caplog):
    audio = Audio([device(MIC[:31])])
    app = owner(monkeypatch, audio, [0.0])
    app.config['microphone_name'] = MIC[:31]
    app._start_ace_engine()
    endpoints = [frozenset({('usb-a', MIC), ('usb-b', MIC + ' #2')})]
    changes = recovery.DeviceChanges(app, endpoints=lambda: endpoints[0])
    changes.prime()
    for identities in (frozenset({('usb-b', MIC + ' #2')}),
                       frozenset({('usb-a', MIC), ('usb-b', MIC + ' #2')})):
        endpoints[0] = identities
        changes._observe()
    assert not changes._input_pending
    assert not app._ace_engine.device_lost
    messages = [r.getMessage() for r in caplog.records if 'ambiguous' in r.getMessage()]
    assert messages == ['[MIC] Capture endpoint name is ambiguous; leaving audio unchanged']
    assert MIC not in caplog.text
    assert audio.events == []
    app._ace_engine.stop()


def test_g1_refused_hold_retry_then_fresh_stop_cue_keeps_first_callback_grace(monkeypatch):
    now, audio = [0.0], Audio()
    app = owner(monkeypatch, audio, now)
    app._start_sound_stream()
    app.play_sound('error')
    with pytest.raises(RescanDeferred):
        recovery.refresh(app)
    # The old cue drains, but a hold refuses the next retry before cue_busy.
    app._sound_stream_callback(np.zeros((16, 1)), 16, None, None)
    now[0] = 10.0
    app.recording = True
    with pytest.raises(RescanDeferred, match='Capture session is open'):
        recovery.refresh(app)
    app.recording = False
    app._sound_cache['stop'] = np.ones((1024, 1), dtype=np.float32)
    app.play_sound('stop')
    with pytest.raises(RescanDeferred):
        recovery.refresh(app)  # Before this cue's first callback (~21 ms).
    assert len(app._playback_buffer) == 1024
    assert not app._sound_stream.closed
    assert audio.events == []
    app._sound_stream_callback(np.zeros((1024, 1)), 1024, None, None)
    app.stop_sound_stream()


@pytest.mark.parametrize('state', ['lost', 'fallback'])
def test_g3_ambiguous_name_still_requests_lost_or_fallback_input(monkeypatch, state, caplog):
    audio = Audio([device(MIC[:31])])
    app = owner(monkeypatch, audio, [0.0])
    app.config['microphone_name'] = MIC[:31]
    app._start_ace_engine()
    if state == 'lost':
        app._ace_engine._device_lost = True
    else:
        app._using_default_mic = True
    endpoints = [frozenset({('usb-a', MIC), ('usb-b', MIC + ' #2')})]
    changes = recovery.DeviceChanges(app, endpoints=lambda: endpoints[0])
    changes.prime()
    endpoints[0] |= {('other', 'Microphone (Realtek(R) Audio)')}
    changes._observe()
    assert changes._input_pending
    assert not audio.events
    assert MIC not in caplog.text
    app._ace_engine.stop()


@pytest.mark.parametrize('callbacks', [False, True], ids=['stalled', 'live'])
def test_g2_real_speech_worker_bounds_stall_but_preserves_callback_progress(monkeypatch, callbacks):
    from samsara.tts import winrt_engine as winrt
    from samsara.tts.engine_base import SpeechHandle

    now, audio = [0.0], Audio()
    app = owner(monkeypatch, audio, now)
    monkeypatch.setattr(winrt, '_import_winsdk', lambda: (None, None, None, None))
    monkeypatch.setattr(winrt, 'get_helper', lambda: None)
    monkeypatch.setattr(winrt.WinRTEngine, '_build_voice_list', lambda self: [])
    engine = winrt.WinRTEngine()
    app.tts_engine = engine
    pcm = np.ones(16000 * 4, dtype=np.float32)
    monkeypatch.setattr(engine, '_synthesize', lambda *args: b'fake pcm')
    monkeypatch.setattr(winrt, 'parse_wav', lambda raw: (pcm, 16000, 1))
    monkeypatch.setattr(winrt, 'resample_pcm', lambda data, *args: data)
    done = Mock()
    utterance = winrt._Utterance(SpeechHandle('558', 'winrt'), done, sample_rate=16000)
    engine._active['558'] = utterance
    stream = engine._tts_stream
    # An earlier utterance proves a queued one must not time out behind live audio.
    if callbacks:
        engine._tts_buffer.append((np.ones(16000 * 2, dtype=np.float32), 'earlier'))

    def sleep(_seconds):
        now[0] = round(now[0] + .25, 2)
        if callbacks:
            stream.kwargs['callback'](np.zeros((4000, 1)), 4000, None, None)
            with pytest.raises(RescanDeferred):
                recovery.refresh(app)
            assert not stream.closed
            assert engine.is_speaking()
            done.assert_not_called()
        else:
            assert now[0] <= 1.5, 'speech drain exceeded the progress bound'

    monkeypatch.setattr(recovery.time, 'sleep', sleep)
    try:
        engine._playback_worker(utterance, '', None, 1.0, 0.0)
        assert utterance.get_engine_state() == ('done' if callbacks else 'failed')
        assert not engine.is_speaking()
        assert not engine._tts_buffer
        done.assert_called_once_with()
        if callbacks:
            assert now[0] == 6.0  # Long speech and queueing are not an absolute timeout.
        else:
            assert now[0] <= 1.25
        audio.pending = [device(MIC)]
        changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: frozenset())
        changes.request_input()
        changes.tick()
        now[0] += .5
        changes.tick()
        assert audio.events == ['terminate', 'initialize']
        assert app._ace_engine.running
    finally:
        if app._ace_engine is not None:
            app._ace_engine.stop()
        app.stop_sound_stream()
        engine.shutdown()
