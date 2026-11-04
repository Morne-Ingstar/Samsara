"""Idle admission, exact restoration and native worker policy without app boot."""
import copy
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara.game_mode import GameMode, Sample, before_capture, decode_model, partials_enabled


@pytest.fixture
def rig():
    app = NS(config={'model_size': 'small', 'device': 'auto', 'game_mode': {}},
             model=object(), device_type='cuda', compute_type='float16',
             model_lock=threading.Lock(), _hold_capture_lifecycle_lock=threading.RLock(),
             recording=False, _running=True)
    factory, notify = Mock(return_value=object()), Mock()
    app._game_mode = GameMode(app, factory, notify=notify)
    return app, app._game_mode, factory, notify


@pytest.mark.parametrize('busy', ['recording', '_hotkey_recording', '_stop_in_flight',
                                 '_hold_park_finalizing', '_mic_score_active', 'loading_model'])
def test_never_swaps_during_capture_or_finalization(rig, busy):
    app, policy, factory, notify = rig
    original = app.model
    setattr(app, busy, True)
    policy.set_manual(True)
    policy.prepare()
    assert not policy.reconcile()
    assert app.model is original
    setattr(app, busy, False)
    assert policy.reconcile()
    assert app.model is factory.return_value


@pytest.mark.parametrize('busy', ['session', 'decoder', 'model_lock', 'lifecycle'])
def test_pending_or_locked_work_prevents_switch(rig, busy):
    app, policy, *_ = rig
    policy.manual = True
    policy.prepare()
    other = threading.Event()
    release = threading.Event()
    if busy == 'session':
        app._streaming_session = object()
    elif busy == 'decoder':
        app._hold_decode_job = NS(done=threading.Event())
    elif busy == 'model_lock':
        app.model_lock.acquire()
    else:
        def own_lock():
            with app._hold_capture_lifecycle_lock:
                other.set()
                release.wait(2)
        thread = threading.Thread(target=own_lock)
        thread.start()
        assert other.wait(1)
    try:
        assert not policy.reconcile()
        assert not policy.active
    finally:
        if busy == 'model_lock':
            app.model_lock.release()
        if busy == 'lifecycle':
            release.set()
            thread.join(2)


def test_exact_state_restored_resident_reused_and_notice_once(rig):
    app, policy, factory, notify = rig
    original = app.model, app.device_type, app.compute_type
    config = copy.deepcopy(app.config)
    for _ in range(2):
        policy.manual = True
        policy.prepare()
        assert policy.reconcile()
        assert (app.device_type, app.compute_type) == ('cpu', 'int8')
        assert not partials_enabled(app)
        policy.set_manual(False)
        assert (app.model, app.device_type, app.compute_type) == original
        assert partials_enabled(app)
        assert app.config == config
    factory.assert_called_once_with('base.en', device='cpu', compute_type='int8', cpu_threads=4, num_workers=2)
    assert sum(call.args[2] is not None for call in notify.call_args_list) == 1


def test_manual_override_beats_auto_in_both_directions(rig):
    app, policy, *_ = rig
    policy.sample = Sample(True, True, 42)
    assert policy.wanted
    policy.set_manual(False)
    assert not policy.wanted
    policy.sample = Sample()
    policy.set_manual(True)
    assert policy.wanted
    policy.set_manual(None)
    assert not policy.wanted
    app.config['game_mode']['detect'] = 'manual'
    policy.sample = Sample(True, True, 42)
    assert policy.wanted  # Explicit auto also overrides the saved manual setting.


def test_saved_manual_detection_waits_for_explicit_auto(rig):
    app, policy, *_ = rig
    app.config['game_mode']['detect'] = 'manual'
    policy.sample = Sample(True, True, 42)
    assert not policy.wanted
    policy.set_manual(None)
    assert policy.wanted
    assert app.config['game_mode']['detect'] == 'manual'


def test_primary_cuda_recovery_is_preserved_when_game_mode_leaves(rig):
    from samsara.gpu_fallback import ResilientSpeechModel
    app, policy, *_ = rig
    primary = ResilientSpeechModel(app, object(), lambda: object(), device='cuda')
    app.model = primary
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    primary._device = 'cpu'  # A recovery on the independent Ava/command lane.
    policy.set_manual(False)
    assert app.model is primary
    assert (app.device_type, app.compute_type) == ('cpu', 'int8')


def test_fake_fullscreen_gpu_sample_triggers_real_monitor(rig):
    app, policy, *_ = rig
    def probe():
        policy.stop.set()  # one real monitor iteration
        return Sample(True, True, 42)
    policy.probe = probe
    policy.run()
    assert policy.active


@pytest.mark.parametrize('sample', [Sample(True, False), Sample(False, True), Sample()])
def test_auto_needs_both_signals(rig, sample):
    _, policy, *_ = rig
    policy.sample = sample
    policy.prepare()
    assert not policy.reconcile()


def test_override_during_hold_applies_only_after_hold(rig):
    app, policy, *_ = rig
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    app.recording = True
    policy.set_manual(False)
    assert policy.active
    app.recording = False
    assert policy.reconcile()
    assert not policy.active


def test_ava_uses_primary_without_losing_resident(rig):
    app, policy, factory, _ = rig
    original = app.model
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    assert decode_model(app, 'ava') is original
    assert decode_model(app, 'hold_final') is factory.return_value
    before_capture(app, 'ava_hold')
    assert app.model is original and app.device_type == 'cuda'
    before_capture(app, 'dictation_hold')
    assert app.model is factory.return_value
    factory.assert_called_once()


def test_failed_load_keeps_working_model_and_can_retry(rig):
    app, policy, factory, notify = rig
    original = app.model
    factory.side_effect = OSError('unavailable')
    policy.manual = True
    policy.prepare()
    assert not policy.reconcile()
    assert app.model is original
    assert notify.call_count == 1
    factory.side_effect = None
    policy.set_manual(True)
    policy.prepare()
    assert policy.reconcile()


