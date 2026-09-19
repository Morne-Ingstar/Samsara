"""Queue 251: truthful, local, bounded Ava familiarity."""
import os
import tempfile
import types
from pathlib import Path


# The profile modules resolve their paths while importing. Isolate this entire
# test process before importing them; never read or write the owner's profile.
os.environ["SAMSARA_HOME_DIR"] = tempfile.mkdtemp(prefix="samsara-251-")

from plugins.commands import ask_ollama  # noqa: E402
from samsara import ava_corrections, ava_profile  # noqa: E402


class _Speech:
    def __init__(self):
        self.lines = []

    def speak(self, text, **_kwargs):
        self.lines.append(text)
        return types.SimpleNamespace(utterance_id="spoken")


def _app():
    return types.SimpleNamespace(config={"ollama": {}}, audio_coordinator=_Speech())


def _reset_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(ava_profile, "_PROFILE_PATH", str(tmp_path / "ava_profile.json"))
    ava_profile._profile.clear()
    ava_profile._updated.clear()
    ava_profile._last_save_error = "the profile file could not be written"


def test_failed_profile_write_is_spoken_as_failure_without_saved_receipt(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    app = _app()
    monkeypatch.setattr(ava_profile, "_save_locked", lambda: False)
    monkeypatch.setattr(ava_profile, "_last_save_error", "the file is locked")

    assert ask_ollama._check_teaching_intent(app, "call me Matt") is True

    assert ava_profile.get("name") is None
    assert app.audio_coordinator.lines == ["I couldn't save that — the file is locked."]
    assert "saved" not in app.audio_coordinator.lines[0].lower()


def test_failed_alias_write_is_spoken_as_failure_and_rolls_back(monkeypatch):
    app = _app()
    monkeypatch.setattr(ava_corrections, "_aliases", {})
    monkeypatch.setattr(ava_corrections, "_save", lambda: False)
    monkeypatch.setattr(ava_corrections, "_last_save_error", "the file is locked")

    assert ask_ollama._check_teaching_intent(app, "remember shortcut means open settings") is True

    assert ava_corrections.get("shortcut") is None
    assert app.audio_coordinator.lines == ["I couldn't save that alias — the file is locked."]
    assert "saved" not in app.audio_coordinator.lines[0].lower()


def test_failed_alias_forget_is_spoken_as_failure_and_rolls_back(monkeypatch):
    app = _app()
    original = {"expansion": "open settings", "created": "now", "use_count": 0}
    monkeypatch.setattr(ava_corrections, "_aliases", {"shortcut": original})
    monkeypatch.setattr(ava_corrections, "_save", lambda allow_empty=False: False)
    monkeypatch.setattr(ava_corrections, "_last_save_error", "the file is locked")

    assert ask_ollama._check_teaching_intent(app, "forget shortcut") is True

    assert ava_corrections.get("shortcut") == original
    assert app.audio_coordinator.lines == ["I couldn't forget shortcut — the file is locked."]


def test_forget_removes_the_saved_field_and_gives_a_receipt(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    app = _app()
    assert ava_profile.set_field("name", "Matt") == ("set", "Matt")

    assert ask_ollama._check_teaching_intent(app, "forget my name") is True

    assert ava_profile.get("name") is None
    assert app.audio_coordinator.lines == ["I removed your preferred name: Matt."]


def test_what_do_you_remember_reads_saved_fields_and_offers_voice_forget(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    app = _app()
    assert ava_profile.set_field("name", "Matt")[0] == "set"
    assert ava_profile.set_field("interests", "soundtracks")[0] == "set"

    assert ask_ollama._check_teaching_intent(app, "what do you remember about me") is True

    answer = app.audio_coordinator.lines[-1].lower()
    assert "preferred name: matt" in answer
    assert "interests: soundtracks" in answer
    assert "say forget" in answer


def test_context_is_capped_and_keeps_name_then_preferences_then_recent_fields(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    for field, value in [
        ("interests", "soundtracks"),
        ("name", "Matt"),
        ("name_pronunciation", "mat"),
        ("answer_length", "short"),
        ("pace", "slow"),
    ]:
        assert ava_profile.set_field(field, value)[0] == "set"

    context = ava_profile.build_context_section(190)

    assert len(context) <= 190
    assert context.index("Preferred name") < context.index("Name pronunciation")
    assert context.index("Name pronunciation") < context.index("Answer length")
    assert context.index("Answer length") < context.index("Interests")


def test_alias_context_excludes_irrelevant_aliases_and_uses_the_prior_two_turns(monkeypatch):
    monkeypatch.setattr(ava_corrections, "_aliases", {
        "soundtracks": {"expansion": "film music", "use_count": 0},
        "blue moon": {"expansion": "the music project", "use_count": 0},
    })

    irrelevant = ava_corrections.build_context_section("tell me the weather", (), 600)
    relevant = ava_corrections.build_context_section("tell me the weather", ["I like blue moon"], 600)

    assert irrelevant == ""
    assert '"blue moon" means: the music project' in relevant
    assert "soundtracks" not in relevant


def test_short_answer_preference_changes_the_system_instructions(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    assert ava_profile.set_field("answer_length", "short")[0] == "set"

    prompt = ask_ollama._apply_communication_preferences("BASE PROMPT", _app())

    assert "BASE PROMPT" in prompt
    assert "one short sentence" in prompt


def test_cloud_provider_gets_no_profile_or_alias_context(tmp_path, monkeypatch):
    _reset_profile(tmp_path, monkeypatch)
    assert ava_profile.set_field("name", "Matt")[0] == "set"
    monkeypatch.setattr(ava_corrections, "_aliases", {
        "soundtracks": {"expansion": "film music", "use_count": 0},
    })
    seen = {}

    class _Memory:
        def add_user(self, _text):
            pass

        def get_messages(self, system, **_kwargs):
            seen["system"] = system
            return [{"role": "system", "content": system}]

        def add_assistant(self, _text):
            pass

        def save(self):
            pass

    app = types.SimpleNamespace(config={"cloud_llm": {"enabled": True}}, _ava_memory=_Memory())
    monkeypatch.setattr(ask_ollama.cloud_llm, "is_enabled", lambda _app: True)
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", lambda *_args, **_kwargs: "plain reply")
    monkeypatch.setattr(ask_ollama.ava_readiness, "configured_provider", lambda _app: "fake")

    reply = ask_ollama.ask_model(
        "tell me about soundtracks", app, system="{USER_PROFILE}\n{USER_ALIASES}")

    assert reply.text == "plain reply"
    assert "Matt" not in seen["system"]
    assert "soundtracks" not in seen["system"]


def test_persona_has_no_attachment_or_dependency_language():
    persona = ask_ollama.AVA_PERSONA_TEXT.lower()
    for banned in ("i need you", "don't leave", "i missed you", "only i"):
        assert banned not in persona
    assert "not a person" in persona
    assert "ordinary\ndictation" in persona
