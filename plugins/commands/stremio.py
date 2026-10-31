"""Stremio voice control plugin.

Uses AutoHotkey v1 UIA to control Stremio — the _UIA variant handles
Electron window activation correctly.

The actual AHK plumbing lives in tools/stremio_control.py, shared with the
standalone LAN phone remote (tools/stremio_remote.py) so both surfaces stay
in sync. This module is a thin voice-command wrapper around it.
"""

import subprocess
import sys
import re
from pathlib import Path

from samsara.plugin_commands import command
from samsara.command_registry import DispatchResult, DispatchState

from samsara.log import get_logger

logger = get_logger(__name__)

# tools/ is not a samsara package -- it's a standalone-tool directory shared
# with the LAN phone remote, so it isn't importable as `samsara.tools.x`.
# Add it to sys.path (idempotent) so `import stremio_control` resolves
# regardless of how the app was launched (dev source run vs packaged exe).
# In a frozen build, this file executes from dist\Samsara\_internal\plugins\
# commands\stremio.py (plugins/commands/*.py are loaded dynamically at
# runtime -- a directory scan + exec, not a static import -- so PyInstaller
# bundles this directory as DATA, at the same relative layout as source;
# see scripts/samsara.spec), so parents[2] already correctly lands on
# _internal, matching tools/'s own datas destination there.
_TOOLS_DIR = Path(__file__).resolve().parents[2] / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

try:
    import stremio_control  # noqa: E402 -- must follow the sys.path bootstrap above
except ImportError:
    # 2026-07-10: CI's frozen smoke test hit "ModuleNotFoundError: No
    # module named 'stremio_control'" when tools/ wasn't yet bundled at
    # all (see scripts/samsara.spec's tools/ datas entry, added alongside
    # this fallback). That's fixed at the source now, but this stays as a
    # defensive second path: sys._MEIPASS is PyInstaller's own canonical
    # frozen-extraction-root attribute, decoupled from __file__.parents[N]
    # index-counting fragility (e.g. a future refactor that moves this
    # file to a different directory depth would silently break parents[2]
    # without touching this fallback).
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        _meipass_tools = Path(sys._MEIPASS) / "tools"
        if str(_meipass_tools) not in sys.path:
            sys.path.insert(0, str(_meipass_tools))
        import stremio_control  # noqa: E402 -- re-raises ImportError if this also fails
    else:
        raise

@command("pause stremio", aliases=[
    "pause the video", "pause the movie", "pause the show",
    "pause the stream", "stop the video", "stop playing",
    "hold on", "pause it"
], pack="stremio")
def handle_pause(app, remainder):
    """Pauses what is playing in Stremio."""
    print("[STREMIO] Pausing")
    return stremio_control.pause_play()


@command("resume stremio", aliases=[
    "play stremio", "resume the video", "resume the movie",
    "resume the show", "resume the stream", "continue playing",
    "unpause", "unpause stremio", "keep playing", "resume it"
], pack="stremio")
def handle_resume(app, remainder):
    """Resumes what was playing in Stremio."""
    print("[STREMIO] Resuming")
    return stremio_control.pause_play()


_PLAYER_SCOPE = {"apps": ["stremio-shell-ng.exe", "stremio.exe"]}
_SEEK_ARGS = {"remainder": {"type": "str", "required": False}}


def _seek_feedback(app, message, failed=False):
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        show(message, "warning" if failed else "success", 3500)
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is not None:
        coordinator.speak(message, category="error" if failed else "command")


def _handle_seek(app, remainder, forward):
    try:
        seconds = stremio_control.parse_seek_seconds(remainder)
    except ValueError:
        message = "Say a positive number of seconds or minutes"
        _seek_feedback(app, message, failed=True)
        return DispatchResult(DispatchState.FAILED, detail={"reason": message})
    action = stremio_control.skip_forward if forward else stremio_control.skip_back
    if not action(seconds):
        message = "Stremio seek failed"
        _seek_feedback(app, message, failed=True)
        return DispatchResult(DispatchState.FAILED, detail={"reason": message})
    _seek_feedback(app, f"{'Forward' if forward else 'Back'} {seconds} seconds")
    return True


