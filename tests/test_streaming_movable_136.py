"""Queue 136: movable, persisted, click-through dictate preview."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from samsara import config_defaults
from samsara.streaming import IdleSettings, _StreamingWidget


def _close(qapp, overlay):
    overlay.stop_life()
    overlay._w.hide()
    overlay._w.deleteLater()
    qapp.processEvents()


def test_drag_commits_normalized_position_and_restores_it_after_restart(qapp):
    saved = []
    overlay = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, (),
                               on_placement_committed=saved.append)
    overlay.show_overlay()
    qapp.processEvents()
    try:
        widget = overlay._w
        before = widget.pos()
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        QTest.mouseMove(widget, QPoint(180, 100))
        QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, pos=QPoint(180, 100))
        qapp.processEvents()
        assert widget.pos() != before
        assert len(saved) == 1 and saved[0].startswith("custom|")
    finally:
        _close(qapp, overlay)

    restored = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, (),
                                preview_position=saved[0])
    restored.show_overlay()
    qapp.processEvents()
    try:
        primary = QApplication.primaryScreen().availableGeometry()
        rect = restored._w.frameGeometry()
        assert primary.contains(rect), "saved normalized placement is clamped to available geometry"
    finally:
        _close(qapp, restored)


def test_custom_position_with_missing_second_monitor_falls_back_on_screen(qapp):
    overlay = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, (),
                               preview_position="custom|disconnected-monitor|1.0|1.0")
    overlay.show_overlay()
    qapp.processEvents()
    try:
        primary = QApplication.primaryScreen().availableGeometry()
        rect = overlay._w.frameGeometry()
        assert primary.contains(rect), "disconnected second monitor cannot strand the preview"
    finally:
        _close(qapp, overlay)


def test_idle_preview_stays_click_through_and_cannot_begin_a_drag(qapp):
    overlay = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False, ())
    overlay.show_overlay()
    qapp.processEvents()
    try:
        widget = overlay._w
        widget._begin_idle(widget._now())
        QTest.mousePress(widget, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
        assert widget._click_through is True and widget._dragging is False
    finally:
        _close(qapp, overlay)


def test_preview_position_has_one_schema_derived_default():
    assert config_defaults.cfg_get({}, "command_mode.preview_position") == "bottom-center"
