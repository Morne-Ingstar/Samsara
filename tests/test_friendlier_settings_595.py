"""595: native Settings composition, lazy notice and navigable references."""
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import QApplication, QLabel
from shiboken6 import isValid
from samsara.ui import settings_qt
from samsara.ui.settings import agents_section, tools_page
from samsara.ui.widgets.beta_notice import BetaNotice
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def window(qapp, monkeypatch):
    from samsara import autostart, cuda_detect
    from samsara.runtime import thread_registry
    from samsara.ui.first_run_qt import ComponentsPage
    import sounddevice
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(sounddevice, 'query_devices', lambda *a, **k: [])
    monkeypatch.setattr(sounddevice, 'query_hostapis', lambda *a, **k: [])
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *a: None)
    monkeypatch.setattr(cuda_detect, 'request_cuda_status', lambda *a: None)
    monkeypatch.setattr(thread_registry, 'spawn', lambda *a, **k: None)
    state = SimpleNamespace(status='off', port=47832, connection_details=lambda: None)
    monkeypatch.setattr(agents_section, 'for_app', lambda app: state)
    config = _schema_defaults()
    config['ui']['live_surface'] = {'enabled': True}
    win = settings_qt._SettingsWindow(_StubApp(config))
    win.show()
    qapp.processEvents()
    yield win
    # Qt can delete earlier queued windows too. Keep their Python wrappers
    # alive until Qt has finished, rather than leaving those deletes pending.
    pending_wrappers = QApplication.allWidgets()
    win.hide()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(win)
    qapp.processEvents()
    del pending_wrappers


def test_notice_is_lazy_one_per_page_and_dismiss_hides_all_pages(window):
    assert set(window._pages) == {'General'}
    assert len(window.findChildren(BetaNotice)) == 1
    window.show_tab('Dictation')
    window._pages['General'].findChild(BetaNotice).close_button.click()
    for name in settings_qt._TAB_NAMES:
        window.show_tab(name)
        page = window._pages[name]
        notices = page.findChildren(BetaNotice)
        assert len(notices) == 1 and notices[0].isHidden()
        assert page.widget().layout().itemAt(0).widget() is notices[0]
    for name in tools_page.TOOLS:
        window._select_tool(name)
        page = window._tool_pages[name]
        assert len(page.findChildren(BetaNotice)) == 1
        assert page.findChild(BetaNotice).isHidden()
        assert page.widget().layout().itemAt(0).widget() is page.findChild(BetaNotice)


def test_dismissal_resets_when_settings_reopens(window, qapp):
    old = window._pages['General'].findChild(BetaNotice)
    old.close_button.click()
    window.close()
    window.show()
    qapp.processEvents()
    new = window._pages['General'].findChild(BetaNotice)
    assert new is not old and not new.isHidden()


def test_notice_height_at_900_is_one_text_line_plus_margins(window, qapp):
    page = window._pages['General']
    window.resize(1300, 800)
    qapp.processEvents()
    notice = page.findChild(BetaNotice)
    assert notice.width() >= 900
    text_height = notice.message.heightForWidth(notice.message.width())
    assert text_height == notice.message.fontMetrics().height() + 2
    margins = notice.layout().contentsMargins()
    assert notice.height() == text_height + margins.top() + margins.bottom() + 2 * notice.frameWidth()


def test_links_land_on_named_lazy_row_without_changing_value(window, qapp):
    window._select_tool('Command library')
    controls = window._tool_pages['Command library'].findChild(agents_section.AgentsControls)
    before = controls.enabled.isChecked()
    link = next(label for label in controls.findChildren(QLabel) if 'href="agents"' in label.text())
    link.linkActivated.emit('agents')
    qapp.processEvents()
    assert QApplication.focusWidget() is controls.enabled
    assert controls.enabled.isChecked() is before
    assert controls.enabled.visibleRegion().boundingRect().height() > 0
    assert not window._widgets['_pack_scroll'].isAncestorOf(controls)
    assert 'Shortcuts' not in set(window._pages)
    key_link = next(label for label in controls.findChildren(QLabel) if 'href="command_hotkey"' in label.text())
    key_link.linkActivated.emit('command_hotkey')
    qapp.processEvents()
    row = window._rows_by_widget['command_hotkey']
    assert window._stack.currentWidget() is window._pages['Shortcuts']
    assert row.title == 'Command-only key'
    assert QApplication.focusWidget() is window._widgets['command_hotkey']
    assert row.visibleRegion().boundingRect().height() > 0
    assert 'text-decoration: none' in key_link.text()


def test_selected_route_only_is_visible_and_accessible(window, qapp):
    window._select_tool('Command library')
    controls = window._tool_pages['Command library'].findChild(agents_section.AgentsControls)
    qapp.processEvents()
    assert controls.code_panel.isVisible() and not controls.desktop_panel.isVisible()
    interface = QAccessible.queryAccessibleInterface(controls.desktop_copy)
    assert interface.state().invisible
    controls.app_selector.setCurrentIndex(1)
    qapp.processEvents()
    assert not controls.code_panel.isVisible() and controls.desktop_panel.isVisible()
    assert QAccessible.queryAccessibleInterface(controls.code_copy).state().invisible
    assert controls.command_preview.copy_button.text() == 'Copy'
    assert not controls.help.content.isVisible()
    controls.help.reveal()
    qapp.processEvents()
    assert controls.help_routes.panels[1].isVisible()
    assert not controls.help_routes.panels[0].isVisible()
