"""Exercise the Daily help receipt with the real command executor."""

from samsara.ui import context_help_qt
from tests.test_daily_receipts_1262 import probe
from tests.test_help_intents_943 import executor, loaded_registry, rig


def test_daily_help_receipt_accepts_the_current_wake_word_answer(
    probe, rig, tmp_path, monkeypatch
):
    _, app, _ = rig
    app._receipt_replay = None
    app.config = {
        "wake_word_enabled": False,
        "command_mode": {"enabled": False},
        "command_packs": {"window-management": True, "ai": True},
    }
    speak = app.audio_coordinator.speak
    opened = []
    monkeypatch.setattr(
        context_help_qt, "open_help_entry",
        lambda app, entry: opened.append((entry.kind, entry.id)),
    )

    result = probe.perform(app, tmp_path, "help")

    assert opened == [("term", "wake-word")]
    assert app.audio_coordinator.speak is speak
    assert [call.args[0] for call in speak.call_args_list] == [
        "Say “jarvis” to make Samsara listen for one command."
    ]
    assert result == {"command_completed": True, "correct_answer_to_speech": True}
