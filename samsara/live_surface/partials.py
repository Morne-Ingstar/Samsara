"""Per-capture partial sequencing and CPU final-only policy."""
from __future__ import annotations

from dataclasses import dataclass

from samsara.live_surface.model import EventKind, Lane, SurfaceEvent


def cpu_final_only(app) -> bool:
    """CPU capture never schedules an additional partial decode."""
    return str(getattr(app, "device_type", "") or app.config.get("device", "cpu")).lower() == "cpu"


@dataclass
class CapturePartials:
    controller: object
    lane: Lane
    capture_id: str
    cpu_only: bool = False
    sequence: int = 1
    stopped: bool = False

    def partial(self, text: str) -> bool:
        if self.cpu_only or self.stopped or not text:
            return False
        self.sequence += 1
        self.controller.post(SurfaceEvent(EventKind.PARTIAL, capture_id=self.capture_id,
                             sequence=self.sequence, lane=self.lane, text=text))
        return True

    def final(self, text: str, *, document_id: str | None = None,
              revision: int | None = None, delivered: bool = False) -> None:
        self.sequence += 1
        kind = EventKind.DELIVERY_FINISHED if delivered else EventKind.DOCUMENT_UPDATED
        self.controller.post(SurfaceEvent(kind, capture_id=self.capture_id,
                             sequence=self.sequence, lane=self.lane, text=text,
                             document_id=document_id, revision=revision))

    def stop(self) -> None:
        self.stopped = True
        self.controller.stop_capture(capture_id=self.capture_id)
