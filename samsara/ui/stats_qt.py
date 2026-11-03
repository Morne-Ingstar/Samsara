"""Local usage page and the Home entry button (1056)."""
# PLACEHOLDER COPY (1056): ordinary page labels and metric explanations.
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)
from samsara.ui import theme
from samsara.usage_stats import read_stats


class StatsCard(QPushButton):
    def __init__(self):
        super().__init__()
        self.setObjectName('homeCard')
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)

    def bind(self, navigate):
        self.setAccessibleName('Words today - open stats')
        self.setToolTip('Open stats')
        self.clicked.connect(lambda: navigate('Stats'))
        self.setStyleSheet(f'''
            QPushButton#homeCard {{ background: {theme.DATA_SURFACE};
                border: 1px solid transparent; border-radius: 10px; padding: 0; }}
            QPushButton#homeCard:hover, QPushButton#homeCard:focus {{ border-color: {theme.ACCENT}; }}
        ''')
        self._hint = QLabel('Stats', self)
        self._hint.setStyleSheet(f'color: {theme.ACCENT}; background: transparent; font-size: 12px;')
        self._hint.setAttribute(Qt.WA_TransparentForMouseEvents)
        for label in self.findChildren(QLabel):
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._hint.hide()

    def _update_hint(self):
        if hasattr(self, '_hint'):
            self._hint.adjustSize()
            self._hint.move(self.width() - self._hint.width() - 10, 6)
            self._hint.setVisible(self.underMouse() or self.hasFocus())

    def enterEvent(self, event):
        super().enterEvent(event)
        self._update_hint()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._update_hint()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._update_hint()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._update_hint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_hint()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.click()
            event.accept()
        else:
            super().keyPressEvent(event)


class DailyBars(QWidget):
    def __init__(self, days):
        super().__init__()
        self.days = days
        self.setMinimumHeight(110)
        self.setAccessibleName('Words per day, last 30 days')
        self.setAccessibleDescription('; '.join(f'{d:%d %b}: {v:,} words' for d, v in days))

    def paintEvent(self, event):
        painter = QPainter(self)
        maximum = max((value for _, value in self.days), default=0) or 1
        step = self.width() / 30
        height = self.height() - 1
        painter.setPen(QColor(theme.BORDER))
        painter.drawLine(0, height, self.width(), height)
        for i, (_, value) in enumerate(self.days):
            bar = value / maximum * (height - 8)
            painter.fillRect(QRectF(i * step + 2, height - bar, max(1, step - 5), bar), QColor(theme.ACCENT))
        painter.end()


def duration_label(ms):
    seconds = int(ms / 1000)
    if seconds >= 3600:
        return f'{seconds // 3600}h {(seconds % 3600) // 60}m'
    return f'{seconds // 60}m {seconds % 60}s'


class StatsPage(QScrollArea):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setAccessibleName('Stats')
        self.reload()

    def showEvent(self, event):
        super().showEvent(event)
        self.reload()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_box'):
            self._box.setFixedWidth(max(1, min(1100, self.viewport().width() - 64)))

    def reload(self):
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(32, 28, 32, 28)
        box = QWidget()
        self._box = box
        box.setFixedWidth(max(1, min(1100, self.viewport().width() - 64)))
        outer.addWidget(box, 0, Qt.AlignHCenter)
        outer.addStretch()
        lay = QVBoxLayout(box)
        lay.setSpacing(18)

        def label(text, size=14, color=None):
            widget = QLabel(text)
            widget.setWordWrap(True)
            widget.setTextFormat(Qt.PlainText)
            widget.setStyleSheet(f'font-size: {size}px; color: {color or theme.TEXT_PRIMARY}; background: transparent;')
            return widget

        lay.addWidget(label('Stats', 32))
        lay.addWidget(label('Your voice, over time', 18, theme.TEXT_SECONDARY))
        try:
            stats = read_stats(getattr(self._app, 'history_store', None))
        except Exception:
            lay.addWidget(label('Stats unavailable — try opening this page again.'))
            self.setWidget(body)
            return
        self.stats = stats
        first = stats['first']
        if first is None:
            lay.addWidget(label('Nothing yet - dictate something'))
        else:
            lay.addWidget(label(f'Since {first:%d %b %Y} · Retained history only · Stored on this device', color=theme.TEXT_SECONDARY))
            grid = QGridLayout()
            grid.setHorizontalSpacing(32)
            grid.setVerticalSpacing(18)
            metrics = [
                (f"{stats['today']:,}", 'Words today'),
                (f"{stats['week']:,}", 'Words this week'),
                (f"{stats['total']:,}", 'Words all time'),
                (f"{stats['dictations']:,}", 'Dictations'),
                (f"{stats['commands']:,}", 'Commands'),
                (f"{stats['average_hold']:,.1f}", 'Average words per hold'),
                (duration_label(stats['longest_hold']), 'Longest hold'),
                (f"{stats['holds_per_day']:.1f}", 'Holds per day'),
                (duration_label(stats['duration']), 'Time dictating'),
                (f"{stats['streak']} days", 'Current streak'),
            ]
            for i, (value, title) in enumerate(metrics):
                cell = QVBoxLayout()
                cell.setSpacing(3)
                figure = label(value, 26, theme.ACCENT)
                figure.setMinimumHeight(40)
                cell.addWidget(figure)
                title_label = label(title, color=theme.TEXT_SECONDARY)
                title_label.setMinimumHeight(42)
                cell.addWidget(title_label)
                grid.addLayout(cell, i // 3, i % 3)
                grid.setColumnStretch(i % 3, 1)
            lay.addLayout(grid)
            divider = QFrame()
            divider.setFixedHeight(1)
            divider.setStyleSheet(f'background: {theme.BORDER};')
            lay.addWidget(divider)
            lay.addWidget(label('Words per day · Last 30 days', 18))
            lay.addWidget(DailyBars(stats['daily']))
            dates = QHBoxLayout()
            dates.addWidget(label(stats['daily'][0][0].strftime('%d %b'), 12, theme.TEXT_SECONDARY))
            dates.addStretch()
            dates.addWidget(label('Today', 12, theme.TEXT_SECONDARY))
            lay.addLayout(dates)
            lay.addWidget(label('Top commands', 18))
            for command, count in stats['top_commands']:
                lay.addWidget(label(f'{command}   ·   {count:,}'))
            if not stats['top_commands']:
                lay.addWidget(label('No commands yet', color=theme.TEXT_SECONDARY))
            lay.addWidget(label('Successful entries only. Week starts Monday. Holds are hold-mode dictations; holds per day includes inactive days since first retained use. Streak includes yesterday if today is still quiet. Ava turns and correction usage are not recorded.', 12, theme.TEXT_SECONDARY))
        old = self.takeWidget()
        self.setWidget(body)
        if old is not None:
            old.deleteLater()
