"""
Core utility commands — app-level controls (restart, quit, etc.).
"""

import os
import subprocess
import sys
import time

from samsara.plugin_commands import command
from samsara.runtime import thread_registry

from samsara.log import get_logger

logger = get_logger(__name__)

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
    aliases=["reset window positions"],
    pack="core",
    ai_visible=False,
    risk_class="ui",
)
def reset_floating_windows(app, remainder="", **kwargs):
    """Restores both floating windows to their default positions."""
    # The combined hands-free lane is also a dictation lane. Do not take a
    # prefix out of a sentence; this is an exact whole-utterance recovery
    # command, like the draft-view controls in session_modes.
    if str(remainder or "").strip():
        return False
    from samsara.ui import qt_runtime
    from samsara.streaming import reset_preview_placement
    from samsara.ui.listening_indicator import reset_indicator_placement

    def _reset():
        reset_preview_placement(app)
        reset_indicator_placement(app)
        speak_if_available(app, "Floating windows reset.")

    # Voice commands arrive from the session worker; the live Qt widgets must
    # only be moved on the Qt runtime's thread.
    qt_runtime.post(_reset)
    return True
