"""Voice comfort commands: fake effects, production registry, no app import."""
from types import SimpleNamespace

import pytest

from samsara.config_schema import apply_feedback_level
from samsara.execution_policy import Route
from samsara.plugin_commands import load_plugins as _load_plugins


class App:
    def __init__(self, level="brief", volume=.5, setter=True):
        self.config = {"audio_feedback": False, "sound_volume": volume}
        apply_feedback_level(self.config, level)
        self.saved = 0
        self.chips, self.speech, self.sounds, self.sets = [], [], [], []
        self.audio_coordinator = SimpleNamespace(speak=lambda text, **kw: self.speech.append((text, kw)))
        if not setter:
            self.set_feedback_level = None

    def save_config(self):
        self.saved += 1

    def set_feedback_level(self, level):
        self.sets.append(level)
        apply_feedback_level(self.config, level)
        self.save_config()

    def _show_outcome_chip(self, text, *args):
        self.chips.append(text)

    def play_sound(self, key):
        self.sounds.append((key, self.config["sound_volume"]))


@pytest.mark.parametrize("setter", [True, False])
@pytest.mark.parametrize("handler,start,end,already", [
    ("handle_talk_less", "full", "brief", False),
    ("handle_talk_less", "brief", "silent", False),
    ("handle_talk_less", "silent", "silent", True),
    ("handle_talk_more", "silent", "brief", False),
    ("handle_talk_more", "brief", "full", False),
    ("handle_talk_more", "full", "full", True),
] + [("handle_feedback_" + level, "brief", level, level == "brief")
     for level in ("silent", "brief", "full")])
def test_feedback_changes(handler, start, end, already, setter):
    from plugins.commands import feedback_level as plugin
    app = App(start, setter=setter)
    assert getattr(plugin, handler)(app, "") is True
    assert app.config["tts"] == {"feedback_level": end, "enabled": end != "silent"}
    assert app.saved == 1
    assert app.sets == ([end] if setter else [])
    text = f"Feedback: {end.title()}" + (" (already)" if already else "")
    assert app.chips == [text]
    assert len(app.speech) == 1
    assert app.speech[0][0] == text
    assert app.speech[0][1]["category"] == "agent_response"
    assert app.config["audio_feedback"] is False


@pytest.mark.parametrize("handler,start,end,already", [
    ("handle_sounds_quieter", .7, .6, False),
    ("handle_sounds_quieter", .05, 0, False),
    ("handle_sounds_quieter", 0, 0, True),
    ("handle_sounds_louder", .7, .8, False),
    ("handle_sounds_louder", .95, 1, False),
    ("handle_sounds_louder", 1, 1, True),
    ("handle_sounds_quieter", .555, .455, False),
])
def test_sound_changes(handler, start, end, already):
    from plugins.commands import feedback_level as plugin
    app = App(volume=start)
    assert getattr(plugin, handler)(app, "") is True
    assert app.config["sound_volume"] == pytest.approx(end)
    assert app.saved == 1
    assert app.sounds == [("action_complete", app.config["sound_volume"])]
    text = f"Samsara sounds {int(app.config['sound_volume'] * 100)}%" + (" (already)" if already else "")
    assert app.chips == [text]
    assert len(app.speech) == 1 and app.speech[0][0] == text
    assert app.speech[0][1]["category"] == "agent_response"
    assert app.config["audio_feedback"] is False


@pytest.fixture
def executor(monkeypatch):
    from samsara import plugin_commands
    from tools.dump_command_metadata import build_executor
    # conftest clears the registry and suppresses production discovery per test.
    monkeypatch.setattr(plugin_commands, "load_plugins", _load_plugins)
    return build_executor()


PHRASES = {
    "talk less": ["less talking", "less feedback"],
    "talk more": ["more talking", "more feedback"],
    "feedback silent": [], "feedback brief": [], "feedback full": [],
    "sounds quieter": ["quieter sounds", "beeps quieter", "lower the sounds"],
    "sounds louder": ["louder sounds", "beeps louder", "raise the sounds"],
}


