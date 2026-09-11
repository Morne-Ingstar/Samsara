"""Exercise the real key polling seam without importing/starting the app."""
import ast
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import flight_recorder


@pytest.fixture
def polling(monkeypatch):
    source_path = Path(__file__).resolve().parents[1] / "dictation.py"
    source = source_path.read_text(encoding="utf-8")
    start = source.index("_VK_BY_NAME =")
    end = source.index("\nfrom pynput.mouse", start)
    recorder = SimpleNamespace(record=Mock())
    logger = Mock()
    namespace = {"flight_recorder": recorder, "logger": logger}
    exec(compile(ast.parse(source[start:end]), str(source_path), "exec"), namespace)

    ga = Mock(return_value=0)
    monkeypatch.setattr(
        ctypes, "windll",
        SimpleNamespace(user32=SimpleNamespace(GetAsyncKeyState=ga)),
        raising=False,
    )
    return SimpleNamespace(
        poll=namespace["_raw_key_pressed"], ga=ga, recorder=recorder, logger=logger,
    )


@pytest.mark.parametrize("bits", [0, 1, 0x8000, -32768])
def test_steady_state_is_silent_without_diagnostic_rereads(polling, bits):
    polling.ga.return_value = bits
    for _ in range(1000):
        assert polling.poll("ctrl") is bool(bits & 0x8000)
    polling.recorder.record.assert_not_called()
    # All three modifier VKs are read when up; any() short-circuits when down.
    assert polling.ga.call_count == (1000 if bits & 0x8000 else 3000)


def test_each_observed_release_emits_exactly_once_and_rearms(polling):
    for releases in (1, 2):
        polling.ga.return_value = 0x8000
        for _ in range(20):
            assert polling.poll("ctrl") is True
        assert polling.recorder.record.call_count == releases - 1

        polling.ga.return_value = 0
        for _ in range(1000):
            assert polling.poll("ctrl") is False
        assert polling.recorder.record.call_count == releases
        polling.recorder.record.assert_called_with(
            "key_state.not_pressed", key="ctrl",
            raw_bits={"0x11": 0, "0xa2": 0, "0xa3": 0},
        )


def test_keys_have_independent_transitions(polling):
    down = {0xA3, 0xA1}  # Right ctrl/shift must count even if generic VK is up.
    polling.ga.side_effect = lambda vk: 0x8000 if vk in down else 0
    assert polling.poll("ctrl") is True
    assert polling.poll("shift") is True
    down.remove(0xA3)
    assert polling.poll("ctrl") is False
    assert polling.poll("shift") is True
    assert polling.poll("ctrl") is False
    assert polling.recorder.record.call_count == 1
    down.clear()
    assert polling.poll("shift") is False
    assert polling.poll("shift") is False
    assert [call.kwargs["key"] for call in polling.recorder.record.call_args_list] == [
        "ctrl", "shift",
    ]


@pytest.mark.parametrize("pressed_name,released_name", [
    ("ctrl", " CTRL "), ("escape", "esc"), ("caps lock", "capslock"),
])
def test_aliases_share_transition_state_and_preserve_record_key(
    polling, pressed_name, released_name,
):
    polling.ga.return_value = 0x8000
    assert polling.poll(pressed_name) is True
    polling.ga.return_value = 0
    assert polling.poll(released_name) is False
    assert polling.poll(pressed_name) is False
    polling.recorder.record.assert_called_once()
    assert polling.recorder.record.call_args.kwargs["key"] == released_name


def test_unknown_key_stays_silent_and_warns_once(polling):
    for _ in range(100):
        assert polling.poll("unmapped key") is False
    polling.ga.assert_not_called()
    polling.recorder.record.assert_not_called()
    polling.logger.warning.assert_called_once()


def test_diagnostic_reread_does_not_change_release_verdict(polling):
    polling.ga.return_value = 0x8000
    assert polling.poll("ctrl") is True
    # The key changes again between the verdict and its diagnostic re-read.
    polling.ga.side_effect = [0, 0, 0, -32768, 1, 0]
    assert polling.poll("ctrl") is False
    polling.recorder.record.assert_called_once_with(
        "key_state.not_pressed", key="ctrl",
        raw_bits={"0x11": -32768, "0xa2": 1, "0xa3": 0},
    )
    polling.ga.side_effect = None
    polling.ga.return_value = 0
    assert polling.poll("ctrl") is False
    assert polling.recorder.record.call_count == 1


def test_release_jsonl_schema_unchanged(polling, tmp_path, monkeypatch):
    monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
    polling.recorder.record = flight_recorder.record
    polling.ga.return_value = 0x8000
    assert polling.poll("ctrl") is True
    polling.ga.return_value = 0
    assert polling.poll("ctrl") is False
    assert polling.poll("ctrl") is False

    lines = [
        line
        for path in (tmp_path / "flight").glob("events-*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert set(event) == {
        "event", "t_wall", "t_wall_iso", "t_mono", "thread", "key", "raw_bits",
    }
    assert event["event"] == "key_state.not_pressed"
    assert event["key"] == "ctrl"
    assert event["raw_bits"] == {"0x11": 0, "0xa2": 0, "0xa3": 0}
    assert isinstance(event["t_wall"], float)
    assert isinstance(event["t_mono"], float)
    assert isinstance(event["t_wall_iso"], str)
    assert isinstance(event["thread"], str)
