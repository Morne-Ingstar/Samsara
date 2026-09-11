import json
import math
import sys
import wave
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from tools.probes import hf_corpus_record as recorder
from tools.probes.hf_corpus_record import SCRIPT, load_manifest, rms_dbfs, write_manifest


def test_manifest_write_and_resume(tmp_path):
    path = tmp_path / "hf_corpus" / "manifest.json"
    rows = [{"file": "speech_owner_01.wav", "label": "speech_owner",
             "expected_text": "hello", "duration_s": 1.0,
             "rms_dbfs": -12.0, "timestamp": "now"}]
    write_manifest(path, rows)
    assert load_manifest(path) == rows
    assert {r["file"] for r in load_manifest(path)} == {"speech_owner_01.wav"}


def test_script_labels_are_exact_and_read_items_have_expected_text():
    assert Counter(item["label"] for item in SCRIPT) == {
        "speech_owner": 6, "nonspeech_body": 5, "nonspeech_room": 4, "silence": 3,
        "media_only": 3, "speech_owner_over_media": 3, "wake_over_media": 2,
        "speech_owner_over_media_leadin": 4,
    }
    for item in SCRIPT:
        if item["label"].startswith("speech_owner"):
            assert item["expected_text"]
    leadin = [item for item in SCRIPT if item["label"] == "speech_owner_over_media_leadin"]
    assert [item["expected_text"] for item in leadin] == recorder.READ_SENTENCES[:4]
    assert all("TV playing at normal volume; wait for a line of dialogue, THEN press and read."
               in item["prompt"] for item in leadin)


def test_rms_dbfs_on_synthetic_buffer():
    amplitude = 0.5
    expected = 20 * math.log10(amplitude / math.sqrt(2))
    samples = [16384 * math.sin(2 * math.pi * i / 32) for i in range(3200)]
    assert rms_dbfs(samples) == pytest.approx(expected, abs=0.1)


@pytest.fixture(autouse=True)
def fake_audio(monkeypatch):
    """Every recorder test replaces InputStream; no test can open the mic."""
    import sounddevice
    streams = []

    class FakeStream:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.active = False
            streams.append(self)

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, *exc):
            self.active = False

        def feed(self, values):
            assert self.active
            block = np.asarray(values, dtype=np.float32).reshape(-1, 1)
            self.kwargs["callback"](block, len(block), None, None)

    monkeypatch.setattr(sounddevice, "InputStream", FakeStream)
    monkeypatch.setattr(recorder, "_beep", lambda: None)
    return streams


@pytest.mark.parametrize("capacity,available", [(0, 50), (32, 50), (64, 16), (32, 0)])
def test_ring_concatenation_and_actual_press_offset(fake_audio, tmp_path, capacity, available):
    history = np.linspace(-0.5, 0.5, available, dtype=np.float32)
    pressed = np.asarray([0.25, -0.25, 0.125, 0.5], dtype=np.float32)
    with recorder.RollingRecorder(capacity / recorder.SAMPLE_RATE) as capture:
        stream = fake_audio[-1]
        # Multiple callbacks exercise the rolling eviction boundary.
        stream.feed(history[:10])
        stream.feed(history[10:])
        capture.start_take()
        stream.feed(pressed)
        samples, level, offset = capture.finish_take()
        # Continuing input after finishing cannot change the kept take.
        stream.feed([0.75] * 100)

    buffered = min(capacity, available)
    prefix = history[-buffered:] if buffered else np.empty(0, dtype=np.float32)
    expected = (np.concatenate([prefix, pressed]) * 32767.0).astype(np.int16)
    assert samples == expected.tolist()
    assert offset == buffered / recorder.SAMPLE_RATE
    assert math.isfinite(level)
    assert len(fake_audio) == 1 and not stream.active
    wav = tmp_path / "take.wav"
    recorder._write_wav(wav, samples)
    with wave.open(str(wav), "rb") as wf:
        assert wf.getnframes() == buffered + len(pressed)
        assert round(offset * wf.getframerate()) == buffered


def test_only_appends_four_items_and_resume_keeps_existing_rows(tmp_path, monkeypatch, fake_audio):
    manifest = tmp_path / "hf_corpus" / "manifest.json"
    existing = {"file": "speech_owner_01.wav", "label": "speech_owner", "custom": "preserve me"}
    write_manifest(manifest, [existing])

    def answer(prompt):
        if "begin the take" in prompt:
            fake_audio[-1].feed([0.125] * 32)
            return ""
        if "stop recording" in prompt:
            fake_audio[-1].feed([0.5] * 4)
            return ""
        assert prompt == "keep / redo / skip: "
        return "keep"

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(recorder, "_beep", lambda: pytest.fail("beep would contaminate lead-in"))
    argv = ["--out-dir", str(tmp_path), "--only", "speech_owner_over_media_leadin", "--lead-in", "0.001"]
    recorder.main(argv)
    rows = load_manifest(manifest)
    assert rows[0] == existing
    assert len(rows) == 5
    assert {row["label"] for row in rows[1:]} == {"speech_owner_over_media_leadin"}
    for row in rows[1:]:
        assert row["lead_in_s"] == 0.001
        assert row["press_offset_s"] == 16 / recorder.SAMPLE_RATE
        assert row["expected_text"] in recorder.READ_SENTENCES[:4]
        with wave.open(str(manifest.parent / row["file"]), "rb") as wf:
            assert wf.getnframes() == 20
    assert len(fake_audio) == 1 and not fake_audio[0].active
    recorder.main(argv)
    assert load_manifest(manifest) == rows
    assert len(fake_audio) == 1  # All four done: do not even start another stream.


def test_default_lead_in_metadata_is_zero(tmp_path, monkeypatch, fake_audio):
    monkeypatch.setattr(recorder.time, "sleep", lambda _: None)
    monkeypatch.setattr(recorder, "_record", lambda item, source: ([1, 2, 3], -12.0, 0.0))
    monkeypatch.setattr("builtins.input", lambda _: "keep")
    recorder.main(["--out-dir", str(tmp_path), "--only", "wake_over_media"])
    rows = load_manifest(tmp_path / "hf_corpus" / "manifest.json")
    assert len(rows) == 2
    assert all(row["lead_in_s"] == 0.0 and row["press_offset_s"] == 0.0 for row in rows)
    assert not fake_audio


@pytest.mark.parametrize("value", ["-1", "nan", "inf"])
def test_invalid_lead_in_rejected_before_opening_audio(value, tmp_path, fake_audio):
    with pytest.raises(SystemExit) as exc:
        recorder.main(["--out-dir", str(tmp_path), "--lead-in", value])
    assert exc.value.code == 2
    assert not fake_audio
