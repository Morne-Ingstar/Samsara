"""Media key routing — give play/pause to the focused app's SMTC session.

Windows routes Bluetooth media keys through SMTC's "current session" arbitration,
which often sends the keypress to the wrong app. This plugin bypasses arbitration
entirely by finding the foreground app's SMTC session and calling WinRT methods
on it directly.

Commands:
    "pause this"          — pause focused app's media
    "play this"           — play focused app's media
    "toggle this"         — toggle play/pause in focused app
    "next track this"     — skip forward in focused app
    "previous track this" — skip back in focused app
"""

import asyncio
import logging

import psutil
import win32gui
import win32process

from samsara.plugin_commands import command

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_foreground_process_name():
    """Return the lowercase .exe name of the foreground window's process."""
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return None
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


async def _get_session_for_process(target_process_name):
    """Return the SMTC session whose source ID contains the process stem.

    source_app_user_model_id examples:
        Spotify   -> "Spotify.Spotify"
        Stremio   -> "org.stremio.Stremio" or "stremio.exe"
        Firefox   -> "Firefox-308046B0AF4A39CB" (varies by install)
        Chrome    -> "Google.Chrome...."

    Strategy: strip .exe, lowercase, substring match.
    """
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )
    except ImportError:
        logger.warning("[MEDIA KEYS] winsdk not available -- SMTC commands disabled")
        return None
    try:
        manager = await SessionManager.request_async()
    except Exception as e:
        logger.error("[MEDIA KEYS] SessionManager unavailable: %s", e)
        return None
    sessions = manager.get_sessions()
    bare = target_process_name.replace('.exe', '').lower()
    for session in sessions:
        try:
            source = session.source_app_user_model_id.lower()
        except Exception:
            continue
        if bare in source:
            return session
    return None


def _run_async(coro):
    """Run an async coroutine from synchronous command-handler code.

    Creates a fresh event loop per call — slow (~5-10ms) but acceptable
    for infrequent voice commands. No persistent background loop needed.
    Returns None on any exception.
    """
    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    except Exception as e:
        logger.error("[MEDIA KEYS] Async action failed: %s", e)
        return None


async def _send_action(action_name):
    """Dispatch a media action to the foreground app's SMTC session.

    Returns (ok: bool, message: str).
    """
    proc = _get_foreground_process_name()
    if not proc:
        return False, "no foreground app"

    session = await _get_session_for_process(proc)
    if not session:
        return False, f"{proc} has no media session"

    try:
        if action_name == 'play':
            ok = await session.try_play_async()
        elif action_name == 'pause':
            ok = await session.try_pause_async()
        elif action_name == 'toggle':
            ok = await session.try_toggle_play_pause_async()
        elif action_name == 'next':
            ok = await session.try_skip_next_async()
        elif action_name == 'previous':
            ok = await session.try_skip_previous_async()
        else:
            return False, f"unknown action: {action_name}"
    except Exception as e:
        return False, f"{action_name} failed: {e}"

    return bool(ok), f"{action_name} sent to {proc}"


# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("pause this", aliases=["pause focused", "pause active", "pause music", "stop music", "play pause"],
         pack="media", debounce=1.5)
def handle_pause_this(app, remainder):
    ok, msg = _run_async(_send_action('pause')) or (False, "error")
    print(f"[MEDIA KEYS] {msg}")
    return True


@command("play this", aliases=["play focused", "resume this"],
         pack="media", debounce=1.5)
def handle_play_this(app, remainder):
    ok, msg = _run_async(_send_action('play')) or (False, "error")
    print(f"[MEDIA KEYS] {msg}")
    return True


@command("toggle this", aliases=["toggle focused"],
         pack="media", debounce=1.5)
def handle_toggle_this(app, remainder):
    ok, msg = _run_async(_send_action('toggle')) or (False, "error")
    print(f"[MEDIA KEYS] {msg}")
    return True


@command("next track this", aliases=["next this", "skip this", "next track", "next song", "skip song"],
         pack="media", debounce=0.8)
def handle_next_this(app, remainder):
    ok, msg = _run_async(_send_action('next')) or (False, "error")
    print(f"[MEDIA KEYS] {msg}")
    return True


@command("previous track this", aliases=["previous this", "back this", "previous track", "previous song", "back a song", "previous"],
         pack="media", debounce=0.8)
def handle_prev_this(app, remainder):
    ok, msg = _run_async(_send_action('previous')) or (False, "error")
    print(f"[MEDIA KEYS] {msg}")
    return True
