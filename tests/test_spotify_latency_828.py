"""Regression coverage for the Spotify media-manager cache."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.commands import music
from samsara.command_registry import DispatchState


def test_spotify_session_manager_is_requested_once_for_repeated_calls(monkeypatch):
    requests = []
    manager = object()

    async def slow_fake_request():
        requests.append("request")
        await asyncio.sleep(0.01)
        return manager

    monkeypatch.setattr(music, "_SESSION_MANAGER", None)
    first = asyncio.run(music._cached_session_manager(slow_fake_request))
    second = asyncio.run(music._cached_session_manager(slow_fake_request))

    assert first is manager
    assert second is manager
    assert requests == ["request"]


def install_backend(monkeypatch, manager):
    backend = SimpleNamespace(request_async=AsyncMock(return_value=manager))
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=backend))
    return backend


def player_manager():
    player = SimpleNamespace(source_app_user_model_id='Spotify.exe',
                             try_pause_async=AsyncMock(return_value=True))
    manager = SimpleNamespace(get_sessions=Mock(return_value=[player]),
                              get_current_session=Mock())
    return player, manager


def test_warm_named_pause_reuses_manager_but_resolves_sessions_again(monkeypatch):
    monkeypatch.setattr(music, '_SESSION_MANAGER', None)
    player, manager = player_manager()
    backend = install_backend(monkeypatch, manager)

    assert music._media_transport('pause', 'spotify').detail['message'] == 'Spotify paused'
    assert music._media_transport('pause', 'spotify').detail['message'] == 'Spotify paused'
    backend.request_async.assert_awaited_once()
    assert manager.get_sessions.call_count == 2
    assert player.try_pause_async.await_count == 2
    manager.get_current_session.assert_not_called()


def test_changed_backend_does_not_reuse_previous_manager(monkeypatch):
    monkeypatch.setattr(music, '_SESSION_MANAGER', None)
    old_player, old_manager = player_manager()
    old_backend = install_backend(monkeypatch, old_manager)
    assert music._media_transport('pause', 'spotify').state is DispatchState.COMPLETED

    player, manager = player_manager()
    backend = install_backend(monkeypatch, manager)
    assert music._media_transport('pause', 'spotify').detail['message'] == 'Spotify paused'
    old_backend.request_async.assert_awaited_once()
    backend.request_async.assert_awaited_once()
    old_player.try_pause_async.assert_awaited_once()
    player.try_pause_async.assert_awaited_once()


@pytest.mark.parametrize('available', [True, False])
def test_stale_cached_lookup_refreshes_using_normal_resolution(monkeypatch, available):
    monkeypatch.setattr(music, '_SESSION_MANAGER', None)
    old_player, old_manager = player_manager()
    backend = install_backend(monkeypatch, old_manager)
    music._media_transport('pause', 'spotify')
    old_manager.get_sessions.side_effect = RuntimeError('stale manager')
    player, manager = player_manager()
    if not available:
        manager.get_sessions.return_value = []
    backend.request_async.return_value = manager

    result = music._media_transport('pause', 'spotify')
    if available:
        assert result.state is DispatchState.COMPLETED
        assert result.detail['message'] == 'Spotify paused'
        player.try_pause_async.assert_awaited_once()
    else:
        assert result.state is DispatchState.FAILED
        assert result.detail['reason'] == 'Spotify has no media session'
        player.try_pause_async.assert_not_awaited()
    assert backend.request_async.await_count == 2
    old_player.try_pause_async.assert_awaited_once()
    manager.get_current_session.assert_not_called()
