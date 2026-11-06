"""Declarative media-player vocabulary. No plugin imports or player I/O."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Player:
    exes: tuple[str, ...]
    sources: tuple[str, ...]
    display: str
    pack: str
    capabilities: tuple[str, ...] = ('transport', 'volume', 'launch')
    names: tuple[str, ...] = ()
    driver: str = 'smtc'
    install_paths: tuple[str, ...] = ()
    package: str = ''
    aumid: str = ''


PLAYERS = {
    'spotify': Player(('spotify.exe',), ('spotify',), 'Spotify', 'spotify',
                      names=('spotify', 'spot ify'), install_paths=('Spotify/Spotify.exe',)),
    'vlc': Player(('vlc.exe',), ('vlc',), 'VLC', 'vlc',
                  names=('vlc', 'v l c', 'vee el see'), driver='vlc',
                  install_paths=('VideoLAN/VLC/vlc.exe',)),
    'itunes': Player(('itunes.exe',), ('itunes',), 'iTunes', 'itunes',
                     names=('itunes', 'i tunes', 'eye tunes'), driver='itunes',
                     install_paths=('iTunes/iTunes.exe',),
                     package='AppleInc.iTunes', aumid='AppleInc.iTunes_nzyj5cx40ttqa!iTunes'),
    'apple music': Player(('applemusic.exe',), ('AppleInc.AppleMusic', 'AppleMusic.exe'),
                          'Apple Music', 'apple-music', names=('apple music',),
                          package='AppleInc.AppleMusic',
                          aumid='AppleInc.AppleMusic_nzyj5cx40ttqa!App'),
    'media player': Player(('microsoft.media.player.exe', 'music.ui.exe'),
                           ('Microsoft.ZuneMusic', 'Microsoft.Media.Player'),
                           'Media Player', 'windows-media-player',
                           names=('media player', 'new media player'),
                           package='Microsoft.ZuneMusic',
                           aumid='Microsoft.ZuneMusic_8wekyb3d8bbwe!Microsoft.ZuneMusic'),
    'windows media player': Player(('wmplayer.exe',), (), 'Windows Media Player (legacy)',
                                   'windows-media-player', ('volume', 'launch'),
                                   names=('windows media player', 'legacy media player'),
                                   driver='none', install_paths=('Windows Media Player/wmplayer.exe',)),
    'foobar2000': Player(('foobar2000.exe',), ('foobar2000',), 'foobar2000', 'foobar2000',
                         names=('foobar2000', 'foobar', 'foo bar', 'foo bar two thousand'),
                         install_paths=('foobar2000/foobar2000.exe',)),
    'musicbee': Player(('musicbee.exe',), (), 'MusicBee', 'musicbee', ('volume', 'launch'),
                       names=('musicbee', 'music bee'), driver='none',
                       install_paths=('MusicBee/MusicBee.exe',)),
    'aimp': Player(('aimp.exe',), ('aimp',), 'AIMP', 'aimp',
                   names=('aimp', 'a imp', 'a i m p'), driver='aimp',
                   install_paths=('AIMP/AIMP.exe',)),
}

# Capability, canonical phrase, aliases. One row adds a player; no handler code.
PLAYER_PHRASES = {
    'pause': ('transport', 'pause {app}', ('stop {app}',)),
    'play': ('transport', 'play {app}', ('resume {app}',)),
    'toggle': ('transport', 'toggle {app}', ('play pause {app}', '{app} play pause')),
    'next': ('transport', 'next {app}', ('{app} next', '{app} next track')),
    'previous': ('transport', 'previous {app}', ('{app} previous', '{app} previous track')),
    'volume_up': ('volume', '{app} volume up', ('{app} louder', 'turn {app} up')),
    'volume_down': ('volume', '{app} volume down', ('{app} quieter', 'turn {app} down')),
    'mute': ('volume', 'silence {app}', ('mute {app}', '{app} mute')),
    'unmute': ('volume', 'unmute {app}', ('{app} unmute',)),
    'open': ('launch', 'open {app}', ('launch {app}', 'start {app}')),
    'close': ('launch', 'close {app}', ('quit {app}', 'exit {app}')),
}

# PLACEHOLDER COPY (1153): user-facing sentences for every shared verb.
PLAYER_DESCRIPTIONS = {
    'pause': 'Pauses playback in {app} without changing the focused app.',
    'play': 'Starts or resumes playback in {app} without changing the focused app.',
    'toggle': 'Switches {app} between playing and paused.',
    'next': 'Skips to the next track in {app}.',
    'previous': 'Returns to the previous track in {app}.',
    'volume_up': "Raises {app}'s own volume by ten points.",
    'volume_down': "Lowers {app}'s own volume by ten points.",
    'mute': "Mutes {app}'s sound without changing other apps.",
    'unmute': "Restores {app}'s sound without changing other apps.",
    'open': 'Launches {app} so you can play media.',
    'close': "Closes {app}'s player window.",
}


def player_row(name, row):
    """Normalize Player rows and the original factory/extension tuple formats."""
    if isinstance(row, Player):
        return row
    if len(row) == 3:
        exe, source, display = row
        return Player((exe,), (source,), display, '', names=(name,) if name else ())
    exe, source, display, pack, capabilities = row
    return Player((exe,), (source,), display, pack, tuple(capabilities), (name,))


def phrases_for(name, row, action):
    player = player_row(name, row)
    capability, phrase, aliases = PLAYER_PHRASES[action]
    if capability not in player.capabilities:
        return ()
    phrases = [phrase.format(app=name)]
    # Spotify already has a launch command. Leave open/launch/start Spotify to
    # that command's grammar instead of adding competing named-player aliases.
    templates = (phrase, *aliases)
    if name == 'spotify' and action == 'open':
        phrases = []
        templates = ('{app} open',)
    for spoken in player.names or (name,):
        phrases.extend(template.format(app=spoken) for template in templates)
    # The existing media-pack mute Spotify command toggles the first session.
    # Keep that phrase and behaviour; "silence Spotify" sets mute explicitly.
    preserved = {'mute': 'mute spotify', 'open': 'open spotify'}
    return tuple(dict.fromkeys(p for p in phrases if not (
        name == 'spotify' and p == preserved.get(action))))


def reference_examples(records, enabled_packs):
    """Select enabled player examples from the catalogue; never invent a phrase."""
    from samsara.command_catalog import canonical_phrase

    by_phrase = {canonical_phrase(record): record for record in records or []}
    examples = []
    for name, row in PLAYERS.items():
        action = 'pause' if 'transport' in row.capabilities else 'volume_up'
        record = by_phrase.get(phrases_for(name, row, action)[0])
        if record and record.get('pack') in enabled_packs:
            examples.append((row.display, canonical_phrase(record)))
    for record in records or []:
        if (record.get('canonical_id') == 'vlc.vlc_skip_forward'
                and record.get('pack') in enabled_packs and record.get('examples')):
            examples.append(('VLC skip', record['examples'][0]))
    return examples
