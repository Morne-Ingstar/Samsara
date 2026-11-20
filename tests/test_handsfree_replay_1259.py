"""1259: synthetic-only diagnostic capture, derived recipes and admission stop."""
from collections import deque
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace as NS

import numpy as np
import pytest

from samsara import handsfree_capture as capture
from tools.voice_corpus import handsfree_replay as replay
from tools.voice_corpus.storage import digest, write_wav


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("HOME", "USERPROFILE", "SAMSARA_HOME_DIR", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path / ".samsara-private"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


def app(enabled=True):
    return NS(config={"diagnostics": {"handsfree_session_capture": enabled},
                      "command_mode": {"mode": "toggle"}}, command_mode_active=True)


def frame(seq=0, epoch=0):
    return NS(pcm=np.arange(1600, dtype="int16"), t_capture=100 + seq * .1,
              device_epoch=epoch, seq=seq)


def finish(current):
    current.close()
    assert current.finished.wait(5), "Diagnostic writer did not finish"
    path = current.root / (current.id + ".json")
    return json.loads(path.read_text())


def record_frame(target, source):
    observation = capture.observe_frame(target, source)
    if observation is not None:
        observation.record()


def part(current, suffix):
    return current.root / f"{current.id}-p000001.{suffix}"


def test_off_does_not_create_recorder_or_profile(tmp_path):
    target = app(False)
    capture.observe_state(target)
    capture.observe_frame(target, frame())
    assert not hasattr(target, "_handsfree_capture_1259")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("profile", ["", ".samsara", ".samsara-daily", "unselected"])
def test_switch_cannot_record_live_or_implicit_profile(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path / profile) if profile else "")
    target = app()
    capture.observe_frame(target, frame())
    assert not hasattr(target, "_handsfree_capture_1259")
    assert list(tmp_path.iterdir()) == []


def test_continuous_pcm_is_copied_with_timestamps_and_draft_events(tmp_path):
    target = app()
    manager = NS(draft_document=NS(text="Synthetic draft.", revision=1), mode=NS(value="dictate"))
    target._session_mode_manager = manager
    source = frame()
    expected = source.pcm.copy()
    record_frame(target, source)
    current = target._handsfree_capture_1259
    source.pcm[:] = 0  # Simulate ring reuse while the asynchronous writer runs.
    record_frame(target, frame(1))
    capture.observe_chunk(target, [expected], speech_start_ns=100, speech_end_ns=200)
    manager._commit_redecode_fn = NS(__self__=target)
    capture.observe_dispatch(manager, NS(kind="dictate_committed", detail={"text": "Synthetic draft."}))
    metadata = finish(current)
    assert metadata["complete"] and not metadata["fidelity_ready"]
    assert metadata["samples"] == 3200
    saved = replay.read_wav(part(current, "wav"))
    assert np.array_equal(saved[:1600], expected)
    rows = [json.loads(line) for line in part(current, "jsonl").read_text().splitlines()]
    assert [r["event"] for r in rows] == ["part_start", "enter", "mode", "draft", "chunk", "dispatch", "part_end"]
    assert [r[3] for r in capture.FRAME_ROW.iter_unpack(part(current, "frames").read_bytes())] == [0, 1600]
    assert rows[4]["speech_end_ns"] == 200
    # 1302 records text lengths only. Schema 2 cannot satisfy legacy exact-text pair admission.
    assert rows[5]["delivered_chars"] == 16 and rows[5]["delivered_words"] == 2
    assert "Synthetic draft" not in json.dumps(rows)
    assert all(isinstance(r["observed_ns"], int) for r in rows)
    assert "samsara/session_modes.py" in metadata["source_sha256"]
    assert replay.inventory([current.root])["paired_real_sessions"] == 0


@pytest.mark.parametrize("second", [frame(2), frame(1, 1)])
def test_frame_loss_or_device_change_invalidates_capture(tmp_path, second):
    target = app()
    record_frame(target, frame())
    record_frame(target, second)
    metadata = finish(target._handsfree_capture_1259)
    assert not metadata["complete"] and metadata["incomplete_reason"] == "frame_discontinuity"


