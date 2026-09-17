"""Queue 163: reachable floating-window recovery and placement."""

from types import SimpleNamespace

import pytest

from plugins.commands import core_utils
from samsara import streaming
from samsara.streaming import IdleSettings, _StreamingWidget
from samsara.ui.listening_indicator import ListeningIndicator, reset_indicator_placement
from samsara.ui.settings.modes_qt import ModesPage
from samsara.ui.tray_qt import SamsaraTrayQt


OWNER_PREVIEW = "custom|ASUS VG289Q1A|0.892578|0.116429"
OWNER_INDICATOR = {"screen": "ASUS VG289Q1A", "cx": 0.0768, "cy": 0.9853}
_MARGIN = 24


def _close(qapp, widget):
    widget.stop_life()
    widget._w.hide()
    widget._w.deleteLater()
    qapp.processEvents()


class _App:
    def __init__(self, preview=None, indicator=None):
        self.config = {
            "command_mode": {"preview_position": OWNER_PREVIEW},
            "listening_indicator_position": "bottom-center",
            "listening_indicator_custom_position": dict(OWNER_INDICATOR),
        }
        self._dictate_preview = SimpleNamespace(_overlay=preview) if preview else None
        self._indicator = indicator
        self.saved = []

    def update_config_and_save(self, changes):
        for key, value in changes.items():
            self.config[key] = value
        self.saved.append(changes)

    def apply_listening_indicator_settings(self):
        self._indicator.set_position(self.config["listening_indicator_position"])


class _PreviewSink:
    def __init__(self, widget):
        self.widget = widget

    def move_draft(self, position):
        self.widget._w.set_preview_position(position)


def _assert_default_geometry(qapp, preview, indicator):
    screen = qapp.primaryScreen().availableGeometry()
    assert preview._w.y() == screen.bottom() - preview._w.height() + 1 - (
        streaming.TASKBAR_RESERVE + streaming.OVERLAY_GAP_ABOVE_TASKBAR
    )
    assert indicator.geometry().bottom() == screen.bottom() - _MARGIN


@pytest.mark.parametrize("entry", ["settings", "tray", "voice"])
def test_each_recovery_entry_resets_both_windows_to_default_geometry(qapp, monkeypatch, entry):
    preview = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False,
                               preview_position=OWNER_PREVIEW)
    preview.show_overlay()
    indicator = ListeningIndicator()
    indicator.set_custom_position(OWNER_INDICATOR["screen"], OWNER_INDICATOR["cx"], OWNER_INDICATOR["cy"])
    indicator.show()
    qapp.processEvents()
    app = _App(_PreviewSink(preview), indicator)
    try:
        if entry == "settings":
            ModesPage._reset_floating_window_positions(SimpleNamespace(
                _reset_preview_position=lambda: streaming.reset_preview_placement(app),
                _reset_indicator_position=lambda: reset_indicator_placement(app),
            ))
        elif entry == "tray":
            SamsaraTrayQt._reset_floating_window_positions(SimpleNamespace(
                _reset_preview_position=lambda: streaming.reset_preview_placement(app),
                _reset_indicator_position=lambda: reset_indicator_placement(app),
            ))
        else:
            monkeypatch.setattr("samsara.ui.qt_runtime.post", lambda callback: callback())
            core_utils.reset_floating_windows(app)
        qapp.processEvents()
        _assert_default_geometry(qapp, preview, indicator)
    finally:
        indicator.hide()
        indicator.deleteLater()
        _close(qapp, preview)


@pytest.mark.parametrize("x, y", [
    (-10000, 400), (10000, 400), (400, -10000), (400, 10000),
    (-10000, -10000), (10000, -10000), (-10000, 10000), (10000, 10000),
])
def test_dragged_windows_stay_inside_the_reachable_inset(qapp, x, y):
    geom = qapp.primaryScreen().availableGeometry()
    preview = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False)
    preview.show_overlay()
    indicator = ListeningIndicator()
    try:
        preview._w.move(x, y)
        preview._w._commit_drag_position()
        indicator.enter_move_mode()
        indicator.move(x, y)
        indicator._commit_drag_position()
        qapp.processEvents()
        for window in (preview._w, indicator):
            rect = window.geometry()
            assert rect.left() >= geom.left() + _MARGIN
            assert rect.right() <= geom.right() - _MARGIN
            assert rect.top() >= geom.top() + _MARGIN
            assert rect.bottom() <= geom.bottom() - _MARGIN
    finally:
        indicator.hide()
        indicator.deleteLater()
        _close(qapp, preview)


def test_owner_saved_positions_on_a_missing_monitor_restore_primary_defaults(qapp, monkeypatch):
    from PySide6.QtWidgets import QApplication

    primary = QApplication.primaryScreen
    monkeypatch.setattr(QApplication, "screens", staticmethod(lambda: []))
    monkeypatch.setattr(QApplication, "primaryScreen", staticmethod(primary))
    preview = _StreamingWidget(False, IdleSettings(5.0, 0.25), lambda: False,
                               preview_position=OWNER_PREVIEW)
    indicator = ListeningIndicator()
    try:
        preview.show_overlay()
        indicator.set_custom_position(OWNER_INDICATOR["screen"], OWNER_INDICATOR["cx"], OWNER_INDICATOR["cy"])
        indicator.show()
        qapp.processEvents()
        assert preview._w._preview_position == streaming.PREVIEW_POSITION_DEFAULT
        assert indicator._custom_position is None
        assert indicator._corner == "bottom-center"
        _assert_default_geometry(qapp, preview, indicator)
    finally:
        indicator.hide()
        indicator.deleteLater()
        _close(qapp, preview)


def test_correction_choice_callbacks_do_not_crash_preview_construction(qapp):
    overlay = streaming.StreamingOverlayQt(idle=IdleSettings(5.0, 0.25))
    overlay.set_correction_choice_callbacks(lambda _choice: None, lambda: None)
    widget = overlay._new_widget()
    try:
        assert widget._w._on_choice is not None
        assert widget._w._on_choice_dismiss is not None
    finally:
        _close(qapp, widget)


def test_voice_reset_refuses_a_sentence_prefix(monkeypatch):
    app = _App()
    posted = []
    monkeypatch.setattr("samsara.ui.qt_runtime.post", lambda callback: posted.append(callback))
    assert core_utils.reset_floating_windows(app, remainder="later today") is False
    assert posted == []
    assert app.config["command_mode"]["preview_position"] == OWNER_PREVIEW
