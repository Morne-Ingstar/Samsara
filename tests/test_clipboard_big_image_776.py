"""776: synthetic clipboard + real session/paste seams; never owner data."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery, paste
from samsara import session_modes as sm
from tests.test_clipboard_audit6 import fake, target  # reusable, fully fake Win32 transport


@pytest.fixture
def session(fake, target, monkeypatch):
    # 826b/887 / 888a: privacy metadata is synthetic; never probe the owner's editor.
    from samsara import recent_privacy
    monkeypatch.setattr(recent_privacy, 'focused_target',
                        lambda: recent_privacy.TargetPrivacy('', 'editor.exe'))
    fake.formats = {cb.CF_DIB: b'synthetic pixels', cb.CF_BITMAP: b'fake GDI handle'}
    fake.size_override[cb.CF_DIB] = cb._MAX_FORMAT_BYTES + 1
    monkeypatch.setattr(cb._user32, 'CreateWindowExW', Mock(return_value=777), raising=False)
    monkeypatch.setattr(cb._user32, 'DestroyWindow', Mock(return_value=True), raising=False)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', lambda: cb.DeliveryOutcome.SENT)
    monkeypatch.setattr(cb, '_paste_target', lambda: (42, 42))
    monkeypatch.setattr(cb, '_paste_target_matches', lambda *_: True)
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'editor.exe')
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda *_: SimpleNamespace(blocked=False, confirmed_ok=True))
    foreground = ['editor.exe', 42]
    typed = Mock(return_value=True)
    app = SimpleNamespace(config={'paste_min_chars': 300}, _foreground_wants_typed_injection=lambda: False,
        _record_undoable_paste=Mock(), adaptive_learner=SimpleNamespace(record_transcription=Mock()),
        _flight_foreground_process_name=lambda: 'editor.exe', _schedule_ui=lambda fn: fn())

    def inject(text, guard):
        # Mirrors the app's legacy bool-returning boundary; context must carry
        # refusal and consent across this callback without changing its API.
        result = paste.paste_preserving_clipboard(app, text, before_paste=guard,
            return_delivery_confirmation=True, type_text_unicode_fn=typed,
            get_foreground_hwnd_fn=lambda: foreground[1])
        return sm.InjectionDelivery(text, confirmed=result[1]) if result[0] else False

    manager = sm.SessionModeManager(abort_phrases=['abort'], foreground_exe_resolver=lambda: foreground[0],
        foreground_hwnd_resolver=lambda: foreground[1], inject_fn=inject, remove_chars_fn=lambda *_: None,
        command_dispatch_fn=lambda *_: sm.CommandDispatchResult(matched=False), agent_dispatch_fn=lambda *_: None,
        buffer_dictate_until_commit=True)
    manager.reset(sm.SessionMode.DICTATE)
    manager._dictate_pending_buffer = 'Synthetic retained draft. ' * 24
    app._session_mode_manager = manager
    return SimpleNamespace(manager=manager, app=app, typed=typed, foreground=foreground,
                           fake=fake, target=target)


def offer(s):
    outcome = s.manager.commit_pending_dictation()
    assert outcome.kind == 'dictate_clipboard_choice'
    assert "image Samsara can't put back." in sm.outcome_chip(outcome.kind, outcome.detail)[0]
    assert s.manager.dictate_pending_buffer
    assert s.fake.empties == 0 and not s.target.pasted
    s.typed.assert_not_called()
    return outcome


@pytest.mark.parametrize('bitmap_only', [False, True])
def test_over_cap_and_bitmap_only_offer_without_touching_image(session, bitmap_only):
    if bitmap_only:
        session.fake.formats = {cb.CF_BITMAP: b'synthetic bitmap'}
    original = dict(session.fake.formats)
    offer(session)
    assert session.fake.formats == original
    assert session.manager._clipboard_choice is not None


def test_allowlist_does_not_silently_choose_typing_after_image_refusal(session):
    session.app._foreground_wants_typed_injection = lambda: True
    offer(session)


def test_explicit_typing_keeps_clipboard_and_clears_draft_once(session):
    offer(session)
    text = session.manager.dictate_pending_buffer
    original = dict(session.fake.formats)
    result = recovery.answer(session.manager, 'type it')
    assert result.kind == 'dictate_committed'
    session.typed.assert_called_once_with(text)
    assert session.fake.formats == original and session.fake.empties == 0
    assert not session.manager.dictate_pending_buffer
    assert recovery.answer(session.manager, 'type it') is None


def test_typing_failure_never_falls_back_to_clipboard(session):
    offer(session)
    session.typed.return_value = False
    result = recovery.answer(session.manager, 'type it')
    assert result.kind == 'dictate_commit_failed'
    assert session.manager.dictate_pending_buffer and session.fake.empties == 0
    assert not session.target.pasted
    assert session.manager._clipboard_choice is not None


def test_paste_anyway_is_explicit_single_use_and_reports_image_loss(session):
    offer(session)
    text = session.manager.dictate_pending_buffer
    result = recovery.answer(session.manager, 'paste anyway')
    assert result.kind == 'dictate_committed'
    assert session.target.pasted == [text]
    assert session.fake.formats == {cb.CF_UNICODETEXT: (text+'\0').encode('utf-16-le')}
    assert not session.manager.dictate_pending_buffer
    label, severity = sm.outcome_chip(result.kind, result.detail)
    assert recovery.LOSS_MESSAGE in label
    assert 'unconfirmed' in label and severity == 'warning'
    assert recovery.answer(session.manager, 'paste anyway') is None


@pytest.mark.parametrize('stage', ['before_retry', 'while_opening'])
def test_new_copy_invalidates_consent_and_is_never_overwritten(session, stage):
    offer(session)
    if stage == 'before_retry':
        session.fake.user_copies_text('synthetic later copy')
    else:
        # Snapshot open succeeds first; change occurs when replacement opens.
        original_open = cb._open_clipboard_with_retry
        calls = []
        def opening(**kwargs):
            calls.append(1)
            if len(calls) == 2:
                session.fake.before_open = lambda: session.fake.user_copies_text('synthetic later copy')
            return original_open(**kwargs)
        cb_open = pytest.MonkeyPatch()
        cb_open.setattr(cb, '_open_clipboard_with_retry', opening)
    try:
        result = recovery.answer(session.manager, 'paste anyway')
        assert result.kind == 'dictate_commit_failed'
        assert session.fake.text() == 'synthetic later copy'
        assert session.fake.empties == 0 and not session.target.pasted
        assert session.manager.dictate_pending_buffer
    finally:
        if stage == 'while_opening':
            cb_open.undo()


@pytest.mark.parametrize('change', ['draft', 'hwnd', 'process', 'mode', 'clear', 'reset'])
def test_stale_choice_cannot_deliver(session, change):
    offer(session)
    if change == 'draft':
        session.manager._dictate_pending_buffer += ' Added synthetic words.'
    elif change == 'hwnd':
        session.foreground[1] = 99
    elif change == 'process':
        session.foreground[0] = 'other.exe'
    elif change == 'mode':
        session.manager.mode = sm.SessionMode.COMMAND
    elif change == 'clear':
        session.manager.clear_pending_draft()
    else:
        session.manager.reset(sm.SessionMode.DICTATE)
    result = recovery.answer(session.manager, 'paste anyway')
    assert result is None or result.kind == 'dictate_commit_failed'
    assert session.fake.empties == 0 and not session.target.pasted


@pytest.mark.parametrize('failure', ['allocation', 'set_data', 'focus', 'readiness'])
def test_recovery_failures_keep_draft_and_report_actual_clipboard_loss(session, monkeypatch, failure):
    offer(session)
    if failure == 'allocation':
        session.fake.alloc_fail_on = session.fake.alloc_calls + 1
    elif failure == 'set_data':
        session.fake.set_fail_fmts.add(cb.CF_UNICODETEXT)
    elif failure == 'focus':
        monkeypatch.setattr(session.manager, '_foreground_verdict', lambda: SimpleNamespace(blocked=True))
    else:
        monkeypatch.setattr(cb, 'wait_for_clipboard_ready', lambda *_: 'timeout')
    result = recovery.answer(session.manager, 'paste anyway')
    assert result.kind != 'dictate_committed'
    assert session.manager.dictate_pending_buffer and not session.target.pasted
    label = sm.outcome_chip(result.kind, result.detail)[0]
    assert (recovery.LOSS_MESSAGE in label) == (failure in ('set_data', 'readiness'))
    assert not session.fake.allocated


def test_target_change_after_preparation_prevents_input(session, monkeypatch):
    offer(session)
    def readiness(*_):
        session.foreground[1] = 99
        return 'ready'
    monkeypatch.setattr(cb, 'wait_for_clipboard_ready', readiness)
    result = recovery.answer(session.manager, 'paste anyway')
    assert result.kind == 'dictate_commit_blocked_focus_lock'
    assert session.manager.dictate_pending_buffer and not session.target.pasted
    assert result.detail.get('clipboard_image_removed')  # loss must not be hidden by focus failure


def test_no_choice_or_non_reply_never_grants_consent(session):
    assert recovery.answer(session.manager, 'paste anyway') is None
    offer(session)
    assert recovery.answer(session.manager, 'please paste anyway after lunch') is None
    assert recovery.answer(session.manager, 'paste anyway', allowed=False) is None
    assert session.fake.empties == 0 and session.manager.dictate_pending_buffer


def test_voice_reply_reuses_pending_choice_and_hallucination_gate(session):
    offer(session)
    signals = sm.UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
    result = session.manager.dispatch_utterance('type it', signals)
    assert result.kind == 'dictate_committed'
    session.typed.assert_called_once()


def test_only_verified_opener_blames_another_app(session, monkeypatch):
    monkeypatch.setattr(cb._user32, 'GetOpenClipboardWindow', lambda: 0)
    assert 'another app' not in recovery.clipboard_failure_message()
    monkeypatch.setattr(cb._user32, 'GetOpenClipboardWindow', lambda: 123)
    assert recovery.clipboard_failure_message() == 'Paste blocked - clipboard held by another app'


@pytest.mark.parametrize('sequence', [None, 0])
def test_no_sequence_cannot_authorize_loss(session, sequence):
    offer(session)
    pending = list(session.manager._clipboard_choice)
    pending[3] = sequence
    session.manager._clipboard_choice = tuple(pending)
    result = recovery.answer(session.manager, 'paste anyway')
    assert result.kind == 'dictate_commit_failed'
    assert 'version unavailable' in result.detail['error']
    assert session.fake.empties == 0 and session.manager.dictate_pending_buffer


def test_old_popup_cannot_answer_a_new_offer(session):
    offer(session)
    old = session.manager._clipboard_choice
    offer(session)
    result = recovery.answer(session.manager, 'paste anyway', expected=old)
    assert result.kind == 'dictate_commit_failed'
    assert session.manager._clipboard_choice is not old
    assert session.fake.empties == 0


def test_mixed_unreadable_content_refuses_without_image_only_consent(session):
    session.fake.formats[cb.CF_HDROP] = b'synthetic file copy'
    session.fake.no_data_formats.add(cb.CF_HDROP)
    result = session.manager.commit_pending_dictation()
    assert result.kind == 'dictate_commit_failed'
    assert 'CF_HDROP' in result.detail['error']
    assert session.manager._clipboard_choice is None
    assert session.fake.empties == 0


def test_bad_voice_evidence_does_not_grant_clipboard_consent(session):
    offer(session)
    result = session.manager.dispatch_utterance('paste anyway', sm.UtteranceSignals(False, (9.0,)))
    assert result.kind != 'dictate_committed'
    assert session.fake.empties == 0 and session.manager.dictate_pending_buffer


@pytest.mark.parametrize('display', ['Full', 'Compact'])
def test_actual_message_click_opens_both_choices_without_focus(session, qapp, monkeypatch, display):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from samsara.live_surface.controller import LiveSurfaceController
    from samsara.live_surface.model import NoticeKind, SurfaceEvent, EventKind
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    offer(session)
    session.app.config['ui'] = {'live_surface': {'enabled': display == 'Full'}}
    session.app._handle_session_dispatch_outcome = Mock()
    if display == 'Full':
        controller = LiveSurfaceController(session.app)
        controller.refresh_expired = lambda: None
        widget = LiveSurfaceWidget(controller, reduced_motion=True)
        controller.widget = widget; controller._connect_intents()
        controller.model.consume(SurfaceEvent(EventKind.NOTICE, notice=NoticeKind.ERROR,
                                             message=recovery.CHOICE_MESSAGE, persistent=True))
        controller.sync_draft(session.manager); controller.drain()
        widget.show(); qapp.processEvents()
        band = widget._chip or widget._band
        QTest.mouseClick(band, Qt.MouseButton.LeftButton)
    else:
        widget = ListeningIndicator(); widget.set_idle_animation(False)
        session.app.listening_indicator = widget
        recovery.bind_compact(session.app)
        widget.show_outcome(recovery.CHOICE_MESSAGE, 'warning', None)
        widget.dismiss_error()
    qapp.processEvents()
    try:
        panel = widget._clipboard_choices
        assert panel.isVisible()
        assert panel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        buttons = {b.text(): b for b in panel.findChildren(QPushButton)}
        assert {'Type it', 'Paste anyway', 'Keep draft'} == set(buttons)
        # Keep this test deterministic while exercising the real clicked route.
        monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, fn, **kwargs: fn())
        QTest.mouseClick(buttons['Type it'], Qt.MouseButton.LeftButton)
        assert not session.manager.dictate_pending_buffer
        session.typed.assert_called_once()
        session.app._handle_session_dispatch_outcome.assert_called_once()
    finally:
        if display == 'Compact': widget.destroy()
        else: widget.close()
        widget.deleteLater(); qapp.processEvents()
