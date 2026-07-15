"""Focused tests for Samsara's bundled, Torch-free Silero ONNX path."""

from pathlib import Path
from unittest.mock import Mock
import threading

import numpy as np

import dictation as _d


class _Stub:
    _load_vad_model = _d.DictationApp._load_vad_model
    _vad_probabilities = _d.DictationApp._vad_probabilities
    _vad_is_speech = _d.DictationApp._vad_is_speech
    _vad_reset = _d.DictationApp._vad_reset
    _buffer_has_contiguous_speech = _d.DictationApp._buffer_has_contiguous_speech
    _zcr_energy_contiguous_speech = _d.DictationApp._zcr_energy_contiguous_speech

    def __init__(self, model=None, available=True):
        self._vad_model = model
        self._vad_available = available
        self._vad_lock = threading.Lock()
        self.capture_rate = 16000


class _RecordingModel:
    def __init__(self, probabilities):
        self.probabilities = np.asarray(probabilities, dtype=np.float32)
        self.inputs = []

    def __call__(self, audio):
        self.inputs.append(np.asarray(audio).copy())
        return self.probabilities.copy()


def test_load_uses_bundled_asset_and_publishes_model_last(monkeypatch, tmp_path):
    import faster_whisper.utils as fw_utils
    import faster_whisper.vad as fw_vad

    asset = tmp_path / "silero_vad_v6.onnx"
    asset.write_bytes(b"model")
    sentinel = object()
    constructor = Mock(return_value=sentinel)
    monkeypatch.setattr(fw_utils, "get_assets_path", lambda: str(tmp_path))
    monkeypatch.setattr(fw_vad, "SileroVADModel", constructor)

    stub = _Stub(model=None, available=False)
    stub._load_vad_model()

    constructor.assert_called_once_with(str(asset))
    assert stub._vad_model is sentinel
    assert stub._vad_available is True


def test_missing_bundled_asset_falls_back_without_constructing(monkeypatch, tmp_path):
    import faster_whisper.utils as fw_utils
    import faster_whisper.vad as fw_vad

    constructor = Mock()
    monkeypatch.setattr(fw_utils, "get_assets_path", lambda: str(tmp_path))
    monkeypatch.setattr(fw_vad, "SileroVADModel", constructor)

    stub = _Stub(model=object(), available=False)
    stub._load_vad_model()

    constructor.assert_not_called()
    assert stub._vad_model is None
    assert stub._vad_available is False


def test_live_detection_flattens_float32_and_discards_partial_frame():
    model = _RecordingModel([0.1, 0.8, 0.1])
    stub = _Stub(model=model)
    audio = np.ones((1600, 1), dtype=np.float64)

    assert stub._vad_is_speech(audio, src_rate=16000) is True
    assert len(model.inputs) == 1
    assert model.inputs[0].shape == (1536,)
    assert model.inputs[0].dtype == np.float32


def test_live_detection_returns_false_for_short_or_low_probability_audio():
    model = _RecordingModel([0.2])
    stub = _Stub(model=model)
    assert stub._vad_is_speech(np.zeros(511, dtype=np.float32), 16000) is False
    assert model.inputs == []
    assert stub._vad_is_speech(np.zeros(512, dtype=np.float32), 16000) is False


def test_reset_is_noop_for_stateless_onnx_model():
    model = Mock()
    stub = _Stub(model=model)
    assert stub._vad_reset() is None
    assert not model.mock_calls


def test_gate_inference_failure_uses_zcr_fallback():
    model = Mock(side_effect=RuntimeError("onnx failure"))
    stub = _Stub(model=model)
    fallback = Mock(return_value=True)
    stub._zcr_energy_contiguous_speech = fallback
    audio = np.zeros(1024, dtype=np.float32)

    assert stub._buffer_has_contiguous_speech(audio, 16000, min_ms=64) is True
    fallback.assert_called_once_with(audio, 16000, min_ms=64)


def test_installed_bundled_onnx_model_runs_without_network():
    from faster_whisper.utils import get_assets_path
    from faster_whisper.vad import SileroVADModel

    asset = Path(get_assets_path()) / "silero_vad_v6.onnx"
    assert asset.is_file()
    model = SileroVADModel(str(asset))
    probabilities = np.asarray(model(np.zeros(512, dtype=np.float32))).reshape(-1)

    assert probabilities.shape == (1,)
    assert np.isfinite(probabilities).all()
    assert "CPUExecutionProvider" in model.session.get_providers()
