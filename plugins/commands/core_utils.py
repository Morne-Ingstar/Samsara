"""
Core utility commands — app-level controls (restart, quit, etc.).
"""

import os
import subprocess
import sys
import threading
import time

from samsara.plugin_commands import command

# ---------------------------------------------------------------------------
# Per-app suggestion map for "what can I say"
# ---------------------------------------------------------------------------

_APP_SUGGESTIONS = {
    'chrome.exe':   "Try 'new tab', 'close tab', 'find tab', 'show numbers', 'scroll down', 'go to address bar'.",
    'msedge.exe':   "Try 'new tab', 'close tab', 'find tab', 'show numbers', 'scroll down', 'go to address bar'.",
    'firefox.exe':  "Try 'new tab', 'close tab', 'find tab', 'show numbers', 'scroll down', 'go to address bar'.",
    'code.exe':     "Try 'save', 'undo', 'select all', 'show numbers', 'scroll down'.",
    'discord.exe':  "Try 'scroll down', 'show numbers', 'mute'.",
    'slack.exe':    "Try 'scroll down', 'show numbers'.",
    'notepad.exe':  "Try 'select all', 'undo', 'save'.",
    'explorer.exe': "Try 'show numbers', 'scroll down'.",
}

_FALLBACK_SUGGESTION = (
    "Try 'show numbers' to click by voice, 'scroll down', 'undo', 'select all'."
    " Say 'open cheat sheet' for the full list."
)


def speak_if_available(app, text):
    if hasattr(app, 'audio_coordinator') and app.audio_coordinator:
        try:
            app.audio_coordinator.speak(text, category="agent_response",
                                        interruptible=False)
        except Exception:
            pass


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


@command("restart samsara", aliases=["restart", "reboot samsara"], pack="core")
def restart_app(app, remainder="", **kwargs):
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
    threading.Thread(target=_do_restart, daemon=True).start()


@command(
    "reload config",
    aliases=["refresh config", "reread config", "reload configuration"],
    pack="core",
    ai_visible=False,
)
def reload_config(app, remainder="", **kwargs):
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
)
def what_can_i_say(app, remainder="", **kwargs):
    """Speak the most relevant commands for the current foreground app."""
    try:
        from samsara.handlers import _get_foreground_exe_lower
        exe = _get_foreground_exe_lower()
        msg = _APP_SUGGESTIONS.get(exe or '', _FALLBACK_SUGGESTION)
    except Exception:
        msg = _FALLBACK_SUGGESTION
    speak_if_available(app, msg)
    return True


@command(
    "reset hints",
    aliases=["replay hints", "show hints again"],
    pack="core",
    ai_visible=False,
)
def reset_hints(app, remainder="", **kwargs):
    """Clear hint history so all contextual hints fire again."""
    hints = getattr(app, 'hints', None)
    if hints is None:
        speak_if_available(app, "Hints not available.")
        return True
    hints.reset()
    speak_if_available(app, "Hints reset.")
    return True
