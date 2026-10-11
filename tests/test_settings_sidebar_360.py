"""Settings sidebar matches the reference while keeping the accessibility floor (queue 360).

Asserts on the constructed window after __init__ (design scale included), never
on stylesheet strings. The look itself is judged from ui_proof/360 renders.
"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget

from samsara.ui import theme
from samsara.ui.settings import sidebar


@pytest.fixture
def dark_again():
    theme.set_theme("dark", refresh=False)
    yield
    theme.set_theme("dark", refresh=False)


@pytest.fixture
def window(qapp, dark_again):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp

    win = _SettingsWindow(_StubApp())
    win.resize(1160, 820)
    win.show()
    qapp.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    qapp.processEvents()


def _panel(win) -> QWidget:
    panel = win.findChild(QWidget, "settingsSidebar")
    assert panel is not None
    return panel


def _pill_in_panel(win, row: int):
    panel, pages = _panel(win), win._sidebar
    pill = sidebar.SidebarDelegate.pill_rect(pages.visualItemRect(pages.item(row)))
    top_left = pages.viewport().mapTo(panel, pill.topLeft())
    return top_left.x(), top_left.x() + pill.width() - 1, pill


def test_rows_and_search_are_at_least_the_hit_target(window):
    pages = window._sidebar
    for row in range(pages.count()):
        assert pages.visualItemRect(pages.item(row)).height() >= theme.HIT_TARGET_MIN
    assert window._search_edit.height() >= theme.HIT_TARGET_MIN


def test_search_lives_inside_the_sidebar_panel(window, qapp):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp

    panel = _panel(window)
    assert panel.isAncestorOf(window._search_edit)
    assert panel.isAncestorOf(window._sidebar)
    root = window.centralWidget().layout()
    assert all(root.itemAt(i).widget() is not window._search_edit for i in range(root.count()))

    tool = _SettingsWindow(_StubApp(), tool="Music")
    try:
        assert not tool.centralWidget().isAncestorOf(tool._search_edit)
    finally:
        tool.deleteLater()
        qapp.processEvents()


def test_search_edges_line_up_with_the_pill_edges(window):
    left, right, _ = _pill_in_panel(window, 0)
    search = window._search_edit.geometry()
    assert abs(search.left() - left) <= 1
    assert abs(search.right() - right) <= 1


def test_pill_is_inset_equally_by_space_1(window):
    panel = _panel(window)
    left, right, _ = _pill_in_panel(window, 0)
    assert abs(left - theme.SPACE_1) <= 1
    assert abs((panel.width() - 1 - right) - theme.SPACE_1) <= 1


def test_pill_height_and_label_column(window):
    pages = window._sidebar
    xs = set()
    for row in range(pages.count()):
        rect = pages.visualItemRect(pages.item(row))
        assert sidebar.SidebarDelegate.pill_rect(rect).height() == rect.height() - 4
        xs.add(sidebar.SidebarDelegate.label_rect(rect).left())
    assert len(xs) == 1


def test_row_text_size_and_selected_weight():
    regular = sidebar.SidebarDelegate.label_font(selected=False)
    heavy = sidebar.SidebarDelegate.label_font(selected=True)
    assert regular.pixelSize() >= theme.TYPE_BODY
    assert heavy.pixelSize() == regular.pixelSize()
    assert heavy.weight() > regular.weight()


@pytest.mark.parametrize("palette", ["dark", "light"])
def test_label_and_placeholder_contrast(palette, dark_again):
    theme.set_theme(palette, refresh=False)
    assert theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG1) >= 4.5
    placeholder = sidebar.placeholder_colour_token()
    for surface in (theme.BG1, theme.BG2):
        assert theme.contrast_ratio(placeholder, surface) >= 4.5


def test_items_names_and_navigation_unchanged(window):
    from samsara.ui import settings_qt

    pages = window._sidebar
    assert [pages.item(i).text() for i in range(pages.count())] == settings_qt._TAB_NAMES
    assert pages.accessibleName() == "Settings pages"
    assert window._search_edit.accessibleName() == "Search settings"
    for row in range(pages.count()):
        pages.setCurrentRow(row)
        assert window._stack.currentIndex() == row


def test_footer_has_air(window):
    apply = window.findChild(QPushButton, "apply")
    strip = window.findChild(QWidget, "footer")
    assert strip is not None and strip.isAncestorOf(apply)
    footer = strip.layout()
    assert isinstance(footer, QHBoxLayout)
    margins = footer.contentsMargins()
    assert min(margins.left(), margins.right(), margins.bottom()) >= theme.SPACE_2


def test_minimum_width_leaves_the_pages_their_old_room(window):
    # 720 - 180 before; 780 - 240 now: the page area is no narrower at minimum size.
    assert window.minimumWidth() - sidebar.SIDEBAR_WIDTH >= 720 - 180


def test_focus_ring_follows_keyboard_not_mouse(window, qapp):
    pages = window._sidebar
    pages.setFocus(Qt.FocusReason.TabFocusReason)
    qapp.processEvents()
    if not pages.hasFocus():  # an unfocused offscreen window cannot take focus
        pytest.skip("offscreen window did not accept focus")
    window.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, True)   # Tab / arrows
    assert sidebar.keyboard_focus_visible(pages) is True
    window.setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, False)  # a mouse click
    assert sidebar.keyboard_focus_visible(pages) is False


def test_theme_flip_repaints_the_panel_both_ways(window, qapp):
    panel = _panel(window)
    probe = QPoint(2, panel.height() - 3)  # empty panel surface below the rows

    def pixel():
        qapp.processEvents()
        return panel.grab().toImage().pixel(probe)

    dark = pixel()
    theme.set_theme("light")
    light = pixel()
    theme.set_theme("dark")
    dark_again = pixel()
    assert light != dark
    assert dark_again == dark
