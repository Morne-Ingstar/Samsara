"""Shared UI Automation service; import is inert, COM starts lazily on one worker.

snapshot()/search() return immutable values, never COM controls. Actions accept
Node tokens and must be called only after the consumer's execution-policy gate.
The 0.75 s TTL is the invalidation fallback for providers without events; explicit
invalidate(hwnd) is available to structure/focus observers. Foreground changes
also invalidate the affected window. No text/name values are logged here.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, TimeoutError
from dataclasses import dataclass, field, replace
import math
import queue
import threading
import time
import unicodedata

from samsara.runtime import thread_registry
from . import backend as b


class UIAError(RuntimeError):
    pass


class StaleTarget(UIAError):
    pass


class UIATimeout(UIAError):
    pass


@dataclass(frozen=True)
class Node:
    hwnd: int
    generation: int
    index: int
    name: str = field(repr=False)
    control_type: str
    rect: tuple
    enabled: bool
    offscreen: bool
    in_document: bool
    depth: int
    foreground_hwnd: int = 0  # Non-foreground targets remain bound to their owner.


@dataclass(frozen=True)
class Snapshot:
    hwnd: int
    generation: int
    nodes: tuple[Node, ...]
    stats: tuple


def normalize_name(text):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', text).casefold() if ch.isalnum())


def _query(text):
    if len(text.split()) != 1:
        raise ValueError('UIA search requires one word')
    result = normalize_name(text)
    if not result:
        raise ValueError('UIA search requires a word')
    return result


class ControlProxy:
    """Compatibility facade for numbered clicks; contains only a Node token."""
    def __init__(self, service, node):
        self._service, self._node = service, node

    @property
    def BoundingRectangle(self):
        return b.Rect(*self._service.geometry(self._node))

    @property
    def IsEnabled(self):
        self._service.geometry(self._node)  # validates enabled, visible and alive
        return True

    def Click(self, simulateMove=True):
        self._service.act(self._node, 'physical_click')

    def DoubleClick(self, simulateMove=True):
        self._service.act(self._node, 'double_click')

    def RightClick(self, simulateMove=True):
        self._service.act(self._node, 'right_click')


def _click_rect(rect, verb):
    import ctypes
    import win32api
    import win32con
    # UIA coordinates are physical; do not let caller DPI awareness rescale them.
    previous = ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        win32api.SetCursorPos(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2))
        right = verb == 'right_click'
        down = win32con.MOUSEEVENTF_RIGHTDOWN if right else win32con.MOUSEEVENTF_LEFTDOWN
        up = win32con.MOUSEEVENTF_RIGHTUP if right else win32con.MOUSEEVENTF_LEFTUP
        for i in range(2 if verb == 'double_click' else 1):
            if i:
                time.sleep(0.05)
            win32api.mouse_event(down, 0, 0, 0, 0)
            win32api.mouse_event(up, 0, 0, 0, 0)
    finally:
        if previous:
            ctypes.windll.user32.SetThreadDpiAwarenessContext(previous)


def _overlay_actionable(control, control_type):
    if control_type in b._CLICKABLE_TYPES:
        return True
    # Providers inherit Invoke on noninteractive text/containers. Those are
    # eligible only when they also expose their own keyboard focus stop.
    structural = {'TextControl', 'PaneControl', 'GroupControl', 'StatusBarControl',
                  'ToolBarControl', 'WindowControl', 'DocumentControl', 'TabControl'}
    raw = getattr(control, 'element', getattr(control, '_el', None))
    if control_type in structural:
        try:
            focusable = raw.CurrentIsKeyboardFocusable if raw is not None else control.IsKeyboardFocusable
            if not focusable:
                return False
        except Exception:
            return False
    for getter, pattern_id in (('GetInvokePattern', 10000), ('GetTogglePattern', 10015),
                               ('GetSelectionItemPattern', 10010), ('GetExpandCollapsePattern', 10005)):
        try:
            pattern = raw.GetCurrentPattern(pattern_id) if raw is not None else getattr(control, getter, lambda: None)()
            if pattern is not None and bool(pattern):
                return True
        except Exception:
            continue
    return False


def overlay_target_nodes(nodes, actionable=None):
    """Pure overlay-only filter; search/actions keep the original snapshot.

    Preorder depths reconstruct ancestry without exporting native controls.
    Ignore empty structural rects (some providers use these for virtual
    containers), but enforce every real ancestor's visible clip. Suppress a
    duplicate ancestor when a descendant covers its actionable region. A
    tab and its small close button remain separate, independently usable
    targets; their labels are separated by the bounded layout pass.
    """
    candidates, ancestors, lineage = [], [], {}
    for node in nodes:
        while ancestors and ancestors[-1].depth >= node.depth:
            ancestors.pop()
        lineage[node.index] = tuple(a.index for a in ancestors)
        l, t, r, bot = node.rect
        valid = (node.enabled and not node.offscreen and
                 8 <= r-l <= 1500 and 8 <= bot-t <= 1000)
        for parent in ancestors:
            pl, pt, pr, pb = parent.rect
            if (parent.offscreen or not parent.enabled or
                    (pr > pl and pb > pt and not (pl <= l and pt <= t and r <= pr and bot <= pb))):
                valid = False
                break
        if valid and (actionable(node) if actionable else node.control_type in b._CLICKABLE_TYPES):
            candidates.append(node)
        ancestors.append(node)
    suppressed = set()
    by_index = {node.index: node for node in candidates}
    for node in candidates:
        for index in lineage[node.index]:
            parent = by_index.get(index)
            if parent is None:
                continue
            l, t, r, bot = parent.rect
            nl, nt, nr, nb = node.rect
            covered = max(0, min(r, nr)-max(l, nl)) * max(0, min(bot, nb)-max(t, nt))
            if covered >= (r-l)*(bot-t)*0.8:
                suppressed.add(index)
    return [node for node in candidates if node.index not in suppressed]


class UIAService:
    def __init__(self, backend_factory=b.WindowsBackend, *, ttl=0.75, max_windows=8,
                 timeout=5.0, clock=time.monotonic, click=_click_rect):
        if ttl <= 0 or max_windows < 1:
            raise ValueError('positive TTL and cache size required')
        self._factory, self._ttl, self._limit = backend_factory, ttl, max_windows
        self._timeout, self._clock, self._click = timeout, clock, click
        self._queue = queue.Queue(maxsize=64)
        self._lock = threading.Lock()
        self._thread = None
        self._closed = False
        self._backend = None
        self._cache = OrderedDict()
        self._generation = 0
        self._foreground = None

    def _call(self, fn, *args):
        with self._lock:
            if self._closed:
                raise UIAError('UIA service closed')
            if self._thread is None:
                self._thread = thread_registry.spawn('uia-service', self._run)
            future = Future()
            try:
                self._queue.put_nowait((future, fn, args, time.monotonic() + self._timeout))
            except queue.Full:
                raise UIAError('UIA service busy') from None
        try:
            return future.result(self._timeout)
        except TimeoutError:
            future.cancel()  # queued side effects must never run after timeout
            raise UIATimeout('UIA provider did not respond') from None

    def _run(self):
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                future, fn, args, self._deadline = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    future.set_result(fn(*args))
                except Exception as exc:
                    # Never export a provider traceback retaining COM references.
                    error = type(exc) if isinstance(exc, UIAError) else UIAError
                    future.set_exception(error('UIA request failed: ' + type(exc).__name__))
                finally:
                    item = fn = args = None
        finally:
            self._cache.clear()
            if self._backend is not None:
                self._backend.close()
                self._backend = None

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            if thread:
                # Cancel pending jobs, then request cleanup on the COM owner.
                while True:
                    try:
                        job = self._queue.get_nowait()
                        job[0].cancel()
                    except queue.Empty:
                        break
                self._queue.put_nowait(None)
        if thread and thread is not threading.current_thread():
            thread.join(self._timeout)

    def _check_deadline(self):
        if time.monotonic() >= self._deadline:
            raise UIATimeout('request expired before action')

    def _get_backend(self):
        if self._backend is None:
            self._backend = self._factory()
        return self._backend

    def invalidate(self, hwnd=None):
        def clear():
            if hwnd is None:
                self._cache.clear()
            else:
                self._cache.pop(hwnd, None)
        return self._call(clear)

    def _window(self):
        hwnd = self._get_backend().foreground()
        if self._foreground != hwnd:
            self._cache.pop(self._foreground, None)
            self._cache.pop(hwnd, None)
            self._foreground = hwnd
        if not hwnd:
            raise UIAError('no foreground window')
        return hwnd

    def snapshot(self, *, refresh=False):
        return self._call(self._snapshot, refresh)

    def _snapshot(self, refresh=False, target_hwnd=None):
        owner = self._window()
        hwnd = target_hwnd or owner
        now = self._clock()
        existing = self._cache.get(hwnd)
        if existing and not refresh and now - existing[0] < self._ttl:
            self._cache.move_to_end(hwnd)
            return existing[1]
        # Drop old controls before a failed rebuild too.
        self._cache.pop(hwnd, None)
        started = time.perf_counter()
        root, source = self._get_backend().root(hwnd)
        fetched = time.perf_counter()
        self._generation += 1
        nodes, controls, stats = self._walk(root, hwnd, self._generation)
        if hwnd != owner:
            nodes = [replace(n, foreground_hwnd=owner) for n in nodes]
        stats.update(fetch_ms=int((fetched - started) * 1000),
                     ms=int((time.perf_counter() - started) * 1000))
        snapshot = Snapshot(hwnd, self._generation, tuple(nodes), tuple(dict(stats, source=source).items()))
        self._cache[hwnd] = (self._clock(), snapshot, controls)
        while len(self._cache) > self._limit:
            self._cache.popitem(last=False)
        return snapshot

    def _walk(self, root, hwnd, generation, *, node_budget=8000, max_depth=64, time_budget=3.0):
        started = time.perf_counter()
        nodes, controls = [], []
        stack = [(root, 0, False)]
        visited, deepest, budget_hit = 0, 0, None
        while stack:
            if visited >= node_budget:
                budget_hit = 'nodes'
                break
            if time.perf_counter() - started > time_budget:
                budget_hit = 'time'
                break
            ctrl, depth, in_doc = stack.pop()
            visited += 1
            deepest = max(deepest, depth)
            ctype = ''
            try:
                ctype = ctrl.ControlTypeName
                r = ctrl.BoundingRectangle
                node = Node(hwnd, generation, len(nodes), ctrl.Name or '', ctype,
                            (r.left, r.top, r.right, r.bottom), bool(ctrl.IsEnabled),
                            bool(getattr(ctrl, 'IsOffscreen', False)), in_doc, depth)
                nodes.append(node)
                controls.append(ctrl)
            except Exception:
                pass  # An unavailable node must not hide its siblings.
            if depth < max_depth:
                try:
                    children = ctrl.GetChildren()
                except Exception:
                    children = []
                stack.extend((child, depth + 1, in_doc or ctype == 'DocumentControl')
                             for child in reversed(children))
        return nodes, controls, dict(nodes=visited, max_depth=deepest, budget_hit=budget_hit,
                                     ms=int((time.perf_counter() - started) * 1000))

    def search(self, word):
        query = _query(word)
        return self._call(lambda: tuple(n for n in self._snapshot().nodes
                                       if n.enabled and not n.offscreen and query in normalize_name(n.name)))

    def _target(self, node):
        if self._window() != (node.foreground_hwnd or node.hwnd):
            raise StaleTarget('foreground changed')
        cached = self._cache.get(node.hwnd)
        if (not cached or cached[1].generation != node.generation or
                node.index < 0 or node.index >= len(cached[2]) or
                cached[1].nodes[node.index] != node):
            raise StaleTarget('tree changed')
        ctrl = cached[2][node.index]
        if hasattr(ctrl, 'live'):
            ctrl = ctrl.live()
        elif isinstance(ctrl, b._CachedNode):
            ctrl = ctrl.control()
        r = ctrl.BoundingRectangle
        rect = (r.left, r.top, r.right, r.bottom)
        if not ctrl.IsEnabled or getattr(ctrl, 'IsOffscreen', False) or r.width() <= 0 or r.height() <= 0:
            raise StaleTarget('target unavailable')
        if rect != node.rect:
            raise StaleTarget('target moved')
        if self._window() != (node.foreground_hwnd or node.hwnd):
            raise StaleTarget('foreground changed during validation')
        self._check_deadline()
        return ctrl, rect

    def geometry(self, node):
        return self._call(lambda: self._target(node)[1])

    @staticmethod
    def _pattern(ctrl, kind):
        if hasattr(ctrl, 'pattern'):
            return ctrl.pattern(kind)
        getter = getattr(ctrl, {'invoke': 'GetInvokePattern', 'toggle': 'GetTogglePattern',
                               'expand': 'GetExpandCollapsePattern', 'range': 'GetRangeValuePattern',
                               'text': 'GetTextPattern'}[kind], None)
        return getter() if getter else None

    def act(self, node, verb, value=None):
        """Perform one policy-approved action. Missing patterns may fall back to a click.

        Activation verbs can use that fallback when their pattern is absent.
        Range values and text reads require the actual pattern.
        Provider failures never trigger another side effect (it may have executed).
        """
        verbs = {'click', 'invoke', 'toggle', 'expand', 'collapse', 'focus', 'set_value',
                 'read', 'double_click', 'right_click', 'physical_click'}
        if verb not in verbs:
            raise ValueError('unsupported UIA verb')
        if verb == 'set_value' and (value is None or not math.isfinite(float(value))):
            raise ValueError('finite range value required')
        return self._call(self._act, node, verb, value)

    def _act(self, node, verb, value):
        ctrl, rect = self._target(node)
        try:
            if verb in {'double_click', 'right_click', 'physical_click'}:
                self._check_deadline()
                self._click(rect, 'click' if verb == 'physical_click' else verb)
                return True
            if verb == 'focus':
                self._check_deadline()
                if ctrl.SetFocus() is False:
                    raise UIAError('focus refused')
            else:
                kind = {'click': 'invoke', 'invoke': 'invoke', 'toggle': 'toggle',
                        'expand': 'expand', 'collapse': 'expand', 'set_value': 'range', 'read': 'text'}[verb]
                pattern = self._pattern(ctrl, kind)
                if pattern is None:
                    if verb not in {'click', 'invoke', 'toggle', 'expand', 'collapse'}:
                        raise UIAError('pattern unavailable')
                    self._check_deadline()
                    self._click(rect, 'click')
                elif verb == 'read':
                    return self._text(ctrl, pattern, 65536)
                elif verb == 'set_value':
                    readonly = getattr(pattern, 'CurrentIsReadOnly', getattr(pattern, 'IsReadOnly', False))
                    minimum = getattr(pattern, 'CurrentMinimum', getattr(pattern, 'Minimum', float('-inf')))
                    maximum = getattr(pattern, 'CurrentMaximum', getattr(pattern, 'Maximum', float('inf')))
                    if readonly or not minimum <= float(value) <= maximum:
                        raise UIAError('range value refused')
                    self._check_deadline()
                    if pattern.SetValue(float(value)) is False:
                        raise UIAError('range value refused')
                else:
                    method = {'click': 'Invoke', 'invoke': 'Invoke', 'toggle': 'Toggle',
                              'expand': 'Expand', 'collapse': 'Collapse'}[verb]
                    self._check_deadline()
                    if getattr(pattern, method)() is False:
                        raise UIAError('pattern action refused')
            return True

        finally:
            if verb != 'read':
                self._cache.pop(node.hwnd, None)

    @staticmethod
    def _text(ctrl, pattern, max_chars):
        if getattr(ctrl, 'IsPassword', False):
            raise UIAError('password field')
        return pattern.DocumentRange.GetText(max_chars)

    def read_focused_text(self, max_chars=65536):
        if not 0 < max_chars <= 65536:
            raise ValueError('text read limit must be 1..65536')
        def read():
            hwnd = self._window()
            ctrl = self._get_backend().focused()
            if ctrl is None or getattr(ctrl, 'IsPassword', False):
                raise UIAError('focused text unavailable')
            pattern = self._pattern(ctrl, 'text')
            if pattern is None:
                raise UIAError('TextPattern unavailable')
            result = self._text(ctrl, pattern, max_chars)
            if hwnd != self._window():
                raise StaleTarget('foreground changed during text read')
            return result
        return self._call(read)

    def overlay_elements(self, *, loader=b._load_uia, foreground=b._get_foreground_control,
                         cached_root=b._cached_root, node_budget=8000, max_depth=64, time_budget=3.0,
                         target_hwnd=None):
        def enumerate_elements():
            # Compatibility hooks accept pure in-memory trees, never native COM.
            # Existing test/consumer fixtures can keep using the old provider seam.
            injected = loader is not b._load_uia or foreground is not b._get_foreground_control or cached_root is not b._cached_root
            if injected:
                auto = loader()
                if auto is None:
                    raise UIAError('provider unavailable')
                root = foreground(auto)
                if root is None:
                    raise UIAError('foreground unavailable')
                hwnd = getattr(root, 'NativeWindowHandle', 0)
                source = 'uia-walk'
                if isinstance(hwnd, int) and hwnd:
                    try:
                        cached = cached_root(auto, hwnd)
                        if cached is not None:
                            root, source = cached, 'uia-cache'
                    except Exception:
                        pass
                nodes, controls, stats = self._walk(root, hwnd, 0, node_budget=node_budget,
                                                   max_depth=max_depth, time_budget=time_budget)
            else:
                snap = self._snapshot(bool(target_hwnd), target_hwnd)
                nodes, stats = snap.nodes, dict(snap.stats)
                controls, source = None, stats['source']
            provider_controls = controls if injected else self._cache[snap.hwnd][2]
            selected = overlay_target_nodes(nodes, lambda node: _overlay_actionable(
                provider_controls[node.index], node.control_type))
            candidates = []
            for node in selected:
                control = controls[node.index] if injected else ControlProxy(self, node)
                if isinstance(control, b._CachedNode):
                    control = control.control()
                candidates.append(dict(control=control, prect=node.rect, rect=node.rect, name=node.name,
                                       type=node.control_type, in_document=node.in_document))
            results = b._apply_cap(candidates)
            stats.update(source=source, count=len(results), candidates=len(candidates),
                         in_document=sum(c['in_document'] for c in candidates), capped=len(candidates)>99)
            return results, stats
        return self._call(enumerate_elements)


_singleton = None
_singleton_lock = threading.Lock()


def get_service():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = UIAService()
            thread_registry.on_shutdown(_singleton.close)
        return _singleton

