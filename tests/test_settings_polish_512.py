"""Settings separators, actual control geometry and viewport-only soft edges."""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFrame, QPushButton, QScrollArea, QVBoxLayout, QWidget
from samsara.ui import theme
from samsara.ui.settings.calm_layout import ScrollEdges
from samsara.ui.settings.presentation import SettingRow, Disclosure
from tests.test_settings_calm_layout_459 import settings, settle, reveal


def destinations(window):
    return dict.fromkeys((*window._pages.values(), *window._tool_pages.values()))


@pytest.mark.parametrize('width', (780, 1160))
def test_every_page_rows_have_one_full_width_hairline_and_right_edge(settings, qapp, width):
    settings.resize(width, 900)
    settings._widgets['todoist_enabled'].setChecked(True)
    for page in destinations(settings):
        settings._stack.setCurrentWidget(page)
        if hasattr(page, 'advanced'):
            page.advanced.reveal()
        for disclosure in page.findChildren(Disclosure):
            disclosure.reveal()
        settle(qapp)
        assert page.horizontalScrollBar().maximum() == 0
        assert page.widget().width() <= page.viewport().width()
        for layout in page.widget().findChildren(QVBoxLayout):
            visible = [layout.itemAt(i).widget() for i in range(layout.count())
                       if layout.itemAt(i).widget() and not layout.itemAt(i).widget().isHidden()]
            for i, row in enumerate(visible):
                if row.property('role') != 'row' and not row.property('settingsToolRow'):
                    continue
                following = visible[i + 1:]
                divider = following and following[0].property('settingsRowDivider')
                next_row = following[1] if divider and len(following) > 1 else following[0] if following else None
                expected = next_row is not None and (next_row.property('role') == 'row' or next_row.property('settingsToolRow'))
                assert bool(divider) == bool(expected), (page.windowTitle(), getattr(row, 'title', row.objectName()))
                if divider:
                    line = following[0].findChild(QFrame, 'settingsRowHairline')
                    assert line.height() == 1
                    assert line.width() == row.width()
                    margins = following[0].layout().contentsMargins()
                    assert margins.top() == margins.bottom() == theme.SPACE_1 // 2
                    assert not (len(following) > 1 and following[1].property('settingsRowDivider'))
        edges = [row.value.mapTo(page.widget(), QPoint(row.value.width(), 0)).x()
                 for row in page.findChildren(SettingRow) if row.isVisibleTo(page)]
        if edges:
            assert max(edges) - min(edges) <= 1, edges
        for scroll in (page, *page.findChildren(QScrollArea)):
            assert scroll.horizontalScrollBar().maximum() == 0


def test_filter_and_conditions_remove_trailing_and_duplicate_dividers(settings, qapp):
    settings._stack.setCurrentWidget(settings._pages['General'])
    settings._search_edit.setText('Theme')
    settings._apply_search_filter()
    settle(qapp)
    page = settings._pages['General']
    row = settings._rows_by_widget['theme_combo']
    assert row.isVisibleTo(page)
    assert not any(widget.isVisibleTo(page) for widget in page.findChildren(QWidget)
                   if widget.property('settingsRowDivider'))
    settings._search_edit.clear()
    settings._apply_search_filter()
    settle(qapp)
    before = len(page.findChildren(QFrame, 'settingsRowHairline'))
    settings.apply_theme()
    settings.apply_theme()
    settle(qapp)
    assert len(page.findChildren(QFrame, 'settingsRowHairline')) == before
    page.advanced.reveal()
    settings._search_edit.setText('unmatched-512-fixture')
    settings._apply_search_filter()
    settle(qapp)
    assert page.advanced.no_match.isVisibleTo(page)
    settings._search_edit.clear()
    settings._apply_search_filter()
    settle(qapp)


def test_help_is_beside_title_and_long_copy_retains_its_words(settings, qapp):
    page = reveal(settings, 'General', qapp)
    row = settings._rows_by_widget['hints_enabled']
    heading = row.heading.mapTo(row, QPoint(row.heading.width(), 0)).x()
    help_left = row.help.mapTo(row, QPoint()).x()
    assert 0 <= help_left - heading <= theme.SPACE_1 + 1
    long = settings._rows_by_widget['listening_display']
    assert long.detail.text() == long.description
    assert long.value.mapTo(long, QPoint()).y() < long.detail.mapTo(long, QPoint()).y() + long.detail.height() / 2


