"""602: shared UIA behavior using in-memory providers; no desktop effects."""
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from samsara.uia import ControlProxy, StaleTarget, UIAError, UIAService, UIATimeout
from samsara.uia.backend import Rect


class FakeControl:
    def __init__(self, owner, name='', kind='ButtonControl', children=(), rect=(10, 10, 80, 40)):
        self.owner, self.name, self.kind = owner, name, kind
        self.children, self.rect = list(children), rect
        self.enabled, self.offscreen, self.password = True, False, False
        self.patterns = {}
        self.calls = []

    def check(self):
        assert threading.get_ident() == self.owner[0], 'provider escaped its apartment'

    @property
    def Name(self):
        self.check()
        return self.name

    @property
    def ControlTypeName(self):
        self.check()
        return self.kind

    @property
    def BoundingRectangle(self):
        self.check()
        return Rect(*self.rect)

    @property
    def IsEnabled(self):
        self.check()
        return self.enabled

    @property
    def IsOffscreen(self):
        self.check()
        return self.offscreen

    @property
    def IsPassword(self):
        self.check()
        return self.password

    def GetChildren(self):
        self.check()
        return self.children

    def pattern(self, kind):
        self.check()
        return self.patterns.get(kind)

    def SetFocus(self):
        self.check()
        self.calls.append('focus')


class FakeBackend:
    def __init__(self, owner, root, leaf):
        owner.append(threading.get_ident())
        self.owner, self.tree, self.leaf = owner, root, leaf
        self.hwnd, self.builds, self.closed = 21, 0, False

    def foreground(self):
        assert threading.get_ident() == self.owner[0]
        return self.hwnd

    def root(self, hwnd):
        assert hwnd == self.hwnd
        self.builds += 1
        return self.tree, 'fake'

    def focused(self):
        return self.leaf

    def close(self):
        assert threading.get_ident() == self.owner[0]
        self.closed = True


@pytest.fixture
def harness():
    owner, backends, clicks = [], [], []
    leaf = FakeControl(owner, 'Pri-vacy & Security')
    root = FakeControl(owner, kind='WindowControl', children=[leaf], rect=(0, 0, 1200, 800))
    now = [0.0]

    def factory():
        backend = FakeBackend(owner, root, leaf)
        backends.append(backend)
        return backend

    service = UIAService(factory, clock=lambda: now[0], click=lambda rect, verb: clicks.append((rect, verb, threading.get_ident())))
    h = SimpleNamespace(service=service, leaf=leaf, root=root, owner=owner, backends=backends, clicks=clicks, now=now)
    yield h
    service.close()
    if backends:
        assert backends[0].closed


def test_import_and_construction_are_inert(harness):
    assert harness.service._thread is None
    assert harness.backends == []


def test_snapshot_is_data_and_all_provider_access_stays_on_one_worker(harness):
    snap = harness.service.snapshot()
    assert snap.nodes[1].name == 'Pri-vacy & Security'
    assert harness.owner != [threading.get_ident()]
    assert not hasattr(snap.nodes[1], 'control')
    assert harness.service.snapshot() is snap
    assert harness.backends[0].builds == 1
    harness.now[0] = 0.76
    assert harness.service.snapshot().generation != snap.generation
    assert harness.backends[0].builds == 2


def test_explicit_invalidation_rejects_old_tokens(harness):
    node = harness.service.search('privacy')[0]
    harness.service.invalidate(node.hwnd)
    with pytest.raises(StaleTarget):
        harness.service.act(node, 'click')
    assert harness.clicks == []


def test_ttl_and_refresh_reject_old_generation(harness):
    node = harness.service.search('priv')[0]
    harness.service.snapshot(refresh=True)
    with pytest.raises(StaleTarget):
        harness.service.geometry(node)


def test_switch_away_and_back_rebuilds(harness):
    first = harness.service.snapshot()
    backend = harness.backends[0]
    backend.hwnd = 99
    assert harness.service.snapshot().hwnd == 99
    backend.hwnd = 21
    assert harness.service.snapshot().generation != first.generation


