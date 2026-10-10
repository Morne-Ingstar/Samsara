"""Capture destinations and final-delivery guards, independent of Qt imports.

A tutorial registration is a step lifetime; an Owner is one capture lifetime.
Owners are chosen at onset, never from a waiting hook at delivery. Revoking a
registration drops its unfinished captures rather than pasting into our UI.
Audio buffers carry the owner through queued decoding without global state.
"""
from contextvars import ContextVar
from dataclasses import dataclass, field
import ctypes
import logging
import threading
import uuid

logger = logging.getLogger(__name__)
_current = ContextVar('tutorial_capture_owner', default=None)


def window_ready(hwnd):
    """Native checks can run on capture threads without touching Qt widgets."""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return bool(hwnd and user32.IsWindowVisible(ctypes.c_void_p(hwnd))
                    and not user32.IsIconic(ctypes.c_void_p(hwnd))
                    and user32.GetForegroundWindow() == hwnd)
    except (AttributeError, OSError):
        return False


@dataclass(eq=False)
class Registration:
    hwnd: int
    callback: object
    active: bool = True
    event: str = field(default_factory=lambda: 'dictation_done:' + uuid.uuid4().hex)
    lock: object = field(default_factory=threading.RLock)


@dataclass(eq=False)
class Owner:
    registration: Registration
    delivered: bool = False


def register(app, hwnd, callback):
    release(app)
    registration = Registration(hwnd, callback)
    app._tutorial_capture_registration = registration
    app._tutorial_hooks['dictation'] = callback
    return registration


def release(app):
    registration = getattr(app, '_tutorial_capture_registration', None)
    if registration is not None:
        with registration.lock:
            registration.active = False
        app._tutorial_capture_registration = None
    getattr(app, '_tutorial_hooks', {}).pop('dictation', None)


def begin(app):
    """Call before capture feedback can change focus; exclude non-dictation lanes."""
    excluded = ('command_mode_recording', 'ava_mode_recording', '_memo_recording',
                '_capture_sink', 'ava_command_session_active')
    if any(getattr(app, name, False) is True or
           (name == '_capture_sink' and bool(getattr(app, name, None))) for name in excluded):
        return None
    if getattr(app, 'command_mode_active', False) is True:
        manager = getattr(app, '_session_mode_manager', None)
        mode = getattr(getattr(manager, 'mode', None), 'value', None)
        if mode != 'dictate':
            return None
    registration = getattr(app, '_tutorial_capture_registration', None)
    if registration is None:
        return None
    with registration.lock:
        if registration.active and window_ready(registration.hwnd):
            return Owner(registration)
    return None


def current():
    return _current.get()


def resolve(value):
    """The old boolean keyword grants no ownership by itself."""
    return value if isinstance(value, Owner) else current() if value is True else None


def alive(app, owner):
    return (isinstance(owner, Owner) and owner.registration.active
            and getattr(app, '_tutorial_capture_registration', None) is owner.registration)


def accept_event(app, event):
    registration = getattr(app, '_tutorial_capture_registration', None)
    return bool(registration and registration.active and event == registration.event)


def deliver(app, owner, text):
    if not isinstance(owner, Owner) or not str(text or '').strip():
        return False
    with owner.registration.lock:
        if not alive(app, owner):
            return False
        if owner.delivered:
            return True
        try:
            owner.registration.callback(text)
        except Exception:
            logger.exception('Tutorial capture handoff failed')
            return False
        owner.delivered = True
        return True


def handoff(app, owner, text, confirmation=False):
    delivered = deliver(app, owner, text)
    if delivered:
        from samsara.paste import show_delivery_result
        show_delivery_result(app, True, True)
    return (delivered, delivered) if confirmation else delivered


def notify(app, kind, text):
    if kind == 'dictation':
        return deliver(app, current(), text)
    callback = getattr(app, '_tutorial_hooks', {}).pop(kind, None) if str(text or '').strip() else None
    if callback is None:
        return False
    try:
        callback(text)
        return True
    except Exception:
        logger.exception('Tutorial command hook failed')
        return False


class CaptureFrames(list):
    def __init__(self, frames=(), owner=None):
        super().__init__(frames)
        self.tutorial_owner = owner


def start_frames(app, frames=()):
    return CaptureFrames(frames, begin(app))


def copy_frames(frames):
    return CaptureFrames(frames, getattr(frames, 'tutorial_owner', None))


def carry(source, destination):
    destination.tutorial_owner = getattr(source, 'tutorial_owner', None)
    return destination


def run(frames, callback, *args, **kwargs):
    context = _current.set(getattr(frames, 'tutorial_owner', None))
    try:
        return callback(*args, **kwargs)
    finally:
        _current.reset(context)


