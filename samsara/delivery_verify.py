"""783: memory-only readback receipts; never guess an application's undo unit.

UIA stays on the existing apartment-confined service. Only native Edit/RichEdit
controls offer a target-scoped replacement here. Other readable controls can
confirm delivery, but cannot authorize global Backspace or Ctrl+Z.
"""
from __future__ import annotations

import ctypes
import logging

from samsara import injection_safety

log = logging.getLogger("Samsara")
REFUSAL = "Can't take that back - Samsara couldn't confirm it typed it."
CHANGED = "Can't take that back - the text or cursor has changed."
MAX_UNITS = 65536
NATIVE_CLASSES = frozenset({
    'edit', 'richedit20w', 'richedit50w', 'richeditd2dpt', 'richeditd2d',
})


def _focus():
    from ctypes import wintypes as w
    from samsara.clipboard import _GUIThreadInfo, _user32 as u
    class LastInput(ctypes.Structure):
        _fields_ = [('size', w.UINT), ('tick', w.DWORD)]
    tick = LastInput(ctypes.sizeof(LastInput), 0)
    window = u.GetForegroundWindow()
    thread = u.GetWindowThreadProcessId(window, None)
    info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
    if (not window or not thread or not u.GetGUIThreadInfo(thread, ctypes.byref(info))
            or not info.hwndFocus or not u.GetLastInputInfo(ctypes.byref(tick))):
        raise RuntimeError('focus unavailable')
    return window, info.hwndFocus, tick.tick


def _send(control, message, wp=0, lp=0):
    from samsara.clipboard import _user32 as u
    result = ctypes.c_size_t()
    if not u.SendMessageTimeoutW(control, message, wp, lp, 0x23, 200,
                                 ctypes.byref(result)):
        raise RuntimeError('editor unavailable')
    return result.value


def _native_snapshot():
    """Same UTF-16 receipt shape as 606, extended to native RichEdit controls."""
    from ctypes import wintypes as w
    from samsara.clipboard import _user32 as u
    window, control, tick = _focus()
    name = ctypes.create_unicode_buffer(256)
    u.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
    u.GetWindowLongW.restype = w.LONG
    if (not u.GetClassNameW(control, name, len(name))
            or name.value.lower() not in NATIVE_CLASSES
            or u.GetWindowLongW(control, -16) & 0x20
            or not injection_safety.window_integrity(control).confirmed_ok):
        return None
    length = _send(control, 0x000E)
    if length > MAX_UNITS:
        return None
    text = ctypes.create_unicode_buffer(length + 1)
    copied = _send(control, 0x000D, length + 1, ctypes.addressof(text))
    start, end = w.DWORD(), w.DWORD()
    _send(control, 0x00B0, ctypes.addressof(start), ctypes.addressof(end))
    payload = text.value.encode('utf-16-le')
    if (copied != len(payload) // 2 or copied != length
            or not 0 <= start.value <= end.value <= length
            or _focus() != (window, control, tick)):
        return None
    return window, control, payload, start.value, end.value, tick


def _uia_snapshot():
    from samsara.uia import get_service
    service = get_service()
    def read():
        window, focus, tick = _focus()
        if not injection_safety.window_integrity(window).confirmed_ok:
            return None
        backend = service._get_backend()
        ctrl = backend.focused()
        if (ctrl is None or ctrl.IsPassword or not ctrl.element.CurrentHasKeyboardFocus):
            return None
        pattern = ctrl.pattern('text')
        if pattern is None:
            return None  # ValuePattern alone cannot prove a caret position.
        ranges = pattern.GetSelection()
        if not ranges or ranges.Length != 1:
            return None
        selection = ranges.GetElement(0)
        document = pattern.DocumentRange
        text = document.GetText(MAX_UNITS + 1)
        if len(text.encode('utf-16-le')) // 2 > MAX_UNITS:
            return None
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, selection, 0)  # End -> selection Start
        start = len(prefix.GetText(MAX_UNITS + 1).encode('utf-16-le')) // 2
        prefix.MoveEndpointByRange(1, selection, 1)
        end = len(prefix.GetText(MAX_UNITS + 1).encode('utf-16-le')) // 2
        identity = tuple(ctrl.element.GetRuntimeId())
        current = backend.focused()
        if (not identity or current is None or tuple(current.element.GetRuntimeId()) != identity
                or not current.element.CurrentHasKeyboardFocus
                or _focus() != (window, focus, tick)):
            return None
        return window, identity, text.encode('utf-16-le'), start, end, tick
    return service._call(read)