def test_single_word_search_folds_case_punctuation_and_returns_all_duplicates(harness):
    duplicate = FakeControl(harness.owner, 'Privacy tools')
    harness.root.children.append(duplicate)
    assert len(harness.service.search('PRiV')) == 2
    assert len(harness.service.search('PRI-VACY!')) == 2
    assert harness.service.search('missing') == ()
    harness.leaf.offscreen = True
    harness.service.invalidate()
    assert len(harness.service.search('priv')) == 1


@pytest.mark.parametrize('query', ['', '  ', 'two words', '---'])
def test_invalid_query_does_not_touch_provider(harness, query):
    with pytest.raises(ValueError):
        harness.service.search(query)
    assert harness.backends == []


@pytest.mark.parametrize('verb,kind,method', [
    ('invoke', 'invoke', 'Invoke'), ('click', 'invoke', 'Invoke'),
    ('toggle', 'toggle', 'Toggle'), ('expand', 'expand', 'Expand'),
    ('collapse', 'expand', 'Collapse'),
])
def test_semantic_verbs_use_patterns(harness, verb, kind, method):
    calls = []
    harness.leaf.patterns[kind] = SimpleNamespace(**{method: lambda: calls.append(threading.get_ident())})
    node = harness.service.search('priv')[0]
    assert harness.service.act(node, verb)
    assert calls == harness.owner
    assert harness.clicks == []
    with pytest.raises(StaleTarget):
        harness.service.act(node, verb)


def test_focus_uses_setfocus(harness):
    assert harness.service.act(harness.service.search('priv')[0], 'focus')
    assert harness.leaf.calls == ['focus']


@pytest.mark.parametrize('verb', ['click', 'invoke', 'toggle', 'expand', 'collapse', 'right_click', 'double_click'])
def test_click_fallback_uses_validated_bounds_on_worker(harness, verb):
    assert harness.service.act(harness.service.search('priv')[0], verb)
    assert harness.clicks == [(harness.leaf.rect, verb if verb in {'right_click', 'double_click'} else 'click', harness.owner[0])]


@pytest.mark.parametrize('verb', ['set_value', 'read'])
def test_missing_non_emulatable_patterns_fail_without_clicking(harness, verb):
    with pytest.raises(UIAError):
        harness.service.act(harness.service.search('priv')[0], verb, 5)
    assert harness.clicks == []


def test_provider_error_never_retries_with_physical_click(harness):
    def fail():
        raise OSError('provider failed after dispatch')
    harness.leaf.patterns['invoke'] = SimpleNamespace(Invoke=fail)
    with pytest.raises(UIAError):
        harness.service.act(harness.service.search('priv')[0], 'invoke')
    assert harness.clicks == []


