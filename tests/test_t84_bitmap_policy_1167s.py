"""1167s (T84 P2): bitmap synthesis, advisory owner ping and Paste-and-Warn.

Every test drives the production snapshot/paste/session routines against the
in-memory FakeClipboard (tests/clipboard_fake.py via the audit6 fixtures).
Nothing here touches a real clipboard, a real window or Snipping Tool; these
are simulations of Windows' documented conversions, not end-to-end proof of
what a real screenshot tool or Electron editor does.
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery, paste
from samsara import session_modes as sm
from tests.test_clipboard_audit6 import fake, target  # noqa: F401  (fixtures)
from tests.test_clipboard_big_image_776 import session  # noqa: F401  (fixture)

PNG = 0xC0B0
FILE_CONTENTS = 0xC0B1
RTF = 0xC0B2
NAMES = {PNG: 'PNG', FILE_CONTENTS: 'FileContents', RTF: 'Rich Text Format'}
DIB_BYTES = b'(BITMAPINFOHEADER+synthetic pixels)'


def _utf16(text):
    return (text + '\0').encode('utf-16-le')


def _names(monkeypatch):
    def name(fmt, buf, _n):
        buf.value = NAMES.get(fmt, '')
        return len(buf.value)
    monkeypatch.setattr(cb._user32, 'GetClipboardFormatNameW', name)


def _windows_conversion(fake, monkeypatch, result=DIB_BYTES):
    """Model the documented CF_BITMAP -> CF_DIB/CF_DIBV5 conversion.

    A requested DIB that is not advertised is produced from the bitmap, as
    Windows does on GetClipboardData. ``result`` None models a failed
    conversion (NULL handle). Records every request and every GlobalLock.
    """
    native_get, native_lock = fake.GetClipboardData, fake.GlobalLock
    requests, locked = [], []

    def get(fmt):
        requests.append(fmt)
        assert fmt != cb.CF_BITMAP, 'a GDI bitmap handle must never be requested'
        if fmt in (cb.CF_DIB, cb.CF_DIBV5) and fmt not in fake.formats and cb.CF_BITMAP in fake.formats:
            if result is None:
                return 0
            handle = fake._new_handle(bytearray(result))
            fake.handle_fmt[handle] = fmt
            return handle
        return native_get(fmt)

    def lock(handle):
        locked.append(fake.handle_fmt.get(handle))
        return native_lock(handle)

    monkeypatch.setattr(cb._user32, 'GetClipboardData', get)
    monkeypatch.setattr(cb._kernel32, 'GlobalLock', lock)
    return SimpleNamespace(requests=requests, locked=locked)


def _standalone(s, text='Synthetic dictated sentence.', **kwargs):
    s.app._show_outcome_chip = Mock()
    s.app.play_sound = Mock()
    s.app.listening_indicator = SimpleNamespace(flash_success=Mock())
    return paste.paste_preserving_clipboard(s.app, text, type_text_unicode_fn=s.typed,
                                            get_foreground_hwnd_fn=lambda: 42, **kwargs)


def _chip(s):
    s.app._show_outcome_chip.assert_called_once()
    return s.app._show_outcome_chip.call_args.args


def _no_success(s):
    s.app.play_sound.assert_not_called()
    s.app.listening_indicator.flash_success.assert_not_called()


def _transport_gone(fake, text):
    assert text not in fake.text()
    assert not any(fake.registered.get(n) in fake.formats for n in cb.PRIVATE_CLIPBOARD_FORMATS)


@pytest.fixture
def dictation(session):
    """The same session, delivering through the app's DEFAULT dictation seam."""
    app, typed, fg = session.app, session.typed, session.foreground

    def inject(text, guard):
        result = paste.paste_preserving_clipboard(app, text, before_paste=guard,
            return_delivery_confirmation=True, type_text_unicode_fn=typed,
            get_foreground_hwnd_fn=lambda: fg[1])
        return sm.InjectionDelivery(text, confirmed=result[1]) if result[0] else False

    session.manager._inject_fn = inject
    session.fake.formats = {}
    session.fake.size_override = {}
    return session


# ------------------------------------------------------------ 1. synthesis

