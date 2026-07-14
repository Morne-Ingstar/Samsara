"""Tests for show_numbers.py's DOM-vs-UIA routing: DOM path used when Brave
is foregrounded and the bridge is connected+fast; UIA fallback -- visibly,
not silently -- on disconnected/timeout/no-content-script; a page that
genuinely has no candidates is a handled outcome, not a fallback; UIA vs
DOM session state stays mutually exclusive; is_overlay_active() reflects
either; existing UIA "click N" behavior is unaffected when no DOM session
is active.
"""
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

for _mod in ("uiautomation", "win32api", "win32con", "win32gui", "win32process", "psutil"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

import plugins.commands.show_numbers as sn


class _FakeBridge:
    def __init__(self):
        self.started = False
        self.connected = False
        self.hints_to_return = None
        self.unavailable_reason = None
        self.selection_ok = True
        self.dismiss_calls = 0
        self.on_dismissed_cb = None
        self.selection_calls = []
        self.stop_calls = 0

    def start(self):
        self.started = True
        return True

    def stop(self, timeout=2.0):
        self.stop_calls += 1

    def is_connected(self):
        return self.connected

    def set_on_dismissed(self, cb):
        self.on_dismissed_cb = cb

    def request_hints(self, timeout=0.8):
        return self.hints_to_return

    @property
    def last_hints_unavailable_reason(self):
        return self.unavailable_reason

    def send_selection(self, number, action, timeout=0.8, modifiers=None):
        self.selection_calls.append((number, action, modifiers))
        return self.selection_ok

    def send_dismiss(self):
        self.dismiss_calls += 1


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test gets a fresh fake bridge and a clean module-level state
    slate, regardless of what a previous test left behind."""
    fake = _FakeBridge()
    monkeypatch.setattr(sn.browser_bridge, "get_bridge", lambda: fake)
    sn._bridge_started = False
    with sn._state_lock:
        sn._elements.clear()
    with sn._dom_lock:
        sn._dom_active = False
        sn._dom_hint_count = 0
    yield fake
    with sn._state_lock:
        sn._elements.clear()
    with sn._dom_lock:
        sn._dom_active = False
        sn._dom_hint_count = 0


def _app():
    return SimpleNamespace(hints=SimpleNamespace(increment=lambda *a: None))


def _speak_capture(monkeypatch):
    spoken = []
    monkeypatch.setattr(sn, "_speak", lambda app, text: spoken.append(text))
    return spoken


# ---------------------------------------------------------------------------
# DOM path preferred when applicable
# ---------------------------------------------------------------------------

def test_dom_path_used_when_brave_and_connected(monkeypatch, _clean_state):
    fake = _clean_state
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: True)
    fake.connected = True
    fake.hints_to_return = [{"index": 1, "kind": "button", "rect": {}}] * 3

    def _fail_if_called(*a, **k):
        raise AssertionError("UIA enumeration must not run on a successful DOM path")

    monkeypatch.setattr(sn, "_enumerate_foreground_clickables", _fail_if_called)
    monkeypatch.setattr(sn, "_cached_enumerate", _fail_if_called)

    assert sn.handle_show_numbers(_app(), "") is True
    with sn._dom_lock:
        assert sn._dom_active is True
        assert sn._dom_hint_count == 3
    with sn._state_lock:
        assert sn._elements == []


def test_dom_not_attempted_when_not_brave(monkeypatch, _clean_state):
    fake = _clean_state
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: False)
    monkeypatch.setattr(sn, "_cached_enumerate", lambda: [
        {"control": object(), "rect": (0, 0, 10, 10), "name": "x", "type": "ButtonControl"}
    ])
    monkeypatch.setattr(sn, "_draw_overlay", lambda app, elements, caption="": None)

    assert sn.handle_show_numbers(_app(), "") is True
    assert fake.started is False  # bridge never even lazily started


# ---------------------------------------------------------------------------
# Visible UIA fallback (not silent)
# ---------------------------------------------------------------------------

def test_uia_fallback_when_bridge_disconnected(monkeypatch, _clean_state):
    fake = _clean_state
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: True)
    fake.connected = False

    monkeypatch.setattr(sn, "_cached_enumerate", lambda: [
        {"control": object(), "rect": (0, 0, 10, 10), "name": "x", "type": "ButtonControl"}
    ])
    captured = {}
    monkeypatch.setattr(
        sn, "_draw_overlay",
        lambda app, elements, caption="": captured.update(caption=caption),
    )

    assert sn.handle_show_numbers(_app(), "") is True
    assert captured["caption"] != "", "fallback must be visible (non-empty overlay caption)"


def test_uia_fallback_on_timeout(monkeypatch, _clean_state, caplog):
    fake = _clean_state
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: True)
    fake.connected = True
    fake.hints_to_return = None
    fake.unavailable_reason = "timeout_or_disconnected"

    monkeypatch.setattr(sn, "_cached_enumerate", lambda: [
        {"control": object(), "rect": (0, 0, 10, 10), "name": "x", "type": "ButtonControl"}
    ])
    captured = {}
    monkeypatch.setattr(
        sn, "_draw_overlay",
        lambda app, elements, caption="": captured.update(caption=caption),
    )

    with caplog.at_level("INFO"):
        assert sn.handle_show_numbers(_app(), "") is True
    assert captured["caption"] != ""
    assert any("falling back to UIA" in r.getMessage() for r in caplog.records), (
        "a distinct log line must record the fallback, per the visible-not-silent requirement"
    )


def test_no_candidates_is_handled_not_a_fallback(monkeypatch, _clean_state):
    """A real, connected DOM response saying the page has no candidates
    must NOT fall back to UIA -- that would just re-show tabs/bookmarks."""
    fake = _clean_state
    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: True)
    fake.connected = True
    fake.hints_to_return = None
    fake.unavailable_reason = "no_candidates"

    spoken = _speak_capture(monkeypatch)

    def _fail_if_called(*a, **k):
        raise AssertionError("must not fall back to UIA for a genuinely-empty page")

    monkeypatch.setattr(sn, "_enumerate_foreground_clickables", _fail_if_called)
    monkeypatch.setattr(sn, "_cached_enumerate", _fail_if_called)
    monkeypatch.setattr(sn, "_draw_overlay", _fail_if_called)

    assert sn.handle_show_numbers(_app(), "") is True
    assert any("No clickable elements" in s for s in spoken)


# ---------------------------------------------------------------------------
# Mutual exclusion between UIA and DOM session state
# ---------------------------------------------------------------------------

def test_starting_dom_session_clears_uia_elements(monkeypatch, _clean_state):
    fake = _clean_state
    with sn._state_lock:
        sn._elements[:] = [object(), object()]

    monkeypatch.setattr(sn, "_is_brave_foreground", lambda hwnd: True)
    fake.connected = True
    fake.hints_to_return = [{"index": 1, "kind": "button", "rect": {}}]

    assert sn.handle_show_numbers(_app(), "") is True
    with sn._state_lock:
        assert sn._elements == []


def test_starting_uia_session_clears_dom_state(monkeypatch, _clean_state):
    fake = _clean_state
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 5

    # _draw_overlay posts to the real qt_runtime by default, which would
    # spin up the process-wide non-daemon "samsara-qt" thread and its
    # foreground-poll QTimer -- neither ever gets torn down by this test,
    # leaking a live timer that keeps polling win32gui (stubbed, possibly
    # broken by another test's monkeypatch by then) forever and blocking
    # process exit. Same no-op double already used in
    # test_destroy_overlay_completely_stops_bridge below.
    monkeypatch.setattr(
        sn, "qt_runtime", SimpleNamespace(ensure_started=lambda: None, post=lambda f: None)
    )
    sn._draw_overlay(_app(), [])  # even an empty draw should clear DOM state
    with sn._dom_lock:
        assert sn._dom_active is False
        assert sn._dom_hint_count == 0


# ---------------------------------------------------------------------------
# is_overlay_active() reflects either kind of session
# ---------------------------------------------------------------------------

def test_is_overlay_active_true_for_dom_session(_clean_state):
    with sn._dom_lock:
        sn._dom_active = True
    assert sn.is_overlay_active() is True


def test_is_overlay_active_false_when_neither(_clean_state):
    assert sn.is_overlay_active() is False


# ---------------------------------------------------------------------------
# handle_click routes to the DOM path when a DOM session is active
# ---------------------------------------------------------------------------

def test_handle_click_routes_to_dom_when_active(_clean_state):
    fake = _clean_state
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 5
    fake.selection_ok = True

    assert sn.handle_click(_app(), "3") is True
    assert fake.selection_calls == [(3, "click", {"ctrlKey": False, "shiftKey": False, "altKey": False})]
    with sn._dom_lock:
        assert sn._dom_active is False  # cleared after a successful selection


def test_handle_click_dom_out_of_range(monkeypatch, _clean_state):
    fake = _clean_state
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 2
    spoken = _speak_capture(monkeypatch)

    assert sn.handle_click(_app(), "5") is True
    assert fake.selection_calls == []
    assert any("not available" in s for s in spoken)
    with sn._dom_lock:
        assert sn._dom_active is True  # an out-of-range click leaves the session intact


def test_handle_click_dom_modifiers_mapped(_clean_state):
    fake = _clean_state
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 5

    sn.handle_click(_app(), "shift click 2 right")
    assert fake.selection_calls == [(2, "rightclick", {"ctrlKey": False, "shiftKey": True, "altKey": False})]


def test_handle_click_dom_selection_failure_keeps_session(monkeypatch, _clean_state):
    fake = _clean_state
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 5
    fake.selection_ok = False
    spoken = _speak_capture(monkeypatch)

    assert sn.handle_click(_app(), "1") is True
    assert any("no longer available" in s for s in spoken)
    with sn._dom_lock:
        assert sn._dom_active is True


# ---------------------------------------------------------------------------
# Shutdown wiring
# ---------------------------------------------------------------------------

def test_destroy_overlay_completely_stops_bridge(monkeypatch, _clean_state):
    fake = _clean_state
    monkeypatch.setattr(sn, "qt_runtime", SimpleNamespace(ensure_started=lambda: None, post=lambda f: None))
    sn._destroy_overlay_completely()
    assert fake.stop_calls == 1


def test_on_dom_dismissed_clears_state(_clean_state):
    with sn._dom_lock:
        sn._dom_active = True
        sn._dom_hint_count = 7
    sn._on_dom_dismissed()
    with sn._dom_lock:
        assert sn._dom_active is False
        assert sn._dom_hint_count == 0
