"""Show Numbers overlay — semantic voice clicking via numbered labels.

Commands:
    "show numbers" / "show"   -- draw numbered labels on clickable UI elements.
                                 If the overlay is already showing, "show"/
                                 "show numbers" re-enumerates and redraws
                                 (folds refresh into the same word) rather
                                 than reusing a stale cached list.
    "hide numbers" / "hide"   -- dismiss the overlay
    "refresh numbers" / "refresh" -- re-enumerate and redraw without dismissing first
    "click N"                 -- left-click element N
    "click N twice"           -- double-click element N
    "click N right"           -- right-click element N
    "ctrl click N"            -- Ctrl+click element N
    "shift click N"           -- Shift+click element N
    "alt click N"             -- Alt+click element N
    "shift click N right"     -- Shift+right-click (modifiers combinable)
    "click thirty seven"      -- spoken numbers also accepted

Architecture:
    Fullscreen Qt widget (FramelessWindowHint, WA_TranslucentBackground,
    WindowTransparentForInput) renders numbered pill labels via QPainter.
    Physical mouse clicks pass through. Does not steal focus.

    Threading model:
      - UIA enumeration runs on the command-dispatch thread (COM, not Qt) and
        returns PHYSICAL rectangles only
      - Monitor mapping, active-screen choice, label placement, _elements
        numbering and the Qt window create/update all run on the Qt thread
        (_render_elements_qt via qt_runtime.post) -- queue 56 moved the
        QScreen queries off the worker thread
      - Foreground-window poll is a QTimer on the Qt main thread
      - Auto-dismiss uses threading.Timer (any thread) -> QTimer.singleShot -> Qt

DOM (browser-extension) path -- see samsara/browser_bridge.py and
browser_extension/ -- used instead of UIA whenever Brave is foregrounded and
the extension is connected: native UIA sees Brave's tabs/bookmarks and the
actual webpage as the same kind of control, so on a real page the 99-result
cap is consumed before webpage elements are ever reached. The DOM path
renders numbered labels inside the webpage itself (a content script, no Qt
window at all) and structurally cannot enumerate tabs/bookmarks, since a
content script's `document` is only ever the page's own DOM. Falls back to
the UIA path above -- visibly (a distinct log line and an overlay caption),
never silently -- when the extension isn't installed, isn't connected, or
the active tab is a restricted page.

    Threading model (DOM path):
      - browser_bridge's WS server thread and per-connection handler
        threads never touch Qt at all
      - DOM session state (_dom_active/_dom_hint_count) has its own lock
        (_dom_lock), separate from UIA's _state_lock/_elements -- only one
        of the two is ever populated at a time
      - An unsolicited in-page Escape reaches Python via a browser_bridge
        connection thread calling _on_dom_dismissed()
"""

import logging
import re
import sys
import threading
import time

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication

from samsara import browser_bridge
from samsara.command_registry import DispatchState
from samsara.plugin_commands import command
from samsara.runtime import thread_registry
from samsara.ui import mouse_grid, qt_runtime
from samsara.ui.numbers_overlay_qt import (
    NumbersOverlayWindow, PILL_ANCHOR_DX, PILL_ANCHOR_DY, _COORD_DEBUG, _map_physical_to_qt,
    _ensure_dpi_thread_context, _with_physical_dpi_context, current_monitor_mappings, pill_rect,
    screen_for_hwnd,
)

logger = logging.getLogger(__name__)

_uia_mod = None
_UIA_AVAILABLE = None  # None = not yet attempted; True/False = result

# ---------------------------------------------------------------------------
# Module-level state  (one overlay at a time)
# ---------------------------------------------------------------------------

_state_lock     = threading.RLock()
_elements: list = []                             # index 0 -> label "1"
_dismiss_timer: "threading.Timer | None" = None
_overlay_window: "NumbersOverlayWindow | None" = None   # Qt thread only
_fg_timer: "QTimer | None" = None                       # Qt thread only
_fg_hwnd_at_show: int = 0                               # HWND of target window
_overlay_hwnd:    int = 0                               # HWND of overlay itself
_overlay_screen_name: str = ''                          # Qt thread only

_enum_cache: "dict | None" = None   # {'hwnd': int, 't': float, 'elements': list}
_CACHE_TTL      = 10.0   # seconds
_AUTO_DISMISS_S = 30
_FG_POLL_MS     = 2000

# ---------------------------------------------------------------------------
# DOM (browser-extension) session state -- deliberately a SEPARATE lock and
# variable set from _state_lock/_elements above, not a repurposing of them.
# A DOM "element" is a (requestId, number) pair to relay over the bridge, not
# a UIA control object with .Click()/.BoundingRectangle -- mixing the two
# into one list would require every UIA-only call site to branch anyway.
# Only one of _elements / _dom_active is ever populated at a time: starting
# one session clears the other (see _try_show_dom_numbers / _draw_overlay).
# Reachable from three different thread identities: the plugin-command
# worker thread (handle_show_numbers/handle_click), the Qt thread (never --
# the DOM path renders entirely in-page, no Qt object touches this lock),
# and a browser_bridge connection-handler thread (an unsolicited "dismissed"
# from the content script's in-page Escape listener, via the on_dismissed
# callback below).
# ---------------------------------------------------------------------------
_dom_lock       = threading.RLock()
_dom_active     = False
_dom_hint_count = 0
_BRAVE_PROCESS_NAMES = frozenset({'brave.exe'})
_bridge_started = False

# ---------------------------------------------------------------------------
# Pill geometry
# ---------------------------------------------------------------------------

_PILL_H = 22

def _pill_w(text: str) -> int:
    return 28 if len(text) == 1 else 36

# ---------------------------------------------------------------------------
# TTS helper (mirrors alarm_commands._speak)
# ---------------------------------------------------------------------------

def _speak(app, text: str) -> None:
    # Messages spoken from this plugin are kept at or under 50 characters:
    # command_mode.tts_char_limit (default 50) silently drops longer
    # agent_response speech during a command session (queue 56 -- the old
    # 53-char "no clickable elements" message was never heard there).
    if hasattr(app, "audio_coordinator") and app.audio_coordinator:
        app.audio_coordinator.speak(text, category="agent_response", interruptible=False)
    elif hasattr(app, "tts_engine") and app.tts_engine:
        app.tts_engine.speak(text)
    else:
        logger.info("[SHOW_NUMBERS] (no TTS) %s", text)


# ---------------------------------------------------------------------------
# Honest reporting + trace (queue 56)
# ---------------------------------------------------------------------------
#
# 2026-09-14: the owner believed "show numbers" ran and drew nothing, and the
# log had no record either way. Every invocation now logs one line per stage
# (foreground target, enumeration result, plan geometry, window shown), and a
# result with nothing to label says so -- spoken, on the chip, and as a
# FAILED dispatch -- instead of returning success.

NOTHING_SPOKEN = "Nothing clickable found in this window."
OPAQUE_SPOKEN = "This app hides its buttons from Samsara."
OPAQUE_MAX_ELEMENTS = 4      # at or below this, check whether the app is opaque to UIA
OPAQUE_MAX_NODES = 20        # a real UI tree has far more nodes than this
_CHIP_DELAY_S = 0.35         # after the dispatch's own chip, which it replaces
CHIP_CROSS = chr(0x2717)

_last_enum_stats: dict = {}


