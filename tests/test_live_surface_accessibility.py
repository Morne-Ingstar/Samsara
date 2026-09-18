"""Focused accessibility contracts for the one live-surface widget."""
from __future__ import annotations

from samsara.live_surface.model import (CaptureState, DocumentState, Notice, NoticeKind,
                                        PresentationState, SurfaceView, VisibleForm)
from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer


def test_announcement_event_exists_in_pinned_pyside() -> None:
    from PySide6.QtGui import QAccessibleAnnouncementEvent
    assert QAccessibleAnnouncementEvent.__name__ == "QAccessibleAnnouncementEvent"


def test_announcer_coalesces_identical_transition(qapp, monkeypatch) -> None:
    from PySide6.QtGui import QAccessible
    from PySide6.QtWidgets import QWidget
    calls = []
    monkeypatch.setattr(QAccessible, "updateAccessibility", lambda event: calls.append(event))
    announcer = LiveSurfaceAnnouncer(QWidget())
    assert announcer.announce_transition(("capture", "recording"), "Recording")
    assert not announcer.announce_transition(("capture", "recording"), "Recording")
    assert len(calls) == 1

