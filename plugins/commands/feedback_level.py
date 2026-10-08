"""Voice controls for Samsara speech feedback and its earcon volume."""
from samsara.config_schema import (
    FEEDBACK_LEVEL_CHOICES, FEEDBACK_LEVEL_FRESH_DEFAULT, FEEDBACK_LEVELS,
    apply_feedback_level,
)
from samsara.plugin_commands import command


def _reply(app, text):
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(text, "success", 3500)
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(text, category="agent_response")


def _current(app):
    level = (app.config.get("tts") or {}).get("feedback_level", FEEDBACK_LEVEL_FRESH_DEFAULT)
    return level if level in FEEDBACK_LEVELS else FEEDBACK_LEVEL_FRESH_DEFAULT


def _set(app, level):
    already = _current(app) == level
    setter = getattr(app, "set_feedback_level", None)
    if callable(setter):
        setter(level)
    else:  # Compatibility for fake apps without the production setter.
        apply_feedback_level(app.config, level)
        app.save_config()
    label = FEEDBACK_LEVEL_CHOICES[level][0]
    _reply(app, f"Feedback: {label}" + (" (already)" if already else ""))
    return True


def _step(app, delta):
    index = FEEDBACK_LEVELS.index(_current(app))
    return _set(app, FEEDBACK_LEVELS[max(0, min(len(FEEDBACK_LEVELS) - 1, index + delta))])


@command("talk less", aliases=["less talking", "less feedback"], pack="core",
         risk_class="ui", ai_composable=True, side_effects=["config", "audio"])
def handle_talk_less(app, remainder=""):
    """Makes Samsara speak less: Full, then Brief, then Silent."""
    return _step(app, -1)


@command("talk more", aliases=["more talking", "more feedback"], pack="core",
         risk_class="ui", ai_composable=True, side_effects=["config", "audio"])
def handle_talk_more(app, remainder=""):
    """Makes Samsara speak more: Silent, then Brief, then Full."""
    return _step(app, 1)


@command("feedback silent", pack="core", risk_class="ui", ai_composable=True,
         side_effects=["config", "audio"])
def handle_feedback_silent(app, remainder=""):
    """Shows Samsara replies on screen without speaking them."""
    return _set(app, "silent")


@command("feedback brief", pack="core", risk_class="ui", ai_composable=True,
         side_effects=["config", "audio"])
def handle_feedback_brief(app, remainder=""):
    """Uses short Samsara sounds for outcomes and speaks questions and read-backs."""
    return _set(app, "brief")


@command("feedback full", pack="core", risk_class="ui", ai_composable=True,
         side_effects=["config", "audio"])
def handle_feedback_full(app, remainder=""):
    """Makes Samsara speak every reply."""
    return _set(app, "full")


def _sound_step(app, delta):
    current = min(1.0, max(0.0, float(app.config.get("sound_volume", .5))))
    volume = round(min(1.0, max(0.0, current + delta)), 10)
    app.config["sound_volume"] = volume
    app.save_config()
    app.play_sound("action_complete")
    text = f"Samsara sounds {int(volume * 100)}%"
    _reply(app, text + (" (already)" if volume == current else ""))
    return True


@command("sounds quieter", aliases=["quieter sounds", "beeps quieter", "lower the sounds"],
         pack="core", risk_class="ui", ai_composable=True, side_effects=["config", "audio"])
def handle_sounds_quieter(app, remainder=""):
    """Lowers Samsara sounds and beeps by ten percentage points."""
    return _sound_step(app, -.1)


@command("sounds louder", aliases=["louder sounds", "beeps louder", "raise the sounds"],
         pack="core", risk_class="ui", ai_composable=True, side_effects=["config", "audio"])
def handle_sounds_louder(app, remainder=""):
    """Raises Samsara sounds and beeps by ten percentage points."""
    return _sound_step(app, .1)
