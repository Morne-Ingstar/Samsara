"""Saved dictation, safe target/clipboard transport, and both real displays."""
from types import SimpleNamespace, MethodType
from unittest.mock import Mock

import pytest

from samsara import paste_again as again, paste, recent_privacy
from tests.test_clipboard_audit6 import fake, target


@pytest.fixture
def rig(monkeypatch):
    jobs, notes = [], []
    app = SimpleNamespace(config={}, _paste_again_text='Synthetic saved paragraph.',
                          _paste_preserving_clipboard=Mock(return_value=True),
                          _show_outcome_chip=lambda *args: notes.append(args))
    foreground = [202]
    monkeypatch.setattr(again, '_get_foreground_hwnd', lambda: foreground[0])
    monkeypatch.setattr(again, '_external_target', lambda hwnd: hwnd in (202, 303))
    spawn = again.thread_registry.spawn
    monkeypatch.setattr(again.thread_registry, 'spawn', lambda name, fn, **kw:
                        jobs.append(fn) if name == 'dictation.paste_again' else spawn(name, fn, **kw))
    return SimpleNamespace(app=app, jobs=jobs, notes=notes, foreground=foreground)


def test_current_window_and_focus_guard(rig):
    rig.app._last_dictation_hwnd = 101
    assert again.request(rig.app)
    rig.jobs.pop()()
    text, = rig.app._paste_preserving_clipboard.call_args.args
    assert text == rig.app._paste_again_text
    guard = rig.app._paste_preserving_clipboard.call_args.kwargs['before_paste']
    assert guard()
    rig.foreground[0] = 303
    assert not guard()
    assert not rig.app._paste_again_busy


def test_window_change_before_worker_does_not_paste(rig):
    assert again.request(rig.app)
    rig.foreground[0] = 303
    rig.jobs.pop()()
    rig.app._paste_preserving_clipboard.assert_not_called()
    assert 'window changed' in rig.notes[0][0]


@pytest.mark.parametrize('text', ['', '  ', None])
def test_nothing_is_a_plain_note(rig, text):
    rig.app._paste_again_text = text or ''
    assert not again.request(rig.app)
    assert rig.notes == [('Nothing to paste yet.', 'info')]
    assert not rig.jobs


def test_samsara_foreground_is_refused(rig):
    rig.foreground[0] = 999
    assert not again.request(rig.app)
    assert not rig.jobs


def test_duplicate_click_does_not_queue_second_paste(rig):
    assert again.request(rig.app)
    assert not again.request(rig.app)
    assert len(rig.jobs) == 1
    rig.jobs.pop()()
    assert again.request(rig.app)


def test_busy_cleared_after_delivery_exception(rig):
    rig.app._paste_preserving_clipboard.side_effect = ValueError('synthetic failure')
    assert again.request(rig.app)
    with pytest.raises(ValueError):
        rig.jobs.pop()()
    assert not rig.app._paste_again_busy


def test_fake_clipboard_is_restored_and_private(rig, fake, target, monkeypatch):
    from samsara import clipboard as cb
    fake.user_copies_text('Synthetic existing clipboard')
    previous = dict(fake.formats)
    published = []
    write = cb._user32.SetClipboardData
    def observe(fmt, handle):
        if fmt == cb.CF_UNICODETEXT:
            published.append(dict(fake.formats))
        return write(fmt, handle)
    monkeypatch.setattr(cb._user32, 'SetClipboardData', observe)
    monkeypatch.setattr(paste.injection_safety, 'window_integrity',
                        lambda: SimpleNamespace(blocked=False, confirmed_ok=True))
    monkeypatch.setattr(paste.delivery_verify, 'snapshot', lambda: None)
    monkeypatch.setattr(paste, '_editor_snapshot', lambda: None)
    monkeypatch.setattr(paste, 'flight_foreground_process_name', lambda: 'notepad.exe')
    monkeypatch.setattr(recent_privacy, 'before_delivery', lambda *_: True)
    app = rig.app
    app._foreground_wants_typed_injection = lambda: False
    app._flight_foreground_process_name = lambda: 'notepad.exe'
    app._record_undoable_paste = Mock()
    app.adaptive_learner = SimpleNamespace(record_transcription=Mock())
    app._paste_preserving_clipboard = lambda text, **kw: paste.paste_preserving_clipboard(
        app, text, get_foreground_hwnd_fn=lambda: rig.foreground[0], **kw)
    assert again.request(app)
    rig.jobs.pop()()
    assert target.pasted == [app._paste_again_text]
    assert fake.formats == previous
    markers = {'ExcludeClipboardContentFromMonitorProcessing',
               'CanIncludeInClipboardHistory', 'CanUploadToCloudClipboard'}
    assert markers <= fake.registered.keys()
    assert all(fake.registered[name] in published[0] for name in markers)


