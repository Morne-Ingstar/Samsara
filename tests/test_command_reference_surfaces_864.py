"""864: compact scanning and one inert, accessible disclosure at a time."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtTest import QTest

from samsara import command_stats
from samsara.ui import command_cheatsheet_qt as cs, theme
from samsara.ui.main_window_qt import _MainWindow


@pytest.fixture(params=['dark', 'light'])
def surface(qapp, monkeypatch, tmp_path, request):
    previous = theme.active_theme()
    theme.set_theme(request.param, refresh=False)
    rows = [dict(phrase=f'command {i}', canonical_id=f'fixture.{i}', aliases=[f'alias {i}'],
                 display_aliases=[f'alias {i}'], description='A useful description. ' * 6,
                 examples=[f'command {i} example'], plugin='builtin', pack='core', args=[])
            for i in range(30)]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: rows)
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs, '_disabled_packs', lambda *a: set())
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    monkeypatch.setattr(_MainWindow, '_restore_geometry', lambda _: None)
    monkeypatch.setattr(_MainWindow, '_save_geometry', lambda _: None)
    monkeypatch.setattr(_MainWindow, '_activate', lambda *a: None)
    fake = SimpleNamespace(config={}, config_path=tmp_path / 'config.json', available_mics=[],
                           update_config_and_save=lambda _: True)
    host = _MainWindow(fake)
    host._poll_timer.stop()
    host._header_mark.stop()
    executed = []
    page = cs._CheatSheetWindow(executed.append, lambda: [], tmp_path / 'palette.json',
                                embedded=True, config_cb=lambda: {})
    host._stack.addWidget(page)
    host._stack.setCurrentWidget(page)
    host.resize(1100, 820)
    host.apply_theme()
    host.show()
    qapp.processEvents()
    yield host, page, executed
    host.hide()
    host.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(previous, refresh=False)


def test_collapsed_rows_are_single_line_and_names_remain_complete(surface):
    _, page, executed = surface
    view = page._list
    delegate = view.itemDelegate()
    assert delegate.expanded_phrase is None
    for i in range(view.count()):
        item = view.item(i)
        assert view.visualItemRect(item).height() == delegate.header_height()
        assert f'alias {i}' in item.text()
        assert 'A useful description.' in item.text()
        assert f'Say: command {i} example' in item.text()
    assert executed == []


def test_click_arrows_enter_and_escape_disclose_only_one(surface, qapp):
    _, page, executed = surface
    view, card = page._list, page._detail_card
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualItemRect(view.item(0)).center())
    qapp.processEvents()
    assert card.isVisible() and card.phrase == 'command 0'
    assert view.visualItemRect(view.item(0)).height() > view.itemDelegate().header_height()
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert card.isVisible() and card.phrase == 'command 1'
    assert view.visualItemRect(view.item(0)).height() == view.itemDelegate().header_height()
    assert view.visualItemRect(view.item(1)).height() > view.itemDelegate().header_height()
    assert view.visualItemRect(view.item(2)).top() > view.visualItemRect(view.item(1)).bottom()
    QTest.keyClick(view, Qt.Key.Key_Left)
    assert not card.isVisible()
    QTest.keyClick(view, Qt.Key.Key_Return)
    assert card.isVisible() and card.phrase == 'command 1'
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not card.isVisible() and executed == []


def test_filter_and_refresh_drop_expansion_and_bottom_remains_reachable(surface, qapp):
    _, page, _ = surface
    view = page._list
    page._on_item_clicked(view.item(0))
    page._filter.setText('alias 29')
    assert not page._detail_card.isVisible()
    assert view.count() == 1 and view.itemDelegate().expanded_phrase is None
    page._on_item_clicked(view.item(0))
    page.refresh_commands()
    assert not page._detail_card.isVisible()
    page._filter.clear()
    view.scrollToBottom()
    qapp.processEvents()
    rect = view.visualItemRect(view.item(view.count() - 1))
    assert 0 <= rect.top() <= rect.bottom() < view.viewport().height()


def test_theme_switch_repaints_existing_collection(surface, qapp):
    host, page, _ = surface
    old = theme.DATA_SURFACE
    theme.set_theme('light' if theme.active_theme() == 'dark' else 'dark')
    qapp.processEvents()
    assert theme.DATA_SURFACE != old
    assert theme.DATA_SURFACE in page._list.styleSheet()
    image = page._list.viewport().grab().toImage()
    assert image.pixelColor(8, 3) == theme.qcolor(theme.DATA_SURFACE)


def test_description_column_aligns_and_long_names_remain_available(surface, qapp):
    _, page, _ = surface
    # 864 FINISH IT 2: uneven names must not move the description column.
    page._all[0]['display_phrase'] = 'a very long command name ' * 12
    page._all[1]['display_phrase'] = 'Short'
    page._apply_filter('')
    view = page._list
    delegate = view.itemDelegate()
    for width in (380, 740):
        parts = [delegate.text_layout(view.model().index(i, 0), width)[0]
                 for i in range(view.count())]
        assert all(len(row) == 2 for row in parts)
        assert len({row[1][3].x() for row in parts}) == 1
        assert parts[0][0][3].width() <= (width - 32) * 2 // 5
        assert all(row[1][3].right() < width for row in parts)
    item = view.item(0)
    assert page._all[0]['phrase'] in item.toolTip()
    assert page._all[0]['display_phrase'].title() in item.text()
    page._filter.setText('alias 1')
    qapp.processEvents()
    assert delegate.text_layout(view.model().index(0, 0), 740)[0][0][3].width() < 200
