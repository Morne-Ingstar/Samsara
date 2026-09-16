"""Queue 72: show numbers enumerates the whole UIA tree.

Live measurement 2026-09-15 (reports/72): Claude desktop's page content sits
at UIA depth 16-37; the old walk stopped at depth 12 and at the 99th match
and returned 4 title-bar buttons. These tests pin the walk, the cap, the
cached-subtree path, the control-type filter against role names captured
from real Chromium windows, and that an empty result is actually spoken.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plugins.commands.show_numbers as sn  # noqa: E402

WINDOW = (0, 0, 1600, 1000)


class _Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class _Node:
    """A live-uiautomation-shaped control for the walk."""

    def __init__(self, ctype, rect=(10, 10, 60, 40), children=(), enabled=True, offscreen=False,
                 name=""):
        self.ControlTypeName = ctype
        self.BoundingRectangle = _Rect(*rect)
        self.IsEnabled = enabled
        self.IsOffscreen = offscreen
        self.Name = name
        self._children = list(children)

    def GetChildren(self):
        return list(self._children)


def _chain(depth, leaf):
    """leaf wrapped in `depth` Group levels."""
    node = leaf
    for _ in range(depth):
        node = _Node("GroupControl", rect=WINDOW, children=[node])
    return node


def _button(i, name=""):
    x = 10 + (i % 30) * 50
    y = 10 + (i // 30) * 40
    return _Node("ButtonControl", rect=(x, y, x + 40, y + 30), name=name)


@pytest.fixture
def live_walk(monkeypatch):
    """Point the enumeration at a fake tree through the live-walk path."""
    def _use(root):
        root.BoundingRectangle = _Rect(*WINDOW)
        monkeypatch.setattr(sn, "_load_uia", lambda: SimpleNamespace())
        monkeypatch.setattr(sn, "_get_foreground_control", lambda auto: root)
        return sn._enumerate_foreground_clickables
    return _use


# ---------------------------------------------------------------------------
# Depth and the cap
# ---------------------------------------------------------------------------

def test_content_deeper_than_twelve_levels_is_found(live_walk):
    """Claude desktop's shape: 4 title-bar buttons near the top, the page's
    buttons inside a document 16+ levels down. The old walk returned 4."""
    title = [_button(i, "title") for i in range(4)]
    page = [_button(10 + i, "page") for i in range(40)]
    document = _Node("DocumentControl", rect=WINDOW, children=page)
    root = _Node("WindowControl", rect=WINDOW, children=[*title, _chain(15, document)])
    found = live_walk(root)()
    assert len(found) == 44
    assert sum(1 for e in found if e["name"] == "page") == 40
    stats = sn._last_enum_stats
    assert stats["max_depth"] >= 17 and stats["in_document"] == 40 and not stats["capped"]


def test_a_tree_exceeding_the_cap_yields_elements_from_beyond_it(live_walk):
    """120 chrome buttons come first in tree order, then a deep document with
    10. The old walk stopped at the 99th chrome button and never reached the
    page; now the cap is applied after the whole walk and keeps the page."""
    chrome = [_button(i, "chrome") for i in range(120)]
    page = [_button(200 + i, "page") for i in range(10)]
    document = _Node("DocumentControl", rect=WINDOW, children=page)
    root = _Node("WindowControl", rect=WINDOW,
                 children=[_Node("ToolBarControl", rect=WINDOW, children=chrome), _chain(20, document)])
    found = live_walk(root)()
    assert len(found) == sn._ENUM_CAP
    assert sum(1 for e in found if e["name"] == "page") == 10
    assert sum(1 for e in found if e["name"] == "chrome") == sn._ENUM_CAP - 10
    stats = sn._last_enum_stats
    assert stats["capped"] and stats["candidates"] == 130 and stats["count"] == sn._ENUM_CAP


def test_capped_elements_keep_tree_order(live_walk):
    chrome = [_button(i, f"c{i}") for i in range(105)]
    page = [_button(300 + i, f"p{i}") for i in range(3)]
    root = _Node("WindowControl", rect=WINDOW,
                 children=[*chrome, _Node("DocumentControl", rect=WINDOW, children=page)])
    names = [e["name"] for e in live_walk(root)()]
    assert names[-3:] == ["p0", "p1", "p2"]
    assert names[:96] == [f"c{i}" for i in range(96)]


def test_the_walk_stops_at_its_budget_not_at_a_match_count(live_walk, monkeypatch):
    monkeypatch.setattr(sn, "_ENUM_NODE_BUDGET", 50)
    root = _Node("WindowControl", rect=WINDOW, children=[_button(i) for i in range(200)])
    found = live_walk(root)()
    assert sn._last_enum_stats["budget_hit"] == "nodes"
    assert len(found) == 49          # 50 nodes examined: the window and 49 buttons


def test_offscreen_and_disabled_controls_get_no_label(live_walk):
    root = _Node("WindowControl", rect=WINDOW, children=[
        _Node("ButtonControl", name="visible"),
        _Node("ButtonControl", name="scrolled away", offscreen=True),
        _Node("ButtonControl", name="disabled", enabled=False),
    ])
    assert [e["name"] for e in live_walk(root)()] == ["visible"]


# ---------------------------------------------------------------------------
# Cached subtree (one cross-process request)
# ---------------------------------------------------------------------------

class _CachedArray:
    def __init__(self, items):
        self._items = items
        self.Length = len(items)

    def GetElement(self, i):
        return self._items[i]


class _CachedElement:
    """IUIAutomationElement with cached properties only."""

    def __init__(self, ctype_id, rect, children=(), enabled=True, offscreen=False):
        self.CachedControlType = ctype_id
        self.CachedBoundingRectangle = SimpleNamespace(left=rect[0], top=rect[1], right=rect[2], bottom=rect[3])
        self.CachedIsEnabled = enabled
        self.CachedIsOffscreen = offscreen
        self.CachedName = ""
        self._children = list(children)
        self.CurrentControlType = ctype_id

    def GetCachedChildren(self):
        return _CachedArray(self._children) if self._children else None


BUTTON, DOCUMENT, GROUP, WINDOW_T = 50000, 50030, 50026, 50032


def _fake_auto(created):
    def _create(element):
        control = SimpleNamespace(element=element)
        created.append(control)
        return control
    return SimpleNamespace(Rect=_Rect, Control=SimpleNamespace(CreateControlFromElement=_create))


def test_cached_subtree_is_used_when_the_window_has_a_handle(monkeypatch):
    created = []
    auto = _fake_auto(created)
    deep = _CachedElement(BUTTON, (100, 100, 180, 130))
    node = _CachedElement(DOCUMENT, WINDOW, children=[deep])
    for _ in range(18):
        node = _CachedElement(GROUP, WINDOW, children=[node])
    root_el = _CachedElement(WINDOW_T, WINDOW, children=[_CachedElement(BUTTON, (5, 5, 40, 30)), node])

    fg = SimpleNamespace(NativeWindowHandle=0x5150, BoundingRectangle=_Rect(*WINDOW))
    monkeypatch.setattr(sn, "_load_uia", lambda: auto)
    monkeypatch.setattr(sn, "_get_foreground_control", lambda a: fg)
    calls = []

    def _root(a, hwnd):
        calls.append(hwnd)
        return sn._CachedNode(root_el, a)
    monkeypatch.setattr(sn, "_cached_root", _root)

    found = sn._enumerate_foreground_clickables()
    assert calls == [0x5150]
    assert sn._last_enum_stats["source"] == "uia-cache"
    assert len(found) == 2 and sn._last_enum_stats["in_document"] == 1
    # clickable entries carry LIVE controls, not cache nodes
    assert [e["control"].element for e in found] == [root_el._children[0], deep]
    assert found[1]["prect"] == (100, 100, 180, 130) and found[1]["type"] == "ButtonControl"


def test_cached_subtree_failure_falls_back_to_the_live_walk(monkeypatch):
    root = _Node("WindowControl", rect=WINDOW, children=[_Node("ButtonControl", name="live")])
    root.NativeWindowHandle = 0x77
    monkeypatch.setattr(sn, "_load_uia", lambda: SimpleNamespace())
    monkeypatch.setattr(sn, "_get_foreground_control", lambda a: root)

    def _boom(a, hwnd):
        raise OSError("COM error")
    monkeypatch.setattr(sn, "_cached_root", _boom)
    found = sn._enumerate_foreground_clickables()
    assert [e["name"] for e in found] == ["live"]
    assert sn._last_enum_stats["source"] == "uia-walk"


# ---------------------------------------------------------------------------
# Control-type filter vs role names captured from real Chromium windows
# ---------------------------------------------------------------------------

#: Role names of keyboard-focusable, enabled, on-screen elements, captured
#: live 2026-09-15 with reports/72/artifacts/probe72c.py (counts in
#: probe72c_focusable_roles.json), plus the invokable roles whose Invoke is
#: inherited from a clickable ancestor (probe72b: Obsidian 336 invokable
#: Group/Text/Image, 1 without an invokable parent, none focusable).
CHROMIUM_TARGET_ROLES = {
    "claude.exe": {"ButtonControl", "HyperlinkControl", "RadioButtonControl", "EditControl"},
    "brave.exe": {"ButtonControl", "HyperlinkControl", "TabItemControl", "DataItemControl",
                  "EditControl", "ListItemControl", "CheckBoxControl"},
    "obsidian.exe": {"ButtonControl", "EditControl"},
}
CHROMIUM_NON_TARGET_ROLES = {
    "GroupControl", "TextControl", "ImageControl", "DocumentControl", "ToolBarControl",
    "ThumbControl", "TabControl",
}


@pytest.mark.parametrize("exe", sorted(CHROMIUM_TARGET_ROLES))
def test_filter_accepts_the_roles_chromium_uses_for_real_targets(exe):
    missing = CHROMIUM_TARGET_ROLES[exe] - sn._CLICKABLE_TYPES
    assert not missing, f"{exe}: {missing}"


def test_filter_rejects_containers_and_click_ancestor_roles():
    assert not (CHROMIUM_NON_TARGET_ROLES & sn._CLICKABLE_TYPES)


# ---------------------------------------------------------------------------
# Zero elements: spoken through the real TTS length gate, and on the chip
# ---------------------------------------------------------------------------

def _speaking_app(monkeypatch):
    from samsara.tts.coordinator import AudioCoordinator

    spoken = []
    engine = SimpleNamespace(speak=lambda text, **kw: spoken.append(text) or SimpleNamespace(utterance_id="u"))
    app = SimpleNamespace(command_mode_active=True, config={"command_mode": {"tts_char_limit": 50}, "tts": {}},
                          chips=[])
    app._show_outcome_chip = lambda label, kind: app.chips.append((label, kind))
    coordinator = AudioCoordinator(app, engine, {})
    monkeypatch.setattr(coordinator, "transition_to", lambda *a, **k: None)
    app.audio_coordinator = coordinator

    def _timer(name, seconds, fn, daemon=True):
        fn()
        return SimpleNamespace(cancel=lambda: None)
    monkeypatch.setattr(sn.thread_registry, "timer", _timer)
    return app, spoken


@pytest.mark.parametrize("fg, nodes, message, chip", [
    ({"exe": "explorer.exe", "cls": "CabinetWClass", "kind": "native"}, 163,
     sn.NOTHING_SPOKEN, "nothing clickable here"),
    ({"exe": "warp.exe", "cls": "Window Class", "kind": "native"}, 7,
     sn.OPAQUE_SPOKEN, "app hides its controls"),
])
def test_zero_elements_is_heard_during_a_command_session(monkeypatch, fg, nodes, message, chip):
    app, spoken = _speaking_app(monkeypatch)
    monkeypatch.setattr(sn, "_last_enum_stats", {"nodes": nodes})
    sn._report_nothing(app, fg)
    assert spoken == [message], "suppressed by command_mode.tts_char_limit"
    assert app.chips and chip in app.chips[-1][0] and app.chips[-1][1] == "error"


def test_the_gate_would_have_dropped_the_old_53_char_message(monkeypatch):
    """Guards the test above: the old message really was silenced."""
    app, spoken = _speaking_app(monkeypatch)
    sn._speak(app, "No clickable elements found in the foreground window.")
    assert spoken == []
