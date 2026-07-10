"""Stremio voice control plugin.

Uses AutoHotkey v1 UIA to control Stremio — the _UIA variant handles
Electron window activation correctly.

The actual AHK plumbing lives in tools/stremio_control.py, shared with the
standalone LAN phone remote (tools/stremio_remote.py) so both surfaces stay
in sync. This module is a thin voice-command wrapper around it.
"""

import subprocess
import sys
from pathlib import Path

from samsara.plugin_commands import command

from samsara.log import get_logger

logger = get_logger(__name__)

# tools/ is not a samsara package -- it's a standalone-tool directory shared
# with the LAN phone remote, so it isn't importable as `samsara.tools.x`.
# Add it to sys.path (idempotent) so `import stremio_control` resolves
# regardless of how the app was launched (dev source run vs packaged exe).
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

import stremio_control  # noqa: E402 -- must follow the sys.path bootstrap above


@command("pause stremio", aliases=[
    "pause the video", "pause the movie", "pause the show",
    "pause the stream", "stop the video", "stop playing",
    "hold on", "pause it"
], pack="stremio")
def handle_pause(app, remainder):
    print("[STREMIO] Pausing")
    return stremio_control.pause_play()


@command("resume stremio", aliases=[
    "play stremio", "resume the video", "resume the movie",
    "resume the show", "resume the stream", "continue playing",
    "unpause", "unpause stremio", "keep playing", "resume it"
], pack="stremio")
def handle_resume(app, remainder):
    print("[STREMIO] Resuming")
    return stremio_control.pause_play()


@command("skip forward", aliases=[
    "skip ahead", "fast forward", "forward", "next bit"
], pack="stremio")
def handle_skip_forward(app, remainder):
    print("[STREMIO] Skipping forward")
    return stremio_control.skip_forward()


@command("skip back", aliases=[
    "rewind", "back up", "skip backward", "go backwards"
], pack="stremio")
def handle_skip_back(app, remainder):
    print("[STREMIO] Skipping back")
    return stremio_control.skip_back()


@command("fullscreen", aliases=[
    "toggle fullscreen", "exit fullscreen",
    "stremio fullscreen", "go fullscreen"
], pack="stremio")
def handle_fullscreen(app, remainder):
    print("[STREMIO] Toggling fullscreen")
    return stremio_control.fullscreen()


@command("mute stremio", aliases=["unmute stremio", "silence stremio"], pack="stremio")
def handle_mute_stremio(app, remainder):
    print("[STREMIO] Toggling mute")
    return stremio_control.mute()


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
    # Was 'stremio.exe' -- stale process name, latent bug fixed 2026-07-10.
    # Stremio's process is now stremio-shell-ng.exe (stremio-runtime.exe is
    # a companion process some builds also spawn).
    stremio_control.kill_stremio()
    return True
