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
        combo = window._widgets["listening_display"]
        assert combo.currentData() == ("full" if expected else "compact")
        assert [combo.itemData(i) for i in range(combo.count())] == ["compact", "full"]
        assert combo.accessibleName() == "Listening display"
        assert "adv_live_surface_enabled" not in window._widgets
        assert "adv_indicator_enabled" not in window._widgets
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


def test_adapters_cover_every_public_overlay_method_and_call_signature(qapp, monkeypatch, qt_widget_scope):
    import inspect
    from unittest.mock import Mock
    from samsara.streaming import StreamingOverlayQt, _LiveSurfaceDictateOverlay, _LiveSurfaceStreamingOverlay, DictatePreviewSession
    # Enumerate the real class, not a hand-maintained list that can drift.
    methods = {name: method for name, method in inspect.getmembers(StreamingOverlayQt, inspect.isfunction)
               if not name.startswith('_')}
    # Cleanup is an explicitly optional registration on the legacy overlay;
    # Full owns that intent on its controller instead of the preview session.
    assert callable(methods.pop('set_cleanup_callback'))
    source = inspect.getsource(DictatePreviewSession.__init__)
    assert 'cleanup = getattr(self._overlay, "set_cleanup_callback", None)' in source
    assert 'if callable(cleanup):' in source
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    controller = LiveSurfaceController(SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}))
    controller._clean_up = cleanup = Mock()
    controller.start()
    with qt_widget_scope(controller.widget):
        try:
            controller.widget.clean_up_requested.emit()
            cleanup.assert_called_once_with()
        finally:
            controller.close()
    for adapter in (_LiveSurfaceDictateOverlay, _LiveSurfaceStreamingOverlay):
        for name, method in methods.items():
            assert callable(getattr(adapter, name, None)), (adapter.__name__, name)
            params = list(inspect.signature(method).parameters.values())[1:]
            args = [None for p in params if p.kind is p.POSITIONAL_ONLY]
            kwargs = {p.name: None for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
            inspect.signature(getattr(adapter, name)).bind(None, *args, **kwargs)


def test_hold_pause_then_partials_reach_real_surface(qapp, monkeypatch, qt_widget_scope):
    from samsara.streaming import DictatePreviewSession, _LiveSurfaceDictateOverlay, _LiveSurfaceStreamingOverlay, StreamingOverlayQt
    from samsara.live_surface.partials import CapturePartials
    monkeypatch.setattr('samsara.live_surface.controller.qt_runtime.post', lambda *_: None)
    c = LiveSurfaceController(SimpleNamespace(config={'ui': {'live_surface': {'enabled': True}}}))
    c.start()
    with qt_widget_scope(c.widget):
        try:
            c.widget.set_reduced_motion(True)  # This case checks routing, not animation.
            preview = CapturePartials(c, Lane.HANDS_FREE_DICTATE, c.begin_capture(Lane.HANDS_FREE_DICTATE))
            session = DictatePreviewSession.__new__(DictatePreviewSession)
            session._overlay = _LiveSurfaceDictateOverlay(c, preview)
            session._generation = 0; session._finalized = []
            hold = CapturePartials(c, Lane.HOLD, c.begin_capture(Lane.HOLD))
            assert session._enter_hold_pause(False)
            overlay = _LiveSurfaceStreamingOverlay(hold)
            overlay.update_text('Synthetic live hold words', StreamingOverlayQt.STATE_PROCESSING)
            c.drain(); qapp.processEvents()
            assert c.widget.isVisible() and c.widget._transcript.isVisible()
            assert 'Synthetic live hold words' in c.widget._transcript.toPlainText()
            assert c.view().capture is CaptureState.RECORDING
            hold.stop(); session._overlay.resume()
            session._overlay.set_transcript([], 'Synthetic resumed words')
            c.drain()
            assert c.view().provisional_text == 'Synthetic resumed words'
        finally:
            c.close()
