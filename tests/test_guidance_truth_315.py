"""Regressions for the source claims in audit 307's claims.csv."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QLabel

from samsara.onboarding.guidance_facts import facts_from_config
from samsara.ui import ava_guide_qt, tutorial_qt


def _dispose_window(window, qapp):
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    from shiboken6 import isValid
    assert not isValid(window)


@pytest.mark.parametrize(
    ("dictation_mode", "session_mode", "expected"),
    [
        ("hold", "toggle", "hands_free_session"),
        ("toggle", "hold", "one_command_window"),
    ],
)
def test_C0340_wake_session_follows_command_mode(dictation_mode, session_mode, expected):
    facts = facts_from_config({
        "mode": dictation_mode,
        "command_mode": {"mode": session_mode},
        "wake_word_config": {"opens_session": True},
    })
    assert facts.wake_activation == expected


def test_C0342_C0343_explicit_empty_termination_lists_stay_empty():
    facts = facts_from_config({"wake_word_config": {"send_words": [], "end_words": []}})
    assert facts.send_words == ()
    assert facts.end_words == ()


def test_C0349_invalid_multiword_commit_uses_runtime_default():
    facts = facts_from_config({"command_mode": {"dictate_commit_word": "finish now"}})
    assert facts.session_end.commit_word == "finish"


def test_C0338_selectable_wake_phrases_are_not_active_aliases():
    facts = facts_from_config({"wake_word_config": {
        "phrase": "jarvis", "phrase_options": ["jarvis", "alexa"],
    }})
    assert "alexa" not in facts.wake_aliases
    assert "alexa" not in facts.wake_phrases


@pytest.fixture
def ava_guide(qapp):
    app = SimpleNamespace(config={
        "ollama": {"model": "qwen2.5:3b"},
        "ava": {"provider_policy": "off"},
    })
    guide = ava_guide_qt._WizardWindow(app)
    try:
        yield guide
    finally:
        _dispose_window(guide, qapp)


def test_C0166_selected_model_requires_exact_installed_tag(ava_guide):
    ava_guide._on_ollama_status(True, ["qwen2.5:7b"])
    ava_guide._selected_model_name = "qwen2.5:3b"
    ava_guide._refresh_model_status()
    assert not ava_guide._model_status_lbl.text().startswith("Installed:")


def test_C0140_C0157_C0174_C0175_C0176_done_uses_observed_readiness(ava_guide, monkeypatch):
    monkeypatch.setattr(ava_guide_qt, "_ping_ollama", lambda **_kwargs: (False, []))
    monkeypatch.setattr(ava_guide_qt.thread_registry, "spawn", lambda _name, function, daemon: function())
    ava_guide._go_to(ava_guide._STEP_DONE)
    assert "not reachable" in ava_guide._done_summary.text().lower()
    assert "not installed" in ava_guide._done_summary.text().lower()
    assert "ready" not in ava_guide._title_lbl.text().lower()
    assert ava_guide._next_btn.text() == "Set up later"


def test_C0226_C2509_tutorial_glyphs_follow_practice_on_done(qapp):
    window = tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    try:
        # Queue 378 inserts a passive overview before done; locate done by name.
        window._step = next(i for i, (key, _) in enumerate(window._steps) if key == "done")
        window._show_step()
        dictation = window.findChild(QLabel, "tutorialDoneCheck1")
        command = window.findChild(QLabel, "tutorialDoneCheck2")
        assert dictation.text() != "✓"
        assert command.text() != "✓"
        window._completed.add(2)
        window._show_step()
        assert dictation.text() != "✓"
        assert command.text() == "✓"
    finally:
        _dispose_window(window, qapp)


def test_C0148_C0154_setup_copy_does_not_promise_automatic_service_or_forever(ava_guide):
    copy = " ".join(label.text() for label in ava_guide.findChildren(QLabel))
    assert "background service" not in copy
    assert "connects to it automatically" not in copy
    assert "offline forever" not in copy


def test_C0179_C0181_C0185_C0187_C0188_C0190_C0192_ava_copy_has_no_false_guarantees():
    intro = ava_guide_qt._ava_intro_text({"cloud_llm": {"enabled": True}})
    confirmation = ava_guide_qt._confirmation_text()
    warmth = ava_guide_qt._keep_warm_note({"ava_command_session": {"keep_warm": False}})
    usage = " ".join(ava_guide_qt._usage_lines({}, "jarvis"))
    assert "Cloud mode is ON" not in intro
    assert "ava local" not in intro.lower()
    assert "Anything that changes something" not in confirmation
    assert "not offered to Ava at all" not in confirmation
    assert "frees it afterwards" not in warmth
    assert "turns cloud mode off" not in usage


def test_C0198_C0201_C0203_C0204_C0205_tutorial_routes_are_qualified(monkeypatch):
    monkeypatch.setattr(tutorial_qt, "_catalog_phrase", lambda _records, _identity: "what can i say")
    instruction = tutorial_qt._command_instruction(
        ["scroll down"], "ctrl+alt+c", "jarvis", True, opens_session=True,
    )
    assert '"jarvis, scroll down"' not in instruction
    assert "after" in instruction.lower()
    assert "anytime" not in tutorial_qt._more_text([])
    pointer = tutorial_qt._pointer_text([])
    assert "on-device" not in pointer
    assert "click anything" not in pointer


def test_C0207_C0212_continuous_and_session_text_follow_config():
    instruction = tutorial_qt._dictation_instruction(
        "ctrl+shift", "continuous", continuous_commit_trigger="key",
    )
    assert "Each pause is transcribed" not in instruction
    session = tutorial_qt._hands_free_text({"command_mode": {"enabled": True, "mode": "hold"}})
    assert "hold" in session.lower() and "tap" not in session.lower()


def test_C0222_dictation_practice_does_not_promise_every_app(qapp):
    window = tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    try:
        copy = " ".join(label.text() for label in window._dict_success.findChildren(QLabel))
        assert "in any app" not in copy
    finally:
        _dispose_window(window, qapp)


def test_C0175_existing_model_choice_is_selected(qapp):
    guide = ava_guide_qt._WizardWindow(SimpleNamespace(config={
        "ollama": {"model": "llama3.2"},
    }))
    try:
        assert guide._selected_model_name == "llama3.2"
        assert guide._model_combo.currentData() == "llama3.2"
    finally:
        _dispose_window(guide, qapp)


def test_C0159_activation_button_names_ava(ava_guide, monkeypatch):
    ava_guide._app.config = ava_guide_qt.ai_preferences.apply_ava_route_choice(
        ava_guide._app.config, policy="local")
    ava_guide._ollama_running = True
    ava_guide._installed_models = [ava_guide._selected_model_name]
    ava_guide._current_step = ava_guide._STEP_DONE
    ava_guide._build_done_summary()
    assert ava_guide._consent_only_missing
    assert ava_guide._next_btn.text() == "Enable Ava"
    activate = Mock()
    monkeypatch.setattr(ava_guide, "_enable_ai_pack", activate)
    ava_guide._next_btn.click()
    activate.assert_called_once_with()


def test_C0176_model_presence_without_consent_is_not_readiness(ava_guide, monkeypatch):
    monkeypatch.setattr(ava_guide_qt, "_ping_ollama", lambda **_kwargs: (True, ["qwen2.5:3b"]))
    monkeypatch.setattr(ava_guide_qt.thread_registry, "spawn", lambda _name, function, daemon: function())
    ava_guide._go_to(ava_guide._STEP_DONE)
    assert "installed" in ava_guide._done_summary.text().lower()
    assert ava_guide._done_title.text() != "Ava is ready."


def test_C0176_custom_host_does_not_inherit_loopback_evidence(ava_guide, monkeypatch):
    ava_guide._app.config["ollama"]["host"] = "http://remote.example:11434"
    ava_guide._ollama_running = True
    ava_guide._installed_models = ["qwen2.5:3b"]
    observed = {}

    def capture_state(*_args, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(allowed=False)

    monkeypatch.setattr(ava_guide_qt.ai_preferences, "effective_state", capture_state)
    ava_guide._build_done_summary()
    assert observed["resolved_addresses"] == ()
    assert ava_guide._done_title.text() == "Ava is not ready."


def test_C0175_selected_model_is_not_ready_before_becoming_configured(ava_guide, monkeypatch):
    ava_guide._selected_model_name = "llama3.2"
    ava_guide._ollama_running = True
    ava_guide._installed_models = ["llama3.2"]
    monkeypatch.setattr(
        ava_guide_qt.ai_preferences, "effective_state",
        lambda *_args, **_kwargs: SimpleNamespace(allowed=True),
    )
    ava_guide._build_done_summary()
    assert ava_guide._done_title.text() == "Ava is not ready."


def test_C0177_C0178_consent_does_not_claim_readiness(ava_guide, monkeypatch):
    request = Mock(return_value={})
    apply = Mock()
    monkeypatch.setattr(ava_guide_qt.ava_consent_qt, "request_consent", request)
    monkeypatch.setattr(ava_guide_qt.ava_consent_qt, "apply_accepted_ava", apply)
    ava_guide._ollama_running = False
    ava_guide._enable_ai_pack()
    request.assert_not_called()
    apply.assert_not_called()
    assert ava_guide._next_btn.text() == "Set up later"
    assert ava_guide._done_title.text() == "Ava is not ready."

    # Exercise consent itself, rather than returning early on missing setup.
    ava_guide._app.config = ava_guide_qt.ai_preferences.apply_ava_route_choice(
        ava_guide._app.config, policy="local")
    ava_guide._ollama_running = True
    ava_guide._installed_models = [ava_guide._selected_model_name]
    ava_guide._enable_ai_pack()
    request.assert_called_once()
    apply.assert_called_once()
    assert ava_guide._next_btn.text() == "Enable Ava"
    assert ava_guide._done_title.text() == "Ava is not ready."

    request.reset_mock()
    apply.reset_mock()
    request.side_effect = RuntimeError("unavailable")
    ava_guide._enable_ai_pack()
    request.assert_called_once()
    apply.assert_not_called()
    assert ava_guide._next_btn.text() == "Enable Ava"
    assert ava_guide._done_title.text() == "Ava is not ready."


def test_C0216_C0225_skipped_tutorial_has_neutral_completion_heading(qapp):
    window = tutorial_qt.TutorialWindow(SimpleNamespace(config={}))
    try:
        # Queue 378 inserts a passive overview before done; locate done by name.
        window._step = next(i for i, (key, _) in enumerate(window._steps) if key == "done")
        window._show_step()
        assert window._title_lbl.text() == "Tutorial complete"
        assert "You know" not in " ".join(label.text() for label in window.findChildren(QLabel))
    finally:
        _dispose_window(window, qapp)
