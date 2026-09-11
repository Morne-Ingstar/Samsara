import json
import sys
from types import SimpleNamespace

import pytest
from unittest.mock import Mock

import numpy as np

from tools import fallback_dictation as fd


def test_main_samsara_running_probes_default_profile_explicitly(monkeypatch):
    acquired = Mock()
    acquire = Mock(return_value=acquired)
    monkeypatch.setattr(fd.single_instance, "acquire_single_instance_mutex", acquire)

    assert fd.main_samsara_running() is False
    acquire.assert_called_once_with(profile_dir=None)
    acquired.close.assert_called_once_with()


def test_main_samsara_running_reports_mutex_collision(monkeypatch):
    def collision(*, profile_dir):
        assert profile_dir is None
        raise fd.single_instance.AlreadyRunningError("primary")

    monkeypatch.setattr(fd.single_instance, "acquire_single_instance_mutex", collision)

    assert fd.main_samsara_running() is True


def test_load_main_config_is_read_only_and_selective(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"hotkey": "alt", "model_size": "small", "unrelated": 9}), encoding="utf-8")
    before = path.read_bytes()

    config = fd.load_main_config(path)

    assert config["hotkey"] == "alt"
    assert config["model_size"] == "small"
    assert "unrelated" not in config
    assert path.read_bytes() == before


def test_resample_audio_preserves_duration():
    source = np.linspace(-1.0, 1.0, 48_000, dtype=np.float32)
    result = fd.resample_audio(source, 48_000, 16_000)
    assert result.dtype == np.float32
    assert len(result) == 16_000


def test_capture_waits_for_quiet_tail_and_keeps_final_speech():
    now = [10.0]
    capture = fd.CaptureBuffer(
        1_000,
        prebuffer_ms=30,
        silence_ms=60,
        max_tail_ms=200,
        speech_rms=0.01,
        clock=lambda: now[0],
    )
    capture.append(np.full(30, 0.02, dtype=np.float32))
    assert capture.start()
    capture.append(np.full(30, 0.04, dtype=np.float32))
    assert capture.release()

    capture.append(np.full(30, 0.03, dtype=np.float32))
    assert not capture.tail_ready()
    capture.append(np.zeros(30, dtype=np.float32))
    assert not capture.tail_ready()
    capture.append(np.zeros(30, dtype=np.float32))
    assert capture.tail_ready()

    audio = capture.finish()
    assert len(audio) == 150
    assert np.max(audio[-90:-60]) == np.float32(0.03)
    assert capture.state == "idle"


def test_capture_tail_has_hard_deadline_under_constant_noise():
    now = [5.0]
    capture = fd.CaptureBuffer(1_000, max_tail_ms=100, clock=lambda: now[0])
    capture.start()
    capture.release()
    capture.append(np.ones(30, dtype=np.float32))
    assert not capture.tail_ready()
    now[0] += 0.101
    assert capture.tail_ready()


def test_resolve_microphone_prefers_stable_name_over_stale_index():
    class FakeSoundDevice:
        devices = [
            {"name": "Wrong mic", "max_input_channels": 1, "default_samplerate": 44_100},
            {"name": "Focusrite", "max_input_channels": 2, "default_samplerate": 48_000},
        ]

        @classmethod
        def query_devices(cls, device=None, kind=None):
            if device is None and kind is None:
                return cls.devices
            chosen = 0 if device is None else device
            return cls.devices[chosen]

    device, rate, name = fd.resolve_microphone(
        {"microphone": 0, "microphone_name": "Focusrite"}, FakeSoundDevice
    )
    assert (device, rate, name) == (1, 48_000, "Focusrite")


def test_resolve_device_prefers_cuda_when_auto_supports_it(monkeypatch):
    class FakeCTranslate(SimpleNamespace):
        def get_supported_compute_types(self, _device):
            return ["int8", "float16", "float32", "cuda"]

    monkeypatch.setitem(sys.modules, "ctranslate2", FakeCTranslate())
    assert fd.resolve_device_and_compute({"device": "auto"}) == ("cuda", "float16")


def test_resolve_device_uses_cpu_when_cuda_not_available(monkeypatch):
    class FakeCTranslate(SimpleNamespace):
        def get_supported_compute_types(self, _device):
            return ["int8", "int16"]

    monkeypatch.setitem(sys.modules, "ctranslate2", FakeCTranslate())
    assert fd.resolve_device_and_compute({"device": "auto"}) == ("cpu", "int8")


def test_resolve_device_rejects_invalid_device():
    with pytest.raises(ValueError, match="Invalid device"):
        fd.resolve_device_and_compute({"device": "quantum"})


def test_resolve_microphone_surface_device_query_errors():
    class FailingSoundDevice:
        @staticmethod
        def query_devices(*_args, **_kwargs):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="Could not query available input devices"):
        fd.resolve_microphone({}, FailingSoundDevice())


def test_prepare_text_uses_conservative_cleanup_and_trailing_space():
    text = fd.prepare_text("this is a test", {"cleanup_mode": "clean", "add_trailing_space": True})
    assert text == "This is a test. "
