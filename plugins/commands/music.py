"""Music playback plugin.

Play music by voice. Supports YouTube links and local files.
Volume control via Windows audio API.

"Jarvis, play some sad music"     - plays the configured sad song
"Jarvis, play funeral march"      - plays Chopin's Funeral March
"Jarvis, volume down"             - lower system volume
"Jarvis, volume up"               - raise system volume
"Jarvis, mute"                    - toggle mute
"""

import webbrowser
import subprocess
import threading
import os
import asyncio
import re
import time
from urllib.parse import quote

from samsara.command_registry import DispatchResult, DispatchState
from samsara.command_registry import view_tokens

from samsara.plugin_commands import command

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


_NAMED_APPS = {
    "spotify": ("spotify.exe", "spotify", "Spotify", "spotify", ("transport", "volume")),
}

_PLAYER_PHRASES = {
    "pause": ("transport", "pause {app}", ("stop {app}",)),
    "play": ("transport", "play {app}", ("resume {app}",)),
    "volume_up": ("volume", "{app} volume up", ("{app} louder", "turn {app} up")),
    "volume_down": ("volume", "{app} volume down", ("{app} quieter", "turn {app} down")),
}


def _media_transport(action, app_stem=None):
    """Send a transport command to the current SMTC session.

    action: "play" | "pause" | "next"
    Acts on whatever session currently holds SMTC foreground — identical
    behavior to a hardware media key / Bluetooth earbud button. Not
    Spotify-bound by design; works with browsers, VLC, Spotify, etc.
    Returns a completed or failed DispatchResult. A transport refusal is a
    claimed command failure, not a dictation fall-through.
    """
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        async def _transport():
            try:
                mgr = await SessionManager.request_async()
                display = next((row[2] for row in _NAMED_APPS.values()
                                if row[1] == app_stem), app_stem)
                if app_stem is None:
                    session = mgr.get_current_session()
                else:
                    session = next((item for item in mgr.get_sessions()
                                    if app_stem.casefold() in
                                    item.source_app_user_model_id.casefold()), None)
                if not session:
                    return DispatchResult(
                        DispatchState.FAILED, phrase=action,
                        detail={"reason": (f"{display} has no media session"
                                           if app_stem is not None else "no media app found")},
                    )
                if action == "play":
                    ok = await session.try_play_async()
                elif action == "pause":
                    ok = await session.try_pause_async()
                elif action == "next":
                    ok = await session.try_skip_next_async()
                elif action == "previous":
                    ok = await session.try_skip_previous_async()
                else:
                    return DispatchResult(
                        DispatchState.FAILED, phrase=action,
                        detail={"reason": "unknown media action"},
                    )
                if not ok:
                    return DispatchResult(
                        DispatchState.FAILED, phrase=action,
                        detail={"reason": "nothing is playing"},
                    )
                print(f"[MEDIA] transport: {action}")
                message = {"pause": "paused music", "play": "resumed music",
                           "next": "next track", "previous": "previous track"}[action]
                if app_stem is not None:
                    message = f"{display} " + {"pause": "paused", "play": "playing",
                                               "next": "next track", "previous": "previous track"}[action]
                return DispatchResult(DispatchState.COMPLETED, phrase=action,
                                      detail={"media_action": action, "message": message})
            except Exception as e:
                print(f"[MEDIA] transport {action} failed: {e}")
                return DispatchResult(
                    DispatchState.FAILED, phrase=action,
                    detail={"reason": "media transport unavailable"},
                )

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_transport())
        finally:
            loop.close()
    except Exception as e:
        print(f"[MEDIA] transport init failed: {e}")
        return DispatchResult(
            DispatchState.FAILED, phrase=action,
            detail={"reason": "media transport unavailable"},
        )


from contextlib import contextmanager


