"""860: rendered backgrounds in the shared theme, rather than QSS string checks."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
from samsara.ui import theme, dictionary_panel_qt as dictionary
from samsara.ui.main_window_qt import _MainWindow
from samsara.ui.memos_qt import MemosPage
from samsara.ui.ava_captions_qt import _CaptionsWindow


@pytest.fixture(params=['dark', 'light'])
def host(request, qapp, tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(dictionary, 'get_dictionary_store', lambda app: SimpleNamespace(
        custom_vocab=['Siobhan'], corrections_dict={}, get_builtin_words=lambda: []))
    monkeypatch.setattr(_MainWindow, '_restore_geometry', lambda self: None)
    monkeypatch.setattr(_MainWindow, '_save_geometry', lambda self: None)
    monkeypatch.setattr(_MainWindow, '_activate', lambda self, name: None)
    previous = theme.active_theme()
    previous_sheet = qapp.styleSheet()
    theme.set_theme(request.param, refresh=False)
    qapp.setStyleSheet('')
    app = SimpleNamespace(config={}, config_path=tmp_path/'config.json', available_mics=[])
    window = _MainWindow(app)
    window._poll_timer.stop()
    window.resize(1400, 900)
    window.apply_theme()
    window.show()
    yield window, app
    window.force_close()
    qapp.setStyleSheet(previous_sheet)
    theme.set_theme(previous, refresh=False)


def mount(host, page, qapp):
    window, _ = host
    window._stack.addWidget(page)
    window._stack.setCurrentWidget(page)
    qapp.processEvents()
    return window.grab().toImage()


def test_tab_remainder_matches_page_and_tabs_have_outlines(host, qapp):
    page = dictionary.DictionaryPanelQt(host[1])
    image = mount(host, page, qapp)
    tabs = page._tabs
    bar = tabs.tabBar()
    remainder = tabs.mapTo(host[0], QPoint(tabs.width()-10, bar.height()//2))
    assert image.pixelColor(remainder) == theme.qcolor(theme.BG1)
    outside = page.mapTo(host[0], QPoint(page.width()-10, 0))
    assert image.pixelColor(remainder) == image.pixelColor(outside.x(), remainder.y())
    painted = bar.grab().toImage()
    for index in range(3):
        rect = bar.tabRect(index)
        expected = theme.ACCENT if index == 0 else theme.BORDER
        swatch = QImage(1, 1, QImage.Format.Format_ARGB32)
        swatch.fill(theme.qcolor(theme.BG2 if index == 0 else theme.BG1))
        painter = QPainter(swatch)
        painter.fillRect(swatch.rect(), theme.qcolor(expected))
        painter.end()
        actual = painted.pixelColor(rect.center().x()-2, rect.top())
        # QSS quantizes alpha slightly differently from QPainter on Windows.
        assert all(abs(a-b) <= 1 for a, b in zip(actual.getRgb(), swatch.pixelColor(0, 0).getRgb()))
    assert [tabs.tabText(i) for i in range(3)] == ['Vocabulary', 'Corrections', 'Wake Words']


def test_memo_arrows_have_no_inner_square(host, qapp, tmp_path):
    page = MemosPage(host[1], home=tmp_path)
    mount(host, page, qapp)
    page._editor_pane.show()
    qapp.processEvents()
    for combo in (page._filter_category, page._category):
        option = QStyleOptionComboBox()
        combo.initStyleOption(option)
        arrow = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, option,
                                            QStyle.SubControl.SC_ComboBoxArrow, combo)
        image = combo.grab().toImage()
        # Former native subcontrol frame: the top and left of its square must
        # now be uninterrupted control fill. Avoid the outer rounded border.
        assert image.pixelColor(arrow.left()+2, 3) == theme.qcolor(theme.BG2)
        assert image.pixelColor(arrow.left()+2, combo.height()//2) == theme.qcolor(theme.BG2)


def test_caption_sections_share_card_background(host, qapp):
    panel = _CaptionsWindow(host[1])
    panel.setParent(host[0], panel.windowFlags())
    try:
        panel._text.setText('A synthetic reply to check the caption background.')
        panel._text.setMaximumWidth(400)
        panel.adjustSize()
        panel.show()
        qapp.processEvents()
        image = panel.grab().toImage()
        for label in (panel._who, panel._text, panel._hint):
            point = label.mapTo(panel, QPoint(label.width()-2, 1))
            assert image.pixelColor(point) == theme.qcolor(theme.BG1)
    finally:
        panel.close()
