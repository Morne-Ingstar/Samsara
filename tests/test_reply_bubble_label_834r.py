"""834r: caption attribution follows the producer, independent of Ava mode."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_agent, ask_ollama as ava
from samsara.agent_channel import claude_reply, hermes_voice
from samsara.tts.coordinator import AudioCoordinator
from samsara.tts.engine_base import SpeechHandle
from samsara.ui import ava_captions_qt as captions


@pytest.fixture
def rig(qapp, monkeypatch):
    app = SimpleNamespace(config={
        "ava": {"provider_policy": "off"},
        "tts": {"feedback_level": "full"},
        "accessibility": {"ava_captions": True, "ava_mute_audio": True},
    }, command_mode_active=False)
    engine = Mock()
    engine.get_engine_state.return_value = "idle"
    engine.speak.return_value = SpeechHandle(utterance_id="test-834r")
    app.audio_coordinator = AudioCoordinator(app, engine, config={})
    monkeypatch.setattr(app.audio_coordinator, "transition_to", Mock())
    monkeypatch.setattr(captions, "_post", lambda callback: callback())
    captions.reset_for_test()
    yield app
    app.audio_coordinator.shutdown()
    captions.reset_for_test()
    qapp.processEvents()


def speaker():
    return captions.active_window()._who.text()


def test_local_teach_result_with_ava_off(rig, monkeypatch):
    from samsara.ui import dictionary_panel_qt
    monkeypatch.setattr(dictionary_panel_qt, "refresh_dictionary", lambda app: None)
    store = SimpleNamespace(normalize_correction_source=lambda word: word,
                            add_correction=Mock(return_value=True),
                            add_vocab=Mock(return_value=True), custom_vocab=[])
    ava._persist_correction(rig, store, "example", "Example")
    assert speaker() == "SAMSARA"
    assert store.add_correction.called


def test_ava_answer_then_local_notice_relabels_same_panel(rig):
    ava._answer_advice_text_only(rig, "A model answer.")
    panel = captions.active_window()
    assert speaker() == "AVA"
    ava.speak_as(rig, "A local confirmation.", source="SAMSARA")
    assert captions.active_window() is panel
    assert speaker() == "SAMSARA"


@pytest.mark.parametrize("name,label", [("gpt", "GPT"), ("claude", "CLAUDE"), ("codex", "GPT")])
def test_agent_answer_and_local_failure(rig, monkeypatch, name, label):
    monkeypatch.setattr(ask_agent, "_thinking", lambda *args: None)
    monkeypatch.setattr(ask_agent.thread_registry, "spawn", lambda name, fn: fn())
    agent = Mock()
    agent.ask.return_value = SimpleNamespace(text="A model answer.")
    monkeypatch.setattr(ask_agent.cli_agents, "agent", lambda name: agent)
    monkeypatch.setattr(ask_agent.cli_agents, "first_reply",
                        lambda *a, **kw: SimpleNamespace(spoken="A model answer."))
    ask_agent.ask_cli(rig, name, "A sample question")
    assert speaker() == label
    assert rig._cli_agent_voice_state["source"] == name
    agent.ask.side_effect = TimeoutError()
    ask_agent.ask_cli(rig, name, "A sample question")
    assert speaker() == "SAMSARA"


def test_hermes_answer_and_local_notice(rig, monkeypatch):
    monkeypatch.setattr(hermes_voice, "capturing", lambda app: False)
    hermes_voice.play(rig, "A model answer.", result=True, source="HERMES")
    assert speaker() == "HERMES"
    hermes_voice.play(rig, "A local confirmation.", result=True)
    assert speaker() == "SAMSARA"


@pytest.mark.parametrize("name,label", [("gpt", "GPT"), ("claude", "CLAUDE"), ("hermes", "HERMES")])
def test_followup_retains_agent_source(rig, name, label):
    claude_reply._deliver(rig, "More of the model answer.", {"source": name}, cli=True)
    assert speaker() == label


@pytest.mark.parametrize("category", ["ava_response", "ava_status", "confirmation", "readback"])
def test_category_cannot_claim_ava_authorship(rig, category):
    rig.config["tts"]["feedback_level"] = "silent"
    rig.audio_coordinator.speak("Local output.", category=category)
    assert speaker() == "SAMSARA"


@pytest.mark.parametrize("source,label", [("AVA", "AVA"), ("GPT", "GPT"),
    ("CLAUDE", "CLAUDE"), ("HERMES", "HERMES"), ("SAMSARA", "SAMSARA"), ("chatgpt", "GPT")])
def test_panel_displays_explicit_source(rig, source, label):
    captions.show_caption(rig, "Example output.", source=source, hold=False)
    assert speaker() == label


def test_caption_facade_requires_source(rig):
    with pytest.raises(TypeError):
        captions.show_caption(rig, "Example output.")


def test_theme_proof_factory_supplies_source(qapp):
    from tests._theme_surfaces import _ava_captions
    panel = _ava_captions()
    try:
        assert panel._who.text() == "AVA"
    finally:
        panel.hide()
        panel.deleteLater()
        qapp.processEvents()


def test_ava_helper_defaults_to_ava(rig):
    ava.speak(rig, "A model answer.")
    assert speaker() == "AVA"


def test_legacy_coordinator_contract_and_callback():
    calls = []
    done = Mock()

    class Legacy:
        def speak(self, text, category=None, interruptible=None, on_done=None):
            calls.append((category, interruptible, on_done))
            return SpeechHandle(utterance_id="legacy")

    app = SimpleNamespace(config={}, audio_coordinator=Legacy())
    assert ava.speak(app, "A reply.", category="confirmation", on_done=done)
    assert calls == [("confirmation", True, done)]


def test_source_support_does_not_retry_internal_type_error():
    from samsara.tts.coordinator import speak_with_caption_source
    speak = Mock(side_effect=TypeError("engine failed"))
    with pytest.raises(TypeError, match="engine failed"):
        speak_with_caption_source(SimpleNamespace(speak=speak), "A reply.", source="AVA")
    speak.assert_called_once_with("A reply.", caption_source="AVA")


def test_local_source_preserves_two_argument_callback(monkeypatch):
    seen = []
    monkeypatch.setattr(ava, "speak", lambda app, text: seen.append((app, text)) or True)
    app = SimpleNamespace()
    assert ava.speak_as(app, "Local result.", "SAMSARA")
    assert seen == [(app, "Local result.")]


def test_scoped_source_resets_after_failure_and_nested_reply():
    from samsara.tts.coordinator import invoke_with_reply_source, reply_source
    seen = []
    def callback(app, text):
        seen.append(reply_source.get())
        invoke_with_reply_source(lambda app, text: seen.append(reply_source.get()),
                                 app, text, "GPT")
        seen.append(reply_source.get())
        raise RuntimeError("delivery failed")
    with pytest.raises(RuntimeError, match="delivery failed"):
        invoke_with_reply_source(callback, None, "Example output.", "SAMSARA")
    assert seen == ["SAMSARA", "GPT", "SAMSARA"]
    assert reply_source.get() == "AVA"