@contextmanager
def _audio_sessions(process_name):
    """Yield every matching render session, releasing COM even on early exit."""
    import ctypes
    import struct
    import psutil
    from ctypes import POINTER, HRESULT, byref, cast, c_void_p

    def guid(value):
        parts = value.split('-')
        return struct.pack('<IHH', int(parts[0], 16), int(parts[1], 16),
                           int(parts[2], 16)) + bytes.fromhex(parts[3] + parts[4])

    def vf(ptr, index, restype, *argtypes):
        table = cast(ptr, POINTER(POINTER(c_void_p)))
        return ctypes.WINFUNCTYPE(restype, *argtypes)(table[0][index])

    def release(ptr):
        if ptr:
            vf(ptr, 2, ctypes.c_ulong, c_void_p)(ptr)

    def checked(hr):
        if hr < 0:
            raise OSError(f'Core Audio HRESULT {hr:#010x}')

    class Volume:
        def __init__(self, ptr):
            self.ptr = ptr

        def get_volume(self):
            value = ctypes.c_float()
            checked(vf(self.ptr, 4, HRESULT, c_void_p, POINTER(ctypes.c_float))(
                self.ptr, byref(value)))
            return round(value.value * 100)

        def set_volume(self, percent):
            checked(vf(self.ptr, 3, HRESULT, c_void_p, ctypes.c_float, c_void_p)(
                self.ptr, percent / 100.0, None))

        def toggle_mute(self):
            muted = ctypes.c_int()
            checked(vf(self.ptr, 6, HRESULT, c_void_p, POINTER(ctypes.c_int))(
                self.ptr, byref(muted)))
            checked(vf(self.ptr, 5, HRESULT, c_void_p, ctypes.c_int, c_void_p)(
                self.ptr, 0 if muted.value else 1, None))
            return True

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize.argtypes = [c_void_p]
    ole32.CoInitialize.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    ole32.CoCreateInstance.argtypes = [
        c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = HRESULT
    checked(ole32.CoInitialize(None))
    enum, device, manager, sessions = (c_void_p() for _ in range(4))

    owned_volumes = []

    def matching():
        count = ctypes.c_int()
        checked(vf(sessions, 3, HRESULT, c_void_p, POINTER(ctypes.c_int))(
            sessions, byref(count)))
        for index in range(count.value):
            ctrl, ctrl2, volume = (c_void_p() for _ in range(3))
            try:
                hr = vf(sessions, 4, HRESULT, c_void_p, ctypes.c_int, POINTER(c_void_p))(
                    sessions, index, byref(ctrl))
                if hr < 0 or not ctrl:
                    continue
                hr = vf(ctrl, 0, HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
                    ctrl, guid('bfb7ff88-7239-4fc9-8fa2-07c950be9c6d'), byref(ctrl2))
                if hr < 0 or not ctrl2:
                    continue
                pid = ctypes.c_ulong()
                hr = vf(ctrl2, 14, HRESULT, c_void_p, POINTER(ctypes.c_ulong))(
                    ctrl2, byref(pid))
                if hr < 0 or not pid.value:
                    continue
                try:
                    name = psutil.Process(pid.value).name().casefold()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if name != process_name.casefold():
                    continue
                checked(vf(ctrl2, 0, HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
                    ctrl2, guid('87CE5498-68D6-44E5-9215-6DA47EF883D8'), byref(volume)))
                if volume:
                    owned_volumes.append(volume)
                    yield Volume(volume)
            finally:
                if volume and volume not in owned_volumes:
                    release(volume)
                release(ctrl2)
                release(ctrl)

    iterator = None
    try:
        checked(ole32.CoCreateInstance(
            guid('BCDE0395-E52F-467C-8E3D-C4579291692E'), None, 23,
            guid('A95664D2-9614-4F35-A746-DE8DB63617E6'), byref(enum)))
        checked(vf(enum, 4, HRESULT, c_void_p, ctypes.c_uint, ctypes.c_uint, POINTER(c_void_p))(
            enum, 0, 1, byref(device)))
        checked(vf(device, 3, HRESULT, c_void_p, c_void_p, ctypes.c_ulong, c_void_p,
                   POINTER(c_void_p))(device, guid('77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F'),
                                     23, None, byref(manager)))
        checked(vf(manager, 5, HRESULT, c_void_p, POINTER(c_void_p))(
            manager, byref(sessions)))
        iterator = matching()
        yield iterator
    finally:
        if iterator is not None:
            iterator.close()
        for volume in owned_volumes:
            release(volume)
        release(sessions)
        release(manager)
        release(device)
        release(enum)
        ole32.CoUninitialize()


def _spotify_mute_toggle():
    """Keep the existing first-session mute toggle and boolean return value."""
    try:
        with _audio_sessions(next(iter(_NAMED_APPS.values()))[0]) as sessions:
            volume = next(sessions, None)
            return volume.toggle_mute() if volume is not None else False
    except Exception:
        logger.debug('[MEDIA] mute unavailable', exc_info=True)
        return False


def _volume_step(current, direction):
    """Ten percentage points, clamped to the player's 0–100 range."""
    return max(0, min(100, current + direction * 10))


def _named_transport_handler(row, action):
    def handler(app, remainder):
        result = _media_transport(action, row[1])
        message = result.detail.get('reason') if result.state is DispatchState.FAILED else (
            f'{row[2]} ' + ('paused' if action == 'pause' else 'playing'))
        _announce(app, message)
        return _music_result(result.state, row[2], message, **{
            key: value for key, value in result.detail.items() if key != 'message'})
    handler.__doc__ = f'{"Pauses" if action == "pause" else "Resumes"} {row[2]}, whatever else is playing.'
    return handler


def _named_volume_handler(row, direction):
    def handler(app, remainder):
        try:
            with _audio_sessions(row[0]) as sessions:
                volumes = list(sessions)
                if not volumes:
                    message = f"{row[2]} isn't playing anything"
                    state = DispatchState.FAILED
                else:
                    current = volumes[0].get_volume()
                    target = _volume_step(current, direction)
                    for volume in volumes:
                        volume.set_volume(target)
                    message = f'{row[2]} volume {target}%' + (' (already)' if target == current else '')
                    state = DispatchState.COMPLETED
        except Exception:
            logger.debug('[MEDIA] named volume unavailable', exc_info=True)
            message = f'{row[2]} volume unavailable'
            state = DispatchState.FAILED
        _announce(app, message)
        return _music_result(state, row[2], message)
    handler.__doc__ = f'{"Raises" if direction > 0 else "Lowers"} {row[2]}\'s own volume by ten points.'
    return handler


def _make_pause_handler(row):
    return _named_transport_handler(row, 'pause')


def _make_play_handler(row):
    return _named_transport_handler(row, 'play')


def _make_volume_up_handler(row):
    return _named_volume_handler(row, 1)


def _make_volume_down_handler(row):
    return _named_volume_handler(row, -1)


def _register_named_commands(rows=None):
    """Expand capability vocabulary for each app through the normal decorator."""
    factories = {'pause': _make_pause_handler, 'play': _make_play_handler,
                 'volume_up': _make_volume_up_handler, 'volume_down': _make_volume_down_handler}
    for name, row in (rows if rows is not None else _NAMED_APPS).items():
        for action, (capability, phrase, aliases) in _PLAYER_PHRASES.items():
            if capability not in row[4]:
                continue
            handler = factories[action](row)
            handler.__name__ = f'handle_named_{action}'
            command(phrase.format(app=name), aliases=[alias.format(app=name) for alias in aliases],
                    pack=row[3], risk_class='safe', ai_composable=True, side_effects=['audio'])(handler)


_register_named_commands()


def _open_track(uri_or_url):
    """Dispatch a URI without passing user-controlled text through a shell."""
    if uri_or_url.startswith('spotify:'):
        os.startfile(uri_or_url)
    else:
        webbrowser.open(uri_or_url)


def _parse_music_request(remainder):
    """Keep name spelling/punctuation; remove only whole-word grammar wrappers."""
    text = remainder.strip()
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"^(?:something from|stuff from|some|my)\s+", "", text,
                      flags=re.IGNORECASE)
    kind_match = re.search(r"^(playlist|album|artist)\s+|\s+(playlist|album|artist)$",
                           text, re.IGNORECASE)
    kind = next((part.casefold() for part in kind_match.groups() if part), None) if kind_match else None
    text = re.sub(r"^(?:playlist|album|artist)\s+|\s+(?:playlist|album|artist)$", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"^my\s+", "", text, flags=re.IGNORECASE).strip()
    return text, kind


