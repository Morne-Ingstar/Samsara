"""Numbered window overview. Widgets and DWM resources stay on the Qt thread."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import time

from PySide6.QtCore import Qt, QTimer, QPoint, QRect, QSize, QEvent, QObject, Signal, Slot, QRunnable, QThreadPool
from PySide6.QtGui import QCursor, QIcon, QKeySequence, QShortcut, QPainter, QPen, QFont, QPixmap
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                               QGridLayout, QLabel, QPushButton, QScrollArea,
                               QCheckBox, QStyle, QSizePolicy, QToolButton)

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

    def update(self, source, rect, visible, source_rect=None):
        handle = self.handles.get(source)
        if handle is None:
            return False
        props = _Properties()
        props.flags = 1 | 4 | 8 | 16  # destination, opacity, visible, client-only
        props.destination = wintypes.RECT(*rect)
        if source_rect is not None:
            props.flags |= 2
            props.source = wintypes.RECT(*source_rect)
        props.opacity, props.visible, props.client_only = 255, visible, False
        return self.api.DwmUpdateThumbnailProperties(handle, ctypes.byref(props)) == 0

    def release(self, source):
        handle = self.handles.get(source)
        if handle is not None and self.api.DwmUnregisterThumbnail(handle) == 0:
            del self.handles[source]
            self.sizes.pop(source, None)

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


class _IconDelivery(QObject):
    ready = Signal(int, object)


class _IconTask(QRunnable):
    def __init__(self, hwnd, number, delivery):
        super().__init__()
        self.hwnd, self.number, self.delivery = hwnd, number, delivery

    def run(self):
        # QImage is reentrant. Create QPixmap/QIcon only in the receiving Qt slot.
        try:
            import win32gui
            from PySide6.QtGui import QImage
            handle = win32gui.SendMessageTimeout(self.hwnd, 0x007F, 1, 0, 2, 20)
            handle = handle or win32gui.GetClassLong(self.hwnd, -14)
            if handle:
                self.delivery.ready.emit(self.number, QImage.fromHICON(handle))
        except Exception:
            pass  # fallback icon is already visible; windows may close at any time


class ListeningDock(QObject):
    """Temporarily suppress the chosen display, keeping its state source alive."""

    def __init__(self, indicator, parent):
        super().__init__(parent)
        self.indicator = indicator
        self.controller = getattr(indicator, 'controller', None)
        self.widget = (getattr(self.controller, 'widget', None)
                       if self.controller is not None else indicator)
        self.wanted = False
        self._hiding = False
        if isinstance(self.widget, QWidget):
            self.wanted = self.widget.isVisible()
            self.widget.installEventFilter(self)
            self._hide()

    def _hide(self):
        self._hiding = True
        QWidget.hide(self.widget)  # do not reset capture/thinking/chip state
        self._hiding = False

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Show:
            self.wanted = True
            self._hide()
        elif event.type() == QEvent.Type.Hide and not self._hiding:
            self.wanted = False
        return False

    def state(self):
        if self.controller is not None:
            view = self.controller.view()
            if view.snoozed:
                return 'Snoozed'
            return {'off': 'Idle', 'wake_armed': 'Armed',
                    'hands_free_listening': 'Listening', 'recording': 'Recording',
                    'transcribing': 'Transcribing', 'unavailable': 'Unavailable'}.get(
                        view.capture.value, 'Idle')
        if callable(getattr(self.indicator, 'display_state', None)):
            return {'capture': 'Recording', 'command-capture': 'Command recording',
                    'armed': 'Armed', 'command': 'Command', 'thinking': 'Thinking',
                    'session': 'Listening'}.get(self.indicator.display_state(),
                                               self.indicator.display_state().capitalize())
        return 'Idle'

    def restore(self):
        if isinstance(self.widget, QWidget):
            self.widget.removeEventFilter(self)
            if self.wanted:
                QWidget.show(self.widget)
        self.widget = None


def _font(size, bold=False):
    return theme.qfont(size, weight=QFont.Weight.Bold if bold else QFont.Weight.Normal)


def _control(text, parent=None):
    button = QPushButton(text, parent)
    button.setFixedHeight(max(44, button.fontMetrics().height() + 20))
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.setFont(_font(13))
    return button


# PLACEHOLDER COPY (869): action names, voice hints and local tray caption.
ACTIONS = [('Left half', 'left'), ('Right half', 'right'), ('Top left', 'top left'),
           ('Top right', 'top right'), ('Bottom left', 'bottom left'), ('Bottom right', 'bottom right'),
           ('Go to', 'go'), ('Maximize', 'maximize'), ('Minimize', 'minimize'),
           ('Next monitor', 'next monitor'), ('Close', 'close')]


class ActionButton(QToolButton):
    """44-DIP hit rectangle; a separate inset painted face and vector icon."""

    def __init__(self, card, label, action):
        super().__init__(card.tray)
        self.card, self.action = card, action
        self.setFixedSize(44, 44)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        spoken = f'close {card.number}' if action == 'close' else f'{card.number} {action}'
        tip = f'{label} — say “{spoken}”'
        self.setToolTip(tip)
        self.setAccessibleName(f"Window {card.number}, {card.row['app']}: {label}")
        self.setAccessibleDescription(tip)
        if action == 'next monitor' and card.row.get('monitor_count') == 1:
            self.setEnabled(False)
            self.setAccessibleDescription(tip + '. Unavailable: only one monitor.')
            self.setToolTip(self.accessibleDescription())
        self.clicked.connect(lambda: card.owner.act(card.number, action))
        self.pressed.connect(self._pressed)
        self.released.connect(lambda: QTimer.singleShot(0, card, card.quick_actions))
        self.installEventFilter(card)

    def _pressed(self):
        # Do not dispatch Select here: it would be a second backend action per click.
        self.card.owner.select(self.card.number)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
        else:
            super().keyPressEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pointer_pressed = True
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.card.owner.select(self.card.number)
            self.setDown(True)
            event.accept()
        else:
            event.accept()

    def mouseReleaseEvent(self, event):
        pressed = getattr(self, '_pointer_pressed', False)
        self._pointer_pressed = False
        self.setDown(False)
        if pressed and self.rect().contains(event.position().toPoint()):
            self.click()
        QTimer.singleShot(0, self.card, self.card.quick_actions)
        event.accept()

    def mouseMoveEvent(self, event):
        if getattr(self, '_pointer_pressed', False):
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        if getattr(self, '_pointer_pressed', False):
            self.setDown(True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        face = theme.ACCENT_DIM if self.underMouse() or self.isDown() else theme.BG2
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(face))
        p.drawRoundedRect(self.rect().adjusted(4, 4, -4, -4), 5, 5)
        ink = theme.TEXT_PRIMARY if self.isEnabled() else theme.TEXT_SECONDARY
        p.setPen(QPen(theme.qcolor(ink), 1.7))
        p.setBrush(Qt.BrushStyle.NoBrush)
        r = QRect(12, 12, 20, 20)
        a = self.action
        if a in ('left', 'right', 'top left', 'top right', 'bottom left', 'bottom right'):
            p.drawRoundedRect(r, 2, 2)
            x = 23 if 'right' in a else 14
            y = 23 if 'bottom' in a else 14
            h = 16 if a in ('left', 'right') else 7
            p.fillRect(QRect(x, y, 7, h), theme.qcolor(ink))
        elif a == 'close':
            p.drawLine(15, 15, 29, 29)
            p.drawLine(29, 15, 15, 29)
        elif a == 'minimize':
            p.drawLine(13, 29, 31, 29)
        elif a == 'maximize':
            p.drawRect(r)
            p.drawLine(13, 16, 31, 16)
        elif a == 'next monitor':
            p.drawRoundedRect(QRect(9, 12, 17, 14), 2, 2)
            p.drawLine(17, 26, 17, 31)
            p.drawLine(12, 31, 22, 31)
            p.drawLine(25, 23, 35, 23)
            p.drawLine(30, 18, 35, 23)
            p.drawLine(30, 28, 35, 23)
        else:
            p.drawRoundedRect(r, 2, 2)
            p.drawLine(8, 22, 25, 22)
            p.drawLine(20, 17, 25, 22)
            p.drawLine(20, 27, 25, 22)
        if self.hasFocus():
            p.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
            p.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 6, 6)


class ConfirmationSurface(QWidget):
    def __init__(self, card):
        super().__init__(card)
        self.card = card
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.card.owner.act(0, 'confirm close')
            event.accept()
        else:
            super().keyPressEvent(event)


class OverviewCard(QWidget):
    """Quiet card with one reusable local action tray, created on discovery."""

    def __init__(self, row, owner):
        super().__init__()
        self.row, self.owner, self.number = row, owner, row['number']
        self.setObjectName('overviewTile')
        self.setAccessibleName(f"Window {self.number}: {row['app']}")
        self.setToolTip(row.get('title', row['app']))
        self.update_monitor_description()
        self.setFixedHeight(210)
        self.setMinimumWidth(288)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.selected = self.hovered = self.confirming = self.tray_visible = False
        self.setProperty('reading', False)
        self.quick, self.buttons = [], {}
        self.tray = self.overlay = None
        self.icon = QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.preview = QLabel(self)
        self.preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setPixmap(self.icon.pixmap(QSize(48, 48)))
        self.preview.setAccessibleName(f'Preview of window {self.number}')
        self.subtitle = row.get('short_title', '')
        if self.subtitle.strip().casefold() == row['app'].strip().casefold():
            self.subtitle = ''
        self.monitor_text = f"M{row.get('monitor', 1)}"
        self.apply_theme()

    def update_monitor_description(self):
        primary = ', primary monitor' if self.row.get('primary') else ''
        self.setAccessibleDescription(f"{self.row.get('title', self.row['app'])}. "
                                      f"Monitor {self.row.get('monitor', 1)}{primary}.")

    def apply_theme(self):
        self.preview.setStyleSheet(f'background: {theme.OVERVIEW_WELL}; border-radius: 4px;')
        if self.tray:
            self.tray.setStyleSheet(f'QWidget#actionTray {{ background: {theme.OVERVIEW_ACTION_SURFACE}; border-radius: 4px; }}')
            self.caption.setStyleSheet(f'color: {theme.TEXT_SECONDARY}; background: transparent;')
        if self.overlay:
            self.overlay.setStyleSheet(f'QWidget#closeOverlay {{ background: {theme.OVERVIEW_CONFIRM}; border-radius: 4px; }}')
            self.question.setStyleSheet(f'color: {theme.INK_LIGHT}; background: transparent;')
            self.yes.setStyleSheet(f'background: {theme.ERROR}; color: {theme.ink_on(theme.ERROR)}; border-radius: 16px; padding: 0 10px;')
        self.update()
        for button in self.quick:
            button.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        reading = bool(self.property('reading'))
        color = (theme.ERROR if self.confirming else theme.ACCENT if self.selected
                 else theme.TEXT_PRIMARY if reading or self.hovered else theme.BORDER)
        p.setPen(QPen(theme.qcolor(color), 2 if self.confirming or self.selected or reading else 1))
        p.setBrush(theme.qcolor(theme.BG2 if reading else theme.BG1))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 8, 8)
        if reading and (self.selected or self.confirming):
            p.setPen(QPen(theme.qcolor(theme.TEXT_PRIMARY), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(self.rect().adjusted(4, 4, -4, -4), 6, 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(theme.ACCENT))
        p.drawRoundedRect(QRect(8, 4, 28, 28), 6, 6)
        p.setPen(theme.qcolor(theme.TEXT_ON_ACCENT))
        p.setFont(_font(16, True))
        p.drawText(QRect(8, 4, 28, 28), Qt.AlignmentFlag.AlignCenter, str(self.number))
        self.icon.paint(p, QRect(44, 10, 16, 16))
        p.setPen(theme.qcolor(theme.TEXT_PRIMARY))
        p.setFont(theme.qfont(15, weight=QFont.Weight.DemiBold))
        name = p.fontMetrics().elidedText(self.row['app'], Qt.TextElideMode.ElideRight, max(1, self.width()-108))
        p.drawText(QRect(68, 0, self.width()-108, 36), Qt.AlignmentFlag.AlignVCenter, name)
        primary = self.row.get('primary', False)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor(theme.ACCENT_DIM if primary else theme.BG2))
        pill = QRect(self.width()-32, 9, 24, 18)
        p.drawRoundedRect(pill, 4, 4)
        p.setPen(theme.qcolor(theme.ACCENT if primary else theme.TEXT_SECONDARY))
        p.setFont(_font(11, True))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, self.monitor_text)
        p.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        p.setFont(_font(12))
        detail = p.fontMetrics().elidedText(self.subtitle, Qt.TextElideMode.ElideRight, self.width()-24)
        p.drawText(QRect(12, self.preview.geometry().bottom()+5, self.width()-24, 30), Qt.AlignmentFlag.AlignVCenter, detail)

    def resizeEvent(self, event):
        # Large text and very narrow windows grow cards rather than shrink targets.
        narrow = self.width() < 288
        self._tray_columns = 3 if narrow else 6
        caption_h = max(20, self.caption.fontMetrics().height()+4) if self.tray else 20
        top = 40 + caption_h + 6
        preview_h = max(130, caption_h+14+(4 if narrow else 2)*44+(12 if narrow else 4))
        if self.overlay:
            question_h = self.question.heightForWidth(max(1, self.width()-40))
            preview_h = max(preview_h, question_h+max(self.yes.height(), self.cancel.height())+28)
        self.setFixedHeight(max(210, 40+preview_h+40, self.fontMetrics().height()+190))
        self.preview.setGeometry(12, 40, self.width()-24, preview_h)
        if self.tray:
            self.tray.setGeometry(self.preview.geometry())
            self.caption.setGeometry(2, 2, self.tray.width()-4, caption_h)
            for index, button in enumerate(self.quick):
                cell = index if index < 10 else 11
                button.move((cell % self._tray_columns)*44, top-40+(cell//self._tray_columns)*48)
        if self.overlay:
            self.overlay.setGeometry(self.preview.geometry())
        if event is not None:
            super().resizeEvent(event)

    def _focus_within(self):
        focus = QApplication.focusWidget()
        return focus is self or (focus is not None and self.isAncestorOf(focus))

    def quick_actions(self):
        pressed = any(b.isDown() for b in self.quick)
        visible = (self.hovered or self.selected or self._focus_within() or pressed) and not self.confirming
        if visible and self.tray is None:
            self.tray = QWidget(self)
            self.tray.setObjectName('actionTray')
            self.tray.installEventFilter(self)
            self.caption = QLabel(f'Arrange window {self.number}', self.tray)  # PLACEHOLDER COPY (869)
            self.caption.setFont(_font(12))
            self.caption.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            for label, action in ACTIONS:
                button = ActionButton(self, label, action)
                self.quick.append(button)
                self.buttons[action] = button
            self.resizeEvent(None)
            self.apply_theme()
        changed = self.tray_visible != visible
        self.tray_visible = visible
        # Hide the compositor-owned texture BEFORE exposing the opaque Qt tray.
        if changed:
            self.owner._update_thumbnails()
        if self.tray:
            self.tray.setVisible(visible)
            self.tray.raise_()
        self.owner._tab_order()
        self.update()

    def _pointer_left(self):
        if self.owner._closing:
            return
        point = self.mapFromGlobal(QCursor.pos())
        self.hovered = self.rect().contains(point)
        self.quick_actions()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.Type.KeyPress and watched in (getattr(self, 'yes', None), getattr(self, 'cancel', None)) and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            watched.click()
            return True
        if kind == QEvent.Type.Enter:
            self.hovered = True
            self.quick_actions()
        elif kind in (QEvent.Type.Leave, QEvent.Type.FocusOut, QEvent.Type.MouseButtonRelease):
            QTimer.singleShot(0, self, self._pointer_left)
        elif kind == QEvent.Type.FocusIn:
            self.quick_actions()
        # An empty tray click selects, and never bubbles into the card's Go.
        if watched is self.tray and kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
            if kind == QEvent.Type.MouseButtonRelease:
                self.owner.act(self.number, 'select')
            return True
        return super().eventFilter(watched, event)

    def enterEvent(self, event):
        self.hovered = True
        self.quick_actions()
        super().enterEvent(event)

    def leaveEvent(self, event):
        QTimer.singleShot(0, self, self._pointer_left)
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if event.reason() in (Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason):
            self.owner.act(self.number, 'select')
            self.owner.select(self.number)
        self.quick_actions()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        QTimer.singleShot(0, self, self.quick_actions)

    def mouseReleaseEvent(self, event):
        if not self.confirming:
            self.owner.act(self.number, 'select' if event.button() == Qt.MouseButton.RightButton else 'go')
        event.accept()

    def keyPressEvent(self, event):
        key = event.key()
        if not event.text().isdigit():
            self.owner._digits = ''
        if key == Qt.Key.Key_Space:
            self.owner.act(self.number, 'select')
            self.owner.select(self.number)
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.owner.act(self.number, 'go')
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            values = list(self.owner._tiles)
            delta = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1,
                     Qt.Key.Key_Up: -self.owner._columns, Qt.Key.Key_Down: self.owner._columns}[key]
            target = values[max(0, min(len(values)-1, values.index(self.number)+delta))]
            self.owner.act(target, 'select')
            self.owner.select(target)
            self.owner._tiles[target].setFocus()
        elif event.text().isdigit():
            self.owner.type_number(event.text())
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def confirmation(self, text):
        previous = self.confirming
        self.confirming = bool(text)
        if text and self.overlay is None:
            self.overlay = ConfirmationSurface(self)
            self.overlay.setObjectName('closeOverlay')
            layout = QVBoxLayout(self.overlay)
            layout.setContentsMargins(8, 8, 8, 8)
            self.question = QLabel(f"Close {self.row['app']}?")  # PLACEHOLDER COPY (869)
            self.question.setTextFormat(Qt.TextFormat.PlainText)
            self.question.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.question.setWordWrap(True)
            self.question.setFont(_font(14, True))
            layout.addWidget(self.question)
            actions = QHBoxLayout()
            actions.setSpacing(8)
            actions.addStretch()
            self.yes, self.cancel = _control('Yes (Enter)'), _control('Cancel (Esc)')
            for button, action in ((self.yes, 'confirm close'), (self.cancel, 'cancel close')):
                button.setMinimumWidth(104)
                button.installEventFilter(self)
                button.clicked.connect(lambda checked=False, a=action: self.owner.act(0, a))
                actions.addWidget(button)
            actions.addStretch()
            layout.addLayout(actions)
            self.apply_theme()
            self.resizeEvent(None)
        self.quick_actions()
        if self.overlay:
            self.overlay.setGeometry(self.preview.geometry())
            self.overlay.setVisible(bool(text))
            self.overlay.raise_()
            if text:
                self.overlay.setFocus()
            elif previous:
                self.setFocus()
        self.owner._tab_order()


class OverviewWindow(QWidget):
    def __init__(self, rows, on_action, on_close, keep_open=False, thumbnails=None,
                 listening_indicator=None):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle('Samsara Window Overview')
        self.setAccessibleName('Window overview')
        self._action, self._closed = on_action, on_close
        self._thumbnails = thumbnails if thumbnails is not None else DwmThumbnails()
        self.destroyed.connect(self._thumbnails.close)
        self._previews, self._tiles = [], {}
        self._closing = False
        self._selected = self._confirming = None
        self._columns = 0
        self._digits, self._digit_time = "", 0.0
        self._registered = False
        self._started = time.perf_counter()
        self._registration_done = None
        self.paint_metrics = {}
        self._paint_pending = False
        self._icons = _IconDelivery(self)
        self._icons.ready.connect(self._set_icon)
        self._icons_started = False
        self._indicator = listening_indicator
        self._dock = None
        self.setFont(_font(14))
        self.setStyleSheet(
            f'OverviewWindow {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; }}'
            f'QLabel, QCheckBox {{ color: {theme.TEXT_PRIMARY}; background: transparent; }}'
            f'QPushButton, QToolButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};'
            f' border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 0 10px; }}'
            f'QToolButton {{ padding: 0; }} QPushButton:hover, QToolButton:hover {{ background: {theme.ACCENT_DIM}; }}'
            f'QScrollArea {{ border: none; background: {theme.BG0}; }}')
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = self.header = QWidget()
        header.setFixedHeight(56)
        heading = self.heading = QGridLayout(header)
        heading.setContentsMargins(40, 0, 40, 0)
        heading.setSpacing(16)
        self.title = QLabel(f'{len(rows)} windows')
        self.title.setFont(_font(20, True))
        self.title.setStyleSheet(f'font-size: {theme.TYPE_HEADING}px; font-weight: 700;')
        heading.addWidget(self.title, 0, 0)
        # PLACEHOLDER COPY (869)
        self.hint = QLabel('Say 3 to select · 3 left · 3 go · close 3 · read windows')
        self.hint.setFont(_font(14))
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f'color: {theme.TEXT_SECONDARY};')
        heading.addWidget(self.hint, 0, 1)
        heading.setColumnStretch(1, 1)
        self.listening = QLabel('● Idle')
        self.listening.setFont(_font(13))
        self.listening.setStyleSheet(f'color: {theme.ACCENT};')
        heading.addWidget(self.listening, 0, 2)
        self.keep = QCheckBox('Keep open')
        self.keep.setFont(_font(13))
        self.keep.setChecked(keep_open)
        self.keep.toggled.connect(lambda checked: self._action(0, 'keep open', checked))
        self.keep.setMinimumHeight(44)
        heading.addWidget(self.keep, 0, 3)
        dismiss = self.dismiss = _control('Close (Esc)')
        dismiss.clicked.connect(self.close)
        heading.addWidget(dismiss, 0, 4)
        root.addWidget(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setAccessibleName('Open windows')
        self.host = QWidget()
        self.host.setStyleSheet(f'background: {theme.BG0};')
        outer = self.outer = QHBoxLayout(self.host)
        outer.setContentsMargins(40, 16, 40, 16)
        outer.addStretch()
        self.container = QWidget()
        self.container.setMaximumWidth(1840)
        self.grid = QGridLayout(self.container)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(16)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(self.container, 1)
        outer.addStretch()
        self.scroll.setWidget(self.host)
        root.addWidget(self.scroll, 1)
        for row in rows:
            card = OverviewCard(row, self)
            self._tiles[row['number']] = card
            self._previews.append((row['hwnd'], card.preview))
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        self.setGeometry(screen.geometry() if screen else QRect(0, 0, 1920, 1080))
        self._reflow()
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_thumbnails)
        self._escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self._escape_pressed)
        QApplication.instance().focusChanged.connect(self._focus_changed)
        theme.theme_change_signal().changed.connect(self.apply_theme)
        self._state_timer = QTimer(self)
        self._state_timer.setInterval(100)
        self._state_timer.timeout.connect(self._listening_state)

    def apply_theme(self, *_):
        self.setStyleSheet(
            f'OverviewWindow {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY}; }}'
            f'QLabel, QCheckBox {{ color: {theme.TEXT_PRIMARY}; background: transparent; }}'
            f'QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 0 10px; }}'
            f'QPushButton:hover {{ background: {theme.ACCENT_DIM}; }}'
            f'QPushButton:focus, QCheckBox:focus {{ border: 2px solid {theme.ACCENT}; }}'
            f'QScrollArea {{ border: none; background: {theme.BG0}; }}')
        self.host.setStyleSheet(f'background: {theme.BG0};')
        self.hint.setStyleSheet(f'color: {theme.TEXT_SECONDARY};')
        self.listening.setStyleSheet(f'color: {theme.ACCENT};')
        for card in self._tiles.values():
            card.apply_theme()

    def _reflow(self):
        gutter = 40 if self.width() >= 1440 else 24
        self.outer.setContentsMargins(gutter, 16, gutter, 16)
        self.heading.setContentsMargins(gutter, 0, gutter, 0)
        required = sum(w.sizeHint().width() for w in (self.title, self.listening, self.keep, self.dismiss)) + gutter*2+64
        wrap = self.width() < 1100 or required+self.hint.fontMetrics().horizontalAdvance(self.hint.text())+16 > self.width()
        self.heading.removeWidget(self.hint)
        self.heading.addWidget(self.hint, 1 if wrap else 0, 0 if wrap else 1, 1, 5 if wrap else 1)
        hint_h = self.hint.heightForWidth(max(1, self.width()-gutter*2)) if wrap else 0
        row_h = max(56, max(w.sizeHint().height() for w in (self.title, self.listening, self.keep, self.dismiss))+8)
        self.header.setFixedHeight(max(92, row_h+hint_h+8) if wrap else row_h)
        available = min(1840, self.width()-gutter*2)
        def columns_for(width):
            return max(1, min(6, (width+16)//304))
        columns = columns_for(available)
        height = max((c.height() for c in self._tiles.values()), default=210)
        def content_height(n):
            rows = (len(self._tiles)+n-1)//n
            return max(0, rows*(height+16)-16)+32
        scrolls = content_height(columns) > self.height()-self.header.height()
        if scrolls:
            available -= self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent)
            columns = columns_for(available)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn if scrolls else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for card in self._tiles.values():
            card.setMinimumWidth(min(288, max(156, available)))
        if columns != self._columns:
            self._columns = columns
            while self.grid.count():
                self.grid.takeAt(0)
            for col in range(6):
                self.grid.setColumnStretch(col, 1 if col < columns else 0)
            for index, card in enumerate(self._tiles.values()):
                self.grid.addWidget(card, index//columns, index%columns)
        self.container.setMinimumHeight(content_height(columns)-32)
        self.host.setMinimumHeight(content_height(columns))
        self._tab_order()

    def _tab_order(self):
        values = list(self._tiles)
        active = self._selected if self._selected in self._tiles else (values[0] if values else None)
        sequence = [self.keep, self.dismiss]
        for n, card in self._tiles.items():
            card.setFocusPolicy(Qt.FocusPolicy.StrongFocus if n == active else Qt.FocusPolicy.ClickFocus)
            for button in card.quick:
                button.setFocusPolicy(Qt.FocusPolicy.StrongFocus if n == active and card.tray_visible else Qt.FocusPolicy.ClickFocus)
        if self._confirming in self._tiles:
            card = self._tiles[self._confirming]
            if card.overlay:
                sequence += [card.overlay, card.yes, card.cancel]
        elif active is not None:
            card = self._tiles[active]
            sequence += [card]+[b for b in card.quick if b.isEnabled() and card.tray_visible]
        for before, after in zip(sequence, sequence[1:]+sequence[:1]):
            QWidget.setTabOrder(before, after)

    def _focus_changed(self, before, after):
        if self._closing:
            return
        self._digits = ''
        if after is None or (after is not self and not self.isAncestorOf(after)):
            self._digits = ''
        for card in self._tiles.values():
            if before is card or (before is not None and card.isAncestorOf(before)) or after is card or (after is not None and card.isAncestorOf(after)):
                card.quick_actions()

    def type_number(self, digit):
        now = time.monotonic()
        self._digits = self._digits+digit if now-self._digit_time < .7 else digit
        self._digit_time = now
        number = int(self._digits)
        if number in self._tiles:
            self.act(number, 'select')
            self.select(number)
        else:
            self._digits = ''

    def keyPressEvent(self, event):
        if event.text().isdigit():
            self.type_number(event.text())
            event.accept()
        else:
            self._digits = ''
            super().keyPressEvent(event)

    def act(self, number, action):
        if number is None:
            return
        if self._closing or (number and number not in self._tiles):
            return
        self._action(number, action, self.keep.isChecked())

    def select(self, number):
        if number not in self._tiles:
            return
        self._selected = number
        for value, tile in self._tiles.items():
            tile.selected = value == number
            tile.quick_actions()
        self._tab_order()
        self.scroll.ensureWidgetVisible(self._tiles[number], 0, 0)

    def remove(self, number):
        card = self._tiles.pop(number, None)
        if card is None:
            return
        self._previews = [(hwnd, preview) for hwnd, preview in self._previews if preview is not card.preview]
        self._thumbnails.release(card.row['hwnd'])
        self.grid.removeWidget(card)
        card.hide()
        card.deleteLater()
        if self._selected == number:
            self._selected = None

        if self._confirming == number:
            self._confirming = None
        self._columns = 0
        self._reflow()
        survivors = list(self._tiles)
        if survivors:
            successor = next((n for n in survivors if n > number), survivors[-1])
            self.select(successor)
            self._tiles[successor].setFocus()
        self.title.setText(f'{len(self._tiles)} windows')
        QTimer.singleShot(0, self, self._update_thumbnails)

    def set_monitor(self, number, index, primary):
        card = self._tiles.get(number)
        if card:
            card.monitor_text = f'M{index}'
            card.row.update(monitor=index, primary=primary)
            card.update_monitor_description()
            card.update()

    def focus_reading(self, number):
        for value, tile in self._tiles.items():
            tile.setProperty('reading', value == number)
            tile.update()
        if number in self._tiles:
            self.scroll.ensureWidgetVisible(self._tiles[number], 0, 0)

    def show_confirmation(self, text):
        import re
        match = re.match(r'Close (\d+):', text)
        number = int(match[1]) if match else None
        self._confirming = number if number in self._tiles else None
        if self._confirming is not None:
            self.select(self._confirming)
        for value, card in self._tiles.items():
            card.confirmation(text if value == self._confirming else '')
        if self._confirming is not None:
            self.scroll.ensureWidgetVisible(self._tiles[self._confirming])
        self._update_thumbnails()

    def _escape_pressed(self):
        if self._confirming is not None:
            self.act(0, 'cancel close')
        else:
            self.close()

    def _enter_pressed(self):
        if self._confirming is not None:
            self.act(0, 'confirm close')
        elif self._selected is not None:
            self.act(self._selected, 'go')

    @Slot(int, object)
    def _set_icon(self, number, image):
        card = self._tiles.get(number)
        if card and not image.isNull():
            card.icon = QIcon(QPixmap.fromImage(image))
            card.update()

    def _listening_state(self):
        if self._dock:
            self.listening.setText('● ' + self._dock.state())

    def showEvent(self, event):
        super().showEvent(event)
        self.apply_theme()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if self._indicator is not None and self._dock is None:
            self._dock = ListeningDock(self._indicator, self)
        self._listening_state()
        self._state_timer.start()
        if not self._icons_started:
            self._icons_started = True
            for number, card in self._tiles.items():
                QThreadPool.globalInstance().start(_IconTask(card.row['hwnd'], number, self._icons))
        if not self._registered:
            self._pending_previews = iter(tuple(self._previews))
            QTimer.singleShot(0, self, self._register_thumbnails)

    def _register_thumbnails(self):
        if not self.isVisible():
            return
        # One cross-process DWM call per turn, so input/paint events can run.
        item = next(self._pending_previews, None)
        if item is None:
            self._registered = True
            self._registration_done = time.perf_counter()
            self.update()
            return
        hwnd, preview = item
        if (hwnd, preview) in self._previews:
            try:
                registered = self._thumbnails.register(int(self.winId()), hwnd)
            except Exception:
                registered = False
            if not registered:
                preview.setPixmap(QApplication.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon).pixmap(QSize(48, 48)))
            self._update_thumbnails()
        QTimer.singleShot(0, self, self._register_thumbnails)

    def _update_thumbnails(self, *_):
        if not self.isVisible():
            return
        dpr = self.devicePixelRatioF()
        viewport = self.scroll.viewport()
        visible_area = QRect(viewport.mapTo(self, QPoint()), viewport.size())
        for card in self._tiles.values():
            hwnd, preview = card.row['hwnd'], card.preview
            rect = QRect(preview.mapTo(self, QPoint()), preview.size())
            visible = visible_area.contains(rect) and not card.confirming and not card.tray_visible
            source_w, source_h = self._thumbnails.sizes.get(hwnd, (rect.width(), rect.height()))
            source_rect = None
            if source_w > 0 and source_h > 0 and rect.width() > 0 and rect.height() > 0:
                # Aspect-fill by cropping the source, never distorting it.
                factor = max(rect.width() / source_w, rect.height() / source_h)
                crop_w, crop_h = round(rect.width() / factor), round(rect.height() / factor)
                x, y = (source_w - crop_w) // 2, (source_h - crop_h) // 2
                source_rect = (x, y, x + crop_w, y + crop_h)
            coords = tuple(round(n * dpr) for n in (rect.x(), rect.y(), rect.right() + 1, rect.bottom() + 1))
            self._thumbnails.update(hwnd, coords, visible, source_rect)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._registration_done is not None and not self.paint_metrics and not self._paint_pending:
            self._paint_pending = True
            # Deliver after this paint traversal, including all child cards.
            QTimer.singleShot(0, self, self._record_painted)

    def _record_painted(self):
        self._paint_pending = False
        if not self.isVisible() or self._registration_done is None or self.paint_metrics:
            return
        now = time.perf_counter()
        self.paint_metrics = dict(windows=len(self._tiles), open_to_painted_ms=(now - self._started) * 1000,
                                 after_registration_ms=(now - self._registration_done) * 1000)
        log.info('Window overview painted: %s', self.paint_metrics)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'grid'):
            self._reflow()
            QTimer.singleShot(0, self, self._update_thumbnails)

    def hideEvent(self, event):
        self._state_timer.stop()
        self._thumbnails.close()
        self._registered = False
        if self._dock:
            self._dock.restore()
            self._dock = None
        super().hideEvent(event)

    def closeEvent(self, event):
        if not self._closing:
            self._closing = True
            self._closed()
        super().closeEvent(event)


class OverviewPanel:
    def __init__(self):
        self.window = None
        self._generation = 0

    def show(self, rows, on_action, on_close, keep_open=False, listening_indicator=None):
        self._generation += 1
        generation = self._generation
        def render():
            if generation != self._generation:
                return
            if self.window:
                self.window._closing = True
                self.window.close()
                self.window.deleteLater()
            self.window = OverviewWindow(rows, on_action, on_close, keep_open,
                                         listening_indicator=listening_indicator)
            self.window.show()
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

    def _post(self, method, *args):
        generation = self._generation
        def update():
            if self.window and generation == self._generation:
                getattr(self.window, method)(*args)
        qt_runtime.post(update)

    def confirmation(self, text):
        self._post('show_confirmation', text)

    def focus_reading(self, number):
        self._post('focus_reading', number)

    def select(self, number):
        self._post('select', number)

    def remove(self, number):
        self._post('remove', number)

    def monitor(self, number, index, primary):
        self._post('set_monitor', number, index, primary)

    def keep_open(self, checked):
        generation = self._generation
        def update():
            if self.window and generation == self._generation:
                self.window.keep.blockSignals(True)
                self.window.keep.setChecked(checked)
                self.window.keep.blockSignals(False)
        qt_runtime.post(update)


_panel = None


def get_panel():
    global _panel
    if _panel is None:
        _panel = OverviewPanel()
    return _panel
