"""Real music handlers, with no live transport, launch, or dictation import."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.commands import music
from samsara import plugin_commands
from samsara.command_registry import DispatchState


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
        assert entry['param_schema']['name']['type'] == 'str'


def test_bare_play_unchanged(monkeypatch):
    transport = Mock(return_value=True)
    monkeypatch.setattr(music, '_media_transport', transport)
    assert music.handle_media_play(SimpleNamespace(), '') is True
    transport.assert_called_once_with('play')


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


def test_unmatched_name_never_opens_search_until_user_says_search(playback):
    app = _app()
    result = music.handle_play(app, 'unlisted music')
    assert result.state is DispatchState.FAILED
    assert "No 'unlisted music' in your music library" in result.detail['message']
    playback[2].assert_not_called()

    search = music.handle_music_search(app, '')
    assert search.state is DispatchState.COMPLETED
    playback[2].assert_called_once_with('spotify:search:unlisted%20music')


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
