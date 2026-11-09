"""1200: truthful CPU/load/decode receipts with fake time and speech models."""
import logging
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara import game_mode as gm
from samsara.vocabulary_decode import PATHS, transcribe_with_vocabulary


class Clock:
    now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Model:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.pulled = 0
        self.failure = None
        self.info = object()
        self.items = [object(), object()]

    def transcribe(self, audio, **params):
        self.calls.append((audio, params))
        self.clock.advance(.01)
        if self.failure == 'call':
            raise RuntimeError('synthetic failure')

        def segments():
            for item in self.items:
                self.clock.advance(.04)
                self.pulled += 1
                if self.failure == 'pull':
                    raise RuntimeError('synthetic failure')
                yield item
            self.clock.advance(.005)

        return segments(), self.info


@pytest.fixture
def rig(monkeypatch, caplog):
    clock = Clock()
    monkeypatch.setattr(gm.time, 'perf_counter', clock)
    primary, cpu = Model(clock), Model(clock)
    app = NS(config={'model_size': 'small.en', 'game_mode': {},
                     'dictation': {'signoff_filter': False}},
             model=primary, device_type='cuda', compute_type='float16',
             model_lock=threading.Lock(), recording=False)
    factory = Mock(return_value=cpu)
    policy = app._game_mode = gm.GameMode(app, factory, notify=Mock(), clock=clock)
    policy.manual = True
    # Vocabulary sourcing is independent; retain the real wrapper and decoder.
    monkeypatch.setattr('samsara.vocabulary_decode._terms', lambda app: [])
    caplog.set_level(logging.INFO, logger='samsara.game_mode')
    return NS(app=app, policy=policy, factory=factory, primary=primary, cpu=cpu, clock=clock)


