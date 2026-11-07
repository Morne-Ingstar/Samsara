"""Hermetic recorder/real-pipeline checks; private corpus is explicitly opt-in."""
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tools.voice_corpus import RATE
from tools.voice_corpus.script import SCRIPT
from tools.voice_corpus.storage import Session, digest, external_path, read_wav, recording_lock, validate_sidecar
from tools.voice_corpus.scoring import compare, measure, score
from tools.voice_corpus.__main__ import integration_enabled, main


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "SAMSARA_HOME_DIR", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    return home


def sample_session(tmp_path):
    return Session(tmp_path / "private_corpus", SCRIPT[:3])


def save(session, **kwargs):
    return session.save(np.ones(RATE, dtype="<i2") * 1000, .6, device_name="Fake input", **kwargs)


def test_advance_redo_skip_pause_resume(tmp_path):
    session = sample_session(tmp_path)
    first = save(session)
    assert session.current["id"] == SCRIPT[1]["id"]
    assert session.control("again")
    assert session.current["id"] == SCRIPT[0]["id"]
    replacement = save(session)
    assert replacement["id"] != first["id"]
    assert (session.takes / (first["id"] + ".wav")).exists()
    session.control("skip")
    assert session.current["id"] == SCRIPT[2]["id"]
    session.control("pause session")
    resumed = sample_session(tmp_path)
    assert resumed.state["paused"] and resumed.completed == 2
    assert resumed.current["id"] == SCRIPT[2]["id"]
    assert resumed.selected()[0]["id"] == replacement["id"]
    assert not resumed.control("not a control")


def test_skipping_redo_keeps_previous_take(tmp_path):
    session = sample_session(tmp_path)
    first = save(session)
    session.control("again")
    session.control("skip")
    assert session.state["selected"][SCRIPT[0]["id"]] == first["id"]
    assert session.current["id"] == SCRIPT[1]["id"]


def test_sidecar_truth_pcm_and_crash_recovery(tmp_path):
    session = sample_session(tmp_path)
    prior_state = json.dumps(session.state)
    row = save(session, synthetic=True)
    assert validate_sidecar(row) == row
    assert row["intended_text"] == SCRIPT[0]["intended_text"]
    assert not {"raw_transcript", "final_text", "transcript"} & row.keys()
    assert row["rms"] == pytest.approx(1000 / 32768)
    path = session.takes / (row["id"] + ".wav")
    assert digest(path) == row["audio_sha256"]
    np.testing.assert_array_equal(read_wav(path), np.ones(RATE) * 1000)
    session.path.write_text(prior_state, encoding="utf-8")
    assert sample_session(tmp_path).selected()[0]["id"] == row["id"]


@pytest.mark.parametrize("key,value", [("peak", float("nan")), ("rms", 2), ("release_s", 9),
                                      ("sample_rate", 48000), ("tags", "quiet"), ("id", "../bad")])
def test_invalid_sidecar_rejected(tmp_path, key, value):
    row = save(sample_session(tmp_path))
    row[key] = value
    with pytest.raises(ValueError):
        validate_sidecar(row)


def test_script_changes_and_repository_storage_rejected(tmp_path):
    session = sample_session(tmp_path)
    save(session)
    with pytest.raises(ValueError, match="Script changed"):
        Session(session.root, SCRIPT[:2])
    with pytest.raises(ValueError, match="outside"):
        external_path(Path(__file__).resolve().parent / "voice_corpus_data")
    with pytest.raises(ValueError, match="outside"):
        external_path(tmp_path / ".samsara" / "takes")


def test_missing_saved_wav_is_reported_not_silently_skipped(tmp_path):
    session = sample_session(tmp_path)
    row = save(session)
    (session.takes / (row["id"] + ".wav")).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        sample_session(tmp_path)


def test_single_recording_window_lock(tmp_path):
    with recording_lock(tmp_path / "corpus"):
        with pytest.raises(ValueError, match="already open"):
            with recording_lock(tmp_path / "corpus"):
                pytest.fail("Second recorder admitted")
    with recording_lock(tmp_path / "corpus"):
        pass


