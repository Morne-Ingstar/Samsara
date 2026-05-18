"""
Core utility commands — app-level controls (restart, quit, etc.).
"""

import os
import subprocess
import sys
import threading
import time

from samsara.plugin_commands import command


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
