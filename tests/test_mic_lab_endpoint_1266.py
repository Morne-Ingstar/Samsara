"""All enumeration, COM and capture are fake; no real device is opened."""
import ctypes as ct
import numpy as np
import pytest

from samsara import mic_lab_endpoint as endpoint


class Provider:
    def __init__(self, volume=.6):
        self.volume = volume
        self.calls = []

    def read(self, **kwargs):
        self.calls.append(kwargs)
        return dict(endpoint_id="fake-id", name="USB microphone", enumerator="USB",
                    mix_format=dict(sample_rate=48000, channels=2), volume_scalar=self.volume)


class Backend:
    def query_devices(self, device, kind):
        assert kind == "input"
        return dict(name="USB microphone", hostapi=0, default_samplerate=48000., max_input_channels=4)

    def query_hostapis(self, index):
        assert index == 0
        return dict(name="Windows WASAPI")


class FakeStream:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.exited = False
        self.audio = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True

    def read(self, count):
        self.audio = np.zeros((count, self.kwargs["channels"]), np.float32)
        return self.audio, False

    def close(self):
        self.closed = True


def test_inspection_and_two_gain_reads():
    provider = Provider()
    result = endpoint.inspect_endpoint(endpoint_id="fake-id", provider=provider)
    assert result.known
    assert result.value["mix_format"] == dict(sample_rate=48000, channels=2)
    assert result.value["interface_kind"].value == "USB"
    before = endpoint.endpoint_volume(endpoint_id="fake-id", provider=provider)
    provider.volume = .62
    after = endpoint.endpoint_volume(endpoint_id="fake-id", provider=provider)
    delta = endpoint.volume_delta(before, after)
    assert delta.value["changed"]
    assert delta.value["delta"] == pytest.approx(.02)
    assert all(call == dict(endpoint_id="fake-id", name=None) for call in provider.calls)


@pytest.mark.parametrize("name,enumerator,kind,hfp", [
    ("Headset (Hands-Free AG Audio)", "", "Bluetooth hands-free", True),
    ("Generic headset", "BTHHFENUM", "Bluetooth hands-free", True),
    ("Stereo", "BTHA2DP", "Bluetooth A2DP", False),
    ("Focusrite USB Audio", "USB", "interface", False),
    ("USB microphone", "USB", "USB", False),
    ("Microphone Array (Realtek)", "HDAUDIO", "built-in", False),
])
def test_interface_guesses(name, enumerator, kind, hfp):
    assert endpoint.interface_kind(name, enumerator).value == kind
    assert endpoint.bluetooth_hands_free(name, enumerator).value is hfp


def test_unknown_interface_is_not_a_claim():
    assert not endpoint.interface_kind("Microphone").known


def test_capture_contract_and_selected_api():
    result = endpoint.capture_configuration(42, backend=Backend())
    assert result.value["host_api"] == "Windows WASAPI"
    assert result.value["mode"] == "shared"
    assert result.value["sample_rate"] == 48000
    assert endpoint.capture_configuration(42, backend=Backend(), capture_rate=44100).value["sample_rate"] == 44100
    class MME(Backend):
        def query_hostapis(self, index):
            return dict(name="MME")
    assert endpoint.capture_configuration(42, backend=MME()).value["mode"] == "not WASAPI"
    assert "engine.py:471" in " ".join(endpoint.CAPTURE_CONTRACT["evidence"])


def test_preflight_opens_native_rate_two_channels_and_closes():
    streams = []
    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream
    result = endpoint.native_preflight(42, .1, backend=Backend(), stream_factory=factory)
    assert result.known, result.reason
    assert streams[0].kwargs == dict(device=42, samplerate=48000, channels=2, dtype="float32")
    assert streams[0].closed and streams[0].exited
    assert result.value["audio"].shape == (4800, 2)
    assert result.value["frames_captured"] == 4800
    assert result.value["read_wall_seconds"] >= 0
    assert not result.value["input_overflow"]
    assert not np.shares_memory(result.value["audio"], streams[0].audio)


