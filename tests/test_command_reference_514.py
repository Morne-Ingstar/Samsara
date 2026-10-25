"""514: reading a command is inert; explicit Run uses the existing callback."""
import json

import pytest
from PySide6.QtCore import QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QAccessible
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from samsara import command_stats
from samsara.ui import command_cheatsheet_qt as cs, theme
from samsara.ui.command_reference_detail import NO_DESCRIPTION
from samsara.ui.settings.sidebar import SearchGlyph, search_qss


@pytest.fixture
def reference(qapp, tmp_path, monkeypatch, request):
    old_theme = theme.active_theme()
    # Layout variants construct only their own panel under the target palette.
    # A global refresh would also restyle windows retained by earlier files.
    palette = getattr(request.node, "callspec", None)
    palette = palette.params.get("palette") if palette is not None else None
    if palette is not None:
        theme.set_theme(palette, refresh=False)
    commands = [
        dict(phrase='open notepad', canonical_id='builtin.notepad', kind='builtin',
             aliases=['open notepad', 'start editor', 'launch editor', 'show editor', 'use editor'],
             description='Open the Notepad text editor.', args=[], plugin='builtin', pack='core'),
        dict(phrase='delete sample', canonical_id='sample.delete', kind='plugin',
             aliases=['delete sample'], description='', risk='destructive',
             args=[dict(name='target', type='text', required=True)], plugin='sample', pack='core'),
        dict(phrase='type sample', canonical_id='builtin.type', kind='builtin',
             aliases=['type sample'], description='Insert <sample> text.', args=[], plugin='builtin', pack='core'),
        dict(phrase='scoped sample', canonical_id='sample.scoped', kind='plugin',
             aliases=['scoped sample'], description='A scoped command.', args=[], plugin='sample', pack='core',
             scope=dict(apps=['synthetic.exe'])),
    ]
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: [dict(row) for row in commands])
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs, '_disabled_packs', lambda: set())
    monkeypatch.setattr(cs._CheatSheetWindow, '_foreground_screen', lambda _: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [('open notepad', 7)])
    executed = []
    win = cs._CheatSheetWindow(executed.append, lambda: [], tmp_path / 'palette.json')
    win.show()
    win.activateWindow()
    qapp.processEvents()
    yield win, executed, commands
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    theme.set_theme(old_theme, refresh=False)


def item(win, phrase='open notepad'):
    return next(win._list.item(i) for i in range(win._list.count())
                if win._list.item(i).data(Qt.ItemDataRole.UserRole) == phrase)


def click_row(win, phrase='open notepad'):
    target = item(win, phrase)
    # 864: the preceding selected row can now expand inside the list.
    win._list.scrollToItem(target)
    QApplication.processEvents()
    QTest.mouseClick(win._list.viewport(), Qt.MouseButton.LeftButton,
                     pos=win._list.visualItemRect(target).center())


@pytest.mark.parametrize('phrase', ['open notepad', 'delete sample', 'type sample'])
def test_read_then_explicit_run_uses_old_path_once(reference, phrase):
    win, executed, _ = reference
    click_row(win, phrase)
    assert win._detail_card.isVisible()
    assert win._detail_card.phrase == phrase
    assert executed == []
    QTest.mouseClick(win._detail_card.run_button, Qt.MouseButton.LeftButton)
    assert executed == [phrase]
    assert not win._detail_card.isVisible()
    win._detail_card.run()  # a stale activation cannot execute twice
    assert executed == [phrase]


def test_double_click_reads_without_execution(reference):
    win, executed, _ = reference
    target = item(win)
    click_row(win)  # a real double click begins with the first press/release
    QTest.mouseDClick(win._list.viewport(), Qt.MouseButton.LeftButton,
                     pos=win._list.visualItemRect(target).center())
    assert win._detail_card.isVisible()
    assert executed == []


