"""Readback coverage for endpoints that accept volume writes but ignore them."""

from plugins.commands import volume
from samsara.command_registry import DispatchState
import pytest
from types import SimpleNamespace
from unittest.mock import Mock


class FakeEndpoint:
    def __init__(self, *, volume=1.0, volume_range=(-60.0, 0.0, 0.5), mute=False,
                 ignores_volume_writes=False):
        self.volume = volume
        self.volume_range = volume_range
        self.mute = mute
        self.ignores_volume_writes = ignores_volume_writes

    def get_volume(self):
        return self.volume

    def get_volume_range(self):
        return self.volume_range

    def set_volume(self, level):
        if not self.ignores_volume_writes:
            self.volume = level
        return True

    def get_mute(self):
        return self.mute

    def set_mute(self, muted):
        self.mute = bool(muted)
        return True


class FakeApp:
    def __init__(self):
        self.outcomes = []

    def _show_outcome_chip(self, text, kind, duration):
        self.outcomes.append((text, kind, duration))


def test_fixed_range_ignored_write_reports_device_owns_volume(monkeypatch):
    endpoint = FakeEndpoint(volume_range=(0.0, 0.0, 1.0),
                            ignores_volume_writes=True)
    app = FakeApp()
    monkeypatch.setattr(volume, "_audio", endpoint)

    result = volume.handle_set_volume(app, "30")

    assert result.state is DispatchState.FAILED
    assert result.detail["reason"] == "This device controls its own volume"
    assert app.outcomes == [("This device controls its own volume", "warning", 3500)]
    assert endpoint.volume == 1.0


def test_normal_endpoint_still_sets_and_reports_volume(monkeypatch):
    endpoint = FakeEndpoint(volume=1.0)
    app = FakeApp()
    monkeypatch.setattr(volume, "_audio", endpoint)

    assert volume.handle_set_volume(app, "30") is True
    assert endpoint.volume == 0.3
    assert app.outcomes == [("Windows volume 30%", "success", 3500)]


def test_volume_up_down_and_mute_are_verified(monkeypatch):
    endpoint = FakeEndpoint(volume=0.5)
    app = FakeApp()
    monkeypatch.setattr(volume, "_audio", endpoint)

    assert volume.handle_volume_up(app, "") is True
    assert endpoint.volume == pytest.approx(0.7)
    assert volume.handle_volume_down(app, "") is True
    assert endpoint.volume == pytest.approx(0.5)
    assert volume.handle_mute(app, "") is True
    assert endpoint.mute is True


def test_ignored_up_write_is_failed_on_fixed_endpoint(monkeypatch):
    endpoint = FakeEndpoint(volume_range=(0.0, 0.0, 1.0),
                            ignores_volume_writes=True)
    app = FakeApp()
    monkeypatch.setattr(volume, "_audio", endpoint)

    result = volume.handle_volume_up(app, "")

    assert result.state is DispatchState.FAILED
    assert result.detail["reason"] == "This device controls its own volume"


@pytest.mark.parametrize("volume_range", [
    None, Mock(), (Mock(), Mock(), Mock()), ("0", "0", "1"),
    (), (0.0,), (float("nan"), 0.0, 1.0),
    (0.0, float("inf"), 1.0), (True, False, 1.0),
])
def test_unknown_range_preserves_setter_contract(monkeypatch, volume_range):
    endpoint = Mock()
    endpoint.get_volume_range.return_value = volume_range
    endpoint.set_volume.return_value = True
    monkeypatch.setattr(volume, "_audio", endpoint)
    app = FakeApp()

    assert volume.handle_set_volume(app, "thirty") is True
    endpoint.set_volume.assert_called_once_with(0.3)
    assert app.outcomes == [("Windows volume 30%", "success", 3500)]


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_unreadable_range_preserves_setter_contract(monkeypatch, missing):
    endpoint = SimpleNamespace(set_volume=Mock(return_value=True))
    if not missing:
        endpoint.get_volume_range = Mock(side_effect=OSError("range unavailable"))
    monkeypatch.setattr(volume, "_audio", endpoint)
    app = FakeApp()

    assert volume.handle_set_volume(app, "30") is True
    endpoint.set_volume.assert_called_once_with(0.3)
    assert app.outcomes == [("Windows volume 30%", "success", 3500)]


def test_unknown_range_does_not_hide_setter_failure(monkeypatch):
    monkeypatch.setattr(volume, "_audio", SimpleNamespace(set_volume=lambda level: False))
    app = FakeApp()
    assert volume.handle_set_volume(app, "30") is False
    assert app.outcomes == []


def test_normal_range_ignored_write_does_not_claim_success(monkeypatch):
    monkeypatch.setattr(volume, "_audio", FakeEndpoint(ignores_volume_writes=True))
    result = volume.handle_set_volume(FakeApp(), "30")
    assert result.state is DispatchState.FAILED
    assert result.detail["reason"] == "Could not set Windows volume"


def test_normal_range_already_at_requested_level_succeeds(monkeypatch):
    monkeypatch.setattr(volume, "_audio", FakeEndpoint(volume=0.3))
    assert volume.handle_set_volume(FakeApp(), "30") is True


def test_native_endpoint_slots_and_signatures(monkeypatch):
    """SDK IAudioEndpointVolume layout, with no COM/device access."""
    audio = volume._CoreAudio()
    monkeypatch.setattr(audio, "_get_volume_interface", lambda: 123)
    calls = []

    def vtable(iface, index, restype, *argtypes):
        calls.append((index, argtypes))

        def invoke(this, *args):
            if index == 20:
                for pointer, value in zip(args, (-60.0, 0.0, 0.5)):
                    pointer._obj.value = value
            elif index == 15:
                args[0]._obj.value = 1
            elif index == 14:
                assert args == (1, None)
            else:
                assert index == 2  # Release
            return 0
        return invoke

    monkeypatch.setattr(volume, "_get_vtable_func", vtable)
    assert audio.get_volume_range() == (-60.0, 0.0, 0.5)
    assert audio.get_mute() is True
    assert audio.set_mute(True) is True
    assert [index for index, _ in calls] == [20, 2, 15, 2, 14, 2]
    assert calls[2][1] == (volume.c_void_p, volume.POINTER(volume.wintypes.BOOL))
    assert calls[4][1] == (volume.c_void_p, volume.wintypes.BOOL, volume.c_void_p)
