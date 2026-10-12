"""Settings consent uses the shared notice; only Apply persists acceptance."""
import copy
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QDialog

from samsara.ui import network_voice_notice


@pytest.fixture
def make_window(qapp, monkeypatch):
    import sounddevice
    from samsara import autostart, listening_display
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    monkeypatch.setattr(sounddevice, 'query_devices', lambda *a, **k: [])
    monkeypatch.setattr(sounddevice, 'query_hostapis', lambda *a, **k: [])
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a: False)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *a: None)
    monkeypatch.setattr(_SettingsWindow, '_replace_tts_runtime', Mock())
    windows = []

    def make(engine='winrt', accepted=False, level='brief'):
        config = copy.deepcopy(_schema_defaults())
        config['tts'].update(engine=engine, network_voice_accepted=accepted,
                             feedback_level=level, enabled=level != 'silent')
        app = _StubApp(config)
        # Network listing is a built-in catalog even before consent (543).
        # Model it so this consent test can preview its saved, selected voice.
        from samsara.tts.edge_tts_engine import _BUILTIN_VOICES
        voices = list(_BUILTIN_VOICES) if engine == 'edge' else []
        app.tts_engine = SimpleNamespace(list_voices=lambda: voices)
        app.audio_coordinator = SimpleNamespace(engine=object(), speak=Mock(), _dispatch_lock=RLock())
        app.save_config = Mock()
        app._session_mode_manager = SimpleNamespace(set_spoken_notices=Mock())
        window = _SettingsWindow(app)
        windows.append(window)
        window.resize(1160, 1000)
        window.show()
        window.show_tab('Audio')
        for _ in range(12): qapp.processEvents()
        return window

    yield make
    for window in windows:
        window.hide()
        window.deleteLater()
    qapp.processEvents()


def respond(monkeypatch, accept):
    calls = []
    def execute(dialog):
        calls.append(dialog)
        return QDialog.DialogCode.Accepted if accept else QDialog.DialogCode.Rejected
    monkeypatch.setattr(network_voice_notice.NetworkVoiceNotice, 'exec', execute)
    return calls


@pytest.mark.parametrize('accept', (False, True))
def test_selection_consent_and_cancel(make_window, monkeypatch, accept):
    window = make_window()
    calls = respond(monkeypatch, accept)
    window._widgets['tts_engine'].setCurrentText('edge')
    assert len(calls) == 1
    assert window._widgets['tts_engine'].currentText() == ('edge' if accept else 'winrt')
    assert window.app.config['tts']['network_voice_accepted'] is False
    window.close()
    assert window.app.config['tts']['engine'] == 'winrt'
    window.app.save_config.assert_not_called()
    window.show_tab('Audio')
    assert window._widgets['tts_engine'].currentText() == 'winrt'
    assert window._widgets['tts_engine'].parentWidget().buttons[0].isChecked()
    assert window._network_voice_accepted is False
    window._widgets['tts_engine'].setCurrentText('edge')
    assert len(calls) == 2


def test_apply_writes_engine_and_consent_together(make_window, monkeypatch):
    window = make_window()
    respond(monkeypatch, True)
    seen = []
    window.app.save_config.side_effect = lambda: seen.append(copy.deepcopy(window.app.config['tts']))
    window._widgets['tts_engine'].setCurrentText('edge')
    assert window.app.config['tts']['network_voice_accepted'] is False
    window._apply_and_close()
    assert len(seen) == 1
    assert seen[0]['engine'] == 'edge'
    assert seen[0]['network_voice_accepted'] is True


def test_accepted_user_quiet_line_and_read_only(make_window, monkeypatch, qapp):
    window = make_window('edge', True)
    calls = respond(monkeypatch, False)
    window._widgets['tts_engine'].setCurrentText('winrt')
    window._widgets['tts_engine'].setCurrentText('edge')
    assert calls == []
    quiet = window._widgets['network_voice_quiet']
    assert not quiet.isHidden()
    assert network_voice_notice.notice_facts()[0] in [w.text() for w in quiet.findChildren(QLabel)]
    window._pages['Audio'].advanced.reveal()
    link = quiet.findChildren(QLabel)[1]
    link.setFocus()
    qapp.processEvents()
    QTest.keyClick(link, Qt.Key.Key_Return)
    assert len(calls) == 1
    assert calls[0].findChildren(QPushButton)[1].isHidden()
    assert window.app.config['tts']['network_voice_accepted'] is True


@pytest.mark.parametrize('local', (False, True))
def test_existing_user_inline_choice_is_pending(make_window, local):
    window = make_window('edge')
    panel = window._widgets['network_voice_existing']
    assert panel.isVisible()
    assert panel.parentWidget() == window._pages['Audio'].widget()
    assert window._widgets['tts_engine'].currentText() == 'edge'
    texts = [w.text() for w in panel.findChildren(QLabel)]
    assert network_voice_notice.NOTICE_HEADING in texts
    assert panel.property('class') == 'card'
    assert all(fact in texts for fact in network_voice_notice.notice_facts())
    panel.findChildren(QPushButton)[0 if local else 1].click()
    assert panel.isHidden()
    assert window.app.config['tts']['engine'] == 'edge'
    assert window.app.config['tts']['network_voice_accepted'] is False
    window._apply_and_close()
    assert window.app.config['tts']['engine'] == ('winrt' if local else 'edge')
    assert window.app.config['tts']['network_voice_accepted'] is (not local)