def _foreground_info(hwnd: int) -> dict:
    """exe, window class, kind ('chromium' for Chrome_WidgetWin_* -- Chromium
    browsers and Electron apps -- else 'native'), physical rect, topmost."""
    info = {"hwnd": hwnd, "exe": "?", "cls": "?", "kind": "native", "rect": None, "topmost": None}
    if not hwnd:
        return info
    try:
        import win32gui
        info["cls"] = win32gui.GetClassName(hwnd)
        info["rect"] = tuple(win32gui.GetWindowRect(hwnd))
        GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x8
        info["topmost"] = bool(win32gui.GetWindowLong(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception as e:
        logger.debug(f"_foreground_info: {e}")
    try:
        import psutil
        import win32process
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        info["exe"] = psutil.Process(pid).name().lower()
    except Exception as e:
        logger.debug(f"_foreground_info exe: {e}")
    if str(info["cls"]).startswith("Chrome_WidgetWin"):
        info["kind"] = "chromium"
    return info


def _looks_opaque(kind: str, count: int, nodes: "int | None") -> bool:
    """True when the app is not exposing its controls to UI Automation, as
    opposed to genuinely having few. Measured 2026-09-15: Claude desktop
    (Electron) 4 title-bar buttons of 44 nodes; Warp 1 of 7 nodes; Explorer
    99+ of 163; Brave 99+ of 1280."""
    if count > OPAQUE_MAX_ELEMENTS:
        return False
    return kind == "chromium" or (nodes is not None and nodes < OPAQUE_MAX_NODES)


def _report_chip(app, label: str, kind: str, delay_s: float = _CHIP_DELAY_S) -> None:
    """Show a descriptive chip shortly AFTER the dispatch's own outcome chip
    (which the plugin cannot give a reason to: adapt_handler_return keeps only
    the state), so the more specific one is what stays on screen."""
    show = getattr(app, "_show_outcome_chip", None)
    if not callable(show):
        return

    def _fire():
        try:
            show(label, kind)
        except Exception as e:
            logger.debug(f"_report_chip: {e}")

    try:
        thread_registry.timer("show_numbers.report_chip", delay_s, _fire, daemon=True)
    except Exception as e:
        logger.debug(f"_report_chip schedule: {e}")


def _report_nothing(app, fg: dict, where: str = "window") -> None:
    nodes = _last_enum_stats.get("nodes")
    opaque = fg.get("kind") == "chromium" or (nodes is not None and nodes < OPAQUE_MAX_NODES)
    if opaque:
        _speak(app, OPAQUE_SPOKEN)
        _report_chip(app, f"{CHIP_CROSS} app hides its controls", "error")
    else:
        _speak(app, NOTHING_SPOKEN)
        _report_chip(app, f"{CHIP_CROSS} nothing clickable here", "error")
    logger.warning(
        "[SHOW_NUMBERS] nothing to label in this %s: exe=%s class=%s kind=%s stats=%s -- %s",
        where, fg.get("exe"), fg.get("cls"), fg.get("kind"), _last_enum_stats,
        "the app exposes no controls to UI Automation" if opaque else "no clickable controls found",
    )

# ---------------------------------------------------------------------------
# Clickable types accepted by the filter
# ---------------------------------------------------------------------------

_CLICKABLE_TYPES = frozenset({
    'ButtonControl',
    'HyperlinkControl',
    'MenuItemControl',
    'TabItemControl',
    'CheckBoxControl',
    'RadioButtonControl',
    'ListItemControl',
    'ComboBoxControl',
    'EditControl',
    'SplitButtonControl',
    'TreeItemControl',
    # Queue 72: the one focusable, selectable role Chromium exposed live that
    # the list lacked (grid rows / cells; Brave, 2026-09-15). Group, Text and
    # Image stay out: Chromium gives them Invoke when any ANCESTOR has a click
    # handler (Obsidian: 336 invokable, 1 without an invokable parent, none
    # focusable), so they would number the same target many times over.
    'DataItemControl',
})

# ---------------------------------------------------------------------------
# UIA helpers
# ---------------------------------------------------------------------------

def _rect_intersects(uia_rect, screen_rect) -> bool:
    """True if uia_rect overlaps the (l, t, r, b) screen_rect tuple."""
    l2, t2, r2, b2 = screen_rect
    return not (
        uia_rect.right <= l2
        or uia_rect.left >= r2
        or uia_rect.bottom <= t2
        or uia_rect.top >= b2
    )


def _is_useful_clickable(control, parent_rect_screen) -> bool:
    """True if control deserves a numbered label."""
    if control.ControlTypeName not in _CLICKABLE_TYPES:
        return False

    rect = control.BoundingRectangle
    w, h = rect.width(), rect.height()

    if w < 8 or h < 8:
        return False
    if w > 1500 or h > 1000:
        return False
    if not _rect_intersects(rect, parent_rect_screen):
        return False
    try:
        if not control.IsEnabled:
            return False
    except Exception as e:
        logger.debug(f"_is_useful_clickable: {e}")
    try:
        # Scrolled out of view or clipped away: its rectangle can still fall
        # inside the window, which would put a label on nothing (queue 72).
        if getattr(control, 'IsOffscreen', False) is True:
            return False
    except Exception as e:
        logger.debug(f"_is_useful_clickable offscreen: {e}")
    return True


def _get_foreground_control(auto):
    """Return the foreground UIA control across uiautomation API versions."""
    getter = getattr(auto, 'GetForegroundControl', None)
    if callable(getter):
        return getter()

    hwnd_getter = getattr(auto, 'GetForegroundWindow', None)
    hwnd = hwnd_getter() if callable(hwnd_getter) else 0
    if not hwnd:
        try:
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
        except (ImportError, AttributeError):
            hwnd = 0

    from_handle = getattr(auto, 'ControlFromHandle', None)
    if hwnd and callable(from_handle):
        return from_handle(hwnd)
    logger.warning(
        "[OVERLAY] uiautomation has no foreground-control API; "
        "falling back to Win32 enumeration"
    )
    return None


# ---------------------------------------------------------------------------
# DOM (browser-extension) Show Numbers path
# ---------------------------------------------------------------------------

def _is_brave_foreground(fg_hwnd: int) -> bool:
    """True if the foreground window belongs to a Brave process."""
    if not fg_hwnd:
        return False
    try:
        import win32process
        import psutil
        _, pid = win32process.GetWindowThreadProcessId(fg_hwnd)
        name = psutil.Process(pid).name().lower()
        return name in _BRAVE_PROCESS_NAMES
    except Exception as e:
        logger.debug(f"_is_brave_foreground: {e}")
        return False


def _on_dom_dismissed() -> None:
    """Called from a browser_bridge connection-handler thread when the
    extension reports an unsolicited dismissal (in-page Escape). Clears
    Python-side DOM session state so a stale 'click N' after Escape fails
    gracefully instead of addressing a session that no longer exists in
    the page."""
    global _dom_active, _dom_hint_count
    with _dom_lock:
        _dom_active = False
        _dom_hint_count = 0
    logger.info("[OVERLAY] DOM overlay dismissed (in-page Escape)")


def _ensure_bridge_started() -> "browser_bridge.BrowserBridge | None":
    """Lazily starts the local bridge server on first use (not at import
    time) -- most Samsara sessions never touch Brave, so there's no reason
    to bind a loopback port until "show numbers" is actually tried while
    Brave is foregrounded. Idempotent; returns None if the bridge failed
    to bind (port taken) so callers fall back to UIA for the session."""
    global _bridge_started
    bridge = browser_bridge.get_bridge()
    if not _bridge_started:
        bridge.set_on_dismissed(_on_dom_dismissed)
        if not bridge.start():
            return None
        _bridge_started = True
    return bridge


def _try_show_dom_numbers(app):
    """Attempts the DOM (browser-extension) Show Numbers path.

    Returns (handled, fallback_reason):
      - (True, None)   -- DOM overlay shown (or page legitimately has no
                           candidates -- both are a fully-handled outcome,
                           not a fallback).
      - (False, None)  -- not applicable (Brave isn't foregrounded); caller
                           should silently use UIA as always.
      - (False, reason)-- Brave IS foregrounded but the DOM path failed
                           (bridge down/timeout/no content script); caller
                           should fall back to UIA AND surface this
                           visibly (distinct log line + overlay caption),
                           per the "never silently do nothing" requirement.
    """
    global _dom_active, _dom_hint_count
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    if not _is_brave_foreground(fg_hwnd):
        return False, None

    bridge = _ensure_bridge_started()
    if bridge is None:
        return False, "bridge_bind_failed"

    if not bridge.is_connected():
        return False, "extension_not_connected"

    hints = bridge.request_hints(timeout=0.8)
    if hints is None:
        reason = bridge.last_hints_unavailable_reason or "unknown"
        if reason == "no_candidates":
            # A real, connected DOM response: the page genuinely has no
            # actionable elements. This is a legitimate outcome, not a
            # failure -- do NOT fall back to UIA (that would just show
            # browser tabs/bookmarks again, the exact problem this feature
            # exists to avoid).
            _speak(app, "No clickable elements found on this page.")
            return True, None
        return False, reason

    with _state_lock:
        _elements.clear()
    with _dom_lock:
        _dom_active = True
        _dom_hint_count = len(hints)

    _cancel_dismiss_timer()
    _schedule_dismiss_timer(app)
    logger.info(f"[OVERLAY] DOM overlay active -- {len(hints)} numbered clickables (webpage)")
    if hasattr(app, 'hints'):
        app.hints.increment('show_numbers_used')
    return True, None


# Labels are spoken 1-99 (_parse_spoken_number), so at most 99 are shown.
_ENUM_CAP = 99
# Queue 72. The walk used to stop at depth 12 and at the 99th match. Measured
# live 2026-09-15: Claude desktop's page content sits at UIA depth 16-37 (569
# nodes, max depth 38), so the depth-12 walk saw 30 nodes and 4 title-bar
# buttons while the same filter accepts 92 elements over the whole tree; Brave
# had 46 page controls past depth 12. The walk now covers the whole subtree,
# bounded only by these budgets, and the cap is applied AFTER collection
# (_apply_cap) so content deep in the tree is never starved by chrome found
# first. The depth bound is a runaway guard, not a filter.
_ENUM_MAX_DEPTH = 64
_ENUM_NODE_BUDGET = 8000
_ENUM_TIME_BUDGET_S = 3.0

# UIA property ids fetched in the one cached subtree request.
_UIA_PROPERTY_IDS = (
    30003,  # ControlType
    30001,  # BoundingRectangle
    30010,  # IsEnabled
    30022,  # IsOffscreen
    30005,  # Name
)
_TREE_SCOPE_SUBTREE = 7
_AUTOMATION_ELEMENT_MODE_FULL = 1


def _load_uia():
    """The uiautomation module, or None (logged once) when not installed."""
    global _uia_mod, _UIA_AVAILABLE
    if _UIA_AVAILABLE is None:
        try:
            import uiautomation as _m
            _uia_mod = _m
            _UIA_AVAILABLE = True
        except ImportError:
            _UIA_AVAILABLE = False
            logger.warning("[SHOW_NUMBERS] uiautomation not available -- win32 fallback active")
    return _uia_mod if _UIA_AVAILABLE else None


class _CachedNode:
    """One element of a cached UIA subtree, answering the attributes the walk
    reads (ControlTypeName, BoundingRectangle, IsEnabled, IsOffscreen, Name,
    GetChildren) from the cache, with no cross-process call per node. The
    element is fetched in Full mode, so control() returns a live uiautomation
    Control for clicking."""

    __slots__ = ("_el", "_auto")

    def __init__(self, element, auto):
        self._el = element
        self._auto = auto

    @property
    def ControlTypeName(self) -> str:
        from uiautomation import uiautomation as _uu  # noqa: PLC0415
        return _uu.ControlTypeNames.get(self._el.CachedControlType, "")

    @property
    def BoundingRectangle(self):
        r = self._el.CachedBoundingRectangle
        return self._auto.Rect(r.left, r.top, r.right, r.bottom)

    @property
    def IsEnabled(self) -> bool:
        return bool(self._el.CachedIsEnabled)

    @property
    def IsOffscreen(self) -> bool:
        return bool(self._el.CachedIsOffscreen)

    @property
    def Name(self) -> str:
        return self._el.CachedName or ""

    def GetChildren(self) -> list:
        kids = self._el.GetCachedChildren()
        if not kids:
            return []
        return [_CachedNode(kids.GetElement(i), self._auto) for i in range(kids.Length)]

    def control(self):
        return self._auto.Control.CreateControlFromElement(self._el)


def _cached_root(auto, hwnd: int):
    """The window's whole raw-view subtree in ONE cross-process request
    (ElementFromHandleBuildCache, TreeScope_Subtree) -- what UIA clients use
    instead of a walker call per node. Measured 2026-09-15: Claude desktop
    0.08 s against 0.54 s for the per-node walk, Obsidian 0.25 s against
    1.63 s, the same nodes."""
    from uiautomation import uiautomation as _uu  # noqa: PLC0415
    uia = _uu._AutomationClient.instance().IUIAutomation
    cache = uia.CreateCacheRequest()
    for pid in _UIA_PROPERTY_IDS:
        cache.AddProperty(pid)
    cache.TreeScope = _TREE_SCOPE_SUBTREE
    cache.TreeFilter = uia.RawViewCondition
    cache.AutomationElementMode = _AUTOMATION_ELEMENT_MODE_FULL
    element = uia.ElementFromHandleBuildCache(hwnd, cache)
    if not element:
        return None
    return _CachedNode(element, auto)


def _collect_clickables(root, fg_screen) -> tuple:
    """Pre-order walk of `root`'s whole subtree (any object with the
    uiautomation Control attributes). Returns (candidates, stats). Nothing
    stops at a match count; the walk ends only at the node/time budgets or
    the runaway depth guard. Each candidate records whether it lies inside a
    DocumentControl (web/app content, as opposed to window chrome)."""
    started = time.perf_counter()
    candidates = []
    nodes = 0
    max_depth = 0
    budget_hit = None
    stack = [(root, 0, False)]
    while stack:
        if nodes >= _ENUM_NODE_BUDGET:
            budget_hit = "nodes"
            break
        if time.perf_counter() - started > _ENUM_TIME_BUDGET_S:
            budget_hit = "time"
            break
        ctrl, depth, in_document = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        ctype = ""
        try:
            ctype = ctrl.ControlTypeName
            if _is_useful_clickable(ctrl, fg_screen):
                r = ctrl.BoundingRectangle
                if _COORD_DEBUG and len(candidates) < 5:
                    logger.debug(
                        "[DPI-COORD] elem %d raw UIA: left=%d top=%d right=%d bottom=%d (%s '%s')",
                        len(candidates) + 1, r.left, r.top, r.right, r.bottom,
                        ctype, (ctrl.Name or '')[:30],
                    )
                prect = (r.left, r.top, r.right, r.bottom)
                candidates.append({
                    'control': ctrl,
                    'prect': prect,
                    'rect': prect,
                    'name': ctrl.Name or '',
                    'type': ctype,
                    'in_document': in_document,
                })
        except Exception as e:
            logger.debug(f"_collect_clickables: {e}")
        if depth >= _ENUM_MAX_DEPTH:
            continue
        try:
            children = ctrl.GetChildren()
        except Exception as e:
            logger.debug(f"_collect_clickables children: {e}")
            children = []
        child_in_document = in_document or ctype == 'DocumentControl'
        for child in reversed(children):
            stack.append((child, depth + 1, child_in_document))
    stats = {
        "nodes": nodes, "max_depth": max_depth, "candidates": len(candidates),
        "in_document": sum(1 for c in candidates if c['in_document']),
        "budget_hit": budget_hit,
        "ms": int((time.perf_counter() - started) * 1000),
    }
    return candidates, stats


def _apply_cap(candidates: list, cap: int = _ENUM_CAP) -> list:
    """At most `cap` candidates, in their original (tree) order. When there
    are more, content inside a document is kept before window chrome, so a
    browser's tabs and toolbars cannot use up the labels before the page."""
    if len(candidates) <= cap:
        return list(candidates)
    content = [c for c in candidates if c.get('in_document')]
    chrome = [c for c in candidates if not c.get('in_document')]
    keep = {id(c) for c in (content + chrome)[:cap]}
    return [c for c in candidates if id(c) in keep]


def _enumerate_foreground_clickables() -> list:
    """Walk the foreground window's UIA subtree; return useful clickables.

    Safe to call from a worker thread -- UIA is COM, not Qt. Rectangles are
    returned in UIA's PHYSICAL pixels ('prect'); conversion to Qt logical
    coordinates happens on the Qt thread in plan_overlay (queue 56: the
    conversion used to query QApplication.screens() from this worker thread).
    Records _last_enum_stats for the trace log.

    The subtree comes from one cached request (_cached_root); if that is
    unavailable the same walk runs over live controls (GetChildren).
    """
    global _last_enum_stats
    auto = _load_uia()
    if auto is None:
        return _enumerate_win32_fallback()

    fg = _get_foreground_control(auto)
    if fg is None:
        return _enumerate_win32_fallback()

    started = time.perf_counter()
    fg_rect = fg.BoundingRectangle
    fg_screen = (fg_rect.left, fg_rect.top, fg_rect.right, fg_rect.bottom)

    root, source, fetch_ms = None, "uia-walk", 0
    hwnd = getattr(fg, 'NativeWindowHandle', 0)
    if isinstance(hwnd, int) and hwnd:
        try:
            t0 = time.perf_counter()
            root = _cached_root(auto, hwnd)
            fetch_ms = int((time.perf_counter() - t0) * 1000)
            source = "uia-cache"
        except Exception as e:
            logger.info("[SHOW_NUMBERS] cached UIA subtree unavailable (%s) -- walking live controls", e)
            root = None
    if root is None:
        root, source = fg, "uia-walk"

    candidates, walk = _collect_clickables(root, fg_screen)
    results = _apply_cap(candidates)
    for e in results:
        if isinstance(e['control'], _CachedNode):
            try:
                e['control'] = e['control'].control()
            except Exception as exc:
                logger.debug(f"_enumerate_foreground_clickables control: {exc}")
    results = [e for e in results if e['control'] is not None]
    _last_enum_stats = {
        "source": source, "count": len(results), "nodes": walk["nodes"],
        "max_depth": walk["max_depth"], "candidates": walk["candidates"],
        "in_document": walk["in_document"], "capped": walk["candidates"] > _ENUM_CAP,
        "budget_hit": walk["budget_hit"], "fetch_ms": fetch_ms,
        "ms": int((time.perf_counter() - started) * 1000),
    }
    return results

# ---------------------------------------------------------------------------
# Win32 fallback (when uiautomation not installed)
# ---------------------------------------------------------------------------

class _Win32Rect:
    def __init__(self, l, t, r, b):
        self.left, self.top, self.right, self.bottom = l, t, r, b

    def width(self):  return self.right - self.left
    def height(self): return self.bottom - self.top


class _Win32Control:
    def __init__(self, hwnd, rect):
        self._hwnd = hwnd
        l, t, r, b = rect
        self.BoundingRectangle = _Win32Rect(l, t, r, b)
        self.IsEnabled = True

    def _center(self):
        br = self.BoundingRectangle
        return (br.left + br.right) // 2, (br.top + br.bottom) // 2

    def Click(self, simulateMove=True):
        self._send(left=True, double=False)

    def DoubleClick(self, simulateMove=True):
        self._send(left=True, double=True)

    def RightClick(self, simulateMove=True):
        self._send(left=False, double=False)

    def _send(self, left: bool, double: bool):
        import time as _time
        import win32api, win32con
        x, y = self._center()
        win32api.SetCursorPos((x, y))
        if left:
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            if double:
                _time.sleep(0.05)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        else:
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)


def _enumerate_win32_fallback() -> list:
    global _last_enum_stats
    _last_enum_stats = {"source": "win32", "count": 0, "nodes": None, "capped": False, "ms": 0}
    try:
        import win32gui
    except ImportError:
        logger.error("[OVERLAY] Neither uiautomation nor win32gui available")
        return []

    try:
        fg_hwnd = win32gui.GetForegroundWindow()
    except AttributeError:
        logger.error("[OVERLAY] win32gui foreground-window API unavailable")
        return []
    if not fg_hwnd:
        return []

    _CLASSES = {'button', 'edit', 'combobox', 'listbox', 'syslink',
                'syslistview32', 'systreeview32', 'systabcontrol32'}
    results = []

    def _cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        cls = win32gui.GetClassName(hwnd).lower()
        if any(c in cls for c in _CLASSES):
            try:
                rect = win32gui.GetWindowRect(hwnd)
                l, t, r, b = rect
                if r - l > 0 and b - t > 0:
                    results.append({
                        'control': _Win32Control(hwnd, rect),
                        'prect': (l, t, r, b),
                        'rect': (l, t, r, b),
                        'name': win32gui.GetWindowText(hwnd),
                        'type': cls,
                    })
            except Exception as e:
                logger.debug(f"_cb: {e}")
        return True

    try:
        win32gui.EnumChildWindows(fg_hwnd, _cb, None)
    except Exception as e:
        logger.error("[OVERLAY] EnumChildWindows failed: %s", e)
    _last_enum_stats = {"source": "win32", "count": len(results), "nodes": None,
                        "capped": False, "ms": 0}
    return results

# ---------------------------------------------------------------------------
# Element enumeration cache
# ---------------------------------------------------------------------------

def _cached_enumerate() -> list:
    global _enum_cache, _last_enum_stats
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
    except Exception:
        hwnd = 0

    now = time.time()
    if (
        _enum_cache is not None
        and _enum_cache['hwnd'] == hwnd
        and now - _enum_cache['t'] < _CACHE_TTL
    ):
        _last_enum_stats = dict(_enum_cache.get('stats') or {}, source="cache")
        logger.info("[SHOW_NUMBERS] using cached element list (%d, %.1f s old)",
                    len(_enum_cache['elements']), now - _enum_cache['t'])
        return list(_enum_cache['elements'])

    elements = _enumerate_foreground_clickables()
    if hwnd and elements:
        _enum_cache = {'hwnd': hwnd, 't': now, 'elements': elements, 'stats': dict(_last_enum_stats)}
    return elements


def _invalidate_cache() -> None:
    global _enum_cache
    _enum_cache = None

# ---------------------------------------------------------------------------
# Collision avoidance
# ---------------------------------------------------------------------------

def _place_labels(elements: list) -> list:
    """Compute pill positions with downward nudging to avoid overlaps.

    Returns list of [screen_x, screen_y, pill_w, pill_h, text].
    """
    placed = []
    for i, e in enumerate(elements, 1):
        rx, ry = e['rect'][0], e['rect'][1]
        text   = str(i)
        pw, ph = _pill_w(text), _PILL_H

        for step in range(20):
            cy = ry + step * (ph + 2)
            collision = any(
                rx < px + ppw and rx + pw > px and cy < py + pph and cy + ph > py
                for px, py, ppw, pph, _ in placed
            )
            if not collision:
                placed.append([rx, cy, pw, ph, text])
                break
        else:
            placed.append([rx, ry, pw, ph, text])   # accept overlap after 20 attempts
    return placed


def _rect_center_in(rect, bounds) -> bool:
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    return bounds[0] <= cx < bounds[2] and bounds[1] <= cy < bounds[3]


def plan_overlay(elements: list, window_rect_phys, mappings: list) -> dict:
    """Pure geometry plan for one overlay (queue 56). Qt-free; tested with a
    mocked monitor layout.

    elements: enumerated dicts. 'prect' (UIA physical pixels) is converted
    through the monitor containing it; an element without 'prect' carries an
    already-logical 'rect'.
    window_rect_phys: the target window's GetWindowRect (physical), or None.
    mappings: [(physical_rect, qt_rect, dpr)] -- current_monitor_mappings()
    minus the QScreen; [] means identity.

    The overlay covers ONE screen: the one containing the target window's
    centre (else the one it overlaps most). Kept elements have their centre
    on that screen AND inside the target window. Pills are clamped inside the
    target window intersected with that screen (`bounds`), so none can land
    on another window or monitor.

    Returns {screen_index, screen_rect, window_rect, bounds, kept, labels,
    dropped_other_screen, dropped_outside_window, clamped}."""
    def to_logical(r):
        if not mappings:
            return tuple(r)
        x1, y1 = _map_physical_to_qt(r[0], r[1], mappings)
        x2, y2 = _map_physical_to_qt(r[2], r[3], mappings)
        return (x1, y1, x2, y2)

    screen_index = 0
    if mappings and window_rect_phys:
        cx = (window_rect_phys[0] + window_rect_phys[2]) // 2
        cy = (window_rect_phys[1] + window_rect_phys[3]) // 2
        best, best_area = None, -1
        for i, (prect, _q, _r) in enumerate(mappings):
            if prect[0] <= cx < prect[2] and prect[1] <= cy < prect[3]:
                best = i
                break
            w = min(prect[2], window_rect_phys[2]) - max(prect[0], window_rect_phys[0])
            h = min(prect[3], window_rect_phys[3]) - max(prect[1], window_rect_phys[1])
            area = max(0, w) * max(0, h)
            if area > best_area:
                best, best_area = i, area
        screen_index = best or 0

    if mappings:
        screen_rect = tuple(mappings[screen_index][1])
    else:
        screen_rect = None

    # Corners can sit on different monitors (or a maximised window's -8 px
    # border just off-screen); the intersection with screen_rect below bounds it.
    window_rect = to_logical(window_rect_phys) if window_rect_phys else None

    if screen_rect is None:
        screen_rect = window_rect or (-10**6, -10**6, 10**6, 10**6)

    if window_rect is not None:
        bounds = (max(screen_rect[0], window_rect[0]), max(screen_rect[1], window_rect[1]),
                  min(screen_rect[2], window_rect[2]), min(screen_rect[3], window_rect[3]))
        if bounds[2] - bounds[0] < 40 or bounds[3] - bounds[1] < 30:
            bounds = screen_rect
    else:
        bounds = screen_rect

    kept, dropped_screen, dropped_window = [], 0, 0
    for e in elements:
        rect = to_logical(e['prect']) if e.get('prect') is not None else tuple(e['rect'])
        if not _rect_center_in(rect, screen_rect):
            dropped_screen += 1
            continue
        if window_rect is not None and not _rect_center_in(rect, window_rect):
            dropped_window += 1
            continue
        kept.append(dict(e, rect=rect))

    labels = _place_labels(kept)
    clamped = 0
    for sx, sy, pw, ph, _text in labels:
        x, y, _w, _h = pill_rect(sx, sy, pw, ph, bounds)
        if (x, y) != (sx - pw + PILL_ANCHOR_DX, sy - ph + PILL_ANCHOR_DY):
            clamped += 1
    return {
        "screen_index": screen_index, "screen_rect": screen_rect, "window_rect": window_rect,
        "bounds": bounds, "kept": kept, "labels": labels,
        "dropped_other_screen": dropped_screen, "dropped_outside_window": dropped_window,
        "clamped": clamped,
    }

# ---------------------------------------------------------------------------
# Click execution
# ---------------------------------------------------------------------------

_KEY_VK_MAP = {'shift': 0x10, 'ctrl': 0x11, 'alt': 0x12}  # VK_SHIFT/CONTROL/MENU


def _apply_modifier_keys(keys: frozenset, fn) -> None:
    """Hold keyboard modifier keys, call fn(), release them. No-op if keys empty."""
    if not keys:
        fn()
        return
    import win32api
    KEYEVENTF_KEYUP = 0x0002
    pressed = [_KEY_VK_MAP[k] for k in sorted(keys) if k in _KEY_VK_MAP]
    for vk in pressed:
        win32api.keybd_event(vk, 0, 0, 0)
    try:
        fn()
    finally:
        for vk in reversed(pressed):
            win32api.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _click_with_validation(element, modifier: str, keys: frozenset = frozenset()) -> bool:
    """Validate element is still alive, then click. Returns True on success."""
    try:
        rect = element.BoundingRectangle
        if rect.width() <= 0 or rect.height() <= 0:
            logger.warning("[OVERLAY] Element no longer valid -- UI changed")
            return False
        if not element.IsEnabled:
            logger.warning("[OVERLAY] Element disabled -- UI changed")
            return False
    except Exception:
        logger.warning("[OVERLAY] Element handle stale -- UI changed")
        return False

    return _perform_click(element, modifier, keys)


def _perform_click(element, modifier: str, keys: frozenset = frozenset()) -> bool:
    """UIA-first click, then Win32 fallback. keys holds modifier key names to hold."""
    try:
        if modifier == 'double':
            _apply_modifier_keys(keys, lambda: element.DoubleClick(simulateMove=False))
        elif modifier == 'right':
            _apply_modifier_keys(keys, lambda: element.RightClick(simulateMove=False))
        else:
            _apply_modifier_keys(keys, lambda: element.Click(simulateMove=False))
        return True
    except Exception as e:
        logger.info("[OVERLAY] UIA click failed (%s), falling back to mouse", e)

    try:
        import win32api, win32con
        rect = element.BoundingRectangle
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        # Rectangles are physical pixels; this thread is system-DPI-aware in
        # the app (pyautogui, reports/70), so place the cursor in PMv2.
        _with_physical_dpi_context(lambda: win32api.SetCursorPos((x, y)))

        def _do_mouse():
            if modifier == 'right':
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)
            else:
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                if modifier == 'double':
                    time.sleep(0.05)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

        _apply_modifier_keys(keys, _do_mouse)
        return True
    except Exception as e:
        logger.error("[OVERLAY] Win32 fallback click failed: %s", e)
        return False

