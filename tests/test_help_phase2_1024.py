"""1024: one local recovery destination, with mic off and no browser handoff."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu, QPushButton

from samsara import help_terms
from samsara.ui import context_help_qt as help_ui, home_qt, qt_runtime, theme, window_activation
from samsara.ui.settings_qt import SettingsQt, _SettingsWindow
from tests._theme_stub_app import StubApp


def settle(qapp):
    for _ in range(8):
        qapp.processEvents()


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Local help must not open a URL or connect to the network')
    import socket
    monkeypatch.setattr(QDesktopServices, 'openUrl', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    monkeypatch.setattr(window_activation, 'surface_requested', lambda w: w.show())
    app = StubApp({'wake_word_enabled': False, 'command_mode': {'enabled': False},
                   'ui': {'live_surface': {'enabled': True}}})
    app.quick_opens = []
    app.open_quick_reference = lambda: app.quick_opens.append(True)
    app.open_settings = lambda: pytest.fail('Help must select Help directly, without opening General first')
    return app


@pytest.mark.parametrize('entry', ['shared', 'home-guide', 'Help', 'Help & Support', 'tray'])
def test_recovery_entries_select_the_same_help_destination(offline, monkeypatch, entry, qapp):
    selected = []
    monkeypatch.setattr(SettingsQt, '_show_preferences', lambda self, page='General': selected.append(page))
    if entry == 'shared':
        help_ui.open_help(offline)
    elif entry == 'home-guide':
        assert home_qt.open_guide(offline, 'Help && support')
    elif entry in ('Help', 'Help & Support'):
        assert home_qt.open_settings_tab(offline, entry)
    else:
        from samsara.ui.tray_actions import add_core_actions
        menu = QMenu()
        tray = SimpleNamespace(_app=offline, _restart_samsara=lambda: None)
        def add(menu, text, callback):
            action = menu.addAction(text)
            action.triggered.connect(callback)
            return action
        tray._add = add
        _, _, rescue, _, _ = add_core_actions(tray, menu)
        rescue.trigger()
    assert selected == ['Help']
    controller = offline._settings_qt
    help_ui.open_help(offline)
    assert offline._settings_qt is controller and selected == ['Help', 'Help']


def test_help_alias_cards_quick_reference_and_reopen(offline, qapp, qt_widget_scope):
    with qt_widget_scope(_SettingsWindow(offline)) as window:
        window.resize(1000, 760)
        for attempt in range(2):
            window.show_tab('Help & Support')
            settle(qapp)
            page = window._stack.currentWidget()
            assert page is window._pages['Help']
            # 1024 n10: retain master's feature definitions beside the added menu.
            words = page.findChild(help_ui.QLabel, 'featureWords')
            assert words is not None and words.isVisible()
            assert all(name in words.text() for name in ('Wake word', 'Hands-free', 'Hold to talk'))
            assert 'without holding a key' in help_terms.explanation('hands-free').body
            # 1024 n08: explanations live in the existing menu, no new Help rows.
            assert page.subtitle.text() == 'Set up, practise and recover when you need to.'
            assert [row.title for row in page.base_rows] == [
                'Documentation', 'Help and community', 'Setup / practice',
                'Support / problem', 'Private backup / restore', 'Updates']
            assert page._offline_help.active is None and page._help_card.isHidden()
            guide = window._widgets['setupGuides']
            assert [a.text() for a in guide.menu().actions()[:4]] == [
                'Interactive tutorial', 'Microphone setup', 'Ava setup', 'Manage profiles']
            assert set(page._help_actions) == set(help_terms.TERMS)
            for term_id, action in page._help_actions.items():
                assert action.text() == help_terms.explanation(term_id).title
                action.trigger()
                settle(qapp)
                assert page._help_card.term.id == term_id
                assert page._help_card.body.text() == help_terms.explanation(term_id, offline.config).body
                assert page._help_card.close_button.hasFocus()
                QTest.keyClick(page._help_card.close_button, Qt.Key.Key_Escape)
                assert guide.hasFocus() and page._offline_help.active is None
            guide.findChild(QAction, 'helpQuickReference').trigger()
            assert len(offline.quick_opens) == attempt + 1
            assert window._widgets['supportChoices'].menu().actions()
            assert window._widgets['diagnostics_write_jsonl'] is not None
            page._help_actions['wake-word'].trigger()
            page._help_actions['hands-free'].trigger()
            assert page._help_card.term.id == 'hands-free'
            window.close()
            assert page._offline_help.active is None


def test_f1_settings_tracks_page_and_focused_row_locally(offline, qapp, qt_widget_scope):
    with qt_widget_scope(_SettingsWindow(offline)) as window:
        window.resize(1000, 760)
        for name in ('General', 'Dictation', 'Audio', 'Ava', 'Shortcuts', 'Help'):
            window.show_tab(name)
            window.activateWindow()
            window._sidebar.setFocus()
            settle(qapp)
            page = window._stack.currentWidget()
            local = window._local_page_help
            assert local.controller.active is None and local.card.isHidden()
            if name == 'General':
                # 1024 n08: More info is explicit, blue and not underlined.
                link = page.subtitle
                assert 'More info</a>' in link.text()
                assert theme.ACCENT in link.text() and 'text-decoration:none' in link.text()
                assert not link.openExternalLinks()
                link.setFocus(Qt.FocusReason.TabFocusReason)
                settle(qapp)
                QTest.keyClick(link, Qt.Key.Key_Return)
                settle(qapp)
                assert local.controller.active is local.card
                assert local.card.heading.text() == name + ' help'
                assert page.heading.mapTo(page.viewport(), page.heading.rect().topLeft()).y() >= 0
                QTest.keyClick(local.card.close_button, Qt.Key.Key_Escape)
                assert link.hasFocus() and local.controller.active is None
                window._sidebar.setFocus()
            QTest.keyClick(window._sidebar, Qt.Key.Key_F1)
            settle(qapp)
            local = window._local_page_help
            assert local.controller.active is local.card
            assert local.card.heading.text() == name + ' help'
            assert local.card.guide_button.isHidden()
            assert window._stack.currentWidget() is page
            if name == 'General':
                # FINISH IT 1: compact copy, with space reserved below the title.
                card = local.card
                assert card.body.y() - card.heading.geometry().bottom() <= 16
                assert card.height() - card.close_button.geometry().bottom() <= 24
                title = page.heading.mapTo(window, page.heading.rect().bottomLeft())
                top = card.mapTo(window, card.rect().topLeft())
                assert top.y() > title.y()
                viewport_top = page.viewport().mapTo(window, page.viewport().rect().topLeft())
                assert title.y() >= viewport_top.y()
                first_row = page.base_rows[0]
                row_top = first_row.mapTo(window, first_row.rect().topLeft())
                card_bottom = card.mapTo(window, card.rect().bottomLeft())
                assert row_top.y() > card_bottom.y()
                window.resize(840, 760)
                settle(qapp)
                assert card.body.height() >= card.body.heightForWidth(card.body.width())
            QTest.keyClick(local.card.close_button, Qt.Key.Key_Escape)
            assert window._sidebar.hasFocus()
        window.show_tab('General')
        target = window._widgets['theme_combo']
        assert local.controller.active is None
        target.setFocus()
        settle(qapp)
        QTest.keyClick(target, Qt.Key.Key_F1)
        assert local.card.heading.text() == 'Theme'
        assert 'appearance' in local.card.body.text().lower()
        local.card.close_button.click()
        assert target.hasFocus()
        # F1 from a checkable setting must preserve both unsaved states.
        window.show_tab('Help')
        toggle = window._widgets['automatic_update_checks']
        for checked in (False, True):
            toggle.setChecked(checked)
            toggle.setFocus()
            settle(qapp)
            QTest.keyClick(toggle, Qt.Key.Key_F1)
            assert local.controller.active is local.card
            assert toggle.isChecked() is checked
            local.card.close_button.click()
            assert toggle.isChecked() is checked and toggle.hasFocus()
        window.show_tab('General')
        target.setFocus()
        QTest.keyClick(target, Qt.Key.Key_F1)
        window.show_tab('Help')
        assert local.controller.active is None
        window.close()
        window.show_tab('General')
        window.activateWindow()
        target = window._widgets['theme_combo']
        target.setFocus()
        settle(qapp)
        QTest.keyClick(target, Qt.Key.Key_F1)
        assert local.controller.active is local.card


def test_home_rescue_visible_and_hub_f1_preserves_page(offline, monkeypatch, tmp_path, qapp, qt_widget_scope):
    from samsara.ui.main_window_qt import _MainWindow
    monkeypatch.setattr(_MainWindow, '_restore_geometry', lambda _: None)
    monkeypatch.setattr(_MainWindow, '_save_geometry', lambda _: None)
    offline.config_path = tmp_path / 'config.json'
    offline.available_mics = []
    offline.update_config_and_save = lambda _: True
    with qt_widget_scope(_MainWindow(offline)) as host:
        host._poll_timer.stop()
        host._header_mark.stop()
        host.resize(1100, 820)
        host.apply_theme()
        host.show()
        host.activateWindow()
        settle(qapp)
        page = host._panel_cache['Home']
        # 1024 n08 removes the unapproved Home button, retaining the old test id.
        assert not hasattr(page, '_rescue_help')
        assert not any(b.text() == 'Something wrong?' and b.isVisible()
                       for b in page.findChildren(QPushButton))
        assert not page._guides_row.isVisible()
        assert page._destination_heading.text() == home_qt.WHERE_TO_GO.upper()
        assert host._local_page_help.controller.active is None
        origin = page._whats_new_button
        origin.setFocus()
        QTest.keyClick(origin, Qt.Key.Key_F1)
        settle(qapp)
        local = host._local_page_help
        assert local.card.heading.text() == 'Home help'
        assert local.card.close_button.hasFocus()
        assert host._stack.currentWidget() is page
        local.card.close_button.click()
        assert origin.hasFocus()


@pytest.mark.parametrize('embedded', [False, True])
def test_reference_f1_keeps_filter_selection_and_details(offline, monkeypatch, tmp_path, qapp, qt_widget_scope, embedded):
    from samsara import command_stats
    from samsara.ui import command_cheatsheet_qt as cs
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    monkeypatch.setattr(cs._CheatSheetWindow, '_foreground_screen', lambda _: None)
    rows = [dict(phrase='example command', canonical_id='fixture.one', aliases=[],
                 description='A command.', examples=[], plugin='builtin', pack='core', args=[])]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: rows)
    with qt_widget_scope(cs._CheatSheetWindow(lambda _: pytest.fail('Help executed a command'),
            lambda: [], tmp_path / 'palette.json', embedded=embedded)) as page:
        if embedded:
            # Use the production hub dispatch: embedded pages own no competing shortcut.
            from samsara.ui.main_window_qt import _MainWindow
            monkeypatch.setattr(_MainWindow, '_restore_geometry', lambda _: None)
            monkeypatch.setattr(_MainWindow, '_save_geometry', lambda _: None)
            offline.config_path = tmp_path / 'config.json'
            offline.available_mics = []
            host = _MainWindow(offline)
            host._poll_timer.stop()
            host._header_mark.stop()
            host._panel_cache['Commands'] = page
            host._stack.addWidget(page)
            host._stack.setCurrentWidget(page)
        else:
            host = page
        try:
            host.show()
            host.activateWindow()
            page._filter.setText('example')
            settle(qapp)
            page._list.setCurrentRow(0)
            page._on_item_clicked(page._list.item(0))
            page._filter.setFocus()
            settle(qapp)
            for _ in range(2):
                QTest.keyClick(page._filter, Qt.Key.Key_F1)
                settle(qapp)
                assert page._context_help.active is page._help_card
                assert page._help_card.term.id == 'command-reference'
                assert page._filter.text() == 'example'
                assert page._list.currentRow() == 0
                assert page._detail_card.phrase == 'example command'
                QTest.keyClick(page._help_card.close_button, Qt.Key.Key_Escape)
                assert page._filter.hasFocus()
            page.close()
            page.show()
            settle(qapp)
            assert page._context_help.active is None
        finally:
            if embedded:
                page.setParent(None)
                host.close()
                host.deleteLater()


def test_quick_reference_real_window_opens_refreshes_and_reopens_offline(offline, qapp, qt_widget_scope):
    from samsara.ui.quick_reference_qt import QuickReferenceQt
    quick = QuickReferenceQt(offline)
    quick.show()
    with qt_widget_scope(quick._window) as window:
        assert window.isVisible()
        window.close()
        assert not window.isVisible()
        quick.show()
        assert window.isVisible()
