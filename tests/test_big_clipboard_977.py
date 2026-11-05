"""986 regression cases (brief retains filename 977): fake Win32 only."""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery, paste
from samsara import session_modes as sm
from tests.test_clipboard_audit6 import fake, target
from tests.test_clipboard_big_image_776 import session


def commit(s):
    original = dict(s.fake.formats)
    text = s.manager.dictate_pending_buffer
    result = s.manager.commit_pending_dictation()
    assert result.kind == 'dictate_committed'
    s.typed.assert_called_once_with(text)
    assert not s.manager.dictate_pending_buffer
    assert s.fake.formats == original and s.fake.empties == 0
    assert s.target.pasted == []
    assert s.manager._clipboard_choice is None
    return result


@pytest.mark.parametrize('size_mb', [50, 100, 200])
def test_large_text_preserves_bytes_or_types_without_clipboard(session, monkeypatch, size_mb):
    s = session
    s.fake.formats = {cb.CF_UNICODETEXT: b'synthetic original text\0\0'}
    s.fake.size_override = {cb.CF_UNICODETEXT: size_mb * 1024 * 1024}
    original = dict(s.fake.formats)
    text = s.manager.dictate_pending_buffer
    if size_mb > 100:
        commit(s)
    else:
        # Virtual large allocation: assert requested byte count without
        # allocating 200 MB in a regression test or touching native memory.
        read = Mock(side_effect=s.fake.string_at)
        monkeypatch.setattr(cb.ctypes, 'string_at', read)
        result = s.manager.commit_pending_dictation()
        assert result.kind == 'dictate_committed'
        assert read.call_args.args[1] == size_mb * 1024 * 1024
        s.typed.assert_not_called()
        assert s.target.pasted == [text]
        assert s.fake.formats == original


@pytest.mark.parametrize('fmt', [cb.CF_DIB, cb.CF_BITMAP])
def test_large_or_unsnapshottable_image_types_without_loss(session, fmt):
    session.fake.formats = {fmt: b'synthetic image'}
    session.fake.size_override = {fmt: 200 * 1024 * 1024}
    commit(session)


def test_many_formats_total_memory_budget_types_and_keeps_all_formats(session, monkeypatch):
    s = session
    s.fake.formats = {0xC001 + i: b'synthetic format' for i in range(8)}
    s.fake.size_override = {fmt: 50 * 1024 * 1024 for fmt in s.fake.formats}
    read = Mock(side_effect=s.fake.string_at)
    monkeypatch.setattr(cb.ctypes, 'string_at', read)
    commit(s)
    assert read.call_count == 2


def test_format_count_budget_stops_enumeration_and_keeps_clipboard(session, monkeypatch):
    s = session
    s.fake.formats = {0xC001 + i: b'x' for i in range(200)}
    read = Mock(side_effect=s.fake.string_at)
    monkeypatch.setattr(cb.ctypes, 'string_at', read)
    commit(s)
    assert read.call_count == cb._MAX_SNAPSHOT_FORMATS


def test_delayed_render_after_responsive_ping_is_bounded_single_flight(session, monkeypatch):
    s = session
    s.fake.owner = 42  # WM_NULL responds, but GetClipboardData then stalls.
    s.fake.formats = {cb.CF_UNICODETEXT: b'synthetic delayed text\0\0'}
    s.fake.size_override = {}
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    read = s.fake.GetClipboardData
    calls = []

    def delayed(fmt):
        calls.append(fmt)
        entered.set()
        assert release.wait(2), 'test must release the fake provider'
        return read(fmt)

    close = s.fake.CloseClipboard

    def closed():
        result = close()
        finished.set()
        return result

    # Use a real monotonic clock for the timeout, never a real Win32 object.
    monkeypatch.setattr(cb.time, 'monotonic', time.monotonic)
    monkeypatch.setattr(cb._user32, 'GetClipboardData', delayed)
    monkeypatch.setattr(cb._user32, 'CloseClipboard', closed)
    monkeypatch.setattr(cb, '_SNAPSHOT_TIMEOUT_S', 0.05)
    original = dict(s.fake.formats)
    started = time.monotonic()
    try:
        commit(s)
        assert entered.is_set() and time.monotonic() - started < 1
        # Repeated attempts cannot spawn an unbounded set of stuck readers.
        for _ in range(3):
            snapshot = cb.save_clipboard()
            assert not snapshot.complete and not snapshot
            assert 'busy' in snapshot.lost[0]
        assert len(calls) == 1
    finally:
        release.set()
        assert finished.wait(1)
        # Wait for the worker's final publication before fixtures unpatch APIs.
        assert cb._snapshot_worker_lock.acquire(timeout=1)
        cb._snapshot_worker_lock.release()
    assert s.fake.formats == original and not s.fake.is_open
    assert cb.save_clipboard().complete  # a later healthy read can recover


def test_timed_out_backup_does_not_read_late_payload(session, monkeypatch):
    s = session
    s.fake.formats = {cb.CF_UNICODETEXT: b'synthetic original'}
    read = s.fake.GetClipboardData

    def delayed(fmt):
        handle = read(fmt)
        cb.time.sleep(cb._SNAPSHOT_TIMEOUT_S + .01)
        return handle

    monkeypatch.setattr(cb._user32, 'GetClipboardData', delayed)
    payload = Mock(side_effect=s.fake.string_at)
    monkeypatch.setattr(cb.ctypes, 'string_at', payload)
    commit(s)
    payload.assert_not_called()


