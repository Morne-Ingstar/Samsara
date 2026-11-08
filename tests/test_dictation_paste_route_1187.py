"""Paste-first dictation and visible, reasoned fallback; no desktop input."""
import logging
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara import clipboard as cb, clipboard_recovery as recovery, paste
from tests.test_clipboard_settle_474 import transport  # noqa: F401


BACKUP_NOTICE = "Typing instead of pasting because the clipboard couldn't be backed up."
READY_NOTICE = 'Typing instead of pasting because the clipboard is busy or not ready.'


@pytest.fixture
def app(monkeypatch, fake_delivery_target):
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda: NS(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'snapshot', lambda: None)
    monkeypatch.setattr(paste.delivery_verify, 'verified_snapshot', lambda *args: None)
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'synthetic.exe')
    monkeypatch.setattr(paste.flight_recorder, 'record', Mock())
    return NS(config={}, _foreground_wants_typed_injection=lambda: False,
              _flight_foreground_process_name=lambda: 'synthetic.exe',
              _record_undoable_paste=Mock(), adaptive_learner=NS(record_transcription=Mock()),
              _show_outcome_chip=Mock(), play_sound=Mock())


def deliver(app, text, **kwargs):
    return paste.paste_preserving_clipboard(app, text,
        paste_with_preservation_fn=cb.paste_with_preservation,
        get_foreground_hwnd_fn=lambda: 101, **kwargs)


@pytest.mark.parametrize('allowlisted', [True, False])
@pytest.mark.parametrize('length,threshold', [(300, 300), (301, 300), (5000, 300),
                                            (300, 1), (5000, 1000)])
def test_long_dictation_above_paste_threshold_pastes_once(
        app, transport, caplog, allowlisted, length, threshold):
    app.config['paste_min_chars'] = threshold
    app._foreground_wants_typed_injection = lambda: allowlisted
    typed = Mock(side_effect=AssertionError('normal dictation must paste'))
    text = 'x' * length
    with caplog.at_level(logging.INFO, logger='Samsara'):
        assert deliver(app, text, type_text_unicode_fn=typed)
    transport.copy.assert_called_once_with(text)
    transport.hotkey.assert_called_once_with('ctrl', 'v')
    transport.restore.assert_called_once_with(transport.saved)
    typed.assert_not_called()
    assert 'route=clipboard reason=normal_dictation' in caplog.text
    assert f'chars={length}' in caplog.text
    assert not any(call.args[0].startswith('Typing instead')
                   for call in app._show_outcome_chip.call_args_list)


@pytest.mark.parametrize('length,threshold', [(12, 300), (299, 10000), (5, 10)])
def test_short_allowlisted_delivery_keeps_1165_receipt_contract(app, transport, length, threshold):
    app.config['paste_min_chars'] = threshold
    app._foreground_wants_typed_injection = lambda: True
    text = 'x' * length
    typed = Mock(return_value=cb.TypedDispatch(length * 2, length * 2))
    result = deliver(app, text, type_text_unicode_fn=typed, return_delivery_confirmation=True)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    typed.assert_called_once_with(text)
    transport.copy.assert_not_called()
    transport.hotkey.assert_not_called()


@pytest.mark.parametrize('failure', ['backup', 'busy', 'missing_unicode', 'opener'])
@pytest.mark.parametrize('partial', [False, True])
def test_impossible_paste_announces_one_reason_then_types_without_retry(
        app, transport, caplog, failure, partial):
    text = 'Synthetic long dictation. ' * 200
    if failure == 'backup':
        transport.saved.lost[0] = 'clipboard backup timed out'
    elif failure == 'busy':
        transport.native.busy_until = 10
    elif failure == 'missing_unicode':
        transport.native.unicode = False
    else:
        transport.native.other_opener_until = 10
    expected = BACKUP_NOTICE if failure == 'backup' else READY_NOTICE
    reason = 'clipboard_backup_unavailable' if failure == 'backup' else 'clipboard_not_ready'

    def type_text(value):
        app._show_outcome_chip.assert_called_once_with(expected, 'warning')
        assert value == text
        return cb.TypedDispatch(4, len(text) * 2, 'cancelled') if partial else cb.TypedDispatch(
            len(text) * 2, len(text) * 2)

    typed = Mock(side_effect=type_text)
    with caplog.at_level(logging.INFO, logger='Samsara'):
        result = deliver(app, text, type_text_unicode_fn=typed, return_delivery_confirmation=True)
    assert result == (True, False) and result.outcome == cb.DeliveryOutcome.UNVERIFIED
    assert result.dispatch == ('partial' if partial else None)
    typed.assert_called_once_with(text)
    app._show_outcome_chip.assert_called_once_with(expected, 'warning')
    transport.hotkey.assert_not_called()
    if failure == 'backup':
        transport.copy.assert_not_called()
        transport.restore.assert_not_called()
    else:
        transport.restore.assert_called_once_with(transport.saved)
    assert f'route=typed_fallback reason={reason} chars={len(text)}' in caplog.text
    assert text not in caplog.text
    if partial:
        app._record_undoable_paste.assert_not_called()


@pytest.mark.parametrize('caller', ['clipboard_only', 'no_feedback', 'silent'])
def test_clipboard_safety_fallback_keeps_existing_caller_contract(app, transport, caller):
    transport.saved.lost[0] = 'clipboard backup timed out'
    kwargs = {'clipboard_only': True} if caller == 'clipboard_only' else (
        {'feedback': 'silent'} if caller == 'silent' else {})
    if caller == 'no_feedback':
        app._show_outcome_chip = None
    typed = Mock(return_value=True)
    result = deliver(app, 'Synthetic words. ' * 30, type_text_unicode_fn=typed,
                     return_delivery_confirmation=True, **kwargs)
    if caller == 'clipboard_only':
        assert result == (False, False)
        typed.assert_not_called()
    else:
        assert result == (True, False)
        typed.assert_called_once()
    transport.copy.assert_not_called()
    transport.hotkey.assert_not_called()


