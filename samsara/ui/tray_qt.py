"""Qt system tray icon for Samsara, and the one routine that draws the mark.

Drop-in replacement for the pystray.Icon usage in dictation.py.
Exposes the same attribute interface the rest of the app uses:
    .icon  = MarkFrame     (property setter, thread-safe; a PIL image still works)
    .title = "Samsara - X" (property setter, thread-safe)
    .stop()                (thread-safe, hides icon)

Must be created on the Qt thread (via QTimer.singleShot or similar).
All Signal-based methods are safe to call from any thread.

render_mark() is the single drawing of the Samsara mark (the three-segment
wheel plus the hands-free eye, from assets/icon/samsara.svg). The tray, the
listening indicator, the splash and tools/gen_icons.py all call it -- there
is no second palette or drawing. It builds Qt image objects, so call it on
the Qt thread only (off-thread Qt image construction once deadlocked boot;
see dictation.py's window-icon comment). Worker threads describe a frame
with MarkFrame and let _apply_icon render it on the Qt thread.
"""

import math
import sys
import threading
import xml.etree.ElementTree as ET
from collections import namedtuple
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QGuiApplication, QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from samsara import config_defaults
from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara.log import get_logger
from samsara.quick_memo import memo_file
from samsara.support_feedback import open_support_tab
from samsara.ui import theme

import os

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# The mark (owner decisions 2026-09-13)
# ---------------------------------------------------------------------------

#: Capture state -> ring colour token and ring drawing. Fill (not colour)
#: is what says "recording"; spin and pulse are runtime.
MARK_CAPTURE = {
    "idle":      (theme.ICON_IDLE, "ring-hollow"),
    "listening": (theme.ACCENT, "ring-hollow"),
    "recording": (theme.RECORDING, "ring-filled"),
    "ava":       (theme.AVA, "ring-hollow"),
}
#: Hands-free state -> eye drawing (None = no eye). "heard" is the static
#: fallback frame of the heard animation: the open eye in RECORDING red with
#: the ring brightened (capture is starting, so red is truthful).
MARK_EYE = {
    "off":    None,
    "asleep": "eye-closed",
    "armed":  "eye-open",
    "heard":  "eye-open",
}
#: Every named state -> (capture, eye). The generated PNG set
#: (assets/icon/states/samsara_<state>_<size>.png) and the montage use these
#: names; the tray composes the live (capture, eye) pair directly.
MARK_STATES = {
    "off":       ("idle", "off"),
    "asleep":    ("idle", "asleep"),
    "idle":      ("idle", "off"),
    "listening": ("listening", "off"),
    "recording": ("recording", "off"),
    "ava":       ("ava", "off"),
    "armed":     ("listening", "armed"),
    "heard":     ("listening", "heard"),
}
#: App / exe / taskbar / window icon: brand cyan wheel, lid closed.
APP_MARK = ("listening", "asleep")
#: Wake phrase heard: (ms from the hit, eye state). The eye flashes red and
#: the ring brightens for ~1 s, then the lid closes as the command session
#: takes over; None hands the eye back to the live state.
HEARD_KEYFRAMES = (
    (0, "heard"), (250, "armed"), (500, "heard"), (750, "armed"),
    (1000, "asleep"), (1300, None),
)
#: Motion means capture (queue 19, correcting 09b3): the ring TURNS in every
#: active capture state and stands still only at rest. Speed is the state
#: channel: seconds per full turn of the ring. Recording keeps its filled band
#: and red -- fill and colour are the state, rotation is the liveness.
#:   armed         wake listener armed (ambient)
#:   listening     continuous mode waiting for speech (ambient, same pace)
#:   recording     capturing speech
#:   thinking      Ava / LLM working (indicator)
#:   transcribing  recording finished, speech-to-text running
SPIN_SECONDS_PER_TURN = {
    "armed": 3.0,
    "listening": 3.0,
    "recording": 1.5,
    "thinking": 2.4,
    "transcribing": 0.9,
}
#: Tray frame from any thread: rendered on the Qt thread by _apply_icon.
MarkFrame = namedtuple("MarkFrame", "capture eye rotation opacity", defaults=(0.0, 1.0))

