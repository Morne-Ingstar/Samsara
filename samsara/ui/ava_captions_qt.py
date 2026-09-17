"""Ava captions: what Ava is saying, on screen, for a user who cannot hear it.

Queue 176, the first of the ROADMAP's "Disability support" modes. Samsara is
voice-first and stays voice-first; this adds a VISUAL channel to Ava's half of
the conversation so a deaf user can hold the same exchange. Audio stays on by
default -- a second setting mutes Ava's speech entirely, for someone who wants
the text and nothing else, or who shares a room.

It is a settings MODE, not a session state: `accessibility.ava_captions` is on
until the user turns it off, and nothing Ava says while it is on goes unseen.

Shape
-----
A frameless, always-on-top panel that never takes focus (the user is dictating
INTO another window; a caption that steals the caret would break the thing it
is helping with). Ava's reply is held on screen while she speaks, then lingers
for `accessibility.ava_captions_linger_s` seconds so a slow reader is not raced
by the end of the audio. Click to dismiss, drag to move.

Threading
---------
Every public function here is safe to call from any thread and marshals onto
the samsara-qt thread with `qt_runtime.post`. NOTHING Qt is constructed while
this module is imported -- queue 173 created a QObject at import in theme.py
and the app access-violated at splash on every start. `_window` is a plain
`None` until the first caption is posted.

Placement
---------
Persisted in `accessibility.ava_captions_position` as either a preset name or
`custom|<screen>|<cx>|<cy>` -- the same serialized normalized-center scheme
ListeningIndicator and the dictation preview use, so a monitor identity
survives a resolution or DPI change without a second config key.
"""

from __future__ import annotations

import math
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

# --- config -------------------------------------------------------------------

CAPTIONS_KEY = "accessibility.ava_captions"
MUTE_KEY = "accessibility.ava_mute_audio"
LINGER_KEY = "accessibility.ava_captions_linger_s"
POSITION_KEY = "accessibility.ava_captions_position"

#: How long the panel stays up after Ava stops speaking. Six seconds is the
#: owner's figure: long enough to finish a two-sentence reply at an unhurried
#: reading pace, short enough that it is gone before the next exchange.
LINGER_DEFAULT_S = 6.0
LINGER_MIN_S = 1.0
LINGER_MAX_S = 60.0

POSITION_DEFAULT = "bottom-center"
POSITION_PRESETS = frozenset({
    "top-left", "top-center", "top-right", "center-left", "center",
    "center-right", "bottom-left", "bottom-center", "bottom-right",
})

#: Keep a held caption from living forever if the TTS engine never reports
#: done (a cancelled utterance, a dead engine). The allowance scales with the
#: text because a long reply legitimately takes longer to speak.
_SPEECH_ALLOWANCE_S_PER_CHAR = 0.10
_SPEECH_ALLOWANCE_MIN_S = 5.0
_SPEECH_ALLOWANCE_MAX_S = 120.0

#: Dragged more than this and the press was a move, not a dismiss.
_DRAG_SLOP_PX = 6
#: Keep a dragged panel off the screen edge so it stays grabbable.
_EDGE_MARGIN = 24
_MAX_WIDTH_PX = 620


def _accessibility(config) -> dict:
    """The `accessibility` block, tolerating a hand-edited config.

    Reads the nested dict the app writes, exactly as `ui.theme` is read in
    settings/general_qt.py. A flat dotted key is accepted too: someone
    hand-editing config.json should not lose the setting to a naming detail.
    """
    if not isinstance(config, dict):
        return {}
    block = config.get("accessibility")
    return dict(block) if isinstance(block, dict) else {}


def _setting(config, dotted: str, default):
    block = _accessibility(config)
    leaf = dotted.split(".", 1)[1]
    if leaf in block:
        return block[leaf]
    if isinstance(config, dict) and dotted in config:
        return config[dotted]
    return default


def captions_enabled(config) -> bool:
    """True when Ava's speech should also be shown on screen."""
    return bool(_setting(config, CAPTIONS_KEY, False))


