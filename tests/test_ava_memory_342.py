"""342: explicit, owner-readable Ava memory (isolated from the live profile)."""

import json
from types import SimpleNamespace

import pytest

from samsara.ava_memory import PersonalMemory
from plugins.commands import ask_ollama


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    return tmp_path


def _app(home, mode="assistant"):
    return SimpleNamespace(
        config={"ava_memory": {"mode": mode}, "ollama": {"model": "local-test"}},
        config_path=str(home / "config.json"),
    )


def test_only_stated_facts_are_saved_and_existing_file_is_extended(home):
    path = home / "ava_memory.md"
    path.write_text("## About you\n- Call me Jo\n\n## Recent sessions\n\n"
                    "## Do not remember\n- secret project\n", encoding="utf-8")
    memory = PersonalMemory(path)
    assert not memory.remember("You probably like gardening")
    assert not memory.remember("remember that the sky is blue")
    assert memory.remember("I'm working on Samsara")
    assert memory.remember("I prefer short answers")
    assert memory.remember("I like gardening")
    assert not memory.remember("I'm working on secret project")
    assert memory.about() == ["Call me Jo", "I'm working on Samsara", "I prefer short answers", "I like gardening"]
    assert "secret project" in path.read_text(encoding="utf-8")


def test_sensitive_requires_explicit_remember_and_is_never_summarized(home):
    memory = PersonalMemory()
    assert not memory.remember("I prefer advice about my salary")
    assert memory.remember("remember that I have a medical diagnosis")
    assert "medical diagnosis" in memory.about()[0]
    assert memory.safe_for_summary("We discussed my medical diagnosis") == ""
    assert "medical diagnosis" in memory.prompt_block()
    assert "medical diagnosis" not in memory.prompt_block(cloud=True)


def test_forget_scrubs_facts_sessions_and_future_summaries(home):
    memory = PersonalMemory()
    assert memory.remember("I'm working on Samsara")
    assert memory.add_summary("We discussed Samsara and gardening", "2026-09-27")
    assert memory.forget("Samsara")
    assert memory.about() == []
    assert "Samsara" not in memory.prompt_block()
    assert not memory.add_summary("We discussed Samsara again")
    assert memory.safe_for_summary("The Samsara project") == ""
    assert "Samsara" in (home / "ava_memory.md").read_text(encoding="utf-8")  # exclusion remains


def test_owner_written_exclusion_takes_effect_on_next_read(home):
    memory = PersonalMemory()
    memory.remember("Call me Jo")
    with memory.path.open("a", encoding="utf-8") as handle:
        # Place an exclusion inside the existing section, as an owner can do.
        handle.write("- Jo\n")
    assert memory.about() == []
    assert "Call me Jo" not in memory.prompt_block()
    assert not memory.add_summary("We discussed Jo")


def test_owner_written_long_line_is_not_truncated_by_an_update(home):
    memory = PersonalMemory()
    memory.ensure_file()
    long_fact = "long owner note " + "x" * 600
    text = memory.path.read_text(encoding="utf-8")
    memory.path.write_text(text.replace("## About you\n", f"## About you\n- {long_fact}\n"),
                           encoding="utf-8")
    memory.remember("Call me Jo")
    assert long_fact in memory.path.read_text(encoding="utf-8")
    assert "Call me Jo" in memory.prompt_block()


def test_session_rotation_recall_limit_and_prompt_cap(home):
    memory = PersonalMemory(sessions_kept=3, recall_sessions=2)
    for day in range(1, 5):
        assert memory.add_summary(f"We discussed topic {day}", f"2026-09-{day:02d}")
    text = (home / "ava_memory.md").read_text(encoding="utf-8")
    assert "topic 1" not in text
    assert "topic 4" in text
    assert "topic 2" not in memory.prompt_block()
    assert "topic 3" in memory.prompt_block(utterance="topic 3")
    for n in range(40):
        memory.remember(f"remember that interest number {n} is a long personal preference")
    assert len(memory.prompt_block().encode("utf-8")) <= 1500


def test_atomic_replace_failure_preserves_existing_file(home, monkeypatch):
    memory = PersonalMemory()
    memory.remember("Call me Jo")
    original = memory.path.read_bytes()

    def fail_replace(*_args):
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr("samsara.ava_memory.os.replace", fail_replace)
    with pytest.raises(OSError):
        memory.remember("I prefer short answers")
    assert memory.path.read_bytes() == original


def test_clear_and_last_history_modes_keep_existing_contract(home):
    last = _app(home, "last")
    history = ask_ollama._build_ava_memory(last)
    history.add_user("one")
    history.save()
    assert (home / "ava_memory.json").exists()
    assert ask_ollama._build_ava_memory(last).turn_count() == 1
    clear = _app(home, "clear")
    assert ask_ollama._build_ava_memory(clear).turn_count() == 0
    assert not (home / "ava_memory.json").exists()
    assert not (home / "ava_memory.md").exists()


