"""UIA provider and pure overlay filters. Call provider functions on the service worker only."""
import logging

logger = logging.getLogger(__name__)
_uia_mod = None
_UIA_AVAILABLE = None

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

class NativeNode:
    """Raw UIA element bound to its backend's apartment, never exported."""
    def __init__(self, backend, element, cached=False):
        self.backend, self.element, self.cached = backend, element, cached

    def _prop(self, name):
        return getattr(self.element, ('Cached' if self.cached else 'Current') + name)

    @property
    def ControlTypeName(self):
        return self.backend.names.get(self._prop('ControlType'), '')

    @property
    def BoundingRectangle(self):
        r = self._prop('BoundingRectangle')
        return Rect(r.left, r.top, r.right, r.bottom)

    @property
    def Name(self):
        return self._prop('Name') or ''

    @property
    def IsEnabled(self):
        return bool(self._prop('IsEnabled'))

    @property
    def IsOffscreen(self):
        return bool(self._prop('IsOffscreen'))

    @property
    def IsPassword(self):
        return bool(self.element.CurrentIsPassword)

    def GetChildren(self):
        if self.cached:
            children = self.element.GetCachedChildren()
            return [NativeNode(self.backend, children.GetElement(i), True)
                    for i in range(children.Length)] if children else []
        result = []
        child = self.backend.walker.GetFirstChildElement(self.element)
        while child:
            result.append(NativeNode(self.backend, child))
            child = self.backend.walker.GetNextSiblingElement(child)
        return result

    def live(self):
        return NativeNode(self.backend, self.element)

    def SetFocus(self):
        self.element.SetFocus()

    def pattern(self, kind):
        pid, interface = {
            'invoke': (10000, 'IUIAutomationInvokePattern'),
            'toggle': (10015, 'IUIAutomationTogglePattern'),
            'expand': (10005, 'IUIAutomationExpandCollapsePattern'),
            'range': (10003, 'IUIAutomationRangeValuePattern'),
            'text': (10014, 'IUIAutomationTextPattern'),
        }[kind]
        try:
            raw = self.element.GetCurrentPattern(pid)
        except Exception as exc:
            # UIA_E_NOTSUPPORTED is absence, not a failed action. Propagate
            # other failures: an unavailable element must never get clicked.
            if (getattr(exc, 'hresult', 0) & 0xFFFFFFFF) == 0x80040200:
                return None
            raise
        return raw.QueryInterface(getattr(self.backend.core, interface)) if raw else None


class Rect:
    __slots__ = ('left', 'top', 'right', 'bottom')

    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class WindowsBackend:
    """Own IUIAutomation client: never reuse uiautomation's global COM singleton."""
    def __init__(self):
        import sys
        was_loaded = 'comtypes' in sys.modules
        import comtypes
        # comtypes initializes its first importing thread as STA. This fresh
        # worker has created no objects yet; balance that import before MTA.
        if not was_loaded:
            comtypes.CoUninitialize()
        comtypes.CoInitializeEx(0)
        self.com = comtypes
        self.client = self.walker = None
        try:
            import comtypes.client
            self.core = comtypes.client.GetModule('UIAutomationCore.dll')
            self.client = comtypes.client.CreateObject(
                '{ff48dba4-60ef-4201-aa87-54103eef594e}', interface=self.core.IUIAutomation)
            self.walker = self.client.RawViewWalker
            # These are integer -> string constants, not the library's client.
            from uiautomation.uiautomation import ControlTypeNames
            self.names = ControlTypeNames
        except BaseException:
            self.close()
            raise

    def foreground(self):
        import win32gui
        return win32gui.GetForegroundWindow()

    def root(self, hwnd):
        request = self.client.CreateCacheRequest()
        for pid in _UIA_PROPERTY_IDS:
            request.AddProperty(pid)
        request.TreeScope = 7
        request.TreeFilter = self.client.RawViewCondition
        request.AutomationElementMode = 1
        try:
            return NativeNode(self, self.client.ElementFromHandleBuildCache(hwnd, request), True), 'uia-cache'
        except Exception:
            return NativeNode(self, self.client.ElementFromHandle(hwnd)), 'uia-walk'

    def focused(self):
        element = self.client.GetFocusedElement()
        return NativeNode(self, element) if element else None

    def close(self):
        self.walker = self.client = None
        self.com.CoUninitialize()
