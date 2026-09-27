"""Queue 344 defaults, measured short-phrase exception, and trainer dry run."""

import hashlib
import json
import wave
from copy import deepcopy

import pytest

from samsara import wake_profiles
from samsara.config_schema import SETTINGS_SCHEMA
from tools import train_wake_model


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / "home"))


def _short_profile():
    return {"id": "claude", "phrase": "hey claude", "oww_model": "hey_claude.onnx",
            "enabled": True}


def test_default_claude_phrase_and_model():
    profiles = SETTINGS_SCHEMA["wake_profiles"]["default"]
    claude = next(item for item in profiles if item["id"] == "claude")
    assert claude["phrase"] == "okay claude"
    assert claude["oww_model"] == "okay_claude.onnx"
    assert wake_profiles.count_syllables(claude["phrase"]) >= 3


def test_short_phrase_without_model_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_profiles, "WAKE_MODELS_DIR", tmp_path)
    profile = _short_profile()
    wake_profiles.validate_wake_profiles([profile])
    assert profile["enabled"] is False


def test_short_phrase_requires_passing_metrics_and_matching_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_profiles, "WAKE_MODELS_DIR", tmp_path)
    model = tmp_path / "hey_claude.onnx"
    model.write_bytes(b"test model bytes")
    metrics = {
        "phrase": "hey claude", "model": model.name,
        "model_sha256": hashlib.sha256(model.read_bytes()).hexdigest(),
        "false_accepts_per_hour": 0.0, "true_accept_rate": 0.95,
        "owner_true_accept_rate": 1.0, "owner_held_out_positives": 4,
        "negative_hours": 0.1, "held_out_positives": 20,
    }
    (tmp_path / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    profile = _short_profile()
    wake_profiles.validate_wake_profiles([profile])
    assert profile["enabled"] is True
    for bad in ({"false_accepts_per_hour": 1.0}, {"true_accept_rate": 0.94},
                {"model_sha256": "wrong"}, {"phrase": "other"}):
        (tmp_path / "metrics.json").write_text(json.dumps(metrics | bad), encoding="utf-8")
        candidate = _short_profile()
        wake_profiles.validate_wake_profiles([candidate])
        assert candidate["enabled"] is False


def test_settings_phrase_change_updates_hidden_model_name(tmp_path, monkeypatch):
    monkeypatch.setattr(wake_profiles, "WAKE_MODELS_DIR", tmp_path)
    profile = deepcopy(SETTINGS_SCHEMA["wake_profiles"]["default"][0])
    profile["phrase"] = "hey claude"
    wake_profiles.validate_wake_profiles([profile])
    assert profile["oww_model"] == "hey_claude.onnx"
    assert profile["enabled"] is False  # no measured model yet


def test_trainer_cli_dry_run_is_read_only(tmp_path, capsys):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "manifest.json").write_text(
        json.dumps([{"file": "speech.wav", "duration_s": 3.6,
                     "expected_text": "ordinary speech"}]), encoding="utf-8")
    proof = tmp_path / "proof"
    assert train_wake_model.main(["hey claude", "--dry-run", "--proof-dir", str(proof),
                                  "--corpus-dir", str(corpus)]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["owner_samples"] == 0
    assert plan["negative_clips"] == 1
    assert "--training_config" in plan["command"]
    assert not proof.exists()


def test_training_config_uses_installed_openwakeword_cli_keys(tmp_path):
    import yaml
    config = train_wake_model._write_training_config(
        tmp_path, tmp_path / "adapter", tmp_path / "generic.npy",
        tmp_path / "negative.npy", synthetic=240, steps=400)
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert doc["target_phrase"] == ["hey claude"]
    assert doc["piper_sample_generator_path"] == str(tmp_path / "adapter")
    assert doc["feature_data_files"]["generic_speech"].endswith("generic.npy")
    assert doc["false_positive_validation_data_path"].endswith("negative.npy")
    assert doc["batch_n_per_class"]["positive"] > 0


def test_negative_corpus_split_keeps_measurement_clips_unseen(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    rows = []
    for index in range(6):
        name = f"speech_{index}.wav"
        (corpus / name).write_bytes(b"fixture")
        rows.append({"file": name, "expected_text": "ordinary speech"})
    (corpus / "manifest.json").write_text(json.dumps(rows), encoding="utf-8")
    training, held_out = train_wake_model._corpus_split(corpus)
    assert [p.name for p in training] == ["speech_0.wav", "speech_2.wav", "speech_4.wav"]
    assert [p.name for p in held_out] == ["speech_1.wav", "speech_3.wav", "speech_5.wav"]
    assert set(training).isdisjoint(held_out)


def test_record_samples_uses_selected_input_rate_and_saves_16k(tmp_path, monkeypatch, capsys):
    import numpy as np
    import sounddevice as sd
    import winsound

    queried = []
    recorded = []

    def query(device, *, kind):
        queried.append((device, kind))
        return {"name": "Focusrite input", "default_samplerate": 44100.0}

    def rec(frames, *, samplerate, channels, dtype, device, blocking):
        recorded.append((frames, samplerate, channels, dtype, device, blocking))
        samples = np.arange(frames, dtype=np.float64)
        pcm = np.rint(10000 * np.sin(2 * np.pi * 440 * samples / samplerate))
        return pcm.astype(np.int16).reshape(-1, 1)

    monkeypatch.setattr(sd, "query_devices", query)
    monkeypatch.setattr(sd, "rec", rec)
    monkeypatch.setattr(winsound, "Beep", lambda *_: None)
    assert train_wake_model.record_samples("hey claude", count=1, seconds=0.1,
                                           device=7) == 1
    assert queried == [(7, "input")]
    assert recorded == [(4410, 44100, 1, "int16", 7, True)]
    with wave.open(str(train_wake_model.samples_dir("hey_claude") / "001.wav")) as wav:
        assert wav.getframerate() == 16000
        assert wav.getnframes() == 1600
    assert "Focusrite input at 44100 Hz" in capsys.readouterr().out