def test_preflight_mono_overflow():
    class Mono(Backend):
        def query_devices(self, *args, **kwargs):
            return dict(default_samplerate=16000, max_input_channels=1)
    class Overflow(FakeStream):
        def read(self, count):
            audio, _ = super().read(count)
            return audio, True
    result = endpoint.native_preflight(2, .1, backend=Mono(), stream_factory=Overflow)
    assert result.value["channels"] == 1
    assert result.value["sample_rate"] == 16000
    assert result.value["input_overflow"]


def test_preflight_read_failure_closes():
    class Broken(FakeStream):
        def read(self, count):
            raise OSError("device removed")
    stream = Broken(channels=2)
    result = endpoint.native_preflight(42, backend=Backend(), stream_factory=lambda **kw: stream)
    assert not result.known and "device removed" in result.reason
    assert stream.closed and stream.exited


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf")])
def test_preflight_bad_duration(seconds):
    assert not endpoint.native_preflight(42, seconds, backend=Backend(), stream_factory=FakeStream).known


def test_safe_failures_and_partial_metadata():
    class Broken:
        def read(self, **kwargs):
            raise OSError("access denied")
        def query_devices(self, *args, **kwargs):
            raise OSError("access denied")
    for result in (endpoint.inspect_endpoint(provider=Broken()), endpoint.endpoint_volume(provider=Broken()), endpoint.capture_configuration(backend=Broken()), endpoint.native_preflight(42, backend=Broken(), stream_factory=FakeStream)):
        assert result.value == "unknown" and "access denied" in result.reason
    unknown = endpoint.Observation(reason="access denied")
    assert not endpoint.volume_delta(unknown, endpoint.Observation(.5)).known
    class Partial(Provider):
        def read(self, **kwargs):
            data = super().read(**kwargs)
            data["volume_scalar"] = unknown
            data["enumerator"] = unknown
            return data
    result = endpoint.inspect_endpoint(provider=Partial())
    assert result.known and result.value["mix_format"]["sample_rate"] == 48000
    assert not result.value["bluetooth_hands_free"].known
    assert not endpoint.endpoint_volume(provider=Partial()).known


@pytest.mark.parametrize("volume", [-.1, 1.1, float("nan"), None])
def test_invalid_volume(volume):
    assert not endpoint.endpoint_volume(provider=Provider(volume)).known


def test_stable_gain_and_direction():
    assert not endpoint.volume_delta(endpoint.Observation(.6), endpoint.Observation(.61)).value["changed"]
    assert endpoint.volume_delta(endpoint.Observation(.6), endpoint.Observation(.5)).value["delta"] == pytest.approx(-.1)
    assert endpoint.volume_delta(endpoint.Observation(float(np.float32(.6))), endpoint.Observation(float(np.float32(.62)))).value["changed"]


def test_no_default_endpoint_substitution(monkeypatch):
    # Replace the entire native boundary, so even failure tests cannot touch COM.
    def read(self, *, endpoint_id, name):
        assert endpoint_id is None and name is None
        raise ValueError("Supply an endpoint ID or exact endpoint name")
    monkeypatch.setattr(endpoint._CoreAudio, "read", read)
    assert not endpoint.inspect_endpoint().known


def test_native_struct_layout():
    assert ct.sizeof(endpoint._GUID) == 16
    assert ct.sizeof(endpoint._WaveFormat) == 18
    assert ct.sizeof(endpoint._PropVariant) == (24 if ct.sizeof(ct.c_void_p) == 8 else 16)


def test_non_windows_unknown_without_com(monkeypatch):
    monkeypatch.setattr(endpoint.sys, "platform", "linux")
    result = endpoint.inspect_endpoint(endpoint_id="fake-id")
    assert not result.known and "Windows Core Audio" in result.reason


