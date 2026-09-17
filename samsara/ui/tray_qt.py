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

from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara.log import get_logger
from samsara.quick_memo import memo_file
from samsara.support_feedback import open_support_tab
from samsara.ui import theme
from samsara.updater import update_check_menu_label

import os

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# The mark (owner decisions 2026-09-13)
# ---------------------------------------------------------------------------


#: Capture state -> ring colour token and ring drawing. Fill (not colour)
#: is what says "recording"; spin and pulse are runtime. Ava uses the
#: thicker brand-weight hollow ring so listening and Ava remain distinct when
#: their colour tokens cannot be seen.
def mark_capture():
    return {
        "idle":      (theme.ICON_IDLE, "ring-hollow"),
        "listening": (theme.ACCENT, "ring-hollow"),
        "recording": (theme.RECORDING, "ring-filled"),
        "ava":       (theme.AVA, "ring-brand"),
    }


#: BRAND presentation (38): the lockup beside the app's name and Home's
#: 60 px mark show the brand, not the tray's state vocabulary. At rest the
#: ring is ACCENT at the brand weight (#ring-brand) with the eye always
#: present -- lid closed when hands-free is off, open when armed. Idle grey
#: and the eyeless ring are tray rules for 16 px and are retired here;
#: state is carried by motion, the eye, and RECORDING red while recording.
#: The tray keeps mark_capture() / MARK_EYE unchanged.
def brand_capture():
    return {
        "idle":      (theme.ACCENT, "ring-brand"),
        "listening": (theme.ACCENT, "ring-brand"),
        "recording": (theme.RECORDING, "ring-filled"),
        "ava":       (theme.AVA, "ring-brand"),
    }
