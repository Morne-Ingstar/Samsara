"""Regression coverage for command read-backs that exceed acknowledgement limits."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugins.commands import health_tracker, reminders, tasks
from samsara.tts.coordinator import AudioCoordinator, SUPPRESSED_CMD_MODE_ID
from samsara.tts.engine_base import SpeechHandle


def _recording_app():
    return SimpleNamespace(audio_coordinator=MagicMock(), tts_engine=None)


def _command_mode_coordinator():
    app = MagicMock()
    app.command_mode_active = True
    app.config = {
        "tts": {},
        "command_mode": {"tts_char_limit": 10},
        "wake_word_config": {"audio": {"speech_threshold": 0.03}},
    }
    app.play_sound = MagicMock()
    engine = MagicMock()
    engine.get_engine_state.return_value = "idle"
    engine.speak.return_value = SpeechHandle(utterance_id="spoken")
    return AudioCoordinator(app, engine), engine


def test_three_item_task_list_is_spoken_in_full_as_a_readback(monkeypatch):
    app = _recording_app()
    active = [{"text": text} for text in ("call the doctor", "buy groceries", "take a break")]
    monkeypatch.setattr(tasks.tasks_store, "get_active", lambda: active)

    assert tasks.handle_read_tasks(app) is True

    text = app.audio_coordinator.speak.call_args.args[0]
    assert app.audio_coordinator.speak.call_args.kwargs["category"] == "readback"
    for task in active:
        assert task["text"] in text
    assert "shortened" not in text


def test_long_task_list_is_bounded_and_says_it_was_shortened(monkeypatch):
    app = _recording_app()
    active = [{"text": f"task {number}"} for number in range(1, 41)]
    monkeypatch.setattr(tasks.tasks_store, "get_active", lambda: active)

    assert tasks.handle_read_tasks(app) is True

    text = app.audio_coordinator.speak.call_args.args[0]
    assert app.audio_coordinator.speak.call_args.kwargs["category"] == "readback"
    assert "You have 40 tasks." in text
    assert "task 5" in text
    assert "task 6" not in text
    assert "shortened list; 35 more tasks were not read" in text


def test_reminder_and_health_readbacks_use_the_readback_category(monkeypatch):
    reminder_app = _recording_app()
    manager = MagicMock()
    manager.get_all_reminders.return_value = [
        {"name": "stretch", "enabled": True, "schedule": {"type": "interval", "minutes": 30}},
    ]
    reminder_app.notification_manager = manager
    assert reminders.handle_read_reminders(reminder_app) is True
    assert reminder_app.audio_coordinator.speak.call_args.kwargs["category"] == "readback"

    health_app = _recording_app()
    entry = {"type": "symptom", "data": {"text": "hands stiff"}, "timestamp": "2026-09-16T09:00:00+00:00"}
    monkeypatch.setattr(health_tracker.health_store, "get_today", lambda: [entry])
    assert health_tracker.handle_read_health(health_app) is True
    assert health_app.audio_coordinator.speak.call_args.kwargs["category"] == "readback"


def test_readback_bypasses_acknowledgement_limit_but_acknowledgement_stays_limited():
    coordinator, engine = _command_mode_coordinator()

    readback = coordinator.speak("A" * 80, category="readback")
    acknowledgement = coordinator.speak("A" * 80, category="agent_response")

    assert readback.utterance_id == "spoken"
    assert engine.speak.call_args.kwargs["category"] == "readback"
    assert acknowledgement.utterance_id == SUPPRESSED_CMD_MODE_ID
