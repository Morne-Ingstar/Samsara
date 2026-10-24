"""839: measure the four hub surfaces in their real host, in both palettes."""
from datetime import datetime
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import (
    QLabel, QLineEdit, QListView, QMessageBox, QPushButton, QStyle, QStyleOptionButton, QTableView, QWidget,
)

from samsara.ui import dictionary_panel_qt as dictionary, history_view, main_window_qt, theme
from samsara.ui.settings import sidebar


@pytest.fixture(params=['dark', 'light'])
def hub(request, qapp, tmp_path, monkeypatch, qt_widget_scope):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    values = {}

    class Settings:
        def __init__(self, *args):
            pass

        def value(self, key, default=None, type=None):
            return values.get(key, default)

        def setValue(self, key, value):
            values[key] = value

    for module in (dictionary, history_view):
        monkeypatch.setattr(module, 'QSettings', Settings)
    monkeypatch.setattr(history_view.thread_registry, 'spawn', lambda _name, target, **kw: target())
    store = SimpleNamespace(custom_vocab=['Siobhan', 'Kubernetes'],
                            corrections_dict={'pie side': 'PySide'}, get_builtin_words=lambda: [])
    monkeypatch.setattr(dictionary, 'get_dictionary_store', lambda app: store)
    monkeypatch.setattr(main_window_qt._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 1920, 1040)], (0, 0, 1920, 1040)))
    # Preserve actual History rendering/copy behaviour without a background DB.
    history = [(datetime.now().isoformat(), 'A synthetic entry to recover.', False)]
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json',
                          history=history, available_mics=[], update_config_and_save=lambda changes: True)
    previous = theme.active_theme()
    theme.set_theme(request.param, refresh=False)
    window = main_window_qt._MainWindow(app)
    window._poll_timer.stop()
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(1920, 1080)
    window.apply_theme()
    with qt_widget_scope(window):
        window.show()
        qapp.processEvents()
        yield window
    theme.set_theme(previous, refresh=False)


@pytest.mark.parametrize('name,tab', [('History', None), ('Snippets', None), ('Dictionary', 0), ('Dictionary', 1)])
def test_headers_widths_heights_and_destructive_style(hub, qapp, name, tab):
    page = hub._activate(name)
    if tab is not None:
        page._tabs.setCurrentIndex(tab)
    qapp.processEvents()
    title = page.findChild(QLabel, 'hubHeaderTitle')
    assert title is not None and title.text() == name and title.isVisibleTo(page)
    assert page.layout().contentsMargins().top() == 24
    assert page._header.layout().contentsMargins().bottom() == 16
    assert title.font().pixelSize() == theme.HUB_TITLE_SIZE
    assert page._header.purpose.font().pixelSize() == theme.HUB_PURPOSE_SIZE
    for kind in (QLineEdit, QTableView, QListView):
        for control in page.findChildren(kind):
            ceiling = theme.HUB_INPUT_WIDTH if kind is QLineEdit else theme.HUB_LIST_WIDTH
            assert control.maximumWidth() <= ceiling, (name, control.accessibleName())
            assert control.width() <= ceiling
    for button in page.findChildren(QPushButton):
        assert button.height() in {theme.HUB_CONTROL_HEIGHT, theme.HUB_INLINE_HEIGHT}, button.text()
    for widget in page.findChildren(QWidget):
        if widget.objectName() == 'danger':
            assert isinstance(widget.window(), QMessageBox)
    assert theme.ERROR not in page.styleSheet()
    if name == 'Snippets':
        assert page._text.maximumWidth() == 480
        assert page._text.maximumHeight() == theme.HIT_TARGET_MIN * 3


def test_history_card_copy_search_range_and_footer(hub, qapp):
    page = hub._activate('History')
    qapp.processEvents()
    assert page._search.width() == 320 and page._scope.width() == 140
    assert page._scope.x() - page._search.geometry().right() - 1 == 8
    rows = page.findChildren(history_view._HistoryRow)
    assert rows
    row = rows[0]
    assert row.objectName() == 'historyCard'
    assert row.time_label.y() < row.text_label.y()
    assert row.copy_button.isVisibleTo(page) and row.copy_button.height() == theme.HUB_INLINE_HEIGHT
    assert row.copy_button.x() - row.text_label.geometry().right() - 1 == 16
    assert page._clear_btn.x() - page._export_btn.geometry().right() - 1 == 12
    assert page._export_btn.x() < page._list.geometry().right()
    option = QStyleOptionButton()
    option.initFrom(page._show_empty_wake)
    indicator = page._show_empty_wake.style().subElementRect(
        QStyle.SubElement.SE_CheckBoxIndicator, option, page._show_empty_wake)
    assert indicator.width() == indicator.height() == 18


def test_dictionary_keeps_tabs_disclosures_and_preference(hub, qapp):
    page = hub._activate('Dictionary')
    qapp.processEvents()
    assert [page._tabs.tabText(i) for i in range(3)] == ['Vocabulary', 'Corrections', 'Wake Words']
    for button in (page._voice_expander, page._builtin_expander):
        assert button.height() == theme.HUB_INLINE_HEIGHT
        assert button.text().startswith('▸ ')
        button.click()
        assert button.text().startswith('▾ ')
        button.click()
    assert page._builtin_enabled.y() > page._builtin_expander.y()
    for i in (0, 1):
        assert page._tabs.widget(i).layout().contentsMargins().bottom() == 16
    assert page._corrections_table.horizontalHeader().defaultAlignment() & Qt.AlignmentFlag.AlignLeft
    for width, height in ((900, 650), (700, 500)):
        hub.resize(width, height)
        qapp.processEvents()
        assert (hub.width(), hub.height()) == (width, height)
        viewport = page._body_scroll.viewport()
        for control in (page._vocab_list, page._vocab_input,
                        *page._tabs.widget(0).findChildren(QPushButton)):
            page._body_scroll.ensureWidgetVisible(control, 0, 0)
            qapp.processEvents()
            assert viewport.rect().contains(QRect(control.mapTo(viewport, QPoint()), control.size()))


def test_sidebar_matches_settings_metrics_without_changing_825_glyphs(hub):
    assert main_window_qt.NAV_RADIUS == sidebar.PILL_RADIUS
    assert main_window_qt.NAV_ICON_PX == sidebar.ICON_BOX
    assert main_window_qt.NAV_SIDE_MARGIN == theme.SPACE_1
    margins = hub.findChild(QWidget, 'hubSidebar').layout().contentsMargins()
    assert margins.left() == margins.right() == main_window_qt.NAV_SIDE_MARGIN
    assert tuple(hub._nav_btns) == main_window_qt.NAV_ORDER
    for button in hub._nav_btns.values():
        assert button.iconSize().width() == sidebar.ICON_BOX
        assert f'border-radius: {sidebar.PILL_RADIUS}px' in button.styleSheet()
        assert 'border-left' not in button.styleSheet()