BRAND_EYE = {
    "off":    "eye-closed",
    "asleep": "eye-closed",
    "armed":  "eye-open",
    "heard":  "eye-open",
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
_RING_IDS = ("ring-hollow", "ring-filled", "ring-brand")
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
# the nose end round (the band's nose closes as a half-ellipse, 42); the other
# ends are butt, so the 12-degree gaps
# stay open at full band weight. gen_icons.py writes the centrelines and both
# weights' expansions into assets/icon/samsara.svg; --check flags drift.

RING_CENTRE = 32.0
VIEWBOX_MARGIN = 0.5
RING_LINE_WIDTH = 3.0         # hollow weight (idle / listening / ava / armed)
RING_BAND_WIDTH = 11.0        # recording weight -- same centreline, its own taper
#: Brand weight (38): the header lockup at 26 px and Home's 60 px mark.
#: Chosen from the candidate sheet (reports/38/artifacts/candidates.png and
#: ui_proof/ouroboros_weights.png, 26 px row): 3.0-4.5 are hairlines at
#: 26 px, 5.0 is the first weight whose head reads, 5.5 keeps head and tail
#: visible at 26 px and clearly at 60 px without the ring turning into a
#: band. Same centreline; the head/tail profile is interpolated for it.
RING_BRAND_WIDTH = 5.5
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
#: band weight (11) the head swells less. Past the nose the stroke has zero
#: width, so no thin tip bridges the gap to the tail (the centreline itself is
#: unchanged). Profiles are interpolated linearly between three ANCHORS --
#: hollow, brand and band -- so a change to the band never moves the brand.
#:   (head_scale, tail_fraction, tip_fraction, nose_fraction)
HOLLOW_PROFILE = (1.9, 0.45, 0.30, 1.00)
#: Brand weight (38), frozen at the values it was drawn with (the 38
#: interpolation between hollow and the old band profile): its head already
#: reads (half-width 4.97, nose 3.94 units over 9 samples), so 42 leaves it
#: byte-for-byte alone.
BRAND_PROFILE = (1.80625, 0.403125, 0.275, 0.765625)
#: 38: the 1.25x band head read as a flat stub; 1.6x is kept (BAND_HEAD_MIN is
#: the floor the identity test holds it to).
BAND_HEAD_MIN = 1.5
#: 42: at 1.6x the band head STILL read as a square cut, and the width was
#: not the cause -- the nose was. The old band nose was 1.29 units long on a
#: head 8.80 units half-wide (3 samples): a rounded end that short on a head
#: that wide is a flat face. The fix lengthens the nose BACKWARDS: the widest
#: point of the head moves BAND_NOSE_LEAD of the span back into the segment
#: (nose_lead), and the nose closes from there as a true half-ellipse -- no
#: tip clamp, no stub cap -- so the nose IS the round cap, about as long as
#: the head is half-wide. Its front stays exactly where the old nose ended
#: (nose fraction 0.25 of the overshoot, unchanged), so the 12 o'clock gap is
#: no narrower. The lead is a function of weight: zero at and below the brand
#: weight (hollow and brand keep the 38 nose and sampling byte-for-byte),
#: BAND_NOSE_LEAD at the band.
BAND_PROFILE = (1.6, 0.30, 0.22, 0.25)
BAND_NOSE_LEAD = 0.175
#: The nose of a weight with a lead (widest point to stroke end) must be at
#: least this multiple of the head's half-width; below it the end reads as a
#: cut, not a head.
NOSE_MIN_RATIO = 0.9
#: A weight with a leading nose gets this many nose samples, spaced densest
#: at the front where the half-ellipse turns fastest (smooth at 128 px).
_LEAD_NOSE_SAMPLES = 32


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _anchor_blend(weight: float, low: tuple, mid: tuple, high: tuple) -> tuple:
    """Piecewise-linear blend through hollow (low), brand (mid) and band
    (high); exactly an anchor's values at that anchor's weight."""
    if weight <= RING_LINE_WIDTH:
        return tuple(low)
    if weight == RING_BRAND_WIDTH:
        return tuple(mid)
    if weight >= RING_BAND_WIDTH:
        return tuple(high)
    if weight < RING_BRAND_WIDTH:
        a, b, lo, hi = RING_LINE_WIDTH, RING_BRAND_WIDTH, low, mid
    else:
        a, b, lo, hi = RING_BRAND_WIDTH, RING_BAND_WIDTH, mid, high
    t = (weight - a) / (b - a)
    return tuple(x + (y - x) * t for x, y in zip(lo, hi))


def weight_profile(weight: float) -> tuple:
    """(head_scale, tail_fraction, tip_fraction, nose_fraction) for a weight."""
    return _anchor_blend(weight, HOLLOW_PROFILE, BRAND_PROFILE, BAND_PROFILE)


def nose_lead(weight: float) -> float:
    """Fraction of the span before the head segment's end at which the head is
    widest and the nose begins: 0 at and below the brand weight (the nose
    starts at the segment end, as in 38), BAND_NOSE_LEAD at the band."""
    return _anchor_blend(weight, (0.0,), (0.0,), (BAND_NOSE_LEAD,))[0]


def head_scale(weight: float) -> float:
    return weight_profile(weight)[0]


def tail_fraction(weight: float) -> float:
    return weight_profile(weight)[1]


def tip_fraction(weight: float) -> float:
    return weight_profile(weight)[2]


#: The whole ring shrinks so the widest head of either weight still fits the
#: viewBox; no segment ever moves off the circle.
#: max(3 * 1.9, 11 * 1.6) = 17.6 -> 32 - 0.5 - 17.6 / 2 = 22.7.
RING_RADIUS = RING_CENTRE - VIEWBOX_MARGIN - max(
    RING_LINE_WIDTH * head_scale(RING_LINE_WIDTH),
    RING_BAND_WIDTH * head_scale(RING_BAND_WIDTH)) / 2.0


def stroke_scale(segment: int, u: float, weight: float = RING_BAND_WIDTH) -> float:
    """Stroke width at arc fraction u, as a multiple of the weight.

    With (H, T, TIP, N) = weight_profile(weight), L = nose_lead(weight),
    P = 1 - L (the head's widest point) and E = 1 + N * overshoot / span (the
    stroke's end); u in [0, 1] runs from the segment's start angle to its end
    angle, and the head segment also takes u > 1, up to HEAD_OVERSHOOT_DEG
    past its end:
      tail (segment TAIL_SEGMENT, u < T):
           TIP + (1 - TIP) * smoothstep(u / T)
      head (segment HEAD_SEGMENT, P - HEAD_FRACTION < u <= P):
           1 + (H - 1) * smoothstep((u - (P - HEAD_FRACTION)) / HEAD_FRACTION)
      nose (segment HEAD_SEGMENT, u > P, v = (u - P) / (E - P)):
           L = 0:  max(TIP, H * sqrt(1 - v^2)) for v <= 1, then 0
           L > 0:  H * sqrt(1 - v^2) for v <= 1, then 0 (a true half-ellipse:
                   the nose closes to its own round front)
      else 1
    With L = 0 (hollow, brand) this is exactly the 38 formula: widest at the
    segment end, nose entirely inside the overshoot, blunt TIP stub.
    """
    head, tail, tip, nose = weight_profile(weight)
    if segment == TAIL_SEGMENT and u < tail:
        return tip + (1.0 - tip) * _smoothstep(u / tail)
    if segment == HEAD_SEGMENT:
        lead = nose_lead(weight)
        peak = 1.0 - lead
        if u > peak:
            v = (u - peak) / (lead + nose * HEAD_OVERSHOOT_DEG / SEGMENT_SPAN_DEG)
            if v > 1.0 + 1e-9:
                return 0.0
            dome = head * math.sqrt(max(0.0, 1.0 - v * v))
            return dome if lead > 0.0 else max(tip, dome)
        if u > peak - HEAD_FRACTION:
            t = (u - (peak - HEAD_FRACTION)) / HEAD_FRACTION
            return 1.0 + (head - 1.0) * _smoothstep(t)
    return 1.0


def ring_width(segment: int, u: float, weight: float = RING_BAND_WIDTH) -> float:
    """Stroke width in viewBox units at arc fraction u for a weight."""
    return weight * stroke_scale(segment, u, weight)


def _nose_span(weight: float) -> tuple[float, float]:
    """(u of the head's widest point, u where the stroke ends) for a weight."""
    lead = nose_lead(weight)
    nose = weight_profile(weight)[3]
    return 1.0 - lead, 1.0 + nose * HEAD_OVERSHOOT_DEG / SEGMENT_SPAN_DEG


def ring_centreline(segment: int, weight: Optional[float] = None) -> list[tuple[float, float, float, float]]:
    """A segment's centreline: (u, angle_rad, x, y) samples, all at RING_RADIUS.

    Without a weight (and for any weight without a nose lead -- hollow and
    brand) these are the 38 samples every weight strokes. A weight with a
    nose lead (the band) keeps every 38 sample before its widest point, on
    the same circle, then _LEAD_NOSE_SAMPLES nose samples from the widest
    point to the stroke end, spaced sin-wise so they crowd the round front,
    and stops there."""
    start = SEGMENT_START_DEG + segment * SEGMENT_STEP_DEG
    fractions = [i / _SEGMENT_SAMPLES for i in range(_SEGMENT_SAMPLES + 1)]
    if segment == HEAD_SEGMENT:
        over = HEAD_OVERSHOOT_DEG / SEGMENT_SPAN_DEG
        fractions += [1.0 + over * i / _NOSE_SAMPLES for i in range(1, _NOSE_SAMPLES + 1)]
        if weight is not None and nose_lead(weight) > 0.0:
            peak, end = _nose_span(weight)
            dense = [peak + (end - peak) * math.sin(0.5 * math.pi * i / _LEAD_NOSE_SAMPLES)
                     for i in range(_LEAD_NOSE_SAMPLES + 1)]
            fractions = [u for u in fractions if u < peak - 1e-9] + dense
    samples = []
    for u in fractions:
        angle = math.radians(start + u * SEGMENT_SPAN_DEG)
        samples.append((u, angle,
                        RING_CENTRE + RING_RADIUS * math.cos(angle),
                        RING_CENTRE + RING_RADIUS * math.sin(angle)))
    return samples


def nose_metrics(weight: float) -> tuple[float, float, int]:
    """(head half-width, nose length, nose sample points) in viewBox units.

    The nose runs from the head's widest point to where the stroke ends,
    measured along the ring; its sample points are the centreline samples
    stroked with a non-zero width in (widest point, stroke end]."""
    peak, end = _nose_span(weight)
    half = weight * head_scale(weight) / 2.0
    length = RING_RADIUS * math.radians((end - peak) * SEGMENT_SPAN_DEG)
    points = sum(1 for u, *_rest in ring_centreline(HEAD_SEGMENT, weight)
                 if peak + 1e-9 < u <= end + 1e-9 and ring_width(HEAD_SEGMENT, u, weight) > 0.0)
    return half, length, points


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
    start cap. Round caps only at the tail tip and the nose; a nose with a
    lead (the band) is its own round front, so it takes no extra cap."""
    samples = ring_centreline(segment, weight)
    left, right = [], []
    for u, angle, x, y in samples:
        half = ring_width(segment, u, weight) / 2.0
        nx, ny = math.cos(angle), math.sin(angle)          # radial (outward) normal
        left.append((x + half * nx, y + half * ny))
        right.append((x - half * nx, y - half * ny))
    points = list(left)
    u_end, a_end, x_end, y_end = samples[-1]
    if segment == HEAD_SEGMENT and nose_lead(weight) == 0.0:   # 38 nose: round cap
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


def mark_colours(capture: str, eye: str, brand: bool = False) -> tuple[str, str]:
    """(ring colour, eye colour) -- one token per state, except the heard
    frame. brand=True uses brand_capture(): never ICON_IDLE."""
    colour = (brand_capture() if brand else mark_capture())[capture][0]
    if eye == "heard":
        return theme._mix(colour, theme.TEXT_PRIMARY, _HEARD_RING_LIFT), theme.RECORDING
    return colour, colour


def mark_eye_id(eye: str, brand: bool = False) -> Optional[str]:
    """The eye drawing for a hands-free state: MARK_EYE (tray: none when
    off) or BRAND_EYE (always present)."""
    return (BRAND_EYE if brand else MARK_EYE)[eye]


def mark_svg(capture: str, eye: str, small: bool, layer: str, source: bytes | None = None,
             brand: bool = False) -> bytes:
    """The SVG with only one layer ('ring' or 'eye') of one state visible.
    brand=True selects the brand presentation (brand_capture() / BRAND_EYE);
    the brand never uses the #small drawing."""
    if brand:
        small = False
    ring_colour, eye_colour = mark_colours(capture, eye, brand)
    ring_id = (brand_capture() if brand else mark_capture())[capture][1]
    eye_id = mark_eye_id(eye, brand)
    suffix = "-small" if small else ""

    root = ET.fromstring(source if source is not None else mark_svg_path().read_bytes())
    by_id = {el.get("id"): el for el in root.iter() if el.get("id")}
    by_id["regular"].set("display", "none" if small else "inline")
    by_id["small"].set("display", "inline" if small else "none")
    for base in _RING_IDS + _EYE_IDS:
        wanted = (layer == "ring" and base == ring_id) or (layer == "eye" and base == eye_id)
        el = by_id.get(base + suffix)
        if el is not None:                       # #small has no brand ring
            el.set("display", "inline" if wanted else "none")

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
    """Drop cached renderers and frames (after samsara.svg is rewritten).

    A palette switch does NOT need this -- the active theme is part of every
    cache key, so the two themes' marks coexist rather than evict each other."""
    with _renderer_lock:
        _renderer_cache.clear()
        _frame_cache.clear()


def _renderer(capture: str, eye: str, small: bool, layer: str,
              brand: bool = False) -> QSvgRenderer | None:
    # theme.active_theme() is part of the key: the SVG is coloured from the
    # palette, so a cached renderer from the other theme would paint the
    # dark cyan mark onto a white header (queue 129).
    key = (capture, eye if layer == "eye" or eye == "heard" else "", small, layer,
           brand, theme.active_theme())
    renderer = _renderer_cache.get(key)
    if renderer is None:
        try:
            renderer = QSvgRenderer(QByteArray(mark_svg(capture, eye, small, layer, brand=brand)))
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
                  rotation: float, opacity: float, small_max: Optional[int] = None,
                  brand: bool = False) -> None:
    """Live vector render: ring rotated, eye upright (caller holds the lock)."""
    small = False if brand else _uses_small(min(rect.width(), rect.height()), small_max)
    ring = _renderer(capture, eye, small, "ring", brand)
    eye_renderer = _renderer(capture, eye, small, "eye", brand) if mark_eye_id(eye, brand) else None
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
           small_max: Optional[int] = None, brand: bool = False) -> QImage:
    """A pre-rendered frame for small sizes (caller holds the lock).

    At 16-24 px a live-rotated ring aliases badly, so spin cycles 24 frames
    in 15-degree steps, each rendered once from the vector and cached.
    """
    small = False if brand else _uses_small(size, small_max)
    key = (capture, eye, size, frame_step(rotation), small, brand,
           theme.active_theme())
    image = _frame_cache.get(key)
    if image is None:
        if len(_frame_cache) >= _FRAME_CACHE_LIMIT:
            _frame_cache.clear()
        image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        frame_painter = QPainter(image)
        _paint_vector(frame_painter, QRectF(0, 0, size, size), capture, eye,
                      key[3] * (360.0 / _FRAME_STEPS), 1.0, small_max, brand)
        frame_painter.end()
        _frame_cache[key] = image
    return image


