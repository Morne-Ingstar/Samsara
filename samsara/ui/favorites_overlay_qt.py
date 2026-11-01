"""Numbered favorites HUD. All widget access belongs to the existing Qt loop."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QFileInfo, QRectF, Qt
from PySide6.QtGui import QFont, QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
                               QFileIconProvider, QPushButton, QScrollArea,
                               QSizeGrip, QSizePolicy)

from samsara.ui import theme, qt_runtime


@lru_cache(maxsize=256)
def _favorite_icon(target):
    """Qt-thread only: cache local shell icons; never fetch site/UNC icons."""
    if target.lower().startswith(('https://', 'http://')):
        return _site_icon()
    provider = QFileIconProvider()
    provider.setOptions(QFileIconProvider.Option.DontUseCustomDirectoryIcons)
    if not target.startswith(('\\\\', '//')):
        info = QFileInfo(target)
        if info.exists():
            icon = provider.icon(info)
            if not icon.isNull():
                return icon
    return provider.icon(QFileIconProvider.IconType.File)


@lru_cache(maxsize=1)
def _site_icon():
    # QFileIconProvider.Network is a drive icon on Windows; draw a local globe.
    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(theme.qcolor(theme.ACCENT), 3))
    painter.drawEllipse(QRectF(6, 6, 36, 36))
    painter.drawEllipse(QRectF(16, 6, 16, 36))
    painter.drawLine(6, 24, 42, 24)
    painter.end()
    return QIcon(pixmap)


class _ElidedLabel(QLabel):
    def __init__(self, text):
        super().__init__()
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setToolTip(text)
        self.setAccessibleName(text)

    def resizeEvent(self, event):
        self.setText(self.fontMetrics().elidedText(
            self.full_text, Qt.TextElideMode.ElideRight, max(0, self.width())))
        super().resizeEvent(event)


class FavoritesWindow(QWidget):
    # PLACEHOLDER COPY (958): headings, empty state, controls and voice hints.
    def __init__(self, rows, on_pick, on_close, settings=None, on_settings=None):
        super().__init__(None, Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setObjectName('favoritesHud')
        self.setFont(theme.qfont(14))
        self.setWindowTitle('Favorites')
        self.setAccessibleName('Numbered favorites')
        self.on_pick, self.on_close = on_pick, on_close
        self.on_settings = on_settings or (lambda values: None)
        settings = settings or {}
        self.font_size = max(14, min(32, int(settings.get('font_size', 18))))
        self.setMinimumSize(480, 320)
        self.resize(max(480, min(1600, int(settings.get('width', 760)))),
                    max(320, min(1200, int(settings.get('height', 760)))))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 12)
        layout.setSpacing(14)
        header = QHBoxLayout()
        self.heading = QLabel('Favorites')
        self.heading.setFont(theme.qfont(26, weight=QFont.Weight.Bold))
        header.addWidget(self.heading)
        header.addStretch()
        for text, name, callback in (
                ('A−', 'Smaller favorites text', lambda: self.set_font_size(self.font_size - 2)),
                ('A+', 'Larger favorites text', lambda: self.set_font_size(self.font_size + 2)),
                ('Close', 'Close favorites', self.close)):
            button = QPushButton(text)
            button.setFont(theme.qfont(14))
            button.setAccessibleName(name)
            button.setMinimumSize(48, 44)
            button.clicked.connect(callback)
            header.addWidget(button)
        layout.addLayout(header)
        self.hint = QLabel('Say a number to open · “hide favorites” to close')
        self.hint.setFont(theme.qfont(14))
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.host = QWidget()
        self.host.setObjectName('favoritesHost')
        self.cards = QVBoxLayout(self.host)
        self.cards.setContentsMargins(0, 0, 8, 0)
        self.cards.setSpacing(6)
        self.scroll.setWidget(self.host)
        layout.addWidget(self.scroll, 1)
        self.status = QLabel()
        self.status.setFont(theme.qfont(14))
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.count = QLabel()
        self.count.setFont(theme.qfont(14))
        self.count.setWordWrap(True)
        footer.addWidget(self.count)
        footer.addStretch()
        footer.addWidget(QSizeGrip(self))
        layout.addLayout(footer)
        self.shortcuts = [QShortcut(QKeySequence('Escape'), self)]
        self.shortcuts[0].activated.connect(self.close)
        self.rows = []
        self.set_rows(rows)
        self.apply_theme()
        theme.theme_change_signal().changed.connect(self.apply_theme)

    def apply_theme(self):
        self.setStyleSheet(
            f'QWidget#favoritesHud {{ background: {theme.BG0}; border: 1px solid {theme.BORDER}; }}'
            f'QLabel {{ color: {theme.TEXT_PRIMARY}; background: transparent; border: none; }}'
            f'QWidget#favoriteRow {{ background: {theme.BG1}; border: none; border-radius: 6px; }}'
            f'QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 4px 10px; }}'
            f'QPushButton:hover, QPushButton:focus {{ background: {theme.ACCENT_DIM}; border-color: {theme.ACCENT}; }}'
            f'QScrollArea {{ background: {theme.BG0}; border: none; }}')
        self.host.setStyleSheet(f'QWidget#favoritesHost {{ background: {theme.BG0}; }}')
        for label in (self.hint, self.count):
            label.setStyleSheet(f'color: {theme.TEXT_SECONDARY};')
        for badge in self.badges:
            badge.setStyleSheet(f'background: {theme.ACCENT}; color: {theme.TEXT_ON_ACCENT}; border-radius: 6px;')
        for target in self.targets:
            target.setStyleSheet(f'color: {theme.TEXT_SECONDARY};')

    def set_rows(self, rows):
        self.rows = list(rows)
        while self.cards.count():
            item = self.cards.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.badges, self.icons, self.names, self.targets, self.buttons = [], [], [], [], []
        if not rows:
            empty = QLabel('Your quick list is empty.\n\nSay “add this to favorites” in Explorer or your browser,\nor “add example.com to quick list”.')
            empty.setWordWrap(True)
            empty.setFont(theme.qfont(self.font_size))
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.cards.addWidget(empty, 1)
        for number, row in enumerate(rows, 1):
            card = QWidget()
            card.setObjectName('favoriteRow')
            card.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            line = QHBoxLayout(card)
            line.setContentsMargins(10, 8, 10, 8)
            line.setSpacing(10)
            badge = QLabel(str(number))
            badge.setFont(theme.qfont(self.font_size, weight=QFont.Weight.Bold))
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedSize(max(44, self.font_size * 2), max(44, self.font_size * 2))
            self.badges.append(badge)
            line.addWidget(badge)
            icon = QLabel()
            icon_size = max(24, self.font_size)
            icon.setFixedSize(icon_size, icon_size)
            icon.setPixmap(_favorite_icon(row['target']).pixmap(icon_size, icon_size))
            self.icons.append(icon)
            line.addWidget(icon)
            name = _ElidedLabel(row['name'])
            name.setFont(theme.qfont(self.font_size, weight=QFont.Weight.DemiBold))
            self.names.append(name)
            line.addWidget(name, 1)
            target = _ElidedLabel(row['target'])
            target.setFont(theme.qfont(max(14, self.font_size - 4)))
            self.targets.append(target)
            line.addWidget(target, 1)
            button = QPushButton('Open')
            button.setFont(theme.qfont(max(14, self.font_size - 4)))
            button.setMinimumHeight(max(44, self.font_size + 20))
            button.setAccessibleName(f"Open favorite {number}: {row['name']}")
            button.setToolTip(f"{row['name']}\n{row['target']}")
            button.clicked.connect(lambda checked=False, n=number: self.on_pick(n))
            self.buttons.append(button)
            line.addWidget(button)
            self.cards.addWidget(card)
        self.cards.addStretch()
        self.count.setText(f'{len(rows)} favorites · Drag the top edge to move; corner to resize')
        self.status.setText('')
        self.apply_theme()

    def set_font_size(self, size):
        self.font_size = max(14, min(32, int(size)))
        self.set_rows(self.rows)
        self.save_settings()

    def save_settings(self):
        self.on_settings(dict(font_size=self.font_size, width=self.width(), height=self.height()))

    def closeEvent(self, event):
        self.save_settings()
        self.on_close()
        super().closeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 24:
            handle = self.windowHandle()
            if handle:
                handle.startSystemMove()
        super().mousePressEvent(event)


class FavoritesPanel:
    """Generation checks prevent late render/update jobs from reopening a closed HUD."""
    def __init__(self):
        self.window = None
        self.generation = 0

    def show(self, rows, on_pick, on_close, settings=None, on_settings=None):
        self.generation += 1
        generation = self.generation
        def render():
            if generation != self.generation:
                return
            if self.window:
                self.window.hide()
                self.window.deleteLater()
            self.window = FavoritesWindow(rows, on_pick, on_close, settings, on_settings)
            screen = self.window.screen().availableGeometry()
            self.window.resize(min(self.window.width(), screen.width()), min(self.window.height(), screen.height()))
            self.window.move(screen.center() - self.window.rect().center())
            self.window.show()
        qt_runtime.post(render)

    def post(self, callback):
        generation = self.generation
        def update():
            if self.window and generation == self.generation:
                callback(self.window)
        qt_runtime.post(update)

    def hide(self):
        self.generation += 1
        self.post(lambda window: window.close())


_panel = None


def get_panel():
    global _panel
    if _panel is None:
        _panel = FavoritesPanel()
    return _panel
