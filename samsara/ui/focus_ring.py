"""Keyboard-only QSS focus rings; real Qt focus and accessibility stay intact."""
import re
from functools import lru_cache

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid


@lru_cache(maxsize=4096)
def keyboard_stylesheet(sheet):
    def selector(match):
        text = match[1]
        if '[keyboardFocus=' in text:
            return match[0]
        base, separator, control = text.partition('::')
        return base + '[keyboardFocus="true"]' + separator + control + ':focus'
    def rule(match):
        selectors, declarations = match[1], match[2]
        revised = re.sub(r'([^\s,{}]+):focus', selector, selectors)
        if ':focus' in selectors and '::' not in selectors and re.search(r'\bborder(?:-color)?\s*:', declarations):
            # Keep the existing edge/radius; focus changes ink, never layout.
            if not re.search(r'\b(?:padding|margin)(?:-[\w-]+)?\s*:', declarations):
                declarations = re.sub(r'\bborder\s*:\s*2px\s+solid', 'border: 1px solid', declarations)
            if not re.search(r'\boutline\s*:', declarations):
                declarations += ' outline: none;'
        # Underline suppression is independent of the visible focus border.
        decoration = re.search(r'text-decoration\s*:[^;{}]+;', declarations)
        plain = ''
        if decoration and not declarations.replace(decoration[0], '').strip():
            return match[0]
        if decoration and revised != selectors:
            plain = selectors + '{' + decoration[0] + '}'
        return plain + revised + '{' + declarations + '}'
    return re.sub(r'([^{}]+)\{([^{}]*)\}', rule, sheet)


class FocusRings(QObject):
    def __init__(self, app):
        super().__init__(app)
        self.app, self.busy, self.navigation = app, False, False
        app.installEventFilter(self)
        self.style(app)
        for widget in app.allWidgets():
            self.style(widget)

    def style(self, widget):
        if self.busy:
            return
        sheet = widget.styleSheet()
        revised = keyboard_stylesheet(sheet)
        app_sheet = self.app.styleSheet()
        app_revised = keyboard_stylesheet(app_sheet)
        if revised != sheet or app_revised != app_sheet:
            self.busy = True
            try:
                if app_revised != app_sheet:
                    self.app.setStyleSheet(app_revised)
                if widget is not self.app and revised != sheet:
                    widget.setStyleSheet(revised)
            finally:
                self.busy = False

    def mark(self, widget, visible):
        if widget is None or not isValid(widget):
            return
        QWidget.window(widget).setAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange, visible)
        changed = False
        settings_focus = widget.property('settingsKeyboardFocus')
        if settings_focus is not None and settings_focus != visible:
            widget.setProperty('settingsKeyboardFocus', visible)
            changed = True
        if bool(widget.property('keyboardFocus')) != visible:
            widget.setProperty('keyboardFocus', visible)
            changed = True
        if not changed:
            return
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def end_navigation(self):
        self.navigation = False

    def eventFilter(self, widget, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self.navigation = False
            self.mark(self.app.focusWidget(), False)
            if isinstance(widget, QWidget):
                QWidget.window(widget).setProperty('keyboardFocusFresh', False)
        elif kind == QEvent.Type.Shortcut or (kind == QEvent.Type.KeyPress and event.key() in (
                Qt.Key.Key_Tab, Qt.Key.Key_Backtab, Qt.Key.Key_Left, Qt.Key.Key_Right,
                Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.navigation = True
            current = self.app.focusWidget()
            if current is not None:
                QWidget.window(current).setProperty('keyboardFocusFresh', False)
            self.mark(self.app.focusWidget(), True)
            QTimer.singleShot(0, self, self.end_navigation)
        if isinstance(widget, QWidget):
            if kind in (QEvent.Type.Polish, QEvent.Type.StyleChange):
                self.style(widget)
            elif kind == QEvent.Type.FocusIn:
                navigating = self.navigation and not QWidget.window(widget).property('keyboardFocusFresh')
                self.mark(widget, navigating or event.reason() in (
                    Qt.FocusReason.TabFocusReason, Qt.FocusReason.BacktabFocusReason,
                    Qt.FocusReason.ShortcutFocusReason))
            elif kind == QEvent.Type.FocusOut:
                self.mark(widget, False)
            elif kind == QEvent.Type.Show and widget.isWindow():
                widget.setProperty('keyboardFocusFresh', True)
                self.mark(self.app.focusWidget(), False)
        return False


def install(app=None):
    app = app or QApplication.instance()
    if app is not None and not hasattr(app, '_samsara_focus_rings'):
        app._samsara_focus_rings = FocusRings(app)