@pytest.mark.parametrize('guard', ['focus', 'integrity'])
def test_fallback_safety_guards_refuse_input_and_keep_draft(session, monkeypatch, guard):
    s = session
    if guard == 'focus':
        monkeypatch.setattr(s.manager, '_foreground_verdict', lambda: type('V', (), {'blocked': True})())
    else:
        monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                            lambda *_: type('V', (), {'blocked': True, 'describe': lambda _: 'test'})())
    original = dict(s.fake.formats)
    assert s.manager.commit_pending_dictation().kind != 'dictate_committed'
    s.typed.assert_not_called()
    assert s.manager.dictate_pending_buffer
    assert s.fake.formats == original and not s.target.pasted


def test_typing_failure_keeps_exact_draft_and_offers_existing_image_recovery(session):
    s = session
    s.typed.return_value = False
    text = s.manager.dictate_pending_buffer
    original = dict(s.fake.formats)
    outcome = s.manager.commit_pending_dictation()
    assert outcome.kind == 'dictate_clipboard_choice'
    assert s.manager.dictate_pending_buffer == text and s.fake.formats == original
    assert 'type it' in sm.outcome_chip(outcome.kind, outcome.detail)[0]
    assert not s.target.pasted and s.fake.empties == 0


def test_standalone_typing_failure_is_visible_and_clipboard_unchanged(session):
    s = session
    s.typed.return_value = False
    s.app._show_outcome_chip = Mock()
    original = dict(s.fake.formats)
    assert paste.paste_preserving_clipboard(s.app, 'Synthetic failed delivery.',
                type_text_unicode_fn=s.typed, get_foreground_hwnd_fn=lambda: 42) is False
    message, severity = s.app._show_outcome_chip.call_args.args
    assert 'History' in message and 'Copy' in message and severity == 'error'
    assert 'Clipboard backup failed' in message
    assert s.fake.formats == original and not s.target.pasted
    assert recovery.current_attempt() is None


@pytest.mark.parametrize('standalone', [False, True])
def test_restore_allocation_failure_is_visible_after_text_sent(session, standalone):
    s = session
    s.fake.formats = {cb.CF_UNICODETEXT: b'original\0\0'}
    s.fake.size_override = {}
    # 1146: protected delivery allocates too; fail the first RESTORE allocation.
    s.target.after_paste.append(lambda: setattr(s.fake, 'alloc_fail_on', s.fake.alloc_calls + 1))
    s.app._show_outcome_chip = Mock()
    if standalone:
        assert paste.paste_preserving_clipboard(s.app, 'Synthetic sent text.',
                    type_text_unicode_fn=s.typed, get_foreground_hwnd_fn=lambda: 42)
        assert s.app._show_outcome_chip.call_args.args == (recovery.RESTORE_MESSAGE, 'warning')
    else:
        result = s.manager.commit_pending_dictation()
        assert result.kind == 'dictate_committed'
        assert sm.outcome_chip(result.kind, result.detail) == (recovery.RESTORE_MESSAGE, 'warning')
    # 1146: one native delivery replace; failed restore must not clear it again.
    assert len(s.target.pasted) == 1 and s.fake.empties == 1


def test_failed_typed_first_attempt_is_not_typed_again(session):
    s = session
    s.app.config['paste_min_chars'] = 5000
    s.app._foreground_wants_typed_injection = lambda: True
    s.typed.return_value = False
    s.manager.commit_pending_dictation()
    s.typed.assert_called_once()
    assert not s.target.pasted and s.manager.dictate_pending_buffer


def test_failed_wake_delivery_saves_exact_text_and_announces_recovery():
    app = SimpleNamespace(add_to_history=Mock(), _log_history=Mock(), _show_outcome_chip=Mock())
    text = ' Synthetic words: café, 漢字, 😀.\nSecond line. '
    paste.retain_failed_dictation(app, text, text)
    app.add_to_history.assert_called_once_with(text, is_command=False)
    app._log_history.assert_called_once_with(raw_text=text, display_text=text, mode='wake',
                                           status='failed', entry_type='dictation')
    app._show_outcome_chip.assert_called_once_with(paste.FAILED_DELIVERY, 'error')


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('restored', [False, True])
def test_failure_and_restore_warning_reach_actual_displays(qapp, monkeypatch, display, restored):
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator
    app = SimpleNamespace(config={'ui': {'live_surface': {'enabled': display == 'Full'}}},
                          _schedule_ui=lambda fn, *a, **kw: fn(*a, **kw), play_sound=Mock())
    controller = None
    if display == 'Full':
        pending = []
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', pending.append)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel())
        app.live_surface = controller
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        indicator = ListeningIndicator()
    app.listening_indicator = indicator
    app._show_outcome_chip = lambda label, kind, *ttl: schedule_feedback(app, label, kind, None)
    receipt = recovery.Attempt(restore_failed=restored, backup_failed=not restored)
    token = recovery._attempt.set(receipt)
    expected = recovery.RESTORE_MESSAGE if restored else paste.CLIPBOARD_FAILED_DELIVERY
    severity = 'warning' if restored else 'error'
    try:
        paste.show_delivery_result(app, restored, cb.DeliveryOutcome.SENT)
        if controller:
            while pending:
                pending.pop(0)()
            controller.drain()
            assert controller.view().notice.message == expected
        else:
            assert indicator._active_chip() == (expected, severity)
        # Optional handover evidence is produced by the same real feedback seam.
        import os
        from pathlib import Path
        if output := os.environ.get('CLIPBOARD_986_ARTIFACTS'):
            widget = controller.widget if controller else indicator
            widget.show()
            qapp.processEvents()
            name = display.lower() + ('-restore-warning.png' if restored else '-backup-failed.png')
            assert widget.grab().save(str(Path(output) / name))
    finally:
        recovery._attempt.reset(token)
        (controller or indicator).close()
        qapp.processEvents()
