"""Real click/name/key/wake routes with an inert UIA provider and effect sinks."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import threading

import pytest

from plugins.commands import ask_ollama, click_by_name as cb, show_numbers as sn
from samsara import click_effect_policy as clicks, effect_policy as effects
from samsara import execution_policy as ep, plugin_commands
from samsara.command_registry import CommandMatcher, DispatchState
from samsara.commands import CommandExecutor
from samsara.uia import UIAService, ControlProxy
from samsara.uia.backend import Rect
from tests.test_cancel_window_69 import FakeTimer


class Control:
    def __init__(self, name, kind='ButtonControl', children=(), rect=(10, 20, 110, 50)):
        self.Name, self.ControlTypeName = name, kind
        self.children, self.rect = list(children), rect
        self.IsEnabled, self.IsOffscreen, self.IsPassword = True, False, False
        self.value, self.calls = '', []

    @property
    def BoundingRectangle(self):
        return Rect(*self.rect)

    def GetChildren(self):
        return self.children

    def GetRuntimeId(self):
        return [id(self)]

    def pattern(self, kind):
        if kind == 'value':
            return SimpleNamespace(Value=self.value)
        return SimpleNamespace(**{method: lambda method=method: self.calls.append(method)
                                  for method in ('Invoke', 'Toggle', 'Expand', 'Collapse')})


@pytest.fixture
def rig(monkeypatch):
    registry = dict(plugin_commands._REGISTRY)
    modules = {key: dict(value) for key, value in plugin_commands._MODULE_ENTRIES.items()}
    for module in (sn, cb):
        plugin_commands._reinstall_module_commands(module)
    ask_ollama.clear_pending_action()
    clicks._request.value = None
    ep._speech.last_onset = ep._speech.last_dispatch_start = 0
    cb.cancel()
    send = Control('Send')
    recipient = Control('To', 'EditControl', rect=(10, 60, 110, 90))
    recipient.value = 'synthetic-person-id'
    root = Control('Synthetic mail', 'WindowControl', [recipient, send], (0, 0, 800, 600))
    host = SimpleNamespace(hwnd=42)
    fired = []
    service = UIAService(lambda: SimpleNamespace(foreground=lambda: host.hwnd,
                         root=lambda hwnd: (root, 'inert'), close=lambda: None),
                         click=lambda rect, verb: fired.append((rect, verb)))
    app = SimpleNamespace(config={'command_packs': {'accessibility': True}, 'auto_paste': True},
                          command_matching_enabled=True, command_mode_active=True,
                          _ava_cmd_generation=7, _show_outcome_chip=Mock(), play_sound=Mock())
    target = [effects.SubmitTarget('email', 42, 'outlook.exe', 'composer-id', 'body-v1',
                                  'account-id', 'conversation-id', 'person-id', 'Synthetic recipient')]
    monkeypatch.setattr(effects, 'resolve_submit_target', lambda app=None: target[0])
    monkeypatch.setattr(ep, 'foreground_window_identity', lambda: {
        'hwnd': host.hwnd, 'process': target[0].process, 'pid': 7, 'title_hash': 'synthetic'})
    monkeypatch.setattr(ep, 'listen_for_confirmation', lambda *args: None)
    monkeypatch.setattr(cb, '_foreground', lambda: host.hwnd)
    monkeypatch.setattr(cb, 'get_service', lambda: service)
    monkeypatch.setattr(cb, '_feedback', lambda *args: None)
    monkeypatch.setattr(cb, '_hide', lambda *args: None)
    monkeypatch.setattr(cb, '_show', lambda *args: None)
    monkeypatch.setattr(sn, 'is_overlay_active', lambda: bool(sn._elements))
    monkeypatch.setattr(sn, 'grid_active', lambda: False)
    monkeypatch.setattr(sn, '_destroy_overlay', lambda app: sn._elements.clear())
    monkeypatch.setattr(sn, '_schedule_dismiss_timer', lambda *args: None)
    monkeypatch.setattr(sn, '_speak', lambda *args: None)
    monkeypatch.setattr(sn, '_report_chip', lambda *args, **kwargs: None)
    monkeypatch.setattr(sn, '_overlay_window', None)
    monkeypatch.setattr(sn, '_dom_active', False)
    monkeypatch.setattr(sn, '_foreground_info', lambda hwnd: {'hwnd': hwnd})
    from samsara import screen_ocr
    monkeypatch.setattr(screen_ocr, 'foreground_hwnd', lambda: host.hwnd)
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: None)
    ex = CommandExecutor.__new__(CommandExecutor)
    ex._app, ex.commands = app, {'submit': {'type': 'press', 'key': 'enter'}}
    ex._matcher_lock = threading.RLock()
    ex._matcher = CommandMatcher()
    ex._matcher.load_plugins({key: value for key, value in plugin_commands._REGISTRY.items()
                             if value['source'] in {sn.__name__, cb.__name__}})
    ex._matcher.freeze()
    app.command_executor = ex
    monkeypatch.setattr(ex, '_build_context', lambda app: SimpleNamespace())
    from samsara import commands
    monkeypatch.setattr(commands, 'get_handler', lambda kind: SimpleNamespace(
        execute=lambda *args: fired.append('key') or True))
    monkeypatch.setitem(sys.modules, 'pyautogui', SimpleNamespace(press=lambda key: fired.append('wake')))
    r = SimpleNamespace(app=app, ex=ex, send=send, recipient=recipient, root=root,
                        service=service, target=target, host=host, fired=fired)
    yield r
    service.close()
    cb.cancel()
    sn._elements.clear()
    ask_ollama.clear_pending_action()
    clicks._request.value = None
    plugin_commands._REGISTRY.clear()
    plugin_commands._REGISTRY.update(registry)
    plugin_commands._MODULE_ENTRIES.clear()
    plugin_commands._MODULE_ENTRIES.update(modules)


def numbered(rig):
    node = rig.service.search('send')[0]
    sn._elements[:] = [ControlProxy(rig.service, node)]
    return sn.handle_click(rig.app, '1')


def wake(rig):
    rig.app._wake_session_mode = 'focus_dictate'
    rig.app._wake_session_started_at = 12
    rig.app._wake_session_delivered_text = 'Synthetic test body'
    rig.app._wake_session_target_hwnd = 42
    rig.app._wake_session_target_process = rig.target[0].process
    rig.app.wake_dictation_buffer = []
    rig.app._emit_wake_trace = Mock()
    rig.app._end_wake_session = Mock()
    return effects.submit_wake_profile(rig.app)


@pytest.mark.parametrize('route', ['name', 'number', 'keyboard', 'wake'])
def test_identical_email_send_asks_once_without_timer_then_runs_once(rig, route):
    if route == 'name':
        result = rig.ex.process_text('click send', rig.app, force_commands=True)
    elif route == 'number':
        result = numbered(rig)
    elif route == 'keyboard':
        result = rig.ex.execute_canonical('submit', rig.app)
    else:
        result = wake(rig)
    op = ep.pending_operation()
    assert op is not None and not isinstance(op, ep.TimedOperation)
    assert op.handling == effects.CONFIRM
    assert not rig.send.calls and not rig.fired
    assert ep.cancel_window_for('click', rig.app) == 0
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert len(rig.send.calls) + len(rig.fired) == 1
    assert not op.approve(app=rig.app)
    assert len(rig.send.calls) + len(rig.fired) == 1


@pytest.mark.parametrize('field', ['account', 'conversation', 'recipient', 'revision', 'control', 'hwnd', 'process'])
def test_changed_destination_refuses_bound_click(rig, field):
    numbered(rig)
    rig.target[0] = replace(rig.target[0], **{field: 99 if field == 'hwnd' else 'changed'})
    assert ep.answer_pending(rig.app, 'yes').startswith('refused')
    assert not rig.fired and not rig.send.calls


def test_stale_overlay_cannot_target_new_recipient_before_a_question(rig):
    node = rig.service.search('send')[0]
    sn._elements[:] = [ControlProxy(rig.service, node)]
    rig.recipient.value = 'different-synthetic-person'
    with pytest.raises(Exception):
        numbered_node = sn._elements[0]._node
        clicks.uia_action(rig.app, rig.service, numbered_node, 'click')
    assert not rig.fired and not rig.send.calls and ep.pending_operation() is None


def test_com_worker_rechecks_recipient_after_final_policy_probe(rig, monkeypatch):
    cb.try_named(rig.app, 'send')
    bound = rig.service.act_bound
    def race(*args):
        rig.recipient.value = 'changed-on-worker-boundary'
        return bound(*args)
    monkeypatch.setattr(rig.service, 'act_bound', race)
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert not rig.fired and not rig.send.calls


@pytest.mark.parametrize('label,kind', [('Home', 'ButtonControl'), ('Next', 'ButtonControl'),
                                      ('query', 'EditControl'), ('tab', 'TabItemControl')])
def test_safe_navigation_is_immediate(rig, label, kind):
    rig.send.Name, rig.send.ControlTypeName = label, kind
    assert cb.try_named(rig.app, label) is True
    assert rig.send.calls == ['Invoke'] and ep.pending_operation() is None


@pytest.mark.parametrize('label', ['Pay', 'Delete', 'Publish', 'Install', 'Send email'])
def test_consequential_labels_override_ai_destination(rig, label):
    rig.send.Name = label
    rig.target[0] = replace(rig.target[0], kind='ai')
    cb.try_named(rig.app, label)
    assert ep.pending_operation().handling == effects.CONFIRM
    assert not rig.send.calls


def test_ai_send_is_immediate_but_still_bound(rig):
    rig.target[0] = replace(rig.target[0], kind='ai')
    assert cb.try_named(rig.app, 'send') is True
    assert rig.send.calls == ['Invoke'] and ep.pending_operation() is None


def test_private_send_has_one_onset_held_grace_and_send_now(rig, monkeypatch):
    rig.target[0] = replace(rig.target[0], kind='known_person')
    timers = []
    from samsara.runtime import thread_registry
    monkeypatch.setattr(thread_registry, 'timer', lambda name, delay, fn, **kw: FakeTimer(delay, fn, timers))
    cb.try_named(rig.app, 'send')
    op = ep.pending_operation()
    assert isinstance(op, ep.TimedOperation) and op.handling == effects.PRIVATE_SEND
    assert timers[-1].seconds == 1.0 and not rig.send.calls
    ep.note_speech_onset(rig.app)
    timers[0].fire()
    assert not rig.send.calls
    assert ep.answer_pending(rig.app, 'send now') == 'approved'
    assert rig.send.calls == ['Invoke']


@pytest.mark.parametrize('action', ['cancel', 'model', 'stale', 'pack', 'expired'])
def test_pending_guards_cannot_be_waived(rig, action):
    cb.try_named(rig.app, 'send')
    op = ep.pending_operation()
    if action == 'cancel':
        assert ep.answer_pending(rig.app, 'no') == 'rejected'
    elif action == 'model':
        assert not op.approve(app=rig.app, source=ep.ANSWER_MODEL)
    else:
        if action == 'stale':
            rig.app._ava_cmd_generation += 1
        elif action == 'pack':
            rig.app.config['command_packs']['accessibility'] = False
            rig.ex.command_availability = lambda cid: ('pack_disabled', 'accessibility')
        else:
            op.deadline = 0
        ep.answer_pending(rig.app, 'yes')
    assert not rig.send.calls and not rig.fired


@pytest.mark.parametrize('kind', ['email', 'ai', 'known_person'])
def test_double_send_is_one_activation(rig, kind):
    rig.target[0] = replace(rig.target[0], kind=kind)
    node = rig.service.search('send')[0]
    clicks.uia_action(rig.app, rig.service, node, 'double_click')
    if ep.pending_operation() is not None:
        ep.answer_pending(rig.app, 'send now' if kind == 'known_person' else 'yes')
    assert rig.send.calls == ['Invoke'] and not rig.fired


def test_ocr_snapshot_binds_all_visible_text(rig, monkeypatch):
    from samsara import screen_ocr
    snap = screen_ocr.Snapshot(42, (0, 0, 800, 600), (0, 0, 800, 600),
        (screen_ocr.Line((screen_ocr.Word('Send', (10, 20, 110, 50)),
                          screen_ocr.Word('Synthetic recipient', (10, 60, 110, 90)))),), 1)
    current = [snap]
    monkeypatch.setattr(screen_ocr, 'read_foreground', lambda *args: current[0])
    monkeypatch.setattr(screen_ocr, 'point_reaches_window', lambda *args: True)
    clicks.coordinate_action(rig.app, (60, 35), lambda: rig.fired.append('ocr'), label='Send', snapshot=snap)
    current[0] = replace(snap, lines=(screen_ocr.Line((screen_ocr.Word('Other recipient', (10, 60, 110, 90)),)),))
    assert ep.answer_pending(rig.app, 'yes').startswith('refused')
    assert not rig.fired


def test_browser_number_is_bound_to_one_hint_session(rig):
    live = [True]
    hints = [{'binding': 'synthetic-binding', 'kind': 'button'}]
    result = clicks.dom_action(rig.app, hints, 1, 'single', frozenset(),
                               lambda: live[0], lambda *args: rig.fired.append('dom'))
    assert result.state is DispatchState.QUEUED
    live[0] = False
    assert ep.answer_pending(rig.app, 'yes').startswith('refused')
    assert not rig.fired


@pytest.mark.parametrize('route', ['cursor', 'grid'])
def test_pointer_activation_uses_the_same_email_gate(rig, monkeypatch, route):
    from samsara import uia
    monkeypatch.setattr(uia, 'get_service', lambda: rig.service)
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(GetCursorPos=lambda: (60, 35)))
    if route == 'cursor':
        result = sn._click_at_cursor('single', rig.app)
    else:
        monkeypatch.setattr(sn, 'grid_state', lambda: {'rect': (10, 20, 110, 50), 'path': [], 'area': (0, 0, 800, 600), 'label': 'synthetic'})
        monkeypatch.setattr(sn, '_move_pointer', lambda *args: None)
        monkeypatch.setattr(sn, '_grid_hide', lambda *args: None)
        result = sn._grid_apply(rig.app, [], 'click')
    assert result.state is DispatchState.QUEUED and not rig.fired
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert rig.fired == [(rig.send.rect, 'click')]


def test_named_model_message_keeps_model_tier_and_one_bound_question(rig, monkeypatch):
    rig.target[0] = replace(rig.target[0], kind='known_person')
    rig.app.config['execution_policy'] = {'model_tool_allowlist': ['click']}
    listener = Mock()
    monkeypatch.setattr(ep, 'listen_for_confirmation', listener)
    result = rig.ex.execute_canonical('click', rig.app, args={'remainder': 'send'},
                                     route=ep.Route.MODEL, generation=7)
    # 1031b FINISH IT 1: model click accepts label:int, never free-form remainder.
    assert result.state is DispatchState.REJECTED
    assert result.detail['reason'] == 'invalid_args'
    assert ep.pending_operation() is None and not rig.send.calls
    listener.assert_not_called()
    node = rig.service.search('send')[0]
    sn._elements[:] = [ControlProxy(rig.service, node)]
    result = rig.ex.execute_canonical('click', rig.app, args={'label': 1},
                                     route=ep.Route.MODEL, generation=7)
    assert result.state is DispatchState.QUEUED
    assert ep.pending_operation().handling == effects.CONFIRM
    assert ep.pending_operation().invocation.route == ep.Route.MODEL
    listener.assert_called_once()
    assert not rig.send.calls
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert rig.fired == [(rig.send.rect, 'click')]


def test_browser_transport_carries_binding_without_live_connection():
    from samsara.browser_bridge import BrowserBridge
    bridge = BrowserBridge.__new__(BrowserBridge)
    sent = []
    bridge._send_and_wait = lambda message, timeout: sent.append(message) or {'ok': True}
    assert bridge.send_selection(2, 'click', binding='synthetic-binding')
    assert sent[0]['binding'] == 'synthetic-binding' and sent[0]['number'] == 2


@pytest.mark.parametrize('action', ['cancel', 'changed', 'elapsed'])
def test_private_click_grace_cancels_or_sends_once(rig, monkeypatch, action):
    from samsara.runtime import thread_registry
    rig.target[0] = replace(rig.target[0], kind='known_person')
    timers = []
    monkeypatch.setattr(thread_registry, 'timer', lambda name, delay, fn, **kw: FakeTimer(delay, fn, timers))
    cb.try_named(rig.app, 'send')
    if action == 'cancel':
        assert ep.answer_pending(rig.app, 'cancel') == 'rejected'
    elif action == 'changed':
        rig.recipient.value = 'changed'
    timers[0].fire()
    timers[0].fire()
    assert rig.send.calls == (['Invoke'] if action == 'elapsed' else [])


@pytest.mark.parametrize('action', [None, 'move'])
def test_grid_refinement_and_pointer_parking_remain_immediate(rig, monkeypatch, action):
    monkeypatch.setattr(sn, 'grid_state', lambda: {'rect': (10, 20, 110, 50), 'path': [],
                                                  'area': (0, 0, 800, 600), 'label': 'synthetic'})
    monkeypatch.setattr(sn, '_move_pointer', lambda *args: None)
    monkeypatch.setattr(sn, '_grid_hide', lambda *args: None)
    assert sn._grid_apply(rig.app, [], action) is True
    assert not rig.fired and ep.pending_operation() is None


@pytest.mark.parametrize('route', ['text', 'number'])
def test_ocr_routes_share_the_bound_email_gate(rig, monkeypatch, route):
    from samsara import screen_ocr
    snap = screen_ocr.Snapshot(42, (0, 0, 800, 600), (0, 0, 800, 600),
                              (screen_ocr.Line((screen_ocr.Word('Send', (10, 20, 110, 50)),)),), 1)
    monkeypatch.setattr(screen_ocr, 'read_foreground', lambda *args: snap)
    monkeypatch.setattr(screen_ocr, 'point_reaches_window', lambda *args: True)
    monkeypatch.setattr(screen_ocr, 'find_matches', lambda *args: [screen_ocr.Match('Send', (10, 20, 110, 50), 1, True)])
    monkeypatch.setattr(cb, 'prepare_text_click', lambda *args: (42, {}, None))
    monkeypatch.setattr(sn, '_click_with_validation', lambda *args: rig.fired.append('ocr') or True)
    if route == 'text':
        result = sn._click_text(rig.app, 'send', 'single', frozenset())
    else:
        target = sn._OcrTarget((10, 20, 110, 50), 42, snap.window_rect, 'Send')
        target._effect_snapshot = snap
        sn._elements[:] = [target]
        result = sn.handle_click(rig.app, '1')
    assert result.state is DispatchState.QUEUED and not rig.fired
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert rig.fired == ['ocr']


def test_unknown_click_outcome_never_uses_mouse_fallback(rig, monkeypatch):
    control = SimpleNamespace(BoundingRectangle=Rect(10, 20, 110, 50))
    def uncertain(**kwargs):
        rig.fired.append('attempt')
        raise OSError('synthetic unknown outcome')
    control.Click = uncertain
    monkeypatch.setitem(sys.modules, 'win32api', SimpleNamespace(
        SetCursorPos=lambda point: None, mouse_event=lambda *args: pytest.fail('duplicate click')))
    monkeypatch.setattr(sn, '_with_physical_dpi_context', lambda fn: fn())
    assert sn._perform_click(control, 'single') is False
    assert rig.fired == ['attempt']


@pytest.mark.parametrize('changed', [False, True])
def test_ocr_number_pills_are_hidden_from_capture_without_retargeting(rig, monkeypatch, tmp_path, changed):
    """Real OCR capture pipeline over composited BGRA images, never the desktop."""
    from PIL import Image, ImageDraw
    from samsara import screen_ocr
    from PySide6.QtWidgets import QApplication
    import ctypes

    overlay = SimpleNamespace(visible=False, highlight_number=lambda number: None)
    overlay.hide = lambda: setattr(overlay, 'visible', False)
    captures, pixels = [], []
    recipient_changed = [False]

    def capture(rect):
        image = Image.new('RGBA', (800, 600), 'white')
        draw = ImageDraw.Draw(image)
        draw.text((10, 20), 'Send', fill='black')
        draw.text((10, 60), 'Other recipient' if recipient_changed[0] else 'Original recipient', fill='black')
        # Pixel markers let the inert recognizer distinguish recipient and pills.
        draw.point((0, 0), fill='red' if recipient_changed[0] else 'white')
        if overlay.visible:
            draw.rectangle((9, 19, 32, 40), fill='teal')
            draw.text((15, 20), '1', fill='white')
        bgra = image.tobytes('raw', 'BGRA')
        captures.append(overlay.visible)
        pixels.append(bgra)
        if len(captures) <= 3:
            image.save(tmp_path / f'capture_{len(captures)}_pills_{overlay.visible}.png')
        return bgra

    def recognize(bgra, width, height, scale):
        image = Image.frombytes('RGBA', (width, height), bgra, 'raw', 'BGRA')
        pill = image.getpixel((9, 19))[:3] == (0, 128, 128)
        other = image.getpixel((0, 0))[:3] == (255, 0, 0)
        return [[('Send', 10, 20, 100, 30),
                 ('Other recipient' if other else 'Original recipient', 10, 60, 100, 30)]
                + ([('1', 15, 20, 10, 10)] if pill else [])]

    monkeypatch.setattr(screen_ocr, 'window_frame_rect', lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(screen_ocr, 'monitor_rect_for_window', lambda hwnd: (0, 0, 800, 600))
    monkeypatch.setattr(screen_ocr, 'capture_screen_rect', capture)
    monkeypatch.setattr(screen_ocr, 'recognize_bgra', recognize)
    monkeypatch.setattr(screen_ocr, 'point_reaches_window', lambda *args: True)
    monkeypatch.setattr(sn, '_overlay_window', overlay)
    monkeypatch.setattr(sn.qt_runtime, 'is_alive', lambda: True)
    # Exercise worker->Qt acknowledgement; post alone is not a completed hide.
    queued = []
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: queued.append(fn) or fn())
    monkeypatch.setattr(QApplication, 'instance', lambda: None)
    monkeypatch.setattr(ctypes.windll.dwmapi, 'DwmFlush', lambda: 0)
    monkeypatch.setattr(sn, '_click_with_validation', lambda *args: rig.fired.append('original target') or True)

    clean = screen_ocr.read_foreground(42)
    original = sn._OcrTarget((10, 20, 110, 50), 42, clean.window_rect, 'Send')
    original._effect_snapshot = clean
    sn._elements[:] = [original]
    overlay.visible = True
    covered = screen_ocr.read_foreground(42)
    assert pixels[0] != pixels[1]
    assert clicks.ocr_revision(clean) != clicks.ocr_revision(covered)
    assert captures == [False, True]
    result = sn.handle_click(rig.app, '1')
    assert result.state is DispatchState.QUEUED
    assert queued and all(not visible for visible in captures[2:])
    assert not overlay.visible and not rig.fired

    # Even a newly drawn overlay must be hidden at approval; never re-resolve #1.
    overlay.visible = True
    sn._elements[:] = [sn._OcrTarget((200, 20, 300, 50), 42, clean.window_rect, 'Different target')]
    recipient_changed[0] = changed
    answer = ep.answer_pending(rig.app, 'yes')
    assert not overlay.visible and all(not visible for visible in captures[2:])
    assert answer.startswith('refused') if changed else answer == 'approved'
    assert rig.fired == ([] if changed else ['original target'])


def test_dom_number_keeps_the_returned_binding_through_approval(rig, monkeypatch):
    hints = [{'binding': 'synthetic-session', 'kind': 'button'}]
    bridge = SimpleNamespace(is_connected=lambda: True, request_hints=lambda **kwargs: hints,
        send_selection=lambda *args, **kwargs: rig.fired.append((args, kwargs)) or True,
        send_dismiss=lambda: None)
    monkeypatch.setattr(sn, '_is_brave_foreground', lambda hwnd: True)
    monkeypatch.setattr(sn, '_ensure_bridge_started', lambda: bridge)
    monkeypatch.setattr(sn.browser_bridge, 'get_bridge', lambda: bridge)
    monkeypatch.setattr(sn, '_dom_effect_hints', [])
    monkeypatch.setattr(sn, '_dom_hint_count', 0)
    assert sn._try_show_dom_numbers(rig.app) == (True, None)
    assert sn._dom_effect_hints is hints
    result = sn.handle_click(rig.app, '1')
    assert result.state is DispatchState.QUEUED and not rig.fired
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert rig.fired == [((1, 'click'), {'modifiers': {'ctrlKey': False,
                        'shiftKey': False, 'altKey': False}, 'binding': 'synthetic-session'})]
    assert not sn._dom_active and sn._dom_effect_hints == []
    assert ep.answer_pending(rig.app, 'yes') != 'approved'
    assert len(rig.fired) == 1


def test_ocr_capture_refuses_when_qt_does_not_acknowledge_hide(rig, monkeypatch):
    from samsara import screen_ocr
    from PySide6.QtWidgets import QApplication
    capture = Mock(side_effect=AssertionError('must not capture covered screen'))
    monkeypatch.setattr(screen_ocr, 'read_foreground', capture)
    monkeypatch.setattr(QApplication, 'instance', lambda: None)
    monkeypatch.setattr(sn.qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: None)
    with pytest.raises(RuntimeError, match='could not be hidden'):
        clicks.read_unobscured(42)
    capture.assert_not_called()
    assert ep.pending_operation() is None and not rig.fired


def test_refresh_binding_hides_existing_pills_before_its_initial_snapshot(rig, monkeypatch):
    from samsara import screen_ocr
    from PySide6.QtWidgets import QApplication
    import ctypes
    overlay = SimpleNamespace(visible=True)
    overlay.hide = lambda: setattr(overlay, 'visible', False)
    snapshot = screen_ocr.Snapshot(42, (0, 0, 800, 600), (0, 0, 800, 600), (), 1)
    def read(hwnd=None):
        assert not overlay.visible
        return snapshot
    monkeypatch.setattr(screen_ocr, 'read_foreground', read)
    monkeypatch.setattr(sn, '_overlay_window', overlay)
    monkeypatch.setattr(QApplication, 'instance', lambda: None)
    monkeypatch.setattr(sn.qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(sn.qt_runtime, 'post', lambda fn: fn())
    monkeypatch.setattr(ctypes.windll.dwmapi, 'DwmFlush', lambda: 0)
    target = sn._OcrTarget((10, 20, 110, 50), 42, snapshot.window_rect, 'Send')
    clicks.bind_elements([{'control': target}])
    assert target._effect_snapshot is snapshot and not overlay.visible


def test_failed_initial_ocr_capture_cannot_bind_a_later_recipient(rig, monkeypatch):
    from samsara import screen_ocr
    target = sn._OcrTarget((10, 20, 110, 50), 42, (0, 0, 800, 600), 'Send')
    monkeypatch.setattr(screen_ocr, 'read_foreground', Mock(side_effect=OSError('unreadable')))
    clicks.bind_elements([{'control': target}])
    assert not hasattr(target, '_effect_snapshot')
    rig.target[0] = replace(rig.target[0], recipient='different-synthetic-person')
    assert clicks.element_action(rig.app, target, 'single', frozenset(),
                                 lambda: rig.fired.append('unbound')) is False
    assert ep.pending_operation() is None and not rig.fired


@pytest.fixture
def ocr_navigation(rig, monkeypatch):
    from samsara import screen_ocr
    snap = screen_ocr.Snapshot(42, (0, 0, 800, 600), (0, 0, 800, 600),
        (screen_ocr.Line((screen_ocr.Word('Export', (10, 20, 110, 50)),)),), 1)
    current = [snap]
    monkeypatch.setattr(screen_ocr, 'read_foreground', lambda *args: current[0])
    monkeypatch.setattr(screen_ocr, 'point_reaches_window', lambda *args: True)
    monkeypatch.setattr(cb, 'try_named', lambda *args, **kwargs: None)
    monkeypatch.setattr(sn, '_click_with_validation',
        lambda element, modifier, keys: rig.fired.append((element.Name, modifier, keys)) or True)
    target = sn._OcrTarget((10, 20, 110, 50), 42, snap.window_rect, 'Export')
    target._effect_snapshot = snap
    return target, current


def test_ocr_navigation_through_executor_preserves_double_click_and_keys(rig, ocr_navigation):
    result = rig.ex.process_text('click ctrl export twice', rig.app, force_commands=True)
    assert result.state is DispatchState.COMPLETED
    assert rig.fired == [('Export', 'double', frozenset({'ctrl'}))]
    assert ep.pending_operation() is None


@pytest.mark.parametrize('change', ['text', 'foreground', 'unreadable', 'generation'])
def test_ocr_navigation_still_refuses_a_changed_bound_target(rig, ocr_navigation, monkeypatch, change):
    from samsara import screen_ocr
    target, current = ocr_navigation
    assert isinstance(ep.authorize(ep.Invocation('click', generation=7), app=rig.app), ep.Allowed)
    if change == 'text':
        current[0] = replace(current[0], lines=())
    elif change == 'foreground':
        identity = ep.foreground_window_identity()
        calls = []
        def identity_probe():
            calls.append(True)
            return identity if len(calls) == 1 else dict(identity, hwnd=99)
        monkeypatch.setattr(ep, 'foreground_window_identity', identity_probe)
    elif change == 'unreadable':
        monkeypatch.setattr(screen_ocr, 'read_foreground', Mock(side_effect=OSError('unreadable')))
    else:
        rig.app._ava_cmd_generation += 1
    assert clicks.element_action(rig.app, target, 'single', frozenset(),
                                 lambda: rig.fired.append('stale navigation')) is False
    assert not rig.fired and ep.pending_operation() is None


@pytest.mark.parametrize('label', ['Send', 'Pay', 'Delete', 'Publish', 'Install', 'Confirm'])
def test_ocr_commit_labels_still_ask_and_reduce_double_click_to_one(rig, ocr_navigation, label):
    target, _ = ocr_navigation
    target.Name = label
    result = clicks.element_action(rig.app, target, 'double', frozenset({'ctrl'}),
                                   lambda: rig.fired.append('unapproved double'))
    assert result.state is DispatchState.QUEUED
    assert ep.pending_operation().handling == effects.CONFIRM and not rig.fired
    assert ep.answer_pending(rig.app, 'yes') == 'approved'
    assert rig.fired == [(label, 'single', frozenset({'ctrl'}))]
    assert ep.answer_pending(rig.app, 'yes') != 'approved'
    assert len(rig.fired) == 1


@pytest.mark.parametrize('blocked', [False, True])
def test_ocr_navigation_keeps_model_allowlist_admission(rig, ocr_navigation, blocked):
    target, _ = ocr_navigation
    rig.app.config['execution_policy'] = {'model_tool_allowlist': [] if blocked else ['click']}
    sn._elements[:] = [target]
    result = rig.ex.execute_canonical('click', rig.app, args={'label': 1},
                                     route=ep.Route.MODEL, generation=7)
    assert result.state is (DispatchState.REJECTED if blocked else DispatchState.QUEUED)
    assert not rig.fired
    if blocked:
        assert ep.pending_operation() is None
    else:
        # Model-proposed write commands retain their source-based confirmation.
        assert ep.pending_operation().invocation.route == ep.Route.MODEL
        assert ep.answer_pending(rig.app, 'yes') == 'approved'
        assert rig.fired == [('Export', 'single', frozenset())]
