from types import SimpleNamespace

from samsara.live_surface.controller import LiveSurfaceController
from samsara.live_surface.model import (CaptureState, EventKind, Lane, SurfaceEvent)
from samsara.live_surface.partials import CapturePartials, cpu_final_only


class _Widget:
    def __init__(self, controller):
        self.controller = controller
        self.refreshes = []
        self.shown = 0

    def refresh(self, view, **kwargs): self.refreshes.append(view)
    def show(self): self.shown += 1
    def hide(self): pass
    def deleteLater(self): pass


def _controller():
    controller = LiveSurfaceController(SimpleNamespace(), widget_factory=_Widget)
    controller.start()
    return controller


def test_partials_keep_capture_sequence_and_drop_a_late_partial_after_stop():
    controller = _controller()
    capture_id = controller.begin_capture(Lane.HOLD)
    partials = CapturePartials(controller, Lane.HOLD, capture_id)
    assert partials.partial("still speaking")
    controller.drain()
    assert controller.view().provisional_text == "still speaking"
    partials.stop()
    controller.drain()
    controller.post(SurfaceEvent(EventKind.PARTIAL, capture_id=capture_id,
                                 sequence=99, lane=Lane.HOLD, text="late"))
    assert controller.drain() == 0
    assert controller.view().provisional_text == ""


def test_cpu_policy_never_emits_partial_but_keeps_final():
    controller = _controller()
    capture_id = controller.begin_capture(Lane.TOGGLE)
    partials = CapturePartials(controller, Lane.TOGGLE, capture_id, cpu_only=True)
    assert not partials.partial("not decoded")
    partials.final("final text", document_id="d", revision=1)
    controller.drain()
    assert controller.view().text == "final text"
    assert controller.view().provisional_text == ""


def test_cpu_policy_uses_device_not_a_second_decode_switch():
    assert cpu_final_only(SimpleNamespace(device_type="cpu", config={}))
    assert not cpu_final_only(SimpleNamespace(device_type="cuda", config={}))