def test_queue_overflow_invalidates_instead_of_blocking_audio(tmp_path, monkeypatch):
    ready = threading.Event()
    original = capture.Capture._write
    def wait_writer(self):
        ready.wait(5)
        original(self)
    monkeypatch.setattr(capture.Capture, "_write", wait_writer)
    current = capture.Capture(tmp_path / "capture", {}, queue_size=1)
    capture.FrameEvidence(current, frame()).record()
    current.put("draft", {"chars": 9})
    assert current.failed and current.closed.is_set()
    ready.set()
    metadata = finish(current)
    assert metadata["dropped_events"] == 1 and not metadata["complete"]


def test_session_duration_cap_is_labelled(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_SECONDS", .1)
    target = app()
    record_frame(target, frame())
    record_frame(target, frame(1))
    metadata = finish(target._handsfree_capture_1259)
    # 1302 rotates at the same cap and keeps recording into the next part.
    assert metadata["samples"] == 3200 and metadata["parts"] == 2
    assert metadata["complete"] and metadata["incomplete_reason"] is None


def test_stop_and_disable_close_without_waiting_on_writer():
    target = app()
    record_frame(target, frame())
    current = target._handsfree_capture_1259
    target.command_mode_active = False
    capture.observe_state(target)
    assert current.closed.is_set() and target._handsfree_capture_1259 is None
    assert current.finished.wait(5)


def test_ending_with_pending_decode_is_not_admitted_as_a_complete_session():
    target = app()
    target._wake_consumer = NS(_toggle_worker_active=True)
    record_frame(target, frame())
    current = target._handsfree_capture_1259
    target.command_mode_active = False
    capture.observe_state(target)
    metadata = finish(current)
    assert not metadata["complete"] and metadata["incomplete_reason"] == "ended_with_pending_decode"
    assert replay.inventory([current.root])["paired_real_sessions"] == 0


def test_capture_failures_do_not_escape_to_production(tmp_path):
    capture.observe_state(NS(config=None))
    invalid = app()
    capture.observe_frame(invalid, NS())
    assert not finish(invalid._handsfree_capture_1259)["complete"]
    target = app()
    record_frame(target, frame())
    finish(target._handsfree_capture_1259)
    root = tmp_path / "not_a_directory"
    root.write_text("synthetic")
    current = capture.Capture(root, {})
    assert current.finished.wait(5) and current.failed


def test_retention_prunes_only_uuid_pairs_including_orphans(tmp_path):
    old, recent = "a" * 32, "b" * 32
    for stem, timestamp in ((old, 10), (recent, 100)):
        for suffix in (".wav", ".jsonl"):
            path = tmp_path / (stem + suffix)
            path.write_bytes(b"data")
            os.utime(path, (timestamp, timestamp))
    unrelated = tmp_path / "notes.json"
    unrelated.write_text("keep")
    capture.prune(tmp_path, now=110, max_age_s=50, max_bytes=100)
    assert not (tmp_path / (old + ".wav")).exists()
    assert (tmp_path / (recent + ".wav")).exists()
    capture.prune(tmp_path, now=110, max_age_s=50, max_bytes=0)
    assert list(tmp_path.iterdir()) == [unrelated]


def test_settings_snapshot_does_not_copy_provider_dictionaries():
    target = app()
    target.config.update(model_size="synthetic", provider={"credential": "synthetic"})
    assert capture._settings(target)["model_size"] == "synthetic"
    assert "provider" not in capture._settings(target)


def make_takes(root, *, synthetic=False):
    root.mkdir()
    for index in range(3):
        pcm = np.full(16000, 30 + index, dtype="int16")
        pcm[1000:5000] = 1200
        pcm[7000:14000] = 1400
        path = root / (str(index) + ".wav")
        write_wav(path, pcm)
        path.with_suffix(".json").write_text(json.dumps(dict(kind="prose", synthetic=synthetic,
            expected_text="A synthetic sentence with several simple words.", timestamp=str(index),
            audio_sha256=digest(path))))
    return root


def two_spans(_):
    return [{"start": 1000, "end": 5000}, {"start": 7000, "end": 14000}]


def test_derived_recipes_render_exact_original_speech_and_recorded_room_tone(tmp_path):
    root = make_takes(tmp_path / "takes")
    result = replay.derive(root, spans_fn=two_spans)
    assert result["kind"] == "DERIVED" and len(result["rows"]) == 8
    assert result["injected_pauses"] == 16
    assert {r["injected_gap_s"] for r in result["rows"]} == {.4, .8, 1.5, 3.}
    for row in result["rows"]:
        pcm = replay.render_derived(row)
        original = replay.read_wav(row["clips"][0]["wav"])
        assert np.array_equal(pcm[:5000], original[:5000])
        assert len(pcm) == row["samples"]
        assert np.all(pcm[5000:5000 + round(row["injected_gap_s"] * 16000)] == 30)
    assert "expected_text" not in json.dumps(result)


def test_derived_rejects_synthetic_provenance_and_never_substitutes_silence(tmp_path):
    root = make_takes(tmp_path / "takes", synthetic=True)
    assert not replay.derive(root, spans_fn=two_spans)["rows"]
    for path in root.glob("*.json"):
        row = json.loads(path.read_text())
        row["synthetic"] = False
        path.write_text(json.dumps(row))
    spans = lambda _: [{"start": 0, "end": 16000}]
    assert "No measured room-tone" in replay.derive(root, spans_fn=spans)["reason"]


def test_derived_integrity_rejects_later_source_edits(tmp_path):
    root = make_takes(tmp_path / "takes")
    row = replay.derive(root, spans_fn=two_spans)["rows"][0]
    Path(row["clips"][0]["wav"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="source changed"):
        replay.render_derived(row)


def test_inventory_cannot_misclassify_holds_or_derived_as_real_sessions(tmp_path):
    root = make_takes(tmp_path / "takes")
    report = replay.inventory([root])
    assert report["paired_real_sessions"] == 0
    assert sum(g["wavs"] for g in report["groups"].values()) == 3


@pytest.mark.parametrize("count", [0, 9, 10, 100])
def test_preflight_never_turns_sample_count_into_a_fidelity_pass(count):
    result = replay.preflight({"paired_real_sessions": count})
    assert result["status"] == "BLOCKED"
    assert result["match_rate"] is None and not result["fidelity_verified"]


def test_preflight_cli_exits_nonzero_with_numeric_only_evidence(tmp_path, capsys):
    root = make_takes(tmp_path / "takes")
    output = tmp_path / "preflight.json"
    assert replay.main(["preflight", "--root", str(root), "--output", str(output)]) == 2
    saved = json.loads(output.read_text())
    assert saved["status"] == "BLOCKED" and "expected_text" not in capsys.readouterr().out


def test_live_source_paths_are_rejected_before_any_read(tmp_path):
    with pytest.raises(ValueError, match="Live profiles"):
        replay.inventory([tmp_path / ".samsara"])


def test_production_enqueue_keeps_original_frames_and_fifo():
    from samsara.audio_engine.wake_consumer import WakeConsumer
    frames = [frame().pcm.astype(np.float32) / 32767]
    target = app(False)
    consumer = NS(_app=target, _toggle_queue_lock=threading.Lock(), _toggle_queue=deque(),
                  _toggle_worker_active=True)
    WakeConsumer._enqueue_toggle_utterance(consumer, frames, None, 1, 2, 0)
    assert len(consumer._toggle_queue) == 1
    queued = consumer._toggle_queue[0]
    assert queued.frames is frames and queued.speech_end_ns == 2
    assert not hasattr(target, "_handsfree_capture_1259")


def test_production_dispatch_and_button_commit_observers_preserve_result(monkeypatch):
    from samsara import kept_dictations as kept
    target = app()
    record_frame(target, frame())
    current = target._handsfree_capture_1259
    manager = NS(_commit_redecode_fn=NS(__self__=target), _dispatch_lock=threading.RLock(),
                 draft_document=NS(text="Synthetic draft."))
    outcome = NS(kind="dictate_committed", detail={"text": "Synthetic result."})
    monkeypatch.setattr(kept, "_dispatch_session", lambda *args: outcome)
    marks = []
    assert kept.dispatch_session(manager, "Synthetic", None, None, NS(mark=marks.append)) is outcome
    assert marks == ["dispatch_start", "action_done"]
    wrapped = kept.observe_commit(lambda *args: outcome)
    assert wrapped(manager) is outcome
    assert finish(current)["complete"]
    rows = [json.loads(line) for line in part(current, "jsonl").read_text().splitlines()]
    assert sum(r["event"] == "dispatch" for r in rows) == 2
