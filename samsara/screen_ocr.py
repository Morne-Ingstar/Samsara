"""On-device OCR of the foreground window, for "click <text>" (queue 70).

Why: queue 56 measured what UI Automation returns in the owner's apps -- 4
clickable elements in Claude desktop, 1 in Warp -- and Windows Voice Access
finds nothing in Obsidian or Warp either. Those apps expose nothing to any
accessibility client, but their text is on screen. This module reads it with
the Windows OCR engine (Windows.Media.Ocr via winsdk): it ships with Windows
and runs on the device -- no image or text leaves the machine.

Coordinates. The running app is NOT per-monitor-v2 DPI aware: dictation.py
imports pyautogui at module load, which calls SetProcessDPIAware() before Qt
starts, so the process is system-aware and Qt's own PMv2 request fails
("SetProcessDpiAwarenessContext() failed: Access is denied", reports/70).
Every Win32 geometry query, the screen capture and the cursor placement here
therefore run inside an explicit PMv2 thread context
(numbers_overlay_qt._with_physical_dpi_context), so everything this module
returns and clicks is in PHYSICAL screen pixels regardless of the process's
awareness. Display of those rectangles goes through queue 56's overlay
mapping (plan_overlay / _map_physical_to_qt), which already takes physical
input.

Pipeline (read_foreground): foreground window -> its visible frame
(DWMWA_EXTENDED_FRAME_BOUNDS) intersected with the monitor holding it (main
window, one monitor) -> GDI BitBlt of that rectangle from the screen, i.e.
what the user actually sees -> OCR (optionally upscaled) -> words and lines
with physical screen rectangles.

Matching (find_matches) reuses the intent grammar's token view and Whisper
confusion folding (samsara.intent.normalize.tokens / collapse_confusions), so
a homophone of an on-screen word ("right" for "Write", "for" for "4")
compares equal. Exact folded matches are the only ones treated as certain;
anything fuzzy is returned flagged so the caller disambiguates instead of
clicking.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.wintypes as wt
import difflib
import sys
import time
from dataclasses import dataclass, field

from samsara.log import get_logger

logger = get_logger(__name__)

#: Upscale factor applied before OCR. 1: measured on the owner's Obsidian,
#: Warp, Brave and Claude windows (reports/70), 2x cost 2-3x the time (149-352
#: ms against 40-124 ms) and read no more words -- the two word sets differed
#: by 10-22% in BOTH directions, which is OCR noise, not resolution.
OCR_SCALE = 1
#: A fuzzy candidate needs at least this similarity to be offered at all.
FUZZY_MIN_RATIO = 0.8
#: Queries this short (after folding) must match exactly -- fuzzy matching a
#: two-letter word finds noise.
FUZZY_MIN_CHARS = 4
#: Never offer more numbered candidates than this.
MAX_CANDIDATES = 20


@dataclass(frozen=True)
class Word:
    text: str
    rect: tuple            # physical screen pixels (left, top, right, bottom)


@dataclass(frozen=True)
class Line:
    words: tuple

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Snapshot:
    hwnd: int
    window_rect: tuple     # physical visible frame of the target window
    capture_rect: tuple    # physical rectangle actually captured and read
    lines: tuple
    scale: int
    timings_ms: dict = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return sum(len(line.words) for line in self.lines)


@dataclass(frozen=True)
class Match:
    text: str              # the on-screen words matched
    rect: tuple            # physical union of those words
    score: float           # 1.0 exact (after folding), else similarity
    exact: bool

    @property
    def center(self) -> tuple:
        return ((self.rect[0] + self.rect[2]) // 2, (self.rect[1] + self.rect[3]) // 2)


# ---------------------------------------------------------------------------
# Physical-pixel Win32 helpers
# ---------------------------------------------------------------------------

def _physical(fn):
    from samsara.ui.numbers_overlay_qt import _with_physical_dpi_context  # noqa: PLC0415
    return _with_physical_dpi_context(fn)


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", ctypes.c_ulong)]


def foreground_hwnd() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow() or 0) if sys.platform == "win32" else 0


def window_frame_rect(hwnd: int) -> "tuple | None":
    """The window's visible frame in physical pixels: DWM extended frame
    bounds (no invisible resize border), else GetWindowRect."""
    def _query():
        rect = wt.RECT()
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        try:
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wt.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
            if hr == 0 and rect.right > rect.left:
                return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception as e:
            logger.debug(f"window_frame_rect dwm: {e}")
        if ctypes.windll.user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(rect)):
            return (rect.left, rect.top, rect.right, rect.bottom)
        return None
    return _physical(_query)


def monitor_rect_for_window(hwnd: int) -> "tuple | None":
    def _query():
        hmon = ctypes.windll.user32.MonitorFromWindow(wt.HWND(hwnd), 2)  # DEFAULTTONEAREST
        if not hmon:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return (r.left, r.top, r.right, r.bottom)
    return _physical(_query)


def intersect(a: tuple, b: tuple) -> "tuple | None":
    r = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return r if r[2] > r[0] and r[3] > r[1] else None


def point_reaches_window(hwnd: int, x: int, y: int) -> bool:
    """True when a click at physical (x, y) would land in `hwnd` (or one of
    its children): WindowFromPoint skips click-through windows, exactly as a
    real click does, so a topmost window covering the text makes it False."""
    def _query():
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wt.POINT]
        user32.WindowFromPoint.restype = wt.HWND
        user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
        user32.GetAncestor.restype = wt.HWND
        hit = user32.WindowFromPoint(wt.POINT(int(x), int(y)))
        if not hit:
            return False
        root = user32.GetAncestor(hit, 2)  # GA_ROOT
        return int(root or hit) == int(hwnd)
    return bool(_physical(_query))


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


def _gdi_capture(width: int, height: int, blit) -> bytes:
    """Top-down BGRA pixels of a width x height memory bitmap filled by
    blit(hdc_mem). Pure GDI; caller supplies the source."""
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    user32.GetDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    screen_dc = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(ctypes.c_void_p(screen_dc))
    bmp = gdi32.CreateCompatibleBitmap(ctypes.c_void_p(screen_dc), width, height)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        if not blit(mem_dc, screen_dc):
            raise OSError("capture blit failed")
        header = _BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth, header.biHeight = width, -height   # negative: top-down rows
        header.biPlanes, header.biBitCount, header.biCompression = 1, 32, 0
        buf = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(mem_dc, bmp, 0, height, buf, ctypes.byref(header), 0) != height:
            raise OSError("GetDIBits failed")
        return buf.raw
    finally:
        gdi32.SelectObject(mem_dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(None, ctypes.c_void_p(screen_dc))


def capture_screen_rect(rect: tuple) -> bytes:
    """BGRA pixels of a physical screen rectangle, as the user sees it."""
    left, top, right, bottom = rect
    w, h = right - left, bottom - top
    SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000

    def _blit(mem_dc, screen_dc):
        gdi32 = ctypes.windll.gdi32
        gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        return gdi32.BitBlt(mem_dc, 0, 0, w, h, screen_dc, left, top, SRCCOPY | CAPTUREBLT)

    return _physical(lambda: _gdi_capture(w, h, _blit))


def capture_window_content(hwnd: int, rect: tuple) -> bytes:
    """BGRA pixels of one window's own content (PrintWindow, full content),
    whatever covers it. Measurement tooling only: a click goes where the
    SCREEN is, so the click path uses capture_screen_rect."""
    w, h = rect[2] - rect[0], rect[3] - rect[1]

    def _blit(mem_dc, _screen_dc):
        user32 = ctypes.windll.user32
        user32.PrintWindow.argtypes = [wt.HWND, ctypes.c_void_p, ctypes.c_uint]
        return user32.PrintWindow(wt.HWND(hwnd), mem_dc, 2)   # PW_RENDERFULLCONTENT

    return _physical(lambda: _gdi_capture(w, h, _blit))


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

_engine = None


def _ocr_engine():
    global _engine
    if _engine is None:
        from winsdk.windows.media.ocr import OcrEngine  # noqa: PLC0415
        _engine = OcrEngine.try_create_from_user_profile_languages()
        if _engine is None:
            raise RuntimeError("Windows OCR has no recognizer for the user's languages")
    return _engine


def recognize_bgra(bgra: bytes, width: int, height: int, scale: int = OCR_SCALE) -> list:
    """OCR raw top-down BGRA pixels. Returns [[(text, x, y, w, h), ...] per
    line] in the (possibly upscaled) IMAGE's pixel coordinates."""
    from winsdk.windows.graphics.imaging import BitmapAlphaMode, BitmapPixelFormat, SoftwareBitmap  # noqa: PLC0415
    from winsdk.windows.media.ocr import OcrEngine  # noqa: PLC0415
    from winsdk.windows.storage.streams import DataWriter  # noqa: PLC0415

    limit = int(OcrEngine.max_image_dimension)
    scale = max(1, int(scale))
    while scale > 1 and max(width, height) * scale > limit:
        scale -= 1
    if max(width, height) > limit:
        raise ValueError(f"window {width}x{height} exceeds the OCR limit {limit}")
    if scale > 1:
        from PIL import Image  # noqa: PLC0415
        image = Image.frombuffer("RGBA", (width, height), bgra, "raw", "BGRA", 0, 1)
        image = image.resize((width * scale, height * scale), Image.Resampling.BICUBIC)
        bgra = image.tobytes("raw", "BGRA")
        width, height = width * scale, height * scale

    writer = DataWriter()
    writer.write_bytes(bgra)
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, width, height, BitmapAlphaMode.PREMULTIPLIED)

    async def _run():
        return await _ocr_engine().recognize_async(bitmap)

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
    finally:
        loop.close()
    lines = []
    for line in result.lines:
        words = []
        for word in line.words:
            r = word.bounding_rect
            words.append((word.text, r.x, r.y, r.width, r.height))
        if words:
            lines.append(words)
    return lines


