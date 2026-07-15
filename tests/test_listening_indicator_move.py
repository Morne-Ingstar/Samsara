"""Tests for the listening indicator's draggable custom positioning.

Covers:
  - Normal mode remains click-through.
  - Move mode enables mouse input without stealing focus.
  - Dragging computes and emits a clamped, normalized placement.
  - A missing saved monitor falls back safely to the primary screen.
  - Label-width changes preserve the custom center (not just presets).
  - Preset positioning remains backward compatible.
  - dictation.py wiring: tray action enters move mode, placement_committed
    persists via the existing config save path.
  - Settings displays "Custom" and discards it when a preset is chosen.

Runs on the real "windows" QPA platform (see tests/conftest.py), so
QApplication.primaryScreen()/screens() reflect whatever monitor(s) this
machine actually has -- tests only assert relationships to that real
geometry, never a hardcoded resolution.
"""
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication


# ---------------------------------------------------------------------------
# ListeningIndicator widget -- click-through / move-mode / drag / custom pos
# ---------------------------------------------------------------------------

@pytest.fixture
def indicator(qapp):
    from samsara.ui.listening_indicator import ListeningIndicator
    ind = ListeningIndicator()
    yield ind
    ind.destroy()


class TestNormalModeClickThrough:
    def test_starts_click_through(self, indicator):
        assert bool(indicator.windowFlags() & Qt.WindowType.WindowTransparentForInput)

    def test_display_state_changes_preserve_click_through(self, indicator):
        indicator.set_mode("Listening...")
        indicator.set_listening(True)
        indicator.set_command_mode(True)
        indicator.flash_success()
        assert bool(indicator.windowFlags() & Qt.WindowType.WindowTransparentForInput)


