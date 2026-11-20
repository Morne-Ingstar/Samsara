"""1302: synthetic listener evidence, retention and cross-part alignment."""
import ast
from collections import defaultdict
import json
import math
import os
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import wave

import numpy as np
import pytest

from samsara import handsfree_capture as capture, kept_dictations, speech_presence
from samsara.audio_engine import wake_consumer as wake
from samsara.audio_engine.frame import FRAME_SIZE, SAMPLE_RATE
from tests.test_handsfree_deaf_after_hold_1286 import listener


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / ".samsara-private"
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(path))
    return path


def read_parts(current):
    """Read the shared sample timeline without access to the producing app."""
    manifest = json.loads((current.root / f"{current.id}.json").read_text(encoding="utf-8"))
    parts = []
    for journal in sorted(current.root.glob(f"{current.id}-p*.jsonl")):
        rows = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        first, last = rows[0], rows[-1]
        assert first["event"] == "part_start" and first["session_id"] == current.id
        assert last["event"] == "part_end" and last["part"] == first["part"]
        frames = list(capture.FRAME_ROW.iter_unpack(journal.with_suffix(".frames").read_bytes()))
        with wave.open(str(journal.with_suffix(".wav")), "rb") as source:
            assert (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, SAMPLE_RATE)
            pcm = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
        assert len(pcm) == last["sample_offset"] - first["sample_offset"]
        offset = first["sample_offset"]
        for row in frames:
            assert row[3] == offset
            offset += row[4]
        assert offset == last["sample_offset"]
        parts.append(NS(number=first["part"], start=first["sample_offset"],
                        end=last["sample_offset"], pcm=pcm, frames=frames, rows=rows[1:-1]))
    return manifest, parts


def finish(current):
    current.close()
    assert current.finished.wait(5)
    assert not current.failed
    return read_parts(current)


def synthetic_frame(seq, epoch=0):
    return NS(pcm=np.full(FRAME_SIZE, seq + 1, dtype=np.int16),
              seq=seq, device_epoch=epoch, t_capture=100 + seq * .1)


