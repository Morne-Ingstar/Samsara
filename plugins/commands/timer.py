"""Voice-activated timer plugin.

Say "Jarvis, set a timer for 5 minutes" or "Jarvis, timer 30 seconds".
Parses natural language durations, runs in background, plays notification
sound and shows system toast when done.

Trigger phrases:
  "set a timer"   / "set timer"   / "timer for"   / "timer"
  "remind me in"  / "start a timer"
"""

import re
import threading
import time
import subprocess
import sys

from samsara.plugin_commands import command


def _parse_duration(text):
    """Parse natural language duration into seconds.
    
    Handles: "5 minutes", "30 seconds", "1 hour", "90 seconds",
             "2 minutes 30 seconds", "1 and a half minutes",
             "5 min", "10 sec", "1 hr"
    """
    if not text:
        return None, "no duration specified"
    
    text = text.lower().strip()
    
    # Handle "a half" / "half a" / "and a half"
    text = text.replace("and a half", "30 seconds")
    text = text.replace("a half", "30 seconds")
    text = text.replace("half a minute", "30 seconds")
    text = text.replace("half an hour", "30 minutes")
    
    total_seconds = 0
    found = False
    
    # Pattern: number + unit
    patterns = [
        (r'(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)\b', 3600),
        (r'(\d+(?:\.\d+)?)\s*(?:minutes?|mins?|m)\b', 60),
        (r'(\d+(?:\.\d+)?)\s*(?:seconds?|secs?|s)\b', 1),
    ]
    
    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, text):
            total_seconds += float(match.group(1)) * multiplier
            found = True
    
    # Handle bare numbers: assume minutes if no unit
    if not found:
        bare = re.search(r'(\d+)', text)
        if bare:
            total_seconds = int(bare.group(1)) * 60
            found = True
    
    if not found or total_seconds <= 0:
        return None, f"couldn't parse duration from '{text}'"
    
    return int(total_seconds), None


def _format_duration(seconds):
    """Human-readable duration string."""
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m" if m else f"{h} hour{'s' if h > 1 else ''}"
    elif seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m {s}s" if s else f"{m} minute{'s' if m > 1 else ''}"
    else:
        return f"{seconds} second{'s' if seconds != 1 else ''}"


def _timer_thread(seconds, label, app):
    """Background thread: sleep then notify."""
    time.sleep(seconds)
    
    duration_str = _format_duration(seconds)
    msg = f"Timer done: {duration_str}"
    if label:
        msg = f"Timer done ({label}): {duration_str}"
    
    print(f"[TIMER] {msg}")
    
    # Play notification sound if available
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
            time.sleep(0.5)
            app.play_sound("start")  # play twice for attention
        except Exception:
            pass
    
    # Windows toast notification
    if sys.platform == 'win32':
        try:
            subprocess.Popen([
                'powershell', '-Command',
                f'[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null; '
                f'[System.Windows.Forms.MessageBox]::Show("{msg}", "Samsara Timer", "OK", "Information")'
            ], creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass


# Active timers for status tracking
_active_timers = []
_timer_lock = threading.Lock()


@command("set a timer", aliases=[
    "set timer", "timer for", "timer",
    "remind me in", "start a timer", "start timer"
])
def handle_timer(app, remainder):
    """Set a countdown timer. Usage: 'Jarvis, set a timer for 5 minutes'"""
    seconds, error = _parse_duration(remainder)
    if error:
        print(f"[TIMER] {error}")
        return True  # consumed the command even if parse failed
    
    duration_str = _format_duration(seconds)
    print(f"[TIMER] Started: {duration_str}")
    
    # Start background timer
    t = threading.Thread(
        target=_timer_thread,
        args=(seconds, remainder.strip(), app),
        daemon=True,
        name=f"timer-{seconds}s"
    )
    t.start()
    
    with _timer_lock:
        _active_timers.append({
            'thread': t,
            'seconds': seconds,
            'label': remainder.strip(),
            'started': time.time(),
        })
    
    # Announce via indicator if available
    if hasattr(app, 'listening_indicator'):
        try:
            app._schedule_ui(
                app.listening_indicator.set_mode,
                f"Timer: {duration_str}"
            )
        except Exception:
            pass
    
    return True
