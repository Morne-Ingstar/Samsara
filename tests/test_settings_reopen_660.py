"""Regression for rebuilding Settings after its first close (660)."""

from PySide6.QtWidgets import QVBoxLayout
from shiboken6 import isValid

from tests.test_settings_revisit_543 import window as settings_window
from tests.test_settings_revisit_543 import _wait_catalog


def test_settings_close_reopen_and_switch_tabs_keeps_only_live_rows(qapp, settings_window):
    window = settings_window
    window.show()
    qapp.processEvents()
    window.close()
    qapp.processEvents()
    window.show()
    _wait_catalog(window)
    qapp.processEvents()
    window.show_tab('Dictation')
    qapp.processEvents()
    window.show_tab('Audio')
    qapp.processEvents()

    assert window.isVisible()
    for layout in window.findChildren(QVBoxLayout):
        dividers = getattr(layout, '_settings_dividers', None)
        if dividers is not None:
            assert all(isValid(row) for group in dividers.groups for row in group)
