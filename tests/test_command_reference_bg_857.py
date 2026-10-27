"""857: command-list gutters must blend into the painted command rows."""
import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from samsara import command_stats
from samsara.ui import command_cheatsheet_qt as sheet, theme
from samsara.ui.command_reference_surfaces import row_fill


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
        for position, hovered in (('top', False), ('bottom', False),
                                  ('top', True), ('bottom', True)):
            view.scrollToTop() if position == 'top' else view.scrollToBottom()
            qapp.processEvents()
            # 941: a hosted runner's pointer can hover a row. Pin the pointer
            # state rather than treating the intended hover fill as a defect.
            QApplication.sendEvent(view.viewport(), QEvent(QEvent.Type.Leave))
            view.clearSelection()
            hovered_row = -1
            if hovered:
                point = view.viewport().rect().center()
                hovered_row = view.indexAt(point).row()
                assert hovered_row >= 0
                QApplication.sendEvent(view.viewport(), QMouseEvent(
                    QEvent.Type.MouseMove, QPointF(point),
                    QPointF(view.viewport().mapToGlobal(point)),
                    Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier))
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
                # 864: gutters now live inside each full-width striped row.
                xs = (viewport.left() + 1, viewport.left() + 8,
                      viewport.right() - 1)
                pixels = [image.pixelColor(round(x * scale), round(y * scale))
                          for x in xs]
                assert pixels[0] == pixels[1] == pixels[2], (position, i, pixels)
                # Scroll fades composite the same wash across the entire row.
                point_y = y - viewport.top()
                fades = (win._scroll_edges.top, win._scroll_edges.bottom)
                if not any(fade.isVisible() and fade.geometry().top() <= point_y <= fade.geometry().bottom()
                           for fade in fades):
                    assert pixels[0] == theme.qcolor(row_fill(i, hovered=i == hovered_row)), (position, i, pixels)
                checked += 1
            assert checked >= 2
    finally:
        win.hide()
        win.deleteLater()
        qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        theme.set_theme(previous, refresh=False)
