"""Health tracking plugin -- local-only symptom, pain, and medication logging.

All data stays on disk at ~/.samsara/health_log.json. No cloud, no accounts,
no app store. Built for people who don't trust apps with their health data.

Voice commands:
  "Jarvis, pain level 6"
  "Jarvis, pain level 4 knees"
  "Jarvis, took ibuprofen 400mg"
  "Jarvis, took paracetamol"
  "Jarvis, symptom my hands are stiff today"
  "Jarvis, how's my pain" / "how was my week"
  "Jarvis, health summary"
  "Jarvis, export health log"
  "Jarvis, read health log"
  "Jarvis, undo health log"
"""

import logging
import os
import re

from samsara import health_store
from samsara.plugin_commands import command

logger = logging.getLogger(__name__)


def _speak(app, text):
    """TTS or print fallback."""
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[HEALTH] {text}")


def _parse_pain_level(text):
    """Extract a pain level (1-10) from text. Returns (level, remainder) or (None, text)."""
    if not text:
        return None, ""
    # Match "level 6", "6", "level6", "a 7" etc
    m = re.search(r'(?:level\s*)?(\d{1,2})\b', text, re.IGNORECASE)
    if m:
        level = int(m.group(1))
        if 1 <= level <= 10:
            remainder = (text[:m.start()] + text[m.end():]).strip()
            # Clean up leftover "level" word
            remainder = re.sub(r'\blevel\b', '', remainder, flags=re.IGNORECASE).strip()
            return level, remainder
    return None, text


def _parse_medication(text):
    """Parse 'ibuprofen 400mg' or 'paracetamol' into (name, dose, note)."""
    if not text:
        return None, None, None
    text = text.strip()
    # Try to find a dose pattern: number + optional unit
    dose_pattern = r'(\d+\s*(?:mg|ml|mcg|g|iu|units?|tablets?|caps?|capsules?|drops?|puffs?)?)'
    m = re.search(dose_pattern, text, re.IGNORECASE)
    if m:
        dose = m.group(1).strip()
        name = text[:m.start()].strip()
        note = text[m.end():].strip()
        if not name:
            # dose was at the start, rest is the name
            name = note
            note = ""
        return name or "unknown", dose, note or None
    # No dose found — whole thing is the medication name
    return text, None, None


