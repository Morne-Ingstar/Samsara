"""1127: default-input events and stopped wake word revival, with no live audio."""
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from samsara import microphone_recovery as recovery
from samsara.audio_engine.wake_consumer import fault_from
from tests.test_audio_hotplug_recovery import Audio, dev, engine, recovery_app, app_method


def rig(monkeypatch, *, default=True):
    audio = Audio([dev('First'), dev('Second')], default=0)
    app = recovery_app(audio)
    app.config = {'microphone': None if default else 0,
                  'microphone_name': 'System default' if default else 'First',
                  'wake_word_enabled': True}
    app._no_microphone = False
    app.wake_word_active = True
    app._wake_consumer = SimpleNamespace(running=True)
    app._publish_hands_free_fault = Mock()
    app._hands_free_fault = None
    app.restart_hands_free = Mock()
    capture = engine(monkeypatch, audio, dict(app.config), before_reopen=app._prepare_audio_rescan)
    app._ace_engine = capture
    capture.start()
    now, default_id, endpoints = [0.0], ['first'], [frozenset({('first', 'First'), ('second', 'Second')})]
    changes = recovery.DeviceChanges(app, clock=lambda: now[0], endpoints=lambda: endpoints[0],
                                    input_id=lambda: default_id[0])
    app._microphone_changes = changes
    changes.prime()
    return audio, app, capture, changes, now, default_id, endpoints


def event(changes, now):
    changes.notify()
    now[0] += 1
    changes.tick()


def test_default_change_with_armed_wake_keeps_stream_and_queues_refresh(monkeypatch):
    audio, app, capture, changes, now, default_id, _ = rig(monkeypatch)
    try:
        audio.default.device = (1, -1)
        default_id[0] = 'second'
        event(changes, now)
        assert audio.opened == [0]  # A live armed capture keeps its stream.
        assert changes._input_pending and app._wake_consumer.running
        app.wake_word_active = False  # Wake word no longer armed: queued refresh runs.
        now[0] += 1
        changes.tick()
        assert audio.opened == [0, 1]
        assert not capture.device_lost
    finally:
        capture.stop()


def test_unrelated_default_change_does_not_cycle_chosen_mic(monkeypatch):
    audio, app, capture, changes, now, default_id, _ = rig(monkeypatch, default=False)
    try:
        audio.default.device = (1, -1)
        default_id[0] = 'second'
        event(changes, now)
        assert audio.opened == [0]
    finally:
        capture.stop()


def test_device_event_revives_stopped_consumer_and_clears_fault(monkeypatch):
    audio, app, capture, changes, now, default_id, _ = rig(monkeypatch)
    try:
        app._wake_consumer.running = False
        app.wake_word_active = False
        app._hands_free_fault = fault_from(OSError('device'), at=0)
        def restart(**kwargs):
            app._wake_consumer.running = app.wake_word_active = True
            app._hands_free_fault = None
        app.restart_hands_free.side_effect = restart
        audio.default.device = (1, -1)
        default_id[0] = 'second'
        event(changes, now)
        app.restart_hands_free.assert_called_once_with(source='device-change')
        assert audio.opened == [0, 1] and app._hands_free_fault is None
    finally:
        capture.stop()


def test_restart_failures_are_bounded_and_leave_home_action(monkeypatch, caplog):
    audio, app, capture, changes, now, _, _ = rig(monkeypatch)
    try:
        app._wake_consumer.running = False
        app.wake_word_active = False
        changes.request_wake()
        with caplog.at_level('INFO'):
            for moment in (0, 1, 2, 6, 7, 30, 100):
                now[0] = moment
                changes.tick()
        assert app.restart_hands_free.call_count == 3
        assert not changes._wake_pending
        assert 'attempt 3/3: running=0' in caplog.text
        assert app._hands_free_fault.reason.startswith('Wake word stopped')
        assert not app._hands_free_fault.retrying
        app._publish_hands_free_fault.assert_called_once()
    finally:
        capture.stop()


@pytest.mark.parametrize('blocked', ['recording', 'command_mode_active'])
def test_input_change_waits_for_real_capture_to_finish(monkeypatch, blocked):
    audio, app, capture, changes, now, default_id, _ = rig(monkeypatch)
    try:
        app.wake_word_active = False
        setattr(app, blocked, True)
        audio.default.device = (1, -1)
        default_id[0] = 'second'
        event(changes, now)
        assert audio.opened == [0]
        setattr(app, blocked, False)
        now[0] += 1
        changes.tick()
        assert audio.opened == [0, 1]
    finally:
        capture.stop()


@pytest.mark.parametrize('blocked', ['snoozed', 'disabled', 'missing', 'programming'])
def test_wake_revival_respects_pause_disabled_missing_and_permanent_fault(monkeypatch, blocked):
    audio, app, capture, changes, now, _, _ = rig(monkeypatch)
    try:
        app._wake_consumer.running = False
        app.wake_word_active = False
        if blocked == 'snoozed': app.snoozed = True
        if blocked == 'disabled': app.config['wake_word_enabled'] = False
        if blocked == 'missing': app._no_microphone = True
        if blocked == 'programming': app._hands_free_fault = fault_from(TypeError('bug'), at=0)
        changes.request_wake()
        changes.tick()
        app.restart_hands_free.assert_not_called()
    finally:
        capture.stop()


def test_refresh_exceptions_have_three_attempt_cap(monkeypatch):
    audio, app, capture, changes, now, _, _ = rig(monkeypatch)
    try:
        refresh = Mock(side_effect=OSError('service unavailable'))
        monkeypatch.setattr(recovery, 'refresh', refresh)
        changes.request_input()
        for moment in (0, 5, 10, 15, 60):
            now[0] = moment
            changes.tick()
        assert refresh.call_count == 3
        assert not changes._input_pending
    finally:
        capture.stop()


def test_default_id_uses_windows_capture_endpoint(monkeypatch):
    endpoint = SimpleNamespace(GetId=lambda: 'new-input')
    monkeypatch.setitem(sys.modules, 'pycaw.pycaw', SimpleNamespace(
        AudioUtilities=SimpleNamespace(GetMicrophone=lambda: endpoint)))
    assert recovery.default_capture_id() == 'new-input'


def test_selected_microphone_switch_queues_wake_revival(monkeypatch):
    app = SimpleNamespace(config={'microphone': 0}, continuous_active=False,
        wake_word_active=False, recording=False, available_mics=[{'id': 1, 'name': 'Second'}],
        _config_lock=__import__('threading').RLock(), _ace_engine=None,
        _detect_capture_rate=lambda _: 16000, _run_calibration_if_auto=Mock(),
        persist_config=Mock(), get_current_microphone_name=lambda: 'Second',
        _update_tray_tooltip=Mock())
    app_method('switch_microphone')(app, 1)
    assert app.config['microphone_name'] == 'Second'
    assert recovery.changes_for(app)._wake_pending