def test_pcm_and_packed_rows_join_across_wav_and_journal_rollover(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_SECONDS", .2)
    current = capture.Capture(tmp_path, {})
    for seq in range(7):
        evidence = capture.FrameEvidence(current, synthetic_frame(seq))
        evidence.probabilities(np.array([.01, seq / 10]))
        evidence.decision(.02, .005, .65, True, seq > 1)
        evidence.record()
    manifest, parts = finish(current)
    assert manifest["complete"] and manifest["samples"] == 7 * FRAME_SIZE
    assert [(p.number, p.start, p.end) for p in parts] == [
        (1, 0, 3200), (2, 3200, 6400), (3, 6400, 9600), (4, 9600, 11200)]
    assert np.array_equal(np.concatenate([p.pcm for p in parts]), np.repeat(range(1, 8), FRAME_SIZE))
    rows = [row for part in parts for row in part.frames]
    assert [row[0] for row in rows] == list(range(7))
    assert [row[7] for row in rows] == pytest.approx([.01, .1, .2, .3, .4, .5, .6])
    assert all(row[11] == 1 for row in rows)


def test_event_rollover_keeps_the_same_sample_timeline(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_EVENT_BYTES", 450)
    current = capture.Capture(tmp_path, {})
    capture.FrameEvidence(current, synthetic_frame(0)).record()
    for index in range(20):
        current.put("draft", {"chars": index})
    capture.FrameEvidence(current, synthetic_frame(1)).record()
    manifest, parts = finish(current)
    assert manifest["complete"] and manifest["parts"] > 2
    assert [row["chars"] for part in parts for row in part.rows] == list(range(20))
    assert [part.start for part in parts[1:]] == [part.end for part in parts[:-1]]
    assert [row[0] for part in parts for row in part.frames] == [0, 1]


def test_fragmented_chunk_spans_can_cross_journal_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_EVENT_BYTES", 2500)
    current = capture.Capture(tmp_path, {})
    capture.FrameEvidence(current, synthetic_frame(0)).record()
    spans = [[0, seq * 1600, seq * 1600 + 1] for seq in range(300)]
    current.put("chunk", {"chunk_id": "fixture", "ring_spans": spans})
    _, parts = finish(current)
    assert len(parts) >= 3
    rows = [row for part in parts for row in part.rows if "chunk_id" in row]
    assert all(row["chunk_id"] == "fixture" for row in rows)
    assert [row["first_span"] for row in rows[1:]] == [128, 256]
    assert [span for row in rows for span in row["ring_spans"]] == spans


def existing_part(root, session, timestamp, *, priority=False):
    (root / f"{session}.json").write_text(json.dumps({"priority": priority}), encoding="utf-8")
    path = root / f"{session}-p000001.wav"
    path.write_bytes(b"x" * 100)
    os.utime(path, (timestamp, timestamp))
    return path


def test_pruning_reserves_active_part_and_prioritizes_flagged(tmp_path, monkeypatch):
    ready = threading.Event()
    write = capture.Capture._write_frame

    def first_frame(self, part, frame):
        write(self, part, frame)
        ready.set()

    monkeypatch.setattr(capture.Capture, "_write_frame", first_frame)
    current = capture.Capture(tmp_path, {})
    capture.FrameEvidence(current, synthetic_frame(0)).record()
    assert ready.wait(5)
    active = tmp_path / f"{current.id}-p000001.wav"
    try:
        now = capture.time.time()
        expired = existing_part(tmp_path, "a" * 32, now - 3 * 86400)
        recent = existing_part(tmp_path, "b" * 32, now - 86400)
        flagged = existing_part(tmp_path, "c" * 32, now - 3 * 86400, priority=True)
        capture.prune(tmp_path, now=now)
        assert active.exists() and not expired.exists() and recent.exists() and flagged.exists()
        reserved = next(size for (root, _), size in capture._active_parts.items() if root == current.root)
        manifest_bytes = (tmp_path / f"{current.id}.json").stat().st_size
        capture.prune(tmp_path, now=now, max_bytes=reserved + manifest_bytes + 150)
        assert active.exists() and flagged.exists() and not recent.exists()
        capture.prune(tmp_path, now=now + 5 * 86400)
        assert active.exists() and not flagged.exists()
    finally:
        finish(current)


def test_long_session_prunes_its_own_old_parts_under_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_SECONDS", .1)
    monkeypatch.setattr(capture, "MAX_EVENT_BYTES", 500)
    monkeypatch.setattr(capture, "MAX_BYTES", 17_000)
    current = capture.Capture(tmp_path, {})
    for seq in range(12):
        capture.FrameEvidence(current, synthetic_frame(seq)).record()
    manifest, parts = finish(current)
    assert manifest["parts"] == 12
    assert manifest["incomplete_reason"] == "retention_pruned"
    assert parts[0].number > 1 and parts[-1].number == 12
    assert all(right.start == left.end for left, right in zip(parts, parts[1:]))
    assert sum(path.stat().st_size for path in tmp_path.iterdir()) <= capture.MAX_BYTES


def test_pruning_a_closed_session_marks_its_remaining_history_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "MAX_SECONDS", .1)
    current = capture.Capture(tmp_path, {})
    for seq in range(2):
        capture.FrameEvidence(current, synthetic_frame(seq)).record()
    manifest, _ = finish(current)
    assert manifest["complete"]
    now = capture.time.time()
    for path in tmp_path.glob(f"{current.id}-p000001.*"):
        os.utime(path, (now - 3 * 86400, now - 3 * 86400))
    capture.prune(tmp_path, now=now)
    manifest, parts = read_parts(current)
    assert [part.number for part in parts] == [2]
    assert not manifest["complete"] and manifest["incomplete_reason"] == "retention_pruned"


def test_live_vad_reports_the_probabilities_of_its_single_inference():
    # Compile just the production method to exercise the seam without bootstrapping dictation.
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    method = next(node for node in app_class.body if isinstance(node, ast.FunctionDef) and node.name == "_vad_is_speech")
    scope = {"np": np, "LIVE_VAD_PROB_THRESHOLD": .5, "resample_audio": lambda pcm, *_: pcm}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), scope)
    probabilities = np.array([.17, .83, .26])
    calls = []

    def infer(audio):
        calls.append(audio)
        return probabilities

    app = NS(_vad_available=True, _vad_model=object(), _vad_lock=threading.Lock(), _vad_probabilities=infer)
    evidence = capture.FrameEvidence(None, synthetic_frame(0))
    result = scope["_vad_is_speech"](app, np.zeros(FRAME_SIZE), SAMPLE_RATE, _diagnostic=evidence)
    assert result is True and len(calls) == 1 and evidence.peak == pytest.approx(.83)


