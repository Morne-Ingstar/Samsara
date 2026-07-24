"""Regression tests for the Settings microphone dropdown's "System default"
option (root cause of an incident: a stale PortAudio index -- 29, a
Focusrite line input -- pointed the app at the wrong device, and there was
no way to select "just use whatever Windows considers default" instead of
a specific numbered device id).

Mirrors samsara/ui/mic_setup_wizard_qt.py's _populate_devices/
_on_refresh_devices, the reference implementation for this pattern: a
"System default" row at combo index 0 with userData=None.
"""
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_settings import _StubApp


_DEFAULT_MICS = [
    {"id": 1, "name": "Mic One"},
    {"id": 2, "name": "Mic Two"},
]


def _mic_stub(microphone=None, microphone_name=None, mics=_DEFAULT_MICS):
    stub = _StubApp()
    stub.config = {"microphone": microphone, "microphone_name": microphone_name}
    stub.available_mics = list(mics)
    return stub


class TestPopulate:
    def test_system_default_row_is_always_index_zero(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub())
        combo = win._widgets["mic_combo"]
        assert combo.itemText(0) == "System default"
        assert combo.itemData(0) is None

    def test_concrete_devices_listed_below_system_default(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub())
        combo = win._widgets["mic_combo"]
        assert combo.itemText(1) == "Mic One"
        assert combo.itemData(1) == 1
        assert combo.itemText(2) == "Mic Two"
        assert combo.itemData(2) == 2

    def test_system_default_row_present_even_with_no_devices(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub(mics=[]))
        combo = win._widgets["mic_combo"]
        assert combo.count() == 1
        assert combo.itemText(0) == "System default"


class TestRestore:
    def test_none_microphone_selects_system_default_row(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub(microphone=None))
        combo = win._widgets["mic_combo"]
        assert combo.currentIndex() == 0
        assert combo.currentText() == "System default"
        assert combo.currentData() is None

    def test_concrete_microphone_id_selects_its_row(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub(microphone=2))
        combo = win._widgets["mic_combo"]
        assert combo.currentText() == "Mic Two"
        assert combo.currentData() == 2

    def test_unknown_microphone_id_falls_back_to_system_default(self, qapp):
        """A stale id (e.g. after a device was unplugged/renumbered --
        exactly the 2026-07 incident's shape) must not silently land on
        whatever the first concrete device happens to be."""
        from samsara.ui.settings_qt import _SettingsWindow

        win = _SettingsWindow(_mic_stub(microphone=999))
        combo = win._widgets["mic_combo"]
        assert combo.currentIndex() == 0
        assert combo.currentData() is None


class TestSave:
    def test_selecting_system_default_saves_none(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = _mic_stub(microphone=1, microphone_name="Mic One")
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentIndex(0)

        general_updates = win._save_fns[0]({})

        assert general_updates["microphone"] is None
        assert general_updates["microphone_name"] == "System default"

    def test_selecting_a_concrete_device_saves_its_id(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = _mic_stub(microphone=None)
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentText("Mic Two")

        general_updates = win._save_fns[0]({})

        assert general_updates["microphone"] == 2
        assert general_updates["microphone_name"] == "Mic Two"


class TestSwitchOnApply:
    def _mic_stub_with_switch_tracking(self, microphone, microphone_name):
        stub = _mic_stub(microphone=microphone, microphone_name=microphone_name)
        stub.switch_observations = []

        def _switch_microphone(mic_id):
            stub.switch_observations.append((mic_id, stub.config.get("microphone")))
            stub.config["microphone"] = mic_id
            stub.config["microphone_name"] = (
                "System default" if mic_id is None
                else next((m["name"] for m in stub.available_mics if m["id"] == mic_id), None)
            )

        stub.switch_microphone = _switch_microphone
        return stub

    def test_switching_from_concrete_device_to_system_default(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = self._mic_stub_with_switch_tracking(microphone=1, microphone_name="Mic One")
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentIndex(0)  # System default

        win._apply_and_close()

        assert stub.switch_observations == [(None, 1)]
        assert stub.config["microphone"] is None
        assert stub.config["microphone_name"] == "System default"

    def test_switching_from_system_default_to_concrete_device(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = self._mic_stub_with_switch_tracking(microphone=None, microphone_name="System default")
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentText("Mic Two")

        win._apply_and_close()

        assert stub.switch_observations == [(2, None)]
        assert stub.config["microphone"] == 2
        assert stub.config["microphone_name"] == "Mic Two"

    def test_no_switch_when_system_default_reselected_unchanged(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow

        stub = self._mic_stub_with_switch_tracking(microphone=None, microphone_name="System default")
        win = _SettingsWindow(stub)
        win._widgets["mic_combo"].setCurrentIndex(0)  # already System default

        win._apply_and_close()

        assert stub.switch_observations == []
        assert stub.config["microphone"] is None


class TestRefreshPreservesSelection:
    def test_refresh_preserves_system_default_selection(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        from PySide6.QtWidgets import QPushButton

        stub = _mic_stub(microphone=None)
        stub._mic_refresh_blocked = Mock(return_value=False)
        stub.refresh_audio_devices = Mock(return_value=list(stub.available_mics))
        win = _SettingsWindow(stub)
        combo = win._widgets["mic_combo"]
        combo.setCurrentIndex(0)  # System default

        refresh_btn = win.findChild(QPushButton, "microphoneRefreshButton")
        refresh_btn.click()

        assert combo.currentIndex() == 0
        assert combo.currentText() == "System default"
        assert combo.currentData() is None

    def test_refresh_preserves_concrete_device_selection_by_name(self, qapp):
        from samsara.ui.settings_qt import _SettingsWindow
        from PySide6.QtWidgets import QPushButton

        stub = _mic_stub(microphone=1, microphone_name="Mic One")
        stub._mic_refresh_blocked = Mock(return_value=False)
        stub.refresh_audio_devices = Mock(return_value=list(stub.available_mics))
        win = _SettingsWindow(stub)
        combo = win._widgets["mic_combo"]
        combo.setCurrentText("Mic Two")

        refresh_btn = win.findChild(QPushButton, "microphoneRefreshButton")
        refresh_btn.click()

        assert combo.currentText() == "Mic Two"
        assert combo.currentData() == 2
