"""Queue 151: direct coverage for the extracted audio-helper module."""

from types import SimpleNamespace

import numpy as np

from samsara import audio_tools


def test_rms_coverage_and_silent_loss_backstop_keep_their_thresholds():
    silent = np.zeros(30 * 16_000, dtype=np.float32)
    voiced = np.full(30 * 16_000, 0.1, dtype=np.float32)

    assert audio_tools._speech_rms_coverage(silent, 16_000) == 0.0
    assert audio_tools._speech_rms_coverage(voiced, 16_000) == 1.0
    assert not audio_tools._suspected_silent_data_loss("hi", voiced, 16_000, 5.0)
    assert audio_tools._suspected_silent_data_loss("short", voiced, 16_000, 30.0)


def test_retry_runs_once_and_keeps_the_recovered_result():
    audio = np.full(30 * 16_000, 0.1, dtype=np.float32)
    original = audio_tools._HotkeyDecodeResult("short", False, [], "en", "short")
    recovered = original._replace(text="This retry contains enough words to pass the sanity check " * 3)
    calls = []

    result, suspected, retried = audio_tools._apply_retry_on_suspected_loss(
        original, lambda: calls.append(True) or recovered, audio, 16_000, 30.0, False)

    assert result is recovered
    assert (suspected, retried) == (False, True)
    assert calls == [True]


def test_split_fade_dump_and_resample_are_directly_importable(tmp_path, monkeypatch):
    audio = np.concatenate([
        np.full(16_000 * 20, 0.1, dtype=np.float32),
        np.zeros(16_000, dtype=np.float32),
        np.full(16_000 * 20, 0.1, dtype=np.float32),
    ])
    chunks = audio_tools._split_audio_at_silences(audio, 16_000)
    assert len(chunks) == 2
    assert np.array_equal(np.concatenate(chunks), audio)

    edge_input = np.ones(1_000, dtype=np.float32)
    faded = audio_tools._fade_edges(edge_input, 10_000, fade_ms=10)
    assert faded[0] == 0.0 and faded[-1] == 0.0
    assert np.array_equal(edge_input, np.ones(1_000, dtype=np.float32))

    monkeypatch.setattr(audio_tools, "Path", SimpleNamespace(home=lambda: tmp_path))
    audio_tools._dump_hotkey_buffer(np.array([0.0, 0.5], dtype=np.float32), 16_000)
    assert len(list((tmp_path / ".samsara" / "debug").glob("hotkey_*.wav"))) == 1

    resampled = audio_tools.resample_audio(np.arange(48_000, dtype=np.float32), 48_000, 16_000)
    assert resampled.dtype == np.float32 and len(resampled) == 16_000


def test_dictation_reexports_every_extracted_name():
    import dictation

    for name in (
        "_speech_rms_coverage", "_suspected_silent_data_loss",
        "_apply_retry_on_suspected_loss", "_split_audio_at_silences", "_fade_edges",
        "_dump_hotkey_buffer", "resample_audio", "_HotkeyDecodeResult",
    ):
        assert getattr(dictation, name) is getattr(audio_tools, name)