def test_rolling_recorder_fake_input_preserves_preroll_and_tail(monkeypatch):
    from tools.voice_corpus.audio import Capture
    capture = Capture()
    capture._callback(np.full((1600, 1), .1), 1600, None, None)
    capture.start_take()
    capture._callback(np.full((1600, 1), .2), 1600, None, True)
    capture.tail_done.set()  # Fake the elapsed maximum tail.
    pcm, release, statuses = capture.release()
    assert len(pcm) == 3200 and release >= .1
    assert pcm[0] == 3276 and pcm[-1] == 6553
    assert statuses == ["input overflow or stream warning"]


@pytest.mark.parametrize("reference,hypothesis,expected", [
    ("one two three", "one extra two three", dict(insertions=1, wer=1/3)),
    ("one two three", "two", dict(missing_start_words=1, missing_end_words=1, deletions=2)),
    ("one two", "one one two", dict(doubled_words=1, insertions=1)),
    ("hello world", "hello world hello world", dict(doubled_words=2, insertions=2)),
    ("really really good", "really really good", dict(doubled_words=0, wer=0)),
    ("", "invented words", dict(insertions=2, wer=2)),
    ("", "", dict(wer=0, missing_end_words=0)),
    ("one two", "", dict(deletions=2, missing_start_words=2, missing_end_words=2)),
    ("Hello, world!", "hello world", dict(wer=0, exact_text=False)),
])
def test_score_math(reference, hypothesis, expected):
    result = measure(reference, hypothesis)
    for key, value in expected.items():
        assert result[key] == pytest.approx(value)


def example_run():
    row = dict(id="a" * 32, line_id="line_01", tags=["quiet"], release_to_text_ms=100.,
               **measure("one two", "one two"))
    return dict(build_id="old", corpus_sha256="same", settings_sha256="same", takes=[row])


def test_tag_micro_average_and_regression_comparison():
    before = example_run()
    assert score(before)["tags"]["quiet"]["wer"] == 0
    after = json.loads(json.dumps(before))
    after["build_id"] = "new"
    after["takes"][0].update(measure("one two", "one two two"))
    result = compare(score(before), score(after))
    assert result["regressions"][0]["regressed"] == ["wer", "insertions", "doubled_words"]
    after["corpus_sha256"] = "changed"
    with pytest.raises(ValueError, match="Corpus"):
        compare(before, after)
    after["corpus_sha256"] = "same"
    after["settings_sha256"] = "different"
    with pytest.raises(ValueError, match="settings"):
        compare(before, after)


def test_score_cli_writes_numeric_dated_baseline(tmp_path, capsys):
    source = tmp_path / "run.json"
    source.write_text(json.dumps(example_run()), encoding="utf-8")
    assert main(["score", str(source)]) == 0
    assert "word error rate" in capsys.readouterr().out
    baseline = next(tmp_path.glob("baseline_old_*.json"))
    assert json.loads(baseline.read_text())["totals"]["takes"] == 1
    assert baseline.with_suffix(".md").exists()


def test_virtual_detection_requires_both_endpoints():
    from tools.voice_corpus.virtual_mic import detect
    assert not detect([])["available"]
    output = dict(name="CABLE Input (VB-Audio Virtual Cable)", max_output_channels=2)
    assert not detect([output])["available"]
    found = detect([output, dict(name="CABLE Output (VB-Audio Virtual Cable)", max_input_channels=2)])
    assert found["available"] and found["playback_devices"][0]["id"] == 0


def test_corpus_marker_is_opt_in_and_never_ci(tmp_path):
    assert not integration_enabled(tmp_path, {})
    assert not integration_enabled(tmp_path / "absent", {"SAMSARA_VOICE_CORPUS": "1", "SAMSARA_VOICE_MODEL": "fake"})
    assert not integration_enabled(tmp_path, {"CI": "1", "SAMSARA_VOICE_CORPUS": "1", "SAMSARA_VOICE_MODEL": "fake"})
    assert integration_enabled(tmp_path, {"SAMSARA_VOICE_CORPUS": "1", "SAMSARA_VOICE_MODEL": "fake"})


class FakeModel:
    def transcribe(self, audio, **params):
        segment = NS(text="A harmless sample sentence.", start=.1, end=.9, avg_logprob=-.1,
                     no_speech_prob=.01, compression_ratio=1., words=[], tokens=[1, 2])
        return iter([segment]), NS(language="en", language_probability=1.)


