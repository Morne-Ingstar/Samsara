"""Application-facing text delivery and single-level paste undo helpers.

The low-level clipboard snapshot/restore implementation lives in
``samsara.clipboard``.  This module owns the DictationApp seam that chooses
typed Unicode delivery, coordinates focus checks, and tracks native undo.
"""

import ctypes
import logging
import time
from samsara.log import describe_text, safe_exception
from samsara import flight_recorder, injection_safety
from samsara.clipboard import (
    DeliveryOutcome,
    paste_with_preservation,
    type_text_unicode,
)
from samsara.constants import CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY
from samsara.handlers import _get_foreground_hwnd
from samsara.runtime import thread_registry


logger = logging.getLogger("Samsara")

_UNDO_TARGET_UNSET = object()
_WAKE_PRIMER_DELAY = 0.12
UNVERIFIED_DELIVERY = "Sent, but not confirmed. Your words are in History."


def _editor_snapshot():
    """Bounded, fail-closed proof for native Edit controls; never log contents.

    Unknown/custom/password controls have no proof. Input time also catches a
    caret moved away and back, which comparing the final selection would miss.
    Indices and payloads use UTF-16 units, as EM_GETSEL does.
    """
    try:
        from ctypes import wintypes as w
        from samsara.clipboard import _GUIThreadInfo, _user32 as u
        u.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
        u.GetWindowLongW.restype = w.LONG
        class LastInput(ctypes.Structure):
            _fields_ = [('size', w.UINT), ('tick', w.DWORD)]
        tick = LastInput(ctypes.sizeof(LastInput), 0)
        if not u.GetLastInputInfo(ctypes.byref(tick)):
            return None
        window = u.GetForegroundWindow()
        thread = u.GetWindowThreadProcessId(window, None)
        info = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
        if not thread or not u.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return None
        control = info.hwndFocus
        name = ctypes.create_unicode_buffer(256)
        if not control or not u.GetClassNameW(control, name, len(name)):
            return None
        if name.value.lower() != 'edit' or u.GetWindowLongW(control, -16) & 0x20:
            return None
        if not injection_safety.window_integrity(control).confirmed_ok:
            return None
        def send(message, wp=0, lp=0):
            result = ctypes.c_size_t()
            if not u.SendMessageTimeoutW(control, message, wp, lp, 0x23, 200,
                                         ctypes.byref(result)):
                raise RuntimeError('editor unavailable')
            return result.value
        length = send(0x000E)  # WM_GETTEXTLENGTH
        if length > 1_000_000:
            return None
        text = ctypes.create_unicode_buffer(length + 1)
        send(0x000D, length + 1, ctypes.addressof(text))
        start, end = w.DWORD(), w.DWORD()
        send(0x00B0, ctypes.addressof(start), ctypes.addressof(end))
        final_tick = LastInput(ctypes.sizeof(LastInput), 0)
        if not u.GetLastInputInfo(ctypes.byref(final_tick)) or final_tick.tick != tick.tick:
            return None
        current = _GUIThreadInfo(cbSize=ctypes.sizeof(_GUIThreadInfo))
        if (u.GetForegroundWindow() != window
                or not u.GetGUIThreadInfo(thread, ctypes.byref(current))
                or current.hwndFocus != control):
            return None
        return (window, control, text.value.encode('utf-16-le'), start.value, end.value, tick.tick)
    except Exception:
        return None


