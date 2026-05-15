"""
Win32 per-pixel-alpha layered window overlay.

Uses UpdateLayeredWindow (ULW_ALPHA) + PIL for rendering — proper Windows
transparency that keeps labels fully opaque while making every other pixel
invisible.  No Tkinter, no chroma-key, no -transparentcolor fights.

The window carries WS_EX_TRANSPARENT so physical mouse events pass straight
through to whatever app is below.  WS_EX_NOACTIVATE prevents focus theft.

Public API:
    overlay = LayeredOverlay()          # create once on main thread
    overlay.show(labels)                # list of (x, y, w, h, text)
    overlay.hide()                      # make invisible, keep HWND alive
    overlay.destroy()                   # full teardown on app quit

Threading:
    Construction (RegisterClass + CreateWindowEx) should happen on the main
    thread.  show() and hide() may be called from worker threads; for a
    WS_POPUP layered window with no message pump UpdateLayeredWindow and
    ShowWindow work correctly cross-thread because neither operation depends
    on the owning thread's message queue.
"""

import ctypes
from ctypes import wintypes
import logging
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Win32 constants
# ---------------------------------------------------------------------------

WS_POPUP           = 0x80000000
WS_EX_LAYERED      = 0x00080000
WS_EX_TRANSPARENT  = 0x00000020
WS_EX_TOOLWINDOW   = 0x00000080
WS_EX_TOPMOST      = 0x00000008
WS_EX_NOACTIVATE   = 0x08000000

ULW_ALPHA     = 0x00000002
AC_SRC_OVER   = 0x00
AC_SRC_ALPHA  = 0x01

SW_HIDE           = 0
SW_SHOWNOACTIVATE = 4

SM_XVIRTUALSCREEN  = 76
SM_YVIRTUALSCREEN  = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

DIB_RGB_COLORS = 0
BI_RGB         = 0

# ---------------------------------------------------------------------------
# Module handles
# ---------------------------------------------------------------------------

