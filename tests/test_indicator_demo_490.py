"""Hermetic Qt-thread checks of illustrative state, geometry and input."""
import pytest
from PySide6.QtCore import Qt, QTimer, QCoreApplication, QEvent, QAbstractAnimation
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from samsara.ui.widgets.indicator_demo import CAPTIONS, IndicatorDemo, POSITIONS
from samsara.ui.widgets.indicator_place import IndicatorPlacePicker


@pytest.fixture
def ui(qapp):
    # Reuse the repository's QApplication fixture on its owning UI thread.
    # A threaded QApplication survives rendering but can crash interpreter exit.
    def call(function):
        return function()
    yield call
    for widget in qapp.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def test_loop_beats_and_repeat(ui):
    def check():
        widget = IndicatorDemo(reduced_motion=False)
        seen = []
        widget.beat_changed.connect(seen.append)
        for milliseconds in range(0, 12001, 40):
            widget.seek(milliseconds)
        assert seen == [1, 2, 3, 4, 0]
        assert widget.caption.text() == CAPTIONS[0]
        widget.deleteLater()
    ui(check)


def test_state_words_use_receiver_helpers_without_creating_real_display(ui, monkeypatch):
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    original = LiveSurfaceWidget._lane_text
    seen = []
    def lane(receiver, view):
        seen.append(receiver._parked_idle(view))
        return original(receiver, view)
    monkeypatch.setattr(LiveSurfaceWidget, '_parked_idle', staticmethod(lambda view: False), raising=False)
    monkeypatch.setattr(LiveSurfaceWidget, '_lane_text', lane)
    widget = IndicatorDemo(reduced_motion=True)
    for beat in range(5):
        widget.set_frame(beat)
        assert widget.desktop.state_words()[0] in ('Hold', 'Command')
    assert seen == [False] * 5
    widget.deleteLater()


@pytest.mark.parametrize('position', POSITIONS)
def test_initial_focus_is_chosen_target(ui, position):
    picker = IndicatorPlacePicker(position)
    picker.show()
    assert picker.focusWidget() is picker.targets[position]
    picker.hide()
    picker.deleteLater()


def test_timer_visibility_and_restart(ui):
    def check():
        parent = QWidget()
        widget = IndicatorDemo(parent, reduced_motion=False)
        assert not widget.timer.isActive()
        assert widget.timer.parent() is widget
        parent.show()
        widget.show()
        assert widget.timer.isActive()
        widget.seek(8000)
        parent.hide()
        assert not widget.timer.isActive()
        assert not widget._elapsed.isValid()
        assert widget._observed_window is None
        parent.show()
        assert widget.beat == 0 and widget.timer.isActive()
        parent.showMinimized()
        assert not widget.timer.isActive()
        parent.showNormal()
        assert widget.beat == 0 and widget.timer.isActive()
        widget.close()
        assert not widget.timer.isActive()
        assert not widget._elapsed.isValid()
        assert widget._observed_window is None
        widget.show()
        assert widget.beat == 0 and widget.timer.isActive()
        widget.close()
        parent.hide()
        parent.deleteLater()
    ui(check)


def test_timer_drives_real_transition(ui):
    # Use the production timeout slot and real elapsed clock with short beats.
    def check():
        widget = IndicatorDemo(reduced_motion=False)
        widget.BEAT_MS, widget.LOOP_MS = 80, 400
        widget.show()
        return widget
    widget = ui(check)
    seen = []
    ui(lambda: widget.beat_changed.connect(seen.append))
    QTest.qWait(450)
    assert seen[:5] == [1, 2, 3, 4, 0]
    ui(lambda: (widget.hide(), widget.deleteLater()))