def messages(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def enter(rig):
    assert rig.policy.prepare()
    assert rig.policy.reconcile()


def test_cpu_load_receipt_once_per_successful_instance(rig, caplog):
    def load(*args, **kwargs):
        rig.clock.advance(.125)
        return rig.cpu

    rig.factory.side_effect = load
    assert rig.policy.prepare()
    assert not rig.policy.prepare()
    assert messages(caplog, '[GAME-MODE] CPU model loaded') == [
        '[GAME-MODE] CPU model loaded model=base.en device=cpu compute_type=int8 load_ms=125.0']
    rig.app.config['game_mode']['cpu_model'] = 'tiny.en'
    assert rig.policy.prepare()
    assert len(messages(caplog, '[GAME-MODE] CPU model loaded')) == 2


def test_failed_load_has_no_success_receipt_or_cpu_indicator(rig, caplog):
    rig.factory.side_effect = OSError('synthetic failure')
    assert not rig.policy.prepare()
    assert not rig.policy.prepare()
    assert not messages(caplog, '[GAME-MODE] CPU model loaded')
    assert not rig.policy.using_cpu
    assert rig.app.model is rig.primary


@pytest.mark.parametrize('path', PATHS)
def test_every_decode_path_reports_actual_model_and_native_time(rig, caplog, path):
    enter(rig)
    caplog.clear()
    audio = object()
    segments, info = transcribe_with_vocabulary(rig.app, audio, path=path, beam_size=3)
    selected = rig.primary if path in ('ava', 'command_fast') else rig.cpu
    assert info is selected.info
    assert selected.pulled == 0  # Diagnostics never force eager CPU decoding.
    assert selected.calls == [(audio, {'beam_size': 3, 'hotwords': None})]
    assert not messages(caplog, '[GAME-MODE] Decode')
    assert next(segments) is selected.items[0]
    rig.clock.advance(10)  # Consumer delay is not decoder work.
    assert list(segments) == selected.items[1:]
    name, device, compute = ('small.en', 'cuda', 'float16') if selected is rig.primary else (
        'base.en', 'cpu', 'int8')
    assert messages(caplog, '[GAME-MODE] Decode') == [
        f'[GAME-MODE] Decode path={path} model={name} device={device} '
        f'compute_type={compute} decode_ms=95.0 status=ok']


def test_each_hold_has_receipt_even_with_identical_state(rig, caplog):
    enter(rig)
    for _ in range(3):
        segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold_final')
        list(segments)
    assert len(messages(caplog, '[GAME-MODE] Decode path=hold_final')) == 3


def test_wanted_but_unloaded_reports_cuda_not_saved_cpu_choice(rig, caplog):
    rig.app.config['game_mode']['cpu_model'] = 'tiny.en'
    segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold')
    list(segments)
    assert 'model=small.en device=cuda compute_type=float16' in messages(
        caplog, '[GAME-MODE] Decode')[0]
    assert not rig.policy.using_cpu


def test_pending_replacement_reports_loaded_name(rig, caplog):
    enter(rig)
    rig.app.config['game_mode']['cpu_model'] = 'tiny.en'
    rig.factory.return_value = Model(rig.clock)
    assert rig.policy.prepare()
    segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold')
    list(segments)
    assert 'model=base.en device=cpu compute_type=int8' in messages(
        caplog, '[GAME-MODE] Decode')[0]
    assert rig.policy.using_cpu


@pytest.mark.parametrize('failure,ms', [('call', '10.0'), ('pull', '50.0')])
def test_failure_is_logged_once_and_propagates_without_error_text(rig, caplog, failure, ms):
    enter(rig)
    rig.cpu.failure = failure
    with pytest.raises(RuntimeError, match='synthetic failure'):
        segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold')
        list(segments)
    receipts = messages(caplog, '[GAME-MODE] Decode')
    assert len(receipts) == 1
    assert f'decode_ms={ms} status=error' in receipts[0]
    assert 'synthetic failure' not in receipts[0]


def test_cancelled_lazy_decode_logs_work_already_done(rig, caplog):
    enter(rig)
    observed = gm.observe_decode(rig.app, rig.cpu, 'hold_final')
    segments, _ = observed.transcribe(object())
    next(segments)
    segments.close()
    assert 'decode_ms=50.0 status=cancelled' in messages(caplog, '[GAME-MODE] Decode')[0]


def test_no_proxy_or_receipt_when_game_mode_is_not_wanted(rig, caplog):
    rig.policy.manual = False
    assert gm.observe_decode(rig.app, rig.primary, 'hold') is rig.primary
    segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold')
    list(segments)
    assert not messages(caplog, '[GAME-MODE] Decode')


def test_cuda_recovery_receipt_names_device_that_completed_decode(rig, caplog):
    from samsara.gpu_fallback import ResilientSpeechModel

    class FailingCUDA:
        def transcribe(self, *args, **kwargs):
            raise RuntimeError('CUDA failed to load')

    rig.app.model = ResilientSpeechModel(rig.app, FailingCUDA(), lambda: rig.primary,
                                        device='cuda', notify=Mock())
    segments, _ = transcribe_with_vocabulary(rig.app, object(), path='hold')
    list(segments)
    assert 'model=small.en device=cpu compute_type=int8' in messages(
        caplog, '[GAME-MODE] Decode')[0]
    assert not rig.policy.using_cpu  # Recovery CPU is not the game-mode instance.


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_display_tracks_selected_cpu_across_loading_waiting_and_restore(
        qapp, monkeypatch, rig, display):
    from samsara.ui import qt_runtime
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import Lane

    monkeypatch.setattr(qt_runtime, 'post', lambda callback: callback())
    controller = None
    if display == 'compact':
        indicator = ListeningIndicator()
        tagged = lambda: 'Game mode' in indicator._resolve_colors()[2]
    else:
        rig.app.config['ui'] = {'live_surface': {'enabled': True}}
        controller = LiveSurfaceController(rig.app)
        controller.start()
        controller.model.lane = Lane.HOLD
        indicator = LiveSurfaceIndicatorAdapter(controller)
        tagged = lambda: 'Game mode' in controller.widget._lane_text(controller.view())
    rig.app.listening_indicator = indicator
    rig.policy.notify = gm.publish
    try:
        gm.publish(rig.app, True)  # Pending intent cannot claim CPU.
        assert not tagged()

        def load(*args, **kwargs):
            gm.publish(rig.app, True)
            assert not tagged()
            return rig.cpu

        rig.factory.side_effect = load
        assert rig.policy.prepare()
        rig.app.recording = True
        assert not rig.policy.reconcile()
        gm.publish(rig.app, True)
        assert not tagged()
        rig.app.recording = False
        assert rig.policy.reconcile()
        assert tagged()
        rig.app.recording = True
        rig.policy.set_manual(False)
        gm.publish(rig.app, False)
        assert tagged()  # CPU still selected until capture is idle.
        rig.app.recording = False
        assert rig.policy.reconcile()
        assert not tagged()
    finally:
        (controller.widget if controller else indicator).close()


def test_stale_active_flag_cannot_label_cuda_as_cpu(rig, monkeypatch):
    from samsara.ui import qt_runtime
    monkeypatch.setattr(qt_runtime, 'post', lambda callback: callback())
    rig.app.listening_indicator = NS(set_game_mode=Mock())
    rig.policy.active = True
    gm.publish(rig.app, True)
    rig.app.listening_indicator.set_game_mode.assert_called_once_with(False)
