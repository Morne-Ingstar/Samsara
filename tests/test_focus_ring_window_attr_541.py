"""541: a widget that keeps something of its own under the name ``window`` must not break the keyboard focus ring.

Daily log, 12 Oct 02:09: thirty uncaught TypeErrors at focus_ring.py line 65 - ``widget.window()`` reached an instance
attribute instead of QWidget.window on a control in Settings. The ring now asks Qt for the window, not the instance."""
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QFocusEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QPushButton, QVBoxLayout, QWidget
from samsara.ui.focus_ring import FocusRings


class _Shadowed(QPushButton):
    def __init__(self, owner, parent):
        super().__init__('x', parent)
        self.window = owner     # not callable: the shape behind the log's TypeError


def _rings():
    app = QApplication.instance() or QApplication([])
    if not hasattr(app, '_samsara_focus_rings'):
        app._samsara_focus_rings = FocusRings(app)
    return app._samsara_focus_rings


def _button():
    top = QWidget()
    QVBoxLayout(top)
    button = _Shadowed(object(), top)
    top.layout().addWidget(button)
    return top, button


def test_mark_sets_and_clears_the_ring_on_a_widget_that_shadows_window():
    rings = _rings()
    top, button = _button()
    rings.mark(button, True)
    assert button.property('keyboardFocus') is True
    assert top.testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
    rings.mark(button, False)
    assert button.property('keyboardFocus') is False
    top.deleteLater()


def test_every_event_path_survives_a_widget_that_shadows_window():
    rings = _rings()
    top, button = _button()
    rings.eventFilter(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.TabFocusReason))
    assert button.property('keyboardFocus') is True
    rings.eventFilter(button, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.MouseFocusReason))
    assert button.property('keyboardFocus') is False
    rings.navigation = True     # the branch that reads the window's 'keyboardFocusFresh'
    rings.eventFilter(button, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.OtherFocusReason))
    assert button.property('keyboardFocus') is True
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(1, 1), QPointF(1, 1), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    rings.eventFilter(button, press)
    assert rings.navigation is False
    assert top.property('keyboardFocusFresh') is False
    top.deleteLater()
