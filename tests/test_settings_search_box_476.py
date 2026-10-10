"""Settings search reserves icon space for placeholder, text and caret."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QStyle, QStyleOptionFrame, QToolButton, QWidget
from samsara.ui import theme


@pytest.mark.parametrize('mode', ['dark', 'light'])
@pytest.mark.parametrize('text', ['', 'echo'])
def test_search_icon_clearance_and_escape(qapp, mode, text):
    old = theme.active_theme()
    theme.set_theme(mode, refresh=False)
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings import _StubApp
    panel = _SettingsWindow(_StubApp())
    panel.resize(1120, 880)
    edit = panel._search_edit
    try:
        panel.show()
        edit.setFocus(Qt.FocusReason.TabFocusReason)
        edit.setText(text)
        qapp.processEvents()
        QTest.qWait(200)  # Qt animates the clear button into view.
        glyph = edit.findChild(QWidget, 'settingsSearchGlyph')
        option = QStyleOptionFrame()
        edit.initStyleOption(option)
        contents = edit.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, edit)
        text_left = contents.x() + edit.textMargins().left() + 2
        assert text_left - (glyph.x() + glyph.width()) >= theme.SPACE_2
        theme.apply_design_scale(panel)  # A restyle must preserve the column too.
        qapp.processEvents()
        edit.initStyleOption(option)
        contents = edit.style().subElementRect(QStyle.SubElement.SE_LineEditContents, option, edit)
        assert contents.x() + edit.textMargins().left() + 2 >= glyph.x() + glyph.width() + theme.SPACE_2
        edit.setCursorPosition(0)
        assert edit.cursorRect().x() >= glyph.x() + glyph.width() + theme.SPACE_2 - 5
        assert edit.height() == theme.HIT_TARGET_MIN
        assert edit.isClearButtonEnabled()
        if text:
            clear = next(button for button in edit.findChildren(QToolButton) if button.isVisible())
            assert edit.rect().contains(clear.geometry())
            QTest.mouseClick(clear, Qt.MouseButton.LeftButton)
            assert edit.text() == ''
            edit.setText(text)
        QTest.keyClick(edit, Qt.Key.Key_Escape)
        assert edit.text() == ''
        assert edit.hasFocus()
    finally:
        panel.hide()
        panel.deleteLater()
        theme.set_theme(old, refresh=False)