def test_range_value_checks_bounds_readonly_and_finite(harness):
    calls = []
    pattern = SimpleNamespace(IsReadOnly=False, Minimum=0, Maximum=10, SetValue=lambda value: calls.append(value))
    harness.leaf.patterns['range'] = pattern
    node = harness.service.search('priv')[0]
    for value in (-1, 11):
        with pytest.raises(UIAError):
            harness.service.act(harness.service.search('priv')[0], 'set_value', value)
    for value in (None, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            harness.service.act(node, 'set_value', value)
    pattern.IsReadOnly = True
    with pytest.raises(UIAError):
        harness.service.act(harness.service.search('priv')[0], 'set_value', 5)
    pattern.IsReadOnly = False
    assert harness.service.act(harness.service.search('priv')[0], 'set_value', 5)
    assert calls == [5.0]


@pytest.mark.parametrize('change', ['moved', 'disabled', 'offscreen', 'foreground', 'forged'])
def test_stale_and_forged_targets_never_click(harness, change):
    node = harness.service.search('priv')[0]
    if change == 'moved':
        harness.leaf.rect = (11, 10, 80, 40)
    elif change == 'disabled':
        harness.leaf.enabled = False
    elif change == 'offscreen':
        harness.leaf.offscreen = True
    elif change == 'foreground':
        harness.backends[0].hwnd = 22
    else:
        node = replace(node, index=-1)
    with pytest.raises(StaleTarget):
        harness.service.act(node, 'click')
    assert harness.clicks == []


def test_focused_text_uses_textpattern_and_respects_limit(harness):
    calls = []
    def read(limit):
        harness.leaf.check()
        calls.append(limit)
        return 'fixture'[:limit]
    harness.leaf.patterns['text'] = SimpleNamespace(DocumentRange=SimpleNamespace(GetText=read))
    assert harness.service.read_focused_text(4) == 'fixt'
    assert calls == [4]
    harness.leaf.password = True
    with pytest.raises(UIAError):
        harness.service.read_focused_text()
    assert calls == [4]


def test_named_read_rejects_password_and_never_clicks(harness):
    harness.leaf.password = True
    harness.leaf.patterns['text'] = SimpleNamespace(DocumentRange=SimpleNamespace(GetText=lambda n: pytest.fail('password read')))
    with pytest.raises(UIAError):
        harness.service.act(harness.service.search('priv')[0], 'read')
    assert harness.clicks == []


def test_overlay_uses_same_cached_snapshot_and_safe_proxy(harness):
    snap = harness.service.snapshot()
    elements, stats = harness.service.overlay_elements()
    assert len(elements) == 1
    proxy = elements[0]['control']
    assert isinstance(proxy, ControlProxy)
    assert proxy._node == snap.nodes[1]
    assert harness.backends[0].builds == 1
    assert proxy.BoundingRectangle.width() == 70
    assert stats['nodes'] == 2
    proxy.Click()
    assert len(harness.clicks) == 1


def test_dead_provider_timeout_does_not_run_a_queued_action():
    started, release = threading.Event(), threading.Event()
    service = UIAService(lambda: pytest.fail('not needed'), timeout=0.05)
    def block():
        started.set()
        release.wait(2)
    try:
        with pytest.raises(UIATimeout):
            service._call(block)
        assert started.is_set()
        effects = []
        with pytest.raises(UIATimeout):
            service._call(lambda: effects.append('late'))
        release.set()
        service._call(lambda: None)
        assert effects == []
    finally:
        release.set()
        service.close()
    with pytest.raises(UIAError):
        service.snapshot()


def test_unavailable_child_does_not_hide_siblings(harness):
    class Broken(FakeControl):
        @property
        def Name(self):
            raise OSError('unavailable')
    harness.root.children.insert(0, Broken(harness.owner))
    assert len(harness.service.search('priv')) == 1
    assert dict(harness.service.snapshot().stats)['nodes'] == 3


def test_numbered_proxy_keeps_physical_modifier_click_semantics(harness):
    harness.leaf.patterns['invoke'] = SimpleNamespace(Invoke=lambda: pytest.fail('numbered clicks must preserve held modifier keys'))
    elements, _ = harness.service.overlay_elements()
    elements[0]['control'].Click(simulateMove=False)
    assert harness.clicks[0][:2] == (harness.leaf.rect, 'click')


def test_failed_pattern_invalidates_proxy_before_outer_fallback(harness):
    def fail():
        raise OSError('provider may already have executed')
    harness.leaf.patterns['invoke'] = SimpleNamespace(Invoke=fail)
    node = harness.service.search('priv')[0]
    with pytest.raises(UIAError):
        harness.service.act(node, 'invoke')
    with pytest.raises(StaleTarget):
        harness.service.geometry(node)
    assert harness.clicks == []


def test_slow_pattern_lookup_expires_before_any_side_effect(harness):
    import time
    service = harness.service
    service._timeout = 0.03
    done = threading.Event()
    def slow_pattern(kind):
        time.sleep(0.08)
        done.set()
        return SimpleNamespace(Invoke=lambda: pytest.fail('late action'))
    harness.leaf.pattern = slow_pattern
    node = service.search('priv')[0]
    with pytest.raises(UIATimeout):
        service.act(node, 'invoke')
    assert done.wait(1)
    service._timeout = 1
    service._call(lambda: None)
    assert harness.clicks == []


def test_native_cached_and_live_properties_and_children_use_own_backend():
    from samsara.uia.backend import NativeNode
    owner = SimpleNamespace(names={50000: 'ButtonControl'})
    child = SimpleNamespace(CachedControlType=50000)
    raw = SimpleNamespace(
        CachedControlType=50000, CachedName='fixture', CachedBoundingRectangle=Rect(1, 2, 30, 40),
        CachedIsEnabled=1, CachedIsOffscreen=0, CurrentName='updated', CurrentIsPassword=0,
        GetCachedChildren=lambda: SimpleNamespace(Length=1, GetElement=lambda i: child))
    node = NativeNode(owner, raw, True)
    assert node.ControlTypeName == 'ButtonControl'
    assert node.Name == 'fixture'
    assert node.IsEnabled and not node.IsOffscreen and not node.IsPassword
    assert node.BoundingRectangle.width() == 29
    assert node.GetChildren()[0].element is child
    assert node.live().Name == 'updated'


def test_native_buildcache_and_live_fallback_share_the_same_client():
    from samsara.uia.backend import WindowsBackend
    requests = []
    class Request:
        def __init__(self):
            self.ids = []
        def AddProperty(self, pid):
            self.ids.append(pid)
    def cache():
        request = Request()
        requests.append(request)
        return request
    raw = object()
    backend = WindowsBackend.__new__(WindowsBackend)
    backend.client = SimpleNamespace(CreateCacheRequest=cache, RawViewCondition='raw',
                                     ElementFromHandleBuildCache=lambda hwnd, request: raw,
                                     ElementFromHandle=lambda hwnd: raw)
    node, source = backend.root(31)
    assert source == 'uia-cache' and node.element is raw and node.cached
    request = requests[0]
    assert request.ids == [30003, 30001, 30010, 30022, 30005]
    assert request.TreeScope == 7 and request.TreeFilter == 'raw' and request.AutomationElementMode == 1
    def unavailable(hwnd, request):
        raise OSError('cache unsupported')
    backend.client.ElementFromHandleBuildCache = unavailable
    node, source = backend.root(31)
    assert source == 'uia-walk' and node.element is raw and not node.cached


@pytest.mark.parametrize('kind,interface,pid', [
    ('invoke', 'IUIAutomationInvokePattern', 10000),
    ('toggle', 'IUIAutomationTogglePattern', 10015),
    ('expand', 'IUIAutomationExpandCollapsePattern', 10005),
    ('range', 'IUIAutomationRangeValuePattern', 10003),
    ('text', 'IUIAutomationTextPattern', 10014),
])
def test_native_patterns_query_the_requested_interface(kind, interface, pid):
    from samsara.uia.backend import NativeNode
    requested = []
    pattern = object()
    def get_pattern(actual):
        requested.append(actual)
        return SimpleNamespace(QueryInterface=lambda actual_interface: pattern if actual_interface == interface else None)
    backend = SimpleNamespace(core=SimpleNamespace(**{interface: interface}))
    node = NativeNode(backend, SimpleNamespace(GetCurrentPattern=get_pattern))
    assert node.pattern(kind) is pattern
    assert requested == [pid]

    class Unsupported(Exception):
        hresult = -2147220992  # UIA_E_NOTSUPPORTED (0x80040200)
    def unsupported(actual):
        raise Unsupported()
    node.element.GetCurrentPattern = unsupported
    assert node.pattern(kind) is None
    def unavailable(actual):
        raise OSError('unavailable target')
    node.element.GetCurrentPattern = unavailable
    with pytest.raises(OSError):
        node.pattern(kind)
