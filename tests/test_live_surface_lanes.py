from types import SimpleNamespace

import pytest

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import CaptureState, Lane
from samsara.live_surface.partials import CapturePartials


def test_live_surface_schema_default_is_opt_in():
    from samsara.config_schema import SETTINGS_SCHEMA

    assert SETTINGS_SCHEMA["ui.live_surface.enabled"]["default"] is False


@pytest.mark.parametrize(("config", "expected"), [
    ({}, False),
    ({"ui": {"live_surface": {"enabled": True}}}, True),
])
def test_advanced_live_surface_setting_reflects_opt_in_state(qapp, config, expected):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests._theme_stub_app import StubApp

    app = StubApp(config)
    window = _SettingsWindow(app)
    try:
        checkbox = window._widgets["adv_live_surface_enabled"]
        assert checkbox.isChecked() is expected
        assert checkbox.accessibleName() == (
            "Use the new live surface (experimental — incomplete; the old indicator "
            "and preview are the default)"
        )
    finally:
        window.deleteLater()


class _Widget:
    instances = []
    def __init__(self, controller):
        self.controller = controller
        self.refreshes = []
        self.visible = False
        type(self).instances.append(self)
    def refresh(self, view, **kwargs): self.refreshes.append(view)
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def deleteLater(self): pass


@pytest.mark.parametrize("lane", [Lane.HOLD, Lane.TOGGLE, Lane.HANDS_FREE_DICTATE, Lane.CONTINUOUS])
def test_every_capture_lane_drives_one_surface_from_start_to_delivery(lane):
    _Widget.instances.clear()
    controller = LiveSurfaceController(SimpleNamespace(), widget_factory=_Widget)
    controller.start()
    capture = controller.begin_capture(lane)
    partials = CapturePartials(controller, lane, capture)
    partials.partial("live words")
    partials.final("delivered words", document_id="draft", revision=1)
    partials.stop()
    controller.drain()
    assert controller.view().lane is lane
    assert controller.view().text == "delivered words"
    assert controller.view().capture is CaptureState.OFF
    assert len(_Widget.instances) == 1


def test_controller_syncs_authoritative_draft_without_a_preview_copy():
    draft = SimpleNamespace(document_id="draft", revision=3, text="settled words")
    manager = SimpleNamespace(draft_document=draft)
    controller = LiveSurfaceController(SimpleNamespace(), widget_factory=_Widget)
    controller.start()
    controller.sync_draft(manager)
    controller.drain()
    assert controller.view().text == "settled words"


def test_dictate_switch_uses_windowless_preview_only_when_live_surface_enabled(monkeypatch):
    import samsara.streaming as streaming

    constructed = []

    class LegacyOverlay:
        def __init__(self, **_kwargs):
            constructed.append(self)
        def set_interaction_callbacks(self, *_args): pass
        def set_correction_choice_callbacks(self, *_args): pass

    monkeypatch.setattr(streaming, "StreamingOverlayQt", LegacyOverlay)

    class Controller:
        def __init__(self): self.begun = []
        def begin_capture(self, lane):
            self.begun.append(lane)
            return 7
        def start(self): pass

    enabled_controller = Controller()
    enabled = SimpleNamespace(
        live_surface=enabled_controller,
        config={"ui": {"live_surface": {"enabled": True}}},
    )
    enabled_session = streaming.DictatePreviewSession(enabled)
    assert isinstance(enabled_session._overlay, streaming._LiveSurfaceDictateOverlay)
    assert not constructed

    disabled = SimpleNamespace(
        live_surface=None,
        config={"ui": {"live_surface": {"enabled": False}}},
    )
    disabled_session = streaming.DictatePreviewSession(disabled)
    assert isinstance(disabled_session._overlay, LegacyOverlay)
    assert len(constructed) == 1

    absent_controller = Controller()
    absent = SimpleNamespace(live_surface=absent_controller, config={})
    absent_session = streaming.DictatePreviewSession(absent)
    assert isinstance(absent_session._overlay, LegacyOverlay)
    assert absent_controller.begun == []
    assert len(constructed) == 2


def test_hold_stream_uses_legacy_overlay_when_live_surface_flag_is_absent(monkeypatch):
    import samsara.streaming as streaming

    constructed = []

    class LegacyOverlay:
        def __init__(self, **_kwargs):
            constructed.append(self)

    class Controller:
        def __init__(self): self.begun = []
        def begin_capture(self, lane):
            self.begun.append(lane)
            return 7

    monkeypatch.setattr(streaming, "StreamingOverlayQt", LegacyOverlay)
    controller = Controller()
    app = SimpleNamespace(config={}, live_surface=controller, device_type="cpu")
    session = streaming.StreamingSession(app)

    assert isinstance(session._overlay, LegacyOverlay)
    assert controller.begun == []
    assert len(constructed) == 1