@pytest.mark.parametrize('status', ['success', 'failed', 'refused'])
def test_delivered_or_failed_history_can_be_repeated(status):
    app = SimpleNamespace()
    again.remember_history(app, dict(entry_type='dictation', status=status,
                                    display_text='Synthetic formatted text.'))
    assert again.last_text(app) == 'Synthetic formatted text.'
    again.remember_history(app, dict(entry_type='command', display_text='paste that again'))
    assert again.last_text(app) == 'Synthetic formatted text.'


def test_fallback_uses_latest_dictation_even_when_failed():
    query = Mock(return_value=[{'display_text': 'Synthetic failed words.', 'status': 'failed'}])
    app = SimpleNamespace(history_store=SimpleNamespace(query=query))
    assert again.last_text(app) == 'Synthetic failed words.'
    query.assert_called_once_with(type_filter='dictation', limit=1)


def test_privacy_redaction_does_not_leave_repeatable_words(monkeypatch):
    app = SimpleNamespace(history_db=SimpleNamespace(add=Mock(), conceal_delivery=Mock(return_value=[])))
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy('password_field'))
    recent_privacy.write_history(app, raw_text='Synthetic concealed', entry_type='dictation')
    assert again.last_text(app) == ''
    again.remember_history(app, dict(display_text='Synthetic delayed privacy', entry_type='dictation'))
    assert recent_privacy.before_delivery(app, 'Synthetic delayed privacy')
    assert again.last_text(app) == ''


def test_unavailable_ui_never_prevents_history_write(monkeypatch):
    app = SimpleNamespace(history_db=SimpleNamespace(add=Mock(return_value=7)),
                          _schedule_ui=Mock(side_effect=RuntimeError('synthetic missing UI')))
    monkeypatch.setattr(recent_privacy, 'focused_target', lambda: recent_privacy.TargetPrivacy(''))
    assert recent_privacy.write_history(app, display_text='Synthetic history', entry_type='dictation') == 7
    app.history_db.add.assert_called_once()


@pytest.fixture
def command_entry(monkeypatch):
    import importlib
    from samsara import plugin_commands
    from plugins.commands import core_utils
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    monkeypatch.setattr(plugin_commands, '_shared_matcher', None)
    importlib.reload(core_utils)
    return plugin_commands


@pytest.mark.parametrize('phrase', ['paste again', 'paste that again', 'Paste that again.'])
def test_whole_voice_command(command_entry, phrase):
    entry, remainder = command_entry.find_command(phrase)
    assert entry['phrase'] == 'paste that again' and remainder == ''


@pytest.mark.parametrize('phrase', ['please paste that again later', 'I said paste again yesterday',
                                  'paste that again and send it'])
def test_sentence_is_not_the_voice_command(command_entry, phrase):
    entry, _ = command_entry.find_command(phrase)
    assert entry is None or entry['phrase'] != 'paste that again'


@pytest.mark.parametrize('phrase', ['paste again', 'paste that again'])
def test_hands_free_preserves_pending_draft_even_when_exact_commands_off(monkeypatch, phrase):
    import dictation  # Maintained gate adapter only; never application bootstrap.
    from samsara.session_modes import SessionModeManager, SessionMode, UtteranceSignals, CommandDispatchResult
    from samsara import button_voice
    monkeypatch.setattr(button_voice, 'dispatch_visible_action', lambda _: False)
    executor = SimpleNamespace(find_command=lambda text: 'paste that again' if text in again.PHRASES else None)
    app = SimpleNamespace(config={'hands_free': {'exact_commands': False}}, command_executor=executor)
    app._probe_media_transport = lambda _: None
    app._probe_hands_free_command = MethodType(dictation.DictationApp._probe_hands_free_command, app)
    dispatch = Mock(return_value=CommandDispatchResult(matched=True, phrase='paste that again'))
    manager = SessionModeManager(abort_phrases=['cancel dictation'],
        foreground_exe_resolver=lambda: 'notepad.exe', foreground_hwnd_resolver=lambda: 202,
        inject_fn=Mock(), remove_chars_fn=Mock(), command_dispatch_fn=dispatch,
        agent_dispatch_fn=Mock(), buffer_dictate_until_commit=True,
        hands_free_command_probe_fn=app._probe_hands_free_command)
    manager.reset(SessionMode.DICTATE)
    signals = UtteranceSignals(True, (1.0,))
    manager.dispatch_utterance('A synthetic finished thought.', signals)
    previous = manager._draft.text
    outcome = manager.dispatch_utterance(phrase, signals)
    assert outcome.kind == 'hands_free_command_executed'
    assert manager._draft.text == previous and manager.mode is SessionMode.DICTATE
    dispatch.assert_called_once_with(phrase)


