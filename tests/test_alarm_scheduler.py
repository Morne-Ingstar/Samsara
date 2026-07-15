from unittest.mock import Mock

import pytest

import samsara.alarms as alarms_module
from samsara.alarms import AlarmManager


class _DummyThread:
    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


@pytest.fixture
def alarm_manager(monkeypatch, tmp_path):
    monkeypatch.setattr(AlarmManager, "_ensure_alarm_sounds", lambda self: None)
    monkeypatch.setattr(AlarmManager, "_load_sound_cache", lambda self: None)
    config = {
        "alarms": {
            "enabled": True,
            "nag_interval_seconds": 60,
            "items": [
                {
                    "id": "water",
                    "name": "Water",
                    "enabled": True,
                    "interval_minutes": 10,
                    "sound": "alarm",
                },
                {
                    "id": "stretch",
                    "name": "Stretch",
                    "enabled": True,
                    "interval_minutes": 10,
                    "sound": "alarm",
                },
            ],
        }
    }
    saves = Mock()
    manager = AlarmManager(tmp_path, tmp_path, lambda: config, saves)
    return manager, config, saves


def test_start_anchors_enabled_alarms_instead_of_firing_immediately(
    alarm_manager, monkeypatch
):
    manager, _config, _saves = alarm_manager
    now = [1000.0]
    monkeypatch.setattr(alarms_module.time, "time", lambda: now[0])
    monkeypatch.setattr(
        alarms_module.thread_registry,
        "spawn",
        lambda *args, **kwargs: _DummyThread(),
    )
    trigger = Mock()
    monkeypatch.setattr(manager, "_trigger_alarm", trigger)

    manager.start()
    assert manager.last_triggered == {"water": 1000.0, "stretch": 1000.0}
    assert manager.get_next_trigger_at("water") == 1600.0

    now[0] = 1599.0
    manager._check_alarms()
    trigger.assert_not_called()

    now[0] = 1600.0
    manager._check_alarms()
    trigger.assert_called_once_with(manager.get_alarm("water"))


def test_timer_is_reanchored_on_add_enable_interval_edit_and_reset(
    alarm_manager, monkeypatch
):
    manager, _config, _saves = alarm_manager
    now = [100.0]
    monkeypatch.setattr(alarms_module.time, "time", lambda: now[0])
    manager.running = True
    manager._started_at = now[0]

    assert manager.update_alarm("water", interval_minutes=20)
    assert manager.last_triggered["water"] == 100.0

    now[0] = 110.0
    manager.reset_alarm_timer("water")
    assert manager.last_triggered["water"] == 110.0

    assert manager.toggle_alarm("water") is False
    assert "water" not in manager.last_triggered
    now[0] = 120.0
    assert manager.toggle_alarm("water") is True
    assert manager.last_triggered["water"] == 120.0

    now[0] = 130.0
    added = manager.add_alarm("Posture", interval_minutes=5)
    assert manager.last_triggered[added["id"]] == 130.0

def test_global_reenable_reanchors_all_enabled_alarms(alarm_manager, monkeypatch):
    manager, _config, _saves = alarm_manager
    now = [200.0]
    monkeypatch.setattr(alarms_module.time, "time", lambda: now[0])
    manager.running = True
    manager.last_triggered = {"water": 10.0, "stretch": 20.0}

    manager.set_global_enabled(False)
    now[0] = 300.0
    manager.set_global_enabled(True)

    assert manager.last_triggered["water"] == 300.0
    assert manager.last_triggered["stretch"] == 300.0




def test_only_one_due_alarm_is_released_per_scan(alarm_manager, monkeypatch):
    manager, _config, _saves = alarm_manager
    monkeypatch.setattr(alarms_module.time, "time", lambda: 1000.0)
    manager.running = True
    manager.last_triggered = {"water": 0.0, "stretch": 0.0}
    trigger = Mock()
    monkeypatch.setattr(manager, "_trigger_alarm", trigger)

    manager._check_alarms()

    trigger.assert_called_once_with(manager.get_alarm("water"))
    assert manager.last_triggered["water"] == 1000.0
    assert manager.last_triggered["stretch"] == 0.0


def test_next_trigger_api_reports_paused_active_and_overdue(
    alarm_manager, monkeypatch
):
    manager, config, _saves = alarm_manager
    now = [1200.0]
    monkeypatch.setattr(alarms_module.time, "time", lambda: now[0])
    manager.running = True
    manager._started_at = 1000.0
    manager.last_triggered["water"] = 1000.0

    assert manager.get_next_trigger_at("water") == 1600.0
    manager.nagging_alarm_id = "water"
    assert manager.get_next_trigger_at("water") == 1200.0
    manager.nagging_alarm_id = None

    manager.last_triggered["water"] = 0.0
    assert manager.get_next_trigger_at("water") == 600.0
    config["alarms"]["enabled"] = False
    assert manager.get_next_trigger_at("water") is None
    config["alarms"]["enabled"] = True
    manager.running = False
    assert manager.get_next_trigger_at("water") is None


def test_visual_callback_failure_does_not_cancel_sound_nag(
    alarm_manager, monkeypatch
):
    manager, _config, _saves = alarm_manager
    monkeypatch.setattr(
        alarms_module.thread_registry,
        "spawn",
        lambda *args, **kwargs: _DummyThread(),
    )

    def fail_visual(_alarm):
        raise RuntimeError("Qt unavailable")

    manager.on_alarm_triggered = fail_visual
    manager._trigger_alarm(manager.get_alarm("water"))

    assert manager.nagging_alarm_id == "water"
    assert manager.nag_thread is not None
