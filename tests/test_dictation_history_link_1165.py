"""1165 FINISH IT: real History rows and native links on both displays."""
from types import SimpleNamespace as NS
from unittest.mock import Mock
import time

import pytest
from PySide6.QtCore import Qt, QEvent, QPoint, QPointF
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from samsara import dictation_history as links, dictation_retry as retry
from samsara.history import HistoryManager
from samsara.history_store import HistoryStore
from samsara.live_surface.feedback import schedule_feedback
from samsara.ui.history_view import HistoryView
from tests.test_message_lifetime_1038 import display
from tests.test_t84_unicode_delivery_1166s import keyboard, seam


@pytest.fixture
def history(display, tmp_path, monkeypatch):
    db = HistoryManager(tmp_path / 'synthetic.db')
    app = display.app
    app.history_db = db
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda name, fn, **kw: fn())
    view = HistoryView(HistoryStore(db))
    app.main_window = NS(_window=NS(_activate=Mock(return_value=view)))
    monkeypatch.setattr('samsara.ui.window_activation.surface_requested', lambda _: None)
    app._show_outcome_chip = lambda label, kind='accent', ttl=8000: schedule_feedback(app, label, kind, ttl)
    yield NS(app=app, db=db, view=view, d=display)
    view.close()
    view.deleteLater()
    db.close()


def label_for(h, qapp):
    h.d.flush()
    qapp.processEvents()
    band = h.d.band()
    return band.label if h.d.c else band.history_label


def activate(h, label, monkeypatch):
    # Native QLabel keyboard activation, with no external URL/browser opening.
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    label.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(label, Qt.Key.Key_Return)


def test_failed_retry_link_is_accent_focusable_and_opens_its_original_take(history, qapp, monkeypatch):
    h = history
    first = NS(expires_at=time.monotonic()+8, history_entry_id=None)
    h.app._hold_retry_audio = first
    retry.show_note(h.app, label=retry.RETRY_FAILED)
    label = label_for(h, qapp)
    assert 'Retry details kept in' in label.text()
    assert 'href="history"' in label.text() and 'text-decoration:none' in label.text()
    from samsara.ui import theme
    assert 'color:' + theme.ACCENT in label.text()
    assert label.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert label.textInteractionFlags() & Qt.TextInteractionFlag.LinksAccessibleByKeyboard
    assert not label.openExternalLinks()
    label.linkHovered.emit('history')
    assert label.cursor().shape() == Qt.CursorShape.PointingHandCursor
    label.linkHovered.emit('')
    assert label.cursor().shape() == Qt.CursorShape.ArrowCursor
    original = first.history_entry_id
    # A replacement take and a newer History entry must not redirect this link.
    h.app._hold_retry_audio = NS(expires_at=time.monotonic()+8, history_entry_id=None)
    h.db.add('Synthetic newer words.')
    h.view._search.setText('no matching entry')
    activate(h, label, monkeypatch)
    assert h.view._current_row_data()['id'] == original
    assert h.view._current_row_data()['row']['raw_text'] == ''
    assert h.view._search.text() == ''
    assert h.view._detail.isHidden() is False


@pytest.mark.parametrize('written_first', [False, True])
def test_delivery_history_before_or_after_feedback_binds_one_entry(history, qapp, monkeypatch, written_first):
    h = history
    text = 'Synthetic delivery words.'
    if written_first:
        original = h.db.add(text)
        links.remember_entry(h.app, original, text)
    links.prepare_delivery(h.app, text)
    h.app._show_outcome_chip('Sent, but not confirmed. Your words are in History.', 'warning')
    label = label_for(h, qapp)
    if not written_first:
        original = h.db.add(text)
        links.remember_entry(h.app, original, text)
    newer = h.db.add(text)
    links.remember_entry(h.app, newer, text)
    activate(h, label, monkeypatch)
    assert h.view._current_row_data()['id'] == original


def test_expired_retry_uses_original_details_without_claiming_recording_kept(history, qapp, monkeypatch):
    h = history
    original = NS(expires_at=time.monotonic()-1, history_entry_id=None)
    assert retry.request(h.app, expected=original) is False
    label = label_for(h, qapp)
    assert 'That retry has expired' in label.text()
    assert 'Recording kept' not in label.text()
    activate(h, label, monkeypatch)
    assert h.view._current_row_data()['id'] == original.history_entry_id


def test_deleted_link_does_not_select_a_different_entry(history, qapp, monkeypatch):
    h = history
    original = h.db.add('Synthetic removed entry.')
    links.remember_entry(h.app, original)
    h.app._show_outcome_chip('Details kept in History.', 'warning')
    label = label_for(h, qapp)
    h.db.delete(original)
    h.db.add('Synthetic unrelated entry.')
    activate(h, label, monkeypatch)
    assert h.view._current_row_data() is None