def test_reduced_motion_five_keyboard_frames(ui):
    def check():
        widget = IndicatorDemo(reduced_motion=True)
        widget.show()
        assert widget.timer is None and widget._elapsed is None
        assert not widget.findChildren(QTimer)
        assert not widget.findChildren(QAbstractAnimation)
        for index, button in enumerate(widget.steps):
            button.setFocus()
            QTest.keyClick(button, Qt.Key.Key_Space)
            assert widget.beat == index
            assert widget.caption.text() == CAPTIONS[index]
            assert widget.caption.accessibleName() == CAPTIONS[index]
        rect = widget.desktop.indicator_rect()
        widget.set_frame(4, 0)
        assert rect == widget.desktop.indicator_rect()
        widget.hide()
        widget.deleteLater()
    ui(check)


@pytest.mark.parametrize("position", POSITIONS)
def test_picker_mouse_and_keyboard(ui, position):
    def check():
        widget = IndicatorPlacePicker("custom")
        widget.resize(640, 360)
        widget.show()
        assert widget.value() == "custom"
        assert not any(button.isChecked() for button in widget.targets.values())
        seen = []
        widget.placement_chosen.connect(seen.append)
        button = widget.targets[position]
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert seen == [position] and widget.value() == position
        assert button.isChecked() and button.accessibleName()
        index = POSITIONS.index(position)
        source = POSITIONS[index // 3 * 3 + (index % 3 - 1) % 3]
        QTest.keyClick(widget.targets[source], Qt.Key.Key_Right)
        assert seen == [position, position]
        assert widget.value() == position and widget.focusWidget() is button
        QTest.keyClick(button, Qt.Key.Key_Down)
        assert widget.value() == POSITIONS[(1 - index // 3) * 3 + index % 3]
        QTest.keyClick(widget.targets[widget.value()], Qt.Key.Key_Up)
        assert widget.value() == position
        QTest.keyClick(button, Qt.Key.Key_Left)
        assert widget.value() == source
        widget.hide()
        widget.deleteLater()
    ui(check)


@pytest.mark.parametrize("scale", [1.0, 1.5])
@pytest.mark.parametrize("display", ["full", "compact"])
def test_indicator_inside_desktop_all_frames(ui, scale, display):
    def check():
        widget = IndicatorDemo(reduced_motion=False, display=display)
        widget.resize(640, 370)
        widget.show()
        for milliseconds in range(0, 12000, 40):
            widget.seek(milliseconds)
            rect = widget.desktop.indicator_rect()
            screen = widget.desktop.work_rect()
            assert screen.contains(rect)
            from PySide6.QtCore import QRectF
            assert QRectF(screen.x()*scale, screen.y()*scale, screen.width()*scale,
                          screen.height()*scale).contains(QRectF(rect.x()*scale, rect.y()*scale,
                                                                 rect.width()*scale, rect.height()*scale))
        assert widget.caption.heightForWidth(widget.caption.width()) <= max(44, widget.caption.fontMetrics().lineSpacing() * 2)
        widget.hide()
        widget.deleteLater()
    ui(check)


def test_theme_font_and_real_state_words(ui):
    from types import SimpleNamespace
    from PySide6.QtGui import QFontInfo
    from samsara.ui.live_surface_qt import LiveSurfaceWidget
    receiver = SimpleNamespace(_parked_idle=LiveSurfaceWidget._parked_idle)
    def check():
        widget = IndicatorDemo(reduced_motion=True)
        assert QFontInfo(widget.font()).family() == "Segoe UI"
        assert QFontInfo(widget.desktop.font()).family() == "Segoe UI"
        for beat in range(5):
            widget.set_frame(beat)
            view = widget.desktop.demo_view()
            assert widget.desktop.state_words() == (LiveSurfaceWidget._lane_text(receiver, view),
                                                    LiveSurfaceWidget._state_text(receiver, view))
        widget.deleteLater()
    ui(check)


def test_destroy_releases_timer_and_children(ui, qapp):
    from shiboken6 import isValid
    widget = IndicatorDemo(reduced_motion=False)
    widget.show()
    timer, desktop = widget.timer, widget.desktop
    widget.close()
    assert not timer.isActive()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(widget) and not isValid(timer) and not isValid(desktop)
