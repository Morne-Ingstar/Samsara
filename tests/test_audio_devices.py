"""Tests for shared audio-device helpers in samsara.audio_devices."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import samsara.audio_devices as audio_devices


class _FakeSd:
    def __init__(self, devices, hostapis=None):
        self.devices = devices
        self.hostapis = hostapis or [{'name': 'Windows WASAPI'}]

    def query_devices(self, *args, **kwargs):
        return self.devices

    def query_hostapis(self):
        return self.hostapis


def _set_fake_sd(monkeypatch, fake_sd):
    monkeypatch.setattr(audio_devices, "sd", fake_sd)
    monkeypatch.setattr(audio_devices, "_ensure_com_initialized", lambda: False)


def _device(name, hostapi=0, channels=1, **extra):
    data = {
        'name': name,
        'hostapi': hostapi,
        'max_input_channels': channels,
    }
    data.update(extra)
    return data


def test_list_microphones_prefers_wasapi_and_dedupes(fake_sd_factory, monkeypatch):
    fake_sd = fake_sd_factory([
        _device("Focusrite USB", hostapi=0, channels=2),
        _device("focusrite usb", hostapi=1, channels=1),  # duplicate name
        _device("Built-in Mic", hostapi=1, channels=1),
        _device("Stereo Mix", hostapi=0, channels=1),
    ], hostapis=[{'name': 'Windows WASAPI'}, {'name': 'MME'}])
    _set_fake_sd(monkeypatch, fake_sd)

    microphones = audio_devices.list_microphones()

    assert microphones == [{'id': 0, 'name': 'Focusrite USB', 'channels': 2}]


def test_show_all_includes_non_wasapi_and_skips_when_disabled(fake_sd_factory, monkeypatch):
    fake_sd = fake_sd_factory([
        _device("Focusrite USB", hostapi=0, channels=2),
        _device("Stereo Mix", hostapi=1, channels=1),
    ], hostapis=[{'name': 'Windows WASAPI'}, {'name': 'MME'}])
    _set_fake_sd(monkeypatch, fake_sd)

    as_wasapi_only = audio_devices.list_microphones()
    as_all = audio_devices.list_microphones(show_all=True)

    assert as_wasapi_only == [{'id': 0, 'name': 'Focusrite USB', 'channels': 2}]
    assert as_all == [
        {'id': 0, 'name': 'Focusrite USB', 'channels': 2},
        {'id': 1, 'name': 'Stereo Mix', 'channels': 1},
    ]


def test_list_microphones_returns_channels(fake_sd_factory, monkeypatch):
    fake_sd = fake_sd_factory([_device("Mic 2ch", channels=2)])
    _set_fake_sd(monkeypatch, fake_sd)

    microphones = audio_devices.list_microphones()

    assert microphones == [{'id': 0, 'name': 'Mic 2ch', 'channels': 2}]


def test_list_microphones_propagates_query_failure(fake_sd_factory, monkeypatch):
    class _FailingSd(_FakeSd):
        def query_devices(self, *args, **kwargs):
            raise RuntimeError("query_devices failed")

    _set_fake_sd(monkeypatch, _FailingSd([_device("Mic")]))

    with pytest.raises(RuntimeError, match="query_devices failed"):
        audio_devices.list_microphones()


def test_force_rescan_uses_portaudio_cycle(fake_sd_factory, monkeypatch):
    calls = []

    class _RescanSd(_FakeSd):
        def _terminate(self):
            calls.append("terminate")

        def _initialize(self):
            calls.append("initialize")

    fake_sd = _RescanSd([_device("Mic")])
    _set_fake_sd(monkeypatch, fake_sd)

    audio_devices.force_rescan(fake_sd)

    assert calls == ["terminate", "initialize"]


@pytest.fixture
def fake_sd_factory():
    def _factory(devices, hostapis=None):
        return _FakeSd(devices, hostapis=hostapis)
    return _factory
