"""Keep session-exit drafts separate from an ordinary hold's capture."""
from __future__ import annotations


def kept_outside_session(app, manager, *, live_capture=False) -> bool:
    document = getattr(manager, 'draft_document', None)
    return (getattr(document, 'kept_session', False) is True
            and bool(getattr(document, 'text', ''))
            and not (live_capture or getattr(app, 'command_mode_active', False)))


def hold_appends_to_draft(app, manager, *, live_capture=False) -> bool:
    document = getattr(manager, 'draft_document', None)
    return bool(getattr(document, 'text', '')) and not kept_outside_session(
        app, manager, live_capture=live_capture)


def begin_hold(partials):
    """Suspend only the display projection; the document remains authoritative."""
    from samsara.live_surface.model import EventKind, Lane, SurfaceEvent
    controller = partials.controller
    app = getattr(controller, 'app', None)
    manager = getattr(app, '_session_mode_manager', None)
    if partials.lane is Lane.HOLD and kept_outside_session(app, manager):
        # No document id: this hides the projection without retiring the draft.
        controller.post(SurfaceEvent(EventKind.DOCUMENT_CLEARED))
    return partials


def finish_hold(partials) -> None:
    """Restore the kept projection only when this capture still owns the display."""
    controller = partials.controller
    owns_display = getattr(controller, '_active_capture', None) == partials.capture_id
    partials.stop()
    app = getattr(controller, 'app', None)
    manager = getattr(app, '_session_mode_manager', None)
    if owns_display and kept_outside_session(app, manager):
        controller.sync_draft(manager)


def capture_finished(session, callback) -> None:
    """Retire capture ownership, then restore Compact's retained-draft reminder."""
    callback(session)
    if session._parking_enabled:
        return
    app = session.app
    manager = getattr(app, '_session_mode_manager', None)
    indicator = getattr(app, 'listening_indicator', None)
    if not getattr(manager, 'retained_draft', '') or indicator is None:
        return
    from samsara.ui import qt_runtime

    def schedule():
        from PySide6.QtCore import QTimer
        from samsara.live_surface.model import RESULT_RECEIPT_S
        chip_sequence = getattr(app, '_hold_chip_seq', 0)
        deadline = getattr(indicator, '_chip_deadline', None)
        delay = (max(0.0, deadline - indicator._now_ms() / 1000)
                 if deadline is not None else RESULT_RECEIPT_S)

        def restore():
            if (getattr(app, '_streaming_session', None) is None
                    and not getattr(app, 'recording', False)
                    and not getattr(app, 'command_mode_active', False)
                    and getattr(app, '_hold_chip_seq', 0) == chip_sequence
                    and getattr(manager, 'retained_draft', '')
                    and not getattr(indicator, '_device_lost', False)
                    and not indicator._question_alive()
                    and not (getattr(indicator, '_chip_deadline', None) is None
                             and getattr(indicator, '_chip_kind', '') in ('error', 'warning', 'pending'))):
                indicator.show_outcome(
                    "Kept draft: restart hands-free to send or clear.",
                    'accent', None)
        QTimer.singleShot(int(delay * 1000) + 1, restore)

    qt_runtime.post(schedule)


def park_hold(app, manager, text, *, source, live_capture=False) -> bool:
    """A refused/reviewed hold must not become part of a kept session draft."""
    if kept_outside_session(app, manager, live_capture=live_capture):
        return False
    if source in ('focus changed', 'voice review'):
        manager.pause_draft(source=source)
    manager.stage_parked_hold(text, source=source)
    return True


def park_streaming_hold(session, manager, text, raw_text, duration_s, *, source):
    if park_hold(session.app, manager, text, source=source,
                 live_capture=getattr(session, '_live_session_capture', False)):
        return
    from samsara import tutorial_capture
    if tutorial_capture.record_final(session, text, raw_text, duration_s):
        show = getattr(session.app, '_show_outcome_chip', None)
        if callable(show):
            show('Send refused; target focus changed' if source == 'focus changed'
                 else 'Delivery failed; draft kept', 'error')


def session_appends_to_draft(session, manager) -> bool:
    return hold_appends_to_draft(session.app, manager,
        live_capture=getattr(session, '_live_session_capture', False))

