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

from samsara.plugin_commands import command


def _open_track(uri_or_url):
    """Open a Spotify track via the desktop app."""
    if uri_or_url.startswith('spotify:track:'):
        # Use start command which routes through Windows protocol handler
        # This works whether Spotify is open or closed
        track_id = uri_or_url.split(':')[-1]
        subprocess.Popen(
            ['cmd', '/c', 'start', '', f'spotify:track:{track_id}'],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    elif uri_or_url.startswith('spotify:'):
        subprocess.Popen(
            ['cmd', '/c', 'start', '', uri_or_url],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
    else:
        webbrowser.open(uri_or_url)

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


@command("play", aliases=[
    "play some", "play me some", "play me",
    "play some music", "play me some music",
    "playing some", "playing me some", "playing",
    "put on some", "put on",
    "place of music", "place some music", "play of music",
    "plays some music", "place music", "plays music"
])
def handle_play(app, remainder):
    """Play music. Usage: 'Jarvis, play some sad music'"""
    if not remainder or remainder.strip().lower() in ('music', 'some music', 'something'):
        # No specific request — pick the "default"
        print("[MUSIC] Playing: ambient selection")
        _open_track(SONGS['mad world'])
        target_vol = int(app.config.get('music_volume', 30))
        threading.Timer(2.0, _set_volume, args=[target_vol]).start()
        return True

    query = remainder.strip().lower()

    # Check configured songs first
    for name, url in SONGS.items():
        if name in query:
            print(f"[MUSIC] Playing: {name}")
            _open_track(url)
            # Set modest volume after a short delay (let browser open)
            target_vol = int(app.config.get('music_volume', 30))
            threading.Timer(2.0, _set_volume, args=[target_vol]).start()
            return True

    # Check user-configured songs
    user_songs = app.config.get('music_library', {})
    for name, url in user_songs.items():
        if name.lower() in query:
            print(f"[MUSIC] Playing: {name}")
            _open_track(url)
            target_vol = int(app.config.get('music_volume', 30))
            threading.Timer(2.0, _set_volume, args=[target_vol]).start()
            return True

    # Fallback: search Spotify
    import urllib.parse
    search_url = f"https://open.spotify.com/search/{urllib.parse.quote(query)}"
    print(f"[MUSIC] Searching Spotify for: {query}")
    webbrowser.open(search_url)
    return True


@command("volume down", aliases=[
    "lower the volume", "turn it down", "quieter"
])
def handle_vol_down(app, remainder):
    """Lower system volume."""
    _set_volume(15)
    print("[MUSIC] Volume lowered")
    return True


@command("volume up", aliases=[
    "raise the volume", "turn it up", "louder"
])
def handle_vol_up(app, remainder):
    """Raise system volume."""
    _set_volume(60)
    print("[MUSIC] Volume raised")
    return True


@command("mute", aliases=["unmute", "toggle mute"])
def handle_mute(app, remainder):
    """Toggle system mute."""
    try:
        subprocess.Popen(
            ['powershell', '-Command',
             '$wshell = New-Object -ComObject WScript.Shell; '
             '$wshell.SendKeys([char]173)'],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        print("[MUSIC] Mute toggled")
    except Exception as e:
        print(f"[MUSIC] {e}")
    return True
