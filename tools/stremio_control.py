"""Shared Stremio control layer -- AutoHotkey v1 window-focus + key-send.

Extracted from plugins/commands/stremio.py (2026-07-10) so the voice-command
plugin and the standalone LAN phone remote (tools/stremio_remote.py) share
ONE implementation instead of two copies drifting apart. Zero imports from
the samsara package or plugins/ -- stdlib only, so this module works from a
bare `python tools/stremio_remote.py` with no Samsara app running.

Win32 SendInput does NOT work on Stremio (Electron) -- AutoHotkey v1's
title-match WinActivate/WinWaitActive/Send is the only verified-working
injection path (empirically confirmed against the current Stremio build,
2026-07-10). Do not attempt SendInput/pyautogui here.

Stremio's process name changed from stremio.exe to stremio-shell-ng.exe.
The old name in the original plugin's taskkill was a latent bug (fixed in
the plugin, which now imports from here).
"""

import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time

from samsara.runtime import thread_registry

logger = logging.getLogger(__name__)

AHK_EXE = r'C:\Program Files\AutoHotkey\v1.1.37.02\AutoHotkeyU64.exe'

# stremio-runtime.exe is a companion process some builds also spawn --
# kill both if present. See is_stremio_running() / kill_stremio().
STREMIO_PROCESS_NAMES = ("stremio-shell-ng.exe", "stremio-runtime.exe")

# Owner's verified arrow step. Stremio makes both arrow steps configurable;
# Shift+arrow is a separate setting, not a guaranteed larger jump.
SEEK_STEP_SECONDS = 10
DEFAULT_SEEK_SECONDS = 10
MAX_SEEK_SECONDS = 30 * 60

_SMALL_NUMBERS = dict(zip(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
_TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))


def _spoken_number(text: str) -> float:
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)
    if text in ("a", "an", "one"):
        return 1
    if text in ("a couple", "a couple of", "couple", "couple of"):
        return 2
    if text in ("half", "half a", "half an"):
        return 0.5
    words = text.replace("-", " ").split()
    # Deliberately a number grammar, not a bag of number-like words.
    def under_hundred(parts):
        if len(parts) == 1:
            return _SMALL_NUMBERS.get(parts[0], _TENS.get(parts[0]))
        if len(parts) == 2 and parts[0] in _TENS and parts[1] in _SMALL_NUMBERS:
            unit = _SMALL_NUMBERS[parts[1]]
            if 1 <= unit <= 9:
                return _TENS[parts[0]] + unit
        return None

    value = under_hundred(words)
    if value is None and len(words) >= 2 and words[1] in ("hundred", "thousand"):
        leading = _SMALL_NUMBERS.get(words[0], 1 if words[0] == "a" else None)
        if leading is not None and 1 <= leading <= 9:
            tail = words[2:]
            if tail[:1] == ["and"]:
                tail = tail[1:]
            rest = under_hundred(tail) if tail else 0
            if rest is not None:
                value = leading * (100 if words[1] == "hundred" else 1000) + rest
    if value is None:
        raise ValueError("Say a number of seconds or minutes")
    return value


def seek_presses(seconds: float) -> int:
    """Round to the nearest arrow tap (ties up), bounded to 1..180."""
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Seek amount must be positive and finite")
    return max(1, math.floor(min(seconds, MAX_SEEK_SECONDS) / SEEK_STEP_SECONDS + 0.5))


def parse_seek_seconds(remainder: str) -> int:
    """Parse a complete spoken duration; missing units mean seconds."""
    text = remainder.lower().strip().rstrip(".!?")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(?:\s+)?stremio$", "", text).strip()
    if not text:
        return DEFAULT_SEEK_SECONDS
    text = re.sub(r"^by\s+", "", text)
    match = re.fullmatch(r"(.+?)(?:\s+(seconds?|secs?|minutes?|mins?))?", text)
    if match is None:
        raise ValueError("Say a number of seconds or minutes")
    amount, unit = match.groups()
    seconds = _spoken_number(amount) * (60 if unit and unit.startswith("min") else 1)
    return seek_presses(seconds) * SEEK_STEP_SECONDS


