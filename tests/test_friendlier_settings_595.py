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
    # 875b / 888a: this fixture exercises the beta notice even in a stable release.
    monkeypatch.setattr('samsara.ui.widgets.beta_notice.__version__', '0.40.0-beta.test')
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
    retired = []
    refresh_visit = win._refresh_visit

    def refresh():
        # A revisit detaches these roots before queueing their deletion.
        retired.extend((win.centralWidget(), win._source_store, win._search_debounce))
        refresh_visit()

    monkeypatch.setattr(win, '_refresh_visit', refresh)
    win.show()
    qapp.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    # Never drain other tests' queued cleanup after their patches have ended.
    # Include our detached visit trees so they cannot leak into a later drain.
    for obj in (win, *retired):
        if isValid(obj):
            QCoreApplication.sendPostedEvents(obj, QEvent.Type.DeferredDelete)
    assert not isValid(win)
    assert all(not isValid(obj) for obj in retired)
    qapp.processEvents()


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
    assert window._tool_pages['Command library'].findChild(agents_section.AgentsControls) is None
    window._select_tool('Integrations')
    controls = window._tool_pages['Integrations'].findChild(agents_section.AgentsControls)
    before = controls.enabled.isChecked()
    # 827: Connect an AI app and its setting links now land under Integrations.
    window.focus_setting('agents')
    qapp.processEvents()
    assert QApplication.focusWidget() is controls.enabled
    assert controls.enabled.isChecked() is before
    assert controls.enabled.visibleRegion().boundingRect().height() > 0
    assert window._tool_name == 'Integrations'
    assert 'Shortcuts' not in set(window._pages)
    # 786: command-key navigation is an ordinary button instead of inline text.
    controls.command_key_button.click()
    qapp.processEvents()
    row = window._rows_by_widget['command_hotkey']
    assert window._stack.currentWidget() is window._pages['Shortcuts']
    assert row.title == 'Command-only key'
    assert QApplication.focusWidget() is window._widgets['command_hotkey']
    assert row.visibleRegion().boundingRect().height() > 0
    assert not any('<a ' in label.text() for label in controls.findChildren(QLabel))


def test_selected_route_only_is_visible_and_accessible(window, qapp):
    window._select_tool('Integrations')
    controls = window._tool_pages['Integrations'].findChild(agents_section.AgentsControls)
    # 858: inspect routes after opening the initially collapsed setup.
    assert not controls.details.toggle.isChecked()
    controls.details.toggle.click()
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
