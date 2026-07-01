"""Samsara Mobile Companion -- Phase 2/3 real-control handlers.

These run on the bridge's single dedicated dispatch thread (see bridge.py's
`_dispatch_loop`), which is what makes it safe to call into Samsara's
COM-based backends here: volume.py's `_audio` singleton lazily calls
CoInitialize and caches the result per-instance, so the first call on this
thread initializes COM once and every later call on the same thread reuses
that apartment. Handler functions must never be called from any thread other
than the bridge's dispatch thread.

Handlers reuse the SAME backend code the voice-command plugins use --
plugins/commands/volume.py's `_audio` singleton and
plugins/commands/media_keys.py's foreground-aware `_send_action` /
`_get_session_for_process` -- instead of re-implementing COM/SMTC access
(the quarantined mobile_companion.py.disabled duplicated this logic; this
subsystem intentionally doesn't). Both plugin modules are already imported
by the plugin loader before this subsystem starts, so importing them here
is a sys.modules lookup, not fresh module-scope I/O.

The app-targeted transport handler (make_app_transport_handler) is ported
from mobile_companion.py.disabled's _smtc_transport_for_app /
_keystroke_transport, adapted to reuse media_keys.py's session lookup
instead of a second hand-rolled copy of it.
"""

VALID_MUTE_ACTIONS = ("toggle", "mute", "unmute")
VALID_TRANSPORT_ACTIONS = ("play", "pause", "toggle", "next", "previous")

# Default target for app-targeted transport when the caller doesn't specify
# one (e.g. the PWA's "Stremio" mode).
DEFAULT_APP_PROCESS = "stremio.exe"

# Apps confirmed (via tools/stremio_smtc_diag.py) to never register an SMTC
# session regardless of focus/playback state. For these, app-targeted
# transport falls back to driving in-app keyboard shortcuts instead of SMTC.
KEYSTROKE_FALLBACK_APPS = ("stremio",)

# Virtual-key codes for the keystroke fallback.
_VK_SPACE = 0x20
_VK_MEDIA_NEXT_TRACK = 0xB0
_VK_MEDIA_PREV_TRACK = 0xB1
_VK_MENU = 0x12
_KEYEVENTF_KEYUP = 0x0002

# Stremio's web player binds play/pause to the spacebar and has no dedicated
# play-only/pause-only shortcut, so "play"/"pause"/"toggle" all map to the
# same key. There's no next/previous-episode shortcut either, but the media
# next/prev keys are forwarded in case a future Stremio build picks them up.
_KEYSTROKE_ACTIONS = {
    "play": _VK_SPACE,
    "pause": _VK_SPACE,
    "toggle": _VK_SPACE,
    "next": _VK_MEDIA_NEXT_TRACK,
    "previous": _VK_MEDIA_PREV_TRACK,
}

# Time to let SetForegroundWindow actually take effect before the synthetic
# keystroke is sent -- too short and the keystroke can land on the previously
# focused window instead.
_KEYSTROKE_FOCUS_SETTLE_SECONDS = 0.05


def _volume_backend():
    from plugins.commands.volume import _audio
    return _audio


def _foreground_process_name():
    from plugins.commands.media_keys import _get_foreground_process_name
    return _get_foreground_process_name()


def _run_transport(action):
    from plugins.commands.media_keys import _send_action, _run_async
    result = _run_async(_send_action(action))
    if result is None:
        return False, "async transport failure"
    return result


def make_status_handler():
    """GET-style status: current volume/mute/foreground app. Read-only."""
    def _status(params):
        audio = _volume_backend()
        vol = audio.get_volume()
        muted = audio.get_mute()
        return {
            "ok": True,
            "volume": round(vol * 100) if vol is not None else None,
            "muted": muted,
            "foreground_app": _foreground_process_name(),
        }
    return _status


def make_volume_set_handler():
    """params: {"level": 0-100}. Sets system volume, returns the new level."""
    def _volume_set(params):
        level = params.get("level")
        if level is None:
            return {"ok": False, "error": "missing 'level'"}
        try:
            level = float(level)
        except (TypeError, ValueError):
            return {"ok": False, "error": "'level' must be a number"}
        audio = _volume_backend()
        ok = audio.set_volume(level / 100.0)
        new_vol = audio.get_volume()
        return {
            "ok": ok,
            "volume": round(new_vol * 100) if new_vol is not None else None,
        }
    return _volume_set


