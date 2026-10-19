"""J5 command routes and window/desktop effects, with all OS seams replaced."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins.commands import app_verbs as av, window_cube as cube, window_switcher as ws
from plugins.commands import windows as win
from samsara import plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.command_scope import MatchContext


@pytest.fixture
def app(monkeypatch):
    cube._reset_for_tests()
    app = SimpleNamespace(config={}, spoken=[], play_sound=Mock())
    monkeypatch.setattr(cube, '_speak', lambda a, t: a.spoken.append(t))
    monkeypatch.setattr(av, '_speak', lambda a, t: a.spoken.append(t))
    monkeypatch.setattr(cube, '_render', Mock())
    yield app
    cube._reset_for_tests()


@pytest.fixture
def matcher():
    # The shared conftest clears the runtime registry between tests.
    for module in (av, cube, ws, win):
        plugin_commands._reinstall_module_commands(module)
    matcher = CommandMatcher()
    data = json.loads((Path(__file__).resolve().parents[1] / 'commands.json').read_text())
    matcher.load_builtins(data['commands'])
    matcher.load_plugins(plugin_commands._REGISTRY)
    matcher.freeze()
    return matcher


@pytest.mark.parametrize('phrase,handler,remainder', [
    ('switch', cube.handle_show_cube, ''),
    ('switch to report', av.handle_focus, 'report'),
    ('list all windows', ws.handle_read_windows, ''),
    ('start Notepad', av.handle_open, 'Notepad'),
    ('desk two', av.handle_desk, 'two'),
    ('snap chrome left', 'handle_snap', 'chrome left'),
])
def test_registered_routes(matcher, phrase, handler, remainder):
    # test_window_destination reloads windows during collection, after this
    # parametrization may have been built. Resolve its current function now.
    if isinstance(handler, str):
        handler = getattr(win, handler)
    entry, rest = matcher.match(phrase)
    assert entry.handler is handler
    assert rest == remainder


@pytest.mark.parametrize('phrase', ['start listening', 'snap left', 'snap right',
                                       'show desktop', 'minimize all'])
def test_existing_literals_keep_precedence(matcher, phrase):
    entry, remainder = matcher.match(phrase)
    assert entry.source == 'builtin'
    assert not remainder


def test_choose_is_scoped(matcher):
    assert matcher.match('choose 3', MatchContext.for_app('test.exe'))[0] is None
    entry, remainder = matcher.match('choose 3', MatchContext.for_app(
        'test.exe', tags={'window_cube.selection'}))
    assert entry.handler is cube.handle_choose and remainder == '3'


def test_list_then_choose_uses_frozen_hwnd(app, monkeypatch):
    rows = [(11, 'Editor', 'Doc A'), (22, 'Browser', 'Doc B')]
    monkeypatch.setattr(cube, '_build_rows', lambda a: list(rows))
    focus = Mock()
    monkeypatch.setattr(cube, '_force_focus', focus)
    assert ws.handle_read_windows(app, '')
    assert '1: Editor' in app.spoken[-1] and '2: Browser' in app.spoken[-1]
    rows.reverse()
    assert cube.handle_choose(app, 'two')
    focus.assert_called_once_with(22)
    assert not cube._pinned


@pytest.mark.parametrize('replacement', [[], [(22, 'Browser', 'Different document')]])
def test_choose_refuses_closed_or_reused_slot(app, monkeypatch, replacement):
    rows = [(22, 'Browser', 'Doc B')]
    monkeypatch.setattr(cube, '_build_rows', lambda a: rows)
    focus = Mock()
    monkeypatch.setattr(cube, '_force_focus', focus)
    ws.handle_read_windows(app, '')
    rows = replacement
    cube.handle_choose(app, '1')
    focus.assert_not_called()


def test_switch_opens_existing_cube(app, monkeypatch):
    monkeypatch.setattr(cube, '_build_rows', lambda a: [(11, 'Editor', 'Doc')])
    assert cube.handle_show_cube(app, '')
    assert cube._pinned
    cube._render.assert_called_once()


def test_start_uses_existing_open(app, monkeypatch):
    opened = Mock(return_value=av.ActionResult.DONE)
    monkeypatch.setattr(av, 'do_open', opened)
    assert av.handle_open(app, 'Notepad')
    opened.assert_called_once_with('Notepad')


@pytest.mark.parametrize('text,side', [('top-left', 'top left'),
                                      ('left third', 'left third'), ('full', 'full')])
def test_snap_new_directions(app, monkeypatch, text, side):
    monkeypatch.setattr(win.win32gui, 'GetForegroundWindow', lambda: 44)
    snap = Mock(return_value=True)
    monkeypatch.setattr(win, 'snap_window', snap)
    assert win.handle_snap(app, text) is True
    snap.assert_called_once_with(44, side)


def test_third_and_full_work_area_geometry():
    monitor = {'rect': (-1367, 40, 0, 1000)}
    assert win._snap_rect(monitor, 'left third') == (-1367, 40, 455, 960)
    assert win._snap_rect(monitor, 'full') == (-1367, 40, 1367, 960)


def test_named_snap_binds_and_rechecks_identity(app, monkeypatch):
    monkeypatch.setattr(av, 'resolve_window', lambda n: (55, 'Doc', 'editor.exe'))
    monkeypatch.setattr(av.win32process, 'GetWindowThreadProcessId', lambda h: (3, 4))
    monkeypatch.setattr(av.win32gui, 'IsWindow', lambda h: True)
    monkeypatch.setattr(av.win32gui, 'GetWindowText', lambda h: 'Doc')

    def snap(hwnd, side, *, preflight):
        assert (hwnd, side) == (55, 'left') and preflight()
        monkeypatch.setattr(av.win32gui, 'GetWindowText', lambda h: 'New Doc')
        assert not preflight()
        return False

    monkeypatch.setattr(win, 'snap_window', snap)
    assert win.handle_snap(app, 'editor left').state is DispatchState.FAILED


def test_named_snap_missing_target_does_not_move_foreground(app, monkeypatch):
    monkeypatch.setattr(av, 'resolve_window', lambda n: None)
    snap = Mock()
    monkeypatch.setattr(win, 'snap_window', snap)
    assert win.handle_snap(app, 'missing left').state is DispatchState.FAILED
    snap.assert_not_called()


def test_next_screen_preserves_snapped_side_and_wraps(app, monkeypatch):
    first = {'index': 1, 'rect': (0, 0, 1920, 1040)}
    second = {'index': 2, 'rect': (1920, 0, 4480, 1400)}
    monkeypatch.setattr(win, 'get_monitors', lambda: [second, first])
    monkeypatch.setattr(win, '_get_monitor_for_window', lambda h, m: second)
    monkeypatch.setattr(win.win32gui, 'GetForegroundWindow', lambda: 66)
    monkeypatch.setattr(win.win32gui, 'GetWindowRect', lambda h: (3200, 0, 4480, 1400))
    snap = Mock(return_value=True)
    monkeypatch.setattr(win, 'snap_window', snap)
    assert win.handle_snap(app, 'next screen') is True
    snap.assert_called_once_with(66, 'right', target_monitor=first)


def test_next_screen_single_monitor_is_refused(app, monkeypatch):
    monkeypatch.setattr(win.win32gui, 'GetForegroundWindow', lambda: 66)
    monkeypatch.setattr(win, 'get_monitors', lambda: [])
    assert win.handle_snap(app, 'next screen').state is DispatchState.FAILED


def test_desk_observes_each_transition(monkeypatch):
    ids = [b'a' * 16, b'b' * 16, b'c' * 16]
    current = [2]
    keys = []
    monkeypatch.setattr(av, '_desktop_state', lambda: (ids, current[0]))
    import pyautogui

    def hotkey(*args):
        keys.append(args)
        current[0] += 1 if args[-1] == 'right' else -1

    monkeypatch.setattr(pyautogui, 'hotkey', hotkey)
    assert av._switch_desktop(1)
    assert keys == [('win', 'ctrl', 'left')] * 2
    assert av._switch_desktop(1)  # no toggle on the current desktop
    assert len(keys) == 2
    assert not av._switch_desktop(4)


def test_desk_missing_state_refuses_without_keys(app, monkeypatch):
    monkeypatch.setattr(av, '_desktop_state', Mock(side_effect=OSError()))
    import pyautogui
    keys = Mock()
    monkeypatch.setattr(pyautogui, 'hotkey', keys)
    assert av.handle_desk(app, 'two').state is DispatchState.FAILED
    keys.assert_not_called()


def test_desk_stalled_transition_stops(monkeypatch):
    monkeypatch.setattr(av, '_desktop_state', lambda: ([b'a', b'b'], 0))
    ticks = iter([0, 2])
    monkeypatch.setattr(av, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
    import pyautogui
    keys = Mock()
    monkeypatch.setattr(pyautogui, 'hotkey', keys)
    assert not av._switch_desktop(2)
    keys.assert_called_once_with('win', 'ctrl', 'right')


def test_desktop_registry_validates_ids(monkeypatch):
    key = Mock()
    key.__enter__ = Mock(return_value=key)
    key.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(av.winreg, 'OpenKey', lambda *a: key)
    values = {'VirtualDesktopIDs': b'a' * 16 + b'b' * 16,
              'CurrentVirtualDesktop': b'b' * 16}
    monkeypatch.setattr(av.winreg, 'QueryValueEx', lambda k, name: (values[name], 3))
    assert av._desktop_state() == ([b'a' * 16, b'b' * 16], 1)
    values['CurrentVirtualDesktop'] = b'c' * 16
    with pytest.raises(ValueError):
        av._desktop_state()


def test_choose_expiry(app, monkeypatch):
    monkeypatch.setattr(cube, '_build_rows', lambda a: [(11, 'Editor', 'Doc')])
    clock = [10.0]
    monkeypatch.setattr(cube, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    ws.handle_read_windows(app, '')
    clock[0] = 41.0
    assert not cube._selection_available()
    assert cube.handle_choose(app, '1') is False