def test_silent_does_not_bypass_consent(make_window, monkeypatch):
    window = make_window(level='silent')
    calls = respond(monkeypatch, False)
    assert window._rows_by_widget['tts_engine'].property('conditionVisible') is False
    window._widgets['tts_engine'].setCurrentText('edge')
    assert len(calls) == 1
    assert window._widgets['tts_engine'].currentText() == 'winrt'


@pytest.mark.parametrize('accept', (False, True))
@pytest.mark.parametrize('pending_local', (False, True))
def test_test_voice_requires_consent_for_live_engine(make_window, monkeypatch, accept, pending_local):
    window = make_window('edge')
    if pending_local: window._widgets['tts_engine'].setCurrentText('winrt')
    calls = respond(monkeypatch, accept)
    window._test_tts()
    if pending_local:
        # 543: a pending engine change must not sample the other live engine.
        assert calls == []
        window.app.audio_coordinator.speak.assert_not_called()
        assert 'Apply' in window._widgets['tts_test_status'].text()
    else:
        assert len(calls) == 1
        assert window.app.audio_coordinator.speak.call_count == int(accept)
    assert window.app.config['tts']['network_voice_accepted'] is False


def test_notice_facts_keyboard_and_safe_default(make_window, qapp):
    window = make_window()
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Escape):
        dialog = window._voice_notice_dialog()
        dialog.show()
        qapp.processEvents()
        buttons = dialog.findChildren(QPushButton)
        assert buttons[0].isDefault()
        assert not buttons[1].autoDefault()
        texts = [label.text() for label in dialog.findChildren(QLabel)]
        assert network_voice_notice.NOTICE_HEADING in texts
        assert all(fact in texts for fact in network_voice_notice.notice_facts())
        buttons[0].setFocus()
        QTest.keyClick(buttons[0], Qt.Key.Key_Tab)
        assert buttons[1].hasFocus()
        QTest.keyClick(buttons[1], Qt.Key.Key_Backtab)
        assert buttons[0].hasFocus()
        QTimer.singleShot(0, lambda d=dialog, k=key: QTest.keyClick(d, k))
        assert dialog.exec() == QDialog.DialogCode.Rejected
        dialog.deleteLater()


def test_notice_layout_is_compact(make_window, qapp):
    from samsara.ui import theme
    window = make_window('edge')
    dialog = window._voice_notice_dialog()
    dialog.show()
    for _ in range(12): qapp.processEvents()
    labels = dialog.findChildren(QLabel)
    facts = [label for label in labels if label.text() in network_voice_notice.notice_facts()]
    for label in labels:
        expected = label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height()
        assert label.height() == expected
    for previous, following in zip(facts, facts[1:]):
        assert following.y() - (previous.y() + previous.height()) == theme.SPACE_2
    buttons = dialog.findChildren(QPushButton)
    assert dialog.height() - (buttons[0].y() + buttons[0].height()) <= 28
    panel = window._widgets['network_voice_existing']
    actions = panel.findChildren(QPushButton)
    assert actions[0].x() > panel.width() // 3
    assert panel.width() - (actions[-1].x() + actions[-1].width()) <= theme.SPACE_4 + 2
    dialog.hide()
    dialog.deleteLater()


def test_render_proofs(make_window, qapp, tmp_path):
    window = make_window()
    page = window._pages['Audio']
    page.advanced.reveal()
    for _ in range(12): qapp.processEvents()
    row = window._rows_by_widget['tts_engine']
    page.verticalScrollBar().setValue(row.mapTo(page.widget(), QPoint()).y() - 150)
    for _ in range(12): qapp.processEvents()
    assert window.grab().save(str(tmp_path / 'audio_local.png'))
    dialog = window._voice_notice_dialog()
    dialog.show()
    for _ in range(12): qapp.processEvents()
    assert dialog.grab().save(str(tmp_path / 'notice.png'))
    dialog.hide()
    dialog.deleteLater()
    window._network_voice_accepted = True
    window._widgets['tts_engine'].setCurrentText('edge')
    for _ in range(12): qapp.processEvents()
    page.verticalScrollBar().setValue(row.mapTo(page.widget(), QPoint()).y() - 150)
    for _ in range(12): qapp.processEvents()
    assert window.grab().save(str(tmp_path / 'audio_accepted.png'))
    existing = make_window('edge')
    for _ in range(12): qapp.processEvents()
    assert existing.grab().save(str(tmp_path / 'audio_existing.png'))
