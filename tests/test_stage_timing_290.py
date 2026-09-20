"""Focused tests for the opt-in stage timing journal and report CLI."""
from __future__ import annotations

import json
import time

from samsara.config_schema import SETTINGS_SCHEMA
from samsara.stage_timing import STAGES, StageTiming
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
    assert all(row["timestamps_ns"][stage] is not None for stage in STAGES)
    assert set(row["deltas_ms"]) == {
        "capture_finalize_ms", "pre_transcribe_ms", "transcribe_ms",
        "post_transcribe_ms", "action_ms", "feedback_ms", "total_ms",
    }
    assert all(value is not None for value in row["deltas_ms"].values())


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
    assert "Top stages by share of total observed latency:" in report
    assert len(records) == 2
