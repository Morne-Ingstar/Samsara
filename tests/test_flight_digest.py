"""Tests for tools/flight_digest.py against synthetic record fixtures.

Never touches the real flight recorder location -- every test writes its
own JSONL fixtures under pytest's tmp_path and points the tool at that
directory explicitly.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import flight_digest  # noqa: E402


def _write_events(flight_dir: Path, stamp: str, records: list) -> None:
    flight_dir.mkdir(parents=True, exist_ok=True)
    path = flight_dir / f"events-{stamp}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _rec(event: str, t_wall: float, **fields) -> dict:
    from datetime import datetime, timezone

    return {
        "event": event,
        "t_wall": t_wall,
        "t_wall_iso": datetime.fromtimestamp(t_wall, tz=timezone.utc).isoformat(),
        "t_mono": t_wall,
        "thread": "MainThread",
        **fields,
    }


def _full_fixture(now: float) -> list:
    """One of everything build_digest()/render_markdown() sections cover."""
    day0 = now
    day1 = now - 86400
    return [
        # injection outcomes: clipboard/typed x ok/failed/cancelled, with why
        _rec("inject", day0, path="typed", why="under_paste_min_chars", result="ok",
             target_process="chrome.exe", chars=12),
        _rec("inject", day0, path="typed", why="under_paste_min_chars",
             result="failed_falling_back_to_clipboard", target_process="warp.exe", chars=5),
        _rec("inject", day0, path="typed", why="under_paste_min_chars",
             result="cancelled_target_changed", target_process="explorer.exe", chars=3),
        _rec("inject", day1, path="clipboard", why="over_paste_min_chars", result="ok",
             target_process="notepad.exe", chars=400),
        _rec("inject", day1, path="clipboard", why="typed_failed", result="failed",
             target_process="notepad.exe", chars=40),

        # hotkey vs hands-free dictation counts
        _rec("hold_recording.start", day0, trigger="hotkey_batch", mode="hold"),
        _rec("hold_recording.start", day0, trigger="hotkey_batch", mode="hold"),
        _rec("hold_recording.start", day1, trigger="capslock_stream", mode="hold"),

        # session.dispatch / wake.* proxies for hands-free metrics
        _rec("session.dispatch", day0, op="started", kind="toggle_command"),
        _rec("session.dispatch", day0, op="completed", kind="toggle_command", exc=False),
        _rec("session.dispatch", day1, op="completed", kind="ava_command", exc=True),
        _rec("session.dispatch", day1, op="dropped", kind="oww_gate_no_hit"),
        _rec("wake.session_open", day0, app_state="asleep"),
        _rec("wake.session_close", day0, reason="silence_flush", speech_s=1.2),
        _rec("wake.oww_confirm", day0, app_state="asleep"),

        # a non-inject, non-exc record with a failure-flavored 'result' that
        # should NOT be misclassified as error/warn (only 'inject' and
        # 'wake.fallback_decode_end' results are interpreted that way)
        _rec("ducker.op", day0, op="start", result="ok"),

        # explicit error-signal events
        _rec("ducker_child_busy", day0, pending_op="stop"),
        _rec("wake.fallback_decode_end", day1, result="paste_failed", text_len=10),

        # a record carrying dictated text content -- must never surface
        _rec("hold_recording.decoded", day0, audio_duration_ms=900, text_len=11,
             text_preview="hello world", low_confidence=False),
    ]


def test_total_by_event_and_percentages(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    assert digest["record_count"] == len(_full_fixture(now))
    assert digest["total_by_event"]["inject"] == 5
    assert digest["total_by_event"]["hold_recording.start"] == 3


def test_injection_outcomes_matrix_and_why_breakdown(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    assert digest["inject_totals"][("typed", "ok")] == 1
    assert digest["inject_totals"][("typed", "failed")] == 1
    assert digest["inject_totals"][("typed", "cancelled")] == 1
    assert digest["inject_totals"][("clipboard", "ok")] == 1
    assert digest["inject_totals"][("clipboard", "failed")] == 1

    why_counter = digest["inject_matrix"][("clipboard", "failed")]
    assert why_counter["typed_failed"] == 1


def test_hotkey_vs_hands_free_counts(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    assert digest["trigger_counts"]["hotkey"] == 2
    assert digest["trigger_counts"]["hands_free"] == 1


def test_hands_free_session_proxies_derived(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    toggle = digest["dispatch_by_kind_op"]["toggle_command"]
    assert toggle["started"] == 1
    assert toggle["completed"] == 1
    assert digest["dispatch_exceptions"]["ava_command"] == 1
    assert digest["wake_close_reasons"]["silence_flush"] == 1
    assert digest["oww_confirm_count"] == 1


def test_error_warn_classification(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    events = {rec["event"] for rec in digest["error_warn_records"]}
    # failed/cancelled inject + explicit exc + known error events +
    # wake.fallback_decode_end's non-ok result
    assert "inject" in events
    assert "session.dispatch" in events
    assert "ducker_child_busy" in events
    assert "wake.fallback_decode_end" in events
    # the healthy inject(ok), ducker.op(result='ok'), and dropped-without-exc
    # session.dispatch must NOT be misclassified as error/warn.
    ok_inject_present = any(
        r["event"] == "inject" and r.get("result") == "ok" for r in digest["error_warn_records"]
    )
    assert not ok_inject_present
    assert not any(r["event"] == "ducker.op" for r in digest["error_warn_records"])


def test_day_by_day_table_has_two_days(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    _write_events(flight_dir, "20990101", _full_fixture(now))

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)

    assert len(digest["day_totals"]) == 2


def test_days_window_excludes_old_records(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    old = now - (30 * 86400)
    _write_events(flight_dir, "20990101", [
        _rec("inject", now, path="clipboard", result="ok"),
        _rec("inject", old, path="clipboard", result="ok"),
    ])

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    assert len(records) == 1
    assert records[0]["t_wall"] == now


def test_malformed_lines_are_skipped(tmp_path):
    flight_dir = tmp_path / "flight"
    flight_dir.mkdir(parents=True)
    path = flight_dir / "events-20990101.jsonl"
    now = time.time()
    with path.open("a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write(json.dumps(_rec("inject", now, path="clipboard", result="ok")) + "\n")
        f.write("\n")
        f.write('["not", "a", "dict"]\n')

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    assert len(records) == 1
    assert records[0]["event"] == "inject"


def test_missing_flight_dir_yields_nothing(tmp_path):
    records = list(flight_digest.iter_records(tmp_path / "no_such_flight_dir", days=7))
    assert records == []


# ---------------------------------------------------------------------------
# The privacy-critical test: dictated text content must never surface.
# ---------------------------------------------------------------------------

def test_text_fields_never_appear_in_output(tmp_path):
    now = time.time()
    flight_dir = tmp_path / "flight"
    secret = "the launch codes are 12345 do not repeat this"
    _write_events(flight_dir, "20990101", [
        _rec("hold_recording.decoded", now, audio_duration_ms=900, text_len=len(secret),
             text_preview=secret[:12], low_confidence=False),
        # a hypothetical future call site passing a full 'text' field --
        # the allowlist must drop this even though it isn't a real call
        # site in this repo today.
        _rec("hold_recording.decoded", now, audio_duration_ms=900, text=secret),
    ])

    records = list(flight_digest.iter_records(flight_dir, days=7, now_ts=now))
    digest = flight_digest.build_digest(records)
    markdown = flight_digest.render_markdown(digest, days=7)

    assert secret not in markdown
    assert secret[:12] not in markdown
    assert "text_preview" not in markdown
    assert "\"text\"" not in markdown

    # _redact() itself must strip these fields regardless of section.
    for rec in records:
        redacted = flight_digest._redact(rec)
        assert "text" not in redacted
        assert "text_preview" not in redacted


def test_redact_is_allowlist_not_blocklist():
    rec = {"event": "inject", "result": "ok", "some_future_unlisted_field": "danger"}
    redacted = flight_digest._redact(rec)
    assert "some_future_unlisted_field" not in redacted
    assert redacted["event"] == "inject"
    assert redacted["result"] == "ok"


def test_render_markdown_runs_end_to_end_on_empty_input():
    digest = flight_digest.build_digest([])
    markdown = flight_digest.render_markdown(digest, days=7)
    assert "Total records: **0**" in markdown
    assert "No `inject` records in this window." in markdown