@pytest.mark.parametrize('display', ['compact', 'full'])
def test_button_on_both_real_displays_and_expiry(qapp, qt_widget_scope, rig, monkeypatch, display):
    import ctypes
    from ctypes import wintypes
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from samsara.ui.paste_again_qt import show_control
    from samsara.ui import paste_again_qt
    from samsara.ui.button_voice_qt import visible_actions
    if display == 'compact':
        from samsara.ui.listening_indicator import ListeningIndicator
        owner = ListeningIndicator()
    else:
        from samsara.ui.live_surface_qt import LiveSurfaceWidget
        from samsara.live_surface.model import LiveSurfaceModel
        owner = LiveSurfaceWidget(LiveSurfaceModel(), reduced_motion=True)
    with qt_widget_scope(owner):
        owner.show()
        rig.app.live_surface = SimpleNamespace(widget=owner) if display == 'full' else None
        rig.app.listening_indicator = owner
        rig.app._schedule_ui = lambda callback: callback()
        again.remember_history(rig.app, dict(entry_type='dictation', display_text='Synthetic saved paragraph.'))
        qapp.processEvents()
        control = owner._paste_again_control
        assert control.isVisible() and control.button.text() == 'Paste again'
        assert control.width() < 180
        if display == 'compact':
            # 1163 n2: the whole clickable action is inside the painted box.
            assert owner._pill_window.frameGeometry().contains(control.frameGeometry())
        assert control.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        assert control.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert control.focusPolicy() == control.button.focusPolicy() == Qt.FocusPolicy.NoFocus
        # 1163 FINISH IT: exercise the Windows click-activation decision without
        # manipulating the owner's real desktop, clipboard or foreground.
        message = wintypes.MSG()
        message.message = 0x0021  # WM_MOUSEACTIVATE
        assert control.nativeEvent(b'windows_generic_MSG', ctypes.addressof(message)) == (True, 3)
        assert any(action.label == 'Paste again' for action in visible_actions(qapp))
        QTest.mouseClick(control.button, Qt.MouseButton.LeftButton)
        assert len(rig.jobs) == 1
        rig.jobs.pop()()
        assert rig.foreground == [202]
        args = rig.app._paste_preserving_clipboard.call_args
        assert args.args == (rig.app._paste_again_text,) and args.kwargs['before_paste']()
        owner.hide()  # A short success receipt may hide before this control expires.
        control.sync()
        assert control.isVisible()
        monkeypatch.setattr(paste_again_qt, 'monotonic', lambda: control._deadline + 1)
        control.sync()
        assert not control.isVisible()
        if display == 'compact':
            assert not owner._pill_window.isVisible()


def test_history_row_action_uses_row_not_latest(qapp, qt_widget_scope, rig, monkeypatch):
    from PySide6.QtWidgets import QWidget
    from samsara.ui.history_view import _HistoryRow
    row = dict(entry_type='dictation', status='failed', display_text='Synthetic older words.')
    with qt_widget_scope(QWidget()) as host:
        # 1163 FINISH IT removes paste from History; its existing Copy stays.
        # Keep this test id so the gate can compare the intentional change.
        copy = Mock()
        widget = _HistoryRow(row, copy, host)
        host.show()
        widget.copy_button.click()
        copy.assert_called_once_with(widget)
        assert host.isVisible()
        assert [b.text() for b in widget.findChildren(__import__('PySide6.QtWidgets', fromlist=['QPushButton']).QPushButton)] == ['Copy']
        assert not rig.jobs


@pytest.mark.parametrize('row', [dict(entry_type='command', display_text='paste again'),
                                dict(entry_type='dictation', status='empty'),
                                dict(entry_type='dictation', status='transcribing', display_text='Synthetic pending'),
                                dict(entry_type='dictation', display_text='Synthetic private', privacy_reason='password_field')])
