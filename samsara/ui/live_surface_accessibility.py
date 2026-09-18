"""Accessibility announcements for the live surface.

The surface deliberately keeps a quiet passive window.  A named announcement
is therefore sent for meaningful state changes instead of trying to make the
rapidly-changing partial transcript into a live region.
"""
from __future__ import annotations

from PySide6.QtCore import QObject
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent


class LiveSurfaceAnnouncer(QObject):
    """Send one announcement for each semantic transition.

    ``transition`` is owned by the controller/model boundary.  Repeating it
    is a repaint or a partial update and must not make a screen reader talk.
    """

    def __init__(self, owner: QObject) -> None:
        super().__init__(owner)
        self._owner = owner
        self._last_transition: object | None = None
        self.last_message = ""

    def announce_transition(self, transition: object, message: str) -> bool:
        """Announce ``message`` once, returning whether an event was sent."""
        if not message or transition == self._last_transition:
            return False
        self._last_transition = transition
        self.last_message = message
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self._owner, message))
        return True

    def reset(self) -> None:
        """Allow the next genuine transition to be announced."""
        self._last_transition = None

