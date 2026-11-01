"""1001: remaining user-visible seams, using synthetic documents/profiles only."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.test_scratch_after_unacknowledged_paste_783 import rig, deliver
from tests.test_scratch_that_unit_606 import GOOD
from tests.test_settings_merge_403 import settings_window
from tests.test_ava_offline_57 import (
    _app, _session_done_harness, _spoken, inline_worker, _fresh_readiness,
    _shutdown_test_coordinators,
)


def test_post_paste_scratch_after_command_key_input(rig):
    editor, app, manager = rig
    editor._replace('Existing synthetic document. ')
    before = editor.document
    assert deliver(rig)
    editor.tick += 1  # Hold-to-talk/shortcut input, with no editor change.
    assert manager.dispatch_utterance('scratch that', GOOD).kind == 'scratch_success'
    assert editor.document == before
    assert editor.messages == [0xB1, 0xC2]
    manager._remove_chars_fn.assert_not_called()


@pytest.mark.parametrize('whole', ['five minute reminder', 'twenty-one minute reminder to stretch',
                                  '105 minute reminder', 'remind me in five minutes'])
def test_spoken_reminder_admission(whole):
    from samsara.commands import CommandExecutor
    from samsara.notifications import NotificationManager
    executor = object.__new__(CommandExecutor)
    assert NotificationManager.parse_remind_command(None, whole)
    assert executor._is_whole_reminder(whole)


@pytest.mark.parametrize('whole', ['tell Sarah five minute reminder', 'someone minute reminder',
                                  'five things minute reminder', 'I need a five minute reminder'])
def test_spoken_reminder_prose_stays_literal(whole):
    from samsara.commands import CommandExecutor
    assert not object.__new__(CommandExecutor)._is_whole_reminder(whole)


@pytest.mark.parametrize('kind,sentence,short', [
    ('empty_reply', 'Ava received an empty answer. Please try again.', 'empty answer'),
    ('answer_cut_off', 'Ava could not finish the answer. Please try again.', 'answer cut off'),
])
def test_ava_failure_reaches_real_speech_and_display(monkeypatch, inline_worker, kind, sentence, short):
    from plugins.commands import ask_ollama
    from samsara import cloud_llm, ava_readiness
    app = _session_done_harness(_app())
    monkeypatch.setattr(cloud_llm, 'send', lambda *a, **kw:
                        cloud_llm.CloudError(cloud_llm.make_failure(kind, 'deepseek')))
    ask_ollama.handle_ask_ava(app, 'Synthetic question', on_done=app._on_ava_session_request_done,
                            generation=None)
    assert _spoken(app) == [sentence]
    label, severity = app._show_outcome_chip.call_args.args
    assert label.endswith(short) and severity == 'warning'
    assert ava_readiness.readiness_for(app).failure_kind == kind


@pytest.mark.parametrize('outside,dialog,expected', [(True, False, True), (False, True, True)])
def test_apply_command_matching_only_after_reconciliation(settings_window, outside, dialog, expected):
    window = settings_window
    # Opening value is False. Outside and dialog can independently change it.
    window._config_at_open.setdefault('command_mode', {})['command_matching_enabled'] = False
    window.app.config.setdefault('command_mode', {})['command_matching_enabled'] = outside
    window.app.command_matching_enabled = outside
    window._widgets['cmd_matching_enabled'].setChecked(dialog)
    for collect in window._save_fns:
        collect({})
        assert window.app.command_matching_enabled is outside, 'collection must have no runtime effect'
    window._apply_and_close()
    assert window.app.config['command_mode']['command_matching_enabled'] is expected
    assert window.app.command_matching_enabled is expected


def test_feedback_enabled_pair_remains_independent():
    from samsara.settings_merge import merge_outside_changes
    opened = {'tts': {'feedback_level': 'brief', 'enabled': True}}
    live = {'tts': {'feedback_level': 'silent', 'enabled': False}}
    dialog = copy.deepcopy(opened)
    dialog['tts']['feedback_level'] = 'full'
    assert merge_outside_changes(opened, live, dialog)[0] == {
        'tts': {'feedback_level': 'full', 'enabled': False}}


def test_keyboard_review_keeps_passive_show_then_reaches_real_actions(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLineEdit, QPushButton, QCheckBox
    from samsara.ui.hint_toast import HintToast
    from samsara.ui.passive_review import ReviewSession
    editor = QLineEdit()
    editor.show()
    editor.activateWindow()
    editor.setFocus()
    qapp.processEvents()
    called = Mock()
    toast = HintToast('Synthetic hint', on_disable=called)
    toast.show()
    qapp.processEvents()
    assert qapp.focusWidget() is editor
    flags = toast.windowFlags()
    session = ReviewSession(toast)
    session.start()
    qapp.processEvents()
    assert not toast.windowFlags() & Qt.WindowDoesNotAcceptFocus
    checkbox = toast.findChild(QCheckBox)
    checkbox.setFocus()
    QTest.keyClick(checkbox, Qt.Key_Space)
    called.assert_called_once()
    session.finish()
    assert toast.windowFlags() == flags
    for button in toast.findChildren(QPushButton):
        assert button.focusPolicy() == Qt.NoFocus
    toast.close()
    editor.close()


def test_keyboard_review_draft_links_and_escape(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
    from samsara.ui.passive_review import ReviewSession
    window = QWidget(None, Qt.ToolTip | Qt.WindowDoesNotAcceptFocus)
    label = QLabel('<a href="word:0">Synthetic</a>')
    label.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
    QVBoxLayout(window).addWidget(label)
    clicked = Mock()
    label.linkActivated.connect(clicked)
    window.show()
    session = ReviewSession(window)
    session.start()
    qapp.processEvents()
    assert label.textInteractionFlags() & Qt.LinksAccessibleByKeyboard
    try:
        label.setFocus(Qt.TabFocusReason)
        QTest.keyClick(label, Qt.Key_Tab)  # Enter the first anchor inside the label.
        QTest.keyClick(label, Qt.Key_Return)
        clicked.assert_called_once_with('word:0')
        QTest.keyClick(label, Qt.Key_Escape)
        assert window.windowFlags() & Qt.WindowDoesNotAcceptFocus
        assert label.textInteractionFlags() == Qt.LinksAccessibleByMouse
    finally:
        session.finish()
        window.close()


@pytest.mark.parametrize('kind', ['status', 'reminder', 'compact', 'full'])
def test_review_reaches_existing_surface_controls_and_restores_flags(qapp, monkeypatch, kind):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from samsara.ui.passive_review import ReviewSession
    called = Mock()
    if kind == 'status':
        from samsara.ui.status_overlay import _StatusWindow
        window = _StatusWindow(None, None)
    elif kind == 'reminder':
        from samsara.ui.reminder_toast import _ToastWindow
        window = _ToastWindow()
        window.add_row('Synthetic reminder', 'Synthetic message', on_complete=called)
    elif kind == 'compact':
        from samsara.streaming import _StreamingWidget
        wrapper = _StreamingWidget(False)
        window = wrapper._w
        window._on_clear = called
        window._controls.show()
        window._clear_btn.show()
    else:
        from samsara.ui.live_surface_qt import LiveSurfaceWidget
        from tests.test_live_surface_qt import _view
        from samsara.live_surface.model import VisibleForm
        window = LiveSurfaceWidget(_view(VisibleForm.DRAFT_BADGE, text='Synthetic draft'))
    window.show()
    qapp.processEvents()
    flags = window.windowFlags()
    session = ReviewSession(window)
    try:
        session.start()
        qapp.processEvents()
        buttons = [b for b in window.findChildren(QPushButton) if b.isVisible() and b.isEnabled()]
        assert buttons
        assert all(b.focusPolicy() == Qt.StrongFocus for b in buttons)
        if kind in ('reminder', 'compact'):
            button = next(b for b in buttons if ('Complete' in b.text() if kind == 'reminder'
                                                 else b is window._clear_btn))
            button.setFocus()
            QTest.keyClick(button, Qt.Key_Space)
            called.assert_called_once()
        session.finish()
        assert window.windowFlags() == flags
    finally:
        if session.active:
            session.finish()
        window.close()


def test_compact_correction_choices_are_keyboard_reachable(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from samsara.streaming import _StreamingWidget
    from samsara.ui.passive_review import ReviewSession
    wrapper = _StreamingWidget(False)
    window = wrapper._w
    chosen = Mock()
    window._on_choice = chosen
    window._choices.open('Synthetic', ['Sample'])
    window.show()
    session = ReviewSession(window)
    try:
        session.start()
        qapp.processEvents()
        button = window._choices.buttons[0]
        button.setFocus()
        QTest.keyClick(button, Qt.Key_Space)
        chosen.assert_called_once_with('Sample')
        assert window._choices.dismiss.focusPolicy() == Qt.StrongFocus
    finally:
        session.finish()
        window.close()


def test_reminder_completion_restores_flags_without_explicit_finish(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    from samsara.ui.passive_review import ReviewSession
    from samsara.ui.reminder_toast import _ToastWindow
    completed, returned = Mock(), Mock()
    window = _ToastWindow()
    window.add_row('Synthetic reminder', 'Synthetic message', on_complete=completed)
    flags = window.windowFlags()
    session = ReviewSession(window, returned)
    try:
        session.start()
        qapp.processEvents()
        button = next(b for b in window.findChildren(QPushButton) if b.text() == 'Complete')
        button.setFocus()
        QTest.keyClick(button, Qt.Key_Space)
        qapp.processEvents()
        completed.assert_called_once()
        returned.assert_called_once()
        assert not session.active
        assert not window.isVisible()
        assert window.windowFlags() == flags
    finally:
        if session.active:
            session.finish()
        window.close()


def test_tray_entry_calls_keyboard_review(monkeypatch):
    from samsara.ui import passive_review
    from samsara.ui.tray_qt import SamsaraTrayQt
    opened = Mock()
    monkeypatch.setattr(passive_review, 'open_review', opened)
    actions = {}
    def add(menu, label, callback):
        actions[label] = callback
        return label
    owner = SimpleNamespace(_app=SimpleNamespace(), _add=add,
                            _open_support_tab=Mock(), _restart_samsara=Mock())
    SamsaraTrayQt._core_actions(owner, SimpleNamespace(setDefaultAction=Mock()))
    actions['Review on-screen messages']()
    opened.assert_called_once_with(owner._app)
