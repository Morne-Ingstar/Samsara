"""521: optional diagnostic recording never gates Ava's conversation memory."""
import json
import threading
from types import SimpleNamespace

import pytest

from plugins.commands import ask_ollama, comfy, dispatch_voice
from samsara import ava_local_context
from samsara.ava_memory import AvaMemory, PersonalMemory, log_conversation
from samsara.config_defaults import DEFAULTS
from samsara.config_schema import SETTINGS_SCHEMA


@pytest.fixture
def stub_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    monkeypatch.setattr(ask_ollama, "_turn_local", threading.local())
    monkeypatch.setattr(dispatch_voice, "route_ava_utterance", lambda *_a: False)
    monkeypatch.setattr(comfy, "try_voice_command", lambda *_a: None)
    for name in ("_handle_persona_intent", "_handle_working_context_intent",
                 "_handle_personal_memory_intent", "_check_teaching_intent",
                 "_disabled_since_request"):
        monkeypatch.setattr(ask_ollama, name, lambda *_a: False)
    monkeypatch.setattr(ask_ollama, "catalog_question_goal", lambda *_a: None)
    monkeypatch.setattr(ask_ollama.thread_registry, "spawn", lambda _name, fn, **_kw: fn())
    monkeypatch.setattr(ask_ollama.execution_policy, "is_current", lambda *_a: True)
    monkeypatch.setattr(ask_ollama, "_check_ollama_available", lambda *_a, **_kw: True)
    monkeypatch.setattr(ask_ollama, "_apply_communication_preferences", lambda s, _a: s)
    monkeypatch.setattr(ask_ollama.ava_profile, "build_context_section", lambda *_a: "")
    monkeypatch.setattr(ask_ollama.ava_corrections, "build_context_section", lambda **_kw: "")
    monkeypatch.setattr(ask_ollama, "_ai_state", lambda app: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity=app.route)))
    monkeypatch.setattr(ava_local_context, "local_clock_line", lambda: "Local clock facts: synthetic time")
    sent = []
    def answer(system, *_a, messages=None, **_kw):
        sent.append((system, list(messages)))
        return "A synthetic reply."
    monkeypatch.setattr(ask_ollama.cloud_llm, "send", answer)
    def local_post(_url, **kw):
        messages = kw["json"]["messages"]
        sent.append((messages[0]["content"], list(messages)))
        return SimpleNamespace(raise_for_status=lambda: None)
    monkeypatch.setattr(ask_ollama.ai_transport, "post_local", local_post)
    monkeypatch.setattr(ask_ollama.ava_readiness, "ollama_response_content",
                        lambda _r: "A synthetic reply.")
    def deliver(_app, text, **_kw):
        ask_ollama._turn_local.spoken_text.append(text)
        return None
    monkeypatch.setattr(ask_ollama, "handle_response", deliver)
    monkeypatch.setattr(ask_ollama, "_outcome_chip", lambda *_a: None)
    monkeypatch.setattr(ask_ollama, "_answered_chip", lambda: ("answered", "success"))
    monkeypatch.setattr(ask_ollama, "speak", lambda *_a: None)
    return sent


@pytest.mark.parametrize("route", ["ollama", "deepseek"])
@pytest.mark.parametrize("mode", ["last", "assistant"])
def test_full_two_turn_memory_is_identical_with_recording_off(route, mode, stub_turn, tmp_path):
    personal = PersonalMemory()
    assert personal.remember("I like cedar kites")
    results = []
    path = tmp_path / "logs" / "ava_conversation.log"
    for enabled in (None, True, None, False):
        ava = {"provider_policy": "local" if route == "ollama" else "cloud",
               "consent": {"memory_cloud_accepted": True}}
        if enabled is not None:
            ava["conversation_log_enabled"] = enabled
        (tmp_path / "history.json").unlink(missing_ok=True)
        app = SimpleNamespace(route=route, config={"ava": ava,
            "ava_memory": {"mode": mode}, "ollama": {},
            "cloud_llm": {"enabled": route != "ollama", "provider": route}},
            _ava_memory=AvaMemory(persist_path=str(tmp_path / "history.json")))
        app._ava_memory._mode = mode
        before = path.read_bytes() if path.exists() else None
        start = len(stub_turn)
        done = []
        for question in ("Tell me about cedar kites", "What did I ask before?"):
            ask_ollama.handle_ask_ava(app, question, on_done=lambda: done.append(True), generation=0)
        assert done == [True, True]
        assert app._ava_turn_outcome == ("answered", "success")
        messages = app._ava_memory.get_messages()
        assert len(messages) == 4
        assert messages[0]["content"] == "Tell me about cedar kites"
        assert messages[1]["content"] == "A synthetic reply."
        assert messages[0] in stub_turn[-1][1] and messages[1] in stub_turn[-1][1]
        if mode == "assistant":
            assert "cedar kites" in stub_turn[-1][0]
        results.append((messages, json.loads((tmp_path / "history.json").read_text()),
                        stub_turn[start:], personal.about()))
        if enabled is True:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            assert len(rows) == 2
            assert rows[0]["user"] == "Tell me about cedar kites"
            assert rows[0]["ava_spoken"] == rows[0]["ava_shown"] == "A synthetic reply."
            assert rows[0]["provider"] == route and rows[0]["action"] == "answered"
            if mode == "assistant":
                assert "cedar kites" in rows[0]["working_context"]
        else:
            assert (path.read_bytes() if path.exists() else None) == before
    assert results[0] == results[1] == results[2] == results[3]