@pytest.mark.parametrize("configured", [False, None])
def test_switch_off_skips_observers_thread_and_files(profile, monkeypatch, configured):
    def forbidden(*args, **kwargs):
        pytest.fail("The disabled listener reached the recorder")

    for name in ("observe_state", "observe_frame", "observe_chunk", "observe_dispatch", "Capture", "run_decode"):
        monkeypatch.setattr(capture, name, forbidden)
    replay = listener(monkeypatch)
    calls = install_fifo(replay, monkeypatch)
    if configured is not None:
        replay.app.config["diagnostics"] = {"handsfree_session_capture": configured}
    replay.feed(np.full(SAMPLE_RATE, .2))
    replay.feed(np.zeros(SAMPLE_RATE))
    assert len(replay.captures) == 1
    assert len(calls) == 2
    assert not hasattr(replay.app, "_handsfree_capture_1259")
    assert not profile.exists()


def test_disk_failure_is_reported_once_and_listener_keeps_flushing(profile, monkeypatch, caplog):
    def broken(self, pcm):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(wave.Wave_write, "writeframesraw", broken)
    replay = listener(monkeypatch)
    replay.app.config["diagnostics"] = {"handsfree_session_capture": True}
    replay.feed(np.zeros(FRAME_SIZE))
    current = replay.app._handsfree_capture_1259
    assert current.finished.wait(5) and current.failed
    replay.feed(np.full(SAMPLE_RATE, .2))
    replay.feed(np.zeros(SAMPLE_RATE))
    assert len(replay.captures) == 1
    messages = [row.message for row in caplog.records if "diagnostics writer failed" in row.message]
    assert messages == ["Hands-free diagnostics writer failed (OSError)"]
    manifest = json.loads((current.root / f"{current.id}.json").read_text(encoding="utf-8"))
    assert not manifest["complete"]


def install_fifo(replay, monkeypatch):
    app, consumer = replay.app, replay.consumer
    app.model_rate = SAMPLE_RATE
    manager = app._session_mode_manager
    manager._commit_redecode_fn = NS(__self__=app)
    manager._dispatch_lock = threading.RLock()
    manager.draft_document = NS(text="", revision=0)
    calls = []
    setattr(app, "_wake_consumer", consumer)
    app._vad_lock = threading.RLock()
    app._vad_probabilities = lambda pcm: np.array([.08])

    def score(pcm, *, src_rate, _diagnostic=None):
        assert src_rate == SAMPLE_RATE
        peak = .91 if np.max(np.abs(pcm)) > .1 else .03
        if _diagnostic is not None:
            _diagnostic.probabilities(np.array([.01, peak]))
        return peak > .5

    app._vad_is_speech = score

    def open_duck():
        app._hands_free_capture_ducker = object()
        return 1

    def close_duck(token):
        assert token == 1
        app._hands_free_capture_ducker = None

    app._open_hands_free_capture_duck = open_duck
    app._close_hands_free_capture_duck = close_duck
    monkeypatch.setattr(speech_presence._presence, "measure", lambda pcm, rate:
                        speech_presence.Presence(True, max_probability=.91))
    monkeypatch.setattr(kept_dictations, "_dispatch_session",
                        lambda *args: NS(kind="dictate_staged", detail={"text": "fixture output"}))

    def handle(frames, rate, **kwargs):
        audio = np.concatenate(frames)
        evidence = getattr(frames, "handsfree_evidence", None)
        original = lambda *args: None
        sink = evidence.gate_sink(original) if evidence is not None else original
        reject = speech_presence.skip_decode(audio, app, path="handsfree", evidence_sink=sink)
        calls.append(("reject" if reject else "pass", tuple(sorted(kwargs.items()))))
        if not reject:
            result = kept_dictations.dispatch_session(manager, "fixture", None, None, NS(mark=lambda key: None))
            calls.append(result.kind)

    app._handle_command_mode_utterance = handle
    original_enqueue = wake.WakeConsumer._enqueue_toggle_utterance

    def enqueue(frames, *args, **kwargs):
        replay.captures.append((replay.clock.now, np.concatenate(frames)))
        original_enqueue(consumer, frames, *args, **kwargs)

    consumer._enqueue_toggle_utterance = enqueue
    monkeypatch.setattr(wake.thread_registry, "spawn", lambda name, fn, **kwargs: fn())
    return calls


