"""Keep hands-free drafts separate from an ordinary hold's capture."""
from __future__ import annotations


COMPACT_KEPT_DRAFT_HINT = "Kept draft: restart hands-free to send or clear."
PAUSED_HOLD_HISTORY_HINT = "Saved to History. The kept draft is unchanged."


def quit_draft_waiting(app, manager) -> bool:
    """Compact's retained snapshot needs the same exit decision as Full."""
    return manager is not None and (
        bool(getattr(manager, 'retained_draft', '').strip())
        or (app._live_surface_hold_parking_enabled()
            and getattr(manager, 'has_parked_hold', False)))


def resolve_quit_choice(manager, choice) -> bool:
    """Use Full's explicit commit path without starting a hands-free session."""
    retained = bool(getattr(manager, 'retained_draft', '').strip())
    if choice == 'discard':
        if retained:
            manager.discard_retained_draft()
        else:
            manager.confirm_clear_draft('quit discard')
        return True
    if choice != 'send':
        return False
    if not retained:
        return manager.commit_pending_dictation().kind == 'dictate_committed'
    # Restore the retained text/audio/undo metadata, then allow the trusted
    # local commit in the idle lane, just as a parked Full draft does.
    manager._restore_retained_draft()
    try:
        # pause_draft is Full-only. Arm the same parked-document latch here
        # for this explicit local send, without changing the selected display.
        manager._parked_hold = True
        return manager.commit_pending_dictation().kind == 'dictate_committed'
    finally:
        if manager.dictate_pending_buffer:
            # A refused/failed send keeps Compact's snapshot and reminder.
            manager.retain_draft()
            manager.clear_pending_draft()


def kept_outside_session(app, manager, *, live_capture=False) -> bool:
    document = getattr(manager, 'draft_document', None)
    return (((getattr(document, 'kept_session', False) is True
              and bool(getattr(document, 'text', '')))
             or bool(getattr(manager, 'retained_draft', '').strip()))
            and not (live_capture or getattr(app, 'command_mode_active', False)))


def session_draft_waiting(app, manager, *, live_capture=False) -> bool:
    document = getattr(manager, 'draft_document', None)
    return kept_outside_session(app, manager) or (
        (live_capture or getattr(app, 'command_mode_active', False))
        and bool(getattr(document, 'text', '')
                 or getattr(manager, 'retained_draft', '').strip()))


def suspend_hands_free_for_hold(app) -> None:
    """Dispatch the older chunk once before the hold owns subsequent audio."""
    from samsara.session_modes import SessionMode
    wake = app._wake_consumer
    manager = getattr(app, '_session_mode_manager', None)
    ordinary = not any(getattr(app, name, False) for name in
                       ('command_mode_recording', 'ava_mode_recording', '_memo_recording'))
    if (ordinary and getattr(app, 'command_mode_active', False)
            and getattr(manager, 'mode', None) is SessionMode.DICTATE
            and app.config.get('hands_free', {}).get('suspend_on_hold', True)):
        wake.flush_utterance()
        # The ordinary no-media pre-roll must not recapture the flushed chunk.
        onset = getattr(app, '_hold_capture_onset', None)
        if onset is not None:
            import time
            onset.policy = 'strict'
            onset.boundary = max(onset.boundary or 0.0, time.perf_counter())
    wake.discard_stale_wake_utterance()


def sync_session_draft(controller, manager) -> None:
    """An older hands-free final cannot paint its draft over a current hold."""
    from samsara.live_surface.model import Lane
    if getattr(controller, '_active_lane', None) is not Lane.HOLD:
        controller.sync_draft(manager)


def on_session_final(preview, text, **kwargs) -> None:
    """A late older final updates the draft, retaining Compact's hold pause."""
    preview.on_utterance_final(text, **kwargs)
    if not preview._closed and preview._suspended_for_hold():
        preview._overlay.set_paused(list(preview._finalized))


def pause_hold(app, manager, *, source) -> None:
    """The control parks this capture, without altering a session document."""
    if not session_draft_waiting(app, manager):
        manager.pause_draft(source=source)


def begin_hold(partials):
    """Suspend only the display projection; the document remains authoritative."""
    from samsara.live_surface.model import EventKind, Lane, SurfaceEvent
    controller = partials.controller
    app = getattr(controller, 'app', None)
    manager = getattr(app, '_session_mode_manager', None)
    if partials.lane is Lane.HOLD and session_draft_waiting(app, manager):
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
    if owns_display and session_draft_waiting(app, manager):
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
                    COMPACT_KEPT_DRAFT_HINT,
                    'accent', None)
        QTimer.singleShot(int(delay * 1000) + 1, restore)

    qt_runtime.post(schedule)


def park_hold(app, manager, text, *, source, live_capture=False) -> bool:
    """A refused/reviewed hold must not become part of a session draft."""
    if session_draft_waiting(app, manager, live_capture=live_capture):
        return False
    if source in ('focus changed', 'voice review'):
        manager.pause_draft(source=source)
    manager.stage_parked_hold(text, source=source)
    return True


def park_batch_hold(app, manager, text, raw_text, duration_s, *, source) -> None:
    """The CPU final follows the same draft separation and receipt as streaming."""
    if park_hold(app, manager, text, source=source):
        controller = getattr(app, 'live_surface', None)
        if controller is not None:
            controller.sync_draft(manager)
        app._park_hold_feedback('Draft parked', 'success')
        return
    app.add_to_history(text.strip(), is_command=False)
    app._log_history(raw_text=raw_text, display_text=text.strip(),
                     duration_ms=int(duration_s * 1000), mode='hold',
                     status='failed' if source == 'focus changed' else 'success',
                     entry_type='dictation')
    app._park_hold_feedback(
        'Send refused; target focus changed' if source == 'focus changed'
        else PAUSED_HOLD_HISTORY_HINT,
        'error' if source == 'focus changed' else 'accent')


def park_streaming_hold(session, manager, text, raw_text, duration_s, *, source):
    if park_hold(session.app, manager, text, source=source,
                 live_capture=getattr(session, '_live_session_capture', False)):
        return
    from samsara import tutorial_capture
    if tutorial_capture.record_final(session, text, raw_text, duration_s):
        show = getattr(session.app, '_show_outcome_chip', None)
        if callable(show):
            if source == 'focus changed':
                show('Send refused; target focus changed', 'error')
            elif source == 'failed delivery':
                show('Delivery failed; draft kept', 'error')
            else:
                show(PAUSED_HOLD_HISTORY_HINT, 'accent')


def session_appends_to_draft(session, manager) -> bool:
    """A waiting document never supplies consent to park a new hold."""
    return False