@command("skip forward", aliases=[
    "skip ahead", "fast forward", "forward", "next bit", "jump ahead"
], pack="stremio", scope=_PLAYER_SCOPE, param_schema=_SEEK_ARGS)
def handle_skip_forward(app, remainder):
    """Jumps forward in what is playing in Stremio."""
    return _handle_seek(app, remainder, True)


@command("skip back", aliases=[
    "rewind", "back up", "skip backward", "go backwards"
], pack="stremio", scope=_PLAYER_SCOPE, param_schema=_SEEK_ARGS)
def handle_skip_back(app, remainder):
    """Jumps back in what is playing in Stremio."""
    return _handle_seek(app, remainder, False)


# A duration is required after this prefix; bare "back" belongs to the browser.
handle_skip_back.spoken_amount_prefixes = {"back": stremio_control.parse_seek_seconds}


@command("next episode", aliases=[
    "next video", "play the next episode", "skip episode"
], pack="stremio", scope=_PLAYER_SCOPE)
def handle_next_episode(app, _remainder):
    """Requests the next episode using Stremio's verified Shift+N shortcut."""
    if not stremio_control.next_episode():
        message = "Stremio next episode failed"
        _seek_feedback(app, message, failed=True)
        return DispatchResult(DispatchState.FAILED, detail={"reason": message})
    _seek_feedback(app, "Next episode requested")
    return True


@command("fullscreen", aliases=[
    "toggle fullscreen", "exit fullscreen",
    "stremio fullscreen", "go fullscreen"
], pack="stremio")
def handle_fullscreen(app, remainder):
    """Switches Stremio between fullscreen and a window."""
    print("[STREMIO] Toggling fullscreen")
    return stremio_control.fullscreen()


@command("mute stremio", aliases=["unmute stremio", "silence stremio"], pack="stremio")
def handle_mute_stremio(app, remainder):
    """Mutes or unmutes Stremio."""
    print("[STREMIO] Toggling mute")
    return stremio_control.mute()


@command("open stremio", aliases=[
    "launch stremio", "start stremio", "bring up stremio"
], pack="stremio")
def handle_open_stremio(app, remainder):
    """Opens the Stremio app."""
    print("[STREMIO] Launching")
    subprocess.Popen(
        ['cmd', '/c', 'start', '', 'stremio://'],
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    return True


@command("close stremio", aliases=["quit stremio", "exit stremio"], pack="stremio")
def handle_close_stremio(app, remainder):
    """Closes the Stremio app."""
    print("[STREMIO] Closing")
    # Was 'stremio.exe' -- stale process name, latent bug fixed 2026-07-10.
    # Stremio's process is now stremio-shell-ng.exe (stremio-runtime.exe is
    # a companion process some builds also spawn).
    stremio_control.kill_stremio()
    return True


STREMIO_EXES = {"stremio-shell-ng.exe", "stremio.exe"}


def repair_couch_command(text, context):
    """Bounded whole-phrase repairs, offered only by the command-lane wash.

    Never fuzzy-match arbitrary prose or pass repaired words as music arguments.
    The caller checks the resulting command's pack and scope before accepting it.
    """
    words = " ".join(text.lower().strip().rstrip(".!?,;:").split())
    foreground = context.resolved and not context.own_window and context.exe in STREMIO_EXES
    if words in {"postremia", "pazdremium"}:
        return "pause stremio"
    # Known phonetic app spellings, including split and possessive forms.
    app_name = r"(?:stremio(?:['’]s)?|stremia|strenio|strami out|stream io|stremium|stramio)"
    explicit = re.fullmatch(
        rf"(pause|paul|pull|paz|resume|play|unpause|open|launch|start|close|quit|exit|mute|unmute) ({app_name})", words)
    if explicit:
        verb = explicit[1]
        canonical = {"paul": "pause", "pull": "pause", "paz": "pause",
                     "play": "resume", "unpause": "resume", "launch": "open",
                     "start": "open", "quit": "close", "exit": "close", "unmute": "mute"}
        return canonical.get(verb, verb) + " stremio"
    if foreground and words in {"pause", "resume", "play", "unpause"}:
        return "pause stremio" if words == "pause" else "resume stremio"
    skip = re.fullmatch(r"(skip forward|skip back|skip backward)(?: (\w+))?", words)
    if skip and (foreground or skip[2] == "stremio"):
        # One extra token is tolerated only for these explicit transport phrases.
        return "skip forward" if skip[1] == "skip forward" else "skip back"
    return None
