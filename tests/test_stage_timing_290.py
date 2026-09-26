"""Focused tests for the opt-in stage timing journal and report CLI."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import threading
import time
import types

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.stage_timing import (
    STAGES, StageTiming, reset_current_stage_timing, set_current_stage_timing,
)
from tools.stage_timing_report import load_records, render_report


def _write_complete_record(root, route: str, mode: str, audio_s: float, words: int):
    timing = StageTiming.create(route, mode, enabled=True, home_dir=root)
    assert timing is not None
    timing.mark("speech_end")
    timing.mark("capture_finalised", audio_s=audio_s)
    timing.mark("transcribe_start", model="small", beam=5)
    timing.mark("transcribe_end", words=words)
    timing.mark("dispatch_start")
    timing.mark("action_done")
    timing.complete(outcome="ok")


def test_stage_timing_defaults_off_and_creates_no_file(tmp_path):
    assert SETTINGS_SCHEMA["diagnostics.stage_timing"]["default"] is False
    timing = StageTiming.create("hold", "dictate", enabled=False, home_dir=tmp_path)
    assert timing.outcome is None
    assert timing.last_voice_source is None
    assert timing.endpoint_wait_ms is None
    timing.mark("speech_end")
    timing.complete()
    assert not (tmp_path / "perf" / "stage_timing.jsonl").exists()


def test_flag_on_writes_one_line_with_all_timestamps_and_fields(tmp_path):
    _write_complete_record(tmp_path, "hold", "dictate", 1.4, 4)
    path = tmp_path / "perf" / "stage_timing.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert (row["route"], row["mode"], row["audio_s"], row["words"]) == (
        "hold", "dictate", 1.4, 4,
    )
    assert (row["model"], row["beam"], row["outcome"]) == ("small", 5, "ok")
    assert set(row["timestamps_ns"]) == set(STAGES)
    assert set(row["timestamps_utc"]) == set(STAGES)
    assert all(row["timestamps_ns"][stage] is not None for stage in STAGES
               if stage != "last_voice")
    assert set(row["deltas_ms"]) == {
        "endpoint_wait_ms", "capture_finalize_ms", "pre_transcribe_ms", "transcribe_ms",
        "post_transcribe_ms", "action_ms", "feedback_ms", "total_ms",
    }
    assert row["deltas_ms"]["endpoint_wait_ms"] is None
    assert all(value is not None for key, value in row["deltas_ms"].items()
               if key != "endpoint_wait_ms")


def test_endpoint_wait_outcome_and_segment_index_serialize(tmp_path):
    timing = StageTiming.create("hold", "dictate", enabled=True, home_dir=tmp_path)
    timing.mark("last_voice", last_voice_source="key_release")
    timing.mark("speech_end", segment_index=2)
    timing.complete(outcome="typed")
    row = json.loads((tmp_path / "perf" / "stage_timing.jsonl").read_text(encoding="utf-8"))
    assert row["deltas_ms"]["endpoint_wait_ms"] >= 0
    assert row["last_voice_source"] == "key_release"
    assert row["segment_index"] == 2
    assert row["outcome"] == "typed"


def test_mark_at_records_known_endpoint_wait(tmp_path):
    timing = StageTiming.create("wake", "dictate", enabled=True, home_dir=tmp_path)
    timing.mark_at("last_voice", 1_000_000_000, last_voice_source="vad")
    timing.mark_at("speech_end", 1_875_000_000)
    assert timing.endpoint_wait_ms == 875.0
    timing.complete()
    row = json.loads((tmp_path / "perf" / "stage_timing.jsonl").read_text(encoding="utf-8"))
    assert row["deltas_ms"]["endpoint_wait_ms"] == 875.0
    assert row["last_voice_source"] == "vad"


def test_stop_recording_with_disabled_timing_uses_noop_surface():
    """Exercise the real stop wrapper body without importing dictation.py."""
    source = Path(__file__).resolve().parents[1] / "dictation.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    app_class = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "DictationApp")
    stop_method = next(node for node in app_class.body
                       if isinstance(node, ast.FunctionDef) and node.name == "stop_recording")
    module = types.ModuleType("dictation_stop_recording_test")
    module.__dict__.update(
        StageTiming=StageTiming,
        set_current_stage_timing=set_current_stage_timing,
        reset_current_stage_timing=reset_current_stage_timing,
    )
    exec(compile(ast.Module(body=[stop_method], type_ignores=[]), str(source), "exec"), module.__dict__)
    app = types.SimpleNamespace(
        recording=False,
        _stage_timing_enabled=False,
        _streaming_session=None,
        _hold_capture_lifecycle_lock=threading.RLock(),
        _stop_recording_impl=lambda **kwargs: None,
        _restore_audio=lambda: None,
        _close_hold_capture_duck=lambda: None,
    )
    module.stop_recording(app)


def test_feedback_completion_waits_for_nonblocking_audio_callback(tmp_path):
    timing = StageTiming.create("handsfree", "command", enabled=True, home_dir=tmp_path)
    timing.mark("speech_end")
    timing.mark("capture_finalised", audio_s=0.8)
    timing.mark("transcribe_start", model="small", beam=5)
    timing.mark("transcribe_end", words=2)
    timing.mark("dispatch_start")
    timing.mark("action_done")
    timing.feedback_started()
    timing.complete()
    path = tmp_path / "perf" / "stage_timing.jsonl"
    assert not path.exists()

    timing.feedback_finished()
    deadline = time.monotonic() + 1.0
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert path.exists()
    rows = path.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    assert json.loads(rows[0])["timestamps_ns"]["feedback_done"] is not None


def test_report_parses_fixture_jsonl_and_prints_route_stage_statistics(tmp_path):
    _write_complete_record(tmp_path, "hold", "dictate", 1.4, 4)
    _write_complete_record(tmp_path, "handsfree", "command", 0.8, 2)
    path = tmp_path / "perf" / "stage_timing.jsonl"
    records = load_records(path)
    report = render_report(records)
    assert "hold" in report and "transcribe_ms" in report
    assert "handsfree" in report
    assert "outcomes: ok=1" in report
    assert "endpoint_wait_ms" in report  # old fixture has no last_voice mark
    assert "Top stages by share of total observed latency:" in report
    assert len(records) == 2


def test_report_fixture_endpoint_wait_and_outcome_counts(tmp_path):
    path = tmp_path / "stage_timing.jsonl"
    rows = [
        {"route": "wake", "outcome": "typed", "deltas_ms": {"endpoint_wait_ms": 750, "total_ms": 900}},
        {"route": "wake", "outcome": "typed", "deltas_ms": {"endpoint_wait_ms": 1000, "total_ms": 1200}},
        {"route": "wake", "outcome": "miss", "deltas_ms": {"endpoint_wait_ms": None, "total_ms": 500}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    report = render_report(load_records(path))
    assert "endpoint_wait_ms" in report
    assert "outcomes: miss=1, typed=2" in report
