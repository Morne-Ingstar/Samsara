"""Plain-language evidence contracts for Home and Voice help (queue 308)."""

import ast
import re
import types
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from samsara import ava_readiness
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
        ("Ava", "Ava"),
    }
    declarations = _declared_settings_rows()
    for page, row in citations:
        assert row in declarations[page], (page, row)


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
        proof = Path(r"C:\Users\Morne\Documents\Claude\ui_proof\308")
        proof.mkdir(parents=True, exist_ok=True)
        assert QWidget.grab(page).save(str(proof / f"voice-help-{case}.png"))
        copy = " ".join(label.text() for label, _evidence in page._capability_rows.values())
        copy += " " + " ".join(evidence.text() for _label, evidence in page._capability_rows.values())
        assert not COPY_KEY.search(copy.casefold()), copy
        assert not INTERNAL_NOUN.search(copy), copy
    finally:
        page.deleteLater()
        qapp.processEvents()