def run_scenario(monkeypatch, enabled, with_hold):
    replay = listener(monkeypatch)
    replay.app.config["diagnostics"] = {"handsfree_session_capture": enabled}
    calls = install_fifo(replay, monkeypatch)
    replay.feed(np.zeros(SAMPLE_RATE))
    replay.feed(np.full(SAMPLE_RATE * 2, .2))
    replay.feed(np.zeros(SAMPLE_RATE))
    if with_hold:
        replay.consumer.abort_utterance()
        replay.app._hotkey_recording = True
        replay.feed(np.full(SAMPLE_RATE * 2, .7))
        replay.app._hotkey_recording = False
        replay.feed(np.zeros(FRAME_SIZE))
    sentence = np.linspace(.21, .25, SAMPLE_RATE * 15, dtype=np.float32)
    replay.feed(sentence)
    replay.feed(np.zeros(SAMPLE_RATE))
    return replay, calls


@pytest.mark.parametrize("with_hold", [False, True])
def test_replay_on_off_has_identical_fifo_audio_and_dispatch(profile, monkeypatch, with_hold):
    monkeypatch.setattr(capture, "MAX_SECONDS", 3)
    off, off_calls = run_scenario(monkeypatch, False, with_hold)
    on, on_calls = run_scenario(monkeypatch, True, with_hold)
    assert off_calls == on_calls and len(on_calls) == 4
    assert [stamp for stamp, _ in off.captures] == [stamp for stamp, _ in on.captures]
    assert len(off.captures) == len(on.captures) == 2
    for (_, expected), (_, actual) in zip(off.captures, on.captures):
        assert np.array_equal(expected, actual)

    current = on.app._handsfree_capture_1259
    manifest, parts = finish(current)
    assert manifest["complete"] and len(parts) > 5
    rows = [row for part in parts for row in part.rows]
    frames = [frame for part in parts for frame in part.frames]
    by_chunk = defaultdict(list)
    for row in rows:
        if "chunk_id" in row:
            by_chunk[row["chunk_id"]].append(row)
    assert len(by_chunk) == 2
    pcm = np.concatenate([part.pcm for part in parts])
    by_ring = {(row[5], row[0]): pcm[row[3]:row[3] + row[4]] for row in frames}
    for (_, expected), (chunk, gate, dispatch, end) in zip(on.captures, by_chunk.values()):
        assert chunk["event"] == "chunk" and chunk["unmapped_samples"] == 0
        assert gate["verdict"] == "pass" and gate["duration_s"] == chunk["duration_s"]
        assert dispatch["delivered_chars"] == 14 and dispatch["delivered_words"] == 2
        assert end["status"] == "returned" and end["gate_observed"]
        restored = []
        for epoch, start, stop in chunk["ring_spans"]:
            while start < stop:
                seq, offset = divmod(start, FRAME_SIZE)
                width = min(FRAME_SIZE - offset, stop - start)
                restored.append(by_ring[epoch, seq][offset:offset + width])
                start += width
        assert np.array_equal(np.concatenate(restored).astype(np.float32) / 32767., expected)

    states = {row[10] for row in frames}
    events = [row["event"] for row in rows]
    assert {capture.IDLE, capture.ONSET, capture.UTTERANCE} <= states
    assert (capture.HOLD in states) is with_hold
    assert ("hold_press" in events and "hold_release" in events) is with_hold
    assert "duck_engage" in events and "duck_restore" in events
    assert all(row[7] == pytest.approx(.91) for row in frames if row[10] == capture.ONSET)
    assert all(math.isnan(row[7]) for row in frames if row[10] == capture.HOLD)
    closes = [row for row in rows if row["event"] == "close"]
    assert len(closes) == 2 and all(row["vad_peak"] == pytest.approx(.08) for row in closes)
    assert all(row[7] == pytest.approx(.03) for row in frames if row[0] in {close["seq"] for close in closes})
    assert "fixture output" not in json.dumps(rows)