# ---------------------------------------------------------------------------
# Qt overlay -- all functions below must run on the Qt main thread
# unless otherwise noted
# ---------------------------------------------------------------------------

def _render_elements_qt(app, elements: list, fg: dict, caption: str = "") -> None:
    """Plan and show the overlay for enumerated elements. Qt main thread only
    -- every QScreen/QApplication query and the window creation happen here.

    Numbering (and so _elements, which "click N" indexes) is decided by the
    plan, which drops elements on other screens / outside the target window,
    so _elements is set here, after planning."""
    fg_hwnd = fg.get("hwnd") or 0
    try:
        with_screens = current_monitor_mappings()
    except Exception as e:
        logger.warning("[SHOW_NUMBERS] monitor mapping failed (%s) -- using identity", e)
        with_screens = []
    if not with_screens:
        # Identity: one entry per Qt screen, so the plan can still pick a screen.
        with_screens = []
        for s in QApplication.screens():
            g = s.geometry()
            r = (g.x(), g.y(), g.x() + g.width(), g.y() + g.height())
            with_screens.append((r, r, 1.0, s))
    if not with_screens:
        logger.error("[SHOW_NUMBERS] no screens available -- overlay not shown")
        _report_chip(app, f"{CHIP_CROSS} numbers: no screen", "error", delay_s=0)
        return

    plan = plan_overlay(elements, fg.get("rect"), [(p, q, r) for p, q, r, _s in with_screens])
    screen = with_screens[plan["screen_index"]][3]
    logger.info(
        "[SHOW_NUMBERS] plan screen=%s screen_rect=%s dpr=%.2f window_rect=%s bounds=%s "
        "enumerated=%d kept=%d dropped_other_screen=%d dropped_outside_window=%d clamped=%d",
        screen.name(), plan["screen_rect"], screen.devicePixelRatio(), plan["window_rect"],
        plan["bounds"], len(elements), len(plan["kept"]), plan["dropped_other_screen"],
        plan["dropped_outside_window"], plan["clamped"],
    )
    if not plan["kept"]:
        with _state_lock:
            _elements.clear()
        _report_nothing(app, fg, where="window on this screen")
        return

    with _state_lock:
        _elements[:] = [e['control'] for e in plan["kept"]]
    _show_overlay_qt(plan["labels"], fg_hwnd, app, caption, bounds=plan["bounds"], screen=screen)

    count = len(plan["kept"])
    if all(e.get("type") == OCR_ELEMENT_TYPE for e in plan["kept"]):
        return   # OCR candidates (click <text>): the app's UIA tree is irrelevant
    if _looks_opaque(fg.get("kind", "native"), count, _last_enum_stats.get("nodes")):
        _speak(app, f"Only {count} found. This app hides its controls."[:50])
        _report_chip(app, f"only {count}: app hides its controls", "warning")


