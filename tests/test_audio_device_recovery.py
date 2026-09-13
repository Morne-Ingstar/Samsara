"""Mic-loss recovery when the device name is ambiguous (2026-09-12 incident).

Live evidence, C:\\Users\\Morne\\.samsara\\logs\\samsara.log 18:57:39-18:58:39:

    [ACE] Input stream died unexpectedly -- entering recovery
    [ACE] Opening stream: device='Analogue 1 + 2 (Focusrite USB Audio)' ...
    [ACE] Recovery attempt failed, retrying in 2s: Multiple input devices
          found for 'Analogue 1 + 2 (Focusrite USB Audio)':
          [.. MME] / [.. Windows DirectSound]
    (30 identical attempts) ... [ACE] Recovery gave up after 60s

Boot opened the SAME device as device=30 -- an index -- and worked. Recovery
passed a bare name, which sounddevice cannot resolve when the name exists
under several host APIs, so all 30 attempts failed identically while the
device was present the whole time.

No real audio: sounddevice is a fake enumeration injected per test.
"""
import threading
import time

import pytest

from samsara.audio_engine import device_resolver as dr

FOCUSRITE = "Analogue 1 + 2 (Focusrite USB Audio)"


class FakeSD:
    """Minimal sounddevice stand-in: an ordered device table + host APIs."""

    def __init__(self, devices=None, hostapis=("MME", "Windows DirectSound", "Windows WASAPI")):
        self.hostapis = list(hostapis)
        self.devices = list(devices if devices is not None else [])
        self.query_count = 0

    def query_hostapis(self):
        return [{"name": name} for name in self.hostapis]

    def query_devices(self, device=None, kind=None):
        self.query_count += 1
        if device is None:
            return list(self.devices)
        if isinstance(device, int):
            return self.devices[device]
        matches = [d for d in self.devices if d["name"] == device]
        if len(matches) > 1:
            raise ValueError(f"Multiple input devices found for {device!r}")
        if not matches:
            raise ValueError(f"No input device matching {device!r}")
        return matches[0]


def _dev(name, hostapi, channels=2, rate=44100):
    return {"name": name, "max_input_channels": channels, "hostapi": hostapi,
            "default_samplerate": rate}


def _ambiguous_focusrite():
    """The exact live shape: same name under MME, DirectSound and WASAPI."""
    return FakeSD([_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)])


# ---------------------------------------------------------------------------
# The live failure
# ---------------------------------------------------------------------------

class TestAmbiguousName:
    def test_bare_name_is_unresolvable_the_way_the_log_shows(self):
        """Reproduces what recovery used to do, to prove the premise."""
        sd = _ambiguous_focusrite()
        with pytest.raises(ValueError, match="Multiple input devices"):
            sd.query_devices(FOCUSRITE)

    def test_recovery_picks_the_recorded_hostapi(self):
        sd = _ambiguous_focusrite()

        index, api, exact = dr.resolve(sd, FOCUSRITE, "Windows DirectSound")

        assert (index, api, exact) == (1, "Windows DirectSound", True)

    def test_resolution_yields_an_index_not_a_name(self):
        sd = _ambiguous_focusrite()
        index, _api, _exact = dr.resolve(sd, FOCUSRITE, "Windows WASAPI")
        assert isinstance(index, int)
        # An index is openable where the ambiguous name was not.
        assert sd.query_devices(index)["name"] == FOCUSRITE


class TestHostApiFallback:
    def test_recorded_hostapi_gone_other_present(self):
        """USB re-enumeration can move a device between backends."""
        sd = FakeSD([_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1)])

        index, api, exact = dr.resolve(sd, FOCUSRITE, "Windows WASAPI")

        assert exact is False, "must report that the recorded host API was gone"
        assert (index, api) == (0, "MME")

    def test_fallback_prefers_wasapi_like_boot_does(self):
        sd = _ambiguous_focusrite()

        index, api, exact = dr.resolve(sd, FOCUSRITE, "")

        assert api == "Windows WASAPI"
        assert index == 2
        assert exact is False

    def test_preference_order_is_wasapi_then_mme_then_directsound(self):
        ranks = [dr._preference_rank(n) for n in
                 ("Windows WASAPI", "MME", "Windows DirectSound")]
        assert ranks == sorted(ranks), f"preference order regressed: {ranks}"

    def test_unknown_hostapi_is_usable_but_last(self):
        sd = FakeSD([_dev(FOCUSRITE, 0)], hostapis=("Some Exotic API",))
        index, api, exact = dr.resolve(sd, FOCUSRITE, "")
        assert (index, api, exact) == (0, "Some Exotic API", False)


class TestNotPresent:
    def test_missing_name_raises(self):
        sd = FakeSD([_dev("Some Other Mic", 0)])
        with pytest.raises(dr.DeviceNotFound):
            dr.resolve(sd, FOCUSRITE, "Windows WASAPI")

    def test_output_only_device_is_not_a_candidate(self):
        sd = FakeSD([_dev(FOCUSRITE, 0, channels=0)])
        with pytest.raises(dr.DeviceNotFound):
            dr.resolve(sd, FOCUSRITE, "")

    def test_no_recorded_name_raises(self):
        with pytest.raises(dr.DeviceNotFound):
            dr.resolve(FakeSD(), "", "")

    def test_device_present_probe(self):
        assert dr.device_present(_ambiguous_focusrite(), FOCUSRITE) is True
        assert dr.device_present(FakeSD([]), FOCUSRITE) is False
        assert dr.device_present(_ambiguous_focusrite(), "") is False