_SPOTIFY_TYPES = frozenset({"playlist", "album", "artist", "track"})
_SPOTIFY_URI_RE = re.compile(r"spotify:(playlist|album|artist|track):([A-Za-z0-9]+)$", re.IGNORECASE)


def normalize_spotify_link(link):
    """Return canonical Spotify metadata for a pasted URI or open.spotify URL."""
    text = str(link or "").strip()
    match = _SPOTIFY_URI_RE.fullmatch(text)
    if match:
        kind, item_id = match.groups()
        return {"uri": f"spotify:{kind.casefold()}:{item_id}", "type": kind.casefold()}

    match = re.fullmatch(
        r"https?://(?:www\.)?open\.spotify\.com/(?:intl-[^/]+/)?"
        r"(playlist|album|artist|track)/([A-Za-z0-9]+)(?:[/?#].*)?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    kind, item_id = match.groups()
    return {"uri": f"spotify:{kind.casefold()}:{item_id}", "type": kind.casefold()}


def migrate_music_library(config):
    """Upgrade legacy ``name: spotify:uri`` values to typed library rows."""
    raw = config.get("music_library", {}) if isinstance(config, dict) else {}
    if not isinstance(raw, dict):
        return {}
    upgraded = {}
    changed = False
    for name, value in raw.items():
        if not isinstance(name, str) or not name.strip():
            changed = True
            continue
        link = value.get("uri") if isinstance(value, dict) else value
        normalized = normalize_spotify_link(link)
        if normalized is None:
            changed = True
            continue
        upgraded[name] = normalized
        changed = changed or value != normalized
    if changed:
        config["music_library"] = upgraded
        logger.info("[MUSIC] migrated legacy music_library entries to typed Spotify rows")
    return upgraded


def _normalised_words(text):
    """The command registry's punctuation-insensitive matching view."""
    return tuple(token for token, _start, _end in view_tokens(text or ""))


def _edit_distance(left, right):
    """Small dependency-free Levenshtein distance for Whisper near-misses."""
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index]
        for other_index, other in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[other_index] + 1,
                               previous[other_index - 1] + (char != other)))
        previous = current
    return previous[-1]