def _verify_overlay_shown(fg: "dict | None" = None) -> dict:
    """Read back what Windows thinks of the overlay window: visible, topmost,
    layered/transparent, rect. Qt thread only. Logged on every show."""
    result = {"hwnd": _overlay_hwnd, "visible": None, "topmost": None, "rect": None}
    if not _overlay_hwnd or sys.platform != "win32":
        return result
    try:
        import ctypes
        user32 = ctypes.windll.user32
        GWL_EXSTYLE, WS_EX_TOPMOST, WS_EX_LAYERED, WS_EX_TRANSPARENT = -20, 0x8, 0x80000, 0x20
        user32.GetWindowLongW.restype = ctypes.c_long
        ex = user32.GetWindowLongW(ctypes.c_void_p(_overlay_hwnd), GWL_EXSTYLE)
        result["visible"] = bool(user32.IsWindowVisible(ctypes.c_void_p(_overlay_hwnd)))
        result["topmost"] = bool(ex & WS_EX_TOPMOST)
        result["layered"] = bool(ex & WS_EX_LAYERED)
        result["click_through"] = bool(ex & WS_EX_TRANSPARENT)

        class _RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rc = _RECT()
        if user32.GetWindowRect(ctypes.c_void_p(_overlay_hwnd), ctypes.byref(rc)):
            result["rect"] = (rc.left, rc.top, rc.right, rc.bottom)
    except Exception as e:
        logger.debug(f"_verify_overlay_shown: {e}")
    return result