def snapshot():
    """Return (state, native_writer), or no proof; contents never leave memory."""
    try:
        state = _native_snapshot()
        if state is not None:
            return state, True
        state = _uia_snapshot()
        return (state, False) if state is not None else None
    except Exception:
        return None


def _matches(before, after, text):
    payload = text.encode('utf-16-le')
    offset = before[3] * 2
    return (bool(payload) and before[:2] == after[:2]
            and before[3] == before[4] and after[3] == after[4]
            and after[3] == before[3] + len(payload) // 2
            and after[2] == before[2][:offset] + payload + before[2][offset:])


def verified_snapshot(before, text, target_hwnd):
    """Read-only delivery proof, including UIA; grants no mutation authority."""
    if before is None or not target_hwnd or before[0][0] != target_hwnd:
        return None
    after = snapshot()
    if after is None or before[1] != after[1] or not _matches(before[0], after[0], text):
        return None
    return after


def verify(before, text, target_hwnd):
    after = verified_snapshot(before, text, target_hwnd)
    if after is None:
        return False, None
    unit = ReadbackUnit(before[0], after[0], text) if after[1] else None
    return True, unit


class ReadbackUnit:
    def __init__(self, before, after, text):
        self.before, self.after, self.text = before, after, text
        self.expected, self.removed = after, False

    def change(self, *, redo=False):
        try:
            return self._change(redo)
        except Exception:
            self.expected = None
            return False

    def _change(self, redo):
        if (self.expected is None or self.removed != redo
                or _native_snapshot() != self.expected):
            return False
        initial = self.expected
        offset = self.before[3]
        end = offset if redo else offset + len(self.text.encode('utf-16-le')) // 2
        control = initial[1]
        _send(control, 0x00B1, offset, end)  # EM_SETSEL, scoped to this control
        selected = _native_snapshot()
        if (selected is None or selected[:3] != initial[:3]
                or selected[3:5] != (offset, end) or selected[5] != initial[5]):
            self.expected = None
            return False
        replacement = ctypes.create_unicode_buffer(self.text if redo else '')
        self.expected = None  # uncertain dispatch must never be retried
        _send(control, 0x00C2, 1, ctypes.addressof(replacement))  # EM_REPLACESEL
        actual = _native_snapshot()
        wanted = self.after if redo else self.before
        if actual is None or actual[:5] != wanted[:5] or actual[5] != selected[5]:
            return False
        self.expected, self.removed = actual, not redo
        if redo:
            self.before, self.after = initial, actual
        return True


def remember_standalone(app, text, sent, confirmed):
    """Join hold/standalone delivery to the same scratch stack as a commit.

    A failed/unverified newest delivery also fences older receipts. Never let
    scratch silently skip over it and remove an earlier sentence instead.
    """
    from samsara.session_modes import SessionMode, StackItem
    manager = getattr(app, '_session_mode_manager', None)
    ensure = getattr(app, '_ensure_session_mode_manager', None)
    # Only a fully initialized app owns the lazy manager slot. Lightweight
    # delivery clients must not bootstrap a session just to report a result.
    if manager is None and hasattr(app, '_session_mode_manager') and callable(ensure):
        manager = ensure()
    if manager is None:
        log.info('[DELIVERY] Standalone sent=%s confirmed=%s chars=%d scratch_armed=False',
                 sent, confirmed, len(text))
        return
    unit = getattr(app, '_undo_unit', None) if sent and confirmed else None
    with manager._dispatch_lock:
        manager._stack.push(StackItem(
            kind='dictation_chunk', payload=text if unit is not None else '',
            mode=SessionMode.DICTATE, timestamp=manager._clock(),
            extra={'target_process': manager._foreground_exe_resolver(),
                   'hwnd': unit.after[0] if unit is not None else None,
                   'undo_unit': unit, 'readback_required': True}))
    log.info('[DELIVERY] Standalone sent=%s confirmed=%s chars=%d scratch_armed=%s',
             sent, confirmed, len(text), unit is not None)


def scratch_message(manager):
    top = manager._stack.peek()
    return CHANGED if top is not None and top.extra.get('undo_unit') is not None else REFUSAL