def ava_audio_muted(config) -> bool:
    """True when Ava's speech should not be played aloud.

    Only Ava's speech: command acknowledgements and every earcon are a
    different channel and are deliberately unaffected -- a user who cannot
    hear Ava's sentences may still want the short sounds that say a command
    landed, and someone who muted Ava has not asked for a silent app.
    """
    return bool(_setting(config, MUTE_KEY, False))


def caption_linger_s(config) -> float:
    """Linger seconds, clamped. A malformed value falls back to the default
    rather than leaving a panel up forever or flashing it away."""
    try:
        value = float(_setting(config, LINGER_KEY, LINGER_DEFAULT_S))
    except (TypeError, ValueError):
        return LINGER_DEFAULT_S
    if not math.isfinite(value):
        return LINGER_DEFAULT_S
    return min(max(value, LINGER_MIN_S), LINGER_MAX_S)


def caption_position(config):
    """A preset name, or `('custom', screen, cx, cy)`.

    Same serialization as the dictation preview's `command_mode.
    preview_position`: one string, so the ordinary config validator keeps a
    monitor identity without a second key.
    """
    value = _setting(config, POSITION_KEY, POSITION_DEFAULT)
    if value in POSITION_PRESETS:
        return value
    if isinstance(value, str) and value.startswith("custom|"):
        parts = value.split("|", 3)
        if len(parts) == 4:
            try:
                cx, cy = float(parts[2]), float(parts[3])
            except (TypeError, ValueError):
                return POSITION_DEFAULT
            if math.isfinite(cx) and math.isfinite(cy):
                return ("custom", parts[1],
                        min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0))
    return POSITION_DEFAULT


def serialize_position(screen_name: str, cx: float, cy: float) -> str:
    return f"custom|{screen_name}|{cx:.4f}|{cy:.4f}"


def speech_allowance_s(text: str) -> float:
    """The longest a held caption may wait for a `done` that never comes."""
    estimate = len(text or "") * _SPEECH_ALLOWANCE_S_PER_CHAR
    return min(max(estimate, _SPEECH_ALLOWANCE_MIN_S), _SPEECH_ALLOWANCE_MAX_S)


# --- the panel ----------------------------------------------------------------

