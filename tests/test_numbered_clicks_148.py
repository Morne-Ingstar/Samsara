"""Queue 148: every click verb shares Show Numbers label resolution."""
import sys
import types
from types import SimpleNamespace

import pytest

from plugins.commands import show_numbers as sn
from samsara.command_registry import CommandMatcher


class _Rect:
    left, top, right, bottom = 100, 200, 140, 240
    width, height = 40, 40


class _Element:
    BoundingRectangle = _Rect()
    IsEnabled = True


@pytest.fixture(autouse=True)
def _numbers_state():
    with sn._state_lock:
        sn._elements[:] = []
    with sn._dom_lock:
        sn._dom_active = False
        sn._dom_hint_count = 0
    yield
    with sn._state_lock:
        sn._elements[:] = []


def _app(spoken):
    return SimpleNamespace(tts_engine=SimpleNamespace(speak=lambda text: spoken.append(text)))


@pytest.mark.parametrize("handler, modifier", [
    (sn.handle_click, "single"),
    (sn.handle_right_click, "right"),
    (sn.handle_double_click, "double"),
])
def test_label_click_verbs_share_label_seven_resolution(monkeypatch, handler, modifier):
    element = _Element()
    with sn._state_lock:
        sn._elements[:] = [_Element() for _ in range(6)] + [element]
    calls = []
    monkeypatch.setattr(sn, "_click_with_validation", lambda target, action, keys: calls.append((target, action)) or True)
    monkeypatch.setattr(sn, "_destroy_overlay", lambda app: None)

    assert handler(_app([]), "7") is True
    assert calls == [(element, modifier)]


@pytest.mark.parametrize("handler, modifier", [
    (sn.handle_click, "single"),
    (sn.handle_right_click, "right"),
    (sn.handle_double_click, "double"),
])
def test_bare_click_verbs_stay_at_cursor(monkeypatch, handler, modifier):
    calls = []
    monkeypatch.setattr(sn, "_click_at_cursor", lambda action: calls.append(action) or True)
    assert handler(_app([]), "") is True
    assert calls == [modifier]


def test_missing_label_refuses_without_clicking(monkeypatch):
    spoken, clicks = [], []
    with sn._state_lock:
        sn._elements[:] = [_Element(), _Element()]
    monkeypatch.setattr(sn, "_click_with_validation", lambda *args: clicks.append(args) or True)
    assert sn.handle_click(_app(spoken), "99") is True
    assert clicks == []
    assert spoken and "not available" in spoken[-1].lower()


def test_target_phrase_is_not_a_candidate_until_numbers_are_visible():
    matcher = CommandMatcher()
    matcher.load_builtins({
        phrase: {"type": "mouse", "scope": {"argument_tags": ["show_numbers.visible"]}}
        for phrase in ("click", "left click", "right click", "double click")
    })
    matcher.freeze()
    hidden = __import__("samsara.command_scope", fromlist=["MatchContext"]).MatchContext.for_app("warp.exe")
    shown = __import__("samsara.command_scope", fromlist=["MatchContext"]).MatchContext.for_app(
        "warp.exe", tags={"show_numbers.visible"})
    assert matcher.match("click 7", hidden) == (None, "")
    assert matcher.match("right click", hidden)[0].phrase == "right click"
    assert matcher.match("right click 7", shown)[0].phrase == "right click"


def test_uia_path_parks_pointer_on_target(monkeypatch):
    positions, invoked = [], []
    api = types.ModuleType("win32api")
    api.SetCursorPos = lambda point: positions.append(point)
    monkeypatch.setitem(sys.modules, "win32api", api)

    class _Clickable(_Element):
        def Click(self, **kwargs):
            invoked.append(("left", kwargs))

    assert sn._perform_click(_Clickable(), "single") is True
    assert positions == [(120, 220)]
    assert invoked == [("left", {"simulateMove": False})]
