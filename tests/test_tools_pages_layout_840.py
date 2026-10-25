"""Tools layout in the real Settings host; no app runtime or private data."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QTableWidget, QWidget
import pytest


@pytest.fixture
def hermes_installed():
    return False


@pytest.fixture(params=('dark', 'light'))
def tools(request, qapp, monkeypatch, qt_widget_scope, hermes_installed):
    from samsara.ui import theme
    from samsara.ui.first_run_qt import ComponentsPage
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults

    monkeypatch.setattr('samsara.autostart.sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr('samsara.ui.settings.hermes_section.installed', lambda: hermes_installed)
    monkeypatch.setattr('samsara.health_store.get_today', lambda: [])
    monkeypatch.setattr('samsara.health_store.get_pain_average', lambda **k: None)
    monkeypatch.setattr('samsara.health_store.get_by_type', lambda *a, **k: [])
    theme.set_theme(request.param, refresh=False)
    config = _schema_defaults()
    config['alarms'] = {'enabled': True, 'items': []}
    config['music_library'] = {}
    window = _SettingsWindow(_StubApp(config), tool="Alarms")
    with qt_widget_scope(window):
        window.resize(1160, 900)
        window.show()
        yield window
    theme.set_theme('dark', refresh=False)


def reveal(window, name, qapp):
    window._select_tool(name)
    # Navigation styles each newly materialized page through the real host.
    for _ in range(12):
        qapp.processEvents()
    return window._tool_pages[name]


def test_real_tools_build_with_capped_tables_and_left_actions(tools, qapp):
    import samsara.ui.settings

    for name in ('Alarms', 'Health', 'Music', 'Command library'):
        page = reveal(tools, name, qapp)
        for table in page.findChildren(QTableWidget):
            assert table.maximumWidth() <= 640
            assert table.width() == 640
        for row in page.findChildren(QHBoxLayout):
            items = [row.itemAt(i) for i in range(row.count())]
            buttons = [i for i, item in enumerate(items) if isinstance(item.widget(), QPushButton)]
            if buttons:
                assert not any(item.spacerItem() and row.stretch(i) > 0
                               for i, item in enumerate(items[:max(buttons)]))
        for button in page.findChildren(QPushButton):
            if button.text() in ('Refresh', 'Export to CSV', 'Open folder', 'Add Alarm',
                                  'Edit', 'Toggle', 'Test', 'Delete', 'Add', 'Reload'):
                assert button.sizePolicy().horizontalPolicy().name == 'Maximum'
                assert button.width() < 250


def test_empty_messages_follow_data_transitions_without_fake_records(tools, qapp, monkeypatch):
    for name, key, expected in (
        ('Alarms', 'alarms_table', 'No alarms yet.'),
        ('Health', None, 'No entries today\nNo pain logged today.'),
        ('Music', 'music_library_table', 'No music saved yet.'),
    ):
        reveal(tools, name, qapp)
        table = tools._widgets[key] if key else tools._health_log_table
        label = table.findChild(QLabel, 'toolsTableEmptyState')
        assert label and label.text() == expected and not label.isHidden()
        assert table.rowCount() == 0
        assert table.height() < 190
        assert label.alignment() == Qt.AlignmentFlag.AlignCenter
        assert label.height() >= label.sizeHint().height()

    tools.app.config['alarms']['items'] = [
        {'id': 'water', 'name': 'Drink water', 'interval_minutes': 60, 'enabled': True},
        {'id': 'stretch', 'name': 'Stretch', 'interval_minutes': 90, 'enabled': False},
    ]
    tools._populate_alarms_table(tools._widgets['alarms_table'])
    table = tools._widgets['alarms_table']
    assert table.rowCount() == 2 and table._empty_state_label.isHidden()
    assert [table.columnWidth(i) for i in (0, 2, 3, 4)] == [56, 112, 128, 104]
    tools.app.config['alarms']['items'] = []
    tools._populate_alarms_table(table)
    assert not table._empty_state_label.isHidden()

    tools._music_library = {'Morning': {'type': 'playlist', 'uri': 'spotify:playlist:123'}}
    tools._refresh_music_library_table()
    table = tools._widgets['music_library_table']
    assert table.rowCount() == 1 and table._empty_state_label.isHidden()
    tools._delete_music_library_entry('Morning')
    assert table.rowCount() == 0 and not table._empty_state_label.isHidden()

    monkeypatch.setattr('samsara.health_store.get_today', lambda: [
        {'type': 'pain', 'timestamp': '2026-10-24T12:00:00Z', 'data': {'level': 2}},
    ])
    tools._refresh_health_log()
    assert tools._health_log_table.rowCount() == 1
    assert tools._health_log_table._empty_state_label.isHidden()


@pytest.mark.parametrize('hermes_installed', (False, True))
def test_packs_use_page_scroll_and_spotify_button_uses_existing_picker(tools, qapp, monkeypatch, hermes_installed):
    page = reveal(tools, 'Command library', qapp)
    assert isinstance(page, QScrollArea)
    assert not page.findChildren(QScrollArea)
    packs = tools._widgets['_pack_scroll']
    assert packs.maximumWidth() == 640
    assert packs.width() == 640
    # Check the actual wrapped labels; an unconstrained sizeHint can describe
    # a narrower width than the page ultimately gives this flow container.
    for label in packs.findChildren(QLabel):
        if label.objectName().startswith('commandPackDescription_') and label.isVisibleTo(packs):
            assert label.height() >= label.heightForWidth(label.width())
            assert label.parentWidget().geometry().bottom() <= packs.height()
    # 840: Hermes shares the save registry but has a separate consent form.
    checkboxes = tools._widgets['_pack_checkboxes']
    assert ('hermes' in checkboxes) == hermes_installed
    for pack_id, cb in checkboxes.items():
        row = cb.parentWidget()
        if pack_id == 'hermes':
            assert not packs.isAncestorOf(cb)
            assert row.layout().contentsMargins().top() == 0
        else:
            assert packs.isAncestorOf(cb), pack_id
            assert row.property('dataListRow'), pack_id
            assert row.layout().contentsMargins().top() == 12, pack_id
    reveal(tools, 'Music', qapp)
    calls = []
    monkeypatch.setattr(tools, '_choose_spotify_export_path', lambda: 'fixture.zip')
    monkeypatch.setattr(tools, '_import_spotify_export', calls.append)
    button = tools._widgets['music_library_spotify_import']
    help_label = tools._widgets['music_library_spotify_help']
    assert button.y() >= help_label.y() + help_label.height()
    button.click()
    assert calls == ['fixture.zip']


def _pixel(widget, x, y):
    image = widget.grab().toImage()
    ratio = image.devicePixelRatio()
    return image.pixelColor(int(x * ratio), int(y * ratio)).name()


def _hover(widget, x, y, qapp):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    # 840: offscreen QTest.mouseMove can omit motion at an unchanged global
    # cursor position after a refresh has reset the table's hover state.
    point = QPointF(x, y)
    qapp.sendEvent(widget, QMouseEvent(
        QEvent.Type.MouseMove, point, QPointF(widget.mapToGlobal(point.toPoint())),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    qapp.processEvents()


def test_filled_panels_geometry_and_record_states(tools, qapp, monkeypatch):
    from samsara.ui import theme

    for name in ('Alarms', 'Health', 'Music', 'Command library'):
        page = reveal(tools, name, qapp)
        table = page.findChild(QTableWidget)
        assert table.height() == 152
        assert table.viewport().height() == 96
        assert table.horizontalHeader().height() == 40
        assert table.showGrid() is False and table.alternatingRowColors()
        assert _pixel(table, 12, 12) == theme.DATA_SURFACE
        assert _pixel(table.viewport(), 4, 20) == theme.DATA_SURFACE
        assert table._empty_state_label.height() >= table._empty_state_label.sizeHint().height()

    reveal(tools, 'Alarms', qapp)
    table = tools._widgets['alarms_table']
    for count in (1, 2, 7, 0):
        tools.app.config['alarms']['items'] = [
            {'id': str(i), 'name': 'A long alarm name ' * 8,
             'interval_minutes': 60, 'enabled': True} for i in range(count)
        ]
        tools._populate_alarms_table(table)
        qapp.processEvents()
        # Refresh deliberately preserves selection; sample base fill after clearing it.
        table.clearSelection()
        table._hover_row = -1
        qapp.processEvents()
        assert table.height() == (56 + 48 * min(count, 5) if count else 152)
        if count:
            assert table.rowHeight(0) == 48
            assert table.item(0, 1).toolTip() == table.item(0, 1).text()
            assert _pixel(table.viewport(), 5, 24) == theme.DATA_SURFACE
        if count >= 2:
            assert _pixel(table.viewport(), 5, 72) == theme.DATA_ROW_ALT
            table.selectRow(0)
            table.setFocus()
            qapp.processEvents()
            assert _pixel(table.viewport(), 5, 24) == theme.DATA_ROW_SELECTED
            assert _pixel(table.viewport(), 1, 24) == theme.ACCENT
            assert _pixel(table.viewport(), 20, 1) == theme.ACCENT
            _hover(table.viewport(), 5, 72, qapp)
            assert table._hover_row == 1
            assert _pixel(table.viewport(), 5, 72) == theme.DATA_ROW_HOVER
            tools._search_edit.setFocus()
            qapp.processEvents()
            assert _pixel(table.viewport(), 5, 24) == theme.DATA_ROW_SELECTED
            table.clearSelection()
            _hover(tools._search_edit, 3, 3, qapp)
            qapp.processEvents()

    reveal(tools, 'Music', qapp)
    tools._music_library = {'Morning': {'type': 'playlist', 'uri': 'spotify:playlist:sample'}}
    tools._refresh_music_library_table()
    table = tools._widgets['music_library_table']
    assert table.height() == 104
    assert table.selectionMode() == QTableWidget.SelectionMode.NoSelection
    assert table.cellWidget(0, 1).property('dataSecondary')
    delete = table.cellWidget(0, 3).findChild(QPushButton)
    assert delete and delete.height() >= 44 and delete.width() <= 80
    assert 'border-radius: 8px' in delete.styleSheet()
    assert _pixel(table.viewport(), 5, 24) == theme.DATA_SURFACE
    tools._music_library['Evening'] = {'type': 'album', 'uri': 'spotify:album:sample'}
    tools._refresh_music_library_table()
    qapp.processEvents()
    assert _pixel(table.viewport(), 506, 72) == theme.DATA_ROW_ALT
    _hover(table.viewport(), 5, 72, qapp)
    assert table._hover_row == 1
    assert _pixel(table.viewport(), 506, 72) == theme.DATA_ROW_HOVER


def test_data_theme_switch_and_narrow_editor(tools, qapp):
    from samsara.ui import theme
    from samsara.ui.settings.music_qt import MusicEditor

    page = reveal(tools, 'Music', qapp)
    table = tools._widgets['music_library_table']
    previous = theme.active_theme()
    theme.set_theme('light' if previous == 'dark' else 'dark', refresh=False)
    tools.apply_theme()
    qapp.processEvents()
    assert _pixel(table, 12, 12) == theme.DATA_SURFACE
    assert _pixel(table.viewport(), 4, 20) == theme.DATA_SURFACE
    heading = next(label for label in page.findChildren(QLabel) if label.text() == 'Music library')
    title = next(label for label in page.findChildren(QLabel) if label.text() == 'Music')
    assert heading.y() - title.geometry().bottom() - 1 == 32
    editor = page.findChild(MusicEditor)
    assert tools._widgets['music_library_name'].width() == 240
    assert tools._widgets['music_library_link'].width() == 280
    tools.setMinimumWidth(0)
    tools.resize(850, 900)
    for _ in range(8):
        qapp.processEvents()
    assert editor._narrow
    assert editor.grid.getItemPosition(editor.grid.indexOf(tools._widgets['music_library_link']))[0] == 1
    assert table.width() <= page.viewport().width()
    assert table.horizontalScrollBar().maximum() > 0
    assert table.columnWidth(1) == 88 and table.columnWidth(3) == 104
    theme.set_theme(previous, refresh=False)


def test_commands_empty_search_and_pack_surface(tools, qapp, monkeypatch):
    from samsara.ui import theme
    import samsara.ui.settings.commands_qt as commands

    page = reveal(tools, 'Command library', qapp)
    table = tools._widgets['cmd_table']
    monkeypatch.setattr(commands, '_collect_command_rows', lambda _: [
        dict(phrase='sample phrase', source='builtin', type='text', pack='core',
             aliases=[], description='A long description ' * 20, data={'type': 'text', 'text': 'sample'})])
    tools._populate_commands_table(table)
    assert table.rowCount() == 1 and table.height() == 104
    assert table.item(0, 3).toolTip() == 'A long description ' * 20
    tools._filter_commands(table, 'no matching fixture')
    assert table.rowCount() == 0 and table.height() == 152
    assert table._empty_state_label.text() == 'No commands match your search.'
    monkeypatch.setattr(commands, '_collect_command_rows', lambda _: [])
    tools._filter_commands(table, '')
    assert table._empty_state_label.text() == 'No commands loaded.'
    packs = tools._widgets['_pack_scroll']
    assert packs.property('settingsDataList')
    assert _pixel(packs, 12, 12) == theme.DATA_SURFACE
    rows = [packs.layout().itemAt(i).widget() for i in range(packs.layout().count())]
    rows = [row for row in rows if not row.isHidden()]
    assert all(row.height() >= 72 for row in rows)
    assert not packs.findChildren(QWidget, 'settingsRowHairline')
    for i, row in enumerate(rows[:2]):
        assert _pixel(row, 2, 2) == (theme.DATA_ROW_ALT if i % 2 else theme.DATA_SURFACE)
