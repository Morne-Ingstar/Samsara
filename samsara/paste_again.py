"""Repeat saved dictation through the normal, target-guarded paste path (1163).

The receipt is memory-only. Never copy speech into diagnostics or clipboard
history, never retry automatically, and never return to the original target.
"""
from __future__ import annotations

import ctypes
import os
import threading

from samsara.handlers import _get_foreground_hwnd
from samsara.runtime import thread_registry


PHRASES = frozenset({'paste again', 'paste that again'})
CONTROL_MS = 15_000
_claim_lock = threading.Lock()
# PLACEHOLDER COPY (1163): ordinary delivery notes and control tooltips.


def _note(app, message):
    show = getattr(app, '_show_outcome_chip', None)
    if callable(show):
        show(message, 'info')


def remember_history(app, fields):
    """Observe only privacy-filtered dictation, including failed deliveries."""
    if fields.get('entry_type', 'dictation') != 'dictation':
        return
    if fields.get('status') in ('empty', 'transcribing'):
        return
    text = '' if fields.get('privacy_reason') else (fields.get('display_text') or fields.get('raw_text') or '')
    app._paste_again_text = text if isinstance(text, str) else ''
    _refresh_control(app)


def last_text(app):
    text = getattr(app, '_paste_again_text', None)
    if text is not None:
        return text
    store = getattr(app, 'history_store', None)
    if store is not None:
        try:
            rows = store.query(type_filter='dictation', limit=1)
            if rows:
                row = dict(rows[0])
                if not row.get('privacy_reason') and row.get('status') not in ('empty', 'transcribing'):
                    return row.get('display_text') or row.get('raw_text') or ''
        except Exception:
            pass
    return ''


def conceal(app, text):
    """A late privacy check also retires an earlier in-memory receipt."""
    saved = getattr(app, '_paste_again_text', None)
    if isinstance(saved, str) and saved.strip() == text.strip():
        app._paste_again_text = ''
        _refresh_control(app)


def _refresh_control(app):
    schedule = getattr(app, '_schedule_ui', None)
    if callable(schedule):
        try:
            schedule(lambda: _show_control(app))
        except Exception:
            pass  # Optional UI must never prevent privacy filtering or History.


def _show_control(app):
    from samsara.ui.paste_again_qt import show_control
    controller = getattr(app, 'live_surface', None)
    owner = getattr(controller, 'widget', None) if controller is not None else None
    if owner is None:
        owner = getattr(app, 'listening_indicator', None)
    if owner is not None:
        show_control(owner, app, bool(last_text(app).strip()))


def _external_target(hwnd):
    if not hwnd:
        return False
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return bool(pid.value and pid.value != os.getpid())


def request(app, text=None):
    """Capture the foreground NOW; the worker rechecks immediately at paste."""
    text = last_text(app) if text is None else text
    if not isinstance(text, str) or not text.strip():
        _note(app, 'Nothing to paste yet.')  # PLACEHOLDER COPY (1163)
        return False
    target = _get_foreground_hwnd()
    if not _external_target(target):
        _note(app, 'Choose a text window, then say “paste that again”.')
        return False
    with _claim_lock:
        if getattr(app, '_paste_again_busy', False):
            return False
        app._paste_again_busy = True

    def deliver():
        try:
            def same_target():
                return _get_foreground_hwnd() == target and _external_target(target)
            if not same_target():
                _note(app, 'The window changed. Say “paste that again” when you\'re ready.')
                return
            app._paste_preserving_clipboard(text, before_paste=same_target)
        finally:
            with _claim_lock:
                app._paste_again_busy = False

    try:
        thread_registry.spawn('dictation.paste_again', deliver, daemon=True)
    except Exception:
        app._paste_again_busy = False
        raise
    return True