def test_real_capture_tail_guards_and_fake_delivery_deterministic(private_home, monkeypatch):
    import sounddevice as sd
    monkeypatch.setattr(sd, "InputStream", lambda *a, **k: pytest.fail("Microphone was opened"))
    monkeypatch.setattr(sd, "play", lambda *a, **k: pytest.fail("Audio playback attempted"))
    from tools.voice_corpus.replay import run_take
    signal = (np.sin(np.arange(RATE) * .1) * 8000).astype("<i2")
    results = [run_take(signal, .9, FakeModel(), {"format_numbers": False}) for _ in range(2)]
    for result in results:
        assert result["pcm_preserved"] and result["captured_samples"] >= RATE
        assert result["text"] == "A harmless sample sentence."
        assert result["final_state"] == "done" and result["delivery_count"] == 1
        assert result["tail"]["reason"] == "silence"
    assert measure(results[0]["text"], results[1]["text"])["wer"] == 0


def test_long_replay_runs_production_preview_and_keep_control(private_home):
    from tools.voice_corpus.replay import run_take
    signal = (np.sin(np.arange(14 * RATE) * .1) * 8000).astype("<i2")
    result = run_take(signal, 13.9, FakeModel(), {"format_numbers": False})
    assert result["partials"] == 1 and result["delivery_count"] == 1
    class KeepModel:
        def transcribe(self, audio, **params):
            segments, info = FakeModel().transcribe(audio, **params)
            segment = next(segments)
            segment.text = "Keep that."
            return iter([segment]), info
    result = run_take(signal[:RATE], .9, KeepModel())
    assert result["held_keep_routed"] and result["delivery_count"] == 0
    assert result["raw"] == "Keep that."


def test_routing_uses_production_rules_with_fake_effects(private_home):
    from tools.voice_corpus.routing import check, route
    assert route("yes") == "pending_reply:yes"
    assert route("keep that") == "keep"
    assert route("clear draft").startswith("session:")
    assert route("bring it back").startswith("session:")
    assert route("pause VLC") == "catalog:music.pause_vlc"
    assert route("show the change") == "catalog:selection_edit.show_the_change"
    assert check("pause VLC", "play VLC")["command_routed_correctly"] is False


def test_recorder_window_key_save_pause_and_redo(private_home, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from tools.voice_corpus.recorder import RecorderWindow
    app = QApplication.instance() or QApplication([])
    class Audio:
        settings = {}
        device = {"name": "Fake input"}
        def start_take(self):
            pass
        def release(self):
            return np.ones(RATE, dtype="<i2"), .8, []
    window = RecorderWindow(sample_session(tmp_path), Audio())
    window.show()
    QTest.keyPress(window, Qt.Key.Key_Space)
    assert window.held
    QTest.keyRelease(window, Qt.Key.Key_Space)
    for _ in range(100):
        QTest.qWait(10)
        if not window.busy:
            break
    assert window.session.completed == 1
    window.control("again")
    assert window.session.current["id"] == SCRIPT[0]["id"]
    window.toggle_pause()
    assert window.session.state["paused"]
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert not window.session.state["paused"]
    # A voice-control decoder failure must still save the completed PCM.
    from tools.voice_corpus import recorder
    monkeypatch.setattr(recorder, "control_phrase", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    window.model = object()
    result = window.finish()
    assert result.startswith("Saved") and len(window.session.selected()) == 1
    assert window.session.selected()[0]["capture_status"] == ["voice controls unavailable for this take"]
    window.close()
    app.processEvents()


@pytest.mark.voice_corpus
def test_opt_in_private_corpus(tmp_path):
    from tools.voice_corpus.storage import DEFAULT_ROOT
    root = Path(os.environ.get("SAMSARA_VOICE_ROOT", DEFAULT_ROOT))
    if not integration_enabled(root):
        pytest.skip("Private corpus absent or not opted in; never runs in CI")
    assert main(["replay", "--corpus", str(root), "--model", os.environ["SAMSARA_VOICE_MODEL"],
                 "--build-id", "explicit-local-check", "--output", str(tmp_path / "private_metrics.json")]) == 0
