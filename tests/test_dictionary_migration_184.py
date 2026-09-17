"""Regression coverage for the pre-783d728 corrections-store migration."""

import json
import logging
from types import SimpleNamespace

from samsara.ui.voice_training_qt import VoiceTrainingQt


def test_legacy_corrections_migrate_once_and_authoritative_entries_win(tmp_path, caplog):
    training_file = tmp_path / "training_data.json"
    training_file.write_text(
        json.dumps({
            "vocabulary": ["unchanged"],
            "corrections": {"conflict": "authoritative"},
        }),
        encoding="utf-8",
    )
    legacy_file = tmp_path / "user_corrections.json"
    legacy_file.write_text(
        json.dumps({"conflict": "legacy", "oldheard": "restored"}),
        encoding="utf-8",
    )
    app = SimpleNamespace(config_path=tmp_path / "config.json", config={"initial_prompt": ""})

    with caplog.at_level(logging.INFO):
        first = VoiceTrainingQt(app)

    assert first.corrections_dict == {
        "conflict": "authoritative",
        "oldheard": "restored",
    }
    assert first.apply_corrections("conflict oldheard") == "authoritative restored"
    assert not legacy_file.exists()
    archives = list(tmp_path.glob("user_corrections.json.migrated-*"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == {
        "conflict": "legacy",
        "oldheard": "restored",
    }
    assert "Migrated 1 legacy user corrections" in caplog.text

    caplog.clear()
    second = VoiceTrainingQt(app)

    assert second.corrections_dict == first.corrections_dict
    assert len(list(tmp_path.glob("user_corrections.json.migrated-*"))) == 1
    assert "Migrated" not in caplog.text