def make_mute_set_handler():
    """params: {"action": "toggle" | "mute" | "unmute"}."""
    def _mute_set(params):
        action = params.get("action", "toggle")
        if action not in VALID_MUTE_ACTIONS:
            return {"ok": False, "error": f"unknown mute action: {action}"}
        audio = _volume_backend()
        if action == "toggle":
            current = audio.get_mute()
            target = (not current) if current is not None else True
        else:
            target = action == "mute"
        ok = audio.set_mute(target)
        return {"ok": ok, "muted": target}
    return _mute_set


def make_transport_handler():
    """params: {"action": "play"|"pause"|"toggle"|"next"|"previous"}.

    Targets the foreground app's SMTC session (same routing as media_keys.py's
    voice commands), not just "whatever Windows thinks is current".
    """
    def _transport(params):
        action = params.get("action")
        if action not in VALID_TRANSPORT_ACTIONS:
            return {"ok": False, "error": f"unknown transport action: {action}"}
        ok, message = _run_transport(action)
        return {"ok": bool(ok), "action": action, "message": message}
    return _transport


def _find_window_for_process(process_name):
    """Return the first visible, titled top-level hwnd belonging to process_name."""
    import win32gui
    import win32process
    import psutil

    bare = process_name.replace(".exe", "").lower()
    found = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname = psutil.Process(pid).name().lower()
        except Exception:
            return
        if bare in pname:
            found.append(hwnd)

    win32gui.EnumWindows(_cb, None)
    return found[0] if found else None


def _focus_window(hwnd):
    """Bring hwnd to the foreground, working around Windows' foreground lock.

    Windows refuses SetForegroundWindow from a background process unless the
    calling thread "owns" the foreground, which this dispatch thread never
    does. Bracketing the call with a synthetic Alt keypress satisfies that
    check (the same trick window managers/automation tools use).
    """
    import ctypes
    import win32gui

    user32 = ctypes.windll.user32
    user32.keybd_event(_VK_MENU, 0, 0, 0)
    try:
        win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
        win32gui.SetForegroundWindow(hwnd)
    finally:
        user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)


def _keystroke_transport(action, process_name):
    """Drive play/pause/next/previous via simulated keystrokes to a window."""
    import ctypes
    import time
    import win32gui

    vk = _KEYSTROKE_ACTIONS.get(action)
    if vk is None:
        return False, f"unknown action: {action}"

    hwnd = _find_window_for_process(process_name)
    if not hwnd:
        return False, f"no window for {process_name}"

    previous_hwnd = win32gui.GetForegroundWindow()
    try:
        _focus_window(hwnd)
        time.sleep(_KEYSTROKE_FOCUS_SETTLE_SECONDS)
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)
    except Exception as e:
        return False, f"keystroke failed: {e}"
    finally:
        if previous_hwnd and previous_hwnd != hwnd:
            try:
                _focus_window(previous_hwnd)
            except Exception:
                pass

    return True, f"keystroke:{process_name}"


def _run_app_transport(action, app_process):
    """Send a transport command to a specific app's SMTC session, by process name.

    Unlike _run_transport(), this never looks at the foreground window --
    it targets app_process directly, so phone controls can reach e.g.
    Stremio regardless of what's currently focused. Falls back to
    _keystroke_transport for apps confirmed to never expose SMTC.
    """
    from plugins.commands.media_keys import _get_session_for_process, _run_async

    async def _do():
        session = await _get_session_for_process(app_process)
        if not session:
            return None
        if action == "play":
            ok = await session.try_play_async()
        elif action == "pause":
            ok = await session.try_pause_async()
        elif action == "toggle":
            ok = await session.try_toggle_play_pause_async()
        elif action == "next":
            ok = await session.try_skip_next_async()
        elif action == "previous":
            ok = await session.try_skip_previous_async()
        else:
            return None
        return bool(ok)

    result = _run_async(_do())
    if result is not None:
        return result, f"app:{app_process}"

    if any(hint in app_process.lower() for hint in KEYSTROKE_FALLBACK_APPS):
        return _keystroke_transport(action, app_process)

    return False, f"no media session for {app_process}"


def make_app_transport_handler():
    """params: {"action": ..., "app": <process name, optional>}.

    Targets a specific app's SMTC session (falls back to keystrokes for apps
    that never expose one), regardless of what's currently in the foreground.
    """
    def _app_transport(params):
        action = params.get("action")
        if action not in VALID_TRANSPORT_ACTIONS:
            return {"ok": False, "error": f"unknown transport action: {action}"}
        app_process = params.get("app") or DEFAULT_APP_PROCESS
        ok, message = _run_app_transport(action, app_process)
        return {"ok": bool(ok), "action": action, "app": app_process, "message": message}
    return _app_transport