def _show_overlay_qt(labels: list, fg_hwnd: int, app, caption: str = "",
                     bounds: "tuple | None" = None, screen=None,
                     cells: "list | None" = None) -> None:
    """Create or update the overlay window. Qt main thread only.

    `cells` (queue 71) are logical cell rectangles for the mouse grid; the
    numbers overlay passes None and draws pills only."""
    global _overlay_window, _fg_timer, _fg_hwnd_at_show, _overlay_hwnd, _overlay_screen_name

    _fg_hwnd_at_show = fg_hwnd
    if not labels:
        # Never draw an empty overlay: it looks exactly like "nothing happened".
        logger.warning("[SHOW_NUMBERS] _show_overlay_qt called with no labels -- not shown")
        return

    active_screen = screen if screen is not None else screen_for_hwnd(fg_hwnd)
    if active_screen is None:
        logger.error("[SHOW_NUMBERS] no screen found for hwnd %#x -- overlay not shown", fg_hwnd or 0)
        return
    active_name = active_screen.name()

    geo = active_screen.geometry()
    logger.info(
        "[OVERLAY] render labels=%d screen=%s geo=(%d,%d %dx%d) dpr=%.2f first=%r bounds=%s",
        len(labels), active_name,
        geo.x(), geo.y(), geo.width(), geo.height(),
        active_screen.devicePixelRatio(),
        labels[0] if labels else None, bounds,
    )

    # Same screen -> reuse the existing window (no HWND recreate), regardless
    # of whether it's currently hidden. HIDE-not-destroy (see _hide_overlay_qt)
    # means a previously-dismissed overlay on the same screen is still alive
    # here, just hidden -- matching status_overlay/task_overlay/reminder_toast's
    # reuse pattern instead of leaking a fresh HWND on every show/hide cycle.
    same_screen = (
        _overlay_window is not None
        and active_name == _overlay_screen_name
    )

    if same_screen:
        if cells:
            _overlay_window.update_labels(labels, caption, bounds, cells)
        else:
            # Keep the pre-71 call shape for the numbers path (and for test
            # doubles of the overlay window, which predate the cells argument).
            _overlay_window.update_labels(labels, caption, bounds)
    else:
        if _overlay_window is not None:
            _overlay_window.hide()
        _ensure_dpi_thread_context()
        _overlay_window = NumbersOverlayWindow(labels, active_screen)
        _overlay_window._caption = caption
        if bounds is not None:
            _overlay_window.set_bounds(bounds)
        if cells:
            _overlay_window.set_cells(cells)
        _overlay_screen_name = active_name

    # Explicit show()+raise() every time (not just on first construction) --
    # matching status_overlay.show()/task_overlay.show()'s re-show path.
    # Without this, a same-screen re-show only updated label content and
    # relied on whatever z-order the window already had, so if another
    # always-on-top window (reminder toast, status/task overlay) had been
    # created or re-shown since, the numbers overlay stayed rendered but
    # invisible underneath it.
    _overlay_window.show()
    _overlay_window.raise_()

    # Capture the overlay's own HWND so _fg_poll_qt can exclude it.
    # winId() is only valid after show(); int() converts from shiboken.VoidPtr.
    try:
        _overlay_hwnd = int(_overlay_window.winId())
    except Exception:
        _overlay_hwnd = 0

    shown = _verify_overlay_shown()
    logger.info(
        "[SHOW_NUMBERS] overlay window hwnd=%#x qt_visible=%s win_visible=%s topmost=%s "
        "layered=%s click_through=%s rect=%s labels=%d",
        _overlay_hwnd or 0, _overlay_window.isVisible(), shown.get("visible"), shown.get("topmost"),
        shown.get("layered"), shown.get("click_through"), shown.get("rect"), len(labels),
    )
    if shown.get("visible") is False or not _overlay_window.isVisible():
        logger.warning("[SHOW_NUMBERS] overlay window is not visible after show()")
        _report_chip(app, f"{CHIP_CROSS} numbers overlay did not appear", "error", delay_s=0)

    # (Re-)start foreground poll
    _stop_fg_timer_qt()
    _fg_timer = QTimer()
    _fg_timer.setInterval(_FG_POLL_MS)
    _fg_timer.timeout.connect(lambda: _fg_poll_qt(app))
    _fg_timer.start()


def _hide_overlay_qt() -> None:
    """Hide the overlay window and stop the fg timer. Qt main thread only.

    HIDE-not-destroy -- same reusable-singleton pattern as status_overlay.py /
    task_overlay.py / reminder_toast.py. The window (and its HWND/screen
    identity) stays alive so the NEXT "show numbers" on the same screen can
    reuse it via _show_overlay_qt's same_screen branch instead of
    reconstructing (and leaking the previous, now-orphaned HWND) every
    single show/hide cycle.
    """
    _stop_fg_timer_qt()
    if _overlay_window is not None:
        _overlay_window.hide()
    with _state_lock:
        _elements.clear()


def _stop_fg_timer_qt() -> None:
    """Stop and release the foreground poll timer. Qt main thread only."""
    global _fg_timer
    if _fg_timer is not None:
        _fg_timer.stop()
        _fg_timer.deleteLater()
        _fg_timer = None


def _fg_poll_qt(app) -> None:
    """Periodic foreground-HWND check. Qt main thread only."""
    if _overlay_window is None or _overlay_window.isHidden():
        _stop_fg_timer_qt()
        return
    try:
        import win32gui
        cur = win32gui.GetForegroundWindow()
        # Dismiss only when focus has moved to a window that is neither the
        # original target window nor the overlay itself. The overlay can briefly
        # hold focus on some monitors when first shown; treating it as a foreign
        # window would cause a false auto-dismiss within the first poll cycle.
        if _fg_hwnd_at_show and cur != _fg_hwnd_at_show and cur != _overlay_hwnd:
            logger.info("[SHOW_NUMBERS] auto-dismissed: foreground window changed "
                        "(target=%#x overlay=%#x now=%#x)", _fg_hwnd_at_show, _overlay_hwnd, cur or 0)
            _destroy_overlay(app)
    except Exception as e:
        logger.debug(f"_fg_poll_qt: {e}")

# ---------------------------------------------------------------------------
# Auto-dismiss + overlay lifecycle (thread-safe)
# ---------------------------------------------------------------------------

def _cancel_dismiss_timer() -> None:
    global _dismiss_timer
    if _dismiss_timer is not None:
        try:
            _dismiss_timer.cancel()
        except Exception as e:
            logger.debug(f"_cancel_dismiss_timer: {e}")
        _dismiss_timer = None


def _schedule_dismiss_timer(app, seconds: int = _AUTO_DISMISS_S) -> None:
    _cancel_dismiss_timer()
    global _dismiss_timer

    def _fire():
        logger.info("[SHOW_NUMBERS] auto-dismissed after %d s", seconds)
        _destroy_overlay(app)

    t = thread_registry.timer("show_numbers.auto_dismiss", seconds, _fire, daemon=True)
    _dismiss_timer = t


def _draw_overlay(app, elements: list, caption: str = "", fg_info: "dict | None" = None) -> None:
    """Build label list and show the Qt overlay. Safe to call from any thread.

    caption: non-empty only when this UIA overlay is being shown as a
    visible fallback from a failed DOM (browser-extension) attempt -- see
    _try_show_dom_numbers -- so the user can tell the two paths apart
    instead of the fallback happening invisibly.
    """
    _clear_dom_session()  # mutual exclusion: starting a UIA session ends any DOM one
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0
    fg = fg_info if fg_info is not None else _foreground_info(fg_hwnd)
    if fg_info is None:
        fg["hwnd"] = fg_hwnd

    # Queue 56: no Qt screen queries on this (worker) thread. The active
    # monitor filter, physical->logical conversion, label placement, window
    # creation and _elements numbering all run on the Qt thread in
    # _render_elements_qt.
    elements = list(elements)

    _cancel_dismiss_timer()
    # Defensive start: mirrors ai_command_hud_qt.show_hud()/splash_qt.py's
    # own pattern. qt_runtime.post() silently drops the callback (logging a
    # warning only) whenever the runtime is not yet RUNNING -- if this is
    # ever the first Qt work requested in the process (e.g. a voice command
    # fires before/around the rest of boot, or the icon-setting block in
    # dictation.py's __init__ swallowed an exception before reaching its own
    # ensure_started() call), the overlay would silently never be built at
    # all. ensure_started() is idempotent -- a no-op when already RUNNING.
    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _render_elements_qt(app, elements, fg, caption))
    _schedule_dismiss_timer(app)
    logger.info("[SHOW_NUMBERS] %d elements posted to the Qt thread for exe=%s class=%s",
                len(elements), fg.get("exe"), fg.get("cls"))


def _clear_dom_session() -> None:
    """Clears Python-side DOM session state and asks the extension to
    drop its in-page hints too. Safe to call even if no DOM session is
    active (no-op bridge.send_dismiss() when nothing is connected)."""
    global _dom_active, _dom_hint_count
    with _dom_lock:
        was_active = _dom_active
        _dom_active = False
        _dom_hint_count = 0
    if was_active:
        browser_bridge.get_bridge().send_dismiss()


def _destroy_overlay(app=None) -> None:
    """Thread-safe dismiss: cancel timers, clear whichever session (UIA or
    DOM) is active. Always safe to call both halves -- clearing an
    inactive session is a no-op."""
    _cancel_dismiss_timer()
    _clear_dom_session()
    qt_runtime.ensure_started()
    qt_runtime.post(_hide_overlay_qt)


def _destroy_overlay_completely() -> None:
    """Full teardown -- call on app quit.

    browser_bridge.stop() is genuinely blocking (bounded, ~1.5s) and is
    called BEFORE the (fire-and-forget) Qt-post below: qt_runtime.post()
    schedules _hide_overlay_qt on the Qt thread and returns immediately
    without waiting for it, so placing the bridge shutdown after it would
    add real, easy-to-miss latency to app quit for no benefit -- the Qt
    hide is already going to happen asynchronously regardless of ordering.
    """
    browser_bridge.get_bridge().stop(timeout=1.5)
    _destroy_overlay()

# ---------------------------------------------------------------------------
# Spoken number parsing
# ---------------------------------------------------------------------------

_WORD_TO_NUM = {
    'zero': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
    'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
    'nineteen': 19, 'twenty': 20, 'thirty': 30, 'forty': 40,
    'fifty': 50, 'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
}


def _parse_spoken_number(text: str) -> "int | None":
    """Parse a spoken number 0-99 from text. Returns None if not found.

    Handles:
      - Digits:   "7", "37"
      - Words:    "seven", "twelve"
      - Compound: "thirty seven", "ninety nine"
    """
    text = text.lower().strip()
    if not text:
        return None

    m = re.search(r'\b(\d{1,2})\b', text)
    if m:
        n = int(m.group(1))
        return n if 0 <= n <= 99 else None

    total = 0
    found = False
    for token in text.split():
        if token in _WORD_TO_NUM:
            total += _WORD_TO_NUM[token]
            found = True
    return total if found and 0 <= total <= 99 else None


def _parse_click_target(text: str):
    """Legacy parser: returns (number | None, modifier_str, keys_frozenset).

    modifier is 'single' | 'double' | 'right'.
    keys is a frozenset of 'shift' | 'ctrl' | 'alt'.
    """
    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip().lower())

    keys: set = set()
    for kw in ('shift', 'ctrl', 'alt'):
        if re.search(r'\b' + kw + r'\b', text):
            keys.add(kw)
            text = re.sub(r'\b' + kw + r'\b', '', text)

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    return _parse_spoken_number(text), modifier, frozenset(keys)

# ---------------------------------------------------------------------------
# Click by on-screen text (queue 70)
# ---------------------------------------------------------------------------
#
# "click <text>" for apps that expose nothing to UI Automation (Obsidian and
# Warp measured that way against Samsara and Windows Voice Access): the
# foreground window is read with the on-device Windows OCR engine
# (samsara.screen_ocr) and the spoken text is matched against it.
#   one exact match      -> click it
#   several, or only near matches -> numbered labels on the existing overlay;
#                           the user says "click N" (never a guess)
#   none                 -> said plainly; nothing is clicked
# Icon-only buttons have no text and cannot be reached this way.

OCR_ELEMENT_TYPE = "ocr_text"
TEXT_NO_MATCH_SPOKEN = "Nothing on screen matches that."
TEXT_NO_MATCH_CHIP = f"{CHIP_CROSS} nothing on screen matches that (icons have no text)"
TEXT_READ_FAILED_SPOKEN = "Could not read the screen."
TEXT_CHANGED_SPOKEN = "The window changed. Say it again."
_OVERLAY_HIDE_WAIT_S = 0.15   # let a hidden label overlay leave the screen before capture

