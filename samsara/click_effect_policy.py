"""1031b: resolve a click once; approval never discovers a different target.

Only local adapters produce ClickTarget. OCR text and model arguments cannot
declare trust. Window contents are hashed in memory and never retained here.
"""
from dataclasses import dataclass, replace
import copy
import threading

from samsara import effect_policy as effects


_request = threading.local()
_COMMANDS = {'click', 'left click', 'right click', 'double click', 'pick number',
             'choose control', 'toggle', 'expand', 'collapse', 'focus control',
             'mouse grid', 'move here'}
_SAFE = {'back', 'forward', 'next', 'previous', 'home', 'search', 'menu', 'settings',
         'cancel', 'help', 'copy', 'select all', 'privacy security',
         'privacy tools', 'pri vacy security'}
_NAV_TYPES = {'EditControl', 'TabItemControl', 'ScrollBarControl', 'TreeItemControl'}
_PROTECTED = {'send', 'pay', 'purchase', 'buy', 'publish', 'post', 'delete', 'remove',
              'erase', 'install', 'uninstall', 'shutdown', 'restart', 'close', 'run',
              'execute', 'submit', 'confirm', 'allow', 'grant', 'transfer'}


@dataclass(frozen=True)
class ClickTarget:
    handling: effects.Handling
    destination: effects.SubmitTarget
    control: str
    revision: str
    label: str


def deferred_activation(inv):
    """Only these registered local resolvers may defer the final effect gate."""
    from samsara import execution_policy as ep
    entry = ep._plugin_entry(inv.command_id) or {}
    return (inv.effect_target is None and inv.command_id in _COMMANDS
            and entry.get('source') in {'plugins.commands.show_numbers', 'plugins.commands.click_by_name'})


def remember(app, inv, decision):
    from samsara import execution_policy as ep
    _request.value = ((app, copy.deepcopy(inv)) if deferred_activation(inv)
                      and isinstance(decision, ep.Allowed) else None)


def handling_for(inv, *, app=None, executor=None):
    if isinstance(inv.effect_target, ClickTarget):
        return inv.effect_target.handling
    return effects.handling_for(inv, app=app, executor=executor)


def _invocation(app, cid):
    from samsara import execution_policy as ep
    previous = getattr(_request, 'value', None)
    _request.value = None
    if previous and previous[0] is app:
        return previous[1]
    return ep.Invocation(cid, generation=ep.capture_generation(app))


def label_remainder(app, remainder):
    """Adapt a schema-checked numeric argument for the legacy click handler."""
    previous = getattr(_request, 'value', None)
    if not remainder and previous and previous[0] is app:
        label = previous[1].args.get('label')
        if type(label) is int:
            return str(label)
    return remainder


def read_unobscured(hwnd=None):
    """Hide our number pills on their Qt thread before capturing screen pixels.

    Keep the already selected target/snapshot. Never rediscover it after hiding.
    A stalled Qt loop refuses capture rather than approving a pill-covered image.
    """
    from plugins.commands import show_numbers as sn
    from samsara import screen_ocr
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    ready, errors = threading.Event(), []

    def hide():
        try:
            sn._hide_overlay_qt()
        except Exception as exc:
            errors.append(exc)
        finally:
            ready.set()

    qt_app = QApplication.instance()
    if qt_app is not None and QThread.currentThread() == qt_app.thread():
        hide()
    elif sn.qt_runtime.is_alive():
        sn.qt_runtime.post(hide)
    elif sn._overlay_window is None:
        ready.set()  # No window and no running event loop can draw pills.
    if not ready.wait(1.0) or errors:
        raise RuntimeError('number overlay could not be hidden')
    # QWidget.hide acknowledges the native hide; wait for the compositor too.
    import sys
    if sys.platform == 'win32' and sn._overlay_window is not None:
        import ctypes
        if ctypes.windll.dwmapi.DwmFlush() != 0:
            raise RuntimeError('number overlay hide could not be presented')
    return screen_ocr.read_foreground(hwnd)


def classify(label, control_type='', verb='click', destination=None, *, semantic=True, route='exact',
             text_navigation=False):
    import re
    words = re.findall(r'\w+', (label or '').casefold())
    text = ' '.join(words)
    if verb in {'focus', 'expand', 'collapse', 'read', 'right_click'}:
        return effects.IMMEDIATE
    # Recognised consequential effects win over roles and trusted AI apps.
    if set(words) & _PROTECTED:
        if text in {'send', 'submit'} and destination is not None and destination.kind in {'ai', 'known_person'}:
            return effects.send_handling(destination, route=route)
        return effects.CONFIRM
    if (text_navigation and words) or (semantic and (text in _SAFE or control_type in _NAV_TYPES)):
        return effects.IMMEDIATE
    return effects.CONFIRM  # unresolved activation; never guess from an executable


