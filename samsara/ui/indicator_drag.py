"""Separate native pill input from transparent receipts; preserve composite captures."""
import weakref

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPainterPath, QRegion
from PySide6.QtWidgets import QApplication, QWidget


def capsule_region(rect):
    path = QPainterPath()
    path.addRoundedRect(rect, rect.height()/2, rect.height()/2)
    return QRegion(path.toFillPolygon().toPolygon())


class _PillWindow(QWidget):
    """Only this rounded, pill-sized window accepts native mouse/touch input."""
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._owner = weakref.proxy(owner)
        self.setAccessibleName('Listening indicator')

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._owner._paint_pill(painter, QRectF(self.rect()), *self._owner._resolve_colors())
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            local = self._owner.mapFromGlobal(event.globalPosition().toPoint()).toPointF()
            self._owner._begin_pointer(local, event.globalPosition())
        event.accept()

    def mouseMoveEvent(self, event):
        self._owner._move_pointer(event.globalPosition())
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner._end_pointer()
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def contextMenuEvent(self, event):
        event.accept()

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse):
            self._owner._end_pointer(cancel=True)
        elif kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                      QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return self._owner._touch_event(event)
        return super().event(event)

    def closeEvent(self, event):
        self.hide()
        event.ignore()


class IndicatorDrag:
    def _update_input_mask(self):
        # The backing/receipt window stays transparent and unmasked. Only the
        # separate pill window is masked; it paints nothing beyond its pill.
        if not hasattr(self, '_chip_label'):
            return
        if getattr(self, '_pill_window', None) is None:
            self._pill_window = _PillWindow(self)
        self._sync_receipt()

    def _retire_input_window(self):
        # Retire native resources while the owner is still alive, on its Qt
        # thread; a hidden ToolTip must not outlive its Python event receiver.
        from shiboken6 import delete
        self._clear_gesture()
        window = getattr(self, '_pill_window', None)
        if window is not None:
            window.hide()
            self._pill_window = None
            delete(window)

    def _sync_receipt(self):
        window = getattr(self, '_pill_window', None)
        if window is None:
            return
        body = self._layout()[2]
        if body is None or not self.isVisible():
            window.hide()
        else:
            window.setGeometry(body.toRect().translated(self.pos()))
            window.setMask(capsule_region(QRectF(window.rect())))
            if not window.isVisible():
                window.show()
            window.update()

    def update(self, *args):
        super().update(*args)
        self._sync_receipt()

    def render(self, *args, **kwargs):
        # Preserve HEAD's composite coordinates and pixels for existing tools.
        self._capture_composite = True
        try:
            return super().render(*args, **kwargs)
        finally:
            self._capture_composite = False

    def grab(self, *args, **kwargs):
        self._capture_composite = True
        try:
            return super().grab(*args, **kwargs)
        finally:
            self._capture_composite = False

    def moveEvent(self, event):
        super().moveEvent(event)
        self._sync_receipt()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_receipt()

    def hideEvent(self, event):
        self._clear_gesture()
        super().hideEvent(event)
        self._sync_receipt()

    def _begin_pointer(self, local, global_pos):
        if QApplication.activeModalWidget() is not None:
            return False
        pill = self._layout()[2]
        if pill is None or not capsule_region(pill).contains(local.toPoint()):
            return False
        self._press_origin = global_pos.toPoint()
        self._gesture_pill = pill
        self._drag_offset = self._press_origin - self.mapToGlobal(pill.topLeft().toPoint())
        self._dragging = False
        if not hasattr(self, '_drag_watchdog'):
            self._drag_watchdog = QTimer(self)
            self._drag_watchdog.setSingleShot(True)
            self._drag_watchdog.setInterval(4000)
            self._drag_watchdog.timeout.connect(lambda: self._end_pointer(cancel=True))
            QApplication.instance().screenRemoved.connect(self._drag_screen_removed)
        self._drag_watchdog.start()
        return True

    def _drag_screen_removed(self, screen):
        self._end_pointer(cancel=True)

    def _move_pointer(self, global_pos):
        origin = getattr(self, '_press_origin', None)
        if origin is None:
            return False
        if QApplication.activeModalWidget() is not None:
            self._end_pointer(cancel=True)
            return False
        self._drag_watchdog.start()
        point = global_pos.toPoint()
        if not self._dragging:
            if (point - origin).manhattanLength() < QApplication.startDragDistance():
                return True
            self._dragging = True
        position = point - self._drag_offset
        screen = QApplication.screenAt(point) or QApplication.primaryScreen()
        pill = self._layout()[2]
        if screen is not None and pill is not None:
            _, placed = self._snap_pill(position.x(), position.y(), pill, screen)
            self.move(round(placed.bounds.x - pill.x()), round(placed.bounds.y - pill.y()))
        return True

    @staticmethod
    def _snap_pill(x, y, pill, screen):
        from samsara.ui.indicator_docking import snap_preview
        from samsara.live_surface.placement import Rect, Screen
        geom = screen.availableGeometry()
        monitor = Screen(screen.name(), Rect(geom.x(), geom.y(), geom.width(), geom.height()))
        return snap_preview(x, y, pill.width(), pill.height(), monitor)

    def _commit_drag_position(self):
        if self._unlocked:
            return super()._commit_drag_position()  # Dormant legacy API.
        from samsara.live_surface.placement import Edge
        pill = self._layout()[2]
        if pill is None:
            return
        origin = self.mapToGlobal(pill.topLeft().toPoint())
        screen = QApplication.screenAt(self.mapToGlobal(pill.center().toPoint())) or QApplication.primaryScreen()
        if screen is None:
            return
        dock, placed = self._snap_pill(origin.x(), origin.y(), pill, screen)
        geom = screen.availableGeometry()
        preset = None
        if dock.edge in (Edge.TOP, Edge.BOTTOM) and screen is QApplication.primaryScreen():
            suffix = {0.: 'left', .5: 'center', 1.: 'right'}.get(dock.t)
            if suffix:
                preset = dock.edge.value + '-' + suffix
        self._corner = preset
        self._custom_position = None if preset else {
            'screen': screen.name(),
            'cx': (placed.bounds.x + pill.width()/2 - geom.x()) / max(1, geom.width()),
            'cy': (placed.bounds.y + pill.height()/2 - geom.y()) / max(1, geom.height())}
        if not preset and dock.edge is not Edge.FREE:
            self._custom_position.update(edge=dock.edge.value, t=dock.t)
        self._reposition()
        self.placement_committed.emit({'type': 'preset', 'position': preset} if preset else
                                      {'type': 'custom', **self._custom_position})

    def _clear_gesture(self):
        self._press_origin = None
        self._dragging = False
        if hasattr(self, '_drag_watchdog'):
            self._drag_watchdog.stop()
        window = getattr(self, '_pill_window', None)
        if window is not None and QWidget.mouseGrabber() is window:
            window.releaseMouse()

    def _end_pointer(self, *, cancel=False):
        if getattr(self, '_press_origin', None) is None:
            return False
        moved = self._dragging
        self._clear_gesture()
        if moved and not cancel:
            self._commit_drag_position()
        elif cancel:
            self._reposition()
        return True

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._begin_pointer(
                event.position(), event.globalPosition()):
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._move_pointer(event.globalPosition()):
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._end_pointer():
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _touch_event(self, event):
        kind = event.type()
        if kind == QEvent.Type.TouchCancel:
            self._end_pointer(cancel=True)
        else:
            points = event.points()
            if points:
                point = points[0]
                if kind == QEvent.Type.TouchBegin:
                    local = self.mapFromGlobal(point.globalPosition().toPoint()).toPointF()
                    self._begin_pointer(local, point.globalPosition())
                else:
                    self._move_pointer(point.globalPosition())
            if kind == QEvent.Type.TouchEnd:
                self._end_pointer()
        event.accept()
        return True

    def event(self, event):
        kind = event.type()
        if kind in (QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate,
                    QEvent.Type.TouchEnd, QEvent.Type.TouchCancel):
            return self._touch_event(event)
        if kind in (QEvent.Type.WindowDeactivate, QEvent.Type.UngrabMouse):
            self._end_pointer(cancel=True)
        return super().event(event)