def paint_mark(painter: QPainter, rect: QRectF, capture: str, eye: str,
               rotation: float = 0.0, opacity: float = 1.0, *,
               small_max: Optional[int] = None, brand: bool = False) -> None:
    """Draw the mark into rect on an existing painter (Qt thread only).

    THE one drawing of the mark: tray, listening indicator, splash, the
    window header and gen_icons all come through here. rotation (degrees)
    spins the ring only -- the eye never turns; opacity fades the whole mark.
    Sizes up to small_max (default _SMALL_MAX = 20) use the heavy #small
    drawing; the taskbar-facing icons pass TASKBAR_SMALL_MAX. 16-24 px draws
    a cached 15-degree frame; 32 px and up rotates the vector live.
    brand=True is the presentation for the window header and Home (38):
    brand_capture() colours at RING_BRAND_WIDTH, the eye always present,
    never the #small drawing.
    """
    size = min(rect.width(), rect.height())
    with _renderer_lock:
        if size <= _FRAME_MAX:
            image = _frame(capture, eye, max(1, int(round(size))), rotation, small_max, brand)
            painter.save()
            painter.setOpacity(painter.opacity() * max(0.0, min(1.0, opacity)))
            painter.drawImage(rect, image)
            painter.restore()
            return
        _paint_vector(painter, rect, capture, eye, rotation, opacity, small_max, brand)