def dispatch(app, target, probe, run, *, command_id='click', timer_factory=None, invocation=None):
    """Run once, or park one bound operation. Unknown outcome is never retried."""
    from samsara import execution_policy as ep
    from samsara.command_registry import DispatchResult, DispatchState
    inv = invocation or _invocation(app, command_id)
    inv = replace(inv, args=copy.deepcopy(inv.args), effect_target=target)
    arg_hash = ep.argument_hash(inv.command_id, inv.args)
    attempted = [False]

    def refusal():
        if not ep.is_fresh(app, inv.generation):
            return 'stale'
        if ep.argument_hash(inv.command_id, inv.args) != arg_hash:
            return 'arguments changed'
        try:
            if target != probe():
                return 'control or destination changed'
        except Exception:
            return 'control or destination unavailable'
        return None

    def commit(confirmed=False):
        if attempted[0] or refusal():
            return False
        if not isinstance(ep.authorize(inv, app=app, confirmed=confirmed), ep.Allowed):
            return False
        attempted[0] = True  # Even an exception/unknown outcome consumes this action.
        return run()

    if refusal():
        return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                              {'reason': 'control or destination changed'})
    decision = ep.authorize(inv, app=app)
    if isinstance(decision, ep.Denied):
        return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                              {'reason': decision.reason})
    if isinstance(decision, ep.NeedsConfirmation):
        # PLACEHOLDER COPY (1031b): existing confirmation surface, both displays.
        prompt = f'Activate {ep._template_value(target.label)} in {ep._template_value(target.destination.process or "this window")}?'
        ep.stage_pending(app, inv, prompt, on_approve=lambda op: commit(True),
                         refusal_guard=refusal, record_type='action')
        ep.listen_for_confirmation(app, prompt)
        return DispatchResult(DispatchState.QUEUED, inv.command_id, inv.command_id,
                              {'awaiting_confirmation': True, 'prompt': prompt})
    if target.handling == effects.PRIVATE_SEND:
        preview = ' '.join(target.destination.preview.split()[:8])
        ep._chip(app, f'Sending to {target.destination.label}: {preview}; say cancel or send now', 'pending')  # PLACEHOLDER COPY (1031b)
        op = ep.stage_cancel_window(app, inv, target.destination.label, 1.0,
                                    commit, bind_foreground=False, timer_factory=timer_factory)
        op._refusal_guard = refusal
        return DispatchResult(DispatchState.QUEUED, inv.command_id, inv.command_id,
                              {'send_grace_s': 1.0, 'op_id': op.op_id})
    return commit()


def _destination(app, hwnd):
    target = effects.resolve_submit_target(app)
    if target.hwnd != hwnd:
        return effects.SubmitTarget(hwnd=hwnd)
    return target


def uia_action(app, service, node, verb, *, command_id='click', keys=frozenset()):
    inv = _invocation(app, command_id)
    if not inv.args and inv.command_id in {'toggle', 'expand', 'collapse', 'focus control'}:
        inv = replace(inv, args={'remainder': node.name})
    if inv.command_id == 'choose control':
        # The one-shot selection was consumed; the bound control replaces its scope.
        inv = replace(inv, command_id='click', args={})

    def probe():
        revision = service.activation_revision(node)
        destination = _destination(app, node.foreground_hwnd or node.hwnd)
        handling = classify(node.name, node.control_type, verb, destination, route=inv.route)
        if node.foreground_hwnd and not (set(node.name.casefold().split()) & _PROTECTED):
            handling = effects.IMMEDIATE  # Taskbar window navigation, bound to its owner.
        return ClickTarget(handling, destination, str((node.hwnd, node.generation, node.index)),
                           revision, node.name or 'the selected control')

    target = probe()
    if verb == 'double_click' and (target.handling in {effects.CONFIRM, effects.PRIVATE_SEND}
                                   or node.name.casefold().strip() in {'send', 'submit'}):
        verb = 'click'  # A consequential activation is never sent twice.

    def run():
        from plugins.commands.show_numbers import _apply_modifier_keys
        # Final context check is INSIDE the COM worker immediately before act.
        result = []
        _apply_modifier_keys(keys, lambda: result.append(
            service.act_bound(node, verb, target.revision)))
        return bool(result and result[0])

    return dispatch(app, target, probe, run, command_id=command_id, invocation=inv)


def ocr_revision(snapshot):
    return effects.text_hash((snapshot.hwnd, snapshot.window_rect, snapshot.capture_rect,
                             [(w.text, w.rect) for line in snapshot.lines for w in line.words]))


