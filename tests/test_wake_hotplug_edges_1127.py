"""1127: late lifecycle findings: actual wake-session state, boot and unplug."""
from unittest.mock import Mock
import pytest
from samsara import microphone_recovery as recovery
from samsara.audio_engine import engine as engine_module
from samsara.audio_engine.wake_consumer import fault_from
from tests.test_wake_device_restart_1127 import rig, event


def test_unplug_then_return_reopens_and_revives_without_restart_loop(monkeypatch):
    workers = []
    monkeypatch.setattr(engine_module.thread_registry, 'spawn', lambda *args, **kw: workers.append(args))
    audio, app, capture, changes, now, default_id, endpoints = rig(monkeypatch)
    try:
        app._wake_consumer.running = False
        app.wake_word_active = False
        app._hands_free_fault = fault_from(OSError('device'), at=0)
        audio.pending = []
        audio.default.device = (-1, -1)
        default_id[0], endpoints[0] = None, frozenset()
        event(changes, now)
        assert capture.device_lost and app._no_microphone
        app.restart_hands_free.assert_not_called()
        audio.pending = [dict(name='Second', hostapi=0, max_input_channels=1, default_samplerate=16000)]
        audio.default.device = (0, -1)
        default_id[0], endpoints[0] = 'second', frozenset({('second', 'Second')})
        def restart(**kw):
            app._wake_consumer.running = app.wake_word_active = True
            app._hands_free_fault = None
        app.restart_hands_free.side_effect = restart
        event(changes, now)
        assert not capture.device_lost and not app._no_microphone
        app.restart_hands_free.assert_called_once_with(source='device-change')
        for _ in range(20):
            now[0] += 5
            changes.tick()
        assert app.restart_hands_free.call_count == 1
        assert len(workers) == 1
    finally:
        capture.stop()


def test_loading_does_not_consume_restart_attempts(monkeypatch):
    audio, app, capture, changes, now, _, _ = rig(monkeypatch)
    try:
        app._wake_consumer.running = False
        app._wake_models_state = 'loading'
        changes.request_wake()
        changes.tick()
        app.restart_hands_free.assert_not_called()
        assert changes._wake_attempts == 0
        app._wake_models_state = 'ready'
        now[0] += 1
        changes.tick()
        assert app.restart_hands_free.call_count == 1
    finally:
        capture.stop()


def test_explicit_runtime_off_stays_off_on_device_change(monkeypatch):
    audio, app, capture, changes, now, default_id, _ = rig(monkeypatch)
    try:
        app.wake_word_active = app._wake_consumer.running = False
        app._wake_consumer_reasons = set()
        default_id[0] = 'second'
        event(changes, now)
        app.restart_hands_free.assert_not_called()
    finally:
        capture.stop()


def test_live_consumer_stale_fault_clears_through_existing_restart(monkeypatch):
    audio, app, capture, changes, now, _, _ = rig(monkeypatch)
    try:
        app._hands_free_fault = fault_from(OSError('old-device'), at=0)
        changes.request_wake()
        changes.tick()
        app.restart_hands_free.assert_called_once_with(source='device-change')
    finally:
        capture.stop()