def render_mark(capture: str, eye: str, size: int,
                rotation: float = 0.0, opacity: float = 1.0, *,
                small_max: Optional[int] = None, brand: bool = False) -> QImage:
    """The mark as a transparent ARGB32 image (Qt thread only)."""
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    paint_mark(painter, QRectF(0, 0, size, size), capture, eye, rotation, opacity,
               small_max=small_max, brand=brand)
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
                on_error=self._show_automatic_update_failure,
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

    def _show_automatic_update_failure(self, error):
        self._report_action_failure(
            "Automatic update check",
            "Couldn't check for updates automatically; Samsara was not changed. "
            f"Check your connection and try again. ({error})",
        )

    def _on_message_clicked(self):
        if self._available_update is not None:
            self._open_update_dialog()

    # ---- Guarded actions -------------------------------------------------
    #
    # 61 (2026-09-14): "Open memos" raised inside a Qt slot when no memo had
    # been recorded; PySide printed the traceback to stderr and the user got
    # nothing. Every tray action is connected through _safe(), so a handler
    # that raises is logged AND shown as a tray notification naming the item.

    def _report_action_failure(self, label: str, message: str) -> None:
        try:
            self._tray.showMessage(
                f"Samsara: {label}", message,
                QSystemTrayIcon.MessageIcon.Warning, 8000,
            )
        except Exception as exc:  # the report itself must never raise
            logger.warning("[TRAY] could not show failure for %r: %s", label, exc)

    def _safe(self, label: str, fn):
        """A slot for ``fn`` that never raises: a failure is logged and shown
        to the user as a tray notification naming ``label``."""
        def _slot(*args):
            try:
                return fn(*args)
            except Exception as exc:
                logger.exception("[TRAY] %s failed: %s", label, exc)
                self._report_action_failure(label, f"That didn't work: {exc}")
                return None
        return _slot

    def _add(self, menu, label: str, fn, *, checked=None, report_as: Optional[str] = None):
        """Add an action wired through _safe. ``fn`` takes no argument unless
        ``checked`` is given; then the action is checkable and ``fn`` receives
        its new checked state. The state is read from the action itself: PySide
        passes nothing to a ``*args`` slot, so Qt's ``checked`` never arrives."""
        act = menu.addAction(label)
        name = report_as or label
        if checked is None:
            act.triggered.connect(self._safe(name, lambda *_a: fn()))
        else:
            act.setCheckable(True)
            act.setChecked(bool(checked))
            act.triggered.connect(self._safe(name, lambda *_a: fn(act.isChecked())))
        return act

    # ---- Action bodies that used to fail without a word ------------------

    def _open_memos(self):
        """Open the memo LIST (queue 92), falling back to the raw file.

        Queue 07 opened memos.md in Notepad because there was nothing else
        to open. There is now: a page that plays the audio, searches the
        transcripts and files them by category. The raw file is still one
        click away inside it, and still what this opens on a build where the
        hub window is not available.
        """
        opener = getattr(self._app, "open_hub_page", None)
        if callable(opener):
            try:
                # Imported here, not at module scope: home_qt imports this
                # module for the mark, so a top-level import would be a cycle.
                from samsara.ui.home_qt import MEMOS  # noqa: PLC0415
                # `is True`, not truthiness: open_hub_page's contract is a
                # real bool, and anything else (a stub, a half-built hub)
                # means "I did not open it" -- fall back rather than leave
                # the user looking at a tray item that did nothing.
                if opener(MEMOS) is True:
                    return
            except Exception as exc:
                logger.debug("[TRAY] Could not open the memo list: %s", exc)
        self._open_memo_file()

    def _open_memo_file(self):
        """Open the raw markdown, creating it first when no memo exists yet.

        Created rather than refused: a missing file only means nothing has
        been recorded yet, and opening the header-only file shows the user
        where memos will land. The header matches quick_memo.append_memo,
        which appends to an existing file without writing a second header.
        """
        path = memo_file(self._app.config.get('memo_file') or None)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with path.open("x", encoding="utf-8") as fh:
                    fh.write("# Memos\n\n")
                logger.info("[TRAY] Created empty memo file %s", path)
            except FileExistsError:
                pass  # a memo was recorded in between -- open that one
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            raise OSError(f"cannot open files on this platform ({path})")
        startfile(str(path))

    _GUIDE_OPENERS = {
        "mic_setup_wizard": "open_mic_setup_guide",
        "ava_guide": "open_ava_guide",
    }

    def _open_guide(self, attr: str, label: str):
        """open_mic_setup_guide / open_ava_guide silently do nothing when the
        window was never built; say so instead of ignoring the click."""
        if getattr(self._app, attr, None) is None:
            self._report_action_failure(label, f"{label} isn't available right now.")
            return
        getattr(self._app, self._GUIDE_OPENERS[attr])()

    def _update_entry_enabled(self) -> bool:
        """updates.tray_menu_entry, default OFF.

        Its label still comes from updater availability, so source and
        isolated runs do not promise a GitHub check they cannot make.
        """
        settings = self._app.config.get('updates', {})
        return isinstance(settings, dict) and settings.get('tray_menu_entry', False) is True

    def _reset_preview_position(self):
        from samsara.streaming import reset_preview_placement  # noqa: PLC0415
        if not reset_preview_placement(self._app):
            raise RuntimeError("Dictation preview reset is unavailable")
        return True

    def _reset_indicator_position(self):
        from samsara.ui.listening_indicator import reset_indicator_placement  # noqa: PLC0415
        if not reset_indicator_placement(self._app):
            raise RuntimeError("Listening indicator reset is unavailable")
        return True

    def _reset_floating_window_positions(self):
        self._reset_preview_position()
        self._reset_indicator_position()

    def _rebuild_menu(self):
        """Rebuild the full context menu from live app state.

        Called by QMenu.aboutToShow each time the user right-clicks the
        tray icon -- once per menu open, not on every hover.

        Recovery-first grouping: a person whose voice control stopped needs
        app visibility, listening/mic/mode controls, instructions, settings,
        and support without opening another submenu. Everyday records live
        in Workspace; setup and one-off tools live in Tools; debug surfaces
        live in Developer. Every action goes through _add(), so none can fail
        silently.
        """
        app  = self._app
        menu = self._menu
        menu.clear()
        add = self._add

        # ---- Show / hide hub ----
        show_act = add(menu, "Show Samsara", lambda: app.show_main_window())
        menu.setDefaultAction(show_act)
        # Mouse hotkey (32/35): state, re-enable without a restart, panic release.
        status_fn = getattr(app, 'mouse_hotkey_status', None)
        mouse_status = status_fn() if callable(status_fn) else {'state': 'n/a'}
        if mouse_status.get('state') == 'disabled':
            info = menu.addAction(f"Mouse hotkey disabled: {mouse_status.get('reason', '')}"[:90])
            info.setEnabled(False)
        if mouse_status.get('state') in ('active', 'disabled') and hasattr(app, 'reenable_mouse_hotkey'):
            add(menu, "Re-enable mouse hotkey", lambda: app.reenable_mouse_hotkey())
        if getattr(app, '_mouse_hook', None) is not None and hasattr(app, 'release_mouse_buttons'):
            add(menu, "Release mouse buttons", lambda: app.release_mouse_buttons())
        if self._available_update is not None and self._update_entry_enabled():
            add(menu, f"Install Samsara v{self._available_update.version}\u2026",
                self._open_update_dialog, report_as="Install update")
        menu.addSeparator()

        # ---- Snooze submenu: pause / resume listening ----
        snoozed = getattr(app, 'snoozed', False)
        snooze_sub = QMenu("Snoozed" if snoozed else "Snooze")
        for label, mins in [
            ("5 minutes",    5),
            ("15 minutes",   15),
            ("30 minutes",   30),
            ("1 hour",       60),
            ("Until resumed", None),
        ]:
            act = add(snooze_sub, label, lambda m=mins: app.snooze_listening(m), report_as="Snooze")
            act.setEnabled(not snoozed)
        snooze_sub.addSeparator()
        resume_act = add(snooze_sub, "Resume now", lambda: app.resume_listening())
        resume_act.setEnabled(snoozed)
        menu.addMenu(snooze_sub)

        # ---- Wake word ----
        ww_phrase = app.config.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)
        ww_label = f"Wake Word  ({ww_phrase})"
        # Wake models load lazily on their own thread (see dictation.py
        # wake_ready_state); say so rather than look enabled-but-deaf.
        wake_state_fn = getattr(app, 'wake_ready_state', None)
        if (app.config.get('wake_word_enabled', False) and callable(wake_state_fn)
                and wake_state_fn() != 'ready'):
            ww_label += "  - loading..."
        add(menu, ww_label, lambda checked: app.set_wake_word_enabled(checked),
            checked=app.config.get('wake_word_enabled', False), report_as="Wake Word")

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
            add(mic_sub, mic['name'],
                lambda _checked, mid=mic['id']: app.switch_microphone_and_refresh(mid),
                checked=mic['id'] == current_mic, report_as="Switch microphone")
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
            act = add(mode_sub, label,
                      lambda checked, m=val: app.switch_mode_from_tray(m) if checked else None,
                      checked=mode == val, report_as="Change mode")
            mode_grp.addAction(act)
        menu.addMenu(mode_sub)

        menu.addSeparator()

        # ---- Recovery first ----
        add(menu, "Quick Reference", lambda: app.open_quick_reference())
        add(menu, "Settings", lambda: app.open_settings())
        add(menu, "Something wrong?", lambda: open_support_tab(app))

        # ---- Everyday records, out of the recovery path ----
        workspace_sub = QMenu("Workspace")
        add(workspace_sub, "History", lambda: app.open_history())
        add(workspace_sub, "Open memos", self._open_memos)
        add(workspace_sub, "Open the raw memo file", self._open_memo_file)
        menu.addMenu(workspace_sub)

        menu.addSeparator()

        # ---- Tools submenu: setup toggles, overlays, one-off tools ----
        tools_sub = QMenu("Tools")
        window_recovery = QMenu("Window recovery")
        add(window_recovery, "Reset dictation preview", self._reset_preview_position,
            report_as="Reset dictation preview")
        add(window_recovery, "Reset listening indicator", self._reset_indicator_position,
            report_as="Reset listening indicator")
        add(window_recovery, "Reset both floating windows", self._reset_floating_window_positions,
            report_as="Reset floating windows")
        tools_sub.addMenu(window_recovery)
        tools_sub.addSeparator()
        add(tools_sub, "Streaming Mode  (CapsLock)", lambda checked: app.set_streaming_mode(checked),
            checked=app.config.get('streaming_mode', False), report_as="Streaming Mode")
        add(tools_sub, "Gesture Lane  (webcam)", lambda checked: app.set_gesture_enabled(checked),
            checked=app.config.get('gesture', {}).get('enabled', False), report_as="Gesture Lane")
        add(tools_sub, "Command Reference", lambda _checked: app.toggle_cheat_sheet(),
            checked=getattr(getattr(app, 'cheat_sheet', None), '_visible', False))
        add(tools_sub, "Show Listening Indicator", lambda _checked: app.toggle_listening_indicator(),
            checked=app.config.get('listening_indicator_enabled', False))
        add(tools_sub, "Move listening indicator...", lambda: app.enter_indicator_move_mode(),
            report_as="Move listening indicator")
        tools_sub.addSeparator()
        add(tools_sub, "Interactive Tutorial", lambda: app.show_tutorial())
        add(tools_sub, "Mic Setup Guide", lambda: self._open_guide("mic_setup_wizard", "Mic Setup Guide"))
        add(tools_sub, "Ava Guide", lambda: self._open_guide("ava_guide", "Ava Guide"))
        add(tools_sub, "Voice Training", lambda: app.open_voice_training())
        add(tools_sub, "Benchmark Review", lambda: app.open_benchmark_review())
        add(tools_sub, "Correct Last Dictation", lambda: app.open_correction_capture())
        add(tools_sub, "Stress Test Wizard", lambda: app.open_stress_test_wizard())
        tools_sub.addSeparator()
        add(tools_sub, "Recalibrate Mic", lambda: app.recalibrate_mic())
        if self._update_entry_enabled() and self._available_update is None:
            add(tools_sub, update_check_menu_label(), self._open_update_dialog,
                report_as="Updates")
        tools_sub.addSeparator()

        cleanup_sub = QMenu("Cleanup")
        cleanup_grp = QActionGroup(cleanup_sub)
        cleanup_grp.setExclusive(True)
        cleanup_mode = app.config.get('cleanup_mode', 'clean')
        for label, val in [
            ("Clean  (remove fillers)", "clean"),
            ("Verbatim  (no cleanup)",  "verbatim"),
        ]:
            act = add(cleanup_sub, label,
                      lambda checked, v=val: app.set_cleanup_mode(v) if checked else None,
                      checked=cleanup_mode == val, report_as="Cleanup")
            cleanup_grp.addAction(act)
        tools_sub.addMenu(cleanup_sub)

        menu.addMenu(tools_sub)

        # ---- Developer submenu: debug/diagnostic surfaces ----
        dev_sub = QMenu("Developer")
        add(dev_sub, "Dictation Diagnostics", lambda: app.open_dictation_diagnostics())
        add(dev_sub, "Wake Word Debug", lambda: app.open_wake_word_debug())
        add(dev_sub, "View Live Log", lambda: app.open_log_viewer())
        dev_sub.addSeparator()
        add(dev_sub, "Calibrate Echo Cancellation", lambda: app.calibrate_echo_cancellation())
        dev_sub.addSeparator()
        add(dev_sub, "Open Config Folder", lambda: app.open_config_folder())
        dev_sub.addSeparator()
        add(dev_sub, "Preview First-Run (fresh profile)", lambda: app.preview_first_run())
        logs_sub = QMenu("View Logs")
        add(logs_sub, "Main Log", lambda: app.open_main_log())
        add(logs_sub, "Voice Training Log", lambda: app.open_voice_training_log())
        dev_sub.addMenu(logs_sub)

        menu.addMenu(dev_sub)
        menu.addSeparator()

        add(menu, "Exit", lambda: app.quit_app())
