"""Offline generated MP3 equivalence and child-process regressions for 477."""
import builtins
import io
import shutil
import subprocess
import wave

import av
import numpy as np
import pytest
from pydub import AudioSegment

from samsara.tts.edge_tts_engine import _mp3_to_pcm
from samsara.tts.exceptions import RenderError
from samsara.tts import mp3_decode


@pytest.fixture(params=[(24000, 1, 44100), (44100, 2, 44100), (48000, 2, 16000)])
def encoded(request):
    rate, channels, target = request.param
    time = np.arange(int(rate * 0.37)) / rate
    signal = [0.2 * np.sin(2 * np.pi * 440 * time)
              + 0.08 * np.sin(2 * np.pi * 997 * time)]
    if channels == 2:
        signal.append(0.16 * np.sin(2 * np.pi * 660 * time))
    samples = np.asarray(signal, dtype=np.float32)
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp3") as container:
        stream = container.add_stream("libmp3lame", rate=rate)
        stream.layout = "mono" if channels == 1 else "stereo"
        for offset in range(0, samples.shape[1], 1024):
            frame = av.AudioFrame.from_ndarray(samples[:, offset:offset + 1024].copy(),
                                             format="fltp", layout=stream.layout)
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue(), rate, target


def test_matches_pydub(encoded, monkeypatch, record_property):
    # The offline reference subprocesses should not flash windows either.
    if mp3_decode.sys.platform == "win32":
        import pydub.utils
        original_popen = subprocess.Popen
        def hidden(*args, **kwargs):
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            return original_popen(*args, **kwargs)
        monkeypatch.setattr(subprocess, "Popen", hidden)
        monkeypatch.setattr(pydub.utils, "Popen", hidden)
    mp3, rate, target = encoded
    if not shutil.which(AudioSegment.converter):
        pytest.skip("pydub reference requires ffmpeg; PyAV/no-child tests still run")
    reference = AudioSegment.from_mp3(io.BytesIO(mp3)).set_frame_rate(target).set_channels(1).set_sample_width(2)
    pcm = _mp3_to_pcm(mp3, target)
    actual = AudioSegment(data=pcm, sample_width=2, frame_rate=target, channels=1)
    assert (actual.frame_rate, actual.channels, actual.sample_width) == (reference.frame_rate, reference.channels, reference.sample_width)
    frame_samples = 576 if rate <= 24000 else 1152
    assert abs(actual.frame_count() - reference.frame_count()) <= frame_samples * target / rate
    assert mp3_decode._pydub_fallback(mp3, target) == reference.raw_data
    a = np.frombuffer(pcm, dtype="<i2")
    b = np.frombuffer(reference.raw_data, dtype="<i2")
    length = min(len(a), len(b))
    correlation = float(np.corrcoef(a[:length], b[:length])[0, 1])
    record_property("correlation", correlation)
    record_property("sample_delta", len(a) - len(b))
    record_property("sample_rate", target)
    assert correlation > 0.995
    assert np.sqrt(np.mean(a[:length].astype(float) ** 2)) == pytest.approx(
        np.sqrt(np.mean(b[:length].astype(float) ** 2)), rel=0.03)


def test_decode_never_starts_child(encoded, monkeypatch):
    mp3, _, target = encoded
    def forbidden(*args, **kwargs):
        pytest.fail("child process reached during PyAV decode")
    for name in ("Popen", "call", "run", "check_call", "check_output"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setenv("PATH", "")
    pcm = _mp3_to_pcm(mp3, target)
    assert len(pcm) > target * 0.3 * 2
    assert len(pcm) % 2 == 0


def test_unavailable_pyav_uses_hidden_fallback(monkeypatch):
    original_import = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == "av":
            raise ImportError("PyAV unavailable")
        return original_import(name, *args, **kwargs)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setparams((1, 2, 44100, 8, "NONE", "not compressed"))
        output.writeframes(bytes(16))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, buffer.getvalue(), b"")
    monkeypatch.setattr(builtins, "__import__", unavailable)
    monkeypatch.setattr(subprocess, "run", run)
    assert _mp3_to_pcm(b"mp3") == bytes(16)
    argv, kwargs = calls[0]
    assert len(calls) == 1 and argv[0] == AudioSegment.converter
    assert kwargs["input"] == b"mp3" and kwargs["check"] is True
    if mp3_decode.sys.platform == "win32":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert kwargs["startupinfo"].wShowWindow == subprocess.SW_HIDE


def test_bad_mp3_is_render_error_without_fallback(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("decode failure must not fall back to a child")
    monkeypatch.setattr(mp3_decode, "_pydub_fallback", forbidden)
    with pytest.raises(RenderError, match="MP3 decode failed"):
        _mp3_to_pcm(b"not an MP3")



