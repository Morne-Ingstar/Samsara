"""604: real shared UIA service with an in-memory tree; no desktop effects."""
import threading
from types import SimpleNamespace

import pytest

from plugins.commands import click_by_name as cb, show_numbers as sn
from samsara import execution_policy, plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.commands import CommandExecutor
from samsara.uia import UIAService
from samsara.uia.backend import Rect


class Control:
    def __init__(self, name, rect=(10, 20, 110, 50), children=()):
        self.Name, self.rect, self.children = name, rect, list(children)
        self.ControlTypeName = 'ButtonControl'
        self.IsEnabled, self.IsOffscreen = True, False
        self.calls = []

    @property
    def BoundingRectangle(self):
        return Rect(*self.rect)

    def GetChildren(self):
        return self.children

    def pattern(self, kind):
        return SimpleNamespace(**{method: lambda method=method: self.calls.append(method)
                                  for method in ('Invoke', 'Toggle', 'Expand', 'Collapse')})

    def SetFocus(self):
        self.calls.append('SetFocus')


@pytest.fixture
def rig(monkeypatch):
    from plugins.commands import app_verbs
    for module in (cb, sn, app_verbs):
        plugin_commands._reinstall_module_commands(module)
    cb.cancel()
    leaf = Control('Pri-vacy & Security')
    root = Control('', (0, 0, 900, 700), [leaf])
    host = SimpleNamespace(hwnd=21)
    backend = SimpleNamespace(foreground=lambda: host.hwnd,
                              root=lambda hwnd: (root, 'fake'), close=lambda: None)
    service = UIAService(lambda: backend, click=lambda *args: pytest.fail('unexpected physical click'))
    app = SimpleNamespace(config={'command_packs': {'accessibility': True}},
                          command_matching_enabled=True, command_mode_active=True)
    state = SimpleNamespace(app=app, service=service, host=host, root=root, leaf=leaf,
                            feedback=[], shown=[], hidden=[], timers=[])
    monkeypatch.setattr(cb, '_foreground', lambda: host.hwnd)
    monkeypatch.setattr(cb, 'get_service', lambda: service)
    monkeypatch.setattr(cb, '_feedback', lambda app, msg: state.feedback.append(msg))
    monkeypatch.setattr(cb, '_show', state.shown.append)
    monkeypatch.setattr(cb, '_hide', state.hidden.append)

    def timer(name, delay, fn, **kwargs):
        state.timers.append(fn)
        return SimpleNamespace(cancel=lambda: None)
    monkeypatch.setattr(cb.thread_registry, 'timer', timer)
    yield state
    cb.cancel()
    service.close()


@pytest.mark.parametrize('query', ['priv', 'PRI-VACY!', 'privacy security'])
def test_partial_case_punctuation_and_multiword_match_acts_once(rig, query):
    assert cb.try_named(rig.app, query) is True
    assert rig.leaf.calls == ['Invoke']
    assert not rig.shown and not rig.feedback


@pytest.mark.parametrize('handler,method', [
    (cb.toggle, 'Toggle'), (cb.expand, 'Expand'), (cb.collapse, 'Collapse'),
    (cb.focus_control, 'SetFocus'),
])
def test_semantic_verbs_use_shared_service(rig, handler, method):
    assert handler(rig.app, 'priv') is True
    assert rig.leaf.calls == [method]


def duplicates(rig):
    second = Control('Privacy tools', (200, 20, 300, 50))
    other = Control('Unrelated', (400, 20, 500, 50))
    rig.root.children += [second, other]
    return second, other


def test_only_matching_nodes_numbered_and_next_number_uses_saved_verb(rig):
    second, other = duplicates(rig)
    cb.toggle(rig.app, 'priv')
    assert not rig.leaf.calls and not second.calls
    selection, = rig.shown
    assert [n.name for n in selection.nodes] == ['Pri-vacy & Security', 'Privacy tools']
    assert cb.selection_number('two', rig.app) == 2
    assert cb.choose_control(rig.app, '2') is True
    assert second.calls == ['Toggle'] and not other.calls
    assert not cb.active() and rig.hidden == [selection]
    cb.choose_control(rig.app, '2')
    assert second.calls == ['Toggle']


@pytest.mark.parametrize('text', ['pick 2', 'two unrelated', '2 then click', 'I have two', '2 1'])
def test_selection_requires_whole_bare_number(rig, text):
    duplicates(rig)
    cb.toggle(rig.app, 'priv')
    assert cb.selection_number(text, rig.app) is None
    assert not rig.leaf.calls


def test_no_match_has_one_short_message_including_what_was_heard(rig):
    cb.toggle(rig.app, 'missing')
    assert rig.feedback == ['No match: missing']
    assert not rig.leaf.calls and not rig.shown


def test_timeout_is_quiet_and_old_timeout_cannot_clear_new_selection(rig):
    duplicates(rig)
    cb.toggle(rig.app, 'priv')
    old_timeout = rig.timers[-1]
    cb.expand(rig.app, 'priv')
    old_timeout()
    assert cb.active()
    rig.timers[-1]()
    assert not cb.active() and not rig.feedback and not rig.leaf.calls


def test_focus_loss_during_discovery_never_acts(rig, monkeypatch):
    search = rig.service.search

    def switch(name):
        nodes = search(name)
        rig.host.hwnd = 99
        return nodes
    monkeypatch.setattr(rig.service, 'search', switch)
    cb.toggle(rig.app, 'priv')
    assert not rig.leaf.calls and not rig.shown
    assert rig.feedback == ['Window changed. Try again.']