def _library_entries(app):
    config = getattr(app, "config", {}) or {}
    library = migrate_music_library(config)
    entries = [{"name": name, **entry, "user": True} for name, entry in library.items()]
    user_names = {_normalised_words(entry["name"]) for entry in entries}
    for name, uri in SONGS.items():
        if _normalised_words(name) not in user_names:
            entries.append({"name": name, "uri": uri, "type": "track", "user": False})
    return entries


def resolve_music_name(app, requested, preferred_type=None):
    """Resolve a spoken name as exact, unique prefix, or small edit distance."""
    words = _normalised_words(requested)
    if not words:
        return "none", []
    candidates = _library_entries(app)
    if preferred_type in _SPOTIFY_TYPES:
        candidates = [entry for entry in candidates if entry["type"] == preferred_type]

    exact = [entry for entry in candidates if _normalised_words(entry["name"]) == words]
    if len(exact) == 1:
        return "match", exact[0]
    if len(exact) > 1:
        return "ambiguous", exact

    prefixes = [entry for entry in candidates
                if _normalised_words(entry["name"])[:len(words)] == words]
    if len(prefixes) == 1:
        return "match", prefixes[0]
    if len(prefixes) > 1:
        return "ambiguous", prefixes

    spoken = "".join(words)
    limit = 1 if len(spoken) < 6 else 2
    nearby = [entry for entry in candidates
              if _edit_distance(spoken, "".join(_normalised_words(entry["name"]))) <= limit]
    if len(nearby) == 1:
        return "match", nearby[0]
    if len(nearby) > 1:
        return "ambiguous", nearby
    return "none", []