class DictationUnit:
    """One verified insertion, removed/restored with one native replacement.

    No Ctrl+Z guess, global keystroke or character-at-a-time deletion. A
    selection replacement is deliberately ineligible: it may have displaced
    user text. Receipts are memory-only and expire with their owning stack.
    """
    def __init__(self, before, after, text):
        self.before, self.after = before, after
        self.text = text
        self.expected = after
        self.removed = False

    @classmethod
    def capture(cls, before, after, text):
        if before is None or after is None or before[:2] != after[:2]:
            return None
        payload = text.encode('utf-16-le')
        offset = before[3] * 2
        if (before[3] != before[4] or not payload
                or after[2] != before[2][:offset] + payload + before[2][offset:]
                or after[3] != after[4] or after[3] != before[3] + len(payload) // 2):
            return None
        return cls(before, after, text)

    def change(self, *, redo=False):
        try:
            return self._change(redo=redo)
        except Exception:
            self.expected = None
            return False

    def _change(self, *, redo=False):
        if self.removed != redo or self.expected is None:
            return False
        if _editor_snapshot() != self.expected:
            return False
        initial = self.expected
        from samsara.clipboard import _user32 as u
        offset = self.before[3]
        end = offset if redo else offset + len(self.text.encode('utf-16-le')) // 2
        control = self.expected[1]
        def send(message, wp, lp):
            result = ctypes.c_size_t()
            return bool(u.SendMessageTimeoutW(control, message, wp, lp, 0x23, 200,
                                              ctypes.byref(result)))
        if not send(0x00B1, offset, end):  # EM_SETSEL
            return False
        selected = _editor_snapshot()
        if (selected is None or selected[:3] != self.expected[:3]
                or selected[3:5] != (offset, end) or selected[5] != self.expected[5]):
            self.expected = None
            return False
        replacement = ctypes.create_unicode_buffer(self.text if redo else '')
        if not send(0x00C2, 1, ctypes.addressof(replacement)):  # EM_REPLACESEL
            self.expected = None  # uncertain dispatch must never be retried
            return False
        actual = _editor_snapshot()
        wanted = self.after if redo else self.before
        self.expected = actual
        if actual is None or actual[:5] != wanted[:5] or actual[5] != selected[5]:
            self.expected = None
            return False
        self.removed = not redo
        if redo:
            self.before, self.after = initial, actual
        return True


def foreground_process_name() -> str:
    """Return the lowercase image name of the focused window's process."""
    try:
        import psutil

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def foreground_wants_typed_injection(app) -> bool:
    """Return whether the focused process is on the typed-input allowlist."""
    allowed = app.config.get('typed_injection_processes') or app._TYPED_INJECTION_PROCESSES
    if not allowed:
        return False
    name = app._foreground_process_name()
    return bool(name) and name in {str(a).lower() for a in allowed}


def flight_foreground_process_name() -> str | None:
    """Return the focused process name for injection flight-recorder events."""
    try:
        import psutil

        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.c_ulong()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None
        return psutil.Process(pid.value).name()
    except Exception:
        return None


class DeliveryResult(tuple):
    """Two legacy booleans plus the three-way delivery outcome."""
    def __new__(cls, sent, confirmed, outcome):
        result = super().__new__(cls, (sent, confirmed))
        result.outcome = outcome
        return result


def paste_preserving_clipboard(
    app,
    text,
    before_paste=None,
    return_delivery_confirmation=False,
    *,
    feedback=True,
    clipboard_only=False,
    paste_with_preservation_fn=paste_with_preservation,
    type_text_unicode_fn=type_text_unicode,
    get_foreground_hwnd_fn=_get_foreground_hwnd,
):
    """Paste text through the app's guarded clipboard/type delivery seam."""
    app._unit_before = None
    app._undo_unit = None
    app._redo_unit = None
    # Queue 50: the single delivery chokepoint (hold, wake, session commit)
    # refuses a window Windows will not let us type into. Callers with
    # their own announcement (session outcome, hold path) check earlier;
    # this guard makes every other lane fail honestly instead of "pasting".
    def _result(sent, confirmed, outcome=DeliveryOutcome.UNVERIFIED):
        if not return_delivery_confirmation:
            show_delivery_result(app, sent, outcome, feedback=feedback)
        return DeliveryResult(sent, confirmed, outcome) if return_delivery_confirmation else sent

    _verdict = injection_safety.window_integrity()
    if _verdict is not None and _verdict.blocked:
        logger.warning("[INJECT] delivery refused: foreground window is elevated (%s)", _verdict.describe())
        return _result(False, False)
    delay = app.config.get('clipboard_delay', CLIPBOARD_RESTORE_DELAY)
    paste_target = {'hwnd': None}

    # 2026-07-24: typed Unicode injection for ordinary dictation lengths.
    # Synthetic Ctrl+V into rich web editors is a documented
    # double-execution hazard (editor keydown handler + native paste both
    # fire) and forces the clipboard-preservation dance. Typing the text
    # as KEYEVENTF_UNICODE events sidesteps both: no clipboard touch,
    # nothing for the target to double. Clipboard-paste remains for long
    # texts where atomic delivery matters.
    threshold = app.config.get('paste_min_chars', 300)
    _typed_wanted = (not clipboard_only and len(text) < threshold
                     and app._foreground_wants_typed_injection())
    _typed_failed = False
    if _typed_wanted:
        if before_paste is not None and not before_paste():
            logger.warning(
                "[TYPE] Injection cancelled because the foreground target changed"
            )
            flight_recorder.record(
                'inject', path='typed', why='under_paste_min_chars',
                target_process=app._flight_foreground_process_name(),
                chars=len(text), result='cancelled_target_changed',
            )
            return _result(False, False)
        typed_hwnd = get_foreground_hwnd_fn()
        app._unit_before = _editor_snapshot()
        if type_text_unicode_fn(text):
            app._record_undoable_paste(text, target_hwnd=typed_hwnd)
            app.adaptive_learner.record_transcription(text)
            logger.info(
                "[TYPE] Unicode-typed chars=%d hwnd=%r", len(text), typed_hwnd,
            )
            flight_recorder.record(
                'inject', path='typed', why='under_paste_min_chars',
                target_process=app._flight_foreground_process_name(),
                chars=len(text), result='ok',
            )
            return _result(True, True, DeliveryOutcome.SENT)
        logger.warning(
            "[TYPE] Typed injection failed; falling back to clipboard paste"
        )
        _typed_failed = True
        flight_recorder.record(
            'inject', path='typed', why='under_paste_min_chars',
            target_process=app._flight_foreground_process_name(),
            chars=len(text), result='failed_falling_back_to_clipboard',
        )

    def _capture_target_before_paste():
        """Compose the caller's focus guard with undo-target capture.

        paste_with_preservation invokes this immediately before Ctrl+V,
        after its clipboard preparation delay. Capturing here avoids
        remembering whichever window happened to be foreground earlier
        when the transcription worker began.
        """
        if before_paste is not None and not before_paste():
            return False
        paste_target['hwnd'] = get_foreground_hwnd_fn()
        app._unit_before = _editor_snapshot()
        return True

    typed_fallback_used = {"value": False}

    def _typed_fallback_without_clipboard():
        if not app._foreground_wants_typed_injection() or not _capture_target_before_paste():
            return False
        typed_fallback_used["value"] = type_text_unicode_fn(text, key_interval=0.05)
        return typed_fallback_used["value"]

    # Keep one clipboard implementation. The centralized path captures
    # the clipboard sequence number immediately after Samsara's copy, so
    # an unrelated copy made during the paste window is never overwritten
    # by restoring our stale snapshot.
    receipt = {"outcome": DeliveryOutcome.SENT}
    paste_ok = paste_with_preservation_fn(
        text,
        paste_delay=CLIPBOARD_PASTE_DELAY,
        restore_delay=delay,
        before_paste=_capture_target_before_paste,
        incomplete_snapshot_fallback=None if clipboard_only else _typed_fallback_without_clipboard,
        delivery_observer=lambda outcome: receipt.update(outcome=outcome),
    )
    outcome = receipt["outcome"]
    # Accept existing boolean observers while the native seam uses three states.
    if isinstance(outcome, bool):
        outcome = DeliveryOutcome.CONFIRMED if outcome else DeliveryOutcome.UNVERIFIED
    if typed_fallback_used["value"] or (_verdict is not None and not getattr(_verdict, 'confirmed_ok', False)):
        outcome = DeliveryOutcome.UNVERIFIED
    delivery_confirmed = outcome == DeliveryOutcome.CONFIRMED
    if paste_ok and delivery_confirmed:
        app._record_undoable_paste(
            text, target_hwnd=paste_target['hwnd'],
        )
        app.adaptive_learner.record_transcription(text)
        logger.info(
            "[PASTE] Message delivery confirmed chars=%d hwnd=%r",
            len(text), get_foreground_hwnd_fn(),
        )
    elif paste_ok:
        if outcome == DeliveryOutcome.UNVERIFIED:
            logger.warning('[PASTE] Sent without target acknowledgement; text recoverable in History')
        _arm_undo_after_paste(app, text, paste_target['hwnd'], get_foreground_hwnd_fn)
    else:
        logger.error("[PASTE] Ctrl+V delivery failed; text retained by caller when possible")
    flight_recorder.record(
        'inject', path='clipboard',
        why=('typed_failed' if _typed_failed else 'over_paste_min_chars' if len(text) >= threshold
             else 'typed_injection_not_enabled_for_target'),
        target_process=app._flight_foreground_process_name(),
        chars=len(text), result=('sent_unverified' if outcome == DeliveryOutcome.UNVERIFIED else 'ok') if paste_ok else 'failed',
    )
    return _result(paste_ok, delivery_confirmed, outcome)


_UNDO_TERMINAL_PROCESSES = frozenset({
    'windowsterminal.exe', 'cmd.exe', 'powershell.exe', 'pwsh.exe',
    'conhost.exe', 'warp.exe', 'wezterm-gui.exe', 'alacritty.exe', 'mintty.exe',
})


def _arm_undo_after_paste(app, text, target_hwnd, get_foreground_hwnd_fn):
    """Arm native undo for a stable editor target, without confirming delivery."""
    if not target_hwnd:
        reason = 'no target window'
    elif get_foreground_hwnd_fn() != target_hwnd:
        reason = 'foreground changed'
    elif (flight_foreground_process_name() or '').lower() in _UNDO_TERMINAL_PROCESSES:
        reason = 'terminal'
    else:
        app._record_undoable_paste(text, target_hwnd=target_hwnd)
        return
    logger.info('[PASTE] undo not armed: %s', reason)


def show_delivery_result(app, sent, outcome, *, feedback=True):
    """Shared Full/Compact outcome, based on receipt rather than API acceptance."""
    # False suppresses success only; streaming partials suppress all feedback.
    if feedback == 'silent':
        return sent
    if isinstance(outcome, bool):
        outcome = DeliveryOutcome.CONFIRMED if outcome else DeliveryOutcome.UNVERIFIED
    if sent and outcome in (DeliveryOutcome.CONFIRMED, DeliveryOutcome.SENT):
        if not feedback:
            return sent
        if callable(sound := getattr(app, 'play_sound', None)):
            sound('success')
        indicator = getattr(app, 'listening_indicator', None)
        if indicator is not None and callable(flash := getattr(indicator, 'flash_success', None)):
            app._schedule_ui(flash)
        if callable(show := getattr(app, '_show_outcome_chip', None)):
            show('typed', 'success', 900)
    elif sent:
        if callable(show := getattr(app, '_show_outcome_chip', None)):
            show(UNVERIFIED_DELIVERY, 'warning')
    return sent


def deliver_text_to_focused_editor(app, text):
    """Remove the wake focus primer, then deliver text through the app seam."""
    import pyautogui

    target_hwnd = getattr(app, '_wake_session_target_hwnd', None)
    if target_hwnd and _get_foreground_hwnd() != target_hwnd:
        return False
    # backspace removes focus-primer char; assumes empty input box at session start
    pyautogui.press('x')
    time.sleep(_WAKE_PRIMER_DELAY)
    pyautogui.press('backspace')
    time.sleep(_WAKE_PRIMER_DELAY)
    if target_hwnd:
        return app._paste_preserving_clipboard(
            text, before_paste=lambda: _get_foreground_hwnd() == target_hwnd)
    return app._paste_preserving_clipboard(text)


def record_undoable_paste(
    app,
    text,
    target_hwnd=_UNDO_TARGET_UNSET,
    *,
    get_foreground_hwnd_fn=_get_foreground_hwnd,
):
    """Remember a paste and the exact window eligible for native undo."""
    app._last_dictation_text = text
    app._undo_unit = DictationUnit.capture(
        getattr(app, '_unit_before', None), _editor_snapshot(), text)
    app._unit_before = None
    app._redo_unit = None
    app._last_dictation_length = len(text)
    app._last_dictation_hwnd = (
        get_foreground_hwnd_fn()
        if target_hwnd is _UNDO_TARGET_UNSET else target_hwnd
    )
    app._arm_undo_timer()


def arm_undo_timer(app):
    """Start a fresh expiry timer; cancel any existing one."""
    if app._undo_timer is not None:
        app._undo_timer.cancel()
    app._undo_timer = thread_registry.timer(
        "dictation.undo_expiry", app._UNDO_EXPIRY_SECONDS,
        app._clear_undo, daemon=True)


def clear_undo(app):
    """Drop undo state (called on expiry or after a successful undo)."""
    app._last_dictation_text = None
    app._last_dictation_length = 0
    app._last_dictation_hwnd = None
    app._undo_unit = None
    app._redo_unit = None
    if app._undo_timer is not None:
        app._undo_timer.cancel()
        app._undo_timer = None


def undo_last_dictation(app, *, get_foreground_hwnd_fn=_get_foreground_hwnd):
    """Undo the last paste through the target application's undo stack."""
    import pyautogui

    if not app._last_dictation_text:
        logger.info("[UNDO] Nothing to undo")
        app.play_sound("error")
        if callable(getattr(app, '_show_outcome_chip', None)):
            app._show_outcome_chip('Nothing to undo', 'error')
        return False

    target_hwnd = getattr(app, '_last_dictation_hwnd', None)
    current_hwnd = get_foreground_hwnd_fn()
    if target_hwnd is None or current_hwnd != target_hwnd:
        logger.warning(
            "[UNDO] Refused: paste window is not foreground "
            "(target_hwnd=%r current_hwnd=%r)",
            target_hwnd, current_hwnd,
        )
        app.play_sound("error")
        if callable(getattr(app, '_show_outcome_chip', None)):
            app._show_outcome_chip('Undo: go back to where it was typed', 'error')
        return False

    # Real delivery always installs this field, including a failed proof.
    # Legacy embedders without receipts retain their native-undo interface.
    if hasattr(app, '_undo_unit'):
        unit = app._undo_unit
        if unit is None or not unit.change():
            app.play_sound('error')
            if callable(getattr(app, '_show_outcome_chip', None)):
                app._show_outcome_chip('Cannot undo safely here', 'error')
            return False
        app._clear_undo()
        app._redo_unit = unit
        app._arm_undo_timer()
        app.play_sound('success')
        if callable(getattr(app, '_show_outcome_chip', None)):
            app._show_outcome_chip('Undone', 'success')
        return True

    text = app._last_dictation_text
    try:
        pyautogui.hotkey('ctrl', 'z')
    except Exception as exc:
        safe_exception(logger, "[UNDO] Ctrl+Z injection failed: %s", describe_text(exc))
        app.play_sound("error")
        return False

    logger.info(f"[UNDO] Native Ctrl+Z sent for: {describe_text(text)}")
    app.play_sound("success")
    app._clear_undo()
    if callable(getattr(app, '_show_outcome_chip', None)):
        app._show_outcome_chip(f'Undid: {text[:40]}', 'success')
    return True


def redo_last_dictation(app):
    """Restore only Samsara's last verified removed unit, never Ctrl+Y."""
    unit = getattr(app, '_redo_unit', None)
    if unit is None or not unit.change(redo=True):
        app.play_sound('error')
        app._show_outcome_chip('Cannot redo safely here', 'error')
        return False
    app._redo_unit = None
    app._undo_unit = unit
    app._last_dictation_text = unit.text
    app._last_dictation_length = len(unit.text)
    app._last_dictation_hwnd = unit.after[0]
    app._arm_undo_timer()
    app.play_sound('success')
    app._show_outcome_chip('Redone', 'success')
    return True