def test_mouse_click_on_history_opens_exact_entry(history, qapp, monkeypatch):
    h = history
    original = h.db.add('Synthetic mouse target.')
    links.remember_entry(h.app, original)
    h.app._show_outcome_chip('Details kept in History.', 'warning')
    label = label_for(h, qapp)
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    target = None
    # Use Qt's actual anchor hit test, including each display's alignment/wrap.
    for y in range(2, label.height(), 4):
        for x in range(2, label.width(), 4):
            QApplication.sendEvent(label, QMouseEvent(QEvent.Type.MouseMove,
                QPointF(x, y), Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier))
            if label.cursor().shape() == Qt.CursorShape.PointingHandCursor:
                target = QPoint(x, y)
                break
        if target is not None:
            break
    assert target is not None
    QTest.mouseClick(label, Qt.MouseButton.LeftButton, pos=target)
    assert h.view._current_row_data()['id'] == original


def test_queued_link_keeps_its_entry_when_later_history_arrives(history, qapp, monkeypatch):
    h = history
    h.app._show_outcome_chip('Synthetic first warning', 'warning')
    h.d.flush()
    original = h.db.add('Synthetic queued words.')
    links.remember_entry(h.app, original, 'Synthetic queued words.')
    links.prepare_delivery(h.app, 'Synthetic queued words.')
    h.app._show_outcome_chip('Your words are in History.', 'warning')
    h.d.flush()
    newer = h.db.add('Synthetic later words.')
    links.remember_entry(h.app, newer, 'Synthetic later words.')
    h.d.clock[0] = 20
    h.d.expire()
    label = label_for(h, qapp)
    activate(h, label, monkeypatch)
    assert h.view._current_row_data()['id'] == original


def test_unavailable_history_never_claims_details_were_kept(display, qapp):
    display.app.history_db = None
    display.app._hold_retry_audio = NS(expires_at=time.monotonic()+8)
    retry.show_note(display.app, label=retry.RETRY_FAILED)
    display.flush()
    assert 'History' not in display.text()
    assert display.text() == retry.RETRY_FAILED


def test_later_capture_with_same_words_cannot_fill_an_unsaved_link(history, qapp, monkeypatch):
    h = history
    links.prepare_delivery(h.app, 'Synthetic repeated sentence.')
    h.app._show_outcome_chip('Your words are in History.', 'warning')
    label = label_for(h, qapp)
    retry.begin_capture(h.app)
    newer = h.db.add('Synthetic repeated sentence.')
    links.remember_entry(h.app, newer, 'Synthetic repeated sentence.')
    activate(h, label, monkeypatch)
    assert h.view._current_row_data() is None


@pytest.mark.parametrize('partial', [False, True])
def test_paced_unverified_receipt_keeps_its_history_link(history, qapp, monkeypatch, seam, partial):
    """1165 n4: landed 1166s receipts keep an exact-entry link on both displays."""
    from samsara import clipboard, paste
    h = history
    h.app._foreground_wants_typed_injection = lambda: True
    h.app._flight_foreground_process_name = lambda: 'obsidian.exe'
    h.app._record_undoable_paste = Mock()
    h.app.adaptive_learner = NS(record_transcription=Mock())
    h.app.play_sound = Mock()
    h.app.listening_indicator.flash_success = Mock()
    if partial:
        seam.accept = lambda index, count: count if index < 3 else 0
    text = 'Synthetic linked receipt.'
    original = h.db.add(text)
    links.remember_entry(h.app, original, text)
    central = Mock(side_effect=AssertionError('no paste after full or partial typed dispatch'))
    result = paste.paste_preserving_clipboard(h.app, text, return_delivery_confirmation=True,
        paste_with_preservation_fn=central, type_text_unicode_fn=clipboard.type_text_unicode,
        get_foreground_hwnd_fn=lambda: 101)
    assert result == (True, False) and result.outcome == clipboard.DeliveryOutcome.UNVERIFIED
    assert (result.dispatch == 'partial') is partial
    paste.show_delivery_result(h.app, result[0], result.outcome)
    label = label_for(h, qapp)
    assert h.d.text() == paste.UNVERIFIED_DELIVERY and 'href="history"' in label.text()
    h.app.play_sound.assert_not_called()
    h.app.listening_indicator.flash_success.assert_not_called()
    central.assert_not_called()
    links.remember_entry(h.app, h.db.add(text), text)
    activate(h, label, monkeypatch)
    assert h.view._current_row_data()['id'] == original
