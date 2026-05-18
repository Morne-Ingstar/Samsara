"""Voice reminder commands — wrap NotificationManager with voice control.

Voice commands:
  "remind me to [task] every [interval]"
  "remind me to [task] at [time]"
  "read reminders" / "list reminders" / "what reminders do I have"
  "cancel reminder [name or number]" / "remove reminder [name or number]"
  "cancel all reminders" / "clear reminders"

Note: "remind me in [duration]" is handled by timer.py — not duplicated here.
"""

import re
from datetime import datetime

from samsara.plugin_commands import command
from plugins.commands.timer import _format_duration, _parse_duration


def _speak(app, text):
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[REMINDERS] {text}")


def _get_manager(app):
    if not hasattr(app, "notification_manager") or app.notification_manager is None:
        return None
    return app.notification_manager


def _parse_time(text):
    """Parse '9 AM', '2:30 PM', '14:00' -> 'HH:MM', or None on failure."""
    text = text.strip().lower()

    m = re.match(r'^(\d{1,2}):(\d{2})\s*(am|pm)?$', text)
    if m:
        h, mins, period = int(m.group(1)), int(m.group(2)), m.group(3)
        if period == 'pm' and h != 12:
            h += 12
        elif period == 'am' and h == 12:
            h = 0
        elif period is None and 1 <= h <= 6:
            h += 12
        return f"{h:02d}:{mins:02d}"

    m = re.match(r'^(\d{1,2})\s*(am|pm)?$', text)
    if m:
        h, period = int(m.group(1)), m.group(2)
        if period == 'pm' and h != 12:
            h += 12
        elif period == 'am' and h == 12:
            h = 0
        elif period is None and 1 <= h <= 6:
            h += 12
        return f"{h:02d}:00"

    return None


def _display_time(hhmm):
    """'14:30' -> '2:30 PM', '09:00' -> '9 AM'. Windows-safe (no %-I)."""
    try:
        dt = datetime.strptime(hhmm, "%H:%M")
        h = dt.hour % 12 or 12
        period = "AM" if dt.hour < 12 else "PM"
        if dt.minute == 0:
            return f"{h} {period}"
        return f"{h}:{dt.minute:02d} {period}"
    except ValueError:
        return hhmm


def _describe_schedule(schedule):
    """Return a spoken description of a schedule dict."""
    stype = schedule.get("type", "")
    if stype == "interval":
        mins = schedule.get("minutes", 0)
        return "every " + _format_duration(mins * 60)
    if stype == "times":
        times = schedule.get("times", [])
        return "at " + ", ".join(_display_time(t) for t in times)
    if stype == "once":
        at = schedule.get("at", "")
        try:
            dt = datetime.fromisoformat(at)
            h = dt.hour % 12 or 12
            period = "AM" if dt.hour < 12 else "PM"
            if dt.minute == 0:
                return f"at {h} {period}"
            return f"at {h}:{dt.minute:02d} {period}"
        except (ValueError, TypeError):
            return f"at {at}"
    return ""