user32   = ctypes.windll.user32
gdi32    = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# Win32 structs
# ---------------------------------------------------------------------------

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize',        wintypes.UINT),
        ('style',         wintypes.UINT),
        ('lpfnWndProc',   WNDPROCTYPE),
        ('cbClsExtra',    ctypes.c_int),
        ('cbWndExtra',    ctypes.c_int),
        ('hInstance',     wintypes.HINSTANCE),
        ('hIcon',         wintypes.HICON),
        ('hCursor',       wintypes.HANDLE),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName',  wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm',       wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [('x', ctypes.c_long), ('y', ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [('cx', ctypes.c_long), ('cy', ctypes.c_long)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ('BlendOp',             ctypes.c_byte),
        ('BlendFlags',          ctypes.c_byte),
        ('SourceConstantAlpha', ctypes.c_byte),
        ('AlphaFormat',         ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize',          wintypes.DWORD),
        ('biWidth',         ctypes.c_long),
        ('biHeight',        ctypes.c_long),
        ('biPlanes',        wintypes.WORD),
        ('biBitCount',      wintypes.WORD),
        ('biCompression',   wintypes.DWORD),
        ('biSizeImage',     wintypes.DWORD),
        ('biXPelsPerMeter', ctypes.c_long),
        ('biYPelsPerMeter', ctypes.c_long),
        ('biClrUsed',       wintypes.DWORD),
        ('biClrImportant',  wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER)]


# ---------------------------------------------------------------------------
# Function signatures
# ---------------------------------------------------------------------------

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,    # dwExStyle
    wintypes.LPCWSTR,  # lpClassName
    wintypes.LPCWSTR,  # lpWindowName
    wintypes.DWORD,    # dwStyle
    ctypes.c_int,      # X
    ctypes.c_int,      # Y
    ctypes.c_int,      # nWidth
    ctypes.c_int,      # nHeight
    wintypes.HWND,     # hWndParent
    wintypes.HMENU,    # hMenu
    wintypes.HINSTANCE, # hInstance
    wintypes.LPVOID,   # lpParam
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND,                   # hWnd
    wintypes.HDC,                    # hdcDst
    ctypes.POINTER(POINT),           # pptDst
    ctypes.POINTER(SIZE),            # psize
    wintypes.HDC,                    # hdcSrc
    ctypes.POINTER(POINT),           # pptSrc
    wintypes.COLORREF,               # crKey
    ctypes.POINTER(BLENDFUNCTION),   # pblend
    wintypes.DWORD,                  # dwFlags
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype  = wintypes.HDC

gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC,
    ctypes.c_void_p,
    wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p),
    wintypes.HANDLE,
    wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP

gdi32.SelectObject.argtypes  = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype   = wintypes.HGDIOBJ
gdi32.DeleteDC.argtypes      = [wintypes.HDC]
gdi32.DeleteDC.restype       = wintypes.BOOL
gdi32.DeleteObject.argtypes  = [wintypes.HGDIOBJ]
gdi32.DeleteObject.restype   = wintypes.BOOL

user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype  = wintypes.ATOM
user32.DefWindowProcW.argtypes   = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype    = ctypes.c_long
user32.ShowWindow.argtypes       = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype        = wintypes.BOOL
user32.DestroyWindow.argtypes    = [wintypes.HWND]
user32.DestroyWindow.restype     = wintypes.BOOL
user32.GetDC.argtypes            = [wintypes.HWND]
user32.GetDC.restype             = wintypes.HDC
user32.ReleaseDC.argtypes        = [wintypes.HWND, wintypes.HDC]
user32.ReleaseDC.restype         = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype  = ctypes.c_int

# ---------------------------------------------------------------------------
# Class registration (once per process)
# ---------------------------------------------------------------------------

_CLASS_NAME = 'SamsaraLayeredOverlay'
_class_registered = False
_wnd_proc_ref = None   # must keep a reference to prevent GC of the callback


def _ensure_class_registered():
    global _class_registered, _wnd_proc_ref

    if _class_registered:
        return

    def _wnd_proc(hwnd, msg, wparam, lparam):
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    _wnd_proc_ref = WNDPROCTYPE(_wnd_proc)

    wc = WNDCLASSEXW()
    wc.cbSize        = ctypes.sizeof(WNDCLASSEXW)
    wc.style         = 0
    wc.lpfnWndProc   = _wnd_proc_ref
    wc.cbClsExtra    = 0
    wc.cbWndExtra    = 0
    wc.hInstance     = kernel32.GetModuleHandleW(None)
    wc.hIcon         = 0
    wc.hCursor       = 0
    wc.hbrBackground = 0   # no background brush — ULW provides all pixels
    wc.lpszMenuName  = None
    wc.lpszClassName = _CLASS_NAME
    wc.hIconSm       = 0

    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        err = ctypes.GetLastError()
        if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS — safe to ignore
            raise OSError(f'RegisterClassExW failed with error {err}')

    _class_registered = True


# ---------------------------------------------------------------------------
# Font loading
# ---------------------------------------------------------------------------

_FONT_PATHS = [
    r'C:\Windows\Fonts\segoeuib.ttf',
    r'C:\Windows\Fonts\segoeui.ttf',
    r'C:\Windows\Fonts\arial.ttf',
]
_cached_font = None


def _get_font(size: int = 12):
    global _cached_font
    if _cached_font is None:
        for path in _FONT_PATHS:
            try:
                _cached_font = ImageFont.truetype(path, size)
                return _cached_font
            except Exception:
                pass
        _cached_font = ImageFont.load_default()
    return _cached_font


# ---------------------------------------------------------------------------
# PIL → Win32 DIB rendering
# ---------------------------------------------------------------------------

def _push_image_to_window(hwnd: int, img: Image.Image, virt_x: int, virt_y: int) -> bool:
    """Render a PIL RGBA image to the layered window via UpdateLayeredWindow.

    Returns True on success.  Logs on failure and returns False.
    """
    w, h = img.size

    # PIL is RGBA; Win32 DIB is BGRA — swap R and B channels
    r, g, b, a = img.split()
    bgra = Image.merge('RGBA', (b, g, r, a))
    pixel_data = bgra.tobytes()

    # Build BITMAPINFO (negative biHeight = top-down scanlines)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth       = w
    bmi.bmiHeader.biHeight      = -h  # top-down
    bmi.bmiHeader.biPlanes      = 1
    bmi.bmiHeader.biBitCount    = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bmi.bmiHeader.biSizeImage   = w * h * 4

    hdc_screen = user32.GetDC(None)
    if not hdc_screen:
        logger.error('[OVERLAY] GetDC(NULL) failed')
        return False

    ppv_bits = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(
        hdc_screen,
        ctypes.byref(bmi),
        DIB_RGB_COLORS,
        ctypes.byref(ppv_bits),
        None,
        0,
    )
    if not hbmp:
        user32.ReleaseDC(None, hdc_screen)
        logger.error('[OVERLAY] CreateDIBSection failed: %d', ctypes.GetLastError())
        return False

    # Copy pixel data to DIB
    ctypes.memmove(ppv_bits, pixel_data, len(pixel_data))

    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    h_old   = gdi32.SelectObject(hdc_mem, hbmp)

    blend = BLENDFUNCTION()
    blend.BlendOp             = AC_SRC_OVER
    blend.BlendFlags          = 0
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat         = AC_SRC_ALPHA

    pt_dst = POINT(virt_x, virt_y)
    pt_src = POINT(0, 0)
    sz     = SIZE(w, h)

    ok = user32.UpdateLayeredWindow(
        hwnd,
        hdc_screen,
        ctypes.byref(pt_dst),
        ctypes.byref(sz),
        hdc_mem,
        ctypes.byref(pt_src),
        0,
        ctypes.byref(blend),
        ULW_ALPHA,
    )

    # Cleanup GDI resources
    gdi32.SelectObject(hdc_mem, h_old)
    gdi32.DeleteDC(hdc_mem)
    gdi32.DeleteObject(hbmp)
    user32.ReleaseDC(None, hdc_screen)

    if not ok:
        logger.error('[OVERLAY] UpdateLayeredWindow failed: %d', ctypes.GetLastError())
        return False
    return True


# ---------------------------------------------------------------------------
# Label drawing
# ---------------------------------------------------------------------------

_LABEL_W  = 28
_LABEL_H  = 22
_FILL     = (255, 149, 0, 255)    # #ff9500 fully opaque orange
_OUTLINE  = (26, 26, 26, 255)     # #1a1a1a
_TEXT_CLR = (255, 255, 255, 255)  # white


def _draw_labels(
    img: Image.Image,
    labels: "List[Tuple[int, int, int, int, str]]",
    virt_x: int,
    virt_y: int,
) -> None:
    """Draw numbered badges onto img.

    labels: list of (screen_x, screen_y, w, h, text) where screen_x/y are
    absolute screen coordinates.  They're mapped to image coordinates by
    subtracting the virtual screen origin.
    """
    draw = ImageDraw.Draw(img)
    font = _get_font(12)

    for sx, sy, lw, lh, text in labels:
        ix = sx - virt_x   # image-relative x
        iy = sy - virt_y   # image-relative y
        x0, y0 = ix, iy
        x1, y1 = ix + lw, iy + lh

        # Clip to image bounds
        if x1 <= 0 or y1 <= 0 or x0 >= img.width or y0 >= img.height:
            continue

        # Filled rounded rect
        draw.rounded_rectangle([x0, y0, x1, y1], radius=3,
                                fill=_FILL, outline=_OUTLINE, width=1)

        # Centered text
        try:
            bbox = font.getbbox(text)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except Exception:
            tw, th = 8 * len(text), 10
        tx = x0 + (lw - tw) // 2
        ty = y0 + (lh - th) // 2
        draw.text((tx, ty), text, fill=_TEXT_CLR, font=font)


# ---------------------------------------------------------------------------
# LayeredOverlay class
# ---------------------------------------------------------------------------

class LayeredOverlay:
    """Fullscreen per-pixel-alpha layered window for the Show Numbers overlay."""

    def __init__(self):
        _ensure_class_registered()

        self._virt_x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        self._virt_y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        self._virt_w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        self._virt_h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        self._visible = False

        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT
                    | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE)

        self._hwnd = user32.CreateWindowExW(
            ex_style,
            _CLASS_NAME,
            '',
            WS_POPUP,
            self._virt_x, self._virt_y,
            self._virt_w, self._virt_h,
            0, 0,
            kernel32.GetModuleHandleW(None),
            None,
        )
        if not self._hwnd:
            raise OSError(
                f'CreateWindowExW failed with error {ctypes.GetLastError()}'
            )

        # Prime the window with a fully-transparent frame so ULW is happy
        # before the first real show()
        self._push_blank()

    def show(self, labels: "List[Tuple[int, int, int, int, str]]") -> None:
        """Render labels and make the window visible."""
        img = Image.new('RGBA', (self._virt_w, self._virt_h), (0, 0, 0, 0))
        _draw_labels(img, labels, self._virt_x, self._virt_y)

        ok = _push_image_to_window(self._hwnd, img, self._virt_x, self._virt_y)
        if not ok:
            logger.error('[OVERLAY] UpdateLayeredWindow failed in show()')
            return

        if not self._visible:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._visible = True

    def hide(self) -> None:
        """Make the window invisible (keep HWND for reuse)."""
        if self._visible:
            user32.ShowWindow(self._hwnd, SW_HIDE)
            self._visible = False

    def is_visible(self) -> bool:
        return self._visible

    def destroy(self) -> None:
        """Full teardown — call on app quit."""
        if self._hwnd:
            user32.DestroyWindow(self._hwnd)
            self._hwnd = 0
            self._visible = False

    def _push_blank(self) -> None:
        """Prime the layered window with a fully-transparent frame."""
        img = Image.new('RGBA', (self._virt_w, self._virt_h), (0, 0, 0, 0))
        _push_image_to_window(self._hwnd, img, self._virt_x, self._virt_y)
