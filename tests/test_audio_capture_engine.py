"""AudioCaptureEngine open/identity/teardown contract.

Companion to tests/test_audio_device_recovery.py, which owns the recovery
policy. This file covers what the engine must guarantee about OPENING a
device -- above all that it never hands sounddevice a bare device name,
which is unresolvable when the name exists under several host APIs and is
what broke mic recovery on 2026-09-12.

No real audio: sounddevice is faked per test.
"""
import sys
import types

import pytest

from tests.test_audio_device_recovery import FOCUSRITE, _OpenableSD, _dev


def _engine(sd, monkeypatch, config=None):
    from samsara.audio_engine.engine import AudioCaptureEngine

    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    epochs = types.SimpleNamespace(n=0)

    def _bump():
        epochs.n += 1
        return epochs.n

    ring = types.SimpleNamespace(write=lambda *a, **k: None, bump_device_epoch=_bump)
    engine = AudioCaptureEngine(ring=ring, config=config or {"microphone": 2})
    engine._epochs = epochs
    return engine


def _three_hostapis():
    return _OpenableSD([_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)])


class TestOpenByIndex:
    def test_open_records_identity_for_recovery(self, monkeypatch):
        sd = _three_hostapis()
        engine = _engine(sd, monkeypatch)

        engine._open_stream(2)

        assert engine._device_identity == (FOCUSRITE, "Windows WASAPI")
        assert engine._device_name == FOCUSRITE

    def test_identity_is_only_recorded_on_success(self, monkeypatch):
        sd = _OpenableSD([])
        engine = _engine(sd, monkeypatch)
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        with pytest.raises(Exception):
            engine._open_stream(99)

        assert engine._device_identity == (FOCUSRITE, "Windows WASAPI"), (
            "a failed open overwrote a good identity"
        )

    def test_native_rate_and_resample_ratio_come_from_the_device(self, monkeypatch):
        sd = _OpenableSD([_dev(FOCUSRITE, 2, rate=44100)])
        engine = _engine(sd, monkeypatch)

        engine._open_stream(0)

        assert engine._native_rate == 44100
        assert (engine._up, engine._down) == (160, 441)   # 16000/44100 reduced

    def test_rate_is_recomputed_per_open_not_cached(self, monkeypatch):
        """A recovered device can come back at a different native rate."""
        sd = _OpenableSD([_dev(FOCUSRITE, 2, rate=44100)])
        engine = _engine(sd, monkeypatch)
        engine._open_stream(0)
        assert engine._native_rate == 44100

        sd.devices = [_dev(FOCUSRITE, 2, rate=48000)]
        engine._open_stream(0)

        assert engine._native_rate == 48000
        assert (engine._up, engine._down) == (1, 3)


class TestNeverOpensByBareName:
    def test_resolve_returns_an_index_for_an_ambiguous_name(self, monkeypatch):
        engine = _engine(_three_hostapis(), monkeypatch)
        engine._device_identity = (FOCUSRITE, "Windows DirectSound")

        device, _note = engine._resolve_device()

        assert isinstance(device, int), (
            "recovery resolved to a bare name again -- this is the 2026-09-12 bug"
        )

    def test_a_bare_name_would_have_failed(self, monkeypatch):
        """Guards the premise: the fake rejects names exactly as sounddevice
        does, so a regression to name-passing fails loudly here."""
        sd = _three_hostapis()
        with pytest.raises(ValueError, match="Multiple input devices"):
            sd.InputStream(device=FOCUSRITE)

    def test_reopen_after_death_opens_the_recorded_index(self, monkeypatch):
        sd = _three_hostapis()
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._open_stream(1)          # DirectSound
        sd.opened.clear()

        assert engine._try_reopen(phase="test") is True
        assert sd.opened == [1], "reopened a different backend than the one recorded"


class TestStreamLifecycle:
    def test_stop_clears_running_and_recovering(self, monkeypatch):
        engine = _engine(_three_hostapis(), monkeypatch)
        engine._running = True
        engine._recovering = True

        engine.stop()

        assert engine._running is False
        assert engine._recovering is False

    def test_deliberate_stop_does_not_trigger_recovery(self, monkeypatch):
        """_on_stream_finished fires for every stop; only an unexpected one
        (with _running still True) may start recovery."""
        import samsara.audio_engine.engine as engine_mod

        engine = _engine(_three_hostapis(), monkeypatch)
        engine._running = False
        spawned = []
        monkeypatch.setattr(engine_mod.thread_registry, "spawn",
                            lambda name, target, **kw: spawned.append(name))

        engine._on_stream_finished()

        assert spawned == []

    def test_second_death_does_not_start_a_second_recovery_thread(self, monkeypatch):
        import samsara.audio_engine.engine as engine_mod

        engine = _engine(_three_hostapis(), monkeypatch)
        engine._running = True
        engine._recovering = True
        spawned = []
        monkeypatch.setattr(engine_mod.thread_registry, "spawn",
                            lambda name, target, **kw: spawned.append(name))

        engine._on_stream_finished()

        assert spawned == []

    def test_reopen_bumps_the_device_epoch(self, monkeypatch):
        """Consumers must see the discontinuity."""
        sd = _three_hostapis()
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")
        before = engine._epochs.n

        engine._try_reopen(phase="test")

        assert engine._epochs.n == before + 1


class TestDeviceLostFlag:
    def test_starts_clear(self, monkeypatch):
        assert _engine(_three_hostapis(), monkeypatch).device_lost is False

    def test_request_reconnect_now_is_public_and_idempotent(self, monkeypatch):
        engine = _engine(_three_hostapis(), monkeypatch)
        engine.request_reconnect_now()
        engine.request_reconnect_now()
        assert engine._reconnect_now.is_set()