def test_changed_clipboard_cancels_without_typing(app, transport):
    transport.native.change_at = .02
    typed = Mock()
    assert not deliver(app, 'Synthetic words.', type_text_unicode_fn=typed)
    typed.assert_not_called()
    transport.hotkey.assert_not_called()
    assert not any(call.args[0].startswith('Typing instead')
                   for call in app._show_outcome_chip.call_args_list)


def test_changed_target_cancels_before_fallback_notice(app, transport):
    transport.saved.lost[0] = 'clipboard backup timed out'
    typed = Mock()
    assert not deliver(app, 'Synthetic words.', type_text_unicode_fn=typed,
                       before_paste=lambda: False)
    typed.assert_not_called()
    assert not any(call.args[0].startswith('Typing instead')
                   for call in app._show_outcome_chip.call_args_list)


def test_unacknowledged_paste_never_retries_with_typing(app, transport, monkeypatch):
    monkeypatch.setattr(cb, 'message_paste_to_focused_control', lambda: cb.DeliveryOutcome.UNVERIFIED)
    typed = Mock()
    result = deliver(app, 'Synthetic words.', type_text_unicode_fn=typed,
                     return_delivery_confirmation=True)
    assert result == (True, False)
    typed.assert_not_called()
    transport.hotkey.assert_called_once_with('ctrl', 'v')


def test_explicit_type_it_keeps_clipboard_and_records_reason(app, transport, caplog):
    typed = Mock(return_value=cb.TypedDispatch(6, 6))
    token = recovery._attempt.set(recovery.Attempt(mode='type'))
    try:
        with caplog.at_level(logging.INFO, logger='Samsara'):
            assert deliver(app, 'abc', type_text_unicode_fn=typed)
    finally:
        recovery._attempt.reset(token)
    typed.assert_called_once_with('abc')
    transport.copy.assert_not_called()
    transport.hotkey.assert_not_called()
    assert 'route=typed reason=explicit_type_request chars=3' in caplog.text


@pytest.mark.parametrize('display', ['Full', 'Compact'])
@pytest.mark.parametrize('failure', ['backup', 'not_ready'])
def test_fallback_notice_reaches_both_displays_before_typing(
        qapp, app, transport, monkeypatch, display, failure):
    import os
    from pathlib import Path
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtTest import QTest
    from samsara.live_surface.feedback import schedule_feedback
    from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
    from samsara.live_surface.model import LiveSurfaceModel
    from samsara.ui.listening_indicator import ListeningIndicator
    from samsara.ui import theme

    monkeypatch.setenv('QT_QPA_FONTDIR', 'C:/Windows/Fonts')
    app.config['ui'] = {'live_surface': {'enabled': display == 'Full'}}
    app._schedule_ui = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    controller = None
    pending = []
    if display == 'Full':
        monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', pending.append)
        controller = LiveSurfaceController(app, model=LiveSurfaceModel())
        app.live_surface = controller
        controller.start()
        indicator = LiveSurfaceIndicatorAdapter(controller)
    else:
        indicator = ListeningIndicator()
    app.listening_indicator = indicator
    shown = []
    widget = controller.widget if controller else indicator
    widget.show()
    qapp.processEvents()
    before = widget.grab().toImage()

    def show(label, kind, *ttl):
        shown.append((label, kind))
        schedule_feedback(app, label, kind, 4000)
    app._show_outcome_chip = show
    expected = BACKUP_NOTICE if failure == 'backup' else READY_NOTICE
    if failure == 'backup':
        transport.saved.lost[0] = 'clipboard backup timed out'
    else:
        transport.native.busy_until = 10

    def type_text(text):
        assert shown == [(expected, 'warning')]
        if controller:
            while pending:
                pending.pop(0)()
            controller.drain()
            assert controller.view().notice.message == expected
        else:
            assert indicator._active_chip() == (expected, 'warning')
        if output := os.environ.get('DELIVERY_1187_ARTIFACTS'):
            QTest.qWait(160)  # Capture the real Full display after its 120 ms resize.
            after = widget.grab().toImage()
            width, height = max(before.width(), after.width()), max(before.height(), after.height())
            comparison = QImage(width * 2 + 48, height + 100, QImage.Format.Format_ARGB32)
            comparison.fill(QColor(theme.BG0))
            painter = QPainter(comparison)
            painter.setPen(QColor(theme.TEXT_PRIMARY))
            painter.setFont(theme.qfont(theme.TYPE_MIN))
            painter.drawText(12, 24, f'{display}: BEFORE (master)')
            painter.drawText(width + 36, 24, f'{display}: AFTER (this change)')
            painter.drawImage(12, 52, before)
            painter.drawImage(width + 36, 52, after)
            painter.end()
            assert comparison.save(str(Path(output) / f'{display.lower()}_{failure}_BEFORE_left_AFTER_right.png'))
        return cb.TypedDispatch(len(text) * 2, len(text) * 2)

    try:
        assert deliver(app, 'Synthetic fallback. ' * 30, type_text_unicode_fn=type_text,
                       return_delivery_confirmation=True) == (True, False)
        assert shown == [(expected, 'warning')]
        app.play_sound.assert_not_called()
    finally:
        (controller or indicator).close()
        qapp.processEvents()
