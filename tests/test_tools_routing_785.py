"""Real tray actions, lazy Settings navigation, and the moved tool editors."""
import copy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QPushButton

from samsara import cuda_detect
from samsara.runtime import thread_registry
from samsara.ui import settings_qt
from samsara.ui.first_run_qt import ComponentsPage
from samsara.ui.settings.hermes_section import HermesSection
from samsara.ui.settings.presentation import Disclosure
from samsara.ui.tray_qt import SamsaraTrayQt
from tests.test_settings_pages import _StubApp, _schema_defaults
from tests.test_tray_qt import _make_app, _submenu


@pytest.fixture
def tools(qapp, monkeypatch):
    monkeypatch.setattr(settings_qt.qt_runtime, 'post', lambda callback: callback())
    monkeypatch.setattr(thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda *a: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *a: None)
    monkeypatch.setattr('samsara.ui.window_activation.confirm_foreground', lambda *a: None)
    monkeypatch.setattr('samsara.ui.settings.hermes_section.installed', lambda: True)
    config = _schema_defaults()
    config['command_packs'] = {'media': True, 'hermes': False}
    config['hermes']['notice_accepted'] = True
    owner = _StubApp(config)
    tray = SamsaraTrayQt(_make_app())
    tray._settings_tools = settings_qt.SettingsQt(owner)
    tray._rebuild_menu()
    yield tray
    window = tray._settings_tools._window
    if window is not None:
        window.hide()
        window.deleteLater()
    qapp.processEvents()


def _settle(qapp):
    for _ in range(4):
        qapp.processEvents()


def _assert_destination(window, name, qapp):
    _settle(qapp)
    page = window._tool_pages[name]
    assert window.isVisible()
    assert window._stack.currentWidget() is page
    label = 'Commands' if name == 'Command library' else name
    assert window.windowTitle() == 'Samsara — ' + label
    assert window._sidebar.currentItem().text() == label
    assert page.verticalScrollBar().value() == 0
    assert page.horizontalScrollBar().value() == 0


@pytest.mark.parametrize('name', settings_qt.tools_page.TOOLS)
def test_each_tray_action_opens_its_page_at_top_after_close(tools, qapp, name):
    menu = _submenu(tools._menu, 'Tools')
    action = next(a for a in menu.actions() if a.text() == name)
    # First open, repeated visible navigation, and rebuild of a hidden visit.
    action.trigger()
    window = tools._settings_tools._window
    _assert_destination(window, name, qapp)
    page = window._tool_pages[name]
    page.verticalScrollBar().setValue(page.verticalScrollBar().maximum())
    action.trigger()
    _assert_destination(window, name, qapp)
    window.close()
    action.trigger()
    assert tools._settings_tools._window is window
    _assert_destination(window, name, qapp)


def test_sidebar_reaches_all_tools_and_retains_drafts(tools, qapp):
    tools._settings_tools._show_preferences()
    window = tools._settings_tools._window
    before = copy.deepcopy(window.app.config)
    volume = window._widgets['sound_volume_slider']
    volume.setValue(37)
    assert not any(b.text() == 'Tools' for b in window.findChildren(QPushButton))
    entries = [window._sidebar.item(i).text() for i in range(window._sidebar.count())]
    assert entries == [*settings_qt._TAB_NAMES, 'Tools', 'Commands', 'Alarms',
                       'Health', 'Music', 'Components', 'Integrations']
    heading = window._sidebar.item(len(settings_qt._TAB_NAMES))
    assert not heading.flags() & Qt.ItemFlag.ItemIsSelectable
    for name in settings_qt.tools_page.TOOLS:
        window._sidebar.setCurrentRow(window._tool_sidebar_rows[name])
        _assert_destination(window, name, qapp)
        assert volume.value() == 37
        assert window.app.config == before
    window.show_tab('Audio')
    assert window._collect_destination_updates({})['sound_volume'] == .37


@pytest.mark.parametrize('first', ('Commands', 'Integrations'))
def test_packs_are_visible_and_hermes_lives_only_in_integrations(tools, qapp, first):
    controller = tools._settings_tools
    controller.show_tool(first)
    window = controller._window
    window.open_tool('Commands')
    _settle(qapp)
    commands = window._tool_pages['Command library']
    packs = window._widgets['_pack_scroll']
    assert packs.isVisible()
    assert not any(d.isAncestorOf(packs) for d in commands.findChildren(Disclosure))
    heading = next(l for l in commands.findChildren(QLabel) if l.text() == 'Command packs')
    assert heading.isVisible()
    assert commands.viewport().rect().intersects(packs.geometry())
    hermes = window._widgets['hermes_section']
    assert not commands.isAncestorOf(hermes)
    window.open_tool('Integrations')
    _settle(qapp)
    integrations = window._tool_pages['Integrations']
    assert integrations.isAncestorOf(hermes)
    assert hermes.isVisible()
    assert not commands.findChildren(HermesSection)


def test_moved_hermes_apply_is_scoped_to_integrations(tools, qapp):
    tools._settings_tools.show_tool('Integrations')
    window = tools._settings_tools._window
    window._widgets['hermes_section'].enabled.setChecked(True)
    window._widgets['hermes_model'].setText('test-model')
    window._widgets['_pack_checkboxes']['media'].setChecked(False)
    updates = window._collect_destination_updates({})
    assert updates['hermes']['model'] == 'test-model'
    assert updates['command_packs']['hermes'] is True
    assert updates['command_packs'].get('media') == window.app.config['command_packs'].get('media')
    window.open_tool('Commands')
    updates = window._collect_destination_updates({})
    assert 'hermes' not in updates
    assert not updates['command_packs'].get('hermes', False)
    assert updates['command_packs']['media'] is False


def test_minimized_tool_and_preference_reopen_choose_requested_page(tools, qapp):
    tools._settings_tools.show_tool('Music')
    window = tools._settings_tools._window
    window.showMinimized()
    _settle(qapp)
    window.open_tool('Components')
    _assert_destination(window, 'Components', qapp)
    assert not window.isMinimized()
    window.close()
    window.show_tab('Audio')
    _settle(qapp)
    assert window._tool_name is None
    assert window._stack.currentWidget() is window._pages['Audio']
