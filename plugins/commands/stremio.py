"""Stremio voice control plugin.

Uses AutoHotkey v1 UIA to control Stremio — the _UIA variant handles
Electron window activation correctly.
"""

import subprocess
import tempfile
import os

from samsara.plugin_commands import command

from samsara.log import get_logger

logger = get_logger(__name__)

AHK_EXE = r'C:\Program Files\AutoHotkey\v1.1.37.02\AutoHotkeyU64.exe'


def _run_ahk(script):
    """Write and execute a one-shot AHK v1 script."""
    tmp = tempfile.NamedTemporaryFile(
        mode='w', suffix='.ahk', delete=False, encoding='utf-8'
    )
    tmp.write(script)
    tmp.close()
    try:
        result = subprocess.run(
            [AHK_EXE, tmp.name],
            capture_output=True, timeout=5
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors='replace')
            print(f"[STREMIO] AHK error (rc={result.returncode}): {stderr[:300]}")
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[STREMIO] AHK script timed out")
        return False
    except Exception as e:
        print(f"[STREMIO] AHK failed: {e}")
        return False
    finally:
        try:
            os.unlink(tmp.name)
        except Exception as e:
            logger.debug(f"_run_ahk: {e}")


def _send_stremio_key(key):
    """Activate Stremio via AHK UIA and send a key."""
    # AHK v1 syntax
    script = f"""#NoEnv
#SingleInstance Force
SetTitleMatchMode, 2
WinActivate, Stremio
WinWaitActive, Stremio,, 2
if ErrorLevel
    ExitApp, 1
Sleep, 150
Send, {{{key}}}
ExitApp, 0
"""
    print(f"[STREMIO] AHK v1 UIA sending: {key}")
    return _run_ahk(script)


@command("pause stremio", aliases=[
    "pause the video", "pause the movie", "pause the show",
    "pause the stream", "stop the video", "stop playing",
    "hold on", "pause it"
], pack="stremio")
def handle_pause(app, remainder):
    print("[STREMIO] Pausing")
    return _send_stremio_key("Space")


@command("resume stremio", aliases=[
    "play stremio", "resume the video", "resume the movie",
    "resume the show", "resume the stream", "continue playing",
    "unpause", "unpause stremio", "keep playing", "resume it"
], pack="stremio")
def handle_resume(app, remainder):
    print("[STREMIO] Resuming")
    return _send_stremio_key("Space")


@command("skip forward", aliases=[
    "skip ahead", "fast forward", "forward", "next bit"
], pack="stremio")
def handle_skip_forward(app, remainder):
    print("[STREMIO] Skipping forward")
    script = """#NoEnv
#SingleInstance Force
SetTitleMatchMode, 2
WinActivate, Stremio
WinWaitActive, Stremio,, 2
if ErrorLevel
    ExitApp, 1
Sleep, 150
Send, {Right 6}
ExitApp, 0
"""
    return _run_ahk(script)


@command("skip back", aliases=[
    "rewind", "back up", "skip backward", "go backwards"
], pack="stremio")
def handle_skip_back(app, remainder):
    print("[STREMIO] Skipping back")
    script = """#NoEnv
#SingleInstance Force
SetTitleMatchMode, 2
WinActivate, Stremio
WinWaitActive, Stremio,, 2
if ErrorLevel
    ExitApp, 1
Sleep, 150
Send, {Left 2}
ExitApp, 0
"""
    return _run_ahk(script)


@command("fullscreen", aliases=[
    "toggle fullscreen", "exit fullscreen",
    "stremio fullscreen", "go fullscreen"
], pack="stremio")
def handle_fullscreen(app, remainder):
    print("[STREMIO] Toggling fullscreen")
    return _send_stremio_key("f")


@command("mute stremio", aliases=["unmute stremio", "silence stremio"], pack="stremio")
def handle_mute_stremio(app, remainder):
    print("[STREMIO] Toggling mute")
    return _send_stremio_key("m")


@command("open stremio", aliases=[
    "launch stremio", "start stremio", "bring up stremio"
], pack="stremio")
def handle_open_stremio(app, remainder):
    print("[STREMIO] Launching")
    subprocess.Popen(
        ['cmd', '/c', 'start', '', 'stremio://'],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    return True


@command("close stremio", aliases=["quit stremio", "exit stremio"], pack="stremio")
def handle_close_stremio(app, remainder):
    print("[STREMIO] Closing")
    subprocess.run(
        ['taskkill', '/IM', 'stremio.exe', '/F'],
        creationflags=subprocess.CREATE_NO_WINDOW,
        capture_output=True
    )
    return True