# ── AHK script templates (pure string builders -- unit-testable without
#    ever invoking AHK_EXE; see tests/test_stremio_control.py) ───────────────

def _build_key_script(key: str) -> str:
    """AHK v1 script: activate Stremio by title, send one key."""
    return f"""#NoEnv
#SingleInstance Force
SetTitleMatchMode, 2
WinActivate, Stremio
WinWaitActive, Stremio,, 2
if ErrorLevel
    ExitApp, 1
Sleep, 150
Send, {{{key}}}
ExitApp, 0
"""


def _build_send_body_script(send_body: str) -> str:
    """AHK v1 script: activate Stremio by title, run an arbitrary Send
    statement (for multi-key sequences like `Send, {Right 6}`)."""
    return f"""#NoEnv
#SingleInstance Force
SetTitleMatchMode, 2
WinActivate, Stremio
WinWaitActive, Stremio,, 2
if ErrorLevel
    ExitApp, 1
Sleep, 150
{send_body}
ExitApp, 0
"""


# ── AHK execution ─────────────────────────────────────────────────────────────

def _run_ahk(script: str) -> bool:
    """Write and execute a one-shot AHK v1 script.

    Returns True when AHK exits 0 (Stremio window found and key sent).
    Returns False when the window wasn't found (ExitApp, 1), AHK itself
    failed to run, or the script timed out -- callers treat any False the
    same way: "stremio not found".
    """
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.ahk', delete=False, encoding='utf-8'
    )
    tmp.write(script)
    tmp.close()
    try:
        result = subprocess.run(
            [AHK_EXE, tmp.name],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace')
            logger.debug(f"[STREMIO] AHK error (rc={result.returncode}): {stderr[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        logger.debug("[STREMIO] AHK script timed out")
        return False
    except Exception as e:
        logger.debug(f"[STREMIO] AHK failed: {e}")
        return False
    finally:
        try:
            os.unlink(tmp.name)
        except Exception as e:
            logger.debug(f"_run_ahk cleanup: {e}")


def _send_stremio_key(key: str) -> bool:
    logger.debug(f"[STREMIO] AHK sending key: {key}")
    return _run_ahk(_build_key_script(key))


def _send_stremio_send_body(send_body: str) -> bool:
    logger.debug(f"[STREMIO] AHK sending: {send_body}")
    return _run_ahk(_build_send_body_script(send_body))


# ── Public control functions ──────────────────────────────────────────────────

def pause_play() -> bool:
    """Toggle play/pause (Space)."""
    return _send_stremio_key("Space")


def skip_forward(seconds: float = DEFAULT_SEEK_SECONDS) -> bool:
    """Seek ahead with one bounded AHK Send (10-second arrow taps)."""
    return _send_stremio_send_body(f"Send, {{Right {seek_presses(seconds)}}}")


def skip_back(seconds: float = DEFAULT_SEEK_SECONDS) -> bool:
    """Seek back with the same default and bounds as forward."""
    return _send_stremio_send_body(f"Send, {{Left {seek_presses(seconds)}}}")


def next_episode() -> bool:
    """Shift+N: stremio-web's playNext shortcut (no previous counterpart).

    Verified: Stremio/stremio-web c34935cc, common/Shortcuts/shortcuts.json
    and routes/Player/Player.js. Sending the shortcut does not verify that
    another episode exists or that playback has started.
    """
    return _send_stremio_send_body("Send, +{n}")


def fullscreen() -> bool:
    """Toggle fullscreen (f)."""
    return _send_stremio_key("f")


def mute() -> bool:
    """Toggle mute (m)."""
    return _send_stremio_key("m")


def volume_up() -> bool:
    """Volume +10% (Up arrow). Verified against the current Stremio player
    2026-07-10."""
    return _send_stremio_key("Up")


def volume_down() -> bool:
    """Volume -10% (Down arrow). Verified against the current Stremio
    player 2026-07-10."""
    return _send_stremio_key("Down")


def switch_monitor() -> bool:
    """Move the focused window to the next monitor (Win+Shift+Right,
    cycles). Windows-native window-management shortcut, not a Stremio
    player control -- reuses the same AHK focus-then-send path since
    focusing Stremio first is exactly what's wanted (move the STREMIO
    window, not whatever else happened to have focus). AHK v1 modifier
    syntax: # = Win, + = Shift, prefixed directly onto the key name (NOT
    inside the braces -- {#+Right} would be invalid, #+{Right} is correct)."""
    return _send_stremio_send_body("Send, #+{Right}")


# ── Sleep timer ─────────────────────────────────────────────────────────────────

_sleep_lock = threading.Lock()
_sleep_timer: "threading.Timer | None" = None
_sleep_deadline: "float | None" = None  # time.monotonic() when timer fires
_sleep_total_minutes: int = 0


def _cancel_timer_locked() -> None:
    """Cancel the running timer if any. Must hold _sleep_lock."""
    global _sleep_timer, _sleep_deadline, _sleep_total_minutes
    if _sleep_timer is not None:
        _sleep_timer.cancel()
        _sleep_timer = None
    _sleep_deadline = None
    _sleep_total_minutes = 0


def _on_sleep_fire() -> None:
    """Called by the timer thread. Pauses Stremio and clears timer state."""
    pause_play()
    global _sleep_timer, _sleep_deadline, _sleep_total_minutes
    with _sleep_lock:
        _sleep_timer = None
        _sleep_deadline = None
        _sleep_total_minutes = 0


def schedule_sleep(minutes: int) -> bool:
    """Schedule a Stremio pause after `minutes`. Cancels any existing timer first.

    Pass `minutes <= 0` to cancel without scheduling a new timer.
    """
    global _sleep_timer, _sleep_deadline, _sleep_total_minutes
    with _sleep_lock:
        _cancel_timer_locked()
        if minutes <= 0:
            return True
        _sleep_total_minutes = minutes
        interval = minutes * 60
        _sleep_timer = thread_registry.timer(
            "stremio_control.sleep",
            interval,
            _on_sleep_fire,
            daemon=True,
        )
        _sleep_deadline = time.monotonic() + interval
        logger.debug(f"[STREMIO] Sleep timer set for {minutes} min")
    return True


def cancel_sleep() -> bool:
    """Cancel any running sleep timer."""
    global _sleep_timer, _sleep_deadline, _sleep_total_minutes
    with _sleep_lock:
        _cancel_timer_locked()
    logger.debug("[STREMIO] Sleep timer cancelled")
    return True


def get_sleep_status() -> dict:
    """Return the current sleep timer state for the remote's /status endpoint."""
    with _sleep_lock:
        if _sleep_deadline is None:
            return {"active": False, "remaining_seconds": None, "duration_seconds": 0}
        remaining = max(0.0, _sleep_deadline - time.monotonic())
        return {
            "active": True,
            "remaining_seconds": round(remaining, 1),
            "duration_seconds": _sleep_total_minutes * 60,
        }


# ── Process helpers ────────────────────────────────────────────────────────────

def is_stremio_running() -> bool:
    """True if any known Stremio process is in the running task list.

    Uses `tasklist` (always present on Windows) rather than psutil to keep
    this module dependency-free. Fails closed (returns False) on any error.
    """
    try:
        result = subprocess.run(
            ['tasklist', '/FO', 'CSV', '/NH'],
            capture_output=True, timeout=5, text=True,
        )
        output_lower = result.stdout.lower()
        return any(name.lower() in output_lower for name in STREMIO_PROCESS_NAMES)
    except Exception as e:
        logger.debug(f"is_stremio_running: {e}")
        return False


def kill_stremio() -> None:
    """Force-kill every known Stremio process, if running. Best-effort --
    never raises."""
    for name in STREMIO_PROCESS_NAMES:
        try:
            subprocess.run(
                ['taskkill', '/IM', name, '/F'],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True,
            )
        except Exception as e:
            logger.debug(f"kill_stremio({name}): {e}")
