"""842: scan primary phrases and read every alias without clipping."""
import inspect

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFontInfo
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QCheckBox

from samsara import command_stats
from samsara.ui import command_cheatsheet_qt as cs, theme
from samsara.ui.command_reference_detail import CommandRowDelegate


def fixture_commands():
    rows = [dict(phrase=f'command {i:02}', canonical_id=f'fixture.{i}',
                 plugin='builtin', pack='core', kind='builtin', args=[],
                 aliases=[f'command {i:02}'], description='Synthetic command.',
                 shown_aliases=([f'alternate phrase {j} with several words for command {i}'
                                 for j in range(8)] if i in (0, 10, 19) else []))
            for i in range(20)]
    for row in rows:
        row["aliases"] += row["shown_aliases"]
    return rows


@pytest.fixture(params=['dark', 'light'])
def panel(qapp, tmp_path, monkeypatch, request):
    old = theme.active_theme()
    theme.set_theme(request.param, refresh=False)
    monkeypatch.setattr(cs, 'catalog_rows', lambda _: fixture_commands())
    monkeypatch.setattr(cs, '_scope_context', lambda: None)
    monkeypatch.setattr(cs, '_disabled_packs', lambda: set())
    monkeypatch.setattr(cs._CheatSheetWindow, '_foreground_screen', lambda _: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    win = cs._CheatSheetWindow(lambda _: None, lambda: [], tmp_path / 'palette.json')
    win.show()
    for _ in range(5):
        qapp.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    theme.set_theme(old, refresh=False)


def test_tiers_wrap_without_elision(panel):
    view = panel._list
    delegate = view.itemDelegate()
    index = view.model().index(0, 0)
    primary, aliases = delegate.tiers(index)
    assert primary == 'Command 00'
    assert 'alternate phrase 7' in aliases
    assert view.textElideMode() == Qt.TextElideMode.ElideNone
    # 864: scan text is one line; complete aliases wrap in selected details.
    assert 'elidedText' in inspect.getsource(CommandRowDelegate)
    (font1, colour1), (font2, colour2) = delegate.tier_styles()
    assert font1.pixelSize() == theme.TYPE_BODY and font2.pixelSize() == round(theme.TYPE_BODY * 0.875)
    assert QFontInfo(font1).pointSizeF() != QFontInfo(font2).pointSizeF()
    assert font1.weight() > font2.weight()
    assert colour1 == theme.TEXT_PRIMARY and colour2 == theme.DATA_TEXT_SECONDARY
    assert colour1 != colour2
    wide = delegate.text_layout(index, view.viewport().width())[1]
    narrow = delegate.text_layout(index, view.viewport().width() // 2)[1]
    assert narrow == wide == delegate.header_height()
    assert view.visualItemRect(view.item(0)).height() == wide
    panel._on_item_clicked(view.item(0))
    card = panel._detail_card
    assert 'alternate phrase 7' in card.labels[1].text()
    assert card.labels[1].wordWrap()
    assert view.visualItemRect(view.item(0)).height() > wide


def test_filter_band_and_native_corner(panel):
    search, category, live = panel._filter, panel._category_bar._combo, panel._category_bar._live_only
    assert isinstance(live, QCheckBox)
    assert category.width() == 140
    assert search.mapTo(panel, QPoint()).y() == category.mapTo(panel, QPoint()).y()
    assert live.mapTo(panel, QPoint()).y() == search.mapTo(panel, QPoint()).y()
    assert 'border-radius:4px' in live.styleSheet()
    # 842 FINISH IT 1: preserve the existing readability minimums.
    assert all(control.height() == theme.HIT_TARGET_MIN for control in (search, category, live))
    assert panel._title_bar._close_button.width() == theme.HIT_TARGET_MIN
    assert 'border:none' in panel._title_bar._close_button.styleSheet()
    assert panel._resize_grip.width() == 12


def test_last_long_row_fully_visible_at_scroll_end(panel, qapp):
    view = panel._list
    view.scrollToBottom()
    for _ in range(5):
        qapp.processEvents()
    rect = view.visualItemRect(view.item(view.count() - 1))
    assert rect.top() >= view.viewport().rect().top()
    assert rect.bottom() <= view.viewport().rect().bottom()
    assert view.mapTo(panel, QPoint(0, view.height())).y() == panel.height() - 13
    assert panel._resize_grip.geometry().top() > view.geometry().bottom()
    panel._filter.setText('alternate phrase 7')
    assert view.count() == 3
    assert panel._category_bar._live_only.isChecked()


def test_catalog_rows_keep_all_display_aliases(monkeypatch):
    record = dict(phrase='primary', aliases=['primary'] + [f'alias {i}' for i in range(8)],
                  canonical_id='fixture.all', plugin='builtin')
    monkeypatch.setattr(cs.command_catalog, 'guidance_catalog', lambda _: [record])
    row = cs.catalog_rows([])[0]
    assert len(row['shown_aliases']) == 3
    assert len(row['display_aliases']) == 8
    assert 'alias 7' in cs.row_text(row)
