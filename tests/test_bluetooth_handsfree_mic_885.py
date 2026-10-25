"""885: Bluetooth capture inputs survive filtering and first-run setup."""

from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QComboBox

from samsara import audio_devices
from samsara.ui import first_run_wizard_qt as wizard
from samsara.ui.settings.general_qt import (
    _microphone_updates,
    _populate_microphone_combo,
)


BLUETOOTH_NAMES = (
    "Headset (Example Headphones Hands-Free AG Audio)",
    "Headset (Example Headphones HF Audio)",
)


def _input(name, hostapi=0, channels=1):
    return {"name": name, "hostapi": hostapi, "max_input_channels": channels}


@pytest.fixture
def devices(monkeypatch):
    records = []
    monkeypatch.setattr(audio_devices, "_ensure_com_initialized", lambda: False)
    monkeypatch.setattr(audio_devices.sd, "query_hostapis", lambda: [
        {"name": "Windows WASAPI"}, {"name": "MME"},
    ])
    monkeypatch.setattr(audio_devices.sd, "query_devices", lambda *a, **k: (
        records[0] if a or k else records
    ))
    return records


@pytest.mark.parametrize("name", BLUETOOTH_NAMES)
def test_default_list_keeps_bluetooth_capture_input(devices, name):
    devices.append(_input(name))
    assert audio_devices.list_microphones() == [
        {"id": 0, "name": name, "channels": 1},
    ]


def test_filter_still_excludes_virtual_loopback_output_and_other_apis(devices):
    # Bluetooth words must not override a genuine exclusion in the same name.
    devices.extend(_input(name) for name in (
        "Stereo Mix", "Wave Out Mix", "What U Hear", "WASAPI Loopback",
        "CABLE Output", "Virtual Audio Input", "VB-Audio Input",
        "VoiceMeeter Input", "SteelSeries Sonar Microphone", "OculusVAD",
        "Headset Hands-Free AG Audio Loopback", "Virtual Audio HF Audio",
        "Microphone ()", "@System32\\drivers\\synthetic microphone",
    ))
    devices.extend([
        _input(BLUETOOTH_NAMES[0], channels=0),  # output-only Bluetooth
        _input(BLUETOOTH_NAMES[1], hostapi=1),  # non-preferred API
        _input("USB Microphone", channels=2),
        _input("usb microphone", channels=2),  # deduplicated as before
    ])
    assert audio_devices.list_microphones() == [
        {"id": len(devices) - 2, "name": "USB Microphone", "channels": 2},
    ]
    assert all(m["channels"] > 0 for m in audio_devices.list_microphones(True))


@pytest.fixture
def setup_window(qapp, monkeypatch, tmp_path, devices):
    monkeypatch.setattr(wizard.thread_registry, "spawn", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "register_notifications", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "capture_endpoints", lambda: ())
    monkeypatch.setattr(wizard, "force_rescan", Mock())
    stream = Mock()
    open_stream = Mock(return_value=stream)
    monkeypatch.setattr(audio_devices.sd, "InputStream", open_stream)
    win = wizard._WizardWindow(tmp_path / "config.json")
    win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    win._load_mics()  # real enumeration, synchronously instead of a worker
    win.show()
    win._go_next()
    qapp.processEvents()
    try:
        yield win, open_stream
    finally:
        win.close()
        win.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qapp.processEvents()


@pytest.mark.parametrize("name", BLUETOOTH_NAMES)
@pytest.mark.parametrize("refresh", [False, True], ids=["initial", "refresh"])
def test_setup_selects_and_continues_with_bluetooth(
    devices, setup_window, monkeypatch, name, refresh,
):
    win, open_stream = setup_window
    # Entered setup with no devices: both paths must recover the sole input.
    assert not win._next_btn.isEnabled()
    devices.append(_input(name))
    if refresh:
        monkeypatch.setattr(wizard.thread_registry, "spawn",
                            lambda label, target, **kwargs: target())
        win._on_refresh_mics_clicked()
        wizard.force_rescan.assert_called_once_with()
    else:
        win._load_mics()
    assert win._mic_combo.currentText() == name
    assert win._get_current_mic_id() == 0
    open_stream.assert_called_once()
    assert open_stream.call_args.kwargs["device"] == 0
    open_stream.return_value.start.assert_called_once_with()
    assert win._mic_capture_ready
    assert win._next_btn.isEnabled()
    win._next_btn.click()
    assert wizard._STEPS[win._step][0] == "Components"
    assert win._config["microphone"] == 0
    open_stream.return_value.stop.assert_called_once_with()
    open_stream.return_value.close.assert_called_once_with()


def test_bluetooth_still_needs_a_stream_that_opens(devices, setup_window):
    win, open_stream = setup_window
    devices.append(_input(BLUETOOTH_NAMES[0]))
    open_stream.side_effect = RuntimeError("synthetic stream open failure")
    win._load_mics()
    assert win._mic_combo.currentText() == BLUETOOTH_NAMES[0]
    open_stream.assert_called_once()
    assert not win._mic_capture_ready
    assert not win._next_btn.isEnabled()
    win._go_next()
    assert wizard._STEPS[win._step][0] == "Microphone"


@pytest.mark.parametrize("name", BLUETOOTH_NAMES)
def test_settings_combo_keeps_same_filtered_bluetooth_input(qapp, devices, name):
    devices.extend([_input(name), _input("Virtual Audio Input"),
                    _input("Headphones", channels=0)])
    combo = QComboBox()
    config = {"microphone": 0, "microphone_name": name}
    try:
        _populate_microphone_combo(combo, audio_devices.list_microphones(), config)
        assert [combo.itemText(i) for i in range(combo.count())] == [
            "System default", name,
        ]
        assert combo.currentText() == name
        assert _microphone_updates(combo) == config
    finally:
        combo.deleteLater()
