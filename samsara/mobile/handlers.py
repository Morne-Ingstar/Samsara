"""Samsara Mobile Companion -- Phase 2 real-control handlers.

These run on the bridge's single dedicated dispatch thread (see bridge.py's
`_dispatch_loop`), which is what makes it safe to call into Samsara's
COM-based backends here: volume.py's `_audio` singleton lazily calls
CoInitialize and caches the result per-instance, so the first call on this
thread initializes COM once and every later call on the same thread reuses
that apartment. Handler functions must never be called from any thread other
than the bridge's dispatch thread.

Handlers reuse the SAME backend code the voice-command plugins use --
plugins/commands/volume.py's `_audio` singleton and
plugins/commands/media_keys.py's foreground-aware `_send_action` -- instead
of re-implementing COM/SMTC access (the quarantined mobile_companion.py.disabled
duplicated this logic; this subsystem intentionally doesn't). Both plugin
modules are already imported by the plugin loader before this subsystem
starts, so importing them here is a sys.modules lookup, not fresh
module-scope I/O.
"""

VALID_MUTE_ACTIONS = ("toggle", "mute", "unmute")
VALID_TRANSPORT_ACTIONS = ("play", "pause", "toggle", "next", "previous")


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
