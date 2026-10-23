"""595: usable copy controls and non-moving, responsive setup illustrations."""
from types import SimpleNamespace
import base64
import json
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from samsara.ui.widgets import code_snippet as snippet
from samsara.ui.settings import agents_section as agents
from tests.test_connect_ai_app_570 import panel
from tests.test_agent_server_564 import memory_clipboard


@pytest.fixture
def field(qapp, monkeypatch):
    copied = []
    monkeypatch.setattr(snippet, 'QApplication', SimpleNamespace(clipboard=lambda: SimpleNamespace(setText=copied.append)))
    widget = snippet.CodeSnippet('claude', name='sign-in command')
    widget.resize(500, 80)
    widget.show()
    qapp.processEvents()
    yield widget, copied
    widget.hide()
    widget.deleteLater()


def test_copy_visible_at_rest_and_keyboard_accessible(field):
    widget, copied = field
    assert widget.copy_button.isVisible() and widget.copy_button.text() == 'Copy'
    assert widget.copy_button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    interface = QAccessible.queryAccessibleInterface(widget.copy_button)
    assert interface.text(QAccessible.Text.Name) == 'Copy sign-in command'


@pytest.mark.parametrize('key', [Qt.Key.Key_Return, Qt.Key.Key_Space])
def test_keyboard_copy_confirms_then_resets(field, key):
    widget, copied = field
    widget.copy_button.setFocus()
    QTest.keyClick(widget.copy_button, key)
    assert copied == ['claude'] and widget.copy_button.text() == 'Copied'
    assert widget._confirmation.interval() == 1500
    QTest.qWait(1600)
    assert widget.copy_button.text() == 'Copy'


def test_rejected_copy_does_not_confirm(field):
    widget, copied = field
    widget.copy_action = lambda: False
    widget.copy_button.click()
    assert copied == [] and widget.copy_button.text() == 'Copy'


def test_highlighted_example_copies_exact_json_without_markup(field):
    widget, copied = field
    text = '{\n  "before": {},\n  "added": {}\n}'
    widget.setText(text, highlight_lines=range(1, 3))
    assert widget.preview.textFormat() == Qt.TextFormat.RichText
    assert 'background-color:' in widget.preview.text()
    widget.copy_button.click()
    assert copied == [text]
    assert json.loads(copied[0]) == {'before': {}, 'added': {}}


def test_private_previews_are_masked_and_copy_real_command(panel):
    controls, state, clipboard, timers = panel
    blocks = controls.findChildren(snippet.CodeSnippet)
    # 786: Hermes has a profile token block and an MCP configuration block.
    assert len(blocks) == 9
    assert controls.command_preview.preview.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction
    assert all(state.token not in block.text() for block in blocks)
    controls.code_copy.click()
    assert state.token in clipboard.text()
    assert controls.code_copy.text() == 'Copied'
    assert timers[-1][0] == 60_000
    assert clipboard.mimeData().hasFormat(agents._EXCLUDE_HISTORY)


def test_merge_example_preserves_existing_entries_and_has_one_samsara_entry(panel):
    controls, state, clipboard, timers = panel
    merged = json.loads(controls.merge_example.text())
    whole = json.loads(controls.desktop_preview.text())
    assert merged['mcpServers']['samsara'] == whole['mcpServers']['samsara']
    assert merged['mcpServers']['other-app'] == {'command': 'other-app'}
    assert merged['preferences'] == {}
    assert 'mcpServers' not in merged['mcpServers']['samsara']
    assert '},\n    "samsara"' in controls.merge_example.text()


def test_illustration_uses_three_stills_and_stacks_when_narrow(qapp):
    widget = agents.SetupIllustration()
    try:
        assert len(base64.b64decode(agents.GIF_DATA)) <= 400_000
        widget.resize(850, 180)
        widget.show()
        qapp.processEvents()
        assert widget.grid.rowCount() == 1
        pictures = [label for label in widget.findChildren(QLabel) if not label.pixmap().isNull()]
        assert len(pictures) == 3
        assert not any(label.movie() for label in pictures)
        assert widget.focusPolicy() == Qt.FocusPolicy.NoFocus
        widget.resize(500, 500)
        qapp.processEvents()
        assert all(widget.grid.getItemPosition(widget.grid.indexOf(card))[:2] == (i, 0) for i, card in enumerate(widget.cards))
    finally:
        widget.hide()
        widget.deleteLater()
