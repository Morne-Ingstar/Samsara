"""Numbered window overview. Widgets and DWM resources stay on the Qt thread."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QSize
from PySide6.QtGui import QCursor, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QLabel, QPushButton, QScrollArea,
                               QCheckBox, QStyle, QSizePolicy)

from samsara.ui import theme, qt_runtime

log = logging.getLogger(__name__)


class _Properties(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD), ("destination", wintypes.RECT),
                ("source", wintypes.RECT), ("opacity", ctypes.c_ubyte),
                ("visible", wintypes.BOOL), ("client_only", wintypes.BOOL)]


class DwmThumbnails:
    """Own every registration, including failure paths. Handles are pointer sized."""

    def __init__(self, api=None):
        self.api = api if api is not None else ctypes.windll.dwmapi
        self.handles = {}
        self.sizes = {}
        if api is None:
            self.api.DwmRegisterThumbnail.argtypes = [wintypes.HWND, wintypes.HWND,
                                                      ctypes.POINTER(wintypes.HANDLE)]
            self.api.DwmRegisterThumbnail.restype = ctypes.c_long
            self.api.DwmUpdateThumbnailProperties.argtypes = [wintypes.HANDLE,
                                                             ctypes.POINTER(_Properties)]
            self.api.DwmUpdateThumbnailProperties.restype = ctypes.c_long
            self.api.DwmUnregisterThumbnail.argtypes = [wintypes.HANDLE]
            self.api.DwmUnregisterThumbnail.restype = ctypes.c_long
            self.api.DwmQueryThumbnailSourceSize.argtypes = [wintypes.HANDLE,
                                                            ctypes.POINTER(wintypes.SIZE)]
            self.api.DwmQueryThumbnailSourceSize.restype = ctypes.c_long

    def register(self, destination, source):
        if source in self.handles:
            return True
        handle = wintypes.HANDLE()
        if self.api.DwmRegisterThumbnail(destination, source, ctypes.byref(handle)) != 0:
            return False
        self.handles[source] = handle
        size = wintypes.SIZE()
        if self.api.DwmQueryThumbnailSourceSize(handle, ctypes.byref(size)) == 0:
            self.sizes[source] = (size.cx, size.cy)
        return True

    def update(self, source, rect, visible):
        handle = self.handles.get(source)
        if handle is None:
            return False
        props = _Properties()
        props.flags = 1 | 4 | 8 | 16  # destination, opacity, visible, client-only
        props.destination = wintypes.RECT(*rect)
        props.opacity, props.visible, props.client_only = 255, visible, False
        return self.api.DwmUpdateThumbnailProperties(handle, ctypes.byref(props)) == 0

    def close(self):
        count = len(self.handles)
        for source, handle in list(self.handles.items()):
            if self.api.DwmUnregisterThumbnail(handle) == 0:
                del self.handles[source]
                self.sizes.pop(source, None)
        log.info("Window overview thumbnails: registered=%d released=%d remaining=%d",
                 count, count - len(self.handles), len(self.handles))


def _plain(label, text):
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)


def _app_icon(hwnd):
    """Copy the window/class icon; native icon ownership stays with the source."""
    try:
        from PySide6.QtGui import QImage, QPixmap
        import win32gui
        # Bounded cross-process message: a hung app must not stall the overview.
        handle = win32gui.SendMessageTimeout(hwnd, 0x007F, 1, 0, 2, 20)
        handle = handle or win32gui.GetClassLong(hwnd, -14)
        if handle:
            return QIcon(QPixmap.fromImage(QImage.fromHICON(handle)))
    except Exception:
        log.debug("Window overview app icon unavailable", exc_info=True)
    return QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)


class OverviewWindow(QWidget):
    def __init__(self, rows, on_action, on_close, keep_open=False, thumbnails=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle("Samsara Window Overview")
        self.setAccessibleName("Window overview")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self._action, self._closed = on_action, on_close
        self._thumbnails = thumbnails if thumbnails is not None else DwmThumbnails()
        self.destroyed.connect(self._thumbnails.close)
        self._previews = []
        self._tiles = {}
        self._closing = False
        # Match the shared large-text convention, independently of OS DPI scaling.
        points = QApplication.font().pointSizeF()
        self._scale = max(1.0, points / 9.0) if points > 0 else 1.0
        self.setFont(theme.qfont(round(theme.TYPE_BODY * self._scale)))
        self.setStyleSheet(
            f"QWidget {{ color: {theme.TEXT_PRIMARY}; background: {theme.BG0}; }}"
            f"QPushButton {{ background: {theme.BG2}; border: 1px solid {theme.BORDER};"
            " border-radius: 6px; padding: 4px; }"
            f"QPushButton:hover {{ background: {theme.BG1}; }}"
            f"QWidget#overviewTile {{ border: 1px solid {theme.BORDER}; border-radius: 8px; }}"
            'QWidget#overviewTile[reading="true"] { border: 2px solid #F0F2F5; background: #1F242D; }'
        )
        root = QVBoxLayout(self)
        heading = QHBoxLayout()
        title = QLabel()
        _plain(title, f"{len(rows)} windows")
        title.setFont(theme.qfont(round(theme.TYPE_HEADING * self._scale)))
        heading.addWidget(title)
        heading.addStretch()
        self.keep = QCheckBox("Keep it open")
        self.keep.setFont(self.font())
        self.keep.setChecked(keep_open)
        self.keep.toggled.connect(lambda checked: self._action(0, "keep open", checked))
        heading.addWidget(self.keep)
        dismiss = QPushButton("Hide windows")
        dismiss.setFont(self.font())
        dismiss.clicked.connect(self.close)
        heading.addWidget(dismiss)
        root.addLayout(heading)
        hint = QLabel('Say “three left”, “two top right”, “close two”, or “cancel”.')
        hint.setFont(self.font())
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.status = QLabel("")
        self.status.setFont(self.font())
        self.status.setWordWrap(True)
        self.confirm = QPushButton("Confirm close")
        self.confirm.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.confirm.setFont(self.font())
        self.confirm.hide()
        self.confirm.clicked.connect(lambda: self._action(0, "confirm close", self.keep.isChecked()))
        root.addWidget(self.status)
        root.addWidget(self.confirm, alignment=Qt.AlignmentFlag.AlignLeft)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAccessibleName("Open windows and layout controls")
        host = QWidget()
        self.grid = QGridLayout(host)
        self.scroll.setWidget(host)
        root.addWidget(self.scroll)
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        width, height = max(320, area.width() - 48), max(240, area.height() - 48)
        columns = max(1, min(4, width // round(400 * self._scale)))
        self.resize(width, height)
        self.move(area.topLeft() + QPoint(24, 24))
        for index, row in enumerate(rows):
            tile = self._tile(row)
            self.grid.addWidget(tile, index // columns, index % columns)
        self.grid.setRowStretch((len(rows) + columns - 1) // columns, 1)
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_thumbnails)
        self.scroll.horizontalScrollBar().valueChanged.connect(self._update_thumbnails)
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.close)

    def _button(self, layout, text, number, action, row, column):
        button = QPushButton(text)
        button.setFont(self.font())
        button.setMinimumHeight(round(theme.HIT_TARGET_MIN * self._scale))
        button.setAccessibleName(f"Window {number}: {text}")
        button.clicked.connect(lambda: self._action(number, action, self.keep.isChecked()))
        layout.addWidget(button, row, column)
        return button

    def _tile(self, row):
        tile = QWidget()
        tile.setObjectName("overviewTile")
        tile.setProperty('reading', False)
        self._tiles[row['number']] = tile
        layout = QVBoxLayout(tile)
        title = QLabel()
        _plain(title, f"{row['number']}  {row['app']}")
        title.setWordWrap(True)
        title.setFont(theme.qfont(round(theme.TYPE_TITLE * self._scale)))
        layout.addWidget(title)
        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedHeight(round(100 * self._scale))
        preview.setAccessibleName(f"Preview of window {row['number']}")
        # Load a native icon only if DWM cannot provide a preview.
        self._previews.append((row['hwnd'], preview))
        layout.addWidget(preview)
        detail = QLabel()
        detail.setFont(self.font())
        _plain(detail, row['short_title'])
        detail.setWordWrap(True)
        layout.addWidget(detail)
        actions = QGridLayout()
        for index, (label, action) in enumerate([
            ("Go to", "go"), ("Left half", "left"), ("Right half", "right"),
            ("Maximize", "maximize"), ("Minimize", "minimize"), ("Next monitor", "next monitor"),
        ]):
            self._button(actions, label, row['number'], action, index // 3, index % 3)
        layout.addLayout(actions)
        quarters = QGridLayout()
        for index, (label, action) in enumerate([
            ("Top left", "top left"), ("Top right", "top right"),
            ("Bottom left", "bottom left"), ("Bottom right", "bottom right"),
        ]):
            self._button(quarters, label, row['number'], action, index // 2, index % 2)
        layout.addLayout(quarters)
        last = QGridLayout()
        self._button(last, "Cube", row['number'], "cube", 0, 0)
        self._button(last, "Close…", row['number'], "close", 0, 1)
        layout.addLayout(last)
        return tile

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._register_thumbnails)

    def _register_thumbnails(self):
        if not self.isVisible():
            return
        for hwnd, preview in self._previews:
            try:
                registered = self._thumbnails.register(int(self.winId()), hwnd)
            except Exception:
                registered = False
            if not registered:
                preview.setPixmap(_app_icon(hwnd).pixmap(QSize(64, 64)))
        self._update_thumbnails()

    def _update_thumbnails(self, *_):
        if not self.isVisible():
            return
        dpr = self.devicePixelRatioF()
        viewport = self.scroll.viewport()
        for hwnd, preview in self._previews:
            rect = QRect(preview.mapTo(self, QPoint()), preview.size())
            visible_area = QRect(viewport.mapTo(self, QPoint()), viewport.size())
            # Never paint thumbnails over the heading or adjacent controls.
            visible = visible_area.contains(rect)
            source_w, source_h = self._thumbnails.sizes.get(hwnd, (rect.width(), rect.height()))
            if source_w > 0 and source_h > 0:
                factor = min(rect.width() / source_w, rect.height() / source_h)
                fitted = QRect(0, 0, round(source_w * factor), round(source_h * factor))
                fitted.moveCenter(rect.center())
                rect = fitted
            coords = tuple(round(n * dpr) for n in (rect.left(), rect.top(),
                                                    rect.x() + rect.width(), rect.y() + rect.height()))
            if not self._thumbnails.update(hwnd, coords, visible) and hwnd in self._thumbnails.handles:
                preview.setPixmap(_app_icon(hwnd).pixmap(QSize(64, 64)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_previews"):
            QTimer.singleShot(0, self._update_thumbnails)

    def show_confirmation(self, text):
        _plain(self.status, text)
        self.confirm.setVisible(bool(text))

    def focus_reading(self, number):
        for value, tile in self._tiles.items():
            tile.setProperty('reading', value == number)
            tile.style().unpolish(tile)
            tile.style().polish(tile)
            tile.update()
        if number in self._tiles:
            self.scroll.ensureWidgetVisible(self._tiles[number])

    def hideEvent(self, event):
        self._thumbnails.close()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._thumbnails.close()
        if not self._closing:
            self._closing = True
            self._closed()
        super().closeEvent(event)


class OverviewPanel:
    def __init__(self):
        self.window = None
        self._generation = 0

    def show(self, rows, on_action, on_close, keep_open=False):
        self._generation += 1
        generation = self._generation
        started = time.perf_counter()

        def render():
            if generation != self._generation:
                return
            if self.window:
                self.window._closing = True
                self.window.close()
                self.window.deleteLater()
            self.window = OverviewWindow(rows, on_action, on_close, keep_open)
            self.window.show()
            log.info("Window overview visible: windows=%d elapsed_ms=%.1f",
                     len(rows), (time.perf_counter() - started) * 1000)

        qt_runtime.ensure_started()
        qt_runtime.post(render)

    def hide(self):
        self._generation += 1
        generation = self._generation
        def hide():
            if self.window and generation == self._generation:
                self.window._closing = True
                self.window.close()
        qt_runtime.post(hide)

    def confirmation(self, text):
        generation = self._generation
        def update():
            if self.window and generation == self._generation:
                self.window.show_confirmation(text)
        qt_runtime.post(update)

    def focus_reading(self, number):
        generation = self._generation
        def update():
            if self.window and generation == self._generation:
                self.window.focus_reading(number)
        qt_runtime.post(update)

    def keep_open(self, checked):
        def update():
            if self.window:
                self.window.keep.setChecked(checked)
        qt_runtime.post(update)


_panel = None


def get_panel():
    global _panel
    if _panel is None:
        _panel = OverviewPanel()
    return _panel