def test_phrase_ownership(executor):
    from samsara import command_catalog as cc
    claims = cc.raw_phrase_claims(executor)
    for canonical, aliases in PHRASES.items():
        for phrase in [canonical] + aliases:
            entry = executor._registry_entry(phrase)
            assert entry is not None and entry.phrase == canonical
            assert "feedback_level.py" in entry.handler.__code__.co_filename
            matched, remainder = executor._matcher.match(phrase)
            assert matched.phrase == canonical and remainder == ""
    assert not [c for c in cc.collisions(claims) if c[0] in {p for c, a in PHRASES.items() for p in [c] + a}]
    for canonical in ("volume up", "volume down"):
        entry = executor._registry_entry(canonical)
        for phrase in [canonical] + list(entry.aliases):
            assert executor._registry_entry(phrase).phrase == canonical
            assert executor._matcher.match(phrase)[0].phrase == canonical


REACH = [(text, "talk less") for text in (
    "talk less", "you talk too much", "stop talking so much", "reduce the audio feedback",
    "less audio feedback", "be less chatty", "too much talking")]
REACH += [(text, "talk more") for text in ("talk more", "give me more feedback")]
REACH += [(text, "sounds quieter") for text in (
    "make the beeps quieter", "the sounds are too loud", "turn down the feedback sounds")]
REACH += [("I can't hear the beeps", "sounds louder")]


@pytest.mark.parametrize("utterance,expected", REACH)
def test_ava_reach(executor, utterance, expected):
    assert expected in executor.ava_menu(utterance, limit=12, app=App(), route=Route.MODEL)


@pytest.mark.parametrize("wire", ["production", "json_stub"])
def test_resolver_proposal(executor, monkeypatch, wire):
    from samsara import ai_preferences, ava_readiness, ava_command_session as resolver
    from plugins.commands import ask_ollama
    app = App()
    app.command_executor = executor
    app.config["ava_command_session"] = {"interpret_misses": True}
    monkeypatch.setattr(ai_preferences, "runtime_state", lambda *args: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity="ollama")))
    monkeypatch.setattr(ava_readiness, "readiness_for", lambda app: SimpleNamespace(ready=True))
    monkeypatch.setattr(ask_ollama, "get_system_prompt", lambda app: "{COMMAND_LIST}")
    response = "CONFIRM I'll talk less.\nACTION talk less"
    if wire == "json_stub":
        import json
        response = '{"type":"action","command":"talk less"}'
        # The brief's JSON is the internal representation, not the real wire grammar.
        monkeypatch.setattr(ask_ollama, "_parse_structured_response", json.loads)
    monkeypatch.setattr(ask_ollama, "ask_ollama", lambda *args, **kwargs: response)
    resolver.reset_cancellations()
    proposal = resolver.resolve(app, "you talk too much", "374", 10**12)
    assert proposal is not None and proposal.command_id == "talk less"


@pytest.mark.parametrize("command", PHRASES)
def test_model_route_executes_without_confirmation(executor, command):
    from samsara.command_registry import DispatchState
    app = App()
    result = executor.execute_canonical(command, app, route=Route.MODEL, generation=0)
    assert result.state is DispatchState.COMPLETED
    assert app.saved == 1


@pytest.mark.parametrize("handler,start,end", [("handle_volume_up", .4, .6), ("handle_volume_down", .8, .6)])
@pytest.mark.parametrize("ok", [True, False])
def test_windows_volume_chip(monkeypatch, handler, start, end, ok):
    from plugins.commands import volume
    changes = []
    monkeypatch.setattr(volume, "_audio", SimpleNamespace(get_volume=lambda: start,
        set_volume=lambda value: changes.append(value) or ok))
    app = App()
    assert getattr(volume, handler)(app, "") is ok
    assert changes == [pytest.approx(end)]
    assert app.chips == (["Windows volume 60%"] if ok else [])
    assert not app.speech and not app.sounds


def test_catalog_has_commands():
    import json
    from pathlib import Path
    document = json.loads((Path(__file__).parents[1] / "commands_catalog.json").read_text(encoding="utf-8"))
    assert {"feedback_level." + phrase.replace(" ", "_") for phrase in PHRASES} <= {
        row["canonical_id"] for row in document["commands"]}