#: Remainders that name a keyboard key: "press enter" is not a request to
#: click the word "Enter" on screen, so these never read the screen.
_KEY_NAMES = frozenset({
    "enter", "return", "escape", "esc", "tab", "space", "spacebar", "backspace", "delete", "del",
    "insert", "home", "end", "page up", "page down", "pageup", "pagedown", "up", "down", "left",
    "arrow up", "arrow down", "arrow left", "arrow right", "up arrow", "down arrow", "left arrow",
    "right arrow", "caps lock", "windows", "win", "menu", "print screen",
    *(f"f{i}" for i in range(1, 13)),
})


def _is_label_number(text: str) -> bool:
    """True when the whole remainder is a label number ("7", "thirty seven",
    "number 7") -- the numbered-overlay path. Anything else is on-screen text."""
    from samsara.screen_ocr import token_view as tokens  # noqa: PLC0415
    toks = tokens(text)
    if len(toks) > 1 and toks[0] in ("number", "num"):
        toks = toks[1:]
    return 1 <= len(toks) <= 2 and all(t.isdigit() or t in _WORD_TO_NUM for t in toks)


class _OcrTarget:
    """One on-screen text match, clickable like a UIA control so "click N"
    and _click_with_validation treat it the same way. Rectangles are physical
    pixels; the cursor is placed inside a PMv2 thread context. IsEnabled is
    False once the window is no longer foreground or has moved, so a stale
    match is reported instead of clicked."""

    def __init__(self, rect: tuple, hwnd: int, window_rect: tuple, text: str):
        self.BoundingRectangle = _Win32Rect(*rect)
        self.Name = text
        self._hwnd = hwnd
        self._window_rect = tuple(window_rect) if window_rect else None

    @property
    def IsEnabled(self) -> bool:
        from samsara import screen_ocr  # noqa: PLC0415
        return (screen_ocr.foreground_hwnd() == self._hwnd
                and screen_ocr.window_frame_rect(self._hwnd) == self._window_rect)

    def Click(self, simulateMove=True):
        self._send("left", double=False)

    def DoubleClick(self, simulateMove=True):
        self._send("left", double=True)

    def RightClick(self, simulateMove=True):
        self._send("right", double=False)

    def _send(self, button: str, double: bool):
        import win32api, win32con
        br = self.BoundingRectangle
        x, y = (br.left + br.right) // 2, (br.top + br.bottom) // 2
        _with_physical_dpi_context(lambda: win32api.SetCursorPos((x, y)))
        down, up = ((win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP) if button == "right"
                    else (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP))
        for i in range(2 if double else 1):
            if i:
                time.sleep(0.05)
            win32api.mouse_event(down, 0, 0, 0, 0)
            win32api.mouse_event(up, 0, 0, 0, 0)


def _click_text(app, query: str, modifier: str, keys: frozenset):
    """click <text>: read the foreground window, match, then act, label or report."""
    from samsara import screen_ocr  # noqa: PLC0415

    fg_hwnd = screen_ocr.foreground_hwnd()
    fg = _foreground_info(fg_hwnd)
    if is_overlay_active():
        # Our own numbered pills must not be read as the app's text.
        _destroy_overlay(app)
        time.sleep(_OVERLAY_HIDE_WAIT_S)
    try:
        snap = screen_ocr.read_foreground(fg_hwnd)
    except Exception as e:
        logger.warning("[CLICK-TEXT] could not read the foreground window exe=%s: %s", fg.get("exe"), e)
        _speak(app, TEXT_READ_FAILED_SPOKEN)
        _report_chip(app, f"{CHIP_CROSS} could not read the screen", "error")
        return DispatchState.FAILED

    found = screen_ocr.find_matches(query, snap.lines)
    matches = [m for m in found if screen_ocr.point_reaches_window(fg_hwnd, *m.center)]
    logger.info(
        "[CLICK-TEXT] query=%r exe=%s class=%s capture=%s words=%d timings_ms=%s matches=%d exact=%d covered=%d",
        query, fg.get("exe"), fg.get("cls"), snap.capture_rect, snap.word_count, snap.timings_ms,
        len(matches), sum(m.exact for m in matches), len(found) - len(matches))

    if not matches:
        _speak(app, TEXT_NO_MATCH_SPOKEN)
        _report_chip(app, TEXT_NO_MATCH_CHIP, "error")
        return DispatchState.FAILED

    def target(m):
        return _OcrTarget(m.rect, fg_hwnd, snap.window_rect, m.text)

    if len(matches) == 1 and matches[0].exact:
        m = matches[0]
        if not _click_with_validation(target(m), modifier, keys):
            logger.info("[CLICK-TEXT] window changed before the click; nothing clicked")
            _speak(app, TEXT_CHANGED_SPOKEN)
            _report_chip(app, f"{CHIP_CROSS} window changed; nothing clicked", "error")
            return DispatchState.FAILED
        logger.info("[CLICK-TEXT] clicked %r at physical %s modifier=%s keys=%s",
                    m.text, m.center, modifier, sorted(keys))
        return True

    elements = [{"control": target(m), "prect": m.rect, "name": m.text, "type": OCR_ELEMENT_TYPE}
                for m in matches]
    exact = matches[0].exact
    if exact:
        spoken = f"{len(matches)} matches. Say click and a number."
        chip = f"{len(matches)} matches: say click and a number"
    elif len(matches) == 1:
        spoken, chip = "Not sure. Say click one to confirm.", "not sure: say click one to confirm"
    else:
        spoken = f"Not sure. {len(matches)} options, say click and a number."
        chip = f"not sure: {len(matches)} options, say click and a number"
    logger.info("[CLICK-TEXT] %s candidates for %r (exact=%s scores=%s) -- labelled, not clicked",
                len(matches), query, exact, [m.score for m in matches])
    # The plan maps each physical rectangle through its own monitor (queue 56).
    _draw_overlay(app, elements, caption=chip, fg_info=dict(fg, hwnd=fg_hwnd, rect=snap.window_rect))
    _speak(app, spoken[:50])
    _report_chip(app, chip, "warning")
    return True

# ---------------------------------------------------------------------------
# Voice commands
# ---------------------------------------------------------------------------

@command("show overlay test",
         aliases=["overlay test", "show numbers debug"],
         pack="accessibility",
         risk_class="ui",
)
def handle_show_overlay_test(app, remainder):
    """Draws four test labels, to check the overlay can paint on this screen.

    Logs Qt event-loop state so the root cause of a blank overlay is visible.
    Keep this command in place — it costs nothing and confirms the renderer
    works independently of element enumeration.
    """
    import threading
    from PySide6.QtCore import QThread

    # Defensive start -- same reasoning as _draw_overlay(): this diagnostic
    # exists specifically to confirm the renderer works independently of
    # element enumeration, so it must not depend on some OTHER code path
    # having already started qt_runtime first.
    qt_runtime.ensure_started()

    qt_app = QApplication.instance()
    logger.info(f"[OVERLAY-TEST] QApplication.instance() = {qt_app}")
    if qt_app is not None:
        qt_thread = qt_app.thread()
        cur_thread = QThread.currentThread()
        on_qt_thread = qt_thread is cur_thread
        # QThread.currentThreadId() isn't exposed in this PySide6 build
        # (QThread.currentThread()/isCurrentThread() are); use Python's own
        # thread ident instead, which is always available.
        logger.info(
            f"[OVERLAY-TEST] on-Qt-thread={on_qt_thread}  "
            f"calling Python thread ident={threading.get_ident()}"
        )
        logger.info(
            f"[OVERLAY-TEST] Python thread: {threading.current_thread().name!r}"
        )
        if on_qt_thread:
            logger.info("[OVERLAY-TEST] Called from Qt thread — singleShot will fire")
        else:
            logger.info(
                "[OVERLAY-TEST] Called from worker thread — singleShot(0, qt_app, cb) "
                "routes to Qt thread via running event loop"
            )
    else:
        logger.info("[OVERLAY-TEST] WARNING: No QApplication — Qt event loop not running!")

    # Hardcoded labels at fixed logical coordinates; bypasses UIA, placement,
    # DPI conversion, and the entire element enumeration stack.
    labels = [
        [100, 100, 40, 30, "1"],
        [200, 100, 40, 30, "2"],
        [300, 100, 40, 30, "3"],
        [400, 100, 40, 30, "4"],
    ]

    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    qt_runtime.post(lambda: _show_overlay_qt(labels, fg_hwnd, app))
    logger.info("[OVERLAY-TEST] Queued 4 test labels — watch for [OVERLAY] _show_overlay_qt called")
    return True


