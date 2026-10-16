"""Dictionary extraction: persisted profiles and consumers keep their behavior."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from samsara.dictionary_store import DictionaryStore, get_dictionary_store


def _app(tmp_path, payload):
    (tmp_path / "training_data.json").write_bytes(payload)
    app = SimpleNamespace(config_path=tmp_path / "config.json", config={})
    app.dictionary_store = DictionaryStore(app)
    return app


@pytest.mark.parametrize("newline,escaped", [("\n", False), ("\r\n", False), ("\n", True), ("\r\n", True)])
def test_existing_training_data_round_trips_byte_identically(tmp_path, newline, escaped):
    data = {"corrections": {"cafe": "café", "teh": "the"},
            "vocabulary": ["München", "TensorFlow"], "profile_version": 7}
    original = (json.dumps(data, ensure_ascii=escaped, indent=4) + "\n").replace("\n", newline).encode("utf-8")
    app = _app(tmp_path, original)
    store = app.dictionary_store

    assert store.custom_vocab == data["vocabulary"]
    assert store.apply_corrections("teh cafe") == "the café"
    assert store.save_training_data() is True
    assert (tmp_path / "training_data.json").read_bytes() == original
    assert (tmp_path / "training_data.json.bak").read_bytes() == original
    reloaded = DictionaryStore(app)
    assert reloaded.custom_vocab == store.custom_vocab
    assert reloaded.corrections_dict == store.corrections_dict
    assert reloaded.save_training_data() is True
    assert (tmp_path / "training_data.json").read_bytes() == original


def test_changed_dictionary_uses_existing_format_and_preserves_backup(tmp_path):
    original = b'{"vocabulary": ["API"], "corrections": {"teh": "the"}}\n'
    app = _app(tmp_path, original)
    assert app.dictionary_store.add_correction("recieve", "receive") is True
    saved = json.loads((tmp_path / "training_data.json").read_bytes())
    assert saved == {"vocabulary": ["API"], "corrections": {"teh": "the", "recieve": "receive"}}
    assert (tmp_path / "training_data.json.bak").read_bytes() == original
    assert DictionaryStore(app).apply_corrections("teh recieve") == "the receive"


def test_prompt_output_and_include_vocabulary_contract_are_preserved(tmp_path):
    app = _app(tmp_path, b'{"vocabulary": ["TensorFlow", "NumPy"], "corrections": {}}')
    app.config.update(initial_prompt="Technical context.", web_shortcuts={"alpha tools": "unused"})
    store = app.dictionary_store
    assert store.get_initial_prompt() == "Technical context. Common terms: TensorFlow, NumPy Voice commands: alpha tools"
    assert store.get_initial_prompt(include_vocabulary=False) == "Technical context."
    app.config["initial_prompt"] = ""
    assert store.get_initial_prompt(include_vocabulary=False) is None


def test_correction_review_writes_to_service_without_legacy_attribute(tmp_path):
    from samsara.ui.correction_capture_qt import CorrectionCaptureWindow

    app = _app(tmp_path, b'{"vocabulary": [], "corrections": {}}')
    messages = []
    review = SimpleNamespace(app=app, _set_status=messages.append)
    CorrectionCaptureWindow._on_always_fix(review, "teh", "the")
    assert app.dictionary_store.apply_corrections("teh word") == "the word"
    assert DictionaryStore(app).corrections_dict == {"teh": "the"}
    assert messages[-1].startswith("Added")


def test_dictionary_panel_uses_service_without_legacy_attribute(tmp_path, qapp):
    from samsara.ui.dictionary_panel_qt import DictionaryPanelQt

    app = _app(tmp_path, b'{"vocabulary": ["API"], "corrections": {"teh": "the"}}')
    panel = DictionaryPanelQt(app)
    try:
        assert panel._vt is app.dictionary_store
        assert panel._custom_vocab == ["API"]
    finally:
        panel.close()
        panel.deleteLater()
        qapp.processEvents()


def test_service_resolver_prefers_new_attribute_and_accepts_legacy_alias():
    current, legacy = object(), object()
    app = SimpleNamespace(dictionary_store=current, voice_training_window=legacy)
    assert get_dictionary_store(app) is current
    del app.dictionary_store
    assert get_dictionary_store(app) is legacy
    assert get_dictionary_store(SimpleNamespace()) is None
    assert get_dictionary_store(None) is None


def test_store_has_no_qt_or_audio_imports():
    source = Path(__file__).resolve().parents[1] / "samsara" / "dictionary_store.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not any(name.startswith(("PySide6", "samsara.ui", "sounddevice", "numpy")) for name in names)