def _announce(app, message):
    """Give the user the same plain outcome in the chip and by speech."""
    show = getattr(app, "_show_outcome_chip", None)
    if callable(show):
        try:
            show(message, "warning", 3500)
        except Exception:
            logger.debug("[MUSIC] outcome chip unavailable", exc_info=True)
    coordinator = getattr(app, "audio_coordinator", None)
    try:
        if coordinator is not None:
            coordinator.speak(message, category="agent_response", interruptible=False)
        elif getattr(app, "tts_engine", None) is not None:
            app.tts_engine.speak(message)
    except Exception:
        logger.debug("[MUSIC] spoken outcome unavailable", exc_info=True)


def _is_spotify(session):
    if session is None:
        return False
    source = session.source_app_user_model_id.lower()
    return (source in {"spotify", "spotify.exe"}
            or source.startswith("spotifyab.spotifymusic_"))


async def _session_manager():
    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as SessionManager,
    )
    return await SessionManager.request_async()


def _is_playing(session):
    # Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus.Playing
    return session is not None and int(session.get_playback_info().playback_status) == 4


def _music_result(state, requested, message, **detail):
    return DispatchResult(state, message, "play", {
        "requested": requested, "message": message, **detail,
    })


async def _play_spotify(requested, uri):
    """Bound startup and observation; never send transport to another app."""
    mgr = await _session_manager()
    session = next((s for s in mgr.get_sessions() if _is_spotify(s)), None)
    if session is None:
        _open_track('spotify:')
        deadline = time.monotonic() + 8.0
        while session is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            session = next((s for s in mgr.get_sessions() if _is_spotify(s)), None)
        if session is None:
            return _music_result(DispatchState.FAILED, requested,
                                 "Spotify session unavailable after 8 s")
    _open_track(uri)
    if not await session.try_play_async():
        return _music_result(DispatchState.FAILED, requested, "Spotify refused play")
    deadline = time.monotonic() + 3.0
    other = None
    while True:
        current = mgr.get_current_session()
        if _is_playing(current):
            if _is_spotify(current):
                detail = {"source_app_id": current.source_app_user_model_id,
                          "actual_started": None, "uri": uri,
                          "request_verified": False}
                try:
                    props = await current.try_get_media_properties_async()
                    detail.update(actual_started=props.title or None,
                                  album=props.album_title or None,
                                  artist=props.artist or None)
                except Exception as exc:
                    detail["metadata_error"] = str(exc)
                actual = detail["actual_started"] or "Spotify (title unavailable)"
                return _music_result(DispatchState.COMPLETED, requested,
                                     f"Playing {actual}", **detail)
            other = current.source_app_user_model_id
        if time.monotonic() >= deadline:
            message = (f"played in {other}, not Spotify" if other else
                       "Spotify playback could not be verified")
            return _music_result(DispatchState.FAILED, requested, message,
                                 source_app_id=other, uri=uri)
        await asyncio.sleep(0.1)