def test_changed_cpu_model_swaps_only_at_idle_and_restores_original(rig):
    app, policy, factory, _ = rig
    original = app.model
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    app.recording = True
    app.config['game_mode']['cpu_model'] = 'small.en'
    factory.return_value = object()
    policy.prepare()
    assert not policy.reconcile()
    app.recording = False
    assert policy.reconcile()
    assert app.model is factory.return_value
    policy.set_manual(False)
    assert app.model is original


def test_streaming_worker_gate_still_runs_final(rig, monkeypatch):
    from samsara.streaming import StreamingWorker
    app, policy, *_ = rig
    policy.active = True  # gate also protects the interval before CPU metadata settles
    session = NS(app=app, stop_event=threading.Event(), cancel_event=threading.Event())
    session.stop_event.set()
    worker = StreamingWorker(session)
    partial, final = Mock(), Mock()
    monkeypatch.setattr(worker, '_loop_partials', partial)
    monkeypatch.setattr(worker, '_final_pass', final)
    worker.run()
    partial.assert_not_called()
    final.assert_called_once()


def test_handsfree_gate_does_not_acquire_decode_lock(rig):
    from samsara.streaming import DictatePreviewSession
    app, policy, *_ = rig
    policy.active = True
    app.model_lock = Mock()
    session = DictatePreviewSession.__new__(DictatePreviewSession)
    session.app = app
    assert session._transcribe_partial() is None
    app.model_lock.acquire.assert_not_called()


def test_cpu_policy_remains_off_after_game_mode_leaves(rig):
    app, policy, *_ = rig
    app.device_type, app.compute_type = 'cpu', 'int8'
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    policy.set_manual(False)
    assert not partials_enabled(app)


def test_handsfree_dictation_uses_cpu_but_ava_session_keeps_primary(rig):
    from samsara.session_modes import SessionMode
    app, policy, factory, _ = rig
    original = app.model
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    app._session_mode_manager = NS(mode=SessionMode.DICTATE)
    assert decode_model(app, 'wake') is factory.return_value
    app._session_mode_manager.mode = SessionMode.AVA
    assert decode_model(app, 'wake') is original


def test_primary_reload_replaces_restore_point_without_dropping_cpu_resident(rig):
    from samsara.game_mode import install
    app, policy, factory, _ = rig
    policy.manual = True
    policy.prepare()
    policy.reconcile()
    replacement = object()
    app.model, app.device_type, app.compute_type = replacement, 'cpu', 'int8'
    install(app, factory)
    assert not policy.active
    assert policy.resident is factory.return_value
    policy.reconcile()
    policy.set_manual(False)
    assert app.model is replacement


def test_auto_exit_restores_primary_and_next_entry_reuses_cpu(rig):
    app, policy, factory, _ = rig
    original = app.model
    policy.sample = Sample(True, True, 42)
    policy.prepare()
    policy.reconcile()
    policy.sample = Sample()
    assert policy.reconcile()
    assert app.model is original
    policy.sample = Sample(True, True, 42)
    policy.prepare()
    assert policy.reconcile()
    factory.assert_called_once()


def test_tray_toggle_and_auto_actions_control_same_override(rig):
    from samsara.game_mode import add_tray
    app, policy, *_ = rig
    calls = []
    tray = NS(_app=app, _add=lambda menu, label, fn, **kw: calls.append((label, fn, kw)))
    add_tray(tray, object())
    calls[0][1](True)
    assert policy.manual is True
    calls[0][1](False)
    assert policy.manual is False
    calls[1][1](True)
    assert policy.manual is None


def test_voice_controls_use_same_policy(rig):
    from plugins.commands.game_mode import game_mode_on, game_mode_off, game_mode_auto
    app, policy, *_ = rig
    assert game_mode_on(app)
    assert policy.manual is True
    assert game_mode_off(app)
    assert policy.manual is False
    assert game_mode_auto(app)
    assert policy.manual is None


def test_settings_controls_save_schema_values_and_preserve_unknown_keys(qapp, monkeypatch):
    from samsara.ui.settings_qt import _SettingsWindow
    from samsara import autostart
    from tests.test_settings_pages import _StubApp
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **kw: None)
    app = _StubApp({'game_mode': {'future': 7}})
    window = _SettingsWindow(app)
    try:
        window._ensure_destination(1)
        assert window._widgets['game_enabled'].isChecked()
        assert window._widgets['game_model'].currentText() == 'base.en'
        window._widgets['game_detect'].setCurrentText('manual')
        window._widgets['game_model'].setCurrentText('small.en')
        saves = [save({}) for save in window._source_saves['advanced']]
        result = next(save['game_mode'] for save in saves if 'game_mode' in save)
        assert result == dict(enabled=True, detect='manual', cpu_model='small.en', future=7)
    finally:
        window.close()
        window.deleteLater()


def test_both_display_tags_and_ava_label(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import Lane
    compact = ListeningIndicator()
    compact.set_game_mode(True)
    assert 'Game mode' in compact._resolve_colors()[2]
    compact.set_game_mode(False)
    assert 'Game mode' not in compact._resolve_colors()[2]
    controller = LiveSurfaceController(NS(config={}))
    controller.start()
    full = LiveSurfaceIndicatorAdapter(controller)
    try:
        full.set_game_mode(True)
        controller.model.lane = Lane.HOLD
        assert 'Game mode' in controller.widget._lane_text(controller.view())
        controller.model.lane = Lane.AVA
        assert 'Game mode' not in controller.widget._lane_text(controller.view())
    finally:
        controller.widget.close()
        compact.close()
