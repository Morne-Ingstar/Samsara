"""Focused regression tests for the microphone setup/runtime handoff."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np


class _WizardApp:
    def __init__(self):
        self.config = {
            "microphone": 1,
            "microphone_name": "Old mic",
            "wake_word_config": {"phrase": "jarvis", "oww_threshold": 0.2},
        }
        self.available_mics = [
            {"id": 1, "name": "Old mic"},
            {"id": 2, "name": "New mic"},
        ]
        self.switch_observations = []
        self.saved_updates = []
        self.calibration_calls = []

    def get_available_microphones(self):
        return list(self.available_mics)

    def switch_microphone(self, mic_id):
        self.switch_observations.append((mic_id, self.config.get("microphone")))
        self.config["microphone"] = mic_id
        match = next((m for m in self.available_mics if m["id"] == mic_id), None)
        if match:
            self.config["microphone_name"] = match["name"]

    def update_config_and_save(self, updates):
        self.saved_updates.append(dict(updates))
        self.config.update(updates)

    def calibrate_wake_mic(self, *, seconds, cancel_event):
        self.calibration_calls.append((seconds, cancel_event))
        return 0.012


def _window(monkeypatch):
    from samsara.ui import mic_setup_wizard_qt as wizard

    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    app = _WizardApp()
    return wizard, app, wizard._WizardWindow(app)


def test_guide_switches_runtime_before_persisting_new_selection(qapp, monkeypatch):
    _, app, window = _window(monkeypatch)
    window._device_combo.setCurrentIndex(window._device_combo.findData(2))

    assert window._apply_selected_microphone() is True

    assert app.switch_observations == [(2, 1)]
    assert app.config["microphone"] == 2
    assert app.config["microphone_name"] == "New mic"


def test_guide_same_device_refreshes_persisted_id_and_name(qapp, monkeypatch):
    _, app, window = _window(monkeypatch)

    assert window._apply_selected_microphone() is True

    assert app.switch_observations == []
    assert app.saved_updates[-1] == {
        "microphone": 1,
        "microphone_name": "Old mic",
    }


class _JoinableThread:
    def __init__(self, order, alive_after_join=False):
        self._order = order
        self._alive = True
        self._alive_after_join = alive_after_join

    def join(self, timeout=None):
        self._order.append(("join", timeout))
        self._alive = self._alive_after_join

    def is_alive(self):
        return self._alive


def test_device_next_joins_the_preview_worker_before_switching_off_the_ui_thread(qapp, monkeypatch):
    wizard, app, window = _window(monkeypatch)
    order = []
    window._audio_thread = _JoinableThread(order)
    real_switch = app.switch_microphone
    app.switch_microphone = lambda mic_id: (order.append(("switch", mic_id)), real_switch(mic_id))
    spawned = {}
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.update(name=name, target=target))
    window._device_combo.setCurrentIndex(window._device_combo.findData(2))

    window._go_next()

    # Worker joined on the Qt thread; the switch itself has NOT run yet.
    assert [o[0] for o in order] == ["join"]
    assert spawned["name"] == "wizard-mic-switch"
    assert window._switch_in_flight is True
    assert not window._next_btn.isEnabled()
    assert not window._back_btn.isEnabled()
    assert not window._skip_btn.isEnabled()
    window._go_next()                                      # a second click is ignored
    assert [o[0] for o in order] == ["join"]

    spawned["target"]()                                    # the worker thread

    assert order == [order[0], ("switch", 2)]
    assert window._switch_in_flight is False
    assert window._current_step == window._STEP_LEVEL
    assert window._back_btn.isEnabled() and window._skip_btn.isEnabled()
    assert app.config["microphone"] == 2


def test_audio_is_not_restarted_while_the_switch_is_in_flight(qapp, monkeypatch):
    from samsara.ui import mic_setup_wizard_qt as wizard

    started = []
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: started.append(name))
    app = _WizardApp()
    window = wizard._WizardWindow(app)                     # real _ensure_audio_running
    started.clear()
    window._wizard_active = False
    window._switch_in_flight = True
    window._ensure_audio_running()
    assert started == []


def test_preview_worker_that_will_not_exit_blocks_the_switch(qapp, monkeypatch):
    wizard, app, window = _window(monkeypatch)
    order = []
    window._audio_thread = _JoinableThread(order, alive_after_join=True)
    spawned = []
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.append(name))
    window._device_combo.setCurrentIndex(window._device_combo.findData(2))

    window._go_next()

    assert spawned == [], "never switch while the preview stream may still be open"
    assert app.switch_observations == []
    assert window._switch_in_flight is False
    assert window._next_btn.isEnabled()
    assert window._current_step == window._STEP_DEVICE
    assert "did not stop" in window._device_status.text()


def test_failed_switch_reports_and_stays_on_the_device_step(qapp, monkeypatch):
    wizard, app, window = _window(monkeypatch)
    spawned = {}
    monkeypatch.setattr(wizard.thread_registry, "spawn",
                        lambda name, target, daemon=True: spawned.update(target=target))

    def boom(mic_id):
        raise RuntimeError("device vanished")

    app.switch_microphone = boom
    window._device_combo.setCurrentIndex(window._device_combo.findData(2))
    window._go_next()
    spawned["target"]()

    assert window._current_step == window._STEP_DEVICE
    assert window._next_btn.isEnabled()
    assert "Could not switch microphones" in window._device_status.text()


def test_wake_step_uses_production_three_second_quiet_calibration(qapp, monkeypatch):
    wizard, app, window = _window(monkeypatch)
    spawned = {}
    monkeypatch.setattr(
        wizard.thread_registry,
        "spawn",
        lambda name, target, daemon=True: spawned.update(
            name=name, target=target, daemon=daemon
        ),
    )
    setup = Mock()
    window._setup_oww_test = setup
    window._current_step = window._STEP_WAKE

    window._begin_wake_calibration()
    spawned["target"]()

    assert spawned["name"] == "wizard-wake-calibration"
    assert spawned["daemon"] is True
    assert len(app.calibration_calls) == 1
    seconds, cancel_event = app.calibration_calls[0]
    assert seconds == 3.0
    assert isinstance(cancel_event, threading.Event)
    assert setup.call_count == 1
    assert "Background level calibrated" in window._oww_result_lbl.text()


def test_closing_guide_cancels_pending_wake_calibration(qapp, monkeypatch):
    wizard, _, window = _window(monkeypatch)
    spawned = {}
    monkeypatch.setattr(
        wizard.thread_registry,
        "spawn",
        lambda name, target, daemon=True: spawned.update(target=target),
    )
    window._current_step = window._STEP_WAKE
    window._begin_wake_calibration()
    cancel_event = window._wake_cal_cancel

    window.close()
    qapp.processEvents()

    assert cancel_event.is_set()
    assert window._wake_cal_cancel is None


def test_audio_worker_debounces_adjacent_positive_frames_until_silence(
    qapp, monkeypatch
):
    wizard, _, window = _window(monkeypatch)
    high = np.full((1600, 1), 0.05, dtype=np.float32)
    quiet = np.zeros((1600, 1), dtype=np.float32)
    chunks = [high, high, high, high, quiet, quiet, quiet, high]

    class _Stream:
        def __init__(self):
            self._index = 0

        def start(self):
            pass

        def read(self, blocksize):
            chunk = chunks[self._index]
            self._index += 1
            if self._index == len(chunks):
                window._wizard_active = False
            return chunk, False

        def stop(self):
            pass

        def close(self):
            pass

    detector = SimpleNamespace(
        detected=Mock(return_value=True),
        reset=Mock(),
    )
    monkeypatch.setattr(wizard.sd, "InputStream", lambda **kwargs: _Stream())
    monkeypatch.setattr(
        wizard.sd,
        "query_devices",
        lambda device: {"default_samplerate": 16000},
    )
    sleep = Mock()
    monkeypatch.setattr(wizard.time, "sleep", sleep)
    hits = []
    window._oww_hit_sig.connect(lambda: hits.append(True))
    window._current_step = window._STEP_WAKE
    window._oww_running = True
    window._oww_detector = detector
    window._oww_armed = True
    window._wizard_active = True

    window._audio_worker()

    assert len(hits) == 2
    assert detector.detected.call_count == 2
    assert detector.reset.call_count == 1
    sleep.assert_not_called()


def test_cancelled_production_calibration_unregisters_without_persisting():
    from dictation import DictationApp

    class _Reader:
        def rewind(self, _frames):
            raise AssertionError("quiet calibration must not rewind old audio")

        def read_next(self):
            raise AssertionError("pre-cancelled calibration must not read")

    reader = _Reader()

    class _Engine:
        _running = True

        def __init__(self):
            self.unregistered = []

        def register_consumer(self, name):
            assert name == "wake-calibration"
            return reader

        def unregister_consumer(self, registered):
            self.unregistered.append(registered)

    app = object.__new__(DictationApp)
    app._ace_engine = _Engine()
    app._wake_noise_floor = 0.07
    app.config = {"wake_word_config": {"audio": {"measured_noise_floor": 0.07}}}
    app._config_lock = threading.Lock()
    app.persist_config = Mock()
    cancel = threading.Event()
    cancel.set()

    result = app.calibrate_wake_mic(seconds=3.0, cancel_event=cancel)

    assert result is None
    assert app._ace_engine.unregistered == [reader]
    assert app._wake_noise_floor == 0.07
    assert app.config["wake_word_config"]["audio"]["measured_noise_floor"] == 0.07
    app.persist_config.assert_not_called()
