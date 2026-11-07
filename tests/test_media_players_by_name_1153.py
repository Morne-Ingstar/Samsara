"""Named player contracts. All player, audio, network and launch effects faked."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import sys

import pytest

from plugins.commands import music, vlc
from samsara import media_player_control as control, plugin_commands as pc
from samsara.command_registry import CommandMatcher, DispatchResult, DispatchState
from samsara.command_scope import MatchContext
from samsara.media_players import PLAYERS, PLAYER_PHRASES, Player, phrases_for


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('Unmocked player operation')
    monkeypatch.setattr(pc, '_REGISTRY', {})
    monkeypatch.setattr(pc, '_MODULE_ENTRIES', {})
    monkeypatch.setattr(pc, '_shared_matcher', None)
    monkeypatch.setattr(music, '_SESSION_MANAGER', None)
    monkeypatch.setattr(music, '_announce', Mock())
    monkeypatch.setattr(music, '_audio_sessions', deny)
    monkeypatch.setattr(control, 'player_windows', deny)
    monkeypatch.setattr(control, 'find_executable', deny)
    monkeypatch.setattr(control, 'package_installed', deny)
    monkeypatch.setattr(control, 'send_message', deny)
    monkeypatch.setattr(control, 'vlc_hotkey', deny)
    monkeypatch.setattr(vlc, '_settings', lambda: {})
    monkeypatch.setattr(vlc, 'build_opener', deny)
    import subprocess
    import os
    monkeypatch.setattr(subprocess, 'Popen', deny)
    monkeypatch.setattr(os, 'startfile', deny, raising=False)


def matcher(packs=None):
    music._register_named_commands()
    vlc._register()
    m = CommandMatcher()
    m.load_plugins(pc._REGISTRY)
    m.set_context_provider(lambda: MatchContext.for_app('notepad.exe'))
    if packs is not None:
        m.set_enabled_packs(packs)
    m.freeze()
    return m


@pytest.mark.parametrize('name', PLAYERS)
def test_every_row_expands_declared_capabilities_and_aliases(name):
    m = matcher()
    player = PLAYERS[name]
    for action, (capability, _, _) in PLAYER_PHRASES.items():
        phrases = phrases_for(name, player, action)
        assert bool(phrases) == (capability in player.capabilities)
        for phrase in phrases:
            entry, remainder = m.match(phrase.upper() + '!')
            assert entry is not None, phrase
            assert entry.pack == player.pack
            assert player.display in entry.description
            assert remainder == ''
            assert entry.scope is None  # Explicit names can control background apps.
    for action in ('pause', 'play', 'toggle', 'next', 'previous'):
        if 'transport' not in player.capabilities:
            assert m.match(f'{action} {name}')[0] is None


def test_fake_row_registers_and_dispatches_without_new_handler(monkeypatch):
    row = Player(('invented.exe',), ('Vendor.Invented',), 'Invented', 'invented',
                 ('transport',), ('invented', 'in vented'))
    music._register_named_commands({'invented': row})
    m = CommandMatcher()
    m.load_plugins(pc._REGISTRY)
    m.freeze()
    call = Mock(return_value=DispatchResult(DispatchState.COMPLETED))
    monkeypatch.setattr(music, '_media_transport', call)
    entry, rest = m.match('pause in vented')
    assert entry.handler(SimpleNamespace(), rest).state is DispatchState.COMPLETED
    call.assert_called_once_with('pause', 'Vendor.Invented')
    assert m.match('invented volume up')[0] is None


def test_legacy_spotify_phrases_and_dispatch_unchanged(monkeypatch):
    m = matcher()
    call = Mock(return_value=DispatchResult(DispatchState.COMPLETED))
    monkeypatch.setattr(music, '_media_transport', call)
    for phrase in ('pause spotify', 'stop spotify'):
        entry, rest = m.match(phrase)
        assert entry.handler(SimpleNamespace(), rest).detail['message'] == 'Spotify paused'
    assert call.call_args_list[0].args == ('pause', 'spotify')
    # Retained outside shared registration: first-session toggle, media pack.
    assert phrases_for('spotify', PLAYERS['spotify'], 'mute')[0] == 'silence spotify'
    assert 'mute spotify' not in pc._REGISTRY


@pytest.mark.parametrize('words', [
    'please explain why pause vlc is useful', 'pause vlc while I write',
    'I said vlc skip forward 30 seconds yesterday',
    'vlc skip forward 30 seconds then open browser', 'vlc subtitles off please',
])
def test_whole_utterance_rejects_prose(words):
    m = matcher()
    assert m.match(words)[0] is None
    assert pc.find_command(words)[0] is None


def test_disabling_vlc_removes_fixed_and_duration_commands():
    m = matcher({'core', 'spotify', 'media'})
    for phrase in ('pause vlc', 'open vlc', 'vlc volume up', 'vlc faster',
                   'vlc subtitles off', 'vlc skip forward 30 seconds'):
        assert m.match(phrase)[0] is None


def test_vlc_pause_uses_real_player_pause_action_never_toggle(monkeypatch):
    state = {'paused': True}
    operations = []
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 12)])
    def action(hwnd, verb):
        assert hwnd == 99
        operations.append(verb)
        if verb == 'pause':
            state['paused'] = True
        elif verb == 'play-pause':
            state['paused'] = not state['paused']
        else:
            pytest.fail(verb)
    monkeypatch.setattr(control, 'vlc_hotkey', action)
    entry, rest = matcher().match('pause vlc')
    for _ in range(2):
        assert entry.handler(SimpleNamespace(), rest).state is DispatchState.COMPLETED
        assert state['paused']
    state['paused'] = False  # Simulate a mouse/remote play between voice commands.
    entry.handler(SimpleNamespace(), rest)
    assert state['paused'] and operations == ['pause', 'pause', 'pause']


@pytest.mark.parametrize('windows', [[], [(1, 10), (2, 20)]])
def test_missing_or_ambiguous_player_fails_without_sending(monkeypatch, windows):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: windows)
    entry, rest = matcher().match('pause vlc')
    assert entry.handler(SimpleNamespace(), rest).state is DispatchState.FAILED


@pytest.mark.parametrize('action,method', [('pause', 'try_pause_async'), ('play', 'try_play_async'),
                                          ('next', 'try_skip_next_async'),
                                          ('previous', 'try_skip_previous_async'),
                                          ('toggle', 'try_toggle_play_pause_async')])
def test_smtc_matches_alternative_sources_never_current(monkeypatch, action, method):
    player = SimpleNamespace(source_app_user_model_id='Microsoft.ZuneMusic_vendor!App')
    call = AsyncMock(return_value=True)
    setattr(player, method, call)
    other = SimpleNamespace(source_app_user_model_id='chrome.exe')
    manager = SimpleNamespace(get_sessions=lambda: [other, player], get_current_session=Mock())
    backend = SimpleNamespace(request_async=AsyncMock(return_value=manager))
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=backend))
    result = music._media_transport(action, PLAYERS['media player'].sources)
    assert result.state is DispatchState.COMPLETED
    call.assert_awaited_once()
    manager.get_current_session.assert_not_called()


def test_smtc_missing_or_refused_has_no_fallback(monkeypatch):
    player = SimpleNamespace(source_app_user_model_id='foobar2000.exe',
                             try_pause_async=AsyncMock(return_value=False))
    manager = SimpleNamespace(get_sessions=lambda: [player], get_current_session=Mock())
    backend = SimpleNamespace(request_async=AsyncMock(return_value=manager))
    monkeypatch.setitem(sys.modules, 'winsdk.windows.media.control', SimpleNamespace(
        GlobalSystemMediaTransportControlsSessionManager=backend))
    for stems in (('AppleInc.AppleMusic', 'AppleMusic.exe'), ('foobar2000',)):
        assert music._media_transport('pause', stems).state is DispatchState.FAILED
    manager.get_current_session.assert_not_called()


def test_multiple_executables_and_explicit_mute_are_idempotent(monkeypatch):
    volumes = [SimpleNamespace(set_mute=Mock()) for _ in range(2)]
    @contextmanager
    def sessions(exes):
        assert exes == PLAYERS['media player'].exes
        yield iter(volumes)
    monkeypatch.setattr(music, '_audio_sessions', sessions)
    m = matcher()
    for phrase, expected in [('mute media player', True), ('mute media player', True),
                             ('unmute media player', False)]:
        entry, rest = m.match(phrase)
        assert entry.handler(SimpleNamespace(), rest).state is DispatchState.COMPLETED
        for volume in volumes:
            assert volume.set_mute.call_args.args == (expected,)


@pytest.mark.parametrize('action,state,expected', [('pause', 1, [0x40]), ('pause', 2, [0x40, 15]),
                                                 ('play', 2, [0x40]), ('play', 1, [0x40, 13]),
                                                 ('next', 2, [17]), ('previous', 2, [18])])
def test_aimp_state_and_remote_protocol(monkeypatch, action, state, expected):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(7, 8)])
    calls = []
    def send(hwnd, msg, wparam=0, lparam=0):
        calls.append(wparam)
        assert hwnd == 7 and msg in (0x475, 0x477)
        return state
    monkeypatch.setattr(control, 'send_message', send)
    control.native_transport(PLAYERS['aimp'], action)
    assert calls == expected


def test_itunes_gets_existing_com_instance_and_balances_com(monkeypatch):
    import pythoncom
    import win32com.client
    init, release = Mock(), Mock()
    client = SimpleNamespace(PlayerState=0, Pause=Mock(), Play=Mock())
    get = Mock(return_value=client)
    monkeypatch.setattr(pythoncom, 'CoInitialize', init)
    monkeypatch.setattr(pythoncom, 'CoUninitialize', release)
    monkeypatch.setattr(win32com.client, 'GetActiveObject', get)
    control.native_transport(PLAYERS['itunes'], 'pause')
    client.Pause.assert_not_called()
    client.PlayerState = 1
    control.native_transport(PLAYERS['itunes'], 'pause')
    client.Pause.assert_called_once()
    get.side_effect = RuntimeError('no running instance')
    with pytest.raises(RuntimeError):
        control.native_transport(PLAYERS['itunes'], 'play')
    assert init.call_count == release.call_count == 3
    assert get.call_args.args == ('iTunes.Application',)


@pytest.mark.parametrize('words,seconds', [('30 seconds', 30), ('a minute', 60), ('half a minute', 30),
                                        ('twenty-five seconds', 25), ('by 1 second', 1), ('', 10)])
def test_vlc_duration_is_exact(words, seconds):
    assert vlc.parse_seconds(words) == seconds


@pytest.mark.parametrize('words', ['0', '-5', 'nan', 'inf', '0.5 seconds', '30 minutes extra',
                                 '31 minutes', '10 seconds stremio', '30 then open browser'])
def test_vlc_invalid_duration_is_rejected(words):
    with pytest.raises(ValueError):
        vlc.parse_seconds(words)


def test_vlc_seek_with_custom_steps_and_no_focus(monkeypatch):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 8)])
    monkeypatch.setattr(vlc, '_settings', lambda: {'short-jump-size': '15'})
    call = Mock()
    monkeypatch.setattr(control, 'vlc_hotkey', call)
    entry, rest = matcher().match('VLC skip forward thirty seconds!')
    assert rest == 'thirty seconds'
    assert entry.handler(SimpleNamespace(), rest).state is DispatchState.COMPLETED
    assert [c.args for c in call.call_args_list] == [(99, 'jump+short')] * 2
    assert vlc._seek_actions(60, {}, False) == ('jump-medium',)
    with pytest.raises(RuntimeError, match='exact skip'):
        vlc._seek_actions(1, {}, True)


@pytest.mark.parametrize('phrase,action', [
    ('vlc faster', 'faster'), ('vlc slower', 'slower'), ('vlc normal speed', 'rate-normal'),
    ('vlc fullscreen', 'fullscreen'), ('vlc next subtitle track', 'subtitle-track'),
    ('vlc previous subtitle track', 'subtitle-revtrack'), ('vlc next audio track', 'audio-track'),
    ('vlc louder inside player', 'vol-up'), ('vlc quieter inside player', 'vol-down'),
    ('vlc toggle mute', 'vol-mute'),
])
def test_vlc_advanced_actions_target_receiver(monkeypatch, phrase, action):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 8)])
    call = Mock()
    monkeypatch.setattr(control, 'vlc_hotkey', call)
    entry, rest = matcher().match(phrase)
    assert entry.handler(SimpleNamespace(), rest).state is DispatchState.COMPLETED
    call.assert_called_once_with(99, action)


def test_bare_advanced_alias_is_vlc_scoped():
    m = matcher()
    assert m.match('faster playback')[0] is None
    m.set_context_provider(lambda: MatchContext.for_app('vlc.exe'))
    assert m.match('faster playback')[0].pack == 'vlc'


def test_subtitles_off_never_uses_a_toggle(monkeypatch):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 8)])
    entry, rest = matcher().match('vlc subtitles off')
    result = entry.handler(SimpleNamespace(), rest)
    assert result.state is DispatchState.FAILED and 'Web interface' in result.detail['reason']
    http = Mock()
    monkeypatch.setattr(vlc, '_http', http)
    entry.handler(SimpleNamespace(), rest)
    entry.handler(SimpleNamespace(), rest)
    assert all(c.args == ({}, 'subtitle_track', -1) for c in http.call_args_list)


def test_http_seek_has_no_native_retry_after_uncertain_result(monkeypatch):
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 8)])
    monkeypatch.setattr(vlc, '_settings', lambda: {'extraintf': 'http', 'http-password': 'test-only'})
    http = Mock(side_effect=RuntimeError('VLC Web control unavailable'))
    monkeypatch.setattr(vlc, '_http', http)
    entry, rest = matcher().match('vlc skip forward 1 second')
    assert entry.handler(SimpleNamespace(), rest).state is DispatchState.FAILED
    assert http.call_count == 1 and http.call_args.args[1:] == ('seek', '+1')


def test_http_stays_loopback_and_does_not_log_credentials(monkeypatch, caplog):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = b'{"state":"paused"}'
    opener = SimpleNamespace(open=Mock(return_value=response))
    build = Mock(return_value=opener)
    monkeypatch.setattr(vlc, 'build_opener', build)
    vlc._http({'extraintf': 'http', 'http-password': 'test-only', 'http-port': '9090'}, 'seek', '+1')
    req = opener.open.call_args.args[0]
    assert req.full_url == 'http://127.0.0.1:9090/requests/status.json?command=seek&val=%2B1'
    assert opener.open.call_args.kwargs == {'timeout': 1.0}
    assert build.call_args.args[0].proxies == {}
    assert isinstance(build.call_args.args[1], vlc._NoRedirect)
    assert not caplog.text
    with pytest.raises(RuntimeError):
        vlc._NoRedirect().redirect_request(None, None, None, None, None, None)


def test_pack_defaults_and_exe_mapping_follow_rows(monkeypatch):
    from samsara import command_packs as packs
    for row in PLAYERS.values():
        for exe in row.exes:
            assert packs.pack_for_exe(exe.upper()) == row.pack
    monkeypatch.setitem(packs.PACKS['vlc'], 'default_enabled', True)
    assert 'vlc' in packs.get_enabled_packs({})
    assert 'vlc' not in packs.get_enabled_packs({'command_packs': {'vlc': False}})
    monkeypatch.setitem(packs.PACKS['vlc'], 'default_enabled', False)
    assert 'vlc' not in packs.get_enabled_packs({})
    assert 'vlc' in packs.get_enabled_packs({'command_packs': {'vlc': True}})


def test_installed_detection_uses_exe_or_package(monkeypatch):
    monkeypatch.setattr(control, 'find_executable', lambda p: None)
    monkeypatch.setattr(control, 'package_installed', lambda p: p == 'AppleInc.AppleMusic')
    assert control.installed(PLAYERS['apple music'])
    assert not control.installed(PLAYERS['vlc'])
    monkeypatch.setattr(control, 'find_executable', lambda p: 'test-player.exe')
    assert control.installed(PLAYERS['vlc'])


def test_launch_uses_argument_list_and_close_is_graceful(monkeypatch):
    import subprocess
    import win32gui
    launch, close = Mock(), Mock()
    monkeypatch.setattr(control, 'find_executable', lambda p: 'test player.exe')
    monkeypatch.setattr(subprocess, 'Popen', launch)
    control.launch(PLAYERS['vlc'])
    launch.assert_called_once_with(['test player.exe'])
    monkeypatch.setattr(control, 'player_windows', lambda *a, **k: [(99, 8)])
    monkeypatch.setattr(win32gui, 'PostMessage', close)
    control.close(PLAYERS['vlc'])
    close.assert_called_once_with(99, 0x10, 0, 0)


def test_vlc_atom_lifetime_and_native_pause_message(monkeypatch):
    import ctypes
    original = _REAL_VLC_HOTKEY
    add, delete = Mock(return_value=123), Mock()
    dll = SimpleNamespace(GlobalAddAtomW=add, GlobalDeleteAtom=delete)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *a, **k: dll)
    send = Mock(return_value=1)
    monkeypatch.setattr(control, 'send_message', send)
    original(99, 'pause')
    add.assert_called_once_with('pause')
    send.assert_called_once_with(99, 0x312, 123)
    delete.assert_called_once_with(123)
    send.side_effect = RuntimeError('timed out')
    with pytest.raises(RuntimeError):
        original(99, 'pause')
    assert delete.call_count == 2


_REAL_VLC_HOTKEY = control.vlc_hotkey


@pytest.mark.parametrize('direction,target', [(-1, 40), (1, 60)])
@pytest.mark.parametrize('row', [
    ('synthetic.exe', 'Synthetic', 'Synthetic'),
    ('synthetic.exe', 'Synthetic', 'Synthetic', 'synthetic', ('volume',)),
])
def test_legacy_volume_factory_rows_and_directions(monkeypatch, row, direction, target):
    volume = SimpleNamespace(get_volume=lambda: 50, set_volume=Mock())

    @contextmanager
    def sessions(exe):
        assert exe == 'synthetic.exe'
        yield iter([volume])

    monkeypatch.setattr(music, '_audio_sessions', sessions)
    result = music._named_volume_handler(row, direction)(SimpleNamespace(), '')
    assert result.state is DispatchState.COMPLETED
    volume.set_volume.assert_called_once_with(target)


@pytest.mark.parametrize('phrase,handler', [
    ('pause', music.handle_media_pause), ('next', music.handle_media_next),
    ('previous music', music.handle_media_previous), ('mute', music.handle_spotify_mute),
])
def test_fixed_media_rejection_does_not_declare_a_text_argument(phrase, handler):
    from samsara.command_catalog import infer_args
    assert infer_args('plugin', phrase, {}, handler) == []
    assert infer_args('plugin', 'play', {}, music.handle_media_play)


@pytest.mark.parametrize('words', ['vlc', 'vlc while I write', 'music bee', 'windows media player'])
def test_fixed_generic_guard_still_rejects_named_fallthrough(monkeypatch, words):
    transport = Mock()
    monkeypatch.setattr(music, '_media_transport', transport)
    result = music.handle_media_pause(SimpleNamespace(), words)
    assert result.state is DispatchState.FAILED
    transport.assert_not_called()


def test_media_reference_reads_live_catalogue_and_pack_refresh(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from samsara import command_catalog as catalog
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow

    records = [
        {'canonical_id': 'music.pause_vlc', 'phrase': 'pause vlc', 'pack': 'vlc', 'aliases': []},
        {'canonical_id': 'vlc.vlc_skip_forward', 'phrase': 'vlc skip forward',
         'pack': 'vlc', 'aliases': [], 'examples': ['VLC skip forward 60 seconds']},
    ]
    live_rows = [{'test': 'live registry snapshot'}]
    loader = Mock(return_value=records)
    monkeypatch.setattr(catalog, 'guidance_catalog', loader)
    config = {'command_packs': {'vlc': True}}
    app = SimpleNamespace(config=config, command_executor=SimpleNamespace(
        _matcher=SimpleNamespace(list_commands=lambda: live_rows)))
    win = _QuickReferenceWindow(app)
    try:
        # 1153 n5: 1150's visible-control section also reads the live catalogue.
        assert loader.call_count >= 1
        assert all(args == (live_rows,) and not kwargs
                   for args, kwargs in loader.call_args_list)
        texts = [label.text() for label in win.findChildren(QLabel)]
        assert 'pause vlc' in texts and 'VLC skip forward 60 seconds' in texts
        config['command_packs']['vlc'] = False
        win.refresh()
        texts = [label.text() for label in win.findChildren(QLabel)]
        assert 'pause vlc' not in texts and 'VLC skip forward 60 seconds' not in texts
        assert 'No enabled player examples are available.' in texts
    finally:
        win.close()


def test_media_reference_missing_catalogue_never_invents_phrases(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel
    from samsara import command_catalog as catalog
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow

    monkeypatch.setattr(catalog, 'load_catalog_json', lambda *_: None)
    win = _QuickReferenceWindow(SimpleNamespace(config={'command_packs': {'vlc': True}}))
    try:
        texts = [label.text() for label in win.findChildren(QLabel)]
        assert 'No enabled player examples are available.' in texts
        assert 'pause vlc' not in texts
    finally:
        win.close()
