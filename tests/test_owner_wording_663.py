"""Delegated wording is exposed by the real controls and outcome branches."""
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QTextDocument
from PySide6.QtWidgets import QCheckBox, QComboBox, QLabel

from samsara.ui.settings import agents_section, help_page, tts_qt


@pytest.fixture
def controls(qapp, monkeypatch):
    state = SimpleNamespace(status='listening', port=47832, connection_details=lambda: None)
    monkeypatch.setattr(agents_section, 'for_app', lambda app: state)
    app = SimpleNamespace(config={
        'command_packs': {'agents': True},
        'agent_server': {'allow_typing': True},
        'ui': {'live_surface': {'enabled': True}},
    })
    widget = agents_section.AgentsControls(SimpleNamespace(app=app), QCheckBox())
    yield widget
    widget.hide()
    widget.deleteLater()


def plain_labels(widget):
    labels = []
    for label in widget.findChildren(QLabel):
        document = QTextDocument()
        document.setHtml(label.text())
        labels.append(document.toPlainText())
    return labels


def test_setup_instructions_keep_actions_and_setting_links(controls):
    labels = plain_labels(controls)
    # 786: ordered setup copy replaces repeated introductions and inline links.
    for text in (
        "Connect Claude Code, Claude Desktop or Hermes to use Samsara's spoken replies and optional typing.",
        'Keep Samsara open while using a connected app.',
        '3. After signing in, open a new PowerShell window.',
        'Select Copy beside the connection command below. Right-click in PowerShell '
        'to paste it, then press Enter. This connection works across your Claude Code projects.',
        'After choosing New token, run this removal command in PowerShell. '
        'Then copy and run the connection command again.',
        "If you connected Samsara for one project before, open PowerShell in that project's folder and run this command first.",
        'After choosing New token, quit and reopen Claude Desktop.',
        '3. Quit and reopen Claude Desktop to load the connection. Keep Samsara open.',
        'Let AI apps type for me. Samsara asks before typing; say yes after it finishes asking.',
    ):
        assert text in labels
    assert '--scope user' in agents_section.claude_code_command(
        'http://127.0.0.1:47832/mcp\nAuthorization: Bearer ' + 'x' * 48)
    links = [label.text() for label in controls.findChildren(QLabel) if '<a ' in label.text()]
    assert links == []
    assert controls.command_key_button.text() == 'Command key settings'
    assert controls.rotate.text() == 'New token'


def test_ready_means_server_ready_and_approval_waits_for_question(controls):
    assert controls.status.text() == 'Ready for an AI app to connect.'
    assert controls.approval_style.itemText(0) == 'Say yes after Samsara asks'
    assert controls.approval_style.itemData(0) is False
    assert controls.approval_style.itemText(1) == 'Hold the Command key and say yes'
    assert controls.approval_style.itemData(1) is True
    # 786: readable numbered instructions replace the tiny thumbnails.
    assert not controls.findChildren(agents_section.SetupIllustration)
    assert any(text.startswith('1. Open Start, type PowerShell') for text in plain_labels(controls))


@pytest.mark.parametrize('pending, expected', (
    ('engine', 'Apply the engine change first, then test the voice.'),
    ('voice', 'Apply the voice change first, then test it.'),
))
def test_pending_voice_guidance(qapp, pending, expected):
    engine, voice, status = QComboBox(), QComboBox(), QLabel()
    engine.addItem('edge' if pending == 'engine' else 'winrt')
    voice.addItem('Default voice', None)
    owner = SimpleNamespace(
        app=SimpleNamespace(config={'tts': {'engine': 'winrt', 'voice_id': 'saved'}}),
        _widgets={'tts_engine': engine, 'tts_voice_combo': voice, 'tts_test_status': status},
    )
    tts_qt.TTSPage._test_tts(owner)
    assert status.text() == expected


@pytest.mark.parametrize('result, network_failed, expected', (
    ('windows', False, "The Network voice didn't answer, so the Windows voice spoke instead."),
    ('pending', True, "The Network voice didn't answer. Trying the Windows voice..."),
    ('failed', False, "Couldn't play the voice sample."),
    ('cancelled', False, 'Voice sample cancelled.'),
))
def test_voice_outcome_wording(qapp, result, network_failed, expected):
    status = QLabel()
    owner = SimpleNamespace(_visit_callback=lambda callback, *widgets: callback)
    handle = SimpleNamespace(voice_result=result, network_failed=network_failed)
    tts_qt.TTSPage._watch_tts_sample(owner, handle, status)
    assert status.text() == expected
    owner._tts_sample_timer.stop()


def test_help_description_names_the_email_action():
    row = next(row for row in help_page.BASE if row[0] == 'Help and community')
    assert row[1] == 'Ask questions, report bugs, share ideas, or email me.'


def test_unavailable_previous_version_keeps_clear_existing_wording(monkeypatch):
    from samsara import previous_version
    from samsara.ui import update_qt
    monkeypatch.setattr(previous_version, 'previous_info', lambda: None)
    monkeypatch.setattr(update_qt, 'update_unavailable_reason', lambda: None)
    assert update_qt.previous_entry() == (None, 'No previous version is available to go back to.')
