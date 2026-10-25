"""857: command-list gutters must blend into the painted command rows."""
import pytest
from PySide6.QtCore import QEvent, Qt

from samsara import command_stats
from samsara.ui import command_cheatsheet_qt as sheet, theme


@pytest.mark.parametrize('palette', ['dark', 'light'])
@pytest.mark.parametrize('embedded', [False, True], ids=['floating', 'hub'])
def test_left_and_right_gutters_match_row_background(
    qapp, tmp_path, monkeypatch, palette, embedded,
):
    previous = theme.active_theme()
    theme.set_theme(palette, refresh=False)
    commands = [dict(phrase=f'sample command {i}', canonical_id=f'fixture.{i}',
                     plugin='builtin', kind='builtin', args=[],
                     pack='core', aliases=[], description='Sample description.')
                for i in range(20)]
    monkeypatch.setattr(sheet, 'catalog_rows', lambda _: commands)
    monkeypatch.setattr(sheet, '_scope_context', lambda: None)
    monkeypatch.setattr(sheet, '_disabled_packs', lambda: set())
    monkeypatch.setattr(sheet._CheatSheetWindow, '_foreground_screen', lambda _: None)
    monkeypatch.setattr(command_stats, 'get_top_commands', lambda _: [])
    win = sheet._CheatSheetWindow(lambda _: None, lambda: [],
                                  tmp_path / 'palette.json', embedded=embedded)
    try:
        win.show()
        view = win._list
        # Check rows at both ends: this catches the gutter even after scrolling.
        for position in ('top', 'bottom'):
            view.scrollToTop() if position == 'top' else view.scrollToBottom()
            qapp.processEvents()
            image = view.grab().toImage()
            scale = image.devicePixelRatio()
            viewport = view.viewport().geometry()
            checked = 0
            for i in range(view.count()):
                rect = view.visualItemRect(view.item(i))
                y = rect.top() + 3  # Above text and below the preceding divider.
                if not 0 <= y < viewport.height():
                    continue
                y += viewport.top()
                xs = (viewport.left() - 2, viewport.left() + 2,
                      viewport.right() + 2)
                pixels = [image.pixelColor(round(x * scale), round(y * scale))
                          for x in xs]
                assert pixels == [theme.qcolor(theme.BG0)] * 3, (position, i, pixels)
                checked += 1
            assert checked >= 2
    finally:
        win.hide()
        win.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        theme.set_theme(previous, refresh=False)
