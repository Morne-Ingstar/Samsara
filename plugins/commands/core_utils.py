"""
Core utility commands — app-level controls (restart, quit, etc.).
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from samsara.plugin_commands import command
from samsara.runtime import thread_registry

from samsara.log import get_logger

logger = get_logger(__name__)


def start_services(app):
    """Install the user-owned alias file after the live matcher exists."""
    try:
        from samsara import plugin_commands
        from samsara.command_catalog import install_user_aliases
        install_user_aliases(plugin_commands._shared_matcher, Path(app.config_path).parent)
    except Exception as exc:
        logger.warning("[ALIASES] Could not load personal aliases: %s", exc)

def speak_if_available(app, text):
    if hasattr(app, 'audio_coordinator') and app.audio_coordinator:
        try:
            app.audio_coordinator.speak(text, category="agent_response",
                                        interruptible=False)
        except Exception as e:
            logger.debug(f"speak_if_available: {e}")


def _build_restart_args() -> tuple[list[str], str]:
    """Return (argv, cwd) for relaunching Samsara in whatever mode it's running."""
    if getattr(sys, 'frozen', False):
        # Running as a PyInstaller-compiled exe — relaunch the exe itself
        exe = sys.executable
        return [exe], os.path.dirname(exe)
    else:
        # Running from source — relaunch via the same interpreter + dictation.py
        interpreter = sys.executable
        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))),
            'dictation.py',
        )
        return [interpreter, script], os.path.dirname(script)


