"""The Ava guide is a content-sized primary action below the setup message."""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara.ui import theme
from tests.test_settings_dell_1010 import window


@pytest.mark.parametrize("window,policy", [(route, route) for route in ("off", "local", "cloud")],
                         indirect=["window"], ids=["off", "local", "cloud"])
def test_guide_placement_and_primary_style(window, qapp, policy):
    window.resize(1280, 860)
    window.show_tab("Ava")
    window.apply_theme()
    qapp.processEvents()
    page = window._pages["Ava"]
    button = window._widgets["ava_setup_guide_button"]

    assert window._widgets["providerRoute"].currentData() == policy
    assert button.isVisible() and button.isEnabled()
    assert button.text() == "Open Ava guide"
    assert button.accessibleName() == "Ava guide"
    assert button.property("class") == "primary"
    assert theme.ACCENT in button.styleSheet()
    assert button.x() == page.subtitle.x()
    assert button.y() > page.subtitle.geometry().bottom()
    assert button.height() >= 36
    assert button.width() == button.sizeHint().width() < page.guide_header.width()
    assert page.base.itemAt(0).widget() is window._rows_by_widget["providerRoute"]


def test_guide_is_in_tab_order_and_opens_from_keyboard(window, qapp):
    window.show_tab("Ava")
    window.activateWindow()
    qapp.processEvents()
    button = window._widgets["ava_setup_guide_button"]
    assert button.focusPolicy() & Qt.FocusPolicy.TabFocus
    button.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.keyClick(button, Qt.Key.Key_Backtab)
    previous = window.focusWidget()
    assert previous is not button
    QTest.keyClick(previous, Qt.Key.Key_Tab)
    assert window.focusWidget() is button
    assert not hasattr(window.app, "guide_opened")
    QTest.keyClick(button, Qt.Key.Key_Space)
    assert window.app.guide_opened is True