def test_history_does_not_offer_paste_for_commands_empty_or_private(qapp, qt_widget_scope, row):
    from samsara.ui.history_view import _HistoryRow
    from PySide6.QtWidgets import QPushButton
    # 1163 FINISH IT: every History row now has Copy only.
    with qt_widget_scope(_HistoryRow(row, Mock())) as widget:
        assert [b.text() for b in widget.findChildren(QPushButton)] == ['Copy']


@pytest.mark.parametrize('palette', ['dark', 'light'])
def test_button_matches_existing_display_actions_and_rethemes(qapp, qt_widget_scope, rig, palette):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget
    from samsara.ui import theme
    from samsara.ui.live_surface_qt import _FeedbackBand
    from samsara.ui.paste_again_qt import PasteAgainControl
    previous = theme.active_theme()
    try:
        with qt_widget_scope(QWidget()) as owner:
            control = PasteAgainControl(owner, rig.app)
            reference = _FeedbackBand(owner, 'exampleFeedback')
            control.present()
            theme.set_theme(palette)
            reference.restyle()
            assert control.button.styleSheet() == reference.report.styleSheet()
            assert control.button.font() == reference.report.font()
            assert control.button.height() == 28
            assert control.layout().contentsMargins().left() == 6
            assert control.layout().spacing() == 6
            assert control.button.width() >= control.button.fontMetrics().horizontalAdvance('Paste again') + 24
            assert control.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            # Both text/background and the opaque backing are theme tokens.
            assert theme.TEXT_PRIMARY in control.button.styleSheet()
            assert theme.BG2 in control.button.styleSheet()
    finally:
        theme.set_theme(previous)


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('corner', ['top-left', 'top-right', 'bottom-left', 'bottom-right'])
def test_compact_border_and_contained_action_follow_receipts_and_lifetime(
        qapp, qt_widget_scope, rig, monkeypatch, palette, corner):
    from samsara.ui import theme, paste_again_qt
    from samsara.ui.listening_indicator import ListeningIndicator
    from PySide6.QtGui import QImage, QPainter
    previous = theme.active_theme()
    try:
        theme.set_theme(palette)
        with qt_widget_scope(ListeningIndicator()) as owner:
            owner.set_position(corner)
            owner.show()
            width_without_action = owner._layout()[2].width()
            def assert_visible_border():
                # Composite the actual native indicator on a same-coloured
                # background: theme borders use translucent rgba tokens.
                pixmap = owner._pill_window.grab()
                image = QImage(pixmap.size(), QImage.Format.Format_ARGB32)
                background = theme.qcolor(theme.BG1)
                image.fill(background)
                painter = QPainter(image)
                painter.drawPixmap(0, 0, pixmap)
                painter.end()
                rim = image.pixelColor(image.width() // 2, 0)
                assert abs(rim.lightnessF() - background.lightnessF()) > .03
            assert_visible_border()
            owner.show_outcome('Synthetic failed delivery', 'error', 15000)
            paste_again_qt.show_control(owner, rig.app)
            control = owner._paste_again_control
            for label in ('Hold to talk', 'Hands-free · Game mode'):
                owner.set_mode(label)
                qapp.processEvents()
                pill = owner._pill_window.frameGeometry()
                assert pill.contains(control.frameGeometry())
                assert pill.center().y() == control.frameGeometry().center().y()
                assert control.height() == control.button.height() == 28
                assert owner.screen().availableGeometry().contains(pill)
                # The reserved label area ends before the button begins.
                assert pill.right() - owner._paste_again_width < control.x()
            owner.set_mode('Hold to talk')
            owner._chip_deadline = 0
            owner.clear_outcome()
            owner.hide()
            control.sync()
            assert owner._pill_window.isVisible()
            assert owner._pill_window.frameGeometry().contains(control.frameGeometry())
            paste_again_qt.show_control(owner, rig.app)
            assert owner._layout()[2].width() == width_without_action + control.width() + 18
            theme.set_theme('light' if palette == 'dark' else 'dark')
            control.sync()
            assert_visible_border()
            assert theme.BG2 in control.button.styleSheet()
            monkeypatch.setattr(paste_again_qt, 'monotonic', lambda: control._deadline + 1)
            control.sync()
            assert not control.isVisible() and not owner._pill_window.isVisible()
            assert owner._layout()[2].width() == width_without_action
    finally:
        theme.set_theme(previous)
