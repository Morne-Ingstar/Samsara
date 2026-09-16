"""Regression coverage for the Dictionary panel's dictated-text store.

The app is deliberately not imported: these tests exercise the shared
VoiceTrainingQt instance used by both normal dictated-text pipelines.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit, QTableWidget

from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
from samsara.ui.voice_training_qt import VoiceTrainingQt


def _app(tmp_path):
    config_path = tmp_path / "config.json"
    (tmp_path / "training_data.json").write_text(
        json.dumps({"vocabulary": ["unchanged name"], "corrections": {}}),
        encoding="utf-8",
    )
    app = SimpleNamespace(config_path=config_path, config={"initial_prompt": ""})
    app.voice_training_window = VoiceTrainingQt(app)
    return app


@pytest.fixture
def panel_app(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    app = _app(tmp_path)
    panel = DictionaryPanelQt(app)
    import samsara.ui.dictionary_panel_qt as panel_module

    # Saving is normally dispatched so the UI never blocks.  Run the worker
    # synchronously here, leaving the actual panel method under test.
    monkeypatch.setattr(
        panel_module.thread_registry,
        "spawn",
        lambda _name, target, daemon=True: target(),
    )
    return panel, app


def _panel_add(panel, qapp, heard="mistranscription", right="intended"):
    table, wrong_input, right_input, status = QTableWidget(), QLineEdit(), QLineEdit(), QLabel()
    wrong_input.setText(heard)
    right_input.setText(right)
    panel._kv_add(table, wrong_input, right_input, "corrections", status)
    qapp.processEvents()


def test_panel_correction_rewrites_dictated_text_and_not_phonetic_wash(panel_app, qapp, tmp_path):
    panel, app = panel_app

    _panel_add(panel, qapp)

    assert app.voice_training_window.apply_corrections("a mistranscription occurred") == "a intended occurred"
    saved = json.loads((tmp_path / "training_data.json").read_text(encoding="utf-8"))
    assert saved["corrections"] == {"mistranscription": "intended"}
    assert not (tmp_path / "user_corrections.json").exists()


def test_both_normal_dictation_paths_use_the_authoritative_correction_pass():
    """Keep the hold and hands-free source paths wired to the same pass.

    Importing dictation is forbidden while the live app runs, so inspect only
    its source here.  The two named paths are the hold transcription and the
    hands-free session decode; both must invoke the VoiceTrainingQt method.
    """
    source = (Path(__file__).parents[1] / "dictation.py").read_text(encoding="utf-8")
    assert "# Apply corrections dictionary\n            text = self.voice_training_window.apply_corrections(text)" in source
    assert "text = self.voice_training_window.apply_corrections(text)\n\n            if not text:\n                logger.debug('[CMD-UTT] Empty transcription')" in source


def test_deleted_bundled_correction_stays_deleted_after_restart(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    app = SimpleNamespace(config_path=tmp_path / "config.json", config={"initial_prompt": ""})
    first = VoiceTrainingQt(app)
    app.voice_training_window = first
    assert "claud" in first.corrections_dict

    panel = DictionaryPanelQt(app)
    import samsara.ui.dictionary_panel_qt as panel_module

    monkeypatch.setattr(
        panel_module.thread_registry,
        "spawn",
        lambda _name, target, daemon=True: target(),
    )
    table = QTableWidget(0, 3)
    panel._kv_load(table, "corrections")
    row = next(
        index for index in range(table.rowCount())
        if table.item(index, 0).text() == "claud"
    )
    table.selectRow(row)
    panel._kv_remove(table, "corrections", QLabel())
    qapp.processEvents()

    second = VoiceTrainingQt(app)

    assert "claud" not in second.corrections_dict
    assert second.custom_vocab == first.custom_vocab
