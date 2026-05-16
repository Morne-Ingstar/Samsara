"""
Core utility commands — app-level controls (restart, quit, etc.).
"""

import subprocess
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


@command("restart samsara", aliases=["restart", "reboot samsara"], pack="core")
def restart_app(app, remainder="", **kwargs):
    def _do_restart():
        time.sleep(0.5)
        subprocess.Popen(
            [r'F:\envs\sami\python.exe', r'dictation.py'],
            cwd=r'C:\Users\Morne\Projects\Samsara-dev',
        )
        app.quit_app()

    speak_if_available(app, "Restarting.")
    threading.Thread(target=_do_restart, daemon=True).start()