class _CaptionsWindow(QWidget):
    """The caption panel itself. Built on the samsara-qt thread, once."""

    def __init__(self, app):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._app = app
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # The user is dictating into someone else's window. A caption that
        # activates would move the caret out from under the text it is
        # captioning, which is the one thing this must never do.
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAccessibleName("Ava captions")
        self.setObjectName("avaCaptions")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._card = QFrame()
        self._card.setObjectName("avaCaptionCard")
        outer.addWidget(self._card)

        inner = QVBoxLayout(self._card)
        inner.setContentsMargins(20, 16, 20, 18)
        inner.setSpacing(6)

        self._who = QLabel("AVA")
        self._who.setObjectName("avaCaptionWho")
        self._who.setAccessibleName("Speaker")
        inner.addWidget(self._who)

        self._text = QLabel("")
        self._text.setObjectName("avaCaptionText")
        self._text.setAccessibleName("Ava caption text")
        self._text.setWordWrap(True)
        self._text.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        inner.addWidget(self._text)

        self._hint = QLabel("Click to dismiss  |  drag to move")
        self._hint.setObjectName("avaCaptionHint")
        self._hint.setAccessibleName("Caption controls")
        inner.addWidget(self._hint)

        # Linger / hard-cap timer. One timer, restarted -- a held caption
        # replaced by a newer reply must not be taken down by the old one's
        # countdown.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self._dragging = False
        self._drag_origin = None
        self._drag_offset = None

        self.restyle()

    # -- painting ------------------------------------------------------------

    def restyle(self) -> None:
        """Read every colour and size HERE, per call.

        Not at import and not in a constant: a token captured once keeps its
        value for the life of the process, so the palette switch repaints
        everything except this panel (tests/test_colour_tokens.py).
        """
        self._card.setStyleSheet(
            f"QFrame#avaCaptionCard {{"
            f" background-color: {theme.BG1};"
            f" border: 1px solid {theme.AVA};"
            f" border-radius: 12px; }}"
        )
        self._who.setStyleSheet(
            f"color: {theme.AVA}; font-size: {theme.TYPE_BODY}px;"
            f" font-weight: 700; letter-spacing: 0.12em;"
        )
        # The caption itself is the reading text: the largest size on the
        # panel, primary ink on a card, which is the highest-contrast pair
        # the palette has (tests/test_theme_contrast.py).
        self._text.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_TITLE}px;"
            f" font-weight: 600;"
        )
        self._hint.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_MIN}px;"
        )

    # -- content -------------------------------------------------------------

    def show_text(self, text: str, hold: bool, linger_s: float) -> None:
        self.restyle()
        self._text.setText(str(text or ""))
        self._text.setMaximumWidth(_MAX_WIDTH_PX)
        self.setMaximumWidth(_MAX_WIDTH_PX + 2)
        self.adjustSize()
        self._apply_position()
        if not self.isVisible():
            self.show()
        self.raise_()
        if hold:
            # Speech is starting. Keep it up, but never past the point where
            # a `done` that never arrived would have arrived anyway.
            self._arm(linger_s + speech_allowance_s(text))
        else:
            self._arm(linger_s)

    def release(self, linger_s: float) -> None:
        """Speech ended: start the linger countdown."""
        if not self.isVisible():
            return
        self._arm(linger_s)

    def _arm(self, seconds: float) -> None:
        self._timer.stop()
        self._timer.start(int(max(seconds, 0.1) * 1000))

    def dismiss(self) -> None:
        self._timer.stop()
        self.hide()

    # -- placement -----------------------------------------------------------

    def _screen_for(self, name):
        for screen in QApplication.screens():
            if screen.name() == name:
                return screen
        if name:
            logger.info("Ava captions: screen %r is gone; using the primary screen.", name)
        return QApplication.primaryScreen()

    def _apply_position(self) -> None:
        placement = caption_position(getattr(self._app, "config", None))
        width, height = self.width(), self.height()
        if isinstance(placement, tuple):
            _kind, name, cx, cy = placement
            screen = self._screen_for(name)
            if screen is None:
                return
            geom = screen.availableGeometry()
            x = int(geom.x() + cx * geom.width() - width / 2)
            y = int(geom.y() + cy * geom.height() - height / 2)
            self.move(*_reachable(x, y, width, height, geom))
            return
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        self.move(*_preset_origin(str(placement), width, height, geom))

    def _commit_position(self) -> None:
        """Store the just-dragged placement, normalized to its monitor."""
        centre = self.mapToGlobal(self.rect().center())
        screen = QApplication.screenAt(centre) or QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        width, height = self.width(), self.height()
        x, y = _reachable(self.x(), self.y(), width, height, geom)
        self.move(x, y)
        cx = min(max(((x + width / 2.0) - geom.x()) / max(geom.width(), 1), 0.0), 1.0)
        cy = min(max(((y + height / 2.0) - geom.y()) / max(geom.height(), 1), 0.0), 1.0)
        store_position(self._app, serialize_position(screen.name(), cx, cy))

    # -- mouse ---------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self._drag_origin = event.globalPosition().toPoint()
        self._drag_offset = self._drag_origin - self.pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if not self._dragging or self._drag_offset is None:
            super().mouseMoveEvent(event)
            return
        self.move(event.globalPosition().toPoint() - self._drag_offset)
        event.accept()

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        moved = 0
        if self._drag_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_origin
            moved = max(abs(delta.x()), abs(delta.y()))
        self._drag_origin = None
        self._drag_offset = None
        # A click is a dismiss; anything that actually travelled is a move,
        # so a user repositioning the panel does not lose the caption they
        # were still reading.
        if moved > _DRAG_SLOP_PX:
            self._commit_position()
        else:
            self.dismiss()
        event.accept()


def _clamp(x: int, y: int, w: int, h: int, geom) -> tuple:
    max_x = geom.x() + max(geom.width() - w, 0)
    max_y = geom.y() + max(geom.height() - h, 0)
    return min(max(x, geom.x()), max_x), min(max(y, geom.y()), max_y)


