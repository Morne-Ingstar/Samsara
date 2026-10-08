"""Real music handlers, with no live transport, launch, or dictation import."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.commands import music
from samsara import plugin_commands
from samsara.command_registry import DispatchResult, DispatchState


@pytest.fixture
def playback(monkeypatch):
    now = [0.0]
    async def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(music, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(music.asyncio, 'sleep', sleep)
    launch = Mock()
    monkeypatch.setattr(music, '_open_track', launch)
    spotify = session('Spotify.exe', 'Alt Rock')
    manager = SimpleNamespace(get_sessions=Mock(return_value=[spotify]),
                              get_current_session=Mock(return_value=spotify))
    monkeypatch.setattr(music, '_session_manager', AsyncMock(return_value=manager))
    return manager, spotify, launch


def session(source, title, playing=True):
    return SimpleNamespace(
        source_app_user_model_id=source,
        get_playback_info=Mock(return_value=SimpleNamespace(playback_status=4 if playing else 5)),
        try_play_async=AsyncMock(return_value=True),
        try_get_media_properties_async=AsyncMock(return_value=SimpleNamespace(
            title=title, album_title='Album', artist='Artist')))


def _app(library=None):
    return SimpleNamespace(config={'music_library': library or {}})


@pytest.mark.parametrize('words,query,kind', [
    ('my alternative rock playlist', 'alternative rock', 'playlist'),
    ('alternative rock playlist', 'alternative rock', 'playlist'),
    ('playlist alternative rock', 'alternative rock', 'playlist'),
    ('album kind of blue', 'kind of blue', 'album'),
    ('artist nina simone', 'nina simone', 'artist'),
    ('some alternative rock', 'alternative rock', None),
    ('something from alternative rock', 'alternative rock', None),
    ('alternative rock', 'alternative rock', None),
    ('something from my alternative rock playlist', 'alternative rock', 'playlist'),
    ('stuff from my alternative rock playlist', 'alternative rock', 'playlist'),
    ("my Morne's Road Trip playlist", "Morne's Road Trip", 'playlist'),
    ('playlist my Alt Rock', 'Alt Rock', 'playlist'),
])
def test_grammar_and_named_play(words, query, kind):
    assert music._parse_music_request(words) == (query, kind)


@pytest.mark.parametrize('link,expected', [
    ('spotify:playlist:abc123', {'uri': 'spotify:playlist:abc123', 'type': 'playlist'}),
    ('https://open.spotify.com/album/abc123?si=token', {'uri': 'spotify:album:abc123', 'type': 'album'}),
    ('https://www.open.spotify.com/artist/abc123', {'uri': 'spotify:artist:abc123', 'type': 'artist'}),
    ('https://open.spotify.com/track/abc123/', {'uri': 'spotify:track:abc123', 'type': 'track'}),
])
def test_normalize_spotify_links_for_each_library_type(link, expected):
    assert music.normalize_spotify_link(link) == expected


def test_normalize_spotify_link_rejects_non_spotify_url():
    assert music.normalize_spotify_link('https://example.com/playlist/abc') is None


def test_flat_music_library_migrates_once_to_typed_rows(caplog):
    caplog.set_level('INFO')
    config = {'music_library': {'Morning Coffee': 'spotify:playlist:abc'}}
    assert music.migrate_music_library(config) == {
        'Morning Coffee': {'uri': 'spotify:playlist:abc', 'type': 'playlist'}}
    assert 'migrated legacy music_library' in caplog.text
    caplog.clear()
    music.migrate_music_library(config)
    assert 'migrated legacy music_library' not in caplog.text


def test_resolver_handles_exact_prefix_and_edit_distance():
    app = _app({'Morning Coffee': {'uri': 'spotify:playlist:coffee', 'type': 'playlist'}})
    for spoken in ('MORNING, COFFEE!', 'morning', 'morning coffe'):
        state, entry = music.resolve_music_name(app, spoken, 'playlist')
        assert state == 'match'
        assert entry['name'] == 'Morning Coffee'


def test_resolver_reports_ambiguous_and_missing_names():
    app = _app({
        'Morning Coffee': {'uri': 'spotify:playlist:coffee', 'type': 'playlist'},
        'Morning Run': {'uri': 'spotify:playlist:run', 'type': 'playlist'},
    })
    state, entries = music.resolve_music_name(app, 'morning', 'playlist')
    assert state == 'ambiguous'
    assert [entry['name'] for entry in entries] == ['Morning Coffee', 'Morning Run']
    assert music.resolve_music_name(app, 'evening', 'playlist') == ('none', [])


def test_user_library_wins_over_a_hardcoded_song_name():
    app = _app({'Mad World': {'uri': 'spotify:playlist:mine', 'type': 'playlist'}})
    state, entry = music.resolve_music_name(app, 'mad world')
    assert state == 'match'
    assert entry['uri'] == 'spotify:playlist:mine'


def test_named_music_grammar_is_registered_in_the_media_catalog():
    plugin_commands._reinstall_module_commands(music)
    for phrase in ('play', 'playlist', 'album', 'artist'):
        entry = plugin_commands._REGISTRY[phrase]
        assert entry['pack'] == 'media'
        assert entry['ai_composable'] is True
    assert plugin_commands._REGISTRY['play']['param_schema']['name']['type'] == 'str'
    for phrase in ('playlist', 'album', 'artist'):
        assert plugin_commands._REGISTRY[phrase]['param_schema']['remainder']['type'] == 'str'


def test_bare_play_unchanged(monkeypatch):
    transport = Mock(return_value=True)
    monkeypatch.setattr(music, '_media_transport', transport)
    assert music.handle_media_play(SimpleNamespace(), '') is True
    transport.assert_called_once_with('play')


def test_pause_music_returns_a_claimed_failure_when_transport_refuses(monkeypatch):
    failed = DispatchResult(
        DispatchState.FAILED, phrase='pause', detail={'reason': 'nothing is playing'},
    )
    transport = Mock(return_value=failed)
    monkeypatch.setattr(music, '_media_transport', transport)

    assert music.handle_media_pause(SimpleNamespace(), '') is failed
    transport.assert_called_once_with('pause')


def test_music_transport_aliases_are_not_foreground_scoped():
    plugin_commands._reinstall_module_commands(music)
    assert plugin_commands._REGISTRY['pause music']['phrase'] == 'pause'
    assert plugin_commands._REGISTRY['next song']['phrase'] == 'next'
    assert plugin_commands._REGISTRY['previous song']['phrase'] == 'previous music'
    assert plugin_commands._REGISTRY['unpause music']['phrase'] == 'play'
    assert plugin_commands._REGISTRY['resume music']['phrase'] == 'play'


def test_bare_play_music_resumes_current_media(monkeypatch):
    transport = Mock(return_value=DispatchResult(DispatchState.COMPLETED, phrase='play'))
    monkeypatch.setattr(music, '_media_transport', transport)
    result = music.handle_play(SimpleNamespace(), '')
    assert result.state is DispatchState.COMPLETED
    transport.assert_called_once_with('play')


@pytest.mark.parametrize('action,message', [('pause', 'paused music'), ('play', 'resumed music')])
def test_smtc_success_reports_the_actual_media_action(monkeypatch, action, message):
    import sys
    control = SimpleNamespace(GlobalSystemMediaTransportControlsSessionManager=SimpleNamespace(
        request_async=AsyncMock(return_value=SimpleNamespace(get_current_session=lambda: SimpleNamespace(
            try_play_async=AsyncMock(return_value=True), try_pause_async=AsyncMock(return_value=True))))))
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', control)
    result = music._media_transport(action)
    assert result.state is DispatchState.COMPLETED
    assert result.detail == {'media_action': action, 'message': message}


def test_spotify_absent_launch_then_play(playback):
    manager, spotify, launch = playback
    manager.get_sessions.side_effect = [[], [], [spotify]]
    result = music.handle_media_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'some rock')
    assert result.state is DispatchState.COMPLETED
    assert [c.args[0] for c in launch.call_args_list] == ['spotify:', 'spotify:track:rock']
    spotify.try_play_async.assert_awaited_once()


def test_other_app_active_fails_without_controlling_it(playback):
    manager, spotify, _ = playback
    other = session('vlc.exe', 'A movie')
    manager.get_current_session.return_value = other
    result = music.handle_media_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'rock')
    assert result.state is DispatchState.FAILED
    assert result.detail['message'] == 'played in vlc.exe, not Spotify'
    other.try_play_async.assert_not_awaited()
    spotify.try_play_async.assert_awaited_once()


def test_startup_timeout_does_not_play_other_session(playback):
    manager, spotify, launch = playback
    manager.get_sessions.return_value = []
    result = music.handle_media_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'rock')
    assert result.state is DispatchState.FAILED
    assert '8 s' in result.detail['message']
    launch.assert_called_once_with('spotify:')
    spotify.try_play_async.assert_not_awaited()


def test_play_rejected_is_failure(playback):
    playback[1].try_play_async.return_value = False
    assert music.handle_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'rock').state is DispatchState.FAILED


def test_paused_spotify_is_not_success(playback):
    playback[1].get_playback_info.return_value.playback_status = 5
    assert music.handle_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'rock').state is DispatchState.FAILED


def test_metadata_unavailable_does_not_invent_requested_title(playback):
    playback[1].try_get_media_properties_async.side_effect = RuntimeError('unavailable')
    result = music.handle_play(_app({'Rock': {'uri': 'spotify:track:rock', 'type': 'track'}}), 'rock')
    assert result.state is DispatchState.COMPLETED
    assert result.detail['actual_started'] is None
    assert 'title unavailable' in result.detail['message']


def test_configured_spotify_uri(playback):
    app = _app({"Morne's Mix": {'uri': 'spotify:playlist:abc', 'type': 'playlist'}})
    music.handle_play(app, "my Morne's Mix playlist")
    playback[2].assert_called_once_with('spotify:playlist:abc')


def test_unmatched_name_never_opens_search_until_user_says_search(playback, monkeypatch):
    app = _app()
    result = music.handle_play(app, 'unlisted music')
    assert result.state is DispatchState.FAILED
    assert "No 'unlisted music' in your music library" in result.detail['message']
    playback[2].assert_not_called()

    announce = Mock()
    monkeypatch.setattr(music, '_announce', announce)
    search = music.handle_music_search(app, '')
    assert search.state is DispatchState.COMPLETED
    assert search.detail['message'] == 'Spotify search opened.'
    assert search.detail['outcome'] == 'opened_search'
    assert search.detail['request_verified'] is False
    assert search.detail['uri'] == 'spotify:search:unlisted%20music'
    assert search.detail['query'] == 'unlisted music'
    assert 'Playing' not in search.detail['message']
    announce.assert_called_once_with(app, 'Spotify search opened.')
    playback[2].assert_called_once_with('spotify:search:unlisted%20music')
    playback[1].try_play_async.assert_not_awaited()


def test_music_search_open_failure_is_announced_without_transport(playback, monkeypatch):
    app = _app()
    music.handle_play(app, 'unlisted music')
    playback[2].side_effect = OSError('protocol handler unavailable')
    announce = Mock()
    monkeypatch.setattr(music, '_announce', announce)

    result = music.handle_music_search(app, '')

    assert result.state is DispatchState.FAILED
    assert result.detail['message'] == 'Spotify search failed.'
    announce.assert_called_once_with(app, 'Spotify search failed.')
    playback[1].try_play_async.assert_not_awaited()


def test_music_search_expires_after_thirty_seconds(playback, monkeypatch):
    app = _app()
    music.handle_play(app, 'unlisted music')
    monkeypatch.setattr(music, 'time', SimpleNamespace(monotonic=lambda: 31.0))

    assert music.handle_music_search(app, '') is False
    playback[2].assert_not_called()


def test_music_search_rejects_old_request_generation(playback):
    app = _app()
    app._ava_cmd_generation = 3
    music.handle_play(app, 'unlisted music')
    app._ava_cmd_generation = 4

    assert music.handle_music_search(app, '') is False
    playback[2].assert_not_called()


def test_second_play_request_replaces_pending_search(playback):
    app = _app()
    music.handle_play(app, 'first missing query')
    music.handle_play(app, 'second missing query')

    result = music.handle_music_search(app, '')

    assert result.state is DispatchState.COMPLETED
    playback[2].assert_called_once_with('spotify:search:second%20missing%20query')
    playback[1].try_play_async.assert_not_awaited()


def test_music_search_without_pending_request_returns_false(playback):
    assert music.handle_music_search(_app(), '') is False
    playback[2].assert_not_called()


def test_ambiguous_name_does_not_open_spotify(playback):
    app = _app({
        'Morning Coffee': {'uri': 'spotify:playlist:coffee', 'type': 'playlist'},
        'Morning Run': {'uri': 'spotify:playlist:run', 'type': 'playlist'},
    })
    result = music.handle_playlist(app, 'morning')
    assert result.state is DispatchState.FAILED
    assert result.detail['message'] == 'Which one: Morning Coffee or Morning Run?'
    playback[2].assert_not_called()


@pytest.mark.parametrize('source,expected', [
    ('Spotify.exe', True), ('Spotify', True),
    ('SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify', True),
    ('notspotify.exe', False), ('chrome.exe', False),
])
def test_source_app_id(source, expected):
    assert music._is_spotify(session(source, 'track')) is expected


def test_uri_uses_protocol_handler_without_shell(monkeypatch):
    start = Mock()
    monkeypatch.setattr(music.os, 'startfile', start)
    music._open_track('spotify:search:Morne%27s%20Mix')
    start.assert_called_once_with('spotify:search:Morne%27s%20Mix')


@pytest.fixture
def named_transport(monkeypatch):
    import sys
    browser = session('chrome.exe', 'video')
    player = session('Spotify.Spotify', 'song')
    for item in (browser, player):
        item.try_pause_async = AsyncMock(return_value=True)
    manager = SimpleNamespace(get_current_session=Mock(return_value=browser),
                              get_sessions=Mock(return_value=[browser, player]))
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=SimpleNamespace(
            request_async=AsyncMock(return_value=manager))))
    return manager, browser, player


@pytest.mark.parametrize('action,verb', [('pause', 'paused'), ('play', 'playing')])
def test_named_transport_targets_player_and_generic_targets_current(named_transport, action, verb):
    manager, browser, player = named_transport
    result = music._media_transport(action, 'spotify')
    assert result.state is DispatchState.COMPLETED
    assert result.detail['message'] == f'Spotify {verb}'
    getattr(player, f'try_{action}_async').assert_awaited_once()
    getattr(browser, f'try_{action}_async').assert_not_awaited()
    manager.get_current_session.assert_not_called()
    music._media_transport(action)
    getattr(browser, f'try_{action}_async').assert_awaited_once()


def test_named_transport_missing_never_falls_back(named_transport):
    manager, browser, _ = named_transport
    manager.get_sessions.return_value = [browser]
    result = music._media_transport('pause', 'spotify')
    assert result.state is DispatchState.FAILED
    assert result.detail['reason'] == 'Spotify has no media session'
    browser.try_pause_async.assert_not_awaited()
    manager.get_current_session.assert_not_called()


@pytest.mark.parametrize('initial,action,expected,already', [
    (50, 'volume_up', 60, False), (50, 'volume_down', 40, False),
    (95, 'volume_up', 100, False), (5, 'volume_down', 0, False),
    (100, 'volume_up', 100, True), (0, 'volume_down', 0, True),
])
def test_named_volume_all_sessions_and_exact_reply(monkeypatch, initial, action, expected, already):
    from contextlib import contextmanager
    volumes = [SimpleNamespace(get_volume=Mock(return_value=initial), set_volume=Mock()),
               SimpleNamespace(get_volume=Mock(return_value=20), set_volume=Mock())]
    @contextmanager
    def sessions(process):
        assert process == 'spotify.exe'
        yield iter(volumes)
    monkeypatch.setattr(music, '_audio_sessions', sessions)
    announce = Mock()
    monkeypatch.setattr(music, '_announce', announce)
    plugin_commands._reinstall_module_commands(music)
    phrase = 'spotify volume up' if action == 'volume_up' else 'spotify volume down'
    app = _app()
    result = plugin_commands._REGISTRY[phrase]['func'](app, '')
    message = f'Spotify volume {expected}%' + (' (already)' if already else '')
    assert result.state is DispatchState.COMPLETED
    assert result.detail['message'] == message
    announce.assert_called_once_with(app, message)
    for volume in volumes:
        volume.set_volume.assert_called_once_with(expected)


def test_named_volume_missing_and_mute_compatibility(monkeypatch):
    from contextlib import contextmanager
    volume = SimpleNamespace(toggle_mute=Mock(return_value=True))
    available = []
    @contextmanager
    def sessions(process):
        assert process == 'spotify.exe'
        yield iter(available)
    monkeypatch.setattr(music, '_audio_sessions', sessions)
    monkeypatch.setattr(music, '_announce', Mock())
    plugin_commands._reinstall_module_commands(music)
    result = plugin_commands._REGISTRY['spotify volume up']['func'](_app(), '')
    assert result.state is DispatchState.FAILED
    assert result.detail['message'] == "Spotify isn't playing anything"
    assert music._spotify_mute_toggle() is False
    available.extend([volume, SimpleNamespace(toggle_mute=Mock())])
    assert music._spotify_mute_toggle() is True
    volume.toggle_mute.assert_called_once_with()
    available[1].toggle_mute.assert_not_called()


def test_named_phrases_real_registry_and_disabled_pack():
    from tools.dump_command_metadata import build_executor
    from samsara.command_packs import get_enabled_packs, PACKS
    from plugins.commands import volume
    # conftest suppresses discovery; reinstall relevant real modules explicitly.
    plugin_commands._reinstall_module_commands(music)
    plugin_commands._reinstall_module_commands(volume)
    matcher = build_executor()._matcher
    groups = {
        'pause spotify': ['pause spotify', 'stop spotify'],
        'play spotify': ['play spotify', 'resume spotify'],
        'spotify volume up': ['spotify volume up', 'spotify louder', 'turn spotify up'],
        'spotify volume down': ['spotify volume down', 'spotify quieter', 'turn spotify down'],
    }
    for canonical, phrases in groups.items():
        expected = plugin_commands._REGISTRY[canonical]['func']
        for phrase in phrases:
            entry, remainder = matcher.match(phrase.upper() + '!')
            assert entry.handler is expected and remainder == ''
            assert entry.pack == 'spotify' and entry.scope is None
            assert 'Spotify' in entry.description
            assert entry.risk_class == 'safe' and entry.ai_composable
            assert entry.side_effects == ['audio']
    for phrase, canonical in [('volume up', 'volume up'), ('louder', 'volume up'),
                              ('quieter', 'volume down'), ('pause', 'pause'),
                              ('play', 'play'), ('play Morning Coffee', 'play')]:
        assert matcher.match(phrase)[0].phrase == canonical
    assert matcher.match('play Morning Coffee')[1] == 'Morning Coffee'
    assert PACKS['spotify']['default_enabled'] is True
    matcher.set_enabled_packs(get_enabled_packs({'command_packs': {'spotify': False}}))
    for phrases in groups.values():
        for phrase in phrases:
            assert matcher.match(phrase) == (None, '')
    assert matcher.match('pause')[0].phrase == 'pause'


def test_fake_row_uses_same_registration_loop(monkeypatch, named_transport):
    from samsara.command_registry import CommandMatcher
    rows = {'testplayer': ('testplayer.exe', 'testplayer', 'Test Player', 'testplayer', ('transport',))}
    # Isolate test registrations from module discovery's persistent entries.
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    monkeypatch.setattr(plugin_commands, '_MODULE_ENTRIES', {})
    music._register_named_commands(rows)
    matcher = CommandMatcher()
    matcher.load_plugins(plugin_commands._REGISTRY)
    matcher.freeze()
    manager, browser, spotify = named_transport
    fake = session('vendor.testplayer', 'song')
    fake.try_pause_async = AsyncMock(return_value=True)
    manager.get_sessions.return_value = [browser, spotify, fake]
    entry, remainder = matcher.match('pause testplayer')
    result = entry.handler(_app(), remainder)
    assert result.detail['message'] == 'Test Player paused'
    fake.try_pause_async.assert_awaited_once()
    spotify.try_pause_async.assert_not_awaited()
    browser.try_pause_async.assert_not_awaited()
    assert matcher.match('testplayer volume up') == (None, '')


def test_named_catalog_rows():
    from samsara import command_catalog as cc
    from tools.dump_command_metadata import build_executor
    plugin_commands._reinstall_module_commands(music)
    specs = cc.build_catalog(build_executor())
    rows = [row for row in specs if row.pack == 'spotify']
    assert len(rows) == 4
    assert all('Spotify' in row.description for row in rows)


@pytest.mark.parametrize('early_exit', [False, True])
def test_audio_finder_retains_volumes_and_balances_com_with_fakes(monkeypatch, early_exit):
    import ctypes
    import psutil
    released = []
    acquired = []
    writes = []

    def acquire(out, pointer):
        out._obj.value = pointer
        acquired.append(pointer)
        return 0

    def call(address, *args):
        pointer, slot = divmod(address, 100)
        assert pointer not in released
        if slot == 2:
            released.append(pointer)
            return 0
        if pointer == 1 and slot == 4:
            return acquire(args[-1], 2)
        if pointer == 2 and slot == 3:
            return acquire(args[-1], 3)
        if pointer == 3 and slot == 5:
            return acquire(args[-1], 4)
        if pointer == 4 and slot == 3:
            args[-1]._obj.value = 3
            return 0
        if pointer == 4 and slot == 4:
            return acquire(args[-1], 10 + args[1])
        if pointer in (10, 11, 12) and slot == 0:
            return acquire(args[-1], pointer + 10)
        if pointer in (20, 21, 22) and slot == 14:
            args[-1]._obj.value = pointer
            return 0
        if pointer in (20, 22) and slot == 0:
            return acquire(args[-1], pointer + 10)
        if pointer in (30, 32) and slot == 4:
            args[-1]._obj.value = 0.5
            return 0
        if pointer in (30, 32) and slot == 3:
            writes.append((pointer, args[1]))
            return 0
        raise AssertionError((pointer, slot))

    monkeypatch.setattr(ctypes, 'cast', lambda ptr, _type: [[ptr.value * 100 + i for i in range(16)]])
    monkeypatch.setattr(ctypes, 'WINFUNCTYPE', lambda *types: lambda address: lambda *args: call(address, *args))
    ole = SimpleNamespace(CoInitialize=Mock(return_value=0), CoUninitialize=Mock(),
                          CoCreateInstance=Mock(side_effect=lambda *args: acquire(args[-1], 1)))
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(ole32=ole))
    monkeypatch.setattr(psutil, 'Process', lambda pid: SimpleNamespace(
        name=lambda: 'browser.exe' if pid == 21 else 'spotify.exe'))
    if early_exit:
        with pytest.raises(RuntimeError, match='operation failed'):
            with music._audio_sessions('spotify.exe') as sessions:
                assert next(sessions).get_volume() == 50
                raise RuntimeError('operation failed')
    else:
        with music._audio_sessions('spotify.exe') as sessions:
            volumes = list(sessions)
            assert len(volumes) == 2
            assert 30 not in released and 32 not in released
            for volume in volumes:
                volume.set_volume(60)
            assert writes == [(30, 0.6), (32, 0.6)]
    assert sorted(acquired) == sorted(released)
    assert len(released) == len(set(released))
    ole.CoUninitialize.assert_called_once_with()