def lines_to_screen(raw_lines: list, origin: tuple, scale: int) -> tuple:
    """OCR image coordinates -> physical screen rectangles. Pure.
    origin: physical top-left of the captured rectangle; scale: the upscale
    applied before OCR."""
    ox, oy = origin
    s = float(scale) or 1.0
    out = []
    for raw in raw_lines:
        words = tuple(
            Word(text, (int(round(ox + x / s)), int(round(oy + y / s)),
                        int(round(ox + (x + w) / s)), int(round(oy + (y + h) / s))))
            for text, x, y, w, h in raw)
        out.append(Line(words))
    return tuple(out)


def read_foreground(hwnd: "int | None" = None, scale: int = OCR_SCALE) -> Snapshot:
    """Capture and OCR the foreground window (main window, one monitor)."""
    t0 = time.perf_counter()
    hwnd = hwnd or foreground_hwnd()
    if not hwnd:
        raise RuntimeError("no foreground window")
    frame = window_frame_rect(hwnd)
    monitor = monitor_rect_for_window(hwnd)
    if frame is None or monitor is None:
        raise RuntimeError("could not read the window's position")
    capture = intersect(frame, monitor)
    if capture is None:
        raise RuntimeError("the window is not on a monitor")
    t1 = time.perf_counter()
    pixels = capture_screen_rect(capture)
    t2 = time.perf_counter()
    width, height = capture[2] - capture[0], capture[3] - capture[1]
    raw = recognize_bgra(pixels, width, height, scale)
    t3 = time.perf_counter()
    lines = lines_to_screen(raw, (capture[0], capture[1]), scale)
    snap = Snapshot(hwnd, frame, capture, lines, scale, {
        "geometry": round((t1 - t0) * 1000, 1), "capture": round((t2 - t1) * 1000, 1),
        "ocr": round((t3 - t2) * 1000, 1), "total": round((time.perf_counter() - t0) * 1000, 1)})
    return snap


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def token_view(text: str) -> list:
    """The intent grammar's token view of text (lowercase, punctuation
    stripped per token). Exposed here so plugins reuse it without importing
    samsara.intent themselves (tests/test_intent_grammar.py keeps that
    package out of plugin and dispatch source)."""
    from samsara.intent.normalize import tokens  # noqa: PLC0415
    return tokens(text)