@pytest.mark.parametrize("enabled", [False, None, "true", 1])
def test_writer_returns_before_resolving_any_path(enabled, monkeypatch):
    from samsara import ava_memory
    def forbidden():
        pytest.fail("disabled logging must not resolve or touch the log path")
    monkeypatch.setattr(ava_memory, "samsara_home_dir", forbidden)
    log_conversation(lane="model", provider="local", user_text="synthetic",
                     ava_spoken="reply", ava_shown="reply", action="answered", enabled=enabled)


def test_schema_default_is_off():
    assert SETTINGS_SCHEMA["ava.conversation_log_enabled"] == {
        "type": "bool", "default": False, "tab": "ava"}
    assert DEFAULTS["ava.conversation_log_enabled"] is False


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("width", [780, 1160])
def test_advanced_row_search_and_save_preserve_ava_siblings(qapp, monkeypatch, mode, width):
    from PySide6.QtCore import QPoint
    from samsara import autostart
    from samsara.ui import theme
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from samsara.ui.settings.calm_controls import SwitchSkin
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(autostart, "sync_run_entry", lambda *_a, **_kw: None)
    monkeypatch.setattr(ComponentsPage, "refresh_async", lambda _self: None)
    cfg = _schema_defaults()
    cfg["ava"].pop("conversation_log_enabled")
    cfg["ava"]["retained_sibling"] = "preserve"
    theme.set_theme(mode, refresh=False)
    window = _SettingsWindow(_StubApp(cfg))
    try:
        window.resize(width, 900)
        window._stack.setCurrentWidget(window._pages["Ava"])
        window._pages["Ava"].advanced.reveal()
        window.show()
        switch = window._widgets["ava_conversation_log_enabled"]
        row = window._rows_by_widget["ava_conversation_log_enabled"]
        assert not switch.isChecked()
        assert isinstance(switch._calm_switch, SwitchSkin)
        assert type(switch._calm_switch) is type(window._widgets["sc_enabled"]._calm_switch)
        assert switch.size() == window._widgets["sc_enabled"].size()
        assert row.disclosure is window._pages["Ava"].advanced
        for query in ("", "Keep a conversation log"):
            window._search_edit.setText(query)
            window._apply_search_filter()
            for _ in range(20):
                qapp.processEvents()
            assert not row.isHidden()
            title_y = row.heading.mapTo(row, QPoint()).y() + row.heading.height() / 2
            switch_y = switch.mapTo(row, QPoint()).y() + switch.height() / 2
            assert abs(title_y - switch_y) <= 1
            assert row.detail.mapTo(row, QPoint()).y() >= max(
                row.heading.mapTo(row, QPoint()).y() + row.heading.height(),
                switch.mapTo(row, QPoint()).y() + switch.height())
        assert "_build_ava_cloud_tab" in window._source_saves["ava"][0].__qualname__
        assert window._pages["Ava"].isAncestorOf(switch)
        for checked in (True, False):
            switch.setChecked(checked)
            updates = window._collect_destination_updates({})
            assert updates["ava"]["conversation_log_enabled"] is checked
            assert updates["ava"]["retained_sibling"] == "preserve"
        window._tool_name = "Integrations"
        assert "ava" not in window._collect_destination_updates({})
    finally:
        window.hide()
        window.deleteLater()
        theme.set_theme("dark", refresh=False)