# Pre-configured songs — Spotify URIs open directly in the app
SONGS = {
    'sad music': 'spotify:track:3JOVTQ5h8HGFnDdp4VT3MP',             # Mad World - Gary Jules
    'funeral march': 'spotify:track:6MFGNwMgSV3Yp1QNftOGzZ',         # Chopin - Funeral March
    'moonlight': 'spotify:track:3BMFGMiLMbUGiMQufRMUHJ',             # Beethoven - Moonlight Sonata
    'mad world': 'spotify:track:3JOVTQ5h8HGFnDdp4VT3MP',             # Gary Jules - Mad World
    'hurt': 'spotify:track:28cnXtME493VX9NOw9cIUh',                   # Johnny Cash - Hurt
    'everybody hurts': 'spotify:track:6PypGyiu0Y2lCDBN1XZEnP',        # R.E.M.
    'gymnopedie': 'spotify:track:5NGtFXVpXSvwunEIGeViY3',             # Satie - Gymnopedie No. 1
    'requiem': 'spotify:track:4SFBGAO0qlmz2FUMkGLPas',               # Mozart - Lacrimosa
    'adagio': 'spotify:track:5PjdY0CKGZdEuoNab3yDmX',                # Barber - Adagio for Strings
    'sound of silence': 'spotify:track:3YfS47QufnLMHssiKKOl3e',       # Disturbed version
}


