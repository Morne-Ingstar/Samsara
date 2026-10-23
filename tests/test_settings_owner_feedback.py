"""Owner regressions: Tools navigate in place and sliders blend into cards."""
import copy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
from samsara.ui import settings_qt, theme
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def window(qapp, monkeypatch):
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: [])
    monkeypatch.setattr(settings_qt.qt_runtime, 'post', lambda callback: callback())
    win = settings_qt._SettingsWindow(_StubApp(copy.deepcopy(_schema_defaults())))
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    win.show()
    qapp.processEvents()
    yield win
    for widget in qapp.topLevelWidgets():
        if isinstance(widget, settings_qt._SettingsWindow):
            widget.hide()
            widget.deleteLater()
    qapp.processEvents()
    theme.set_theme('dark', refresh=False)


def test_explicit_settings_reopen_restores_minimized_shared_window(window, qapp):
    volume = window._widgets['sound_volume_slider']
    volume.setValue(37)
    window.showMinimized()
    qapp.processEvents()
    assert window.isMinimized()

    window.show_tab('Audio')
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window._stack.currentWidget() is window._pages['Audio']
    assert volume.value() == 37

    window.showMinimized()
    qapp.processEvents()
    window.open_tool('Music')
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window._stack.currentWidget() is window._tool_pages['Music']
    assert volume.value() == 37


def test_tools_menu_reuses_current_window_and_preserves_drafts(window, qapp):
    original_config = copy.deepcopy(window.app.config)
    volume = window._widgets['sound_volume_slider']
    volume.setValue(37)
    # 785: sidebar navigation replaces the Tools drop-up and keeps draft editors.
    assert not any(button.text() == 'Tools' for button in window.findChildren(QPushButton))
    for name in ('Integrations', 'Music', 'Command library', 'Alarms', 'Health',
                 'Components', 'Integrations'):
        window._sidebar.setCurrentRow(window._tool_sidebar_rows[name])
        qapp.processEvents()
        visible = [w for w in qapp.topLevelWidgets()
                   if isinstance(w, settings_qt._SettingsWindow) and w.isVisible()]
        assert visible == [window], 'Tools opened another Settings window'
        assert window._stack.currentWidget() is window._tool_pages[name]
        if name == 'Integrations':
            window._widgets['todoist_enabled'].setChecked(True)
        assert volume.value() == 37
        assert window.app.config == original_config
    assert window._widgets['todoist_enabled'].isChecked()
    # Tool Apply collects only that tool, not the pending Audio edit.
    assert 'sound_volume' not in window._collect_destination_updates({})
    window.show_tab('Audio')
    assert window._stack.currentWidget() is window._pages['Audio']
    assert window._collect_destination_updates({})['sound_volume'] == .37
    assert window.app.config == original_config


def test_controller_and_preference_links_reuse_window(window, qapp):
    controller = settings_qt.SettingsQt(window.app)
    controller._window = window
    for name in ('Alarms', 'Music', 'Alarms'):
        controller.show_tool(name)
        qapp.processEvents()
        assert controller._window is window
        assert window._stack.currentWidget() is window._tool_pages[name]
        assert [w for w in qapp.topLevelWidgets()
                if isinstance(w, settings_qt._SettingsWindow) and w.isVisible()] == [window]
    window.open_preferences('Shortcuts')
    assert window._stack.currentWidget() is window._pages['Shortcuts']
    assert window._tool_name is None
    window.open_tool('Music')
    window._search_edit.setText('Sound volume')
    window._apply_search_filter()
    assert window._stack.currentWidget() is window._pages['Audio']
    assert window._tool_name is None


@pytest.mark.parametrize('palette', ('dark', 'light'))
def test_sound_slider_background_matches_card(window, qapp, palette):
    from PySide6.QtCore import QPoint
    theme.set_theme(palette, refresh=False)
    window.apply_theme()
    window.show_tab('Audio')
    qapp.processEvents()
    row = window._rows_by_widget['sound_volume_slider']
    slider = window._widgets['sound_volume_slider']
    image = row.grab().toImage()
    point = slider.mapTo(row, QPoint(3, 3))
    ratio = image.devicePixelRatio()
    slider_background = image.pixelColor(int(point.x() * ratio), int(point.y() * ratio))
    card_background = image.pixelColor(int(20 * ratio), int(5 * ratio))
    import os
    from pathlib import Path
    if os.environ.get('SETTINGS_PROOF_DIR'):
        directory = Path(os.environ['SETTINGS_PROOF_DIR'])
        directory.mkdir(parents=True, exist_ok=True)
        assert image.save(str(directory / ('sound-volume-' + palette + '.png')))
    assert slider_background == card_background, (
        slider_background.name(), card_background.name())
