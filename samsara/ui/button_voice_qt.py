"""Qt-thread discovery and activation of the currently visible controls.

No widget access on the speech thread. Snapshot matching is exact; activation
rechecks identity, visibility, enabled state and uniqueness on the Qt thread.
"""
from __future__ import annotations

import weakref
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import QAbstractButton, QCheckBox, QMenu

from samsara.button_voice import (
    CAPTION_DISMISS_LABEL, normalize_label, phrases_for_label, source_phrases,
)


# Deliberately bounded to the session surfaces named in 1150. No third-party
# windows, text editors, arbitrary links or model-generated answer text.
SURFACE_MODULES = frozenset({
    "samsara.ui.paste_again_qt",
    "samsara.streaming", "samsara.ui.live_surface_qt",
    "samsara.ui.listening_indicator", "samsara.ui.ava_captions_qt",
    "samsara.ui.ava_search_panel_qt", "samsara.ui.ava_consent_qt",
    "samsara.ui.correction_picker_qt", "samsara.bug_report.preview_qt",
    "samsara.bug_report.compact_qt", "samsara.ui.settings_qt",
    "samsara.ava_edit.draft_review",
})


@dataclass(frozen=True)
class Action:
    identity: tuple
    label: str
    target: object
    operation: str = "click"
    aliases: tuple = ()
    explicit_only: bool = False

    @property
    def phrases(self):
        phrases = phrases_for_label(self.label)
        return ((*[p for p in phrases if p.startswith("click ")], *self.aliases)
                if self.explicit_only else (*phrases, *self.aliases))


def button_label(button):
    # Settings switches deliberately paint no text on the switch itself.
    # SettingRow gives them the adjacent visible row heading as their name.
    if isinstance(button, QCheckBox):
        parent = button.parentWidget()
        while parent is not None:
            if parent.property("role") == "row" and getattr(parent, "title", ""):
                return parent.title
            parent = parent.parentWidget()
    return button.text().strip() or button.accessibleName().strip()


def _supported(window):
    return (isinstance(window, QMenu)
            or type(window).__module__ in SURFACE_MODULES
            or window.accessibleName() == "Clipboard image choices")


def _context(button, window):
    """A reused display button must not answer a newer question or draft."""
    parent = button.parentWidget()
    while parent is not None:
        notice = getattr(parent, "notice", None)
        if notice is not None:
            return (id(notice),)
        parent = parent.parentWidget()
    view = getattr(window, "_view", None)
    if view is not None:
        return (view.document_id, view.revision, view.feedback_revision)
    return ()


def visible_actions(app):
    """Called only on Qt's thread; return immutable descriptors, never text fields."""
    modal = app.activeModalWidget()
    popup = app.activePopupWidget()
    windows = [modal or popup] if modal or popup else app.topLevelWidgets()
    result = []
    for window in windows:
        if window is None or not window.isVisible() or not _supported(window):
            continue
        if isinstance(window, QMenu):
            for action in window.actions():
                if (action.isVisible() and action.isEnabled() and not action.isSeparator()
                        and not window.actionGeometry(action).isEmpty()):
                    result.append(Action((id(action), id(window)), action.text(),
                                         (weakref.ref(action), weakref.ref(window)), "menu"))
            continue
        for button in window.findChildren(QAbstractButton):
            if (button.window() is window and button.isVisible() and button.isEnabled()
                    and not button.visibleRegion().isEmpty()):
                label = button_label(button)
                if label:
                    result.append(Action((id(button), *_context(button, window)), label, weakref.ref(button)))
        if type(window).__module__ == "samsara.ui.ava_captions_qt":
            result.append(Action((id(window), "caption"), CAPTION_DISMISS_LABEL,
                                 weakref.ref(window), "dismiss"))
        if type(window).__module__ == "samsara.ui.ava_search_panel_qt":
            sources = window.sources
            for index in range(sources.count()):
                item = sources.item(index)
                if (item.data(Qt.ItemDataRole.UserRole) and sources.isVisible()
                        and sources.visualItemRect(item).intersects(sources.viewport().rect())):
                    result.append(Action((id(item), id(sources)), item.text(),
                        (weakref.ref(sources), weakref.ref(item)), "source", source_phrases(index + 1), True))
    return tuple(result)


class VisibleActions(QObject):
    def __init__(self, app, post):
        super().__init__(app)
        self.app, self.post = app, post
        self.snapshot = ()
        # Includes label swaps (Accept?/Cancel, retry/report) and controls
        # scrolled into view, without touching any of their display code.
        self.timer = QTimer(self)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.refresh)
        self.refresh()
        self.timer.start()

    def refresh(self):
        self.snapshot = visible_actions(self.app)

    @staticmethod
    def _matches(actions, phrase):
        return [action for action in actions if phrase in action.phrases]

    def dispatch(self, text):
        """Speech-thread safe: queue a unique match, with no widget calls here."""
        phrase = normalize_label(text)
        matches = self._matches(self.snapshot, phrase)
        if len(matches) != 1:
            return False
        chosen = matches[0]
        self.post(lambda: self._activate(chosen, phrase))
        return True

    def _activate(self, chosen, phrase):
        # A queued utterance must never act on a replacement question/window.
        matches = self._matches(visible_actions(self.app), phrase)
        if len(matches) != 1 or matches[0].identity != chosen.identity:
            self.refresh()
            return
        action = matches[0]
        if action.operation == "source":
            sources, item = (ref() for ref in action.target)
            sources.itemActivated.emit(item)
        elif action.operation == "menu":
            item, menu = (ref() for ref in action.target)
            submenu = item.menu()
            if submenu is not None:
                submenu.popup(menu.mapToGlobal(menu.actionGeometry(item).topRight()))
            else:
                menu.close()
                item.trigger()
        else:
            target = action.target()
            getattr(target, action.operation)()
        self.refresh()