def test_com_read_slots_and_allocations(monkeypatch):
    """Exercise actual format/property/scalar adapters with fake COM slots."""
    calls, freed, released = [], [], []
    text = ct.create_unicode_buffer("Synthetic USB microphone")
    wave = endpoint._WaveFormat(3, 2, 48000, 384000, 8, 32, 0)

    def call(pointer, slot, args=(), types=()):
        calls.append((pointer.value, slot))
        if pointer.value == 10 and slot == 4:  # IMMDevice.OpenPropertyStore
            args[1]._obj.value = 20
        elif pointer.value == 20 and slot == 5:  # IPropertyStore.GetValue
            assert args[0]._obj.pid == 14
            args[1]._obj.vt = 31
            args[1]._obj.value.text = ct.addressof(text)
        elif pointer.value == 30 and slot == 8:  # IAudioClient.GetMixFormat
            args[0]._obj.value = ct.addressof(wave)
        elif pointer.value == 40 and slot == 9:  # IAudioEndpointVolume.GetMasterVolumeLevelScalar
            args[0]._obj.value = .7
        else:
            raise AssertionError((pointer, slot))

    class Ole:
        def CoTaskMemFree(self, value):
            freed.append(value.value)
        def PropVariantClear(self, value):
            freed.append("property")
    monkeypatch.setattr(endpoint, "_call", call)
    monkeypatch.setattr(endpoint, "_release", lambda pointer: released.append(pointer.value))
    monkeypatch.setattr(endpoint._CoreAudio, "_activate", staticmethod(lambda device, iid: ct.c_void_p(30 if iid.startswith("1cb9") else 40)))
    core = endpoint._CoreAudio()
    device = ct.c_void_p(10)
    assert core._property(device, Ole(), 14) == "Synthetic USB microphone"
    assert core._format(device, Ole()) == dict(sample_rate=48000, channels=2)
    assert core._volume(device) == pytest.approx(.7)
    assert freed == ["property", ct.addressof(wave)]
    assert released == [20, 30, 40]
    assert calls == [(10, 4), (20, 5), (30, 8), (40, 9)]


def test_com_property_failure_releases_store(monkeypatch):
    released, cleared = [], []
    def call(pointer, slot, args=(), types=()):
        if slot == 4:
            args[1]._obj.value = 20
        else:
            raise OSError("property denied")
    class Ole:
        def PropVariantClear(self, value):
            cleared.append(True)
    monkeypatch.setattr(endpoint, "_call", call)
    monkeypatch.setattr(endpoint, "_release", lambda pointer: released.append(pointer.value))
    with pytest.raises(OSError, match="property denied"):
        endpoint._CoreAudio()._property(ct.c_void_p(10), Ole(), 14)
    assert released == [20] and cleared == [True]


def test_com_vtable_dispatch_and_hresult(monkeypatch):
    # Fake in-memory COM object, including vtable: no OS calls or real pointers.
    monkeypatch.setattr(ct, "WINFUNCTYPE", ct.CFUNCTYPE, raising=False)
    callback_type = ct.CFUNCTYPE(ct.c_long, ct.c_void_p, ct.POINTER(ct.c_float))
    def scalar(this, output):
        output[0] = .5
        return 0
    callback = callback_type(scalar)
    table = (ct.c_void_p * 10)()
    table[9] = ct.cast(callback, ct.c_void_p).value
    obj = ct.pointer(ct.cast(table, ct.POINTER(ct.c_void_p)))
    value = ct.c_float()
    endpoint._call(ct.cast(obj, ct.c_void_p), 9, (ct.byref(value),), (ct.POINTER(ct.c_float),))
    assert value.value == .5
    failure = callback_type(lambda this, output: -1)
    table[9] = ct.cast(failure, ct.c_void_p).value
    with pytest.raises(OSError, match="HRESULT"):
        endpoint._call(ct.cast(obj, ct.c_void_p), 9, (ct.byref(value),), (ct.POINTER(ct.c_float),))