def _folded(text: str) -> list:
    from samsara.intent.normalize import collapse_confusions, tokens  # noqa: PLC0415
    return collapse_confusions(tokens(text))


def _union(words) -> tuple:
    return (min(w.rect[0] for w in words), min(w.rect[1] for w in words),
            max(w.rect[2] for w in words), max(w.rect[3] for w in words))


def find_matches(query: str, lines) -> list:
    """On-screen word runs matching `query`, top-to-bottom, left-to-right.

    Both sides are compared in the intent grammar's folded token view
    (lowercase, punctuation stripped, digits as number words, Whisper
    look-alikes folded). Exact folded matches, if any exist, are the only
    ones returned (score 1.0, exact=True). Otherwise near matches at or above
    FUZZY_MIN_RATIO are returned with exact=False -- never to be clicked
    without the user choosing."""
    q = _folded(query)
    if not q:
        return []
    q_text = " ".join(q)
    exact, fuzzy = [], []
    for line in lines:
        toks, owner = [], []
        for wi, word in enumerate(line.words):
            for tok in _folded(word.text):
                toks.append(tok)
                owner.append(wi)
        n = len(q)
        for i in range(0, len(toks) - n + 1):
            if toks[i:i + n] == q:
                words = line.words[owner[i]:owner[i + n - 1] + 1]
                exact.append(Match(" ".join(w.text for w in words), _union(words), 1.0, True))
        if len(q_text.replace(" ", "")) < FUZZY_MIN_CHARS:
            continue
        best_on_line = {}
        for size in {max(1, n - 1), n, n + 1}:
            for i in range(0, len(toks) - size + 1):
                window = toks[i:i + size]
                cand = " ".join(window)
                ratio = max(difflib.SequenceMatcher(None, q_text, cand).ratio(),
                            difflib.SequenceMatcher(None, q_text.replace(" ", ""), cand.replace(" ", "")).ratio())
                if ratio >= FUZZY_MIN_RATIO and window != q:
                    span = (owner[i], owner[i + size - 1])
                    if ratio > best_on_line.get(span, (0.0,))[0]:
                        best_on_line[span] = (ratio, span)
        for ratio, (a, b) in best_on_line.values():
            words = line.words[a:b + 1]
            fuzzy.append(Match(" ".join(w.text for w in words), _union(words), round(ratio, 3), False))
    chosen = exact if exact else _drop_overlaps(sorted(fuzzy, key=lambda m: -m.score))
    chosen = _dedupe(chosen)
    chosen.sort(key=lambda m: (m.rect[1], m.rect[0]))
    return chosen[:MAX_CANDIDATES]


def _overlaps(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _drop_overlaps(matches: list) -> list:
    kept = []
    for m in matches:
        if not any(_overlaps(m.rect, k.rect) for k in kept):
            kept.append(m)
    return kept


def _dedupe(matches: list) -> list:
    seen, out = set(), []
    for m in matches:
        if m.rect not in seen:
            seen.add(m.rect)
            out.append(m)
    return out
