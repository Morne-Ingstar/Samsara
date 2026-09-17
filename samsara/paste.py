"""Application-facing text delivery and single-level paste undo helpers.

The low-level clipboard snapshot/restore implementation lives in
``samsara.clipboard``.  This module owns the DictationApp seam that chooses
typed Unicode delivery, coordinates focus checks, and tracks native undo.
"""

import ctypes
import logging
import time

from samsara import flight_recorder, injection_safety
from samsara.clipboard import (
    paste_with_preservation,
    type_text_unicode,
)
from samsara.constants import CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY
from samsara.handlers import _get_foreground_hwnd
from samsara.runtime import thread_registry


logger = logging.getLogger("Samsara")

_UNDO_TARGET_UNSET = object()
_WAKE_PRIMER_DELAY = 0.12


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


def paste_preserving_clipboard(
    app,
    text,
    before_paste=None,
    return_delivery_confirmation=False,
    *,
    paste_with_preservation_fn=paste_with_preservation,
    type_text_unicode_fn=type_text_unicode,
    get_foreground_hwnd_fn=_get_foreground_hwnd,
):
    """Paste text through the app's guarded clipboard/type delivery seam."""
    # Queue 50: the single delivery chokepoint (hold, wake, session commit)
    # refuses a window Windows will not let us type into. Callers with
    # their own announcement (session outcome, hold path) check earlier;
    # this guard makes every other lane fail honestly instead of "pasting".
    def _result(sent, confirmed):
        return (sent, confirmed) if return_delivery_confirmation else sent

    _verdict = injection_safety.window_integrity()
    if _verdict.blocked:
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
    _typed_wanted = len(text) < threshold and app._foreground_wants_typed_injection()
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
            return _result(True, True)
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
        return True

    typed_fallback_used = {"value": False}

    def _typed_fallback_without_clipboard():
        if not app._foreground_wants_typed_injection() or not _capture_target_before_paste():
            return False
        typed_fallback_used["value"] = type_text_unicode_fn(text)
        return typed_fallback_used["value"]

    # Keep one clipboard implementation. The centralized path captures
    # the clipboard sequence number immediately after Samsara's copy, so
    # an unrelated copy made during the paste window is never overwritten
    # by restoring our stale snapshot.
    paste_ok = paste_with_preservation_fn(
        text,
        paste_delay=CLIPBOARD_PASTE_DELAY,
        restore_delay=delay,
        before_paste=_capture_target_before_paste,
        incomplete_snapshot_fallback=_typed_fallback_without_clipboard,
    )
    delivery_confirmed = bool(typed_fallback_used["value"])
    if paste_ok and delivery_confirmed:
        app._record_undoable_paste(
            text, target_hwnd=paste_target['hwnd'],
        )
        app.adaptive_learner.record_transcription(text)
        logger.info(
            "[TYPE] Unicode fallback sent chars=%d hwnd=%r" if typed_fallback_used["value"]
            else "[PASTE] Ctrl+V sent chars=%d hwnd=%r",
            len(text), get_foreground_hwnd_fn(),
        )
    elif paste_ok:
        logger.warning("[PASTE] Ctrl+V shortcut sent without target acknowledgement; undo is not armed")
    else:
        logger.error("[PASTE] Ctrl+V delivery failed; text retained by caller when possible")
    flight_recorder.record(
        'inject', path='clipboard',
        why=('typed_failed' if _typed_failed
             else 'over_paste_min_chars' if len(text) >= threshold
             else 'typed_injection_not_enabled_for_target'),
        target_process=app._flight_foreground_process_name(),
        chars=len(text), result='ok' if paste_ok else 'failed',
    )
    return _result(paste_ok, delivery_confirmed)


def deliver_text_to_focused_editor(app, text):
    """Remove the wake focus primer, then deliver text through the app seam."""
    import pyautogui

    # backspace removes focus-primer char; assumes empty input box at session start
    pyautogui.press('x')
    time.sleep(_WAKE_PRIMER_DELAY)
    pyautogui.press('backspace')
    time.sleep(_WAKE_PRIMER_DELAY)
    app._paste_preserving_clipboard(text)


def record_undoable_paste(
    app,
    text,
    target_hwnd=_UNDO_TARGET_UNSET,
    *,
    get_foreground_hwnd_fn=_get_foreground_hwnd,
):
    """Remember a paste and the exact window eligible for native undo."""
    app._last_dictation_text = text
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
    if app._undo_timer is not None:
        app._undo_timer.cancel()
        app._undo_timer = None


def undo_last_dictation(app, *, get_foreground_hwnd_fn=_get_foreground_hwnd):
    """Undo the last paste through the target application's undo stack."""
    import pyautogui

    if not app._last_dictation_text:
        logger.info("[UNDO] Nothing to undo")
        app.play_sound("error")
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
        return False

    text = app._last_dictation_text
    try:
        pyautogui.hotkey('ctrl', 'z')
    except Exception as exc:
        logger.exception("[UNDO] Ctrl+Z injection failed: %s", exc)
        app.play_sound("error")
        return False

    preview = text[:50] + ("..." if len(text) > 50 else "")
    logger.info(f"[UNDO] Native Ctrl+Z sent for: {preview}")
    app.play_sound("success")
    app._clear_undo()
    return True