def test_focus_loss_on_com_worker_between_dispatch_and_action_never_acts(rig, monkeypatch):
    act = rig.service.act

    def switch(*args):
        rig.host.hwnd = 99
        return act(*args)
    monkeypatch.setattr(rig.service, 'act', switch)
    cb.toggle(rig.app, 'priv')
    assert not rig.leaf.calls
    assert rig.feedback == ['Target changed or unavailable. Try again.']


def test_focus_loss_while_choosing_dismisses_quietly(rig):
    duplicates(rig)
    cb.toggle(rig.app, 'priv')
    rig.host.hwnd = 99
    cb.choose_control(rig.app, '1')
    assert not cb.active() and not rig.feedback and not rig.leaf.calls


def test_moved_duplicate_is_not_clicked(rig):
    second, _ = duplicates(rig)
    cb.toggle(rig.app, 'priv')
    second.rect = (300, 30, 400, 60)
    cb.choose_control(rig.app, '2')
    assert not second.calls and not cb.active()
    assert rig.feedback == ['Target changed or unavailable. Try again.']


def test_click_handler_reuses_uia_and_retains_ocr_fallback(rig, monkeypatch):
    from samsara import screen_ocr
    monkeypatch.setattr(sn, 'grid_active', lambda: False)
    monkeypatch.setattr(sn, 'is_overlay_active', lambda: False)
    monkeypatch.setattr(sn, '_foreground_info', lambda hwnd: {})
    monkeypatch.setattr(screen_ocr, 'foreground_hwnd', lambda: rig.host.hwnd)
    ocr = []
    def read(hwnd):
        ocr.append(hwnd)
        raise RuntimeError('fake OCR unavailable')
    monkeypatch.setattr(screen_ocr, 'read_foreground', read)
    monkeypatch.setattr(sn, '_speak', lambda *args: None)
    monkeypatch.setattr(sn, '_report_chip', lambda *args: None)
    assert sn.handle_click(rig.app, 'priv') is True
    assert rig.leaf.calls == ['Invoke'] and not ocr
    assert sn.handle_click(rig.app, 'missing') is DispatchState.FAILED
    assert ocr == [21]


def executor(app):
    result = CommandExecutor.__new__(CommandExecutor)
    result._app, result.commands = app, {}
    result._matcher_lock = threading.RLock()
    result._matcher = CommandMatcher()
    result._matcher.load_plugins({k: v for k, v in plugin_commands._REGISTRY.items()
                                 if v['source'] in {cb.__name__, sn.__name__}})
    result._matcher.freeze()
    return result


def test_bare_number_routes_through_policy_before_action(rig, monkeypatch):
    duplicates(rig)
    cb.toggle(rig.app, 'priv')
    ex = executor(rig.app)
    seen = []

    def deny(inv, **kwargs):
        seen.append(inv)
        return execution_policy.Denied('stale')
    monkeypatch.setattr(execution_policy, 'authorize', deny)
    result = ex.process_text('two', rig.app, force_commands=True)
    assert result.state is DispatchState.REJECTED
    assert len(seen) == 1 and seen[0].command_id == 'choose control'
    assert not rig.leaf.calls


def test_registered_phrases_do_not_replace_click_or_app_focus(rig):
    from plugins.commands import app_verbs
    assert plugin_commands._REGISTRY['click']['func'] is sn.handle_click
    assert plugin_commands._REGISTRY['focus']['func'] is app_verbs.handle_focus
    ex = executor(rig.app)
    entry, remainder = ex._matcher.match('click priv')
    assert entry.handler is sn.handle_click and remainder == 'priv'
    result = ex.process_text('toggle priv', rig.app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert rig.leaf.calls == ['Toggle']
    second, _ = duplicates(rig)
    assert ex.process_text('toggle priv', rig.app, force_commands=True).state is DispatchState.COMPLETED
    assert ex.process_text('two', rig.app, force_commands=True).state is DispatchState.COMPLETED
    assert second.calls == ['Toggle'] and not cb.active()


def test_explicit_control_focus_dispatch_preserves_app_focus(rig, monkeypatch):
    from plugins.commands import app_verbs as av
    monkeypatch.setattr(av, 'do_focus', lambda name: pytest.fail('control focus reached app resolver'))
    ex = executor(rig.app)
    assert ex.process_text('focus control priv', rig.app, force_commands=True).state is DispatchState.COMPLETED
    assert rig.leaf.calls == ['SetFocus']


def test_match_renderer_contains_no_unmatched_labels(monkeypatch):
    from samsara.ui import numbers_overlay_qt as nov
    windows = []
    screen = SimpleNamespace(name=lambda: 'fake')
    monkeypatch.setattr(nov, 'QApplication', SimpleNamespace(screenAt=lambda p: screen))
    monkeypatch.setattr(nov, 'phys_to_logical', lambda x, y: (x, y))

    class Window:
        def __init__(self, labels, screen):
            windows.append(labels)
        def hide(self):
            pass
        def deleteLater(self):
            pass
    monkeypatch.setattr(nov, 'NumbersOverlayWindow', Window)
    monkeypatch.setattr(nov, 'QTimer', lambda: SimpleNamespace(
        setInterval=lambda n: None, timeout=SimpleNamespace(connect=lambda f: None),
        stop=lambda: None, deleteLater=lambda: None))
    overlay = nov.MatchNumbersOverlay([SimpleNamespace(rect=(10, 20, 30, 40)),
                                       SimpleNamespace(rect=(50, 60, 70, 80))], lambda: True, lambda: None)
    assert windows == [[[10, 20, 28, 22, '1'], [50, 60, 28, 22, '2']]]
    overlay.hide()
    overlay.hide()  # Foreground cancellation and the Qt poll may both hide it.
