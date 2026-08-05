"""Targeted tests for samsara/flight_recorder.py and one instrumented seam.

Run in isolation (not the full suite):
    python -m pytest tests/test_flight_recorder.py -v
"""
import json
import shutil
import threading
from unittest.mock import Mock, patch

from samsara import flight_recorder


def test_concurrent_writes_produce_valid_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    events_per_thread = 25
    thread_count = 8

    def worker(idx):
        for seq in range(events_per_thread):
            flight_recorder.record("test.concurrent", thread_idx=idx, seq=seq)

    threads = [threading.Thread(target=worker, args=(idx,)) for idx in range(thread_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    files = list((tmp_path / "flight").glob("events-*.jsonl"))
    assert files, "expected at least one rotated events file"

    lines = []
    for f in files:
        lines.extend(f.read_text(encoding="utf-8").splitlines())

    assert len(lines) == thread_count * events_per_thread

    seen = set()
    for line in lines:
        obj = json.loads(line)  # must not raise -- every line is valid JSON
        assert obj["event"] == "test.concurrent"
        assert "t_wall" in obj and "t_mono" in obj and "thread" in obj
        seen.add((obj["thread_idx"], obj["seq"]))

    assert len(seen) == thread_count * events_per_thread


def test_record_never_raises_when_directory_deleted_mid_run(tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))

    flight_recorder.record("test.before_delete")
    shutil.rmtree(tmp_path / "flight")

    flight_recorder.record("test.after_delete")  # must not raise

    files = list((tmp_path / "flight").glob("events-*.jsonl"))
    assert files
    lines = files[0].read_text(encoding="utf-8").splitlines()
    events = [json.loads(line)["event"] for line in lines]
    assert "test.after_delete" in events


def test_hold_stop_records_reason_field(monkeypatch):
    """dictation.py's on_key_release seam (release-verification) must record
    which check ended the recording -- see cdc39ea's spurious-release
    suspect list this module exists to catch evidence for."""
    from dictation import DictationApp

    app = DictationApp.__new__(DictationApp)
    app.config = {
        "hotkey": "ctrl+shift",
        "continuous_hotkey": "ctrl+alt+d",
        "wake_word_hotkey": "ctrl+alt+w",
        "command_hotkey": "ctrl+alt+c",
        "mode": "hold",
    }
    app.current_keys = set()
    app.get_key_name = Mock(return_value="shift")
    app._check_command_mode_key = Mock()
    app.check_hotkey_state = Mock(return_value=False)
    app.hotkey_pressed = True
    app.recording = True
    app.command_mode_recording = False
    app._stop_in_flight = False

    recorded = []
    monkeypatch.setattr(
        "dictation.flight_recorder.record",
        lambda event, **fields: recorded.append((event, fields)),
    )

    with patch("dictation.thread_registry.spawn"):
        app.on_key_release("shift")

    stop_events = [fields for event, fields in recorded if event == "hold_recording.stop_triggered"]
    assert stop_events, f"expected a hold_recording.stop_triggered event, got {recorded}"
    assert stop_events[0]["reason"] == "main_hotkey_release"
