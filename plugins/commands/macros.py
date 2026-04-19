"""Multi-step "Tony Stark" voice macros.

Each macro chains 3-5 actions into one spoken command. NirCmd calls go
through the audio_switch module's finder, so if nircmd.exe is absent the
volume step silently no-ops while the rest of the macro still runs.

Volume scale (NirCmd setsysvolume): 0-65535.
  13107 = ~20%, 32768 = ~50%, 52428 = ~80%, 65535 = 100%.
"""

import subprocess
import time
import webbrowser

import pyautogui

from samsara.plugin_commands import command


def _nircmd(args):
    """Run a NirCmd command. Returns True if successful."""
    from samsara.audio_switch import _find_nircmd
    nircmd = _find_nircmd()
    if not nircmd:
        return False
    try:
        subprocess.run([nircmd] + args, check=True, capture_output=True)
        return True
    except Exception:
        return False


@command("going dark", aliases=["end of day", "shut it down",
                                "goodnight"])
def going_dark(app, remainder):
    """Mute, minimize everything, lock screen."""
    print("[MACRO] Going dark...")
    _nircmd(["mutesysvolume", "1"])       # mute system audio
    time.sleep(0.1)
    pyautogui.hotkey('win', 'd')          # show desktop (minimize all)
    time.sleep(0.3)
    pyautogui.hotkey('win', 'l')          # lock screen
    print("[MACRO] Goodnight.")
    return True


@command("focus mode", aliases=["time to work", "let's work"])
def focus_mode(app, remainder):
    """Low volume, open IDE, minimal distractions."""
    print("[MACRO] Entering focus mode...")
    _nircmd(["setsysvolume", "13107"])     # ~20% volume
    time.sleep(0.1)
    pyautogui.hotkey('win', 'd')          # clear the desktop
    time.sleep(0.3)
    try:
        subprocess.Popen(["code"], shell=True)
        print("[MACRO] VS Code launched")
    except Exception:
        print("[MACRO] VS Code not found, skipping")
    return True


@command("break time", aliases=["take a break", "stretch break"])
def break_time(app, remainder):
    """Pause media, lock screen for a break."""
    print("[MACRO] Break time...")
    pyautogui.press('playpause')          # pause whatever is playing
    time.sleep(0.2)
    pyautogui.hotkey('win', 'l')          # lock screen
    print("[MACRO] Go stretch.")
    return True


@command("morning routine", aliases=["good morning", "start my day"])
def morning_routine(app, remainder):
    """Open daily sites, set comfortable volume."""
    print("[MACRO] Good morning...")
    _nircmd(["setsysvolume", "32768"])     # ~50% volume
    time.sleep(0.2)
    shortcuts = {}
    if app is not None and hasattr(app, 'config'):
        shortcuts = app.config.get('web_shortcuts', {}) or {}
    morning_sites = ['mail', 'email', 'github']
    opened = 0
    for site in morning_sites:
        if site in shortcuts:
            webbrowser.open(shortcuts[site])
            opened += 1
            time.sleep(0.3)  # stagger so browser doesn't choke
    if opened == 0:
        webbrowser.open("https://mail.google.com")
    print(f"[MACRO] Opened {opened} morning sites.")
    return True


@command("presentation mode", aliases=["demo mode"])
def presentation_mode(app, remainder):
    """Maximize current window, full-ish volume."""
    print("[MACRO] Presentation mode...")
    _nircmd(["setsysvolume", "52428"])     # ~80% volume
    time.sleep(0.1)
    pyautogui.hotkey('win', 'up')         # maximize current window
    time.sleep(0.2)
    # Win+N opens the notification center but there's no clean DND
    # toggle hotkey -- skip automating that for now.
    print("[MACRO] Ready to present.")
    return True


@command("clear my desk", aliases=["hide everything",
                                   "clean desktop"])
def clear_desk(app, remainder):
    """Minimize all windows."""
    print("[MACRO] Clearing desktop...")
    pyautogui.hotkey('win', 'd')
    return True