def _show_grid_qt(fg_hwnd: int, app) -> None:
    """Draw diagnostic grid on the active monitor. Qt main thread only.

    Computes pill positions at the time of rendering from the actual active
    screen geometry so coordinates are always correct regardless of monitor
    layout or DPI scale.
    """
    global _overlay_window, _overlay_screen_name

    active_screen = screen_for_hwnd(fg_hwnd)
    if active_screen is None:
        active_screen = QApplication.primaryScreen()
    if active_screen is None:
        logger.info("[OVERLAY-GRID] ERROR: no screen available")
        return

    geo = active_screen.geometry()
    x0, y0, w, h = geo.x(), geo.y(), geo.width(), geo.height()
    pw, ph = _pill_w("TL"), _PILL_H

    labels = [
        [x0 + 100,         y0 + 100,        pw, ph, "TL"],
        [x0 + w // 2,      y0 + 100,        pw, ph, "TC"],
        [x0 + w - 160,     y0 + 100,        pw, ph, "TR"],
        [x0 + 100,         y0 + h - 140,    pw, ph, "BL"],
        [x0 + w // 2,      y0 + h // 2,     pw, ph, "MM"],
    ]

    if _overlay_window is not None:
        _overlay_window.close()
    _ensure_dpi_thread_context()
    _overlay_window = NumbersOverlayWindow(labels, active_screen)
    _overlay_screen_name = active_screen.name()
    _overlay_window.show()

    logger.info(
        f"[OVERLAY-GRID] Grid on {active_screen.name()} "
        f"{w}x{h} @{active_screen.devicePixelRatio():.1f}x:\n"
        f"  TL=({x0+100},{y0+100})  TC=({x0+w//2},{y0+100})"
        f"  TR=({x0+w-160},{y0+100})\n"
        f"  BL=({x0+100},{y0+h-140})  MM=({x0+w//2},{y0+h//2})"
    )


@command("overlay grid",
         aliases=["grid test", "overlay grid test"],
         pack="accessibility",
         risk_class="ui",
)
def handle_overlay_grid(app, remainder):
    """Draws five test labels across the screen, to check the overlay paints.

    Positions (TL, TC, TR, BL, MM) are computed on the Qt thread from the actual
    screen geometry so coordinates are always relative to whatever monitor the
    foreground window is on. Check the log for exact values.
    """
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _show_grid_qt(fg_hwnd, app))
    logger.info("[OVERLAY-GRID] Grid test queued — check log for pill coordinates")
    return True


# ---------------------------------------------------------------------------
# Mouse grid (queue 71) -- the universal last resort
# ---------------------------------------------------------------------------
#
# Numbers and OCR both need the app to expose something: UI Automation elements
# (show numbers) or readable text (click <text>). Icon-only buttons, canvases,
# games and video players expose neither. The grid needs nothing at all: it
# divides an area into nine numbered cells, a spoken digit picks one, and that
# cell becomes the new area. Three digits reach ~1/27 of the screen.
#
# The pointer MOVES on every refinement, like Dragon's MouseGrid. That is what
# makes "move without clicking" free, and it means the app's existing global
# "left click" / "right click" / "double click" work at the chosen point
# without this module registering competing phrases.
#
# Why build it when Windows Voice Access already has `show grid`: two
# always-listening voice systems share one microphone and both act on the same
# speech, so using Voice Access's grid means every Samsara phrase is also heard
# by Voice Access (and vice versa). A user who needs a grid needs it inside the
# tool they are already talking to.

_GRID_TAG = "mouse_grid.visible"
_grid_lock = threading.Lock()
#: None, or {"rect": physical (l,t,r,b), "path": [digits], "area": (l,t,r,b),
#: "screen_name": str, "label": str}
_grid_state: "dict | None" = None


def grid_active() -> bool:
    """True while the mouse grid is on screen. Any thread.

    This is the tag source queue 68's command_scope reads, so the grid's own
    phrases are candidates ONLY while it is visible -- the same rule the window
    cube uses for its bare numbers.
    """
    with _grid_lock:
        return _grid_state is not None


def grid_state() -> "dict | None":
    with _grid_lock:
        return dict(_grid_state) if _grid_state else None


def _monitor_rects_physical() -> list:
    """(number, physical rect) per monitor, numbered left-to-right (queue 71).

    Physical pixels, read inside a per-monitor-V2 thread context, so the
    numbers are right whatever the process-wide awareness is (queue 70 found
    the running app is only system-DPI-aware).
    """
    from samsara.ui.numbers_overlay_qt import _win32_monitor_rects  # noqa: PLC0415
    return mouse_grid.order_monitors(_win32_monitor_rects())


def _window_rect_physical(hwnd: int) -> "tuple | None":
    try:
        import win32gui  # noqa: PLC0415
        rect = _with_physical_dpi_context(lambda: win32gui.GetWindowRect(hwnd))
        left, top, right, bottom = (int(v) for v in rect)
        if right - left > 20 and bottom - top > 20:
            return left, top, right, bottom
    except Exception as exc:
        logger.debug("[GRID] window rect unavailable: %s", exc)
    return None


def _grid_area(scope: "str | None", monitor: "int | None", fg_hwnd: int) -> tuple:
    """(physical rect, label) for the first grid, honouring the spoken scope."""
    monitors = _monitor_rects_physical()
    if scope == mouse_grid.SCOPE_WINDOW:
        rect = _window_rect_physical(fg_hwnd)
        if rect is not None:
            return rect, "this window"
        logger.info("[GRID] no window rect -- falling back to the active monitor")
    if scope == mouse_grid.SCOPE_MONITOR and monitor is not None:
        for number, rect in monitors:
            if number == monitor:
                return rect, f"monitor {number}"
        logger.info("[GRID] monitor %s not found (%d attached)", monitor, len(monitors))
    # Default: the monitor the foreground window is on.
    rect = _window_rect_physical(fg_hwnd)
    if rect is not None and monitors:
        cx, cy = mouse_grid.center(rect)
        for number, mon in monitors:
            if mon[0] <= cx < mon[2] and mon[1] <= cy < mon[3]:
                return mon, f"monitor {number}"
    if monitors:
        return monitors[0][1], "monitor 1"
    return (0, 0, 1920, 1080), "screen"


def _move_pointer(x: int, y: int) -> None:
    """Park the pointer at a physical screen point, in a PMv2 thread context."""
    import win32api  # noqa: PLC0415
    _with_physical_dpi_context(lambda: win32api.SetCursorPos((int(x), int(y))))


def _grid_click(action: str) -> None:
    import win32api, win32con  # noqa: PLC0415
    if action == "right":
        pairs = [(win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP)]
    elif action == "double":
        pairs = [(win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP)] * 2
    else:
        pairs = [(win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP)]
    for i, (down, up) in enumerate(pairs):
        if i:
            time.sleep(0.05)
        win32api.mouse_event(down, 0, 0, 0, 0)
        win32api.mouse_event(up, 0, 0, 0, 0)


def _grid_pills(rect: tuple) -> tuple:
    """(labels, cells) in Qt LOGICAL coordinates for one grid level.

    Cell geometry is computed in physical pixels (the coordinate system the
    pointer uses) and mapped through queue 56's phys_to_logical, so a mixed-DPI
    desktop puts every pill on the pixel its number owns. Qt thread only.
    """
    from samsara.ui.numbers_overlay_qt import phys_to_logical  # noqa: PLC0415
    labels, cells = [], []
    for digit, cell in mouse_grid.cells(rect):
        cx, cy = mouse_grid.center(cell)
        lx, ly = phys_to_logical(cx, cy)
        pw, ph = _pill_w(str(digit)), _PILL_H
        # pill_rect() anchors a pill's bottom-right just outside (sx, sy); ask
        # for the anchor that leaves the pill centred on the cell centre.
        labels.append([lx + pw // 2 - PILL_ANCHOR_DX, ly + ph // 2 - PILL_ANCHOR_DY, pw, ph, str(digit)])
        l, t = phys_to_logical(cell[0], cell[1])
        r, b = phys_to_logical(cell[2], cell[3])
        cells.append((l, t, r, b))
    return labels, cells


def _show_grid_level_qt(app, rect: tuple, fg_hwnd: int, caption: str) -> None:
    """Draw one grid level. Qt main thread only."""
    from samsara.ui.numbers_overlay_qt import phys_to_logical  # noqa: PLC0415
    labels, cells = _grid_pills(rect)
    lx, ly = phys_to_logical(rect[0], rect[1])
    rx, ry = phys_to_logical(rect[2], rect[3])
    screen = _screen_for_physical_point(*mouse_grid.center(rect), fg_hwnd=fg_hwnd)
    _show_overlay_qt(labels, fg_hwnd, app, caption=caption,
                     bounds=(lx, ly, rx, ry) if (rx - lx) > 40 and (ry - ly) > 30 else None,
                     screen=screen, cells=cells)


def _screen_for_physical_point(px: int, py: int, fg_hwnd: int = 0):
    """The QScreen containing a physical point (Qt thread), else the
    foreground window's screen."""
    from samsara.ui.numbers_overlay_qt import phys_to_logical, screen_for_hwnd  # noqa: PLC0415
    try:
        lx, ly = phys_to_logical(px, py)
        screen = QApplication.screenAt(QPoint(lx, ly))
        if screen is not None:
            return screen
    except Exception as exc:
        logger.debug("[GRID] screenAt failed: %s", exc)
    return screen_for_hwnd(fg_hwnd)


def _grid_show(app, rect: tuple, area: tuple, path: list, label: str, fg_hwnd: int) -> None:
    """Publish grid state, move the pointer to the centre, and draw."""
    global _grid_state
    cx, cy = mouse_grid.center(rect)
    with _grid_lock:
        _grid_state = {"rect": tuple(rect), "area": tuple(area), "path": list(path),
                       "screen_name": "", "label": label, "hwnd": fg_hwnd}
    try:
        _move_pointer(cx, cy)
    except Exception as exc:
        logger.warning("[GRID] could not move the pointer: %s", exc)
    steps = "".join(str(d) for d in path)
    caption = f"grid {label}" + (f" -- {steps}" if steps else "") + " -- say a number"
    qt_runtime.ensure_started()
    qt_runtime.post(lambda: _show_grid_level_qt(app, rect, fg_hwnd, caption))
    logger.info("[GRID] level %d %s over %s, pointer at (%d,%d)",
                len(path), mouse_grid.describe(rect), label, cx, cy)


def _grid_hide(app=None) -> None:
    global _grid_state
    with _grid_lock:
        was = _grid_state is not None
        _grid_state = None
    if was:
        qt_runtime.post(_hide_overlay_qt)
        logger.info("[GRID] hidden")


def _grid_apply(app, digits: list, action: "str | None") -> bool:
    """Refine by `digits`, then perform `action` (or leave the grid up).

    This is the ONE path for both spellings: a chained "grid 5 3 8 click" and
    the same steps said one at a time land here with the same arguments, which
    is what the chaining test asserts.
    """
    state = grid_state()
    if state is None:
        return False
    rect = state["rect"]
    path = list(state["path"])
    for digit in digits:
        if mouse_grid.too_small(rect):
            logger.info("[GRID] cell is already %s -- not subdividing further",
                        mouse_grid.describe(rect))
            break
        rect = mouse_grid.cell_rect(rect, digit)
        path.append(digit)
    if digits:
        _grid_show(app, rect, state["area"], path, state["label"], state.get("hwnd", 0))
    if not action:
        return True

    cx, cy = mouse_grid.center(rect)
    try:
        _move_pointer(cx, cy)
    except Exception as exc:
        logger.warning("[GRID] could not move the pointer: %s", exc)
    if action == "move":
        _grid_hide(app)
        _report_chip(app, f"pointer at ({cx},{cy})", "success", delay_s=0)
        logger.info("[GRID] pointer parked at (%d,%d) -- no click", cx, cy)
        return True
    _grid_hide(app)
    try:
        _grid_click(action)
    except Exception as exc:
        logger.warning("[GRID] click failed: %s", exc)
        _report_chip(app, f"{CHIP_CROSS} grid click failed", "error", delay_s=0)
        return True
    wording = {"click": "clicked", "right": "right-clicked", "double": "double-clicked"}[action]
    _report_chip(app, f"{wording} ({cx},{cy})", "success", delay_s=0)
    logger.info("[GRID] %s at (%d,%d) after %s", wording, cx, cy,
                "".join(str(d) for d in path) or "no refinement")
    return True


def _grid_from_click(app, remainder) -> "bool | None":
    """Route a click utterance to the visible grid, or None to let the normal
    numbered/OCR click path handle it.

    "click 5" / a bare "5"  -> refine to cell 5
    "click"                 -> left click at the current cell
    "click right" / "twice" -> right / double click there
    anything else ("click save") -> None: the grid stays up and the usual
    text path runs, so the grid never swallows a click by name.
    """
    if not grid_active():
        return None
    text = (remainder or "").strip()
    if not text:
        return _grid_apply(app, [], "click")
    parsed = mouse_grid.parse(text)
    if parsed.get("back"):
        handle_grid_back(app, "")
        return True
    if parsed["digits"]:
        return _grid_apply(app, parsed["digits"], parsed["action"])
    if parsed["action"]:
        return _grid_apply(app, [], parsed["action"])
    if re.fullmatch(r"(twice|double)", text.lower()):
        return _grid_apply(app, [], "double")
    return None


@command("mouse grid",
         aliases=["grid", "show grid", "mousegrid"],
         pack="accessibility", risk_class="read")
def handle_mouse_grid(app, remainder):
    """Puts a numbered grid on the screen for pointing at anything.

    "grid" the monitor the focused window is on "grid window" / "grid here" just
    the focused window "grid monitor two" a named monitor (left to right) "grid
    five three eight" three refinements in one utterance "grid 5 3 8 click" ...
    and a left click at the end Then: a bare number refines, "click"/"right
    click"/"double click" act, "move here" parks the pointer, "hide grid" closes
    it.
    """
    parsed = mouse_grid.parse(remainder or "")
    try:
        import win32gui  # noqa: PLC0415
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0

    if parsed.get("back") and grid_active():
        return handle_grid_back(app, "")

    area, label = _grid_area(parsed["scope"], parsed["monitor"], fg_hwnd)
    _grid_show(app, area, area, [], label, fg_hwnd)
    if parsed["digits"] or parsed["action"]:
        _grid_apply(app, parsed["digits"], parsed["action"])
    return True


@command("hide grid",
         aliases=["grid off", "close grid", "cancel grid"],
         pack="accessibility", risk_class="read",
         scope={"tags": [_GRID_TAG]})
def handle_hide_grid(app, remainder):
    """Closes the mouse grid.

    Only a candidate while the grid is on screen.
    """
    _grid_hide(app)
    return True


@command("grid back",
         aliases=["undo grid", "grid up"],
         pack="accessibility", risk_class="read",
         scope={"tags": [_GRID_TAG]})
def handle_grid_back(app, remainder):
    """Steps the mouse grid back one refinement, after a misheard number."""
    state = grid_state()
    if state is None:
        return True
    path = list(state["path"])[:-1]
    rect = mouse_grid.refine(state["area"], path)
    _grid_show(app, rect, state["area"], path, state["label"], state.get("hwnd", 0))
    return True


@command("move here",
         aliases=["park here", "move mouse here", "just move"],
         pack="accessibility", risk_class="read",
         scope={"tags": [_GRID_TAG]})
def handle_move_here(app, remainder):
    """Moves the pointer to the current grid cell and closes the grid, without clicking."""
    return _grid_apply(app, [], "move")


try:  # pragma: no cover - import-time wiring
    from samsara import command_scope as _command_scope  # noqa: E402

    _command_scope.register_tag_source(_GRID_TAG, grid_active)
except Exception as _exc:  # pragma: no cover
    logger.debug("[GRID] could not register the scope tag: %s", _exc)


@command("show numbers",
         aliases=["show clickable", "show labels", "label things", "show"],
         pack="accessibility", risk_class="read")
def handle_show_numbers(app, remainder):
    """Numbers everything clickable in the focused window so you can name one.

    Tries the DOM (browser-extension) path first when Brave is the foreground
    window -- native UI Automation sees Brave's own tabs/ bookmarks and the
    actual webpage as the same kind of thing, so on a real page with many
    tabs/bookmarks the 99-result UIA cap is consumed before webpage controls are
    ever reached. The DOM path structurally cannot enumerate tabs/bookmarks at
    all (a content script's `document` is only ever the webpage itself), so it's
    preferred whenever available. Falls back to UIA -- visibly, not silently --
    when the extension isn't installed, isn't connected, or the active tab is a
    restricted page. If the (UIA) overlay is already showing, re-enumerate fresh
    (bypass the cache) instead of reusing whatever _cached_enumerate() last saw
    -- folds show + refresh into one word, since saying "show"/"show numbers"
    again while it's already up is almost always "the UI changed, refresh this,"
    not "show me the exact same labels again." This is the primary show ->
    observe -> click loop the short "show" alias is meant to serve.
    """
    try:
        import win32gui
        fg_hwnd = win32gui.GetForegroundWindow()
    except Exception:
        fg_hwnd = 0
    fg = _foreground_info(fg_hwnd)
    logger.info(
        "[SHOW_NUMBERS] invoked: foreground exe=%s class=%s kind=%s rect=%s topmost=%s",
        fg.get("exe"), fg.get("cls"), fg.get("kind"), fg.get("rect"), fg.get("topmost"),
    )

    handled, fallback_reason = _try_show_dom_numbers(app)
    if handled:
        logger.info("[SHOW_NUMBERS] handled by the browser-extension (DOM) path")
        return True

    caption = ""
    if fallback_reason is not None:
        logger.info(
            "[SHOW_NUMBERS] DOM bridge unavailable/timeout -- falling back to "
            "UIA (reason=%s)", fallback_reason,
        )
        caption = "Native fallback (browser extension unavailable)"

    if is_overlay_active():
        _invalidate_cache()
        elements = _enumerate_foreground_clickables()
    else:
        elements = _cached_enumerate()
    logger.info("[SHOW_NUMBERS] enumerated %d clickable elements %s", len(elements), _last_enum_stats)
    if not elements:
        # Never an empty overlay and never a success: say what happened.
        _report_nothing(app, fg)
        return DispatchState.FAILED
    _draw_overlay(app, elements, caption)
    if hasattr(app, 'hints'):
        app.hints.increment('show_numbers_used')
    return True


@command("hide numbers",
         aliases=["dismiss numbers", "hide labels", "clear labels", "hide"],
         pack="accessibility",
         risk_class="ui",
)
def handle_hide_numbers(app, remainder):
    """Clears the numbered labels from the screen."""
    _destroy_overlay(app)
    return True


@command("refresh numbers",
         aliases=["update numbers", "refresh"],
         pack="accessibility",
         risk_class="ui",
)
def handle_refresh_numbers(app, remainder):
    """Rescans the focused window and redraws the numbered labels."""
    _invalidate_cache()
    elements = _enumerate_foreground_clickables()
    logger.info("[SHOW_NUMBERS] refresh enumerated %d %s", len(elements), _last_enum_stats)
    if not elements:
        try:
            import win32gui
            fg = _foreground_info(win32gui.GetForegroundWindow())
        except Exception:
            fg = _foreground_info(0)
        _report_nothing(app, fg)
        return DispatchState.FAILED
    _draw_overlay(app, elements)
    return True


@command("click",
         aliases=["tap", "press"],
         pack="accessibility",
         risk_class="write", param_schema={"label": {"type": "int", "required": False}},
)
def handle_click(app, remainder):
    """Clicks the numbered thing you name, such as click seven.

    While the mouse grid is on screen, a number refines the grid instead of
    clicking a label, and a bare "click" acts at the current cell (queue 71).
    The dictation lane turns a sole spoken number into "click N", so this is
    also where a bare "five" lands.
    """
    if grid_active():
        handled = _grid_from_click(app, remainder)
        if handled is not None:
            return handled
    if not remainder or not remainder.strip():
        return True

    text = remainder.strip().lower()

    keys: set = set()
    for kw in ('shift', 'ctrl', 'alt'):
        if re.search(r'\b' + kw + r'\b', text):
            keys.add(kw)
            text = re.sub(r'\b' + kw + r'\b', '', text)
    keys_frozen = frozenset(keys)

    modifier = 'single'
    if re.search(r'\b(twice|double)\b', text):
        modifier = 'double'
        text = re.sub(r'\b(twice|double)\b', '', text)
    elif re.search(r'\bright\b', text):
        modifier = 'right'
        text = re.sub(r'\bright\b', '', text)

    text = re.sub(r'^(click|tap|press|select)\s+', '', text.strip()).strip()

    if text and not _is_label_number(text):
        from samsara.screen_ocr import token_view as tokens  # noqa: PLC0415
        if " ".join(tokens(text)) in _KEY_NAMES:
            logger.info("[CLICK-TEXT] %r names a key, not on-screen text -- not reading the screen", text)
            return True
        return _click_text(app, text, modifier, keys_frozen)

    number = _parse_spoken_number(text)
    if number is None:
        logger.info(f"[OVERLAY] Couldn't parse number from: {remainder!r}")
        return True

    with _dom_lock:
        dom_active = _dom_active
        dom_count = _dom_hint_count

    if dom_active:
        if number < 1 or number > dom_count:
            msg = f"Element {number} is not available. There are {dom_count} elements."
            logger.info(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        action = {'single': 'click', 'double': 'doubleclick', 'right': 'rightclick'}.get(
            modifier, 'click'
        )
        dom_modifiers = {
            'ctrlKey': 'ctrl' in keys_frozen,
            'shiftKey': 'shift' in keys_frozen,
            'altKey': 'alt' in keys_frozen,
        }
        ok = browser_bridge.get_bridge().send_selection(number, action, modifiers=dom_modifiers)
        if not ok:
            msg = f"Element {number} is no longer available."
            logger.info(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        # content.js already clears its own hints on a successful selection;
        # this only clears the Python-side flags (a redundant dismiss to an
        # already-cleared content script is a harmless no-op there).
        _clear_dom_session()
        return True

    with _state_lock:
        if not _elements:
            logger.info("[OVERLAY] No overlay active -- use 'show numbers' first")
            return True
        if number < 1 or number > len(_elements):
            msg = f"Element {number} is not available. There are {len(_elements)} elements."
            logger.info(f"[OVERLAY] {msg}")
            _speak(app, msg)
            return True
        element = _elements[number - 1]

    if not _click_with_validation(element, modifier, keys_frozen):
        msg = f"Element {number} is no longer available."
        logger.info(f"[OVERLAY] {msg}")
        _speak(app, msg)
        return True

    _destroy_overlay(app)
    return True


def enumerate_clickable_elements() -> list:
    """Public alias for _enumerate_foreground_clickables."""
    return _enumerate_foreground_clickables()


def is_overlay_active() -> bool:
    """True while a numbered overlay -- UIA/Qt OR DOM/in-page -- is
    currently shown.

    Safe to call from any thread: _elements is populated (any thread, under
    _state_lock) in _draw_overlay before the Qt window is posted, and cleared
    (Qt thread, under _state_lock) in _hide_overlay_qt; _dom_active is set/
    cleared (worker thread or a browser_bridge connection thread, under
    _dom_lock) in _try_show_dom_numbers/_clear_dom_session -- so this never
    reads Qt objects across threads, just the two lock-guarded flags other
    callers (e.g. reminder_toast) use to gate on overlay visibility of
    either kind.
    """
    with _state_lock:
        uia = bool(_elements)
    with _dom_lock:
        dom = _dom_active
    return uia or dom or grid_active()