def test_bitmap_only_backs_up_real_dib_bytes_without_touching_the_gdi_handle(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    conv = _windows_conversion(s.fake, monkeypatch)
    snap = cb.save_clipboard()
    assert snap.complete, snap.lost
    assert snap[cb.CF_DIB] == DIB_BYTES and snap.synthesized == {cb.CF_DIB}
    assert cb.CF_BITMAP not in conv.requests and cb.CF_BITMAP not in conv.locked
    assert snap.source_seq == s.fake.seq


def test_bitmap_png_filecontents_without_enumerated_dib_pastes_once_and_restores(dictation, monkeypatch):
    s = dictation
    _names(monkeypatch)
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP', PNG: b'\x89PNG synthetic', FILE_CONTENTS: b'x'}
    s.fake.no_data_formats.add(FILE_CONTENTS)
    conv = _windows_conversion(s.fake, monkeypatch)
    text = 'Synthetic full sentence for a screenshot clipboard.'
    assert _standalone(s, text) is True
    assert s.target.pasted == [text]
    s.typed.assert_not_called()
    assert s.fake.formats == {cb.CF_DIB: DIB_BYTES, PNG: b'\x89PNG synthetic'}  # bitmap restorable from DIB
    assert cb.CF_BITMAP not in conv.locked and cb.CF_BITMAP not in conv.requests
    assert s.app._show_outcome_chip.call_args.args[0] == 'typed'  # ordinary preserve route, no loss warning


def test_enumerated_empty_dib_is_not_coverage_and_dibv5_is_requested(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP', cb.CF_DIB: b''}
    conv = _windows_conversion(s.fake, monkeypatch)
    snap = cb.save_clipboard()
    assert snap.complete and snap.synthesized == {cb.CF_DIBV5}
    assert snap[cb.CF_DIBV5] == DIB_BYTES and cb.CF_DIB not in snap
    assert conv.requests == [cb.CF_DIB, cb.CF_DIBV5]  # enumeration order, then the conversion


@pytest.mark.parametrize('result', [None, b''])
def test_failed_or_empty_conversion_leaves_bitmap_honestly_lost(dictation, monkeypatch, result):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch, result)
    snap = cb.save_clipboard()
    assert not snap.complete and set(snap.lost) == {cb.CF_BITMAP}
    assert 'no image bytes' in snap.lost[cb.CF_BITMAP] and not snap and not snap.synthesized


def test_conversion_respects_the_per_format_cap_without_copying(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch)
    s.fake.size_override = {cb.CF_DIB: cb._MAX_FORMAT_BYTES + 1, cb.CF_DIBV5: cb._MAX_FORMAT_BYTES + 1}
    copy = Mock(side_effect=s.fake.string_at)
    monkeypatch.setattr(cb.ctypes, 'string_at', copy)
    snap = cb.save_clipboard()
    copy.assert_not_called()
    assert set(snap.lost) == {cb.CF_BITMAP} and '100 MB' in snap.lost[cb.CF_BITMAP]
    assert not s.fake.is_open


def test_conversion_counts_against_the_total_budget(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {0xC001: b'a', cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch)
    s.fake.size_override = {0xC001: 60 * 1024 * 1024, cb.CF_DIB: 60 * 1024 * 1024}
    monkeypatch.setattr(cb.ctypes, 'string_at', lambda ptr, size: b'x')
    snap = cb.save_clipboard()
    assert 'total' in snap.lost[0] and cb.CF_DIB not in snap
    assert not s.fake.is_open


def test_dibv5_alpha_is_kept_and_never_claimed_from_a_converted_dib(dictation, monkeypatch):
    s = dictation
    alpha = b'BITMAPV5HEADER+synthetic premultiplied alpha'
    s.fake.formats = {cb.CF_DIBV5: alpha, cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch)
    snap = cb.save_clipboard()
    assert snap.complete and snap[cb.CF_DIBV5] == alpha and not snap.synthesized
    # The owner advertises DIBV5 but provides nothing: a converted DIB covers
    # the bitmap, never the alpha-bearing DIBV5.
    s.fake.no_data_formats.add(cb.CF_DIBV5)
    snap = cb.save_clipboard()
    assert snap.synthesized == {cb.CF_DIB}
    assert set(snap.lost) == {cb.CF_DIBV5} and recovery.lost_is_image_only(snap)


def test_rich_text_and_file_drop_are_preserved_without_a_warning(dictation, monkeypatch):
    s = dictation
    _names(monkeypatch)
    original = {cb.CF_UNICODETEXT: _utf16('synthetic words'), RTF: b'{\\rtf1 synthetic}',
                cb.CF_HDROP: b'synthetic DROPFILES'}
    s.fake.formats = dict(original)
    assert _standalone(s) is True
    assert s.fake.formats == original
    assert _chip(s)[0] == 'typed'


# ------------------------------------------------------- 2. owner handling

def test_slow_owner_with_readable_image_and_text_is_preserved(dictation, monkeypatch):
    s = dictation
    s.fake.owner = 4242
    monkeypatch.setattr(cb._user32, 'SendMessageTimeoutW', lambda *a: 0)
    original = {cb.CF_UNICODETEXT: _utf16('synthetic slow owner'), cb.CF_DIB: DIB_BYTES}
    s.fake.formats = dict(original)
    assert _standalone(s) is True
    assert s.fake.formats == original and s.target.pasted


def test_blocked_delayed_render_is_bounded_single_flight_and_never_contaminates(dictation, monkeypatch):
    s = dictation
    s.fake.owner = 4242
    monkeypatch.setattr(cb._user32, 'SendMessageTimeoutW', lambda *a: 0)  # slow, not hung
    s.fake.formats = {cb.CF_UNICODETEXT: _utf16('synthetic delayed')}
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    native_get, native_close = s.fake.GetClipboardData, s.fake.CloseClipboard
    calls = []

    def stuck(fmt):
        calls.append(fmt)
        entered.set()
        assert release.wait(2)
        return native_get(fmt)

    def close():
        result = native_close()
        closed.set()
        return result

    monkeypatch.setattr(cb.time, 'monotonic', time.monotonic)
    monkeypatch.setattr(cb._user32, 'GetClipboardData', stuck)
    monkeypatch.setattr(cb._user32, 'CloseClipboard', close)
    monkeypatch.setattr(cb, '_SNAPSHOT_TIMEOUT_S', 0.05)
    original = dict(s.fake.formats)
    started = time.monotonic()
    try:
        # A timed-out snapshot has no sequence: it can never authorize the
        # destructive Paste-and-Warn. The clipboard is not replaced.
        _standalone(s)
        assert time.monotonic() - started < 1 and entered.is_set()
        assert s.fake.empties == 0 and not s.target.pasted
        for _ in range(3):
            snap = cb.save_clipboard()
            assert 'busy' in snap.lost[0] and getattr(snap, 'source_seq', None) is None
        assert len(calls) == 1  # one stuck reader, never one per attempt
    finally:
        release.set()
        assert closed.wait(1)
        assert cb._snapshot_worker_lock.acquire(timeout=1)
        cb._snapshot_worker_lock.release()
    assert s.fake.formats == original
    fresh = cb.save_clipboard()  # the late payload belongs to nobody
    assert fresh.complete and fresh[cb.CF_UNICODETEXT] == original[cb.CF_UNICODETEXT]


# ------------------------------------------- 3-6. Paste-and-Warn (standalone)

@pytest.mark.parametrize('case', ['bitmap_no_bytes', 'over_cap_image', 'over_cap_files', 'hung_owner'])
def test_incomplete_snapshot_pastes_once_and_warns(dictation, monkeypatch, case):
    s = dictation
    text = 'Synthetic paste and warn.'
    if case == 'bitmap_no_bytes':
        s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
        _windows_conversion(s.fake, monkeypatch, None)
        kept = {}
    elif case == 'over_cap_image':
        s.fake.formats = {cb.CF_UNICODETEXT: _utf16('caption'), cb.CF_DIB: b'huge'}
        s.fake.size_override = {cb.CF_DIB: cb._MAX_FORMAT_BYTES + 1}
        kept = {cb.CF_UNICODETEXT: _utf16('caption')}
    elif case == 'over_cap_files':
        s.fake.formats = {cb.CF_UNICODETEXT: _utf16('names'), cb.CF_HDROP: b'files'}
        s.fake.size_override = {cb.CF_HDROP: cb._MAX_FORMAT_BYTES + 1}
        kept = {cb.CF_UNICODETEXT: _utf16('names')}
    else:
        s.fake.formats = {cb.CF_UNICODETEXT: _utf16('frozen')}
        s.fake.owner = 4242
        monkeypatch.setattr(cb._user32, 'IsHungAppWindow', lambda _h: True)
        kept = {}
    assert _standalone(s, text) is True
    assert s.target.pasted == [text]          # exactly one clipboard paste
    s.typed.assert_not_called()               # never automatic fragile typing
    assert s.fake.formats == kept             # everything captured is put back
    _transport_gone(s.fake, text)
    message, severity = _chip(s)
    expected = (recovery.IMAGE_REMOVED_UNCONFIRMED if case in ('bitmap_no_bytes', 'over_cap_image')
                else recovery.RESTORE_MESSAGE)
    assert (message, severity) == (expected, 'warning')
    _no_success(s)


def test_complete_snapshot_takes_the_ordinary_route(dictation):
    s = dictation
    s.fake.formats = {cb.CF_UNICODETEXT: _utf16('ordinary')}
    assert _standalone(s) is True
    assert _chip(s) == ('typed', 'success', 900)


def test_confirmed_unconfirmed_and_not_sent_messages_are_distinct():
    receipt = recovery.Attempt(replaced=True)
    assert recovery.loss_message(receipt, True, cb.DeliveryOutcome.CONFIRMED)[0] == recovery.IMAGE_REMOVED_CONFIRMED
    assert recovery.loss_message(receipt, True, cb.DeliveryOutcome.UNVERIFIED)[0] == recovery.IMAGE_REMOVED_UNCONFIRMED
    assert recovery.loss_message(receipt, False, cb.DeliveryOutcome.NOT_SENT) == (
        recovery.IMAGE_REMOVED_NOT_SENT, 'error')
    receipt = recovery.Attempt(restore_failed=True)
    assert recovery.loss_message(receipt, True, cb.DeliveryOutcome.UNVERIFIED)[0] == recovery.RESTORE_UNCONFIRMED_MESSAGE
    assert recovery.loss_message(receipt, True, cb.DeliveryOutcome.CONFIRMED)[0] == recovery.RESTORE_MESSAGE
    assert recovery.loss_message(recovery.Attempt(backup_failed=True), True, 'sent') is None
    # Standalone image wording equals the session chip for the same receipt.
    for unverified, expected in ((True, recovery.IMAGE_REMOVED_UNCONFIRMED),
                                 (False, recovery.IMAGE_REMOVED_CONFIRMED)):
        detail = {'clipboard_image_removed': True, 'delivery_unverified': unverified}
        assert sm.outcome_chip('dictate_committed', detail) == (expected, 'warning')


def test_readback_confirmed_delivery_with_image_loss(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch, None)
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', lambda: cb.DeliveryOutcome.CONFIRMED)
    assert _standalone(s) is True
    assert _chip(s) == (recovery.IMAGE_REMOVED_CONFIRMED, 'warning')
    _no_success(s)


@pytest.mark.parametrize('when', ['before_replacement', 'while_preparing', 'before_restore'])
def test_external_copy_is_never_overwritten(dictation, monkeypatch, when):
    s = dictation
    s.fake.formats = {cb.CF_UNICODETEXT: _utf16('old'), cb.CF_DIB: b'huge'}
    s.fake.size_override = {cb.CF_DIB: cb._MAX_FORMAT_BYTES + 1}
    later = 'synthetic newer copy'
    if when == 'before_replacement':
        real = cb._copy_private_text_windows

        def copy(text, *a, **kw):
            s.fake.user_copies_text(later)
            return real(text, *a, **kw)
        monkeypatch.setattr(cb, '_copy_private_text_windows', copy)
    elif when == 'while_preparing':
        real_ready = cb.wait_for_clipboard_ready

        def ready(seq, settle):
            s.fake.user_copies_text(later)
            return real_ready(seq, settle)
        monkeypatch.setattr(cb, 'wait_for_clipboard_ready', ready)
    else:
        s.target.after_paste.append(lambda: s.fake.user_copies_text(later))
    sent = _standalone(s)
    assert s.fake.text() == later
    if when == 'before_replacement':
        assert sent is False and s.fake.empties == 0 and not s.target.pasted
        assert _chip(s) == (paste.CLIPBOARD_FAILED_DELIVERY, 'error')
    elif when == 'while_preparing':
        # The old picture was replaced before the copy; the newer copy wins.
        assert sent is False and not s.target.pasted
        assert _chip(s) == (recovery.IMAGE_REMOVED_NOT_SENT, 'error')
    else:
        assert sent is True and len(s.target.pasted) == 1
        assert _chip(s) == (recovery.IMAGE_REMOVED_UNCONFIRMED, 'warning')
    s.typed.assert_not_called()


@pytest.mark.parametrize('failure', ['target', 'privacy', 'busy'])
def test_safety_failures_cancel_before_replacement(dictation, monkeypatch, failure):
    s = dictation
    original = {cb.CF_UNICODETEXT: _utf16('old'), cb.CF_DIB: b'huge'}
    s.fake.formats = dict(original)
    s.fake.size_override = {cb.CF_DIB: cb._MAX_FORMAT_BYTES + 1}
    kwargs = {}
    if failure == 'target':
        kwargs['before_paste'] = lambda: False
    elif failure == 'privacy':
        from samsara import recent_privacy
        monkeypatch.setattr(recent_privacy, 'before_delivery', lambda *_: False)
    else:
        real = cb._open_clipboard_with_retry
        calls = []

        def busy(*a, **kw):
            calls.append(1)
            return False if len(calls) > 1 else real(*a, **kw)  # snapshot opens, write cannot
        monkeypatch.setattr(cb, '_open_clipboard_with_retry', busy)
    assert _standalone(s, **kwargs) is False
    assert s.fake.formats == original and s.fake.empties == 0 and not s.target.pasted
    s.typed.assert_not_called()
    assert _chip(s)[1] == 'error'


def test_failed_privacy_marker_never_exposes_text_and_restores_captured(dictation):
    s = dictation
    s.fake.formats = {cb.CF_UNICODETEXT: _utf16('old'), cb.CF_DIB: b'huge'}
    s.fake.size_override = {cb.CF_DIB: cb._MAX_FORMAT_BYTES + 1}
    marker = s.fake.RegisterClipboardFormatW(cb.PRIVATE_CLIPBOARD_FORMATS[1])
    s.fake.set_fail_fmts.add(marker)
    text = 'Synthetic private words.'
    assert _standalone(s, text) is False
    assert not s.target.pasted and text not in s.fake.text()
    assert s.fake.formats == {cb.CF_UNICODETEXT: _utf16('old')}
    assert _chip(s) == (recovery.IMAGE_REMOVED_NOT_SENT, 'error')
    assert not s.fake.allocated


def test_unknown_sequence_never_authorizes_replacement(dictation, monkeypatch):
    s = dictation
    snap = cb.ClipboardSnapshot()
    snap.lost[cb.CF_BITMAP] = 'synthetic'
    snap.source_seq = 0
    monkeypatch.setattr(cb, 'save_clipboard', lambda: snap)
    s.typed.return_value = False
    assert _standalone(s) is False
    assert s.fake.empties == 0 and not s.target.pasted


# --------------------------------------------------- caller policy matrix

def test_explicit_type_it_keeps_the_clipboard(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch, None)
    token = recovery._attempt.set(recovery.Attempt(mode='type'))
    try:
        assert _standalone(s, 'Typed on request.') is True
    finally:
        recovery._attempt.reset(token)
    s.typed.assert_called_once_with('Typed on request.')
    assert s.fake.empties == 0 and s.fake.formats == {cb.CF_BITMAP: b'synthetic HBITMAP'}


@pytest.mark.parametrize('caller', ['clipboard_only', 'silent', 'generic'])
def test_generic_callers_keep_the_preservation_contract(dictation, caller):
    s = dictation
    original = {cb.CF_UNICODETEXT: _utf16('old'), cb.CF_HDROP: b'files'}
    s.fake.formats = dict(original)
    s.fake.size_override = {cb.CF_HDROP: cb._MAX_FORMAT_BYTES + 1}
    s.typed.return_value = False
    if caller == 'generic':
        assert cb.paste_with_preservation('synthetic', paste_delay=0, restore_delay=0) is False
    else:
        kwargs = {'clipboard_only': True} if caller == 'clipboard_only' else {'feedback': 'silent'}
        assert _standalone(s, **kwargs) is False
    assert s.fake.formats == original and s.fake.empties == 0 and not s.target.pasted


def test_hands_free_commit_pastes_once_warns_and_leaves_no_choice(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch, None)
    text = s.manager.dictate_pending_buffer
    s.manager._clipboard_choice = ('stale',) * 6  # an older offer must not survive
    result = s.manager.commit_pending_dictation()
    assert result.kind == 'dictate_committed'
    assert s.target.pasted == [text] and not s.manager.dictate_pending_buffer
    s.typed.assert_not_called()
    assert s.manager._clipboard_choice is None
    assert sm.outcome_chip(result.kind, result.detail) == (recovery.IMAGE_REMOVED_UNCONFIRMED, 'warning')
    _transport_gone(s.fake, text)
    assert recovery.answer(s.manager, 'paste anyway') is None


def test_hands_free_failure_after_replacement_keeps_draft_and_names_the_loss(dictation, monkeypatch):
    s = dictation
    s.fake.formats = {cb.CF_BITMAP: b'synthetic HBITMAP'}
    _windows_conversion(s.fake, monkeypatch, None)
    text = s.manager.dictate_pending_buffer

    def readiness(*_):
        s.foreground[1] = 99   # target moves after the clipboard was replaced
        return 'ready'
    monkeypatch.setattr(cb, 'wait_for_clipboard_ready', readiness)
    result = s.manager.commit_pending_dictation()
    assert result.kind == 'dictate_commit_blocked_focus_lock'
    assert s.manager.dictate_pending_buffer == text and not s.target.pasted
    assert result.detail['clipboard_image_removed'] is True
    assert s.manager._clipboard_choice is None
    _transport_gone(s.fake, text)


def test_hands_free_complete_snapshot_has_no_loss_flags(dictation):
    s = dictation
    s.fake.formats = {cb.CF_UNICODETEXT: _utf16('ordinary')}
    result = s.manager.commit_pending_dictation()
    assert result.kind == 'dictate_committed'
    assert 'clipboard_image_removed' not in result.detail
    assert 'clipboard_restore_failed' not in result.detail


# ------------------------------------------------------------- displays

@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('kind', ['image', 'restore'])
def test_warning_reaches_both_displays_without_a_green_chip(qapp, monkeypatch, display, kind):
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
    shown = []
    app._show_outcome_chip = lambda label, sev, *ttl: (shown.append((label, sev)),
                                                       schedule_feedback(app, label, sev, None))
    receipt = recovery.Attempt(replaced=kind == 'image', restore_failed=kind == 'restore', backup_failed=True)
    expected = (recovery.IMAGE_REMOVED_UNCONFIRMED if kind == 'image'
                else recovery.RESTORE_UNCONFIRMED_MESSAGE)
    token = recovery._attempt.set(receipt)
    try:
        paste.show_delivery_result(app, True, cb.DeliveryOutcome.UNVERIFIED)
        assert shown == [(expected, 'warning')]
        app.play_sound.assert_not_called()
        if controller:
            while pending:
                pending.pop(0)()
            controller.drain()
            assert controller.view().notice.message == expected
        else:
            assert indicator._active_chip() == (expected, 'warning')
        import os
        from pathlib import Path
        if output := os.environ.get('CLIPBOARD_1167S_ARTIFACTS'):
            widget = controller.widget if controller else indicator
            widget.show()
            qapp.processEvents()
            assert widget.grab().save(str(Path(output) / f'{display.lower()}-{kind}-after.png'))
    finally:
        recovery._attempt.reset(token)
        (controller or indicator).close()
        qapp.processEvents()
