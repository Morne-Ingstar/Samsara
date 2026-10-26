"""Voice-activated timer plugin.

Say "Jarvis, set a timer for 5 minutes" or "Jarvis, timer 30 seconds".
Parses natural language durations, runs in background, plays notification
sound and shows a nonactivating themed toast when done.

Trigger phrases:
  "set a timer"   / "set timer"   / "timer for"   / "timer"
  "remind me in"  / "start a timer"
"""

from html import escape
import math
import re
import threading
import time

from samsara.plugin_commands import command
from samsara.runtime import thread_registry

from samsara.log import get_logger

logger = get_logger(__name__)

# Bound converter work and timer lifetime: at most 512 characters / 64 words
# per request, and a positive duration no longer than seven days. Fractions of
# a second are preserved rather than rounded down to an immediate completion.
_MAX_INPUT_CHARS = 512
_MAX_INPUT_WORDS = 64
_MAX_SECONDS = 7 * 24 * 3600
_AMOUNT = r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:\s+and\s+(?:a\s+)?half)?|(?:a\s+)?half(?:\s+(?:a|an))?'
_UNIT = r'hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s'
_PART = re.compile(rf'(?P<amount>{_AMOUNT})\s*(?P<unit>{_UNIT})\b')


def _parse_duration(text):
    """Parse a duration prefix, leaving an optional trailing label as text.

    Whole bare numbers mean minutes. Mixed units, decimals and halves retain
    their named unit. Signs, exponents, nonfinite and incomplete durations
    are invalid; duration-like junk cannot be skipped to find a later number.
    """
    # PLACEHOLDER COPY (929): ordinary parser error text.
    invalid = "Use a positive timer duration of at most seven days."
    if not isinstance(text, str) or not text.strip():
        return None, "no duration specified"  # PLACEHOLDER COPY (929)
    if len(text) > _MAX_INPUT_CHARS or len(text.split()) > _MAX_INPUT_WORDS:
        return None, "Timer request is too long."  # PLACEHOLDER COPY (929)
    from samsara.spoken_numbers import number_words_to_digits
    # "second" is also an ordinal to the shared converter. Protect unit
    # tokens so "one second" / "half a second" retain their seconds unit.
    pieces = re.split(rf'(\b(?:{_UNIT})\b)', text.lower().strip())
    text = ''.join(piece if i % 2 else number_words_to_digits(piece)
                   for i, piece in enumerate(pieces))
    text = re.sub(r'^for\s+', '', text)
    total_seconds = 0.0
    rest = text
    while match := _PART.match(rest):
        amount = match['amount']
        if 'half' in amount:
            whole = amount.split(' and ', 1)[0] if ' and ' in amount else '0'
            value = float(whole) + 0.5
        else:
            value = float(amount)
        if value < 0 or not math.isfinite(value):
            return None, invalid
        unit = match['unit'][0]
        total_seconds += value * {'h': 3600, 'm': 60, 's': 1}[unit]
        rest = rest[match.end():].strip()
        if not rest:
            break
        # Only consume a separator when another complete duration follows.
        following = re.sub(r'^(?:,\s*(?:and\s+)?|and\s+)', '', rest)
        if _PART.match(following):
            rest = following
        else:
            break
    if total_seconds == 0:
        bare = re.match(r'([0-9]+)(?=\s|$)', text)
        if not bare:
            return None, invalid
        total_seconds = float(bare[1]) * 60
        rest = text[bare.end():].strip()
    # Labels begin after the complete duration. Reject an unfinished numeric
    # clause (including a dangling connector), rather than silently shorten it.
    if rest and re.match(rf'(?:[\d.+\-,]|and\b|half\b|a\s+half\b|minus\b|negative\b|nan\b|inf(?:inity)?\b|(?:{_UNIT})\b)', rest):
        return None, invalid
    if not math.isfinite(total_seconds) or not 0 < total_seconds <= _MAX_SECONDS:
        return None, invalid
    return int(total_seconds) if total_seconds.is_integer() else total_seconds, None


def _format_duration(seconds):
    """Human-readable duration string."""
    if seconds >= 3600:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h:g}h {m:g}m" if m else f"{h:g} hour{'s' if h > 1 else ''}"
    elif seconds >= 60:
        m = seconds // 60
        s = seconds % 60
        return f"{m:g}m {s:g}s" if s else f"{m:g} minute{'s' if m > 1 else ''}"
    else:
        return f"{seconds:g} second{'s' if seconds != 1 else ''}"


def _timer_thread(seconds, label, app):
    """Background thread: sleep then notify."""
    time.sleep(seconds)
    
    duration_str = _format_duration(seconds)
    # PLACEHOLDER COPY (929): timer completion and title below.
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
        except Exception as e:
            logger.debug(f"_timer_thread: {e}")
    
    # The manager posts through ReminderToast -> qt_runtime.post. It never
    # opens a shell or activates a modal window. QLabel auto-detects rich text:
    # an explicit escaped span keeps markup in labels literal, too.
    manager = getattr(app, 'notification_manager', None)
    if manager is not None:
        try:
            manager.show_notification("Samsara Timer", f"<span>{escape(msg)}</span>")
        except Exception as e:
            logger.debug(f"_timer_thread: {e}")


# Active timers for status tracking
_active_timers = []
_timer_lock = threading.Lock()


@command("set a timer", aliases=[
    "set timer", "timer for", "timer",
    "remind me in", "start a timer", "start timer"
], pack="utilities")
def handle_timer(app, remainder):
    """Sets a countdown timer for the length you say.

    Usage: 'Jarvis, set a timer for 5 minutes'
    """
    seconds, error = _parse_duration(remainder)
    if error:
        print(f"[TIMER] {error}")
        return True  # consumed the command even if parse failed
    
    duration_str = _format_duration(seconds)
    print(f"[TIMER] Started: {duration_str}")
    
    # Start background timer
    t = thread_registry.spawn(
        f"timer-{seconds}s", _timer_thread,
        args=(seconds, remainder.strip(), app),
        daemon=True,
    )
    
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
        except Exception as e:
            logger.debug(f"handle_timer: {e}")
    
    return True