class TestMoveModeEntryExit:
    def test_enter_move_mode_drops_transparent_for_input(self, indicator):
        indicator.enter_move_mode()
        flags = indicator.windowFlags()
        assert not (flags & Qt.WindowType.WindowTransparentForInput)
        assert indicator._unlocked is True

    def test_enter_move_mode_does_not_activate_or_take_focus(self, indicator):
        indicator.enter_move_mode()
        flags = indicator.windowFlags()
        # WindowDoesNotAcceptFocus must stay set throughout -- this is what
        # stops the pill from stealing keyboard focus while draggable.
        assert bool(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        assert indicator.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def test_enter_move_mode_shows_unlocked_appearance(self, indicator):
        indicator.enter_move_mode()
        _, _, label, _ = indicator._resolve_colors()
        assert label == "Drag to move"

    def test_exit_move_mode_restores_click_through(self, indicator):
        indicator.enter_move_mode()
        indicator.exit_move_mode()
        assert bool(indicator.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        assert indicator._unlocked is False

    def test_cancel_reverts_to_pre_move_placement(self, indicator):
        indicator.set_position("top-left")
        indicator.enter_move_mode()
        # Simulate an in-progress (uncommitted) change picked up mid-move.
        indicator._custom_position = {"screen": "SOMEWHERE", "cx": 0.1, "cy": 0.1}
        indicator._corner = None
        indicator.exit_move_mode(cancel=True)
        assert indicator._corner == "top-left"
        assert indicator._custom_position is None

    def test_double_enter_is_a_noop(self, indicator):
        indicator.enter_move_mode()
        flags_after_first = indicator.windowFlags()
        indicator.enter_move_mode()
        assert indicator.windowFlags() == flags_after_first
        assert indicator._unlocked is True


class TestDragCommit:
    def test_drag_commit_emits_normalized_custom_placement(self, indicator):
        screen = QApplication.primaryScreen()
        geom = screen.availableGeometry()

        indicator.show()
        indicator.enter_move_mode()
        indicator.move(geom.x() + geom.width() // 2, geom.y() + geom.height() // 2)

        received = []
        indicator.placement_committed.connect(received.append)

        indicator._dragging = True
        indicator._commit_drag_position()

        assert len(received) == 1
        payload = received[0]
        assert payload["type"] == "custom"
        assert payload["screen"] == screen.name()
        assert 0.0 <= payload["cx"] <= 1.0
        assert 0.0 <= payload["cy"] <= 1.0
        assert indicator._unlocked is False
        assert indicator._custom_position == {
            "screen": payload["screen"], "cx": payload["cx"], "cy": payload["cy"],
        }

    def test_drag_commit_clamps_to_available_geometry(self, indicator):
        screen = QApplication.primaryScreen()
        geom = screen.availableGeometry()

        indicator.show()
        indicator.enter_move_mode()
        # Drag far past the bottom-right edge of the monitor.
        indicator.move(geom.x() + geom.width() + 5000, geom.y() + geom.height() + 5000)

        indicator._dragging = True
        indicator._commit_drag_position()

        assert indicator.x() >= geom.x()
        assert indicator.y() >= geom.y()
        assert indicator.x() + indicator.width() <= geom.x() + geom.width()
        assert indicator.y() + indicator.height() <= geom.y() + geom.height()

    def test_drag_commit_clamps_negative_position(self, indicator):
        screen = QApplication.primaryScreen()
        geom = screen.availableGeometry()

        indicator.show()
        indicator.enter_move_mode()
        indicator.move(geom.x() - 5000, geom.y() - 5000)

        indicator._dragging = True
        indicator._commit_drag_position()

        assert indicator.x() >= geom.x()
        assert indicator.y() >= geom.y()


class TestCustomPositionFallback:
    def test_missing_saved_screen_falls_back_to_primary(self, indicator):
        indicator.set_custom_position("NONEXISTENT-SCREEN-ID", 0.5, 0.5)
        resolved = indicator._resolve_custom_screen()
        assert resolved is QApplication.primaryScreen()

    def test_missing_saved_screen_does_not_strand_offscreen(self, indicator):
        indicator.set_custom_position("NONEXISTENT-SCREEN-ID", 0.5, 0.5)
        indicator.show()
        geom = QApplication.primaryScreen().availableGeometry()
        assert geom.x() <= indicator.x() <= geom.x() + geom.width()
        assert geom.y() <= indicator.y() <= geom.y() + geom.height()

    def test_cx_cy_are_clamped_to_unit_range(self, indicator):
        indicator.set_custom_position("SOME-SCREEN", 1.5, -0.5)
        assert indicator._custom_position["cx"] == 1.0
        assert indicator._custom_position["cy"] == 0.0

    def test_none_cx_cy_does_not_crash(self, indicator):
        # A partial/malformed config record (key present, value null).
        indicator.set_custom_position("SOME-SCREEN", None, 0.5)
        assert indicator._custom_position is None

    def test_non_numeric_cx_cy_does_not_crash(self, indicator):
        indicator.set_custom_position("SOME-SCREEN", "not-a-number", 0.5)
        assert indicator._custom_position is None

    def test_non_finite_cx_cy_does_not_crash(self, indicator):
        indicator.set_custom_position("SOME-SCREEN", float("nan"), 0.5)
        assert indicator._custom_position is None
        indicator.set_custom_position("SOME-SCREEN", float("inf"), 0.5)
        assert indicator._custom_position is None

    def test_non_string_screen_name_is_ignored_not_crashed(self, indicator):
        indicator.set_custom_position(12345, 0.5, 0.5)
        assert indicator._custom_position["screen"] is None
        assert indicator._custom_position["cx"] == 0.5


class TestCustomCenterPreservedAcrossResize:
    def test_label_width_change_preserves_custom_center(self, indicator):
        screen = QApplication.primaryScreen()
        indicator.set_custom_position(screen.name(), 0.5, 0.5)
        indicator.show()

        cx_before = indicator.x() + indicator.width() / 2
        cy_before = indicator.y() + indicator.height() / 2

        indicator.set_mode("A Considerably Longer Mode Label Than Before")

        cx_after = indicator.x() + indicator.width() / 2
        cy_after = indicator.y() + indicator.height() / 2

        assert abs(cx_after - cx_before) <= 1
        assert abs(cy_after - cy_before) <= 1

    def test_set_listening_preserves_custom_center(self, indicator):
        screen = QApplication.primaryScreen()
        indicator.set_custom_position(screen.name(), 0.3, 0.7)
        indicator.show()

        cx_before = indicator.x() + indicator.width() / 2
        cy_before = indicator.y() + indicator.height() / 2

        indicator.set_listening(True)

        cx_after = indicator.x() + indicator.width() / 2
        cy_after = indicator.y() + indicator.height() / 2

        assert abs(cx_after - cx_before) <= 1
        assert abs(cy_after - cy_before) <= 1

    def test_flash_preserves_custom_center(self, indicator):
        screen = QApplication.primaryScreen()
        indicator.set_custom_position(screen.name(), 0.5, 0.5)
        indicator.show()

        cx_before = indicator.x() + indicator.width() / 2
        cy_before = indicator.y() + indicator.height() / 2

        indicator.flash_error()

        cx_after = indicator.x() + indicator.width() / 2
        cy_after = indicator.y() + indicator.height() / 2

        assert abs(cx_after - cx_before) <= 1
        assert abs(cy_after - cy_before) <= 1


class TestPresetPositionsBackwardCompatible:
    """Locks in the pre-existing six-preset formula for the primary screen."""

    def _expected_xy(self, corner, geom, pill_w, pill_h, m):
        cx = geom.x() + (geom.width() - pill_w) // 2
        if corner == "top-left":
            return geom.x() + m, geom.y() + m
        if corner == "top-center":
            return cx, geom.y() + m
        if corner == "top-right":
            return geom.x() + geom.width() - pill_w - m, geom.y() + m
        if corner == "bottom-left":
            return geom.x() + m, geom.y() + geom.height() - pill_h - m
        if corner == "bottom-right":
            return geom.x() + geom.width() - pill_w - m, geom.y() + geom.height() - pill_h - m
        return cx, geom.y() + geom.height() - pill_h - m  # bottom-center

    @pytest.mark.parametrize("corner", [
        "top-left", "top-center", "top-right",
        "bottom-left", "bottom-center", "bottom-right",
    ])
    def test_preset_matches_original_formula(self, indicator, corner):
        from samsara.ui.listening_indicator import _EDGE_MARGIN

        indicator.set_position(corner)
        indicator.show()

        geom = QApplication.primaryScreen().availableGeometry()
        expected = self._expected_xy(
            corner, geom, indicator.width(), indicator.height(), _EDGE_MARGIN
        )
        assert (indicator.x(), indicator.y()) == expected

    def test_set_position_clears_custom_position(self, indicator):
        screen = QApplication.primaryScreen()
        indicator.set_custom_position(screen.name(), 0.5, 0.5)
        assert indicator._custom_position is not None
        indicator.set_position("top-left")
        assert indicator._custom_position is None


class TestShutdownAndHiddenSafety:
    def test_hide_while_unlocked_restores_click_through(self, indicator):
        indicator.show()
        indicator.enter_move_mode()
        indicator.hide()
        assert bool(indicator.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        assert indicator._unlocked is False

    def test_destroy_while_unlocked_restores_click_through(self, indicator):
        indicator.show()
        indicator.enter_move_mode()
        indicator.destroy()
        assert bool(indicator.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        assert indicator._unlocked is False

    def test_enter_move_mode_on_hidden_indicator_is_safe(self, indicator):
        # Indicator never shown (e.g. listening_indicator_enabled=False).
        assert not indicator.isVisible()
        indicator.enter_move_mode()
        assert indicator.isVisible()
        assert indicator._unlocked is True
        indicator.exit_move_mode(cancel=True)
        # It was hidden before the move started -- must return to hidden.
        assert not indicator.isVisible()


# ---------------------------------------------------------------------------
# dictation.py wiring -- real methods bound onto a lightweight stub, matching
# the existing _make_stub pattern in test_session_badge.py / test_command_mode.py.
# ---------------------------------------------------------------------------

def _make_app_stub(config=None):
    import dictation as _d

    class _Stub:
        enter_indicator_move_mode = _d.DictationApp.enter_indicator_move_mode
        _on_indicator_placement_committed = _d.DictationApp._on_indicator_placement_committed
        update_config_and_save = _d.DictationApp.update_config_and_save

        def __init__(self):
            self.config = config or {}
            self._config_lock = threading.Lock()
            self.listening_indicator = Mock()
            self._running = True

        def _schedule_ui(self, func, *args):
            func(*args)

        def save_config(self):
            pass

    return _Stub()


class TestTrayEntersMoveMode:
    def test_enter_indicator_move_mode_calls_widget(self):
        stub = _make_app_stub()
        stub.enter_indicator_move_mode()
        stub.listening_indicator.enter_move_mode.assert_called_once()

    def test_enter_indicator_move_mode_safe_without_indicator(self):
        stub = _make_app_stub()
        stub.listening_indicator = None
        stub.enter_indicator_move_mode()  # must not raise


class TestPlacementPersistence:
    def test_custom_placement_persists_position_and_coordinates(self):
        stub = _make_app_stub()
        stub._on_indicator_placement_committed({
            "type": "custom", "screen": "\\\\.\\DISPLAY1", "cx": 0.25, "cy": 0.8,
        })
        assert stub.config["listening_indicator_position"] == "custom"
        assert stub.config["listening_indicator_custom_position"] == {
            "screen": "\\\\.\\DISPLAY1", "cx": 0.25, "cy": 0.8,
        }

    def test_preset_placement_persists_position_only(self):
        stub = _make_app_stub()
        stub._on_indicator_placement_committed({"type": "preset", "position": "top-left"})
        assert stub.config["listening_indicator_position"] == "top-left"
        assert "listening_indicator_custom_position" not in stub.config


# ---------------------------------------------------------------------------
# Settings tab -- displays "Custom" and discards it on a preset switch.
# ---------------------------------------------------------------------------

class _SettingsStubApp:
    """Minimal app stand-in for headlessly constructing _SettingsWindow --
    mirrors tests/test_settings.py's _StubApp (kept local so this file has
    no cross-test-module import)."""

    def __init__(self, config=None):
        self.config = config or {}
        self._config_lock = threading.Lock()
        self.command_executor = SimpleNamespace(commands={}, find_command=lambda p: None)
        self.hints = None
        self.alarm_manager = None

    def play_sound(self, *a, **k):
        pass

    def save_config(self):
        pass

    def load_commands(self):
        return {}

    def load_training_data(self):
        pass

    def _load_sound_cache(self):
        pass


class TestSettingsCustomPosition:
    def test_displays_custom_when_position_is_custom(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _SettingsStubApp({"listening_indicator_position": "custom"})
        win = _SettingsWindow(stub)
        combo = win._widgets["adv_indicator_pos"]
        assert combo.currentText() == "custom"

    def test_custom_not_offered_when_not_active(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _SettingsStubApp({"listening_indicator_position": "bottom-center"})
        win = _SettingsWindow(stub)
        combo = win._widgets["adv_indicator_pos"]
        items = [combo.itemText(i) for i in range(combo.count())]
        assert "custom" not in items
        assert combo.currentText() == "bottom-center"

    def test_switching_to_preset_discards_custom_position(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _SettingsStubApp({
            "listening_indicator_position": "custom",
            "listening_indicator_custom_position": {"screen": "X", "cx": 0.5, "cy": 0.5},
        })
        win = _SettingsWindow(stub)
        combo = win._widgets["adv_indicator_pos"]
        combo.setCurrentText("bottom-center")

        advanced_save_fn = win._save_fns[7]
        produced = advanced_save_fn({})

        assert produced["listening_indicator_position"] == "bottom-center"
        assert produced["listening_indicator_custom_position"] is None

    def test_keeping_custom_does_not_touch_custom_position_key(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _SettingsStubApp({
            "listening_indicator_position": "custom",
            "listening_indicator_custom_position": {"screen": "X", "cx": 0.5, "cy": 0.5},
        })
        win = _SettingsWindow(stub)
        # Combo already shows "custom"; leave it untouched.
        advanced_save_fn = win._save_fns[7]
        produced = advanced_save_fn({})

        assert produced["listening_indicator_position"] == "custom"
        assert "listening_indicator_custom_position" not in produced

    def test_no_stored_custom_position_no_clear_written(self, qapp):
        """Regression guard for the apply-and-close snapshot test: a fresh/
        default config has no custom_position to discard, so _save must not
        introduce the key at all."""
        from samsara.ui.settings_qt import _SettingsWindow
        stub = _SettingsStubApp({"listening_indicator_position": "bottom-center"})
        win = _SettingsWindow(stub)
        advanced_save_fn = win._save_fns[7]
        produced = advanced_save_fn({})
        assert "listening_indicator_custom_position" not in produced
