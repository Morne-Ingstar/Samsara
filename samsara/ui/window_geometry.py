"""Screen-independent rules for restoring and resetting the Samsara hub.

Rectangles are (x, y, width, height) in Qt logical pixels. Margins are
(left, top, right, bottom), including the native title bar and frame.
No screen, settings, or Qt dependencies live here.
"""

Rect = tuple[int, int, int, int]
Margins = tuple[int, int, int, int]


def outer_rect(rect: Rect, margins: Margins = (0, 0, 0, 0)) -> Rect:
    x, y, w, h = rect
    left, top, right, bottom = margins
    return x - left, y - top, w + left + right, h + top + bottom


def contains(screen: Rect, rect: Rect) -> bool:
    sx, sy, sw, sh = screen
    x, y, w, h = rect
    return (w > 0 and h > 0 and sx <= x and sy <= y
            and x + w <= sx + sw and y + h <= sy + sh)


def default_geometry(screen: Rect, default_size: tuple[int, int],
                     margins: Margins = (0, 0, 0, 0)) -> Rect:
    """Centre the default client size, capped so its whole frame fits."""
    sx, sy, sw, sh = screen
    left, top, right, bottom = margins
    w = max(1, min(default_size[0], sw - left - right))
    h = max(1, min(default_size[1], sh - top - bottom))
    return (sx + (sw - w - left - right) // 2 + left,
            sy + (sh - h - top - bottom) // 2 + top, w, h)


def restore_geometry(stored: Rect | None, screens: list[Rect], primary: Rect,
                     default_size: tuple[int, int],
                     margins: Margins = (0, 0, 0, 0)) -> Rect:
    """Keep a saved window only when its entire frame fits on one screen.

    This also rejects a reachable title bar whose body is behind a taskbar.
    A changed monitor layout/scale needs no special case: current available
    rectangles are the authority. Invalid or absent geometry uses defaults.
    """
    if stored is not None and any(contains(s, outer_rect(stored, margins)) for s in screens):
        return stored
    return default_geometry(primary, default_size, margins)


def reset_geometry(current_frame: Rect, screens: list[Rect], primary: Rect,
                   default_size: tuple[int, int],
                   margins: Margins = (0, 0, 0, 0)) -> Rect:
    """Reset on the screen with most window area; primary wins ties/no overlap."""
    x, y, w, h = current_frame

    def overlap(screen):
        sx, sy, sw, sh = screen
        return (max(0, min(x + w, sx + sw) - max(x, sx))
                * max(0, min(y + h, sy + sh) - max(y, sy)))

    screen = max([primary, *screens], key=overlap)
    return default_geometry(screen, default_size, margins)
