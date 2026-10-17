"""605: taskbar discovery and policy-gated bare labels, without desktop effects."""
import sys
import threading
from types import SimpleNamespace

import pytest

from plugins.commands import show_numbers as sn, click_by_name as cb
from samsara import execution_policy, plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.commands import CommandExecutor
from samsara.uia import UIAService
from samsara.uia.backend import Rect


class Control:
    ControlTypeName = 'ButtonControl'
    IsEnabled, IsOffscreen = True, False
    Name = 'taskbar item'

    def __init__(self, rect, children=()):
        self.rect, self.children = rect, children

    @property
    def BoundingRectangle(self):
        return Rect(*self.rect)

    def GetChildren(self):
        return self.children


@pytest.fixture
def rig(monkeypatch):
    cb.cancel()
    host = SimpleNamespace(hwnd=21)
    leaf = Control((20, 710, 90, 750))
    root = Control((0, 700, 1000, 760), [leaf])
    calls, effects = [], []
    def read(hwnd):
        calls.append((hwnd, threading.get_ident()))
        return root, 'fake'
    service = UIAService(lambda: SimpleNamespace(
        foreground=lambda: host.hwnd, root=read, close=lambda: None),
        click=lambda rect, verb: effects.append((rect, verb)))
    app = SimpleNamespace(config={'command_packs': {'accessibility': True}},
                          command_mode_active=True, command_matching_enabled=True)
    shown, spoken = [], []
    monkeypatch.setattr(sn, 'get_service', lambda: service)
    monkeypatch.setattr(sn, '_foreground_info', lambda hwnd: {
        'hwnd': hwnd, 'rect': root.rect, 'kind': 'native'})
    monkeypatch.setattr(sn, '_speak', lambda app, text: spoken.append(text))
    monkeypatch.setattr(sn, 'grid_active', lambda: False)
    monkeypatch.setattr(sn, '_cancel_dismiss_timer', lambda: None)
    monkeypatch.setattr(sn.qt_runtime, 'ensure_started', lambda: None)
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: fn())
    def draw(app, elements, caption='', fg_info=None):
        shown.append((elements, fg_info))
        sn._elements[:] = [e['control'] for e in elements]
    monkeypatch.setattr(sn, '_draw_overlay', draw)
    monkeypatch.setattr(sn, '_hide_overlay_qt', lambda: sn._elements.clear())
    monkeypatch.setattr(sn, '_perform_click', lambda ctrl, verb, keys=frozenset():
                        getattr(ctrl, {'single': 'Click', 'right': 'RightClick',
                                       'double': 'DoubleClick'}[verb])() is None)
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        GetForegroundWindow=lambda: host.hwnd, FindWindow=lambda cls, name: 99,
        IsWindowVisible=lambda hwnd: True))
    sn._elements.clear()
    sn._dom_active = False
    sn._taskbar_mode = False
    for module in (sn, cb):
        plugin_commands._reinstall_module_commands(module)
    yield SimpleNamespace(app=app, service=service, host=host, leaf=leaf,
                          shown=shown, spoken=spoken, calls=calls, effects=effects)
    sn._elements.clear()
    sn._dom_active = False
    sn._taskbar_mode = False
    cb.cancel()
    service.close()


def executor(app):
    ex = CommandExecutor.__new__(CommandExecutor)
    ex._app, ex.commands = app, {}
    ex._matcher_lock = threading.RLock()
    ex._matcher = CommandMatcher()
    ex._matcher.load_plugins({k: v for k, v in plugin_commands._REGISTRY.items()
                             if v['source'] in {sn.__name__, cb.__name__}})
    ex._matcher.freeze()
    return ex


@pytest.mark.parametrize('phrase,verb', [('two', 'click'), ('right click 2', 'right_click'),
                                       ('double click 2', 'double_click'),
                                       ('right-click 2', 'right_click'),
                                       ('double-click 2', 'double_click')])
def test_taskbar_dispatch_uses_shared_worker_and_existing_clicks(rig, phrase, verb):
    ex = executor(rig.app)
    assert ex.process_text('show numbers on taskbar', rig.app, force_commands=True).state is DispatchState.COMPLETED
    elements, fg = rig.shown[0]
    assert fg['hwnd'] == 21 and fg['rect'] == (0, 700, 1000, 760)
    assert {hwnd for hwnd, tid in rig.calls} == {99}
    assert all(tid != threading.get_ident() for hwnd, tid in rig.calls)
    assert rig.host.hwnd == 21 and not rig.effects
    # The synthetic root is clickable too; label 2 is the leaf.
    assert ex.process_text(phrase, rig.app, force_commands=True).state is DispatchState.COMPLETED
    assert rig.effects == [(rig.leaf.rect, verb)]
    assert not sn.is_overlay_active()


