"""Real music handlers, with no live transport, launch, or dictation import."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.commands import music
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


@pytest.mark.parametrize('words,query,playlist', [
    ('my alternative rock playlist', 'alternative rock', True),
    ('alternative rock playlist', 'alternative rock', True),
    ('playlist alternative rock', 'alternative rock', True),
    ('some alternative rock', 'alternative rock', False),
    ('something from alternative rock', 'alternative rock', False),
    ('alternative rock', 'alternative rock', False),
    ('something from my alternative rock playlist', 'alternative rock', True),
    ('stuff from my alternative rock playlist', 'alternative rock', True),
    ("my Morne's Road Trip playlist", "Morne's Road Trip", True),
    ('playlist my Alt Rock', 'Alt Rock', True),
])
def test_grammar_and_named_play(words, query, playlist, playback):
    assert music._parse_music_request(words) == (query, playlist)
    result = music.handle_media_play(SimpleNamespace(config={}), words)
    assert result.state is DispatchState.COMPLETED
    assert result.detail['requested'] == query
    assert result.detail['actual_started'] == 'Alt Rock'
    assert result.detail['request_verified'] is False
    from urllib.parse import quote
    playback[2].assert_called_once_with('spotify:search:' + quote(query, safe=''))


def test_bare_play_unchanged(monkeypatch):
    transport = Mock(return_value=True)
    monkeypatch.setattr(music, '_media_transport', transport)
    assert music.handle_media_play(SimpleNamespace(), '') is True
    transport.assert_called_once_with('play')


def test_spotify_absent_launch_then_play(playback):
    manager, spotify, launch = playback
    manager.get_sessions.side_effect = [[], [], [spotify]]
    result = music.handle_media_play(SimpleNamespace(config={}), 'some rock')
    assert result.state is DispatchState.COMPLETED
    assert [c.args[0] for c in launch.call_args_list] == ['spotify:', 'spotify:search:rock']
    spotify.try_play_async.assert_awaited_once()


def test_other_app_active_fails_without_controlling_it(playback):
    manager, spotify, _ = playback
    other = session('vlc.exe', 'A movie')
    manager.get_current_session.return_value = other
    result = music.handle_media_play(SimpleNamespace(config={}), 'rock')
    assert result.state is DispatchState.FAILED
    assert result.detail['message'] == 'played in vlc.exe, not Spotify'
    other.try_play_async.assert_not_awaited()
    spotify.try_play_async.assert_awaited_once()


def test_startup_timeout_does_not_play_other_session(playback):
    manager, spotify, launch = playback
    manager.get_sessions.return_value = []
    result = music.handle_media_play(SimpleNamespace(config={}), 'rock')
    assert result.state is DispatchState.FAILED
    assert '8 s' in result.detail['message']
    launch.assert_called_once_with('spotify:')
    spotify.try_play_async.assert_not_awaited()


def test_play_rejected_is_failure(playback):
    playback[1].try_play_async.return_value = False
    assert music.handle_play(SimpleNamespace(config={}), 'rock').state is DispatchState.FAILED


def test_paused_spotify_is_not_success(playback):
    playback[1].get_playback_info.return_value.playback_status = 5
    assert music.handle_play(SimpleNamespace(config={}), 'rock').state is DispatchState.FAILED


def test_metadata_unavailable_does_not_invent_requested_title(playback):
    playback[1].try_get_media_properties_async.side_effect = RuntimeError('unavailable')
    result = music.handle_play(SimpleNamespace(config={}), 'rock')
    assert result.state is DispatchState.COMPLETED
    assert result.detail['actual_started'] is None
    assert 'title unavailable' in result.detail['message']


def test_configured_spotify_uri(playback):
    app = SimpleNamespace(config={'music_library': {"Morne's Mix": 'spotify:playlist:abc'}})
    music.handle_play(app, "my Morne's Mix playlist")
    playback[2].assert_called_once_with('spotify:playlist:abc')


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