def test_arrows_enter_tab_run_and_escape(reference, qapp):
    win, executed, _ = reference
    win._list.setCurrentRow(0)
    win._list.setFocus()
    QTest.keyClick(win._list, Qt.Key.Key_Down)
    assert win._list.currentRow() == 1 and executed == []
    QTest.keyClick(win._list, Qt.Key.Key_Return)
    assert win._detail_card.isVisible() and executed == []
    # 791: an argument command without a reviewed example has no Run it action.
    assert not win._detail_card.run_button.isVisible()
    QTest.keyClick(win._list, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is win._detail_card.close_button
    QTest.keyClick(win._detail_card.close_button, Qt.Key.Key_Escape)
    assert not win._detail_card.isVisible() and executed == []

    # A plain command still offers the keyboard Run it path.
    win._list.setCurrentRow(0)
    QTest.keyClick(win._list, Qt.Key.Key_Return)
    QTest.keyClick(win._list, Qt.Key.Key_Tab)
    assert QApplication.focusWidget() is win._detail_card.run_button
    phrase = win._list.currentItem().data(Qt.ItemDataRole.UserRole)
    QTest.keyClick(win._detail_card.run_button, Qt.Key.Key_Return)
    assert executed == [phrase]
    assert not win._detail_card.isVisible()
    assert QApplication.focusWidget() is win._list
    assert executed == [phrase]


def test_click_elsewhere_and_close_are_inert(reference):
    win, executed, _ = reference
    click_row(win)
    QTest.mouseClick(win._filter, Qt.MouseButton.LeftButton)
    assert not win._detail_card.isVisible()
    click_row(win)
    QTest.mouseClick(win._detail_card.close_button, Qt.MouseButton.LeftButton)
    assert not win._detail_card.isVisible() and executed == []
    assert QApplication.focusWidget() is win._list


def test_catalogue_details_plain_text_full_aliases_and_arguments(reference, monkeypatch):
    win, executed, _ = reference
    announcements = []
    monkeypatch.setattr(QAccessible, 'updateAccessibility', lambda event: announcements.append(event.message()))
    click_row(win)
    card = win._detail_card
    assert card.labels[0].text() == 'open notepad'
    assert 'use editor' in card.labels[1].text()
    assert card.labels[2].text() == 'Open the Notepad text editor.'
    interface = QAccessible.queryAccessibleInterface(card)
    assert interface.text(QAccessible.Text.Name) == 'Command details: open notepad'
    assert 'Open the Notepad text editor.' in interface.text(QAccessible.Text.Description)
    assert announcements and 'use editor' in announcements[-1]
    click_row(win, 'delete sample')
    assert card.labels[2].text() == NO_DESCRIPTION
    assert card.labels[3].text() == 'Arguments: target (text, required)'
    click_row(win, 'type sample')
    assert card.labels[2].text() == 'Insert <sample> text.'
    assert card.labels[2].textFormat() == Qt.TextFormat.PlainText
    assert executed == []


def test_search_category_live_filter_and_most_used_preserved(reference, qapp):
    win, executed, _ = reference
    assert win._list.count() == 3
    win._filter.setText('start editor')
    assert win._list.count() == 1
    assert not win._static_pane.isVisible()
    win._filter.clear()
    win._category_bar._live_only.setChecked(False)
    assert win._list.count() == 4
    win._category_bar._combo.setCurrentIndex(win._category_bar._pack_ids.index('sample'))
    assert win._list.count() == 2
    win._most_used_button.click()
    qapp.processEvents()
    row = next(row for row in win.findChildren(cs._StaticRow) if row.isVisibleTo(win))
    QTest.mouseClick(row, Qt.MouseButton.LeftButton)
    assert executed == [] and win._detail_card.phrase == 'open notepad'
    QTest.keyClick(row, Qt.Key.Key_Tab)
    QTest.keyClick(win._detail_card.run_button, Qt.Key.Key_Return)
    assert executed == ['open notepad']
    assert win._most_used_expanded
    assert json.loads(win._palette_path.read_text())['most_used_expanded']


def test_refresh_and_filter_close_card_without_retaining_item(reference):
    win, executed, _ = reference
    click_row(win)
    win._filter.setText('delete')
    assert not win._detail_card.isVisible() and win._detail_card.phrase is None
    click_row(win, 'delete sample')
    win.refresh_commands()
    assert not win._detail_card.isVisible() and executed == []


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('width', ['default', 'minimum'])
def test_grid_control_heights_dropdown_clearance_and_corner_grip(reference, qapp, palette, width):
    win, _, _ = reference
    assert theme.active_theme() == palette
    try:
        win.resize(cs._DEFAULT_W if width == 'default' else win.minimumWidth(), cs._DEFAULT_H)
        qapp.processEvents()
        widgets = [win._title_bar._title_label, win._filter, win._most_used_button,
                   win._category_bar._combo, win._list.viewport()]
        lefts = [widget.mapTo(win, QPoint()).x() for widget in widgets]
        assert max(lefts[i] for i in (1, 2, 4)) - min(lefts[i] for i in (1, 2, 4)) <= 1  # 842: shared content inset.
        assert lefts[0] == 1 + theme.SPACE_2
        # 842 FINISH IT 1: restore readable filter and close targets.
        for control in (win._filter, win._most_used_button, win._category_bar._combo,
                        win._category_bar._live_only, win._title_bar._close_button):
            assert control.height() == theme.HIT_TARGET_MIN
        assert win._title_bar._close_button.size().width() == theme.HIT_TARGET_MIN
        combo = win._category_bar._combo
        rect = QRect(combo.mapTo(win, QPoint()), combo.size())
        list_rect = QRect(win._list.mapTo(win, QPoint()), win._list.size())
        assert not rect.intersects(list_rect)
        assert all(win._list.visualItemRect(win._list.item(i)).height() >= theme.HIT_TARGET_MIN  # 842: wrapped rows grow.
                   for i in range(win._list.count()))
        grip = win._resize_grip
        assert grip.geometry().bottomRight() == grip.parentWidget().rect().bottomRight()
        # 864: collection wrapper keeps its inset outside row painting.
        assert win._list.mapTo(grip.parentWidget(), win._list.rect().bottomRight()).y() == grip.parentWidget().rect().bottom() - 12
    finally:
        win.hide()


def test_shared_search_and_scroll_treatments(reference, qapp):
    from samsara.ui.focus_ring import keyboard_stylesheet  # 515: the focus ring shows for keyboard focus only
    win, _, commands = reference
    assert keyboard_stylesheet(win._filter.styleSheet()) == keyboard_stylesheet(search_qss())
    assert win._filter.findChild(SearchGlyph)
    assert theme.SCROLLBAR_QSS in win._list.styleSheet()
    for index in range(30):
        row = dict(commands[0], phrase=f'synthetic {index}', canonical_id=f'sample.{index}')
        commands.append(row)
    win.refresh_commands()
    qapp.processEvents()
    bar = win._list.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum() // 2)
    qapp.processEvents()
    assert win._scroll_edges.top.isVisible() and win._scroll_edges.bottom.isVisible()
    assert win._scroll_edges.top.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    bar.setValue(bar.maximum())
    assert not win._scroll_edges.bottom.isVisible()


def test_card_fits_small_panel_and_scrolls_long_catalogue_text(reference, qapp):
    win, _, commands = reference
    commands[0]['description'] = 'Synthetic catalogue description. ' * 80
    win.refresh_commands()
    win.resize(win.minimumWidth(), 220)
    qapp.processEvents()
    click_row(win)
    qapp.processEvents()
    card = win._detail_card
    assert card.parentWidget().rect().contains(card.geometry())
    assert card.rect().contains(QRect(card.run_button.mapTo(card, QPoint()), card.run_button.size()))
    assert card.run_button.isVisibleTo(win)
    assert card.scroll.verticalScrollBar().maximum() > 0
