"""VLC 3.x background commands, using VLC's native action receiver.

Exact small seeks and subtitles-off additionally use VLC's optional Web
interface. We never enable it, change its settings or log its credentials.
"""
import base64
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from samsara.command_registry import DispatchResult, DispatchState
from samsara.plugin_commands import command
from samsara.media_players import PLAYERS
from samsara import media_player_control as control

_SCOPE = {'apps': ['vlc.exe']}
_NAMES = PLAYERS['vlc'].names
_ACTIONS = {
    'faster': ('faster', ('faster playback',)),
    'slower': ('slower', ('slower playback',)),
    'normal speed': ('rate-normal', ('normal playback speed',)),
    'fullscreen': ('fullscreen', ('player fullscreen',)),
    'next subtitle track': ('subtitle-track', ('next subtitles',)),
    'previous subtitle track': ('subtitle-revtrack', ('previous subtitles',)),
    'next audio track': ('audio-track', ()),
    'louder inside player': ('vol-up', ('player louder',)),
    'quieter inside player': ('vol-down', ('player quieter',)),
    'toggle mute': ('vol-mute', ('player toggle mute',)),
}

# PLACEHOLDER COPY (1153): catalogue sentences, separate from result feedback.
_DESCRIPTIONS = {
    'faster': 'Increases playback speed in VLC.',
    'slower': 'Decreases playback speed in VLC.',
    'rate-normal': 'Restores normal playback speed in VLC.',
    'fullscreen': 'Switches VLC between fullscreen and windowed playback.',
    'subtitle-track': 'Selects the next subtitle track in VLC.',
    'subtitle-revtrack': 'Selects the previous subtitle track in VLC.',
    'audio-track': 'Selects the next audio track in VLC.',
    'vol-up': "Raises VLC's internal playback volume.",
    'vol-down': "Lowers VLC's internal playback volume.",
    'vol-mute': "Toggles VLC's internal playback sound on or off.",
    'subtitles-off': 'Hides subtitles in VLC using its enabled Web interface.',
    'seek-forward': 'Skips forward by the requested number of seconds in VLC.',
    'seek-back': 'Skips back by the requested number of seconds in VLC.',
}


def _settings():
    """VLC's own config, read only on an advanced command; never Samsara's home."""
    root = os.environ.get('APPDATA')
    if not root:
        return {}
    try:
        path = Path(root) / 'vlc' / 'vlcrc'
        if path.stat().st_size > 1024 * 1024:
            return {}
        keys = {'extraintf', 'http-password', 'http-port',
                'extrashort-jump-size', 'short-jump-size', 'medium-jump-size', 'long-jump-size'}
        return {key: value.strip() for line in path.read_text(encoding='utf-8-sig').splitlines()
                if '=' in line and not line.lstrip().startswith('#')
                for key, value in [line.split('=', 1)] if key in keys}
    except (OSError, UnicodeError):
        return {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError('VLC redirects are not allowed')


def _http(settings, action, value):
    """Only loopback, no environment proxy, redirects, metadata logs or retries."""
    if 'http' not in settings.get('extraintf', '') or not settings.get('http-password'):
        raise RuntimeError('Enable the Web interface in VLC for this command')
    try:
        port = int(settings.get('http-port', '8080'))
        if not 1 <= port <= 65535:
            raise ValueError
        auth = base64.b64encode((':' + settings['http-password']).encode()).decode()
        query = urlencode({'command': action, 'val': str(value)})
        request = Request(f'http://127.0.0.1:{port}/requests/status.json?{query}',
                          headers={'Authorization': 'Basic ' + auth})
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=1.0) as response:
            data = json.loads(response.read(1024 * 1024))
        if not isinstance(data, dict) or 'state' not in data:
            raise ValueError
    except Exception:
        # Exceptions can carry request headers or URLs; never forward/log them.
        raise RuntimeError('VLC Web control unavailable') from None


def parse_seconds(text):
    # Reuse Stremio's number grammar, but not its 10-second rounding or app-name
    # stripping: VLC must consume the entire supplied duration.
    from tools.stremio_control import _spoken_number
    words = ' '.join(text.lower().strip().rstrip('.!?').split())
    if not words:
        return 10
    match = re.fullmatch(r'(?:by )?(.+?)(?: (seconds?|secs?|minutes?|mins?))?', words)
    if not match:
        raise ValueError('Say a number of seconds or minutes')
    amount, unit = match.groups()
    value = _spoken_number(amount) * (60 if unit and unit.startswith('min') else 1)
    if not math.isfinite(value) or value <= 0 or value > 1800 or value != int(value):
        raise ValueError('Say 1 to 1800 whole seconds')
    return int(value)