def test_tts_rms_fallback_device_change_and_exact_park(profile, monkeypatch):
    replay = listener(monkeypatch)
    app = replay.app
    app.config["diagnostics"] = {"handsfree_session_capture": True}
    app.audio_coordinator = NS(is_speaking=True)
    frame = synthetic_frame(0)
    frame.pcm.fill(1638)
    replay.consumer._process_frame(frame)
    app.audio_coordinator.is_speaking = False
    replay.clock.now += .4
    app._vad_available = False
    frame = synthetic_frame(1, epoch=1)
    frame.pcm.fill(1638)
    replay.consumer._process_frame(frame)
    current = app._handsfree_capture_1259
    capture.begin_exit(current)
    app.command_mode_active = False
    assert capture.observe_state(app) is None
    assert not current.closed.is_set()
    capture.close(app, current=current, parked=56)
    manifest, parts = finish(current)
    first, second = parts[0].frames
    assert first[10] == capture.TTS and math.isnan(first[6])
    assert second[6] == pytest.approx(1638 / 32767) and second[8] == pytest.approx(.01)
    assert math.isnan(second[7]) and second[11] == 0
    rows = parts[0].rows
    device = next(row for row in rows if row["event"] == "mic_device_change")
    assert (device["old_epoch"], device["device_epoch"], device["sample_offset"]) == (0, 1, FRAME_SIZE)
    leave, parked = (next(row for row in rows if row["event"] == name) for name in ("leave", "parked_draft"))
    assert parked["chars"] == 56 and parked["observed_ns"] >= leave["observed_ns"]
    assert manifest["priority"] and manifest["incomplete_reason"] == "frame_discontinuity"


@pytest.mark.parametrize("reason", ["reject", "empty_transcription", "signoff_hallucination"])
def test_short_reject_is_flagged_and_late_result_stays_with_its_session(tmp_path, reason):
    current = capture.Capture(tmp_path, {})
    capture.FrameEvidence(current, synthetic_frame(0)).record()
    chunk = current.add_chunk({"chunk_id": "fixture", "samples": SAMPLE_RATE, "duration_s": 1})
    assert chunk is not None
    current.close()
    verdict = "reject" if reason == "reject" else "pass"
    chunk.gate_sink(lambda *args: None)(None, np.zeros(SAMPLE_RATE), SAMPLE_RATE, verdict)
    if reason != "reject":
        chunk.drop(reason, 1.)
    chunk.finish("returned")
    manifest, parts = finish(current)
    assert manifest["priority"] and manifest["complete"]
    gate, end = [row for row in parts[0].rows if row["event"] in ("gate", "decode_end")]
    assert gate["chunk_id"] == end["chunk_id"] == "fixture" and gate["verdict"] == verdict
    assert end["delivered_chars"] == end["delivered_words"] == 0