def test_fades_follow_scroll_range_and_are_mouse_and_keyboard_transparent(settings, qapp):
    for page in destinations(settings):
        settings._stack.setCurrentWidget(page)
        settle(qapp)
        for scroll in (page, *page.findChildren(QScrollArea)):
            bar = scroll.verticalScrollBar()
            edges = scroll._settings_edges
            for value in (bar.minimum(), (bar.minimum() + bar.maximum()) // 2, bar.maximum()):
                bar.setValue(value)
                settle(qapp)
                assert (not edges.top.isHidden()) == (bar.value() > bar.minimum())
                assert (not edges.bottom.isHidden()) == (bar.value() < bar.maximum())
                for overlay in (edges.top, edges.bottom):
                    assert overlay.parentWidget() is scroll.viewport()
                    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
                    assert overlay.height() <= 24
                    assert overlay.width() == scroll.viewport().width()


def test_non_scrolling_viewport_has_no_fade_then_updates_on_resize(qapp):
    scroll = QScrollArea()
    content = QWidget()
    content.setFixedSize(250, 300)
    scroll.setWidget(content)
    edges = ScrollEdges(scroll)
    scroll.resize(300, 400)
    scroll.show()
    settle(qapp)
    assert edges.top.isHidden() and edges.bottom.isHidden()
    scroll.resize(300, 150)
    settle(qapp)
    assert edges.top.isHidden() and not edges.bottom.isHidden()
    scroll.verticalScrollBar().setValue(20)
    settle(qapp)
    assert not edges.top.isHidden() and not edges.bottom.isHidden()
    scroll.hide()
    scroll.deleteLater()


def test_fade_paints_the_active_theme_and_clicks_reach_the_control(qapp):
    scroll = QScrollArea()
    button = QPushButton('Fixture')
    button.setFixedSize(250, 1000)
    button.setStyleSheet(f'background:{theme.TEXT_PRIMARY};border:none;')
    scroll.setWidget(button)
    edges = ScrollEdges(scroll)
    scroll.resize(300, 150)
    scroll.show()
    scroll.verticalScrollBar().setValue(30)
    settle(qapp)
    clicked = []
    button.clicked.connect(lambda: clicked.append(True))
    point = QPoint(50, 3)
    assert scroll.viewport().childAt(point) is button
    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.mapFrom(scroll.viewport(), point))
    assert clicked == [True]
    for mode in ('light', 'dark'):
        theme.set_theme(mode, refresh=False)
        edges.refresh()
        settle(qapp)
        capture = scroll.viewport().grab().toImage()
        dpr = capture.devicePixelRatio()
        pixel = capture.pixelColor(round(50 * dpr), 0)
        background = theme.qcolor(theme.BG0)
        assert max(abs(a-b) for a, b in zip(pixel.getRgb()[:3], background.getRgb()[:3])) <= 12
    scroll.hide()
    scroll.deleteLater()


def test_component_rows_receive_dividers_after_a_manifest_rebuild(settings, qapp):
    components = settings._components_page
    original = components._manifest
    try:
        components._manifest = {'components': [dict(id=f'fixture{i}', name=f'Example component {i}',
            size_bytes=12000000, description='Example capability.', available=False,
            install_dir='fixture', requires=[], default=False) for i in range(3)]}
        components._rebuild()
        page = settings._tool_pages['Components']
        settings._stack.setCurrentWidget(page)
        settle(qapp)
        assert all(controls['_settings_card'].property('settingsToolRow') for controls in components._rows.values())
        lines = components._rows_host.findChildren(QFrame, 'settingsRowHairline')
        assert sum(not line.isHidden() for line in lines) == 2
        settings.apply_theme()
        settle(qapp)
        assert len(components._rows_host.findChildren(QFrame, 'settingsRowHairline')) == 2
    finally:
        components._manifest = original
        components._rebuild()
        settle(qapp)


def test_search_rest_mouse_and_keyboard_focus_have_distinct_painted_borders(settings, qapp):
    qapp.setActiveWindow(settings)
    settings.activateWindow()
    settle(qapp)
    edit = settings._search_edit
    settings._sidebar.setFocus(Qt.FocusReason.TabFocusReason)
    settle(qapp)
    rest = edit.grab().toImage()
    edit.setFocus(Qt.FocusReason.MouseFocusReason)
    settle(qapp)
    mouse = edit.grab().toImage()
    assert edit.property('settingsKeyboardFocus') is False
    edit.clearFocus()
    edit.setFocus(Qt.FocusReason.TabFocusReason)
    settle(qapp)
    focus = edit.grab().toImage()
    assert edit.property('settingsKeyboardFocus') is True
    x = round(edit.width() / 2 * focus.devicePixelRatio())
    assert focus.pixelColor(x, 0) != rest.pixelColor(x, 0)
    assert mouse.pixelColor(x, 0) == rest.pixelColor(x, 0)
    glyph = edit.findChild(QWidget, 'settingsSearchGlyph')
    edit.setText('Theme')
    settle(qapp)
    assert edit.cursorRect().x() >= glyph.geometry().right() + theme.SPACE_2 - 5
    settings.apply_theme()
    settle(qapp)
    assert edit.property('settingsKeyboardFocus') is True
    edit.clear()
