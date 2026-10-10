"""Plain-language evidence contracts for Home and Voice help (queue 308)."""

import ast
import re
import time
import types
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from samsara import ai_preferences, ava_readiness
from samsara.ui import home_signals, voice_help_qt


REPO = Path(__file__).resolve().parents[1]
COPY_KEY = re.compile(r"\b[a-z]+_[a-z_]+\b|\.[a-z_]+\b")
INTERNAL_NOUN = re.compile(r"\b(?:consumer|provider|readiness|vad)\b", re.IGNORECASE)
SETTINGS_CITATION = re.compile(
    r"Settings > ([A-Za-z /&-]+) > ([A-Za-z /&-]+)"
)
DEFAULT_MICS = object()


def _app(*, config=None, mics=DEFAULT_MICS, running=True, armed=True, models="ready",
         snoozed=False, fault=None):
    return types.SimpleNamespace(
        config=config if config is not None else {"wake_word_enabled": True},
        available_mics=[{"id": 1, "name": "Test mic"}] if mics is DEFAULT_MICS else mics,
        snoozed=snoozed,
        wake_word_active=armed,
        _wake_consumer=types.SimpleNamespace(running=running) if running is not None else None,
        _wake_start_pending=False,
        _hands_free_fault=fault,
        wake_ready_state=lambda: models,
    )


def _ava_config(*, policy="local", api_key="present"):
    """A consented Ava profile using the current policy-state contract."""
    config = {
        "ava": {"provider_policy": policy},
        "ava_command_session": {"enabled": True},
        "ollama": {"enabled": policy == "local", "host": "http://localhost:11434"},
        "cloud_llm": {"enabled": policy == "cloud", "provider": "deepseek", "api_key": api_key},
    }
    provider = ai_preferences.provider_for(
        config, policy, resolved_addresses=("127.0.0.1", "::1") if policy == "local" else ())
    config["ava"]["consent"] = {
        "version": ai_preferences.CONSENT_VERSION,
        "accepted_at": "2026-01-01T00:00:00+00:00",
        "features": ["ava"],
        "provider": provider.record(),
        "cloud_version": ai_preferences.CONSENT_VERSION if policy == "cloud" else None,
    }
    return config