def _format_timestamp(ts):
    """Convert ISO timestamp to readable local time."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local = dt.astimezone()
        return local.strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ts


def _format_entry_speech(entry):
    """Format a single entry for TTS readback."""
    t = entry["type"]
    d = entry["data"]
    time_str = _format_timestamp(entry["timestamp"])

    if t == "pain":
        msg = f"{time_str}: pain level {d.get('level', '?')}"
        if d.get("location"):
            msg += f", {d['location']}"
        if d.get("note"):
            msg += f". {d['note']}"
    elif t == "medication":
        msg = f"{time_str}: took {d.get('name', 'something')}"
        if d.get("dose"):
            msg += f" {d['dose']}"
    elif t == "symptom":
        msg = f"{time_str}: {d.get('text', 'symptom noted')}"
    else:
        msg = f"{time_str}: {t}"
    return msg


# ── Pain Level ────────────────────────────────────────────────────────────

@command(
    "pain level",
    aliases=["my pain is", "pain is", "pain at", "log pain"],
    pack="health",
)
def handle_pain_level(app, remainder="", **kwargs):
    """Log a pain level. 'pain level 6' or 'pain level 4 knees'."""
    level, rest = _parse_pain_level(remainder)
    if level is None:
        _speak(app, "What's your pain level, 1 to 10?")
        return True

    data = {"level": level}
    # Anything left after extracting the number is the body location / note
    if rest:
        # Check if it looks like a body part vs a longer note
        short_words = len(rest.split()) <= 4
        if short_words:
            data["location"] = rest
        else:
            data["note"] = rest

    health_store.add_entry("pain", data)
    msg = f"Logged pain level {level}"
    if data.get("location"):
        msg += f", {data['location']}"
    msg += "."
    _speak(app, msg)
    return True


# ── Medication ────────────────────────────────────────────────────────────

@command(
    "took",
    aliases=["take", "medication", "just took", "popped a", "popped",
             "log medication", "log med"],
    pack="health",
)
def handle_medication(app, remainder="", **kwargs):
    """Log medication. 'took ibuprofen 400mg' or 'took paracetamol'."""
    if not remainder or not remainder.strip():
        _speak(app, "What did you take?")
        return True

    name, dose, note = _parse_medication(remainder)
    data = {"name": name}
    if dose:
        data["dose"] = dose
    if note:
        data["note"] = note

    health_store.add_entry("medication", data)
    msg = f"Logged: {name}"
    if dose:
        msg += f" {dose}"
    msg += "."
    _speak(app, msg)
    return True


# ── Symptoms ──────────────────────────────────────────────────────────────

@command(
    "symptom",
    aliases=["symptoms", "i feel", "i'm feeling", "log symptom"],
    pack="health",
)
def handle_symptom(app, remainder="", **kwargs):
    """Log a freeform symptom. 'symptom my knees are stiff today'."""
    if not remainder or not remainder.strip():
        _speak(app, "What symptom should I log?")
        return True

    text = remainder.strip()
    health_store.add_entry("symptom", {"text": text})
    _speak(app, f"Logged: {text}.")
    return True


# ── Summaries ─────────────────────────────────────────────────────────────

@command(
    "health summary",
    aliases=["how's my pain", "how is my pain", "how was my day",
             "how was my week", "health report", "pain summary",
             "how am i doing", "how am i feeling"],
    pack="health",
)
def handle_health_summary(app, remainder="", **kwargs):
    """Speak a summary of recent health data."""
    # Determine time window
    hours = 24
    if remainder:
        r = remainder.lower()
        if "week" in r:
            hours = 168
        elif "month" in r:
            hours = 720
        elif "today" in r:
            hours = 24

    entries = health_store.get_recent(hours=hours)
    if not entries:
        period = "today" if hours <= 24 else f"the last {hours // 24} days"
        _speak(app, f"No health entries logged {period}.")
        return True

    pain_entries = [e for e in entries if e["type"] == "pain"]
    med_entries = [e for e in entries if e["type"] == "medication"]
    symptom_entries = [e for e in entries if e["type"] == "symptom"]

    parts = []

    # Pain summary
    if pain_entries:
        levels = [e["data"]["level"] for e in pain_entries if "level" in e["data"]]
        if levels:
            avg = round(sum(levels) / len(levels), 1)
            low = min(levels)
            high = max(levels)
            parts.append(
                f"Pain: {len(levels)} readings, average {avg}, "
                f"range {low} to {high}."
            )
    else:
        parts.append("No pain levels logged.")

    # Medication summary
    if med_entries:
        med_names = {}
        for e in med_entries:
            name = e["data"].get("name", "unknown")
            med_names[name] = med_names.get(name, 0) + 1
        med_parts = [f"{name} x{count}" if count > 1 else name
                     for name, count in med_names.items()]
        parts.append(f"Medications: {', '.join(med_parts)}.")

    # Symptom count
    if symptom_entries:
        parts.append(f"{len(symptom_entries)} symptom note{'s' if len(symptom_entries) != 1 else ''} logged.")

    _speak(app, " ".join(parts))
    return True


# ── Read / Export / Undo ──────────────────────────────────────────────────

@command(
    "read health log",
    aliases=["read health", "health log", "what did i log",
             "read my health", "health entries"],
    pack="health",
)
def handle_read_health(app, remainder="", **kwargs):
    """Read back today's health log entries via TTS."""
    entries = health_store.get_today()
    if not entries:
        _speak(app, "No health entries logged today.")
        return True

    parts = [f"You have {len(entries)} health entr{'ies' if len(entries) != 1 else 'y'} today."]
    for e in entries:
        parts.append(_format_entry_speech(e))

    _speak(app, " ".join(parts))
    return True


@command(
    "export health log",
    aliases=["export health", "save health log", "health csv",
             "download health log"],
    pack="health",
)
def handle_export_health(app, remainder="", **kwargs):
    """Export the full health log to CSV on disk."""
    filepath = health_store.export_csv()
    _speak(app, f"Health log exported to {os.path.basename(filepath)}.")
    logger.info(f"[HEALTH] Exported to {filepath}")
    return True


@command(
    "undo health log",
    aliases=["undo health", "remove last health", "delete last health",
             "undo health entry"],
    pack="health",
)
def handle_undo_health(app, remainder="", **kwargs):
    """Remove the most recent health log entry."""
    entries = health_store.get_all()
    if not entries:
        _speak(app, "No health entries to undo.")
        return True

    last = entries[-1]
    health_store.remove_entry(last["id"])
    desc = _format_entry_speech(last)
    _speak(app, f"Removed last entry: {desc}.")
    return True


@command(
    "clear health log",
    aliases=["delete health log", "wipe health log", "reset health log"],
    pack="health",
)
def handle_clear_health(app, remainder="", **kwargs):
    """Clear the entire health log. Destructive!"""
    count = health_store.clear_all()
    if count == 0:
        _speak(app, "Health log is already empty.")
    else:
        _speak(app, f"Cleared {count} health log entr{'ies' if count != 1 else 'y'}.")
    return True