_SMALL_MAX = 20          # sizes at or below this use the heavy small drawing (no head/tail)
#: Taskbar-facing icons (the tray icon; the app/exe/window .ico sizes) use the
#: heavy drawing up to this size: at 150% scaling Windows shows their 24 px
#: frame (and 32 px at 200%) in the same slot where 16 px sits at 100%.
TASKBAR_SMALL_MAX = 32
_FRAME_MAX = 24          # sizes at or below this draw from 24 pre-rendered 15-degree frames
_FRAME_STEPS = 24
_FRAME_CACHE_LIMIT = 2048
_HEARD_RING_LIFT = 0.45  # heard: ring colour mixed this far toward TEXT_PRIMARY
_TRAY_SIZES = (16, 24, 32)
_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)
_RING_IDS = ("ring-hollow", "ring-filled")
_EYE_IDS = ("eye-closed", "eye-open")
_renderer_cache: dict = {}
_frame_cache: dict = {}
_renderer_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Ouroboros ring geometry (the #regular drawing, 24 px and up)
# ---------------------------------------------------------------------------
# ONE CENTRELINE PER SEGMENT, stroked at a width that varies along the arc.
#
# Every segment's centreline is an arc of the ONE ring circle (RING_RADIUS,
# constant at every sample -- the head never leaves the circle). At the gap
# at 12 o'clock the segment that ENDS there (HEAD_SEGMENT) swells into a
# blunt snout whose nose runs HEAD_OVERSHOOT_DEG past its end into the gap;
# the segment that STARTS there (TAIL_SEGMENT) grows from a point. Head chases
# tail when the ring spins clockwise.
#
# The same centreline is stroked at two weights -- hollow states at
# RING_LINE_WIDTH, recording at RING_BAND_WIDTH -- each with its own head and
# tail profile (weight_profile). SVG strokes cannot vary in width, so the variable-width stroke is
# expanded here into ONE filled contour per segment (left edge forwards, tip
# cap, right edge back): no outlined band, no double contour. The tail tip and
# the nose end in round caps; the other ends are butt, so the 12-degree gaps
# stay open at full band weight. gen_icons.py writes the centrelines and both
# weights' expansions into assets/icon/samsara.svg; --check flags drift.

RING_CENTRE = 32.0
VIEWBOX_MARGIN = 0.5
RING_LINE_WIDTH = 3.0         # hollow weight (idle / listening / ava / armed)
RING_BAND_WIDTH = 11.0        # recording weight -- same centreline, its own taper
SEGMENT_START_DEG = -84.0     # segment 0 starts just right of 12 o'clock (y down = clockwise)
SEGMENT_SPAN_DEG = 108.0
SEGMENT_STEP_DEG = 120.0      # 12-degree gaps at 12, 4 and 8 o'clock
TAIL_SEGMENT = 0
HEAD_SEGMENT = 2
HEAD_FRACTION = 0.20          # head grows over the last 20% of its arc
HEAD_OVERSHOOT_DEG = 13.0     # the centreline runs this far past the head segment's end (both weights)
_SEGMENT_SAMPLES = 48
_NOSE_SAMPLES = 12
_CAP_SAMPLES = 8

#: Head and tail are WEIGHT-DEPENDENT (queue 19): one multiplier cannot serve
#: both. At the hollow weight (3) a 1.45x snout and short tail barely
#: register, so the hollow profile swells harder and tapers longer; at the
#: band weight (11) the same snout closed most of the 12 o'clock gap, so the
#: band profile swells less and its nose closes within the first quarter of
#: the overshoot; past the nose the stroke has zero width, so no thin tip
#: bridges the gap to the tail (the centreline itself is unchanged). Values between
#: the two weights are interpolated linearly.
#:   (head_scale, tail_fraction, tip_fraction, nose_fraction)
HOLLOW_PROFILE = (1.9, 0.45, 0.30, 1.00)
BAND_PROFILE = (1.25, 0.30, 0.22, 0.25)


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def weight_profile(weight: float) -> tuple:
    """(head_scale, tail_fraction, tip_fraction, nose_fraction) for a weight."""
    t = max(0.0, min(1.0, (weight - RING_LINE_WIDTH) / (RING_BAND_WIDTH - RING_LINE_WIDTH)))
    return tuple(h + (b - h) * t for h, b in zip(HOLLOW_PROFILE, BAND_PROFILE))


def head_scale(weight: float) -> float:
    return weight_profile(weight)[0]


def tail_fraction(weight: float) -> float:
    return weight_profile(weight)[1]


def tip_fraction(weight: float) -> float:
    return weight_profile(weight)[2]


#: The whole ring shrinks so the widest head of either weight still fits the
#: viewBox; no segment ever moves off the circle.
#: max(3 * 1.9, 11 * 1.25) = 13.75 -> 32 - 0.5 - 13.75 / 2 = 24.625.
RING_RADIUS = RING_CENTRE - VIEWBOX_MARGIN - max(
    RING_LINE_WIDTH * head_scale(RING_LINE_WIDTH),
    RING_BAND_WIDTH * head_scale(RING_BAND_WIDTH)) / 2.0


def stroke_scale(segment: int, u: float, weight: float = RING_BAND_WIDTH) -> float:
    """Stroke width at arc fraction u, as a multiple of the weight.

    With (H, T, TIP, N) = weight_profile(weight); u in [0, 1] runs from the
    segment's start angle to its end angle; the head segment also takes u > 1,
    up to HEAD_OVERSHOOT_DEG past its end:
      tail (segment TAIL_SEGMENT, u < T):
           TIP + (1 - TIP) * smoothstep(u / T)
      head (segment HEAD_SEGMENT, 1 - HEAD_FRACTION < u <= 1):
           1 + (H - 1) * smoothstep((u - (1 - HEAD_FRACTION)) / HEAD_FRACTION)
      nose (segment HEAD_SEGMENT, u > 1, v = (u - 1) / (N * overshoot / span)):
           max(TIP, H * sqrt(1 - v^2)) for v <= 1, then 0 (the stroke has ended)
      else 1
    """
    head, tail, tip, nose = weight_profile(weight)
    if segment == TAIL_SEGMENT and u < tail:
        return tip + (1.0 - tip) * _smoothstep(u / tail)
    if segment == HEAD_SEGMENT:
        if u > 1.0:
            v = (u - 1.0) / (nose * HEAD_OVERSHOOT_DEG / SEGMENT_SPAN_DEG)
            if v > 1.0 + 1e-9:
                return 0.0
            return max(tip, head * math.sqrt(max(0.0, 1.0 - v * v)))
        if u > 1.0 - HEAD_FRACTION:
            t = (u - (1.0 - HEAD_FRACTION)) / HEAD_FRACTION
            return 1.0 + (head - 1.0) * _smoothstep(t)
    return 1.0


def ring_width(segment: int, u: float, weight: float = RING_BAND_WIDTH) -> float:
    """Stroke width in viewBox units at arc fraction u for a weight."""
    return weight * stroke_scale(segment, u, weight)


def ring_centreline(segment: int) -> list[tuple[float, float, float, float]]:
    """The one centreline of a segment: (u, angle_rad, x, y) samples, all at
    RING_RADIUS. Both weights stroke exactly these samples."""
    start = SEGMENT_START_DEG + segment * SEGMENT_STEP_DEG
    fractions = [i / _SEGMENT_SAMPLES for i in range(_SEGMENT_SAMPLES + 1)]
    if segment == HEAD_SEGMENT:
        over = HEAD_OVERSHOOT_DEG / SEGMENT_SPAN_DEG
        fractions += [1.0 + over * i / _NOSE_SAMPLES for i in range(1, _NOSE_SAMPLES + 1)]
    samples = []
    for u in fractions:
        angle = math.radians(start + u * SEGMENT_SPAN_DEG)
        samples.append((u, angle,
                        RING_CENTRE + RING_RADIUS * math.cos(angle),
                        RING_CENTRE + RING_RADIUS * math.sin(angle)))
    return samples


def ring_centreline_path_data(segment: int) -> str:
    """The centreline as a single SVG arc (reference geometry in samsara.svg)."""
    samples = ring_centreline(segment)
    _u0, _a0, x0, y0 = samples[0]
    _u1, _a1, x1, y1 = samples[-1]
    r = RING_RADIUS
    return f"M {x0:.2f},{y0:.2f} A {r:.3f} {r:.3f} 0 0 1 {x1:.2f},{y1:.2f}"


def _cap(cx, cy, normal, tangent, half, forward):
    """Round cap points from the left edge around the tip to the right edge
    (forward) or from right around the back to left (not forward)."""
    nx, ny = normal
    tx, ty = tangent if forward else (-tangent[0], -tangent[1])
    sign = 1.0 if forward else -1.0
    points = []
    for i in range(1, _CAP_SAMPLES):
        phi = math.pi * i / _CAP_SAMPLES
        c, s = math.cos(phi), math.sin(phi)
        points.append((cx + half * (sign * c * nx + s * tx), cy + half * (sign * c * ny + s * ty)))
    return points


def ring_stroke_outline(segment: int, weight: float) -> list[tuple[float, float]]:
    """ONE closed contour: the segment's centreline stroked at weight *
    stroke_scale -- left (outer) edge forwards, end cap, right edge back,
    start cap. Round caps only at the tail tip and the nose."""
    samples = ring_centreline(segment)
    left, right = [], []
    for u, angle, x, y in samples:
        half = ring_width(segment, u, weight) / 2.0
        nx, ny = math.cos(angle), math.sin(angle)          # radial (outward) normal
        left.append((x + half * nx, y + half * ny))
        right.append((x - half * nx, y - half * ny))
    points = list(left)
    u_end, a_end, x_end, y_end = samples[-1]
    if segment == HEAD_SEGMENT:                              # nose: round cap
        points += _cap(x_end, y_end, (math.cos(a_end), math.sin(a_end)),
                       (-math.sin(a_end), math.cos(a_end)),
                       ring_width(segment, u_end, weight) / 2.0, forward=True)
    points += right[::-1]
    u0, a0, x0, y0 = samples[0]
    if segment == TAIL_SEGMENT:                              # tail tip: round cap
        points += _cap(x0, y0, (math.cos(a0), math.sin(a0)),
                       (-math.sin(a0), math.cos(a0)),
                       ring_width(segment, u0, weight) / 2.0, forward=False)
    return points


def ring_segment_path_data(segment: int, weight: float = RING_BAND_WIDTH) -> str:
    """SVG path data for one segment's variable-width stroke at a weight."""
    points = ring_stroke_outline(segment, weight)
    head = f"M {points[0][0]:.2f},{points[0][1]:.2f}"
    body = " ".join(f"L {x:.2f},{y:.2f}" for x, y in points[1:])
    return f"{head} {body} Z"


def mark_svg_path() -> Path:
    """assets/icon/samsara.svg in a source checkout or the frozen bundle."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets" / "icon" / "samsara.svg"
    return Path(__file__).resolve().parents[2] / "assets" / "icon" / "samsara.svg"


def mark_colours(capture: str, eye: str) -> tuple[str, str]:
    """(ring colour, eye colour) -- one token per state, except the heard frame."""
    colour = MARK_CAPTURE[capture][0]
    if eye == "heard":
        return theme._mix(colour, theme.TEXT_PRIMARY, _HEARD_RING_LIFT), theme.RECORDING
    return colour, colour


def mark_svg(capture: str, eye: str, small: bool, layer: str, source: bytes | None = None) -> bytes:
    """The SVG with only one layer ('ring' or 'eye') of one state visible."""
    ring_colour, eye_colour = mark_colours(capture, eye)
    ring_id = MARK_CAPTURE[capture][1]
    eye_id = MARK_EYE[eye]
    suffix = "-small" if small else ""

    root = ET.fromstring(source if source is not None else mark_svg_path().read_bytes())
    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}
    by_id["regular"].set("display", "none" if small else "inline")
    by_id["small"].set("display", "inline" if small else "none")
    for base in _RING_IDS + _EYE_IDS:
        wanted = (layer == "ring" and base == ring_id) or (layer == "eye" and base == eye_id)
        by_id[base + suffix].set("display", "inline" if wanted else "none")

    ring = by_id[ring_id + suffix]
    for attr in ("fill", "stroke"):
        if ring.get(attr, "none") != "none":
            ring.set(attr, ring_colour)
    if eye_id is not None:
        for el in by_id[eye_id + suffix].iter():
            role = el.get("data-role")
            if role == "eye":
                el.set("stroke", eye_colour)
            elif role == "pupil":
                el.set("fill", eye_colour)
    return ET.tostring(root, encoding="utf-8")


def clear_mark_caches() -> None:
    """Drop cached renderers and frames (after samsara.svg is rewritten)."""
    with _renderer_lock:
        _renderer_cache.clear()
        _frame_cache.clear()


def _renderer(capture: str, eye: str, small: bool, layer: str) -> QSvgRenderer | None:
    key = (capture, eye if layer == "eye" or eye == "heard" else "", small, layer)
    renderer = _renderer_cache.get(key)
    if renderer is None:
        try:
            renderer = QSvgRenderer(QByteArray(mark_svg(capture, eye, small, layer)))
        except (OSError, ET.ParseError, KeyError) as exc:
            logger.warning("[ICON] Samsara mark unavailable: %s", exc)
            return None
        if not renderer.isValid():
            logger.warning("[ICON] Samsara mark did not parse: %s", mark_svg_path())
            return None
        _renderer_cache[key] = renderer
    return renderer


def _uses_small(size: float, small_max: Optional[int]) -> bool:
    return size <= (_SMALL_MAX if small_max is None else small_max)


def _paint_vector(painter: QPainter, rect: QRectF, capture: str, eye: str,
                  rotation: float, opacity: float, small_max: Optional[int] = None) -> None:
    """Live vector render: ring rotated, eye upright (caller holds the lock)."""
    small = _uses_small(min(rect.width(), rect.height()), small_max)
    ring = _renderer(capture, eye, small, "ring")
    eye_renderer = _renderer(capture, eye, small, "eye") if MARK_EYE[eye] else None
    if ring is None:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
    centre = rect.center()
    painter.translate(centre)
    painter.rotate(rotation)
    painter.translate(-centre)
    ring.render(painter, rect)
    painter.restore()
    if eye_renderer is not None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
        eye_renderer.render(painter, rect)
        painter.restore()


def frame_step(rotation: float) -> int:
    """Which of the _FRAME_STEPS 15-degree frames shows a rotation (degrees)."""
    return int(round(rotation / (360.0 / _FRAME_STEPS))) % _FRAME_STEPS


def _frame(capture: str, eye: str, size: int, rotation: float,
           small_max: Optional[int] = None) -> QImage:
    """A pre-rendered frame for small sizes (caller holds the lock).

    At 16-24 px a live-rotated ring aliases badly, so spin cycles 24 frames
    in 15-degree steps, each rendered once from the vector and cached.
    """
    small = _uses_small(size, small_max)
    key = (capture, eye, size, frame_step(rotation), small)
    image = _frame_cache.get(key)
    if image is None:
        if len(_frame_cache) >= _FRAME_CACHE_LIMIT:
            _frame_cache.clear()
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        frame_painter = QPainter(image)
        _paint_vector(frame_painter, QRectF(0, 0, size, size), capture, eye,
                      key[3] * (360.0 / _FRAME_STEPS), 1.0, small_max)
        frame_painter.end()
        _frame_cache[key] = image
    return image


def paint_mark(painter: QPainter, rect: QRectF, capture: str, eye: str,
               rotation: float = 0.0, opacity: float = 1.0, *,
               small_max: Optional[int] = None) -> None:
    """Draw the mark into rect on an existing painter (Qt thread only).

    THE one drawing of the mark: tray, listening indicator, splash, the
    window header and gen_icons all come through here. rotation (degrees)
    spins the ring only -- the eye never turns; opacity fades the whole mark.
    Sizes up to small_max (default _SMALL_MAX = 20) use the heavy #small
    drawing; the taskbar-facing icons pass TASKBAR_SMALL_MAX. 16-24 px draws
    a cached 15-degree frame; 32 px and up rotates the vector live.
    """
    size = min(rect.width(), rect.height())
    with _renderer_lock:
        if size <= _FRAME_MAX:
            image = _frame(capture, eye, max(1, int(round(size))), rotation, small_max)
            painter.save()
            painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
            painter.drawImage(rect, image)
            painter.restore()
            return
        _paint_vector(painter, rect, capture, eye, rotation, opacity, small_max)


def render_mark(capture: str, eye: str, size: int,
                rotation: float = 0.0, opacity: float = 1.0, *,
                small_max: Optional[int] = None) -> QImage:
    """The mark as a transparent ARGB32 image (Qt thread only)."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_mark(painter, QRectF(0, 0, size, size), capture, eye, rotation, opacity,
               small_max=small_max)
    painter.end()
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def mark_icon(frame: MarkFrame) -> QIcon:
    """A multi-size QIcon for one frame, so Windows picks the DPI-right size.
    Every tray size uses the heavy drawing (TASKBAR_SMALL_MAX): at 150%
    scaling Windows shows the 24 px frame, which must be as present as 16."""
    icon = QIcon()
    for size in _TRAY_SIZES:
        icon.addPixmap(QPixmap.fromImage(
            render_mark(frame.capture, frame.eye, size, frame.rotation, frame.opacity,
                        small_max=TASKBAR_SMALL_MAX)))
    return icon