def _copy_strings(value):
    """Yield constant portions of strings passed to CapabilityState/Hint evidence."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        yield value.value
    if isinstance(value, ast.JoinedStr):
        for part in value.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value


def _declared_settings_rows():
    rows = {}
    for path in (REPO / "samsara" / "ui" / "settings").glob("*_page.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in {"TITLE", "BASE", "ADVANCED"}:
                        values[target.id] = ast.literal_eval(node.value)
        page = values.get("TITLE")
        if page:
            rows[page] = {
                spec[0] for field in ("BASE", "ADVANCED")
                for spec in values.get(field, []) if spec and isinstance(spec[0], str)
            }
    return rows


def _all_state_copy(monkeypatch):
    samples = []
    samples.extend([
        home_signals.dictation_state(_app(snoozed=True)),
        home_signals.dictation_state(_app(mics=None)),
        home_signals.dictation_state(_app(mics=[])),
        home_signals.dictation_state(_app()),
    ])
    samples.extend([
        home_signals.hands_free_state(_app(config={"wake_word_enabled": False})),
        home_signals.hands_free_state(_app(fault=types.SimpleNamespace(
            reason="wake_word_enabled is false", evidence="consumer provider readiness VAD"))),
        home_signals.hands_free_state(_app(snoozed=True)),
        home_signals.hands_free_state(_app(mics=None)),
        home_signals.hands_free_state(_app(mics=[])),
        home_signals.hands_free_state(_app(models="loading")),
        home_signals.hands_free_state(_app(models="off")),
        home_signals.hands_free_state(_app(running=None)),
        home_signals.hands_free_state(_app(running=False)),
        home_signals.hands_free_state(_app(armed=False)),
        home_signals.hands_free_state(_app()),
    ])

    config = {"ollama": {"enabled": True}}
    for readiness_state in (ava_readiness.READY, ava_readiness.WARMING,
                            ava_readiness.OFFLINE, "unknown"):
        monkeypatch.setattr(
            ava_readiness, "readiness_for",
            lambda _app, state=readiness_state: types.SimpleNamespace(
                state=state, sentence=lambda: "raw readiness provider error"))
        samples.append(home_signals.ava_state(_app(config=config)))
    monkeypatch.setattr(
        ava_readiness, "readiness_for",
        lambda _app: (_ for _ in ()).throw(RuntimeError("provider readiness VAD")))
    samples.append(home_signals.ava_state(_app(config=config)))
    samples.append(home_signals.ava_state(_app(config={})))
    return samples


def test_every_state_and_evidence_string_uses_plain_language(monkeypatch):
    states = _all_state_copy(monkeypatch)
    for state in states:
        for text in (state.detail, state.evidence):
            assert not COPY_KEY.search(text.casefold()), text
            assert not INTERNAL_NOUN.search(text), text
        if state.detail and state.evidence:
            assert state.detail.casefold() not in state.evidence.casefold()
            assert state.evidence.casefold() not in state.detail.casefold()

    monkeypatch.setattr(home_signals, "corrections_waiting", lambda _app: 3)
    monkeypatch.setattr(home_signals, "no_text_captures", lambda _app: (3, 20))
    monkeypatch.setattr(home_signals, "recent_misses", lambda _app: (3, 8))
    for hint in home_signals.diagnostic_hints(_app()):
        assert not COPY_KEY.search(hint.evidence.casefold()), hint.evidence
        assert not INTERNAL_NOUN.search(hint.evidence), hint.evidence


def test_all_literal_capability_and_hint_evidence_is_safe_copy():
    tree = ast.parse((REPO / "samsara" / "ui" / "home_signals.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name == "CapabilityState":
            for value in node.args[2:4]:
                found.extend(_copy_strings(value))
        if name == "Hint":
            found.extend(_copy_strings(next(
                (kw.value for kw in node.keywords if kw.arg == "evidence"), ast.Constant(value=""))))
    assert found
    for text in found:
        assert not COPY_KEY.search(text.casefold()), text
        assert not INTERNAL_NOUN.search(text), text


def test_every_settings_location_names_a_declared_page_and_row(monkeypatch):
    states = _all_state_copy(monkeypatch)
    copy = "\n".join(text for state in states for text in (state.detail, state.evidence))
    citations = {(page.strip(), row.strip()) for page, row in SETTINGS_CITATION.findall(copy)}
    assert citations == {
        ("General", "Microphone"),
        ("Dictation", "Wake activation"),
    }
    declarations = _declared_settings_rows()
    for page, row in citations:
        assert row in declarations[page], (page, row)


@pytest.mark.parametrize("readiness, expected", [
    (ava_readiness.READY, (home_signals.AVA_AWAKE, "Ava is ready to answer.")),
    (ava_readiness.WARMING, (home_signals.AVA_WARMING, "Ava is warming up.")),
    (ava_readiness.OFFLINE, (home_signals.AVA_DIM,
                             "Ava is unavailable: unreachable.")),
])
def test_ava_presence_follows_effective_state_then_provider_readiness(
        monkeypatch, readiness, expected):
    config = _ava_config()
    monkeypatch.setattr(
        ava_readiness, "readiness_for",
        lambda _app: ava_readiness.Readiness(readiness, provider="ollama",
                                              failure_kind=ava_readiness.UNREACHABLE),
    )
    state = ai_preferences.runtime_state(config, "ava")
    assert state.allowed
    assert home_signals.ava_presence(_app(config=config)) == expected


def test_ava_presence_uses_effective_state_reason_when_setup_blocks_it():
    config = _ava_config(policy="cloud", api_key="")
    effective = ai_preferences.runtime_state(config, "ava")
    assert effective.status == "setup-pending"
    assert home_signals.ava_presence(_app(config=config)) == (
        home_signals.AVA_CHECKING, effective.reason)


def test_ava_presence_does_not_call_a_setup_blocked_ava_working():
    config = _ava_config(policy="cloud", api_key="")
    effective = ai_preferences.runtime_state(config, "ava")
    app = _app(config=config)
    app.ava_thinking_since = time.monotonic()
    assert home_signals.ava_presence(app) == (home_signals.AVA_CHECKING, effective.reason)


def test_ava_presence_calls_an_off_choice_off_with_its_effective_state_reason():
    config = {"ava": {"provider_policy": "off"}, "ava_command_session": {"enabled": False}}
    effective = ai_preferences.runtime_state(config, "ava")
    assert effective.status == "off-by-choice"
    assert home_signals.ava_presence(_app(config=config)) == (
        home_signals.AVA_ASLEEP, effective.reason)


@pytest.mark.parametrize("case", ["wake-off", "microphone-missing", "ava-offline"])
def test_voice_help_capability_cards_render(case, qapp, monkeypatch, tmp_path):
    home = tmp_path / "samsara-home"
    home.mkdir()
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(home))
    config = {"wake_word_enabled": True}
    mics = [{"id": 1, "name": "Test mic"}]
    if case == "wake-off":
        config["wake_word_enabled"] = False
    elif case == "microphone-missing":
        mics = []
    else:
        config["ollama"] = {"enabled": True}
        monkeypatch.setattr(
            ava_readiness, "readiness_for",
            lambda _app: types.SimpleNamespace(
                state=ava_readiness.OFFLINE,
                sentence=lambda: "Ava offline because the provider readiness check failed."))
    app = _app(config=config, mics=mics)
    page = voice_help_qt.VoiceHelpPage(app)
    try:
        page.resize(980, 920)
        page.show()
        qapp.processEvents()
        proof = tmp_path
        proof.mkdir(parents=True, exist_ok=True)
        assert QWidget.grab(page).save(str(proof / f"voice-help-{case}.png"))
        copy = " ".join(label.text() for label, _evidence in page._capability_rows.values())
        copy += " " + " ".join(evidence.text() for _label, evidence in page._capability_rows.values())
        assert not COPY_KEY.search(copy.casefold()), copy
        assert not INTERNAL_NOUN.search(copy), copy
    finally:
        page.deleteLater()
        qapp.processEvents()