def _set_volume(level):
    """Set system volume using Windows nircmd or PowerShell."""
    try:
        # PowerShell method — works without extra installs
        # Level is 0-100
        ps_cmd = (
            f'$wshell = New-Object -ComObject WScript.Shell; '
            f'1..50 | ForEach-Object {{ $wshell.SendKeys([char]174) }}; '  # vol down to 0
            f'1..{level // 2} | ForEach-Object {{ $wshell.SendKeys([char]175) }}'  # vol up to target
        )
        subprocess.Popen(
            ['powershell', '-Command', ps_cmd],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return True
    except Exception as e:
        print(f"[MUSIC] Volume control failed: {e}")
        return False


@command("play music", aliases=[
    "play some", "play me some", "play me",
    "play some music", "play me some music",
    "playing some", "playing me some",
    "put on some", "put on",
    "place of music", "place some music", "play of music",
    "plays some music", "place music", "plays music"
], pack="media", risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_play(app, remainder):
    """Plays what you name on Spotify, such as play music by Nick Cave."""
    try:
        delattr(app, "_music_pending_search")
    except AttributeError:
        pass
    if not remainder or not remainder.strip():
        return _media_transport("play")
    requested, preferred_type = _parse_music_request(remainder)
    if not requested or requested.lower() in {'music', 'something'}:
        uri = 'spotify:collection:tracks'
        requested = 'Liked Songs'
    else:
        resolution, entry = resolve_music_name(app, requested, preferred_type)
        if resolution == "ambiguous":
            choices = " or ".join(item["name"] for item in entry[:2])
            message = f"Which one: {choices}?"
            _announce(app, message)
            return _music_result(DispatchState.FAILED, requested, message,
                                 candidates=[item["name"] for item in entry])
        if resolution == "none":
            message = f"No '{requested}' in your music library. Say search to search Spotify."
            try:
                from samsara.execution_policy import capture_generation
                setattr(app, "_music_pending_search", {
                    "query": requested,
                    "created": time.monotonic(),
                    "generation": capture_generation(app),
                })
            except Exception:
                pass
            _announce(app, message)
            return _music_result(DispatchState.FAILED, requested, message,
                                 search_available=True)
        uri = entry["uri"]
    try:
        # This handler runs on the command worker, never a timer that outlives it.
        return asyncio.run(asyncio.wait_for(_play_spotify(requested, uri), timeout=15.0))
    except Exception as exc:
        return _music_result(DispatchState.FAILED, requested,
                             f"Spotify playback failed: {type(exc).__name__}: {exc}")


@command("search music", aliases=["search"], pack="media", risk_class='safe', ai_composable=True,
         side_effects=['audio'])
def handle_music_search(app, remainder):
    """Searches Spotify after music was not found in your library and a search was offered."""  # PLACEHOLDER COPY (531)
    pending = getattr(app, "_music_pending_search", None)
    if not isinstance(pending, dict):
        return False
    try:
        delattr(app, "_music_pending_search")
    except AttributeError:
        pass
    try:
        from samsara.execution_policy import current_generation
        requested = pending["query"]
        age = time.monotonic() - pending["created"]
        if age < 0 or age >= 30 or pending["generation"] != current_generation(app):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    uri = 'spotify:search:' + quote(requested, safe='')
    try:
        _open_track(uri)
    except Exception:
        message = "Spotify search failed."
        _announce(app, message)
        return _music_result(DispatchState.FAILED, requested, message)
    message = "Spotify search opened."
    _announce(app, message)
    return _music_result(DispatchState.COMPLETED, requested, message,
                         outcome="opened_search", request_verified=False,
                         uri=uri, query=requested)


# ── Earbud-style transport commands (SMTC, app-agnostic) ──────────────────

@command("play", aliases=["resume", "unpause music", "resume music"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'],
         param_schema={"name": {"type": "str", "required": False, "max_len": 120}})
def handle_media_play(app, remainder):
    """Resumes whatever was playing."""
    if remainder and remainder.strip():
        return handle_play(app, remainder)
    return _media_transport("play")


@command("play playlist", aliases=["playlist"], pack="media", risk_class='safe', ai_composable=True,
         side_effects=['audio'],
         param_schema={"remainder": {"type": "str", "required": True, "max_len": 120}})
def handle_playlist(app, remainder):
    """Plays a named Spotify playlist from your Music library."""  # PLACEHOLDER COPY (531)
    return handle_play(app, f"playlist {remainder}")


@command("play album", aliases=["album"], pack="media", risk_class='safe', ai_composable=True,
         side_effects=['audio'],
         param_schema={"remainder": {"type": "str", "required": True, "max_len": 120}})
def handle_album(app, remainder):
    """Plays a named Spotify album from your Music library."""  # PLACEHOLDER COPY (531)
    return handle_play(app, f"album {remainder}")


@command("play artist", aliases=["artist"], pack="media", risk_class='safe', ai_composable=True,
         side_effects=['audio'],
         param_schema={"remainder": {"type": "str", "required": True, "max_len": 120}})
def handle_artist(app, remainder):
    """Plays a named Spotify artist from your Music library."""  # PLACEHOLDER COPY (531)
    return handle_play(app, f"artist {remainder}")


@command("import my Spotify playlists", aliases=["import Spotify playlists"],
         pack="media", risk_class="safe", ai_composable=False, side_effects=["ui"])
def handle_import_spotify_playlists(app, remainder):
    """Opens the picker for importing playlists from your downloaded Spotify data."""  # PLACEHOLDER COPY (531)
    from samsara.ui.settings.music_qt import open_spotify_import_picker

    if not open_spotify_import_picker(app):
        message = "The Spotify playlist import picker is unavailable."
        _announce(app, message)
        return DispatchResult(
            DispatchState.FAILED, phrase="import my Spotify playlists",
            detail={"message": message},
        )

    message = "Choose your Spotify data export."
    _announce(app, message)
    return DispatchResult(
        DispatchState.COMPLETED, phrase="import my Spotify playlists",
        detail={"message": message},
    )


@command("pause", aliases=["pause music", "stop music", "play pause"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_pause(app, remainder):
    """Pauses whatever is playing."""
    return _media_transport("pause")


@command("next", aliases=["skip", "skip track", "next track", "next song", "skip song"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_next(app, remainder):
    """Skips to the next track."""
    return _media_transport("next")


@command("previous music", aliases=["previous", "previous track", "previous song", "back a song"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_previous(app, remainder):
    """Skips back in the current media session."""
    return _media_transport("previous")


@command("mute", aliases=["mute spotify"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_spotify_mute(app, remainder):
    """Mutes or unmutes Spotify without touching the system volume."""
    return _spotify_mute_toggle()

