"""Device-rate routing tests with PortAudio fully faked."""

import collections
import threading
from unittest.mock import Mock

import numpy as np


class _FakeStream:
    def __init__(self, calls, **kwargs):
        self._calls = calls
        self.written = []
        self._calls.append({**kwargs, "_stream": self})

    def start(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def write(self, block):
        self.written.append(np.asarray(block).copy())


def _fake_output_api(monkeypatch, sd_module, calls, rate=48000):
    monkeypatch.setattr(
        sd_module,
        "query_devices",
        lambda device, kind: {"default_samplerate": rate},
    )
    monkeypatch.setattr(
        sd_module,
        "OutputStream",
        lambda **kwargs: _FakeStream(calls, **kwargs),
    )


def test_persistent_earcons_recache_and_open_at_selected_device_rate(monkeypatch):
    import dictation

    calls = []
    _fake_output_api(monkeypatch, dictation.sd, calls)
    app = object.__new__(dictation.DictationApp)
    app.output_device = 23
    app.output_device_name = "Headphones (Arctis Nova Pro Wireless)"
    app._sound_stream_sr = 44100
    app._sound_stream = None
    app._buffer_lock = threading.Lock()
    app._playback_buffer = np.ones((4, 1), dtype=np.float32)
    app._load_sound_cache = Mock()

    app._start_sound_stream()

    assert calls[0]["device"] == 23
    assert calls[0]["samplerate"] == 48000
    assert app._sound_stream_sr == 48000
    assert app._playback_buffer.shape == (0, 1)
    app._load_sound_cache.assert_called_once_with()


def test_edge_tts_resamples_pcm_to_selected_device_rate(monkeypatch):
    import sounddevice as sd
    from samsara.tts.edge_tts_engine import EdgeTTSEngine

    calls = []
    _fake_output_api(monkeypatch, sd, calls)
    engine = object.__new__(EdgeTTSEngine)
    engine._output_device = 23
    engine._lock = threading.Lock()
    engine._cancelled = False
    source = np.zeros(441, dtype=np.int16).tobytes()  # 10 ms at 44.1 kHz

    engine._play_pcm(source, sample_rate=44100)

    assert calls[0]["device"] == 23
    assert calls[0]["samplerate"] == 48000
    assert sum(len(block) for block in calls[0]["_stream"].written) == 480


def test_winrt_persistent_stream_opens_at_selected_device_rate(monkeypatch):
    import sounddevice as sd
    from samsara.tts.winrt_engine import WinRTEngine

    calls = []
    _fake_output_api(monkeypatch, sd, calls)
    engine = object.__new__(WinRTEngine)
    engine._tts_buffer = collections.deque()
    engine._tts_buffer_lock = threading.Lock()
    engine._tts_stream = None
    engine._using_persistent_stream = False
    engine._output_device = 23
    engine._output_sample_rate = 44100

    engine._open_persistent_stream()

    assert calls[0]["device"] == 23
    assert calls[0]["samplerate"] == 48000
    assert engine._output_sample_rate == 48000


def test_alarm_route_reloads_cache_and_plays_at_selected_device_rate(monkeypatch):
    from samsara import alarms

    monkeypatch.setattr(
        alarms.sd,
        "query_devices",
        lambda device, kind: {"default_samplerate": 48000},
    )
    played = []
    monkeypatch.setattr(
        alarms.sd,
        "play",
        lambda audio, rate, device=None: played.append((len(audio), rate, device)),
    )
    monkeypatch.setattr(alarms.sd, "wait", lambda: None)

    manager = object.__new__(alarms.AlarmManager)
    manager.output_device = None
    manager._sound_sample_rate = 44100
    manager._sound_cache = {"alarm": np.zeros(480, dtype=np.float32)}
    manager._load_sound_cache = Mock()
    manager.get_sound_for_alarm = Mock(
        return_value=np.zeros(480, dtype=np.float32)
    )

    manager.set_output_device(23)
    manager.play_sound({"sound": "alarm"})

    assert manager._sound_sample_rate == 48000
    manager._load_sound_cache.assert_called_once_with()
    assert played == [(480, 48000, 23)]