def append_wake(app, text):
    """Owned chunks finish independently; ordinary buffered chunks stay ordinary."""
    if current() is not None:
        app._output_dictation(text)
        app._tutorial_wake_delivered = True
    else:
        app.wake_dictation_buffer.append(text)


def output_buffer(app, text):
    context = _current.set(None)
    try:
        return app._output_dictation(text)
    finally:
        _current.reset(context)


def install(manager):
    """Keep the normal lane dispatcher, with a destination seam for dictated chunks.

    Controls and command/Ava/task routing still run in the base dispatcher.
    Tutorial prose is handed off before it joins an external pending draft.
    A commit of an ordinary draft cannot borrow its control capture's owner.
    """
    from samsara.session_modes import DispatchOutcome
    if not hasattr(manager, '_dispatch_dictate') or getattr(manager, '_tutorial_seam', False):
        return manager
    dispatch, commit = manager._dispatch_dictate, manager._commit_dictate_buffer

    def dispatch_dictate(chunk_raw, audio_ref=None):
        if current() is None:
            return dispatch(chunk_raw, audio_ref=audio_ref)
        delivered = manager._inject_fn(chunk_raw.strip())
        return DispatchOutcome(
            kind='dictate_injected' if delivered is not False else 'dictate_commit_failed',
            detail={'text': chunk_raw.strip()},
        )

    def commit_dictate_buffer(**kwargs):
        context = _current.set(None)
        try:
            return commit(**kwargs)
        finally:
            _current.reset(context)

    manager._dispatch_dictate = dispatch_dictate
    manager._commit_dictate_buffer = commit_dictate_buffer
    manager._tutorial_seam = True
    return manager


def final_current(session, allow_done=False):
    """Check both cancellation and capture generation at an effect boundary."""
    event = getattr(session, 'cancel_event', None)
    if event is not None and event.is_set():
        return False
    if not allow_done and getattr(session.app, '_streaming_session', session) is not session:
        return False
    if not allow_done and getattr(session, '_state', None) == getattr(session, 'STATE_DONE', 'done'):
        return False
    owner = getattr(session, '_tutorial_owner', None)
    return owner is None or alive(session.app, owner)


def kwargs(owner):
    return {'tutorial_dictation': owner} if owner is not None else {}


def preserve_failed(app, text, raw_text, duration_s, mode):
    """A revoked destination never sends text elsewhere; keep it recoverable."""
    if not str(text or '').strip():
        return
    try:
        app.add_to_history(text.strip(), is_command=False)
        app._log_history(raw_text=raw_text if raw_text is not None else text,
                         display_text=text.strip(), duration_ms=int(duration_s * 1000),
                         mode=mode, status='failed', entry_type='dictation')
    except Exception:
        logger.exception('Revoked tutorial capture could not be saved to History')


def retire_final(session, text, raw_text, duration_s):
    event = getattr(session, 'cancel_event', None)
    owner = getattr(session, '_tutorial_owner', None)
    if (owner is not None and not owner.delivered and not alive(session.app, owner)
            and not (event and event.is_set()) and session._state != session.STATE_DONE):
        preserve_failed(session.app, text, raw_text, duration_s, 'streaming')
    session.cancel()


def revoked_hold(app, owner, text, raw_text, duration_s):
    if owner is None or alive(app, owner):
        return False
    preserve_failed(app, text, raw_text, duration_s, 'hold')
    return True


def finalize_wake(app):
    """Owned quick chunks still close the lane when its silence timer expires."""
    owned = getattr(app, '_tutorial_wake_delivered', False)
    if not app.wake_dictation_mode or app._dictation_require_end:
        return
    if not app.wake_dictation_buffer and not owned:
        return
    if app.app_state == 'wake_session' and getattr(app, '_wake_session_delivery_failed', False):
        return
    text = ' '.join(app.wake_dictation_buffer)
    if not text or output_buffer(app, text):
        app._tutorial_wake_delivered = False
        app._reset_wake_dictation()


def record_final(session, text, raw_text, duration_s):
    if not final_current(session, allow_done=True):
        return False
    app = session.app
    try:
        app.add_to_history(text.strip(), is_command=False)
    except Exception:
        logger.exception('Streaming add_to_history failed')
    try:
        app._log_history(raw_text=raw_text if raw_text is not None else text,
                         display_text=text.strip(), duration_ms=int(duration_s * 1000),
                         mode='streaming', status='success')
    except Exception:
        logger.exception('Streaming history record failed')
    try:
        app._notify_main_window(text.strip())
    except Exception:
        logger.exception('Streaming main-window notification failed')
    return True