class TestIdentityCapture:
    def test_identity_records_name_and_hostapi(self):
        sd = _ambiguous_focusrite()
        assert dr.capture_identity(sd, 2) == (FOCUSRITE, "Windows WASAPI")

    def test_identity_of_a_failed_query_is_none(self):
        assert dr.capture_identity(FakeSD([]), 7) is None

    def test_identity_round_trips_through_resolve(self):
        """Capture at open, resolve at recovery -- the whole contract."""
        sd = _ambiguous_focusrite()
        name, api = dr.capture_identity(sd, 1)
        assert dr.resolve(sd, name, api) == (1, "Windows DirectSound", True)


# ---------------------------------------------------------------------------
# Engine-level: DEVICE_LOST, indefinite probing, reconnect
# ---------------------------------------------------------------------------

def _engine(sd, monkeypatch, **kwargs):
    """A real AudioCaptureEngine with sounddevice and the ring faked out."""
    import sys
    import types as _t
    from samsara.audio_engine.engine import AudioCaptureEngine

    monkeypatch.setitem(sys.modules, "sounddevice", sd)

    epochs = _t.SimpleNamespace(n=0)

    def _bump():
        epochs.n += 1
        return epochs.n

    ring = _t.SimpleNamespace(write=lambda *a, **k: None, bump_device_epoch=_bump)
    engine = AudioCaptureEngine(ring=ring, config={"microphone": 2}, **kwargs)
    return engine


class _OpenableSD(FakeSD):
    """FakeSD that can also 'open' a stream, and can be emptied/refilled."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.opened = []

    def InputStream(self, **kwargs):
        device = kwargs.get("device")
        if not isinstance(device, int):
            raise ValueError(f"Multiple input devices found for {device!r}")
        if device >= len(self.devices):
            raise ValueError("device gone")
        self.opened.append(device)
        sd_self = self

        class _Stream:
            def start(self_inner):
                pass

            def stop(self_inner):
                pass

            def close(self_inner):
                pass

        return _Stream()


class TestEngineRecovery:
    def test_reopen_resolves_to_an_index_and_succeeds(self, monkeypatch):
        sd = _OpenableSD([_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)])
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        assert engine._try_reopen(phase="test") is True
        assert sd.opened == [2], "did not open the recorded WASAPI index"
        assert engine.device_lost is False

    def test_device_lost_is_set_then_cleared_by_a_reopen(self, monkeypatch):
        sd = _OpenableSD([])
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        assert engine._try_reopen(phase="test") is False
        engine._device_lost = True          # what the give-up path sets
        assert engine.device_lost is True

        sd.devices = [_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)]
        assert engine._try_reopen(phase="test") is True
        assert engine.device_lost is False, "DEVICE_LOST must clear on reopen"

    def test_slow_probe_reconnects_when_the_device_returns(self, monkeypatch):
        """The 5-minutes-later case: give up, keep probing, reconnect."""
        sd = _OpenableSD([])
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._device_lost = True
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        done = threading.Event()

        def _run():
            engine._slow_probe_loop(interval_s=0.05)
            done.set()

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        time.sleep(0.15)
        assert engine.device_lost is True, "reconnected before the device returned"

        sd.devices = [_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)]
        assert done.wait(timeout=5.0), "probe loop never reconnected"
        assert engine.device_lost is False
        assert sd.opened == [2]
        engine._running = False

    def test_request_reconnect_now_short_circuits_the_wait(self, monkeypatch):
        """The tray action: force an attempt without waiting out the interval."""
        sd = _OpenableSD([_dev(FOCUSRITE, 0), _dev(FOCUSRITE, 1), _dev(FOCUSRITE, 2)])
        engine = _engine(sd, monkeypatch)
        engine._running = True
        engine._device_lost = True
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        done = threading.Event()
        thread = threading.Thread(
            target=lambda: (engine._slow_probe_loop(interval_s=30.0), done.set()),
            daemon=True,
        )
        thread.start()
        time.sleep(0.05)
        engine.request_reconnect_now()

        assert done.wait(timeout=5.0), "request_reconnect_now did not wake the loop"
        assert engine.device_lost is False
        engine._running = False

    def test_recovery_never_runs_on_the_calling_thread(self, monkeypatch):
        """The 60s window must not block boot or Qt -- _on_stream_finished
        hands off to a daemon thread and returns immediately."""
        import samsara.audio_engine.engine as engine_mod

        sd = _OpenableSD([])
        engine = _engine(sd, monkeypatch)
        engine._running = True

        spawned = {}
        monkeypatch.setattr(
            engine_mod.thread_registry, "spawn",
            lambda name, target, **kw: spawned.update(name=name, daemon=kw.get("daemon")),
        )

        started = time.monotonic()
        engine._on_stream_finished()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5, f"_on_stream_finished blocked for {elapsed:.2f}s"
        assert spawned["name"] == "ace-recovery"
        assert spawned["daemon"] is True

    def test_no_recorded_identity_falls_back_to_configured_id_not_a_name(self, monkeypatch):
        """Never a bare name -- that is the whole bug."""
        sd = _OpenableSD([_dev(FOCUSRITE, 0)])
        engine = _engine(sd, monkeypatch)
        engine._device_identity = None
        engine._device_name = None

        device, note = engine._resolve_device()

        assert device == 2  # config['microphone']
        assert note == ""

    def test_hostapi_change_is_reported(self, monkeypatch):
        sd = _OpenableSD([_dev(FOCUSRITE, 0)])
        engine = _engine(sd, monkeypatch)
        engine._device_identity = (FOCUSRITE, "Windows WASAPI")

        device, note = engine._resolve_device()

        assert device == 0
        assert "Windows WASAPI -> MME" in note
