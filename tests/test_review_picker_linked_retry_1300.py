"""Keyboard review of real Full and Compact retry notices, without app bootstrap."""
from time import monotonic
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLineEdit, QWidget

from samsara import dictation_retry as retry
from samsara.decode_recovery import RetainedAudio
from samsara.history import HistoryManager
from samsara.live_surface.feedback import Feedback, FeedbackKind
from samsara.ui.passive_review import open_review
from tests.test_message_lifetime_1038 import display


@pytest.mark.parametrize('editor_open', [True, False], ids=['editor-open', 'editor-closed'])
def test_linked_retry_from_review_targets_the_original_editor(display, qapp, tmp_path, monkeypatch, editor_open):
    app = display.app
    editor, other_editor = QLineEdit(), QLineEdit()
    other_editor.show()
    editor.show()
    editor.activateWindow()
    editor.setFocus()
    qapp.processEvents()

    slot = RetainedAudio(audio=(), params={}, reason='synthetic',
                         expires_at=monotonic() + retry.NOTE_MS / 1000)
    app._hold_retry_audio = slot
    app._filter_dictation_language = lambda *_: True
    words = 'Synthetic recovered words.'
    decoded = SimpleNamespace(segments=[SimpleNamespace(text=words)], info=None, missed=[])
    decode = Mock(return_value=decoded)
    monkeypatch.setattr(retry, 'decode_slot', decode)
    monkeypatch.setattr('samsara.runtime.thread_registry.spawn', lambda _name, run, **_: run())

    def paste_into_focused_editor(text, *, return_delivery_confirmation):
        assert return_delivery_confirmation
        focused = qapp.focusWidget()
        if focused in (editor, other_editor):
            focused.insert(text)
            return True, True
        return False, False

    app._paste_preserving_clipboard = paste_into_focused_editor
    history = HistoryManager(tmp_path / 'synthetic.db')
    try:
        retry.show_note(app)
        display.flush()
        qapp.processEvents()
        band = display.band()
        window = band.window()
        assert band.report.isVisible()
        assert band.report.focusPolicy() == Qt.FocusPolicy.NoFocus
        assert window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus

        app.history_db = history
        retry.show_note(app)
        display.flush()
        qapp.processEvents()
        band = display.band()
        label = band.label if display.c else band.history_label
        assert slot.history_entry_id is not None
        assert 'href="history"' in label.text()
        assert not window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        # Offscreen Qt can activate Full when it recreates the linked window.
        editor.activateWindow()
        qapp.processEvents()
        assert qapp.focusWidget() is editor

        open_review(app)
        picker = app._passive_review_window
        qapp.processEvents()
        assert window in picker._windows
        assert qapp.focusWidget() is picker.surfaces
        for _ in range(picker._windows.index(window)):
            QTest.keyClick(picker.surfaces, Qt.Key.Key_Down)
        QTest.keyClick(picker.surfaces, Qt.Key.Key_Return)
        qapp.processEvents()
        for _ in window.findChildren(QWidget):
            if qapp.focusWidget() is band.report:
                break
            QTest.keyClick(qapp.focusWidget(), Qt.Key.Key_Tab)
        assert qapp.focusWidget() is band.report
        if not editor_open:
            editor.close()
            qapp.processEvents()
        QTest.keyClick(band.report, Qt.Key.Key_Space)
        qapp.processEvents()

        if editor_open:
            decode.assert_called_once()
            assert decode.call_args.args[1] is slot
            assert editor.text() == words
            assert not picker.isVisible()
        else:
            decode.assert_not_called()
            assert retry.live(app) is slot
            assert picker._session.active
        assert other_editor.text() == ''
    finally:
        picker = getattr(app, '_passive_review_window', None)
        if picker is not None:
            if picker._session is not None:
                picker._session.finish()
            picker.close()
        editor.close()
        other_editor.close()
        history.close()


def test_linked_result_without_retry_is_not_added_to_review(display, qapp):
    display.i.show_feedback('Synthetic result in History.', 'accent', 8000,
                            Feedback(FeedbackKind.INFORMATION, history=Mock()))
    display.flush()
    qapp.processEvents()
    window = display.band().window()
    assert window.isVisible()
    assert not window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus

    open_review(display.app)
    picker = display.app._passive_review_window
    try:
        assert window not in picker._windows
    finally:
        picker.close()
