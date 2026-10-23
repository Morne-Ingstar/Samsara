"""Voice wheels follow focus, without disturbing an existing pane or drag."""

import ctypes
from types import SimpleNamespace

import pytest

from samsara import scroll_target as target


class FakeUser32:
    def __init__(self):
        self.foreground = 0x123456789
        self.other = 0x234567890
        self.cursor = (1800, 400)
        self.moves = []
        self.wheels = []
        self.buttons = set()
        self.fail = None
        self.obscured = False

    def GetForegroundWindow(self):
        return self.foreground

    def GetAncestor(self, hwnd, flag):
        assert flag == target.GA_ROOT
        return self.foreground if hwnd in (self.foreground, 42) else self.other

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.buttons else 0

    def GetCursorPos(self, pointer):
        pointer._obj.x, pointer._obj.y = self.cursor
        return self.fail != 'cursor'

    def WindowFromPoint(self, point):
        if self.obscured:
            return self.other
        return 42 if point.x < 1000 else self.other

    def GetClientRect(self, hwnd, pointer):
        assert hwnd == self.foreground
        pointer._obj.left, pointer._obj.top = 0, 0
        pointer._obj.right = 0 if self.fail == 'empty' else 800
        pointer._obj.bottom = 600
        return self.fail != 'rect'

    def ClientToScreen(self, hwnd, pointer):
        pointer._obj.x += 100
        pointer._obj.y += 50
        return self.fail != 'screen'

    def SetCursorPos(self, x, y):
        if self.fail == 'move':
            return 0
        self.cursor = (x, y)
        self.moves.append(self.cursor)
        return 1

    def SendInput(self, count, pointer, size):
        inp = pointer._obj
        self.wheels.append((self.cursor, inp._input.mi.dwFlags,
                            ctypes.c_int32(inp._input.mi.mouseData).value))
        return 1


@pytest.fixture
def wheel(monkeypatch):
    from plugins.commands import scroll
    api = FakeUser32()
    monkeypatch.setattr(target, '_user32', lambda: api)
    monkeypatch.setattr(scroll, 'user32', api)
    return scroll, api


@pytest.mark.parametrize('name,flags,delta', [
    ('scroll_up_slow', 0x800, 360), ('scroll_down_slow', 0x800, -360),
    ('scroll_up', 0x800, 960), ('scroll_down', 0x800, -960),
    ('scroll_up_medium', 0x800, 1800), ('scroll_down_medium', 0x800, -1800),
    ('scroll_up_high', 0x800, 3000), ('scroll_down_high', 0x800, -3000),
    ('scroll_up_fast', 0x800, 4800), ('scroll_down_fast', 0x800, -4800),
    ('scroll_left', 0x1000, -360), ('scroll_right', 0x1000, 360),
])
def test_other_app_moves_before_each_wheel_and_stays(wheel, name, flags, delta):
    scroll, api = wheel
    assert getattr(scroll, name)(SimpleNamespace(config={}), '')
    assert api.moves == [(500, 350)]
    assert api.cursor == (500, 350)
    assert api.wheels == [((500, 350), flags, delta)]


def test_foreground_child_preserves_existing_pane(wheel):
    scroll, api = wheel
    api.cursor = (250, 500)
    assert scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == []
    assert api.wheels[0][0] == (250, 500)


def test_disabled_keeps_pointer_routing_and_configured_amount(wheel):
    scroll, api = wheel
    app = SimpleNamespace(config={'scroll': {'target_foreground': False, 'default_clicks': 2}})
    assert scroll.scroll_down(app, '')
    assert api.moves == []
    assert api.wheels == [((1800, 400), 0x800, -240)]


@pytest.mark.parametrize('button', target.MOUSE_BUTTONS)
def test_held_button_never_moves_pointer(wheel, button):
    scroll, api = wheel
    api.buttons.add(button)
    assert scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == []
    assert api.wheels[0][0] == (1800, 400)


@pytest.mark.parametrize('failure', ['cursor', 'rect', 'empty', 'screen', 'move'])
def test_targeting_failure_does_not_scroll_another_app(wheel, failure):
    scroll, api = wheel
    api.fail = failure
    assert not scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == api.wheels == []


def test_obscured_client_centre_does_not_scroll_covering_app(wheel):
    scroll, api = wheel
    api.obscured = True
    assert not scroll.scroll_right(SimpleNamespace(config={}), '')
    assert api.moves == api.wheels == []


def test_focus_change_during_targeting_cancels_wheel(wheel, monkeypatch):
    scroll, api = wheel
    foregrounds = iter([api.foreground, api.other])
    monkeypatch.setattr(api, 'GetForegroundWindow', lambda: next(foregrounds))
    assert not scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == api.wheels == []


def test_button_pressed_during_targeting_prevents_move(wheel, monkeypatch):
    scroll, api = wheel
    original = api.ClientToScreen

    def screen(hwnd, pointer):
        api.buttons.add(0x01)
        return original(hwnd, pointer)

    monkeypatch.setattr(api, 'ClientToScreen', screen)
    assert scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == []
    assert api.wheels[0][0] == (1800, 400)


@pytest.mark.parametrize('stored', [None, False, True])
def test_setting_is_compact_beside_mouse_suppression_and_round_trips(qapp, stored):
    from samsara.config_schema import SETTINGS_SCHEMA
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults

    config = _schema_defaults()
    config['scroll'] = {'slow_clicks': 7}
    if stored is not None:
        config['scroll']['target_foreground'] = stored
    window = _SettingsWindow(_StubApp(config))
    try:
        checkbox = window._widgets['scroll_target_foreground']
        assert checkbox.isChecked() is (True if stored is None else stored)
        assert SETTINGS_SCHEMA['scroll.target_foreground']['default'] is True
        row = window._rows_by_widget['scroll_target_foreground']
        assert row.page_name == 'Shortcuts'
        assert row.title == "Scroll the app you're using, not the one under the mouse"
        assert checkbox.maximumWidth() <= 64
        checkbox.setChecked(not checkbox.isChecked())
        updates = window._collect_destination_updates({})
        assert updates['scroll'] == {'slow_clicks': 7, 'target_foreground': checkbox.isChecked()}
        from samsara.ui.settings import shortcuts_page
        controls = [spec[2] for spec in shortcuts_page.ADVANCED]
        assert controls.index('scroll_target_foreground') == controls.index('cmd_tab_suppress') + 1
    finally:
        window.close()
        window.deleteLater()