def _reachable(x: int, y: int, w: int, h: int, geom) -> tuple:
    margin = min(
        _EDGE_MARGIN,
        max((geom.width() - w) // 2, 0),
        max((geom.height() - h) // 2, 0),
    )
    return _clamp(x, y, w, h, geom.adjusted(margin, margin, -margin, -margin))


def _preset_origin(name: str, w: int, h: int, geom) -> tuple:
    if name not in POSITION_PRESETS:
        name = POSITION_DEFAULT
    vertical, _, horizontal = name.partition("-")
    left = {
        "left": geom.x() + _EDGE_MARGIN,
        "center": geom.x() + (geom.width() - w) // 2,
        "right": geom.x() + geom.width() - w - _EDGE_MARGIN,
    }[horizontal or "center"]
    top = {
        "top": geom.y() + _EDGE_MARGIN,
        "center": geom.y() + (geom.height() - h) // 2,
        "bottom": geom.y() + geom.height() - h - _EDGE_MARGIN,
    }[vertical]
    return _clamp(left, top, w, h, geom)


# --- facade -------------------------------------------------------------------

_lock = threading.Lock()
#: Deliberately a plain None at import: see the module docstring on queue 173.
_window: "_CaptionsWindow | None" = None


def store_position(app, value: str) -> bool:
    """Persist a placement through the app's own config path."""
    config = getattr(app, "config", None)
    if not isinstance(config, dict):
        return False
    block = dict(config.get("accessibility") or {})
    block["ava_captions_position"] = value
    update = getattr(app, "update_config_and_save", None)
    if callable(update):
        update({"accessibility": block})
    else:
        config["accessibility"] = block
    return True


def _ensure_window(app):
    """The one panel, built on the calling (Qt) thread. Qt thread only."""
    global _window
    with _lock:
        window = _window
    if window is None:
        window = _CaptionsWindow(app)
        with _lock:
            _window = window
    return window


def _post(fn) -> None:
    """Run fn on the samsara-qt thread, or here when there is no runtime.

    A test (and the offscreen proof tool) owns its own QApplication and never
    starts the runtime; calling straight through keeps this usable there
    without a second code path in the caller.
    """
    try:
        from samsara.ui import qt_runtime            # noqa: PLC0415
        if qt_runtime.is_alive():
            qt_runtime.post(fn)
            return
    except Exception as exc:                          # never break TTS
        logger.debug("Ava captions: qt_runtime unavailable (%s)", exc)
    fn()


def show_caption(app, text: str, *, hold: bool = True) -> None:
    """Show `text` as Ava's caption. Safe from any thread.

    hold=True means speech is starting and the panel stays up until
    release_caption(); hold=False starts the linger immediately, which is the
    muted case, where there is no speech to wait for.
    """
    text = str(text or "").strip()
    if not text:
        return
    linger = caption_linger_s(getattr(app, "config", None))

    def _run():
        try:
            _ensure_window(app).show_text(text, hold, linger)
        except Exception as exc:
            logger.warning("Ava captions: could not show a caption: %s", exc)

    _post(_run)


def release_caption(app) -> None:
    """Ava stopped speaking: begin the linger. Safe from any thread."""
    linger = caption_linger_s(getattr(app, "config", None))

    def _run():
        with _lock:
            window = _window
        if window is None:
            return
        try:
            window.release(linger)
        except Exception as exc:
            logger.debug("Ava captions: could not start the linger: %s", exc)

    _post(_run)


def hide_captions() -> None:
    """Take the panel down now. Safe from any thread."""
    def _run():
        with _lock:
            window = _window
        if window is not None:
            try:
                window.dismiss()
            except Exception as exc:
                logger.debug("Ava captions: could not hide the panel: %s", exc)

    _post(_run)


def active_window():
    """The live panel, or None. For tests and the theme proof tool."""
    with _lock:
        return _window


def reset_for_test() -> None:
    """Drop the cached panel so the next call builds a fresh one."""
    global _window
    with _lock:
        window, _window = _window, None
    if window is not None:
        window.hide()
        window.deleteLater()
