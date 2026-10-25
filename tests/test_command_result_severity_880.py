"""880: real music handlers with fake background players, on both displays."""
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.commands import music
from samsara.command_registry import DispatchState
from samsara.live_surface.feedback import schedule_feedback
from samsara.session_feedback import outcome_chip


@pytest.fixture
def player(monkeypatch):
    session = SimpleNamespace(source_app_user_model_id='Spotify.exe', **{
        name: AsyncMock(return_value=True) for name in (
            'try_pause_async', 'try_play_async', 'try_skip_next_async',
            'try_skip_previous_async')})
    manager = SimpleNamespace(get_sessions=Mock(return_value=[session]),
                              get_current_session=Mock(return_value=session))
    backend = SimpleNamespace(request_async=AsyncMock(return_value=manager))
    monkeypatch.setattr(music, '_SESSION_MANAGER', None)
    monkeypatch.setattr(music, '_SESSION_MANAGER_SOURCE', None)
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=backend))
    return session, manager


@pytest.fixture(params=['Full', 'Compact'])
def display(request, qapp, monkeypatch):
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator

    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': request.param == 'Full'}}},
                          audio_coordinator=SimpleNamespace(speak=Mock()),
                          _schedule_ui=lambda fn, *args: fn(*args))
    controller = None
    if request.param == 'Full':
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel())
        controller.start()
        app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        app.listening_indicator = ListeningIndicator()
        app.listening_indicator.set_idle_animation(False)
    calls = []

    def show(label, kind, ttl):
        calls.append((label, kind, ttl))
        schedule_feedback(app, label, kind, ttl)
        if controller:
            controller.drain()

    app._show_outcome_chip = show

    def check(result, expected):
        label, severity, _ = calls[-1]
        assert label == result.detail['message']
        assert severity == expected
        if controller:
            assert controller.view().notice.feedback.value == expected
            assert controller.view().notice.message == label
        else:
            assert app.listening_indicator._active_chip() == (label, expected)
        assert bool(getattr(app, '_bug_report_failures', {})) == (expected == 'warning')
        app.audio_coordinator.speak.assert_called_with(
            label, category='agent_response', interruptible=False)

    yield app, check
    if controller:
        controller.close()
    else:
        app.listening_indicator.destroy()
        app.listening_indicator.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('action', ['pause', 'play'])
@pytest.mark.parametrize('available,accepted', [(True, True), (True, False), (False, True)])
def test_named_transport_result_reaches_both_displays(player, display, action, available, accepted):
    session, manager = player
    manager.get_sessions.return_value = [session] if available else []
    getattr(session, f'try_{action}_async').return_value = accepted
    app, check = display
    result = music._named_transport_handler(music._NAMED_APPS['spotify'], action)(app, '')
    success = available and accepted
    assert result.state is (DispatchState.COMPLETED if success else DispatchState.FAILED)
    check(result, 'success' if success else 'warning')
    manager.get_current_session.assert_not_called()


@pytest.mark.parametrize('direction', [-1, 1])
@pytest.mark.parametrize('outcome', ['success', 'missing', 'exception'])
def test_named_volume_result_reaches_both_displays(monkeypatch, display, direction, outcome):
    volume = SimpleNamespace(get_volume=Mock(return_value=50), set_volume=Mock())
    if outcome == 'exception':
        volume.set_volume.side_effect = OSError('fake controller failure')

    @contextmanager
    def sessions(_):
        yield iter([] if outcome == 'missing' else [volume])

    monkeypatch.setattr(music, '_audio_sessions', sessions)
    app, check = display
    result = music._named_volume_handler(music._NAMED_APPS['spotify'], direction)(app, '')
    check(result, 'success' if outcome == 'success' else 'warning')


@pytest.mark.parametrize('action', ['pause', 'play', 'next', 'previous'])
@pytest.mark.parametrize('app_stem', [None, 'spotify', 'testplayer'])
def test_shared_transport_success_style(player, action, app_stem):
    session, _ = player
    session.source_app_user_model_id = f'{app_stem or "browser"}.exe'
    result = music._media_transport(action, app_stem)
    assert result.state is DispatchState.COMPLETED
    assert outcome_chip('command_executed', dict(result.detail, state=result.state.value)) == (
        result.detail['message'], 'success')


def test_announcement_without_confirmed_state_remains_warning():
    app = SimpleNamespace(_show_outcome_chip=Mock())
    music._announce(app, 'Spotify playback could not be verified')
    app._show_outcome_chip.assert_called_once_with(
        'Spotify playback could not be verified', 'warning', 3500)
