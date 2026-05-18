"""Alarm voice commands — wrap AlarmManager with voice control.

Voice commands:
  "complete alarm" / "I did it" / "alarm done" / "alarm complete"
  "dismiss alarm" / "skip alarm" / "snooze alarm" / "silence alarm"
  "read alarms" / "list alarms" / "what alarms do I have"
  "enable alarm [name]" / "turn on alarm [name]"
  "disable alarm [name]" / "turn off alarm [name]"
"""

from samsara.plugin_commands import command


def _speak(app, text):
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        print(f"[ALARMS] {text}")


def _get_manager(app):
    """Return alarm_manager or None if unavailable."""
    if not hasattr(app, "alarm_manager") or app.alarm_manager is None:
        return None
    return app.alarm_manager


def _fuzzy_match(name_query, alarms):
    """Return the matching alarm dict or a string error message."""
    q = name_query.lower().strip()
    if not q:
        return "No alarm name provided."

    exact = [a for a in alarms if a.get("name", "").lower() == q]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return "Multiple alarms match. Be more specific."

    starts = [a for a in alarms if a.get("name", "").lower().startswith(q)]
    if len(starts) == 1:
        return starts[0]
    if len(starts) > 1:
        return "Multiple alarms match. Be more specific."

    contains = [a for a in alarms if q in a.get("name", "").lower()]
    if len(contains) == 1:
        return contains[0]
    if len(contains) > 1:
        return "Multiple alarms match. Be more specific."

    return f"No alarm matching {name_query}."


@command(
    "complete alarm",
    aliases=["I did it", "alarm done", "alarm complete"],
    pack="alarms",
)
def handle_complete_alarm(app, remainder="", **kwargs):
    mgr = _get_manager(app)
    if mgr is None:
        _speak(app, "Alarm manager is not available.")
        return True

    alarm_id = mgr.nagging_alarm_id
    completed = mgr.complete()

    if not completed:
        _speak(app, "No alarm is currently active.")
        return True

    if alarm_id:
        stats = mgr.get_stats(alarm_id)
        streak = stats.get("current_streak", 0)
        _speak(app, f"Alarm completed. {streak} day streak.")
    else:
        _speak(app, "Alarm completed.")
    return True


@command(
    "dismiss alarm",
    aliases=["skip alarm", "snooze alarm", "silence alarm"],
    pack="alarms",
)
def handle_dismiss_alarm(app, remainder="", **kwargs):
    mgr = _get_manager(app)
    if mgr is None:
        _speak(app, "Alarm manager is not available.")
        return True

    dismissed = mgr.dismiss()
    if not dismissed:
        _speak(app, "No alarm is currently active.")
        return True

    _speak(app, "Alarm dismissed.")
    return True


@command(
    "read alarms",
    aliases=["list alarms", "what alarms do I have"],
    pack="alarms",
)
def handle_read_alarms(app, remainder="", **kwargs):
    mgr = _get_manager(app)
    if mgr is None:
        _speak(app, "Alarm manager is not available.")
        return True

    alarms = mgr.items
    if not alarms:
        _speak(app, "No alarms configured.")
        return True

    n = len(alarms)
    parts = [f"You have {n} alarm{'s' if n != 1 else '.'}."]
    for alarm in alarms:
        alarm_id = alarm.get("id", alarm.get("name", ""))
        name = alarm.get("name", alarm_id)
        interval = alarm.get("interval_minutes", 60)
        enabled = "enabled" if alarm.get("enabled", False) else "disabled"
        stats = mgr.get_stats(alarm_id)
        streak = stats.get("current_streak", 0)
        parts.append(
            f"{name}: every {interval} minutes, {enabled}, {streak} day streak."
        )

    _speak(app, " ".join(parts))
    return True


@command(
    "enable alarm",
    aliases=["turn on alarm"],
    pack="alarms",
)
def handle_enable_alarm(app, remainder="", **kwargs):
    mgr = _get_manager(app)
    if mgr is None:
        _speak(app, "Alarm manager is not available.")
        return True

    result = _fuzzy_match(remainder, mgr.items)
    if isinstance(result, str):
        _speak(app, result)
        return True

    alarm_id = result.get("id", result.get("name", ""))
    name = result.get("name", alarm_id)
    mgr.update_alarm(alarm_id, enabled=True)
    _speak(app, f"Enabled {name} alarm.")
    return True


@command(
    "disable alarm",
    aliases=["turn off alarm"],
    pack="alarms",
)
def handle_disable_alarm(app, remainder="", **kwargs):
    mgr = _get_manager(app)
    if mgr is None:
        _speak(app, "Alarm manager is not available.")
        return True

    result = _fuzzy_match(remainder, mgr.items)
    if isinstance(result, str):
        _speak(app, result)
        return True

    alarm_id = result.get("id", result.get("name", ""))
    name = result.get("name", alarm_id)
    mgr.update_alarm(alarm_id, enabled=False)
    _speak(app, f"Disabled {name} alarm.")
    return True
