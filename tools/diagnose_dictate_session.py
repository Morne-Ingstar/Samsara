"""Batch diagnostics for toggle-session DICTATE mode.

Read-only: tails the live rotating log from a negative byte offset, reports
the active persisted timeout values, identifies the relevant source guards,
and replays the pure SessionModeManager focus-lock transition.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault(
    "SAMSARA_HOME_DIR", str(Path(tempfile.gettempdir()) / "samsara_dictate_probe")
)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
)


LIVE_LOG = Path.home() / ".samsara" / "logs" / "samsara.log"
CONFIG = Path.home() / ".samsara" / "config.json"
TAIL_BYTES = 1_500_000


def tail_text(path: Path, byte_count: int) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        handle.seek(max(0, handle.tell() - byte_count))
        data = handle.read()
    return data.decode("utf-8", errors="replace")


def source_line(relative: str, needle: str) -> tuple[int, str]:
    path = ROOT / relative
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if needle in line:
            return number, line.strip()
    raise RuntimeError(f"{needle!r} not found in {relative}")


def print_live_repro() -> None:
    print("== LIVE REPRO EVENTS ==")
    text = tail_text(LIVE_LOG, TAIL_BYTES)
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if "[CMD MODE] Entering command mode" in line]
    if not starts:
        print("No toggle-session entry found in live-log tail")
        return
    start = starts[-1]
    interesting = (
        "[CMD MODE]", "[CMD-UTT]", "[SESSION]", "[CAP]",
        "[GATE]", "[GUARD]", "[QUALITY]", "[LONG]",
    )
    for line in lines[start:]:
        if any(marker in line for marker in interesting):
            print(line)


def print_config() -> None:
    print("\n== PERSISTED SESSION CONFIG (READ ONLY) ==")
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    command = cfg.get("command_mode", {})
    for key in (
        "mode",
        "inactivity_timeout_s",
        "utterance_silence_s",
        "dictate_utterance_silence_s",
        "miss_limit",
    ):
        print(f"command_mode.{key}={command.get(key)!r}")


def print_source_facts() -> None:
    print("\n== SOURCE FACTS ==")
    checks = (
        ("dictation.py", "timeout_s = cm_cfg.get('inactivity_timeout_s', 30)"),
        ("dictation.py", "Another transcription is active — waiting on model lock"),
        ("samsara/audio_engine/wake_consumer.py", "_enqueue_toggle_utterance"),
        ("samsara/audio_engine/wake_consumer.py", "if buffer_s >= 7.0"),
        ("samsara/audio_engine/wake_consumer.py", "and not app.command_mode_active"),
        ("samsara/session_modes.py", "self._stage_buffer += to_inject"),
        ("samsara/config_schema.py", '"max": 1800'),
        ("samsara/ui/settings_qt.py", "timeout_spin.setRange(5, 1800)"),
    )
    for relative, needle in checks:
        number, line = source_line(relative, needle)
        print(f"{relative}:{number}: {line}")


def replay_focus_transition() -> None:
    print("\n== PURE MODE-CHANGE REPLAY ==")
    foreground = {"process": "editor.exe"}
    transitions: list[str] = []
    injected: list[str] = []
    prior = {"mode": SessionMode.COMMAND}

    def on_mode_change(mode: SessionMode) -> None:
        transitions.append(f"{prior['mode'].value}->{mode.value}")
        prior["mode"] = mode

    manager = SessionModeManager(
        abort_phrases=["cancel", "abort"],
        foreground_exe_resolver=lambda: foreground["process"],
        foreground_hwnd_resolver=lambda: 1,
        inject_fn=injected.append,
        remove_chars_fn=lambda _n: None,
        command_dispatch_fn=lambda text: CommandDispatchResult(False, text),
        agent_dispatch_fn=lambda _text, _context: None,
        on_mode_change=on_mode_change,
    )
    signals = UtteranceSignals(True, (1.0,))
    manager.dispatch_utterance("dictate mode", signals)
    manager.dispatch_utterance("first complete chunk", signals)
    manager.dispatch_utterance("second complete chunk", signals)
    print(f"before_focus_change mode={manager.mode.value} transitions={transitions}")
    print(f"stage_buffer={manager.stage_buffer!r} injected={injected!r}")
    foreground["process"] = "other.exe"
    outcome = manager.dispatch_utterance("suppressed chunk", signals)
    print(
        "after_focus_change "
        f"mode={manager.mode.value} outcome={outcome.kind} transitions={transitions}"
    )


def main() -> None:
    print_live_repro()
    print_config()
    print_source_facts()
    replay_focus_transition()


if __name__ == "__main__":
    main()