def coordinate_action(app, point, run, *, label='', snapshot=None, command_id='click',
                      modifier='single', expected=None, text_navigation=False):
    """OCR/Win32 fallback: bind window + all visible text + the physical point."""
    from samsara import execution_policy as ep, screen_ocr
    inv = _invocation(app, command_id)
    initial_identity = ep.foreground_window_identity()
    hwnd = (snapshot.hwnd if snapshot is not None else (initial_identity or {}).get('hwnd', 0))
    if not hwnd:
        return False
    if snapshot is None:
        snapshot = read_unobscured(hwnd)
    revision = ocr_revision(snapshot)
    destination = _destination(app, hwnd)
    # A selected text match is navigation unless its label resolves to a commit.
    # Bare coordinates still cannot establish what an arbitrary pixel does.
    handling = classify(label, verb='right_click' if modifier == 'right' else 'click',
                        destination=destination, semantic=False, route=inv.route,
                        text_navigation=text_navigation)
    target = ClickTarget(handling, destination, str(tuple(point)), revision, label or 'the pointer target')
    if expected is not None and revision != expected:
        return False

    def probe():
        if ep.foreground_window_identity() != initial_identity:
            raise ValueError('foreground changed')
        if not screen_ocr.point_reaches_window(hwnd, *point):
            raise ValueError('target obscured')
        current = read_unobscured(hwnd)
        return replace(target, revision=ocr_revision(current), destination=_destination(app, hwnd))

    if handling == effects.IMMEDIATE and inv.route in ep.USER_ROUTES:
        # User navigation is admitted by the caller; direct local handlers need
        # no registry bootstrap. Keep freshness and the same bound target check.
        if not ep.is_fresh(app, inv.generation):
            return False
        try:
            if target != probe():
                return False
        except Exception:
            return False
        return run()
    return dispatch(app, target, probe, run, command_id=command_id, invocation=inv)


def element_action(app, element, modifier, keys, run):
    from samsara.uia import ControlProxy
    from plugins.commands.show_numbers import _OcrTarget
    if isinstance(element, ControlProxy):
        verb = {'single': 'physical_click', 'right': 'right_click', 'double': 'double_click'}[modifier]
        return uia_action(app, element._service, element._node, verb, keys=keys)
    rect = element.BoundingRectangle
    point = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    snapshot = getattr(element, '_effect_snapshot', None)
    if snapshot is None:
        return False  # A failed pre-label capture cannot be rebound at click time.
    label = getattr(element, 'Name', '')
    text_navigation = isinstance(element, _OcrTarget)
    if modifier == 'double' and classify(label, semantic=False,
                                         text_navigation=text_navigation) != effects.IMMEDIATE:
        from plugins.commands.show_numbers import _click_with_validation
        run = lambda: _click_with_validation(element, 'single', keys)
    return coordinate_action(app, point, run, label=label,
                             snapshot=snapshot, modifier=modifier,
                             expected=getattr(element, '_effect_revision', None),
                             text_navigation=text_navigation)


def cursor_action(app, modifier, run, *, point=None):
    """Prefer a UIA target at the pointer; coordinate fallback stays bound."""
    import win32api
    from samsara.uia import get_service
    if point is None:
        point = win32api.GetCursorPos()
    try:
        service = get_service()
        nodes = [n for n in service.snapshot(refresh=True).nodes
                 if n.enabled and not n.offscreen and n.rect[0] <= point[0] < n.rect[2]
                 and n.rect[1] <= point[1] < n.rect[3]]
        node = max(nodes, key=lambda n: n.depth)
    except Exception:
        if modifier == 'double':
            from plugins.commands.show_numbers import _click_at_cursor_raw
            run = lambda: _click_at_cursor_raw('single')
        return coordinate_action(app, point, run, modifier=modifier)
    return uia_action(app, service, node,
                      {'single': 'physical_click', 'right': 'right_click', 'double': 'double_click'}[modifier])


def bind_elements(elements):
    from samsara.uia import ControlProxy
    from samsara import screen_ocr
    snapshot = None
    for item in elements:
        control = item['control']
        if isinstance(control, ControlProxy) or hasattr(control, '_effect_snapshot'):
            continue
        if snapshot is None:
            try:
                snapshot = read_unobscured()
            except Exception:
                continue
        control._effect_snapshot = snapshot


def dom_action(app, hints, number, modifier, keys, live, run):
    from samsara import execution_policy as ep
    from samsara.command_registry import DispatchResult, DispatchState
    inv = _invocation(app, 'click')
    hint = hints[number - 1]
    binding = hint.get('binding')
    if not binding:
        return DispatchResult(DispatchState.REJECTED, inv.command_id, inv.command_id,
                              {'reason': 'browser extension needs activation binding'})
    identity = ep.foreground_window_identity() or {}
    destination = _destination(app, identity.get('hwnd', 0))
    # Only a true text-entry focus is immediate. Other DOM effects are unresolved.
    handling = (effects.IMMEDIATE if hint.get('kind') in {'input', 'textbox', 'contenteditable'}
                and modifier == 'single' else effects.CONFIRM)
    target = ClickTarget(handling, destination, f'{binding}:{number}', binding, 'the browser control')

    def probe():
        if not live() or ep.foreground_window_identity() != identity:
            raise ValueError('browser target changed')
        return replace(target, destination=_destination(app, identity.get('hwnd', 0)))

    return dispatch(app, target, probe, lambda: run(binding, 'single' if handling == effects.CONFIRM else modifier),
                    invocation=inv)