def test_bare_number_passes_policy_and_hidden_number_is_not_captured(rig, monkeypatch):
    ex = executor(rig.app)
    assert sn.selection_number('two') is None
    sn.handle_taskbar_numbers(rig.app, '')
    seen = []
    def deny(inv, **kwargs):
        seen.append(inv.command_id)
        return execution_policy.Denied('test denial')
    monkeypatch.setattr(execution_policy, 'authorize', deny)
    assert ex.process_text('two', rig.app, force_commands=True).state is DispatchState.REJECTED
    assert seen == ['click'] and not rig.effects


@pytest.mark.parametrize('text', ['2 1', 'two unrelated', 'I have two', 'two one',
                                  'twenty twenty', '100', 'number 2', '2 then click'])
def test_bare_selection_accepts_only_whole_number(rig, text):
    sn.handle_taskbar_numbers(rig.app, '')
    assert sn.selection_number(text) is None
    assert not rig.effects


@pytest.mark.parametrize('text,value', [('7', 7), ('seven.', 7), ('thirty seven', 37), ('99', 99)])
def test_bare_selection_words_and_digits(rig, text, value):
    sn.handle_taskbar_numbers(rig.app, '')
    assert sn.selection_number(text) == value


def test_taskbar_focus_change_before_click_refuses(rig):
    sn.handle_taskbar_numbers(rig.app, '')
    rig.host.hwnd = 55
    sn.handle_click(rig.app, '2')
    assert not rig.effects


def test_moved_taskbar_control_refuses(rig):
    sn.handle_taskbar_numbers(rig.app, '')
    rig.leaf.rect = (30, 710, 100, 750)
    sn.handle_right_click(rig.app, '2')
    assert not rig.effects


def test_focus_change_during_discovery_never_draws(rig, monkeypatch):
    read = rig.service.overlay_elements
    def changed(**kwargs):
        result = read(**kwargs)
        rig.host.hwnd = 55
        return result
    monkeypatch.setattr(rig.service, 'overlay_elements', changed)
    assert sn.handle_taskbar_numbers(rig.app, '') is DispatchState.FAILED
    assert not rig.shown and not rig.effects


def test_unavailable_taskbar_clears_previous_labels_and_reports(rig, monkeypatch):
    sn.handle_taskbar_numbers(rig.app, '')
    monkeypatch.setattr(sys.modules['win32gui'], 'FindWindow', lambda *args: 0)
    assert sn.handle_taskbar_numbers(rig.app, '') is DispatchState.FAILED
    assert not sn.is_overlay_active()
    assert rig.spoken == ['No clickable taskbar controls found.']


def test_refresh_preserves_taskbar_target_and_hide_clears_it(rig):
    sn.handle_taskbar_numbers(rig.app, '')
    sn.handle_refresh_numbers(rig.app, '')
    assert [hwnd for hwnd, tid in rig.calls] == [99, 99]
    sn.handle_hide_numbers(rig.app, '')
    assert not sn._taskbar_mode and sn.selection_number('two') is None


def test_grid_keeps_number_ownership(rig, monkeypatch):
    sn.handle_taskbar_numbers(rig.app, '')
    monkeypatch.setattr(sn, 'grid_active', lambda: True)
    assert sn.selection_number('two') is None


def test_dom_bare_label_and_right_click_reuse_bridge(rig, monkeypatch):
    sent = []
    sn._dom_active, sn._dom_hint_count = True, 4
    monkeypatch.setattr(sn.browser_bridge, 'get_bridge', lambda: SimpleNamespace(
        send_selection=lambda *args, **kwargs: sent.append((args, kwargs)) or True,
        send_dismiss=lambda: None))
    assert sn.selection_number('three') == 3
    ex = executor(rig.app)
    assert ex.process_text('three', rig.app, force_commands=True).state is DispatchState.COMPLETED
    assert sent[0][0] == (3, 'click')
    assert not sn._dom_active


def test_overlay_show_and_reshow_do_not_steal_qt_focus(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget
    from samsara.ui.numbers_overlay_qt import NumbersOverlayWindow
    app = QApplication.instance()
    assert app is not None
    host = QWidget()
    host.show()
    QApplication.setActiveWindow(host)
    app.processEvents()
    before = QApplication.activeWindow()
    overlay = NumbersOverlayWindow([[20, 20, 28, 22, '1']], app.primaryScreen())
    try:
        for _ in range(2):
            overlay.show()
            overlay.raise_()
            app.processEvents()
            assert QApplication.activeWindow() is before
            assert overlay.windowFlags() & Qt.WindowStaysOnTopHint
            assert overlay.windowFlags() & Qt.WindowDoesNotAcceptFocus
            assert overlay.windowFlags() & Qt.WindowTransparentForInput
            assert overlay.testAttribute(Qt.WA_ShowWithoutActivating)
            assert overlay.testAttribute(Qt.WA_TranslucentBackground)
            overlay.hide()
    finally:
        overlay.hide()
        overlay.deleteLater()
        host.hide()
        host.deleteLater()
