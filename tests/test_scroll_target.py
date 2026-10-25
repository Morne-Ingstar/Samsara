"""869s: failed pointer probes still address the foreground application."""
from types import SimpleNamespace

import pytest

from samsara import scroll_target as target
from tests.test_scroll_target_790 import FakeUser32


@pytest.fixture
def wheel(monkeypatch):
    from plugins.commands import scroll
    api = FakeUser32()
    api.posts = []

    def post(hwnd, message, wparam, lparam):
        api.posts.append((hwnd, message, wparam, lparam))
        return 1

    monkeypatch.setattr(target, '_user32', lambda: api)
    monkeypatch.setattr(scroll, 'user32', api)
    monkeypatch.setattr(api, 'PostMessageW', post, raising=False)
    return scroll, api


@pytest.mark.parametrize('failure', ['overlay', 'rect', 'cursor', 'empty', 'screen', 'move', 'oserror'])
@pytest.mark.parametrize('horizontal', [False, True])
def test_probe_failure_posts_only_to_foreground_without_moving(wheel, monkeypatch, failure, horizontal):
    scroll, api = wheel
    api.fail = failure
    api.obscured = failure == 'overlay'
    if failure == 'oserror':
        def broken(hwnd, pointer):
            raise OSError('synthetic unavailable client rectangle')
        monkeypatch.setattr(api, 'GetClientRect', broken)
    command = scroll.scroll_right if horizontal else scroll.scroll_down
    assert command(SimpleNamespace(config={}), '')
    assert api.cursor == (1800, 400)
    assert api.moves == api.wheels == []
    assert len(api.posts) == 1
    hwnd, message, wparam, _ = api.posts[0]
    assert hwnd == api.foreground != api.other
    assert message == (0x020E if horizontal else 0x020A)
    delta = (wparam >> 16) & 0xffff
    delta = delta - 65536 if delta & 0x8000 else delta
    assert delta == (360 if horizontal else -960)


def test_no_foreground_sends_nothing(wheel):
    scroll, api = wheel
    api.foreground = None
    assert not scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.posts == api.moves == api.wheels == []


def test_post_rejected_is_failure_without_pointer_wheel(wheel, monkeypatch):
    scroll, api = wheel
    api.obscured = True
    monkeypatch.setattr(api, 'PostMessageW', lambda *args: 0)
    assert not scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.moves == api.wheels == []


def test_foreground_changes_during_fallback_sends_nothing(wheel, monkeypatch):
    scroll, api = wheel
    api.obscured = True
    calls = iter([api.foreground, api.foreground, api.other])
    monkeypatch.setattr(api, 'GetForegroundWindow', lambda: next(calls))
    assert not scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.posts == api.moves == api.wheels == []


def test_negative_screen_coordinates_packed_as_signed_words(wheel, monkeypatch):
    scroll, api = wheel
    api.obscured = True
    def negative(hwnd, pointer):
        pointer._obj.x, pointer._obj.y = -400, -100
        return 1
    monkeypatch.setattr(api, 'ClientToScreen', negative)
    assert scroll.scroll_down(SimpleNamespace(config={}), '')
    assert api.posts[0][3] == ((-400 & 0xffff) | ((-100 & 0xffff) << 16))