def test_legacy_history_migrates_once_without_overwriting_owner_text(home, monkeypatch):
    memory = PersonalMemory()
    memory.remember("Call me Jo")
    (home / "ava_memory.json").write_text(json.dumps([
        {"role": "user", "content": "I'm working on Samsara"},
        {"role": "assistant", "content": "How can I help?"},
    ]), encoding="utf-8")
    app = _app(home)
    app._ava_memory = ask_ollama._build_ava_memory(app)
    assert memory.about() == ["Call me Jo", "I'm working on Samsara"]

    class Response:
        def raise_for_status(self):
            pass

    calls = []
    monkeypatch.setattr(ask_ollama.ai_transport, "post_local",
                        lambda url, **kw: calls.append((url, kw)) or Response())
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content",
                        lambda _response: "We discussed the Samsara project.")
    ask_ollama._finish_ava_session(app, wait=True)
    assert len(calls) == 1
    assert calls[0][0].startswith("http://localhost:11434/")
    assert not (home / "ava_memory.json").exists()
    assert "Call me Jo" in memory.path.read_text(encoding="utf-8")
    assert "We discussed" in memory.path.read_text(encoding="utf-8")


def test_summary_input_and_recall_scrub_exclusions_and_sensitive_data(home, monkeypatch):
    memory = PersonalMemory()
    memory.ensure_file()
    memory.forget("private project")
    app = _app(home)
    app._ava_memory = ask_ollama._build_ava_memory(app)
    app._ava_memory.add_user("I discussed private project")
    app._ava_memory.add_user("I discussed my salary")
    app._ava_memory.add_user("I like gardening")
    seen = []

    class Response:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(ask_ollama.ai_transport, "post_local",
                        lambda url, **kw: seen.append(kw["json"]) or Response())
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content",
                        lambda _response: "We discussed gardening.")
    ask_ollama._finish_ava_session(app, wait=True)
    request = seen[0]["messages"][1]["content"]
    assert "gardening" in request
    assert "salary" not in request
    assert "private project" not in request
    assert "private project" not in memory.prompt_block()


def test_voice_read_forget_and_confirmed_erase_stay_local(home, monkeypatch):
    app = _app(home)
    said = []
    monkeypatch.setattr(ask_ollama, "speak", lambda _app, text: said.append(text))
    assert ask_ollama._handle_personal_memory_intent(app, "Call me Jo")
    assert ask_ollama._handle_personal_memory_intent(app, "what do you remember about me")
    assert "Call me Jo" in said[-1]
    assert ask_ollama._handle_personal_memory_intent(app, "forget Jo")
    assert PersonalMemory().about() == []
    assert ask_ollama._handle_personal_memory_intent(app, "remember that Jo is my name")
    assert PersonalMemory().about() == []  # forgotten exclusion wins
    assert ask_ollama._handle_personal_memory_intent(app, "forget everything")
    assert PersonalMemory().path.exists()
    assert ask_ollama._handle_personal_memory_intent(app, "yes, forget everything")
    assert "Jo" not in PersonalMemory().path.read_text(encoding="utf-8")


def test_cloud_recall_requires_memory_acknowledgement(home):
    app = _app(home)
    app.config.update({"ava": {"provider_policy": "cloud", "consent": {}},
                       "cloud_llm": {"enabled": True}})
    cloud = SimpleNamespace(provider=SimpleNamespace(identity="deepseek"))
    local = SimpleNamespace(provider=SimpleNamespace(identity="ollama"))
    assert not ask_ollama._assistant_recall_allowed(app, cloud)
    assert ask_ollama._assistant_recall_allowed(app, local)
    app.config["ava"]["consent"]["memory_cloud_accepted"] = True
    assert ask_ollama._assistant_recall_allowed(app, cloud)
    app.config["ava"]["provider_policy"] = "local"
    assert not ask_ollama._assistant_recall_allowed(app, cloud)


def test_forget_during_summary_cannot_restore_forgotten_fact(home, monkeypatch):
    app = _app(home)
    app._ava_memory = ask_ollama._build_ava_memory(app)
    app._ava_memory.add_user("I like gardening")
    monkeypatch.setattr(ask_ollama, "speak", lambda *_args: None)

    class Response:
        def raise_for_status(self):
            pass

    def post_local(*_args, **_kwargs):
        assert ask_ollama._handle_personal_memory_intent(app, "forget gardening")
        return Response()

    monkeypatch.setattr(ask_ollama.ai_transport, "post_local", post_local)
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content",
                        lambda _response: "We discussed gardening.")
    ask_ollama._finish_ava_session(app, wait=True)
    assert "Recent session:" not in PersonalMemory().prompt_block()


def test_settings_offers_assistant_mode_and_owner_file_controls(home, qapp, monkeypatch):
    from PySide6.QtWidgets import QPushButton, QMessageBox
    from samsara.ui import settings_qt
    from samsara.ui.settings import ava_cloud_qt
    from tests.test_settings_pages import _StubApp, _schema_defaults

    window = settings_qt._SettingsWindow(_StubApp(_schema_defaults()))
    try:
        combo = window._widgets["ava_memory_mode"]
        assert [combo.itemText(i) for i in range(combo.count())] == [
            "Clear on close", "Keep last session", "Ava remembers me"]
        combo.setCurrentText("Ava remembers me")
        saved = {}
        for save in window._save_fns:
            saved.update(save(saved))
        assert saved["ava_memory"]["mode"] == "assistant"

        buttons = {button.text(): button for button in window.findChildren(QPushButton)}
        monkeypatch.setattr(ava_cloud_qt.QDesktopServices, "openUrl", lambda _url: True)
        buttons["Open memory file"].click()
        assert (home / "ava_memory.md").exists()
        PersonalMemory().remember("Call me Jo")
        monkeypatch.setattr(ava_cloud_qt.QMessageBox, "question",
                            lambda *_args: QMessageBox.StandardButton.Yes)
        buttons["Erase memory"].click()
        assert PersonalMemory().about() == []
    finally:
        window.deleteLater()