# Windows can leave a QSystemTrayIcon's shell-registered screen geometry
# stale after a sleep/resume cycle (no monitor topology change, so
# screenAdded/screenRemoved never fire) -- the next right-click's context
# menu then anchors to whatever stale/zeroed rect Windows reports, which
# can land near the primary screen's center instead of at the icon.
# _refresh_icon_registration() (hide+show) forces Windows to re-register
# it; this periodic timer is the catch-all for that no-topology-change
# case, on top of the immediate, event-driven refresh on actual screen
# add/remove/primary-change below.
_ICON_REFRESH_INTERVAL_MS = 20 * 60 * 1000  # 20 minutes


class SamsaraTrayQt(QObject):
    """QSystemTrayIcon wrapper matching pystray.Icon's property interface."""

    _icon_sig    = Signal(object)  # MarkFrame (rendered on the Qt thread)
    _tooltip_sig = Signal(str)
    _hide_sig    = Signal()
    _warning_sig = Signal(str, str)   # (title, text) -> balloon, from any thread

    def __init__(self, app):
        super().__init__()
        self._app  = app
        self._available_update = None
        self._tray = QSystemTrayIcon()
        self._menu = QMenu()
        self._menu.aboutToShow.connect(self._rebuild_menu)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.messageClicked.connect(self._on_message_clicked)

        # Wire thread-safe signals
        self._icon_sig.connect(self._apply_icon)
        self._tooltip_sig.connect(self._tray.setToolTip)
        self._hide_sig.connect(self._tray.hide)
        self._warning_sig.connect(self._show_warning_balloon)

        # Initial icon + tooltip
        try:
            self._apply_icon(app.create_icon_image())
        except Exception as e:
            logger.debug(f"__init__: {e}")
        self._tray.setToolTip("Samsara")
        self._tray.show()

        # A visible tray is not proof that startup succeeded: Whisper, CUDA,
        # VAD, and the listening services are still loading at this point.
        # Confirm an update only after DictationApp publishes 100% startup.
        # Until then the detached updater retains the rollback installation.
        self._startup_health_done = False
        self._startup_health_timer = QTimer(self)
        self._startup_health_timer.setInterval(250)
        self._startup_health_timer.timeout.connect(self._poll_startup_health)
        self._startup_health_timer.start()

        # See _ICON_REFRESH_INTERVAL_MS: periodic catch-all for the
        # sleep/resume case, plus an immediate refresh on any actual
        # monitor topology change (connect/disconnect, primary-screen
        # swap) -- both guard against the tray context menu popping up at
        # a stale, wrong position instead of anchored to the icon.
        self._icon_refresh_timer = QTimer(self)
        self._icon_refresh_timer.setInterval(_ICON_REFRESH_INTERVAL_MS)
        self._icon_refresh_timer.timeout.connect(self._refresh_icon_registration)
        self._icon_refresh_timer.start()

        gui_app = QGuiApplication.instance()
        if gui_app is not None:
            gui_app.screenAdded.connect(self._on_screen_topology_changed)
            gui_app.screenRemoved.connect(self._on_screen_topology_changed)
            gui_app.primaryScreenChanged.connect(self._on_screen_topology_changed)

    # ------------------------------------------------------------------
    # pystray-compatible property interface (all thread-safe)
    # ------------------------------------------------------------------

    @property
    def icon(self):
        return None

    @icon.setter
    def icon(self, pil_image):
        self._icon_sig.emit(pil_image)

    @property
    def title(self):
        return self._tray.toolTip()

    @title.setter
    def title(self, text: str):
        self._tooltip_sig.emit(str(text))

    def notify_warning(self, title: str, text: str) -> None:
        """Show a warning balloon (thread-safe). Used by the mouse-hotkey fallback (35)."""
        self._warning_sig.emit(str(title), str(text))

    def _show_warning_balloon(self, title: str, text: str) -> None:
        self._tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Warning, 12000)

    def stop(self):
        try:
            self._startup_health_timer.stop()
        except Exception:
            pass
        try:
            self._icon_refresh_timer.stop()
        except Exception:
            pass
        self._hide_sig.emit()

    # ------------------------------------------------------------------
    # Qt thread methods
    # ------------------------------------------------------------------

    def _refresh_icon_registration(self):
        """Re-register the tray icon with Windows so its context menu
        stops anchoring to a stale screen position.

        See _ICON_REFRESH_INTERVAL_MS. Skips silently while the menu is
        currently open so this can never yank an in-progress click/menu
        out from under the user; the next timer tick or topology-change
        event will simply try again.
        """
        if self._menu.isVisible():
            return
        try:
            self._tray.hide()
            self._tray.show()
        except Exception as exc:
            logger.debug(f"_refresh_icon_registration: {exc}")

    def _on_screen_topology_changed(self, *_args):
        """A monitor was connected/disconnected or the primary screen
        changed -- Qt/Windows are still settling screen geometry right as
        this fires, so refresh shortly after rather than immediately."""
        QTimer.singleShot(2000, self._refresh_icon_registration)

    def _poll_startup_health(self):
        """Confirm a replacement only after the whole app is operational."""
        if self._startup_health_done:
            return
        if bool(vars(self._app).get("_startup_failed", False)):
            # Do not acknowledge the new build. The detached helper observes
            # the missing health handshake and restores the previous version.
            self._startup_health_timer.stop()
            return

        progress = vars(self._app).get("_splash_progress", 0)
        if not isinstance(progress, (int, float)) or progress < 100:
            return

        self._startup_health_done = True
        self._startup_health_timer.stop()

        # 2026-07-20 incident: logging can silently freeze forever (see
        # dictation.py's _SafeRotatingFileHandler / _verify_logging_self_
        # check). Surface it here, once, after the app is confirmed fully
        # operational -- never blocks or delays startup itself.
        if bool(vars(self._app).get("_logging_self_check_failed", False)):
            self._tray.showMessage(
                "Samsara logging warning",
                "Logging may not be writing to disk correctly. If you need "
                "to report a bug, check that ~/.samsara/logs/samsara.log "
                "is updating.",
                QSystemTrayIcon.MessageIcon.Warning,
                10000,
            )

        # 2026-07-2x config-backup safeguard: config.json existed but
        # couldn't be read, so load_config() quarantined it and latched
        # save_config() off for this session (see dictation.py's
        # _quarantine_corrupt_config / save_config's latch check) rather
        # than silently overwriting it with defaults. Surface that once,
        # here, same pattern as the logging warning above.
        if bool(vars(self._app).get("_config_load_failed", False)):
            corrupt_name = vars(self._app).get("_config_corrupt_backup_name", None) or "a backup file"
            self._tray.showMessage(
                "Samsara settings warning",
                f"Settings failed to load — running on defaults; your saved "
                f"settings were preserved at {corrupt_name}. Restore via "
                f"Settings > Import.",
                QSystemTrayIcon.MessageIcon.Warning,
                15000,
            )

        try:
            from samsara.updater import reconcile_update_on_startup

            update_status = reconcile_update_on_startup()
            if update_status is not None:
                if update_status.state == "installed":
                    self._tray.showMessage(
                        "Samsara updated",
                        update_status.message,
                        QSystemTrayIcon.MessageIcon.Information,
                        8000,
                    )
                elif update_status.state in {"failed", "rolled_back"}:
                    self._tray.showMessage(
                        "Samsara update problem",
                        update_status.message,
                        QSystemTrayIcon.MessageIcon.Warning,
                        12000,
                    )
                elif update_status.state == "cleanup_pending":
                    self._tray.showMessage(
                        "Samsara update cleanup pending",
                        "Leftover update files remain from a previous update "
                        f"and cleanup will retry automatically. {update_status.message}",
                        QSystemTrayIcon.MessageIcon.Warning,
                        10000,
                    )
        except Exception as exc:
            logger.warning("[UPDATE] Could not reconcile previous update: %s", exc)

        # The coordinator is a no-op unless the user explicitly enabled
        # once-daily GitHub checks. Waiting for healthy startup means a broken
        # build neither confirms itself nor makes an update-network request.
        try:
            from samsara.ui.update_qt import maybe_start_automatic_update_check

            maybe_start_automatic_update_check(
                self._app, self._show_update_available,
            )
        except Exception as exc:
            logger.warning("[UPDATE] Could not schedule automatic check: %s", exc)

    def _apply_icon(self, frame):
        """Render a MarkFrame with render_mark (Qt thread). A PIL image is
        still accepted for callers/tests that pass one."""
        try:
            if isinstance(frame, MarkFrame):
                self._tray.setIcon(mark_icon(frame))
                return
            rgba = frame.convert("RGBA")
            data = rgba.tobytes()
            qi = QImage(data, rgba.width, rgba.height,
                        QImage.Format.Format_RGBA8888)
            self._tray.setIcon(QIcon(QPixmap.fromImage(qi)))
        except Exception as exc:
            print(f"[TRAY] Icon update failed: {exc}")

    def _on_activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            try:
                self._app.show_main_window()
            except Exception as e:
                logger.debug(f"_on_activated: {e}")

    def _open_update_dialog(self):
        from samsara.ui.update_qt import show_update_dialog

        show_update_dialog(
            self._app,
            check_immediately=self._available_update is None,
            initial_release=self._available_update,
        )

    def _show_update_available(self, release):
        """Runs on the Qt thread after an opted-in background check."""
        self._available_update = release
        self._tray.showMessage(
            "Samsara update available",
            f"Version {release.version} is ready. Click this notification or "
            "open the tray menu to install it.",
            QSystemTrayIcon.MessageIcon.Information,
            12000,
        )

    def _on_message_clicked(self):
        if self._available_update is not None:
            self._open_update_dialog()

    def _rebuild_menu(self):
        """Rebuild the full context menu from live app state.

        Called by QMenu.aboutToShow each time the user right-clicks the
        tray icon -- once per menu open, not on every hover.
        """
        app  = self._app
        menu = self._menu
        menu.clear()

        # ---- Show / hide hub ----
        show_act = menu.addAction("Show Samsara")
        show_act.triggered.connect(lambda: app.show_main_window())
        menu.setDefaultAction(show_act)
        # Mouse hotkey (32/35): state, re-enable without a restart, panic release.
        status_fn = getattr(app, 'mouse_hotkey_status', None)
        mouse_status = status_fn() if callable(status_fn) else {'state': 'n/a'}
        if mouse_status.get('state') == 'disabled':
            info = menu.addAction(f"Mouse hotkey disabled: {mouse_status.get('reason', '')}"[:90])
            info.setEnabled(False)
        if mouse_status.get('state') in ('active', 'disabled') and hasattr(app, 'reenable_mouse_hotkey'):
            menu.addAction("Re-enable mouse hotkey").triggered.connect(lambda: app.reenable_mouse_hotkey())
        if getattr(app, '_mouse_hook', None) is not None and hasattr(app, 'release_mouse_buttons'):
            menu.addAction("Release mouse buttons").triggered.connect(lambda: app.release_mouse_buttons())
        menu.addSeparator()

        # ---- Microphone submenu ----
        mic_label = f"[MIC]  {app.get_current_microphone_name()}"
        mic_sub = QMenu(mic_label)
        if not app._mic_refresh_blocked():
            try:
                app.available_mics = app.get_available_microphones()
                app._reconcile_microphone_selection()
            except Exception as e:
                logger.debug(f"_rebuild_menu: {e}")
        current_mic = app.config.get('microphone')
        for mic in app.available_mics:
            act = mic_sub.addAction(mic['name'])
            act.setCheckable(True)
            act.setChecked(mic['id'] == current_mic)
            act.triggered.connect(
                lambda checked, mid=mic['id']: app.switch_microphone_and_refresh(mid)
            )
        menu.addMenu(mic_sub)

        # ---- Mode submenu ----
        mode = app.config.get('mode', 'hold')
        mode_sub  = QMenu(f"Mode:  {mode.title()}")
        mode_grp  = QActionGroup(mode_sub)
        mode_grp.setExclusive(True)
        for label, val in [
            ("Hold to Talk",             "hold"),
            ("Toggle (click to start/stop)", "toggle"),
            ("Continuous",               "continuous"),
        ]:
            act = mode_sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(mode == val)
            act.triggered.connect(
                lambda checked, m=val: app.switch_mode_from_tray(m) if checked else None
            )
            mode_grp.addAction(act)
        menu.addMenu(mode_sub)

        # ---- Wake word ----
        ww_phrase = app.config.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)
        ww_label = f"Wake Word  ({ww_phrase})"
        # Wake models load lazily on their own thread (see dictation.py
        # wake_ready_state); say so rather than look enabled-but-deaf.
        wake_state_fn = getattr(app, 'wake_ready_state', None)
        if (app.config.get('wake_word_enabled', False) and callable(wake_state_fn)
                and wake_state_fn() != 'ready'):
            ww_label += "  - loading..."
        ww_act = menu.addAction(ww_label)
        ww_act.setCheckable(True)
        ww_act.setChecked(bool(app.config.get('wake_word_enabled', False)))
        ww_act.triggered.connect(
            lambda checked: app.set_wake_word_enabled(checked)
        )

        # ---- Streaming mode ----
        stream_act = menu.addAction("Streaming Mode  (CapsLock)")
        stream_act.setCheckable(True)
        stream_act.setChecked(bool(app.config.get('streaming_mode', False)))
        stream_act.triggered.connect(
            lambda checked: app.set_streaming_mode(checked)
        )

        # ---- Gesture lane ----
        gesture_act = menu.addAction("Gesture Lane  (webcam)")
        gesture_act.setCheckable(True)
        gesture_act.setChecked(bool(app.config.get('gesture', {}).get('enabled', False)))
        gesture_act.triggered.connect(
            lambda checked: app.set_gesture_enabled(checked)
        )

        # ---- Snooze submenu ----
        snoozed = getattr(app, 'snoozed', False)
        snooze_sub = QMenu("Snoozed" if snoozed else "Snooze")
        for label, mins in [
            ("5 minutes",    5),
            ("15 minutes",   15),
            ("30 minutes",   30),
            ("1 hour",       60),
            ("Until resumed", None),
        ]:
            act = snooze_sub.addAction(label)
            act.setEnabled(not snoozed)
            act.triggered.connect(
                lambda checked, m=mins: app.snooze_listening(m)
            )
        snooze_sub.addSeparator()
        resume_act = snooze_sub.addAction("Resume now")
        resume_act.setEnabled(snoozed)
        resume_act.triggered.connect(lambda: app.resume_listening())
        menu.addMenu(snooze_sub)

        menu.addSeparator()

        # ---- Daily-use quick access (2026-07-10 declutter pass) ----
        # One click for a daily user with chronic finger-joint pain: the
        # status/mode controls above stay here (operational toggles a user
        # adjusts routinely), plus the reference/visibility windows below.
        # Occasional tools and dev/debug surfaces are grouped into the
        # Tools / Developer submenus further down -- see there for the
        # full placement rationale.
        menu.addAction("Settings").triggered.connect(lambda: app.open_settings())
        # Beta testers without GitHub: one click to the email/diagnostics page.
        menu.addAction("Something wrong?").triggered.connect(
            lambda: open_support_tab(app))
        update_label = (
            f"Install Samsara v{self._available_update.version}…"
            if self._available_update is not None else
            "Check for Updates…"
        )
        menu.addAction(update_label).triggered.connect(self._open_update_dialog)
        menu.addAction("History").triggered.connect(lambda: app.open_history())
        menu.addAction("Open memos").triggered.connect(
            lambda: os.startfile(str(memo_file(app.config.get('memo_file') or None))))
        menu.addAction("Quick Reference").triggered.connect(
            lambda: app.open_quick_reference())

        cr_act = menu.addAction("Command Reference")
        cr_act.setCheckable(True)
        cr_act.setChecked(getattr(getattr(app, 'cheat_sheet', None), '_visible', False))
        cr_act.triggered.connect(lambda: app.toggle_cheat_sheet())

        li_act = menu.addAction("Show Listening Indicator")
        li_act.setCheckable(True)
        li_act.setChecked(bool(app.config.get('listening_indicator_enabled', False)))
        li_act.triggered.connect(lambda: app.toggle_listening_indicator())

        move_act = menu.addAction("Move listening indicator...")
        move_act.triggered.connect(lambda: app.enter_indicator_move_mode())

        menu.addSeparator()

        # ---- Tools submenu: occasionally-used setup/training/review tools ----
        tools_sub = QMenu("Tools")
        tools_sub.addAction("Interactive Tutorial").triggered.connect(
            lambda: app.show_tutorial())
        tools_sub.addSeparator()
        tools_sub.addAction("Mic Setup Guide").triggered.connect(
            lambda: app.open_mic_setup_guide())
        tools_sub.addAction("Ava Guide").triggered.connect(
            lambda: app.open_ava_guide())
        tools_sub.addAction("Voice Training").triggered.connect(
            lambda: app.open_voice_training())
        tools_sub.addAction("Benchmark Review").triggered.connect(
            lambda: app.open_benchmark_review())
        tools_sub.addAction("Correct Last Dictation").triggered.connect(
            lambda: app.open_correction_capture())
        tools_sub.addAction("Stress Test Wizard").triggered.connect(
            lambda: app.open_stress_test_wizard())
        tools_sub.addSeparator()
        tools_sub.addAction("Recalibrate Mic").triggered.connect(
            lambda: app.recalibrate_mic())
        tools_sub.addSeparator()

        cleanup_sub = QMenu("Cleanup")
        cleanup_grp = QActionGroup(cleanup_sub)
        cleanup_grp.setExclusive(True)
        cleanup_mode = app.config.get('cleanup_mode', 'clean')
        for label, val in [
            ("Clean  (remove fillers)", "clean"),
            ("Verbatim  (no cleanup)",  "verbatim"),
        ]:
            act = cleanup_sub.addAction(label)
            act.setCheckable(True)
            act.setChecked(cleanup_mode == val)
            act.triggered.connect(
                lambda checked, v=val: app.set_cleanup_mode(v) if checked else None
            )
            cleanup_grp.addAction(act)
        tools_sub.addMenu(cleanup_sub)

        tools_sub.addSeparator()
        info_hotkey = tools_sub.addAction(
            f"Hotkey:  {app.config.get('hotkey', config_defaults.DEFAULTS['hotkey'])}")
        info_hotkey.setEnabled(False)
        info_model = tools_sub.addAction(
            f"Model:  {app.config.get('model_size', config_defaults.DEFAULTS['model_size'])}")
        info_model.setEnabled(False)

        menu.addMenu(tools_sub)

        # ---- Developer submenu: debug/diagnostic surfaces ----
        dev_sub = QMenu("Developer")
        dev_sub.addAction("Dictation Diagnostics").triggered.connect(
            lambda: app.open_dictation_diagnostics())
        dev_sub.addAction("Wake Word Debug").triggered.connect(
            lambda: app.open_wake_word_debug())
        dev_sub.addAction("View Live Log").triggered.connect(
            lambda: app.open_log_viewer())
        dev_sub.addSeparator()
        dev_sub.addAction("Calibrate Echo Cancellation").triggered.connect(
            lambda: app.calibrate_echo_cancellation())
        dev_sub.addSeparator()
        dev_sub.addAction("Open Config Folder").triggered.connect(
            lambda: app.open_config_folder())
        dev_sub.addSeparator()
        dev_sub.addAction("Preview First-Run (fresh profile)").triggered.connect(
            lambda: app.preview_first_run())
        logs_sub = QMenu("View Logs")
        logs_sub.addAction("Main Log").triggered.connect(
            lambda: app.open_main_log())
        logs_sub.addAction("Voice Training Log").triggered.connect(
            lambda: app.open_voice_training_log())
        dev_sub.addMenu(logs_sub)

        menu.addMenu(dev_sub)
        menu.addSeparator()

        menu.addAction("Exit").triggered.connect(lambda: app.quit_app())