@command("restart samsara", aliases=["restart", "reboot samsara"], pack="core",
         risk_class="destructive",
)
def restart_app(app, remainder="", **kwargs):
    """Closes Samsara and starts it again."""
    def _do_restart():
        time.sleep(0.8)

        args, cwd = _build_restart_args()

        # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP break the child out of
        # the parent's Windows Job Object so it survives after the parent exits.
        flags = 0
        if sys.platform == 'win32':
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        try:
            subprocess.Popen(
                args,
                cwd=cwd,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[RESTART] Failed to spawn new process: {e}")
            return

        app.quit_app()

    speak_if_available(app, "Restarting.")
    thread_registry.spawn("core_utils._do_restart", _do_restart, daemon=True)


@command(
    "check for updates",
    aliases=["check for update", "update samsara"],
    pack="core",
    ai_visible=False,
    risk_class="write",
)
def check_for_updates(app, remainder="", **kwargs):
    """Checks GitHub for a newer version of Samsara."""
    from samsara.ui import qt_runtime
    from samsara.ui.update_qt import show_update_dialog

    qt_runtime.post(
        lambda: show_update_dialog(app, check_immediately=True)
    )
    return True


@command(
    "reload config",
    aliases=["refresh config", "reread config", "reload configuration"],
    pack="core",
    ai_visible=False,
    risk_class="ui",
)
def reload_config(app, remainder="", **kwargs):
    """Re-reads your settings from disk without restarting."""
    if not hasattr(app, 'reload_config_from_disk'):
        speak_if_available(app, "Config reload not available.")
        return
    try:
        n = app.reload_config_from_disk()
        if n == 0:
            speak_if_available(app, "Config reloaded. No changes.")
        else:
            speak_if_available(app, f"Config reloaded. {n} key{'s' if n != 1 else ''} changed.")
    except Exception as e:
        print(f"[CONFIG] reload_config command error: {e}")
        speak_if_available(app, "Config reload failed.")


@command(
    "what can I say",
    aliases=["help", "what are my commands", "what commands do I have"],
    pack="core",
    risk_class="read",
)
def what_can_i_say(app, remainder="", **kwargs):
    """Opens the catalog-backed commands for the focused application."""
    from samsara.command_packs import pack_for_exe
    try:
        from samsara.handlers import _get_foreground_exe_lower
        exe = _get_foreground_exe_lower()
    except Exception:
        exe = None

    from samsara.ui import qt_runtime
    from samsara.ui.command_marquee import EXAMPLE_ALLOWED_PACKS

    allowed_packs = set(EXAMPLE_ALLOWED_PACKS)
    focused_pack = pack_for_exe(exe)
    if focused_pack is not None:
        allowed_packs.add(focused_pack)

    def _show_scoped_sheet():
        cheat_sheet = getattr(app, "cheat_sheet", None)
        if cheat_sheet is not None:
            cheat_sheet.show_scoped(allowed_packs)

    # Voice commands arrive from the session worker; the cheat sheet belongs
    # to the shared Qt runtime thread.
    qt_runtime.post(_show_scoped_sheet)
    speak_if_available(app, "Showing what you can say here.")
    return True

def _history_value(row, key, default=""):
    """Read a history row without requiring a particular row implementation."""
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        getter = getattr(row, "get", None)
        return getter(key, default) if callable(getter) else default


def _last_utterance(app):
    """Return (kind, text) for the latest committed command or dictation.

    HistoryStore is the shared last-utterance record used by the history/Home
    readers. The live delivery field is preferred for dictation because it is
    the exact post-formatting string sent through the paste chokepoint.
    """
    store = getattr(app, "history_store", None)
    query = getattr(store, "query", None)
    if callable(query):
        try:
            rows = query(limit=20)
        except Exception as exc:
            logger.debug("[READBACK] history lookup failed: %s", exc)
            rows = []
        for row in rows or ():
            entry_type = str(_history_value(row, "entry_type", "dictation") or "dictation").lower()
            status = str(_history_value(row, "status", "success") or "success").lower()
            if entry_type in ("command", "wake_command"):
                name = str(_history_value(row, "matched_command", "") or "").strip()
                if not name:
                    name = str(_history_value(row, "display_text", "") or "").strip()
                if name:
                    try:
                        from samsara import outcome_ring
                        name = outcome_ring.canonical_command_label_for_phrase(name, name)
                    except Exception:
                        pass
                    return "command", name
            elif entry_type == "dictation" and status == "success":
                live = getattr(app, "_last_dictation_text", None)
                if isinstance(live, str) and live.strip():
                    return "dictation", live
                text = _history_value(row, "display_text", "")
                if isinstance(text, str) and text.strip():
                    return "dictation", text

    live = getattr(app, "_last_dictation_text", None)
    if isinstance(live, str) and live.strip():
        return "dictation", live
    return "nothing", ""


@command(
    "read that back",
    aliases=["read it back", "what did you type", "read my last dictation"],
    pack="accessibility",
    risk_class="read",
    ai_composable=False,
)
def read_that_back(app, remainder="", **kwargs):
    """Reads the latest committed dictation or command aloud for accessibility."""
    kind, text = _last_utterance(app)
    if kind == "command":
        spoken = f"The last thing was a command: {text}"
    elif kind == "dictation":
        spoken = text
    else:
        spoken = "Nothing dictated yet."

    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        try:
            show("Reading back", "accent")
        except Exception as exc:
            logger.debug("[READBACK] chip failed: %s", exc)

    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(
            spoken,
            category="dictation_readback",
            interruptible=True,
        )
    else:
        engine = getattr(app, "tts_engine", None)
        if engine is not None:
            engine.speak(spoken)
    return True



@command(
    "reset hints",
    aliases=["replay hints", "show hints again"],
    pack="core",
    ai_visible=False,
    risk_class="write",
)
def reset_hints(app, remainder="", **kwargs):
    """Clears the hints you have already seen so they can appear again."""
    hints = getattr(app, 'hints', None)
    if hints is None:
        speak_if_available(app, "Hints not available.")
        return True
    hints.reset()
    speak_if_available(app, "Hints reset.")
    return True


@command(
    "reset floating windows",
    aliases=["reset window positions", "reset live surface", "reset preview position",
             "reset indicator position"],
    pack="core",
    ai_visible=False,
    risk_class="ui",
)
def reset_floating_windows(app, remainder="", **kwargs):
    """Alias for the one live-surface recovery action when available."""
    # The combined hands-free lane is also a dictation lane. Do not take a
    # prefix out of a sentence; this is an exact whole-utterance recovery
    # command, like the draft-view controls in session_modes.
    if str(remainder or "").strip():
        return False
    from samsara.ui import qt_runtime
    from samsara.streaming import reset_preview_placement
    from samsara.ui.listening_indicator import reset_indicator_placement

    def _reset():
        surface = getattr(app, "live_surface_controller", None) or getattr(
            app, "_live_surface_controller", None)
        reset_surface = getattr(surface, "reset_placement", None)
        if callable(reset_surface) and reset_surface():
            speak_if_available(app, "Live surface reset.")
            return
        # Feature flag off: exact legacy recovery behaviour remains intact.
        reset_preview_placement(app)
        reset_indicator_placement(app)
        speak_if_available(app, "Floating windows reset.")

    # Voice commands arrive from the session worker; the live Qt widgets must
    # only be moved on the Qt runtime's thread.
    qt_runtime.post(_reset)
    return True


@command(
    "move preview to this screen",
    aliases=["move live surface to this screen"],
    pack="core",
    ai_visible=False,
    risk_class="ui",
)
def move_preview_to_this_screen(app, remainder="", **kwargs):
    """Moves the live surface's saved placement to the foreground display."""
    if str(remainder or "").strip():
        return False
    from samsara.ui import qt_runtime

    def _move():
        surface = getattr(app, "live_surface_controller", None) or getattr(
            app, "_live_surface_controller", None)
        mover = getattr(surface, "move_to_foreground_screen", None)
        if callable(mover) and mover():
            speak_if_available(app, "Live surface moved to this screen.")
        else:
            speak_if_available(app, "Live surface is not available.")

    qt_runtime.post(_move)
    return True


@command("save alias", aliases=["remove alias"], pack="core", risk_class="write")
def personal_alias(app, remainder="", **kwargs):
    """Saves the offered wording; say "remove alias <words>" to undo it."""
    from samsara.command_catalog import (
        install_user_aliases, remove_user_alias, save_user_alias,
    )

    home_dir = Path(app.config_path).parent
    matcher = getattr(getattr(app, "command_executor", None), "_matcher", None)
    alias = str(remainder or "").strip()
    if alias:
        if remove_user_alias(alias, home_dir, matcher):
            speak_if_available(app, "Alias removed.")
        else:
            speak_if_available(app, "I could not find that alias.")
        return True
    offer = getattr(app, "_personal_alias_offer", None)
    if not offer or time.monotonic() > offer.get("expires", 0):
        app._personal_alias_offer = None
        speak_if_available(app, "There is no alias waiting to save.")
        return True
    alias, canonical = offer["miss"], offer["canonical"]
    existing = getattr(matcher, "_entries", {}).get(alias) if matcher is not None else None
    if existing is not None and existing.phrase != canonical:
        speak_if_available(app, "That phrase already belongs to another command.")
        return True
    if save_user_alias(alias, canonical, home_dir):
        install_user_aliases(matcher, home_dir)
        app._personal_alias_offer = None
        speak_if_available(app, f"Saved {alias} as another way to say {canonical}.")
    return True


_LAST_OUTCOME_REPORT_SOURCE = "spoken_last_outcome"
_COMMAND_EXECUTED_SOURCES = frozenset({
    "command_executed", "hands_free_command_executed",
})
_COMMAND_FAILED_SOURCES = frozenset({
    "command_failed", "hands_free_command_failed",
    "hands_free_command_blocked", "hands_free_command_refused",
})


def _last_outcome_record(app):
    """Return the latest real chip record, skipping this command's own report."""
    from samsara import outcome_ring

    for item in reversed(list(getattr(app, "_outcome_ring", None) or ())):
        record = outcome_ring.as_record(item)
        if record is not None and record.source != _LAST_OUTCOME_REPORT_SOURCE:
            return record
    return None


def _diagnostic_timestamp(record):
    try:
        return datetime.fromisoformat(str(record.ts)).timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _heard_for_outcome(app, outcome):
    """Read the utterance from the same diagnostics source Voice Help uses.

    The outcome chip is rung after dispatch, so the matching diagnostics row
    is the newest non-empty row at or before the chip timestamp. Falling back
    to the newest text keeps old records and small test apps useful when their
    clocks do not carry timestamps.
    """
    try:
        from samsara.ui.voice_help_qt import _diag
        records = [r for r in _diag(app) if str(getattr(r, "text", "")).strip()]
    except Exception as exc:
        logger.debug("last outcome diagnostics unavailable: %s", exc)
        records = []
    if not records:
        return str(getattr(app, "_last_miss_text", "") or "").strip()

    before = []
    for record in records:
        stamp = _diagnostic_timestamp(record)
        if stamp is not None and outcome is not None and stamp <= outcome.at + 0.5:
            before.append(record)
    chosen = before[-1] if before else records[-1]
    return str(getattr(chosen, "text", "") or "").strip()


def _nearest_similarity_command(app, heard):
    if not heard:
        return ""
    try:
        from samsara.intent.resolve import IntentResolver

        matcher = getattr(getattr(app, "command_executor", None), "_matcher", None)
        rows = matcher.list_commands() if matcher is not None else None
        resolver = IntentResolver(rows=rows) if rows else IntentResolver()
        resolution = resolver.resolve(heard)
        if resolution.tier != "similarity":
            return ""
        command_id = (resolution.suggestions or (resolution.canonical_id,))[0]
        record = resolver.by_id.get(command_id)
        if record is None:
            return ""
        from samsara import command_catalog
        return command_catalog.canonical_phrase(record)
    except Exception as exc:
        logger.debug("last outcome similarity lookup unavailable: %s", exc)
        return ""


def _outcome_result(outcome, heard, app):
    from samsara import outcome_ring

    if outcome is None:
        return "No recent outcome was recorded."

    label = str(outcome.label or "").strip()
    source = str(outcome.source or "")
    if label == "typed" and outcome.kind == "success":
        result = "typed"
    elif source in _COMMAND_EXECUTED_SOURCES:
        fragment = label.lstrip(chr(0x2713)).strip()
        command = outcome_ring.canonical_command_label(outcome.canonical_id, fragment)
        result = f"ran {command}" if command else "ran the command"
    elif outcome_ring.is_command_miss(outcome):
        result = "MISS"
        nearest = _nearest_similarity_command(app, heard)
        if nearest:
            result += f". Nearest command at similarity tier: {nearest}"
    elif label.lower().startswith("ava"):
        result = label
    elif label.lower().startswith("refused:"):
        result = label
    elif source in _COMMAND_FAILED_SOURCES and outcome.kind == "warning":
        result = f"refused: {label or 'unknown reason'}"
    elif outcome.kind == "error":
        reason = label.lstrip(chr(0x2717)).strip()
        result = f"failed: {reason}" if reason else "failed"
    else:
        result = label or "no outcome"

    heard_part = f'Heard "{heard}". ' if heard else "No utterance was recorded. "
    return heard_part + result + "."


def _show_last_outcome_chip(app, text):
    """Show the answer through the normal chip path, even when TTS is off."""
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        try:
            show(text, "accent", 8000, source=_LAST_OUTCOME_REPORT_SOURCE)
        except TypeError:
            # Small compatibility stubs from older app/test seams have no
            # source keyword; they can still show the chip.
            show(text, "accent", 8000)
        return
    indicator = getattr(app, "listening_indicator", None)
    if indicator is None or not hasattr(indicator, "show_outcome"):
        return
    try:
        schedule = getattr(app, "_schedule_ui", None)
        if callable(schedule):
            schedule(indicator.show_outcome, text, "accent", 8000)
        else:
            indicator.show_outcome(text, "accent", 8000)
    except Exception as exc:
        logger.debug("last outcome chip unavailable: %s", exc)


@command(
    "why didn't that work",
    aliases=["what just happened", "what did you hear"],
    pack="core",
    ai_visible=False,
    ai_composable=False,
    risk_class="read",
)
def explain_last_outcome(app, remainder="", **kwargs):
    """Speaks and shows the most recent recorded outcome chip."""
    outcome = _last_outcome_record(app)
    heard = _heard_for_outcome(app, outcome)
    text = _outcome_result(outcome, heard, app)
    _show_last_outcome_chip(app, text)
    speak_if_available(app, text)
    return True
