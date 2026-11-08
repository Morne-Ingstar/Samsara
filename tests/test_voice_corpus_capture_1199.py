"""Exact endpoint selection and production PCM identity without hardware or profiles."""
import json
import sys
from types import SimpleNamespace as NS

import numpy as np
import pytest

from tools.voice_corpus.audio import Capture, DailyCapture, settings_snapshot


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key in ("HOME", "USERPROFILE", "SAMSARA_HOME_DIR", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(key, str(tmp_path))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")


class Sound:
    def __init__(self, rate=48000):
        self.apis = [{"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}]
        self.devices = [dict(name="Chosen mic", hostapi=0, max_input_channels=2, default_samplerate=rate),
                        dict(name="Other mic", hostapi=1, max_input_channels=1, default_samplerate=rate),
                        dict(name="Chosen mic", hostapi=1, max_input_channels=2, default_samplerate=rate)]
        self.opened = []
        self.checked = []
        self.closed = False

    def query_hostapis(self):
        return self.apis

    def query_devices(self, device=None, kind=None):
        return self.devices if device is None else self.devices[device]

    def check_input_settings(self, **kwargs):
        self.checked.append(kwargs)

    def InputStream(self, **kwargs):
        self.opened.append(kwargs)
        stream = NS(device=kwargs["device"], samplerate=kwargs["samplerate"])
        # Special methods are looked up on the type, so use a small real class.
        owner = self
        class Stream:
            device = stream.device
            samplerate = stream.samplerate
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                owner.closed = True
        return Stream()


def fake(monkeypatch, rate=48000):
    sd = Sound(rate)
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


def test_exact_name_api_beats_stale_index_and_prints_opened_receipt(monkeypatch, capsys):
    sd = fake(monkeypatch)
    with DailyCapture(dict(microphone_name="Chosen mic", microphone=1)) as capture:
        assert capture.device_id == 2
        assert capture.native_rate == 48000
        assert sd.opened[0]["device"] == sd.checked[0]["device"] == 2
        assert sd.opened[0]["blocksize"] == 4800
        assert sd.opened[0]["channels"] == 1
        assert sd.opened[0]["dtype"] == "float32"
        assert "Windows WASAPI" in capture.receipt
    assert sd.closed
    assert "host API=Windows WASAPI; input=48000 Hz" in capsys.readouterr().out


@pytest.mark.parametrize("failure", ["missing_name", "missing_api", "output_only", "ambiguous", "default"])
def test_no_fallback_or_stream_when_identity_is_not_unique(monkeypatch, failure):
    sd = fake(monkeypatch)
    settings = dict(microphone_name="Chosen mic", microphone=1)
    if failure == "missing_name":
        settings.pop("microphone_name")
    elif failure == "default":
        settings["microphone_name"] = "System default"
    elif failure == "missing_api":
        sd.devices.pop()
    elif failure == "output_only":
        sd.devices[2]["max_input_channels"] = 0
    else:
        sd.devices.append(dict(sd.devices[2]))
    with pytest.raises(ValueError):
        DailyCapture(settings).__enter__()
    assert not sd.opened


def test_explicit_api_and_rate_follow_snapshot_index_rule(monkeypatch):
    sd = fake(monkeypatch)
    with DailyCapture(dict(microphone_name="Chosen mic", microphone_hostapi="Windows DirectSound",
                           microphone=0, _capture_rate=44100)) as capture:
        assert capture.device_id == 0 and capture.native_rate == 44100
    with DailyCapture(dict(microphone_name="Chosen mic", microphone=0, _capture_rate=44100)) as capture:
        assert capture.device_id == 2 and capture.native_rate == 48000


@pytest.mark.parametrize("field,value", [("device", 1), ("samplerate", 16000)])
def test_unexpected_opened_stream_is_closed(monkeypatch, field, value):
    sd = fake(monkeypatch)
    original = sd.InputStream
    def changed(**kwargs):
        stream = original(**kwargs)
        setattr(stream, field, value)
        return stream
    sd.InputStream = changed
    with pytest.raises(ValueError, match="unexpected device or rate"):
        DailyCapture(dict(microphone_name="Chosen mic")).__enter__()
    assert sd.closed


@pytest.mark.parametrize("rate", [16000, 44100, 48000])
def test_production_callback_samples_preroll_tail_and_clipping_are_identical(monkeypatch, rate):
    fake(monkeypatch, rate)
    from samsara.audio_engine.engine import AudioCaptureEngine, _polyphase_resample
    from samsara.audio_engine.frame import FRAME_SIZE
    called = []
    original = AudioCaptureEngine._on_audio_block
    def observed(self, *args):
        called.append(True)
        return original(self, *args)
    monkeypatch.setattr(AudioCaptureEngine, "_on_audio_block", observed)
    with DailyCapture(dict(microphone_name="Chosen mic")) as capture:
        rng = np.random.default_rng(1199)
        blocks = [rng.uniform(-.2, .2, (capture.blocksize, 1)).astype(np.float32) for _ in range(3)]
        blocks[1][:50] = -2
        blocks[1][100:150] = 2
        expected = []
        for block in blocks:
            state = capture._processor._resample_state
            converted = (_polyphase_resample(block[:, 0], capture._processor._up,
                         capture._processor._down, *state) if state else block[:, 0])
            expected.append(np.clip(converted * 32767, -32768, 32767).astype(np.int16))
        capture._callback(blocks[0], capture.blocksize, None, None)
        capture.start_take()
        capture._callback(blocks[1], capture.blocksize, None, True)
        capture._callback(blocks[2], capture.blocksize, None, None)
        capture.tail_done.set()
        pcm, release, statuses = capture.release()
        np.testing.assert_array_equal(pcm, np.concatenate(expected))
        assert len(pcm) == FRAME_SIZE * 3
        assert -32768 in pcm and 32767 in pcm
        assert release >= .1 and statuses == ["input overflow or stream warning"]
        assert len(called) == 3


def test_default_capture_still_requests_16k_without_native_blocksize(monkeypatch):
    sd = fake(monkeypatch)
    with Capture(dict(microphone=0)):
        assert sd.opened[0]["samplerate"] == 16000
        assert "blocksize" not in sd.opened[0]


def test_runtime_snapshot_alias_and_explicit_fields(tmp_path):
    path = tmp_path / "offline.json"
    path.write_text(json.dumps(dict(microphone_index=27, microphone_name="Chosen mic",
                                   microphone_hostapi="Windows WASAPI", _capture_rate=48000,
                                   unrelated="excluded")))
    settings = settings_snapshot(path)
    assert settings == dict(microphone=27, microphone_name="Chosen mic",
                            microphone_hostapi="Windows WASAPI", _capture_rate=48000)


def test_cli_passes_daily_path_and_keeps_default(tmp_path, monkeypatch):
    from tools.voice_corpus import recorder
    from tools.voice_corpus.__main__ import main
    calls = []
    monkeypatch.setattr(recorder, "run", lambda root, settings, **kw: calls.append(kw) or 0)
    for extra in ([], ["--capture-path", "daily"]):
        assert main(["record", "--corpus", str(tmp_path / "corpus"), *extra]) == 0
    assert calls == [dict(capture_path="default"), dict(capture_path="daily")]


def test_saved_take_keeps_receipt_and_index(tmp_path):
    from PySide6.QtWidgets import QApplication
    from tools.voice_corpus.recorder import RecorderWindow
    from tools.voice_corpus.storage import Session
    from tools.voice_corpus.script import SCRIPT
    app = QApplication.instance() or QApplication([])
    capture = NS(settings={}, device={"name": "Chosen mic"}, device_id=2,
                 receipt="capture-path=daily; host API=Windows WASAPI; input=48000 Hz",
                 release=lambda: (np.ones(1600, dtype=np.int16), .05, []))
    session = Session(tmp_path / "corpus", SCRIPT[:1])
    window = RecorderWindow(session, capture)
    assert "Daily match unverified" in window.device_label.text()
    assert window.finish().startswith("Saved")
    row = session.selected()[0]
    assert row["device_id"] == 2 and row["capture_status"] == [capture.receipt]
    window.close()
    app.processEvents()