def _fuzzy_find(query, reminders):
    """Return the matching reminder dict or an error string."""
    q = query.lower().strip()
    if not q:
        return "No reminder name provided."

    exact = [r for r in reminders if r.get("name", "").lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return "Multiple reminders match. Be more specific."

    starts = [r for r in reminders if r.get("name", "").lower().startswith(q)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        return "Multiple reminders match. Be more specific."

    contains = [r for r in reminders if q in r.get("name", "").lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        return "Multiple reminders match. Be more specific."

    return f"No reminder matching {query}."


@command(
    "remind me to",
    aliases=["set a reminder for", "create reminder"],
    pack="utilities",
)
def handle_remind_me_to(app, remainder="", **kwargs):
    nm = _get_manager(app)
    if nm is None:
        _speak(app, "Reminder system is not available.")
        return True

    remainder = (remainder or "").strip()

    # "remind me to [task] every [interval]"
    if " every " in remainder:
        parts = remainder.split(" every ", 1)
        task = parts[0].strip()
        interval_text = parts[1].strip()
        if not task:
            _speak(app, "What should I remind you to do?")
            return True
        seconds, err = _parse_duration(interval_text)
        if err or not seconds:
            _speak(app, f"I couldn't understand the interval: {interval_text}.")
            return True
        minutes = max(1, seconds // 60)
        nm.add_reminder(
            name=task,
            schedule={"type": "interval", "minutes": minutes},
            message=task,
        )
        _speak(app, f"Reminder set: {task} every {_format_duration(seconds)}.")
        return True

    # "remind me to [task] at [time]"
    at_match = re.search(
        r'\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*$',
        remainder,
        re.IGNORECASE,
    )
    if at_match:
        task = remainder[:at_match.start()].strip()
        time_str = at_match.group(1).strip()
        if not task:
            _speak(app, "What should I remind you to do?")
            return True
        hhmm = _parse_time(time_str)
        if hhmm is None:
            _speak(app, f"I couldn't understand the time: {time_str}.")
            return True
        nm.add_reminder(
            name=task,
            schedule={"type": "times", "times": [hhmm]},
            message=task,
        )
        _speak(app, f"Reminder set: {task} at {_display_time(hhmm)}.")
        return True

    _speak(app, "Say 'remind me to [task] every [interval]' or 'remind me to [task] at [time]'.")
    return True


@command(
    "read reminders",
    aliases=["list reminders", "what reminders do I have"],
    pack="utilities",
)
def handle_read_reminders(app, remainder="", **kwargs):
    nm = _get_manager(app)
    if nm is None:
        _speak(app, "Reminder system is not available.")
        return True

    active = [r for r in nm.get_all_reminders() if r.get("enabled", True)]
    if not active:
        _speak(app, "No active reminders.")
        return True

    n = len(active)
    parts = [f"You have {n} reminder{'s' if n != 1 else ''}."]
    for r in active:
        name = r.get("name", "Unnamed")
        desc = _describe_schedule(r.get("schedule", {}))
        parts.append(f"{name} {desc}.".strip())

    _speak(app, " ".join(parts))
    return True


@command(
    "cancel reminder",
    aliases=["remove reminder", "delete reminder"],
    pack="utilities",
)
def handle_cancel_reminder(app, remainder="", **kwargs):
    nm = _get_manager(app)
    if nm is None:
        _speak(app, "Reminder system is not available.")
        return True

    remainder = (remainder or "").strip()
    if not remainder:
        _speak(app, "Which reminder should I cancel?")
        return True

    reminders = nm.get_all_reminders()
    if not reminders:
        _speak(app, "No reminders to cancel.")
        return True

    # Position number (e.g. "cancel reminder 2")
    if re.match(r'^\d+$', remainder):
        pos = int(remainder)
        if 1 <= pos <= len(reminders):
            r = reminders[pos - 1]
            nm.remove_reminder(r["id"])
            _speak(app, f"Cancelled reminder: {r.get('name', 'Unnamed')}.")
        else:
            _speak(app, f"No reminder number {pos}.")
        return True

    # Fuzzy name match
    result = _fuzzy_find(remainder, reminders)
    if isinstance(result, str):
        _speak(app, result)
        return True

    nm.remove_reminder(result["id"])
    _speak(app, f"Cancelled reminder: {result.get('name', 'Unnamed')}.")
    return True


@command(
    "cancel all reminders",
    aliases=["clear reminders", "remove all reminders", "clear all reminders"],
    pack="utilities",
)
def handle_cancel_all_reminders(app, remainder="", **kwargs):
    nm = _get_manager(app)
    if nm is None:
        _speak(app, "Reminder system is not available.")
        return True

    reminders = nm.get_all_reminders()
    if not reminders:
        _speak(app, "No reminders to clear.")
        return True

    for r in reminders:
        nm.remove_reminder(r["id"])

    _speak(app, "All reminders cleared.")
    return True


@command(
    "show reminders",
    aliases=["reminder overview", "reminder status"],
    pack="utilities",
)
def handle_show_reminders(app, remainder="", **kwargs):
    from samsara.ui.status_overlay import get_overlay
    get_overlay().toggle(
        notification_manager=getattr(app, "notification_manager", None),
        alarm_manager=getattr(app, "alarm_manager", None),
    )
    return True
