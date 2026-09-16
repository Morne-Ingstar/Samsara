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

from samsara.plugin_commands import command

from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)


def _media_transport(action):
    """Send a transport command to the current SMTC session.

    action: "play" | "pause" | "next"
    Acts on whatever session currently holds SMTC foreground — identical
    behavior to a hardware media key / Bluetooth earbud button. Not
    Spotify-bound by design; works with browsers, VLC, Spotify, etc.
    Returns True if the command was sent, False if no session or on error.
    """
    try:
        from winsdk.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as SessionManager,
        )

        async def _transport():
            try:
                mgr = await SessionManager.request_async()
                session = mgr.get_current_session()
                if not session:
                    print("[MEDIA] no active media session")
                    return False
                if action == "play":
                    await session.try_play_async()
                elif action == "pause":
                    await session.try_pause_async()
                elif action == "next":
                    await session.try_skip_next_async()
                print(f"[MEDIA] transport: {action}")
                return True
            except Exception as e:
                print(f"[MEDIA] transport {action} failed: {e}")
                return False

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_transport())
        finally:
            loop.close()
    except Exception as e:
        print(f"[MEDIA] transport init failed: {e}")
        return False


def _spotify_mute_toggle():
    """Toggle Spotify's own audio session mute via Core Audio IAudioSessionManager2.

    Enumerates all audio sessions on the default render device, finds the one
    whose process is spotify.exe, and flips ISimpleAudioVolume.SetMute. System
    mute is never touched. Uses raw COM vtables (same style as volume.py).

    vtable indices (Windows SDK audiopolicy.h / mmdeviceapi.h):
      IMMDeviceEnumerator   : 0-2 IUnknown, 3 EnumAudioEndpoints,
                              4 GetDefaultAudioEndpoint
      IMMDevice             : 0-2 IUnknown, 3 Activate
      IAudioSessionManager2 : 0-2 IUnknown, 3 GetAudioSessionControl,
                              4 GetSimpleAudioVolume, 5 GetSessionEnumerator
      IAudioSessionEnumerator: 0-2 IUnknown, 3 GetCount, 4 GetSession
      IAudioSessionControl2 : 0-11 IAudioSessionControl, 12 GetSessionIdentifier,
                              13 GetSessionInstanceIdentifier, 14 GetProcessId
      ISimpleAudioVolume    : 0-2 IUnknown, 3 SetMasterVolume, 4 GetMasterVolume,
                              5 SetMute, 6 GetMute
    Returns True if mute was toggled, False if no Spotify session or on error.
    """
    import ctypes
    from ctypes import POINTER, HRESULT, byref, cast, c_void_p
    import struct

    try:
        import psutil
    except ImportError:
        print("[MEDIA] mute: psutil not available")
        return False

    def _guid(s):
        parts = s.strip('{}').split('-')
        return struct.pack('<IHH', int(parts[0], 16), int(parts[1], 16),
                           int(parts[2], 16)) + bytes.fromhex(parts[3] + parts[4])

    def _vf(ptr, idx, restype, *argtypes):
        """Raw COM vtable call (mirrors volume.py _get_vtable_func)."""
        vtbl = cast(ptr, POINTER(POINTER(c_void_p)))
        functype = ctypes.WINFUNCTYPE(restype, *argtypes)
        return functype(vtbl[0][idx])

    # GUIDs — verified against Windows SDK audiopolicy.h / mmdeviceapi.h
    _CLSID_MMDeviceEnumerator  = _guid('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
    _IID_IMMDeviceEnumerator   = _guid('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    _IID_IAudioSessionManager2 = _guid('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
    _IID_IAudioSessionControl2 = _guid('{bfb7ff88-7239-4fc9-8fa2-07c950be9c6d}')
    _IID_ISimpleAudioVolume    = _guid('{87CE5498-68D6-44E5-9215-6DA47EF883D8}')

    ole32 = ctypes.windll.ole32
    ole32.CoInitialize.argtypes = [c_void_p]
    ole32.CoInitialize.restype  = HRESULT
    ole32.CoCreateInstance.argtypes = [
        c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p)]
    ole32.CoCreateInstance.restype = HRESULT

    hr = ole32.CoInitialize(None)
    if hr < 0 and hr != 1:  # S_OK or S_FALSE (already initialised)
        print(f"[MEDIA] mute: CoInitialize failed {hr:#010x}")
        return False

    # IMMDeviceEnumerator
    enum = c_void_p()
    hr = ole32.CoCreateInstance(
        _CLSID_MMDeviceEnumerator, None, 23,
        _IID_IMMDeviceEnumerator, byref(enum))
    if hr != 0:
        print(f"[MEDIA] mute: CoCreateInstance failed {hr:#010x}")
        return False

    # GetDefaultAudioEndpoint(eRender=0, eMultimedia=1)  vtable index 4
    device = c_void_p()
    hr = _vf(enum, 4, HRESULT, c_void_p, ctypes.c_uint, ctypes.c_uint, POINTER(c_void_p))(
        enum, 0, 1, byref(device))
    _vf(enum, 2, ctypes.c_ulong, c_void_p)(enum)   # Release IMMDeviceEnumerator
    if hr != 0:
        print(f"[MEDIA] mute: GetDefaultAudioEndpoint failed {hr:#010x}")
        return False

    # IMMDevice::Activate(IAudioSessionManager2)  vtable index 3
    mgr = c_void_p()
    hr = _vf(device, 3, HRESULT,
             c_void_p, c_void_p, ctypes.c_ulong, c_void_p, POINTER(c_void_p))(
        device, _IID_IAudioSessionManager2, 23, None, byref(mgr))
    _vf(device, 2, ctypes.c_ulong, c_void_p)(device)   # Release IMMDevice
    if hr != 0:
        print(f"[MEDIA] mute: Activate(IAudioSessionManager2) failed {hr:#010x}")
        return False

    # IAudioSessionManager2::GetSessionEnumerator  vtable index 5
    sess_enum = c_void_p()
    hr = _vf(mgr, 5, HRESULT, c_void_p, POINTER(c_void_p))(mgr, byref(sess_enum))
    _vf(mgr, 2, ctypes.c_ulong, c_void_p)(mgr)         # Release IAudioSessionManager2
    if hr != 0:
        print(f"[MEDIA] mute: GetSessionEnumerator failed {hr:#010x}")
        return False

    # IAudioSessionEnumerator::GetCount  vtable index 3
    count = ctypes.c_int()
    _vf(sess_enum, 3, HRESULT, c_void_p, POINTER(ctypes.c_int))(sess_enum, byref(count))

    found = False
    for i in range(count.value):
        # GetSession  vtable index 4
        ctrl = c_void_p()
        hr = _vf(sess_enum, 4, HRESULT, c_void_p, ctypes.c_int, POINTER(c_void_p))(
            sess_enum, i, byref(ctrl))
        if hr != 0 or not ctrl:
            continue

        # QI IAudioSessionControl → IAudioSessionControl2  (QI = vtable index 0)
        ctrl2 = c_void_p()
        hr2 = _vf(ctrl, 0, HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
            ctrl, _IID_IAudioSessionControl2, byref(ctrl2))
        _vf(ctrl, 2, ctypes.c_ulong, c_void_p)(ctrl)   # Release IAudioSessionControl
        if hr2 != 0 or not ctrl2:
            continue

        # IAudioSessionControl2::GetProcessId  vtable index 14
        pid = ctypes.c_ulong()
        _vf(ctrl2, 14, HRESULT, c_void_p, POINTER(ctypes.c_ulong))(ctrl2, byref(pid))

        proc_name = ''
        try:
            if pid.value:
                proc_name = psutil.Process(pid.value).name().lower()
        except Exception as e:
            logger.debug(f"_spotify_mute_toggle: {e}")

        if proc_name != 'spotify.exe':
            _vf(ctrl2, 2, ctypes.c_ulong, c_void_p)(ctrl2)
            continue

        # Found Spotify — QI for ISimpleAudioVolume  (QI = vtable index 0)
        vol = c_void_p()
        hr3 = _vf(ctrl2, 0, HRESULT, c_void_p, c_void_p, POINTER(c_void_p))(
            ctrl2, _IID_ISimpleAudioVolume, byref(vol))
        _vf(ctrl2, 2, ctypes.c_ulong, c_void_p)(ctrl2)  # Release IAudioSessionControl2
        if hr3 != 0 or not vol:
            print(f"[MEDIA] mute: QI ISimpleAudioVolume failed {hr3:#010x}")
            break

        # ISimpleAudioVolume::GetMute index 6, SetMute index 5
        muted = ctypes.c_int()
        _vf(vol, 6, HRESULT, c_void_p, POINTER(ctypes.c_int))(vol, byref(muted))
        new_mute = 0 if muted.value else 1
        hr4 = _vf(vol, 5, HRESULT, c_void_p, ctypes.c_int, c_void_p)(vol, new_mute, None)
        _vf(vol, 2, ctypes.c_ulong, c_void_p)(vol)      # Release ISimpleAudioVolume

        if hr4 == 0:
            print(f"[MEDIA] Spotify {'muted' if new_mute else 'unmuted'}")
            found = True
        else:
            print(f"[MEDIA] mute: SetMute failed {hr4:#010x}")
        break

    _vf(sess_enum, 2, ctypes.c_ulong, c_void_p)(sess_enum)  # Release IAudioSessionEnumerator

    if not found:
        print("[MEDIA] mute: no Spotify audio session")
    return found


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
    playlist = bool(re.match(r"^playlist\s+", text, re.IGNORECASE)
                    or re.search(r"\s+playlist$", text, re.IGNORECASE))
    text = re.sub(r"^playlist\s+|\s+playlist$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^my\s+", "", text, flags=re.IGNORECASE).strip()
    return text, playlist


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
    requested, playlist = _parse_music_request(remainder)
    if not requested or requested.lower() in {'music', 'something'}:
        uri = 'spotify:collection:tracks'
        requested = 'Liked Songs'
    else:
        library = {**SONGS, **getattr(app, 'config', {}).get('music_library', {})}
        uri = next((value for name, value in library.items()
                    if name.casefold() == requested.casefold()
                    and isinstance(value, str) and value.startswith('spotify:')), None)
        if uri is None:
            uri = 'spotify:search:' + quote(requested, safe='')
    try:
        # This handler runs on the command worker, never a timer that outlives it.
        return asyncio.run(asyncio.wait_for(_play_spotify(requested, uri), timeout=15.0))
    except Exception as exc:
        return _music_result(DispatchState.FAILED, requested,
                             f"Spotify playback failed: {type(exc).__name__}: {exc}")


# ── Earbud-style transport commands (SMTC, app-agnostic) ──────────────────

@command("play", aliases=["resume"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_play(app, remainder):
    """Resumes whatever was playing."""
    if remainder and remainder.strip():
        return handle_play(app, remainder)
    return _media_transport("play")


@command("pause", aliases=["pause music"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_pause(app, remainder):
    """Pauses whatever is playing."""
    return _media_transport("pause")


@command("next", aliases=["skip", "next track", "next song", "skip track"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_media_next(app, remainder):
    """Skips to the next track."""
    return _media_transport("next")


@command("mute", aliases=["mute spotify"], pack="media",
         risk_class='safe', ai_composable=True, side_effects=['audio'])
def handle_spotify_mute(app, remainder):
    """Mutes or unmutes Spotify without touching the system volume."""
    return _spotify_mute_toggle()