def _seek_actions(seconds, settings, forward):
    """Use the configured jump sizes, exactly; never guess an arrow duration."""
    options = []
    for size, default in (('extrashort', 3), ('short', 10), ('medium', 60), ('long', 300)):
        try:
            step = int(settings.get(size + '-jump-size', default))
        except (TypeError, ValueError):
            continue
        if 0 < step <= seconds:
            options.append((step, f'jump+{size}' if forward else f'jump-{size}'))
    # Bounded coin change handles custom steps without overshooting/rounding.
    plans = [()] + [None] * seconds
    for amount in range(1, seconds + 1):
        for step, action in options:
            if step <= amount and plans[amount - step] is not None:
                plan = (*plans[amount - step], action)
                if len(plan) <= 180 and (plans[amount] is None or len(plan) < len(plans[amount])):
                    plans[amount] = plan
    if plans[seconds] is None:
        raise RuntimeError('Enable the Web interface in VLC for this exact skip amount')
    return plans[seconds]


def _run(action, amount=None):
    hwnd = control.unique_window(('vlc.exe',), hidden_class='Static', title_prefix='VLC ghk ')
    if action == 'subtitles-off':
        _http(_settings(), 'subtitle_track', -1)
    elif action in ('seek-forward', 'seek-back'):
        settings = _settings()
        forward = action == 'seek-forward'
        if 'http' in settings.get('extraintf', '') and settings.get('http-password'):
            # Once a request is sent, an uncertain result must not double-seek.
            _http(settings, 'seek', ('+' if forward else '-') + str(amount))
        else:
            for key in _seek_actions(amount, settings, forward):
                control.vlc_hotkey(hwnd, key)
    else:
        control.vlc_hotkey(hwnd, action)


def _handler(action, description, seek=False):
    def invoke(app, text=''):
        try:
            amount = parse_seconds(text) if seek else None
            _run(action, amount)
            state = DispatchState.COMPLETED
            message = f'VLC {description} requested' + (f': {amount} seconds' if seek else '')
        except (ValueError, RuntimeError) as exc:
            state, message = DispatchState.FAILED, str(exc)
        except Exception:
            state, message = DispatchState.FAILED, 'VLC control unavailable'
        from plugins.commands.music import _announce
        _announce(app, message, state=state)
        return DispatchResult(state, detail={'message': message, **(
            {'reason': message} if state is DispatchState.FAILED else {})})
    # 1153 n5: fixed controls do not consume a duration. Keep that distinction
    # visible to catalogue source inspection as well as runtime registration.
    if seek:
        def handler(app, remainder):
            return invoke(app, remainder)
    else:
        def handler(app, remainder):
            return invoke(app)
    handler.__doc__ = _DESCRIPTIONS[action]
    return handler


def _register():
    for phrase, (action, scoped_aliases) in _ACTIONS.items():
        named = [f'{name} {phrase}' for name in _NAMES]
        handler = _handler(action, phrase)
        handler.contextual_aliases = {alias: _SCOPE for alias in scoped_aliases}
        command(named[0], aliases=named[1:], pack='vlc', whole_utterance=True,
                risk_class='safe', ai_composable=True, side_effects=['audio'])(
                    handler)
    command('vlc subtitles off', aliases=['vlc turn subtitles off', 'v l c subtitles off'],
            pack='vlc', whole_utterance=True)(_handler('subtitles-off', 'subtitles off (requires VLC Web interface)'))
    for direction, aliases in (('forward', ('skip ahead',)), ('back', ('skip backward', 'rewind'))):
        prefixes = [f'{name} {verb}' for name in _NAMES
                    for verb in (f'skip {direction}', *aliases)]
        handler = _handler('seek-' + direction, 'skip ' + direction, seek=True)

        def match(text, prefixes=tuple(prefixes)):
            words = ' '.join(text.lower().strip().rstrip('.!?').split())
            for prefix in prefixes:
                if words.startswith(prefix + ' '):
                    remainder = words[len(prefix) + 1:]
                    try:
                        parse_seconds(remainder)
                    except ValueError:
                        return None
                    return remainder
            return None

        handler.match_utterance = match
        command(prefixes[0], aliases=prefixes[1:], pack='vlc', whole_utterance=True,
                param_schema={'remainder': {'type': 'str', 'required': False}},
                risk_class='safe', ai_composable=True, side_effects=['audio'])(handler)


_register()
