"""Gesture-control component boundaries: absent core never starts the camera."""

from __future__ import annotations

from samsara import components
from samsara.vision import camera_service
from tools import gen_release_manifest as manifest


def test_feature_kind_and_gesture_manifest_entry_are_valid():
    assert "feature" in components.KINDS
    gesture = next(c for c in manifest.component_specs("v9.9.9") if c["id"] == "gesture-control")
    assert gesture["kind"] == "feature"
    assert gesture["default"] is False
    assert gesture["install_dir"] == "_internal"


def test_camera_refuses_before_touching_hardware_when_component_is_absent(monkeypatch):
    monkeypatch.setattr(camera_service, "cv2", None)
    service = camera_service.CameraService()

    try:
        service.start()
    except components.ComponentNotInstalled as exc:
        assert str(exc) == components.GESTURE_COMPONENT_MESSAGE
    else:
        raise AssertionError("missing gesture component opened the camera")


def test_component_message_names_the_existing_install_surface():
    assert "Settings > Advanced > Components" in components.GESTURE_COMPONENT_MESSAGE
