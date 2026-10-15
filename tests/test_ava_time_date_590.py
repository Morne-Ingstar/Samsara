"""590: PC time/date answers bypass providers and keep speech/captions together."""

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QLocale

from plugins.commands import ask_ollama as ava
from samsara import ava_local_context as local
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import ava_captions_qt as captions


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class Clock:
        @staticmethod
        def now():
            return datetime(2026, 10, 15, 15, 42)

    monkeypatch.setattr(local, "datetime", Clock)
    monkeypatch.setattr(QLocale, "system", lambda: QLocale("en_US"))


@pytest.mark.parametrize("words", sorted(set.union(*local._CLOCK_QUESTIONS.values())) + [
    "  WHAT   TIME IS IT?! ", "What’s today’s date?", "What's the time.",
])
@pytest.mark.parametrize("policy", ["off", "local", "cloud"])
def test_local_answer_before_ai_gate_with_no_transport(words, policy, monkeypatch):
    app = SimpleNamespace(config={"ava": {"provider_policy": policy}})
    spoken, outcomes = [], []
    done = Mock()
    monkeypatch.setattr(ava, "speak", lambda _app, text: spoken.append(text) or True)
    monkeypatch.setattr(ava, "_log_turn", lambda *_a, **kw: outcomes.append(kw["outcome"]))
    for name in ("_ai_state", "ask_model", "ask_ollama", "_check_ollama_available"):
        monkeypatch.setattr(ava, name, Mock(side_effect=AssertionError(name)))
    for name in ("post_local", "post_cloud"):
        monkeypatch.setattr(ava.ai_transport, name, Mock(side_effect=AssertionError(name)))
    ava.handle_ask_ava(app, words, on_done=done)
    assert spoken == [local.clock_answer(words)]
    assert outcomes == ["local_fast_path"]
    assert app._ava_turn_outcome[1] == "success"
    done.assert_called_once_with()


@pytest.mark.parametrize("locale_name,time,date", [
    ("en_US", "3:42 PM", "Thursday, October 15, 2026"),
    ("en_GB", "15:42", "Thursday, 15 October 2026"),
    ("de_DE", "15:42", "Donnerstag, 15. Oktober 2026"),
])
def test_pc_regional_formats(locale_name, time, date, monkeypatch):
    monkeypatch.setattr(QLocale, "system", lambda: QLocale(locale_name))
    assert local.clock_answer("what time is it") == f"It's {time}."
    assert local.clock_answer("what day is it") == f"It's {date}."


def test_clock_is_read_again_after_midnight():
    moments = iter([datetime(2026, 10, 15, 23, 59), datetime(2026, 10, 16, 0, 0)])
    assert local.clock_answer("what's the date", lambda: next(moments)) == (
        "It's Thursday, October 15, 2026.")
    assert local.clock_answer("what's the date", lambda: next(moments)) == (
        "It's Friday, October 16, 2026.")
    assert local.clock_answer("what time is it", lambda: datetime(2026, 10, 16, 0, 0)) == (
        "It's 12:00 AM.")
    assert local.clock_answer("what time is it", lambda: datetime(2026, 10, 16, 12, 0)) == (
        "It's 12:00 PM.")


@pytest.mark.parametrize("words", [
    "what time is it in Tokyo", "what day is it tomorrow", "what time does the shop close",
    "what's the date of the meeting", "type what's the date", "insert date", "insert time",
    "open clock", "set a timer", "what's today's weather", "what time is it and open chrome",
])
def test_nonlocal_questions_and_commands_fall_through(words):
    assert local.clock_answer(words, lambda: pytest.fail("read clock for nonmatch")) is None


def test_no_catalogue_phrase_is_captured():
    catalogue = json.loads((Path(__file__).resolve().parents[1] / "commands_catalog.json").read_text(
        encoding="utf-8"))
    for command in catalogue["commands"]:
        for phrase in command["aliases"]:
            assert local.clock_answer(phrase) is None, phrase


@pytest.mark.parametrize("display", ["compact", "full"])
@pytest.mark.parametrize("muted", [False, True])
def test_real_delivery_speaks_and_captions_with_ai_off(display, muted, monkeypatch):
    app = SimpleNamespace(config={
        "ava": {"provider_policy": "off"}, "tts": {"feedback_level": "brief"},
        "ui": {"live_surface": {"enabled": display == "full"}},
        "accessibility": {"ava_captions": True, "ava_mute_audio": muted},
    })
    engine = Mock()
    engine.get_engine_state.return_value = "idle"
    engine.speak.return_value = SpeechHandle(utterance_id="clock-590")
    app.audio_coordinator = AudioCoordinator(app, engine, config={})
    # No audio hardware, UI windows, interrupt monitor or live app.
    monkeypatch.setattr(app.audio_coordinator, "transition_to", Mock())
    show = Mock()
    monkeypatch.setattr(captions, "show_caption", show)
    monkeypatch.setattr(ava, "_log_turn", Mock())
    try:
        ava.handle_ask_ava(app, "what time is it")
        show.assert_called_once_with(app, "It's 3:42 PM.", hold=not muted)
        if muted:
            engine.speak.assert_not_called()
        else:
            assert engine.speak.call_args.args[0] == "It's 3:42 PM."
            assert engine.speak.call_args.kwargs["category"] == "ava_response"
    finally:
        app.audio_coordinator.shutdown()


def test_stale_request_does_not_answer(monkeypatch):
    app = SimpleNamespace(config={}, _ava_cmd_generation=2)
    speak = Mock()
    monkeypatch.setattr(ava, "speak", speak)
    monkeypatch.setattr(ava.execution_policy, "is_current", lambda _app, generation: generation == 2)
    ava.handle_ask_ava(app, "what time is it", generation=1)
    speak.assert_not_called()
