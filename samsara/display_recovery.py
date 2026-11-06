"""Voice recovery actions for the selected listening display."""
from __future__ import annotations

from math import isfinite
from samsara.config_defaults import cfg_get


def _unit(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value)) if isfinite(value) else default


def _current_screen():
    from PySide6.QtGui import QCursor, QGuiApplication
    return QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()


def _compact_anchor(rect, area, custom):
    """Return the last normalized point, clamped to a safe default if lost."""
    if rect.width() > 0 and rect.height() > 0 and area.contains(rect.center()):
        return (min(1.0, max(0.0, (rect.center().x() - area.x()) / max(1, area.width()))),
                min(1.0, max(0.0, (rect.center().y() - area.y()) / max(1, area.height()))))
    if isinstance(custom, dict):
        return _unit(custom.get("cx"), .04), _unit(custom.get("cy"), .96)
    return .04, .96


def location(app, *, full: bool) -> str:
    """Describe the persisted corner of the display after FIND."""
    if full:
        controller = getattr(app, "live_surface", None)
        raw, records = controller._placement_config() if controller else ({}, {})
        record = records.get(raw.get("preferred_monitor"), {})
        side = str(record.get("edge", "bottom"))
        if side == "free":
            x, y = _unit(record.get("free_cx"), .04), _unit(record.get("free_cy"), .96)
            side, t = ("right" if x >= .5 else "left"), y
        else:
            t = _unit(record.get("t"), 0.0)
    else:
        config = getattr(app, "config", {})
        custom = config.get("listening_indicator_custom_position") or {}
        if config.get("listening_indicator_position") == "custom" and isinstance(custom, dict):
            side = "bottom" if _unit(custom.get("cy"), .96) >= .5 else "top"
            t = _unit(custom.get("cx"), .04)
        else:
            position = str(cfg_get(config, "listening_indicator_position"))
            side, _, horizontal = position.partition("-")
            t = {"left": 0.0, "center": .5, "right": 1.0}.get(horizontal, 0.0)
    vertical = "top" if side == "top" else "bottom"
    if side in ("left", "right"):
        horizontal = "left" if side == "left" else "right"
        vertical = "top" if t < .5 else "bottom"
    else:
        horizontal = "left" if t <= .25 else "right" if t >= .75 else "center"
    return f"{vertical} {horizontal} of your current screen"


def _show(app):
    from samsara import listening_display
    if not listening_display.display_visible(app):
        app._listening_display_visible = True
        app.apply_listening_indicator_settings()


def reset(app) -> bool:
    from samsara import listening_display
    if listening_display.run_display(app).full:
        controller = getattr(app, "live_surface", None)
        return bool(controller and controller.reset_placement())
    from samsara.ui.listening_indicator import reset_indicator_placement
    return reset_indicator_placement(app)


def find(app) -> bool:
    """Show the selected display, recover it to the pointer's monitor, then flash."""
    from samsara import listening_display
    _show(app)
    if listening_display.run_display(app).full:
        controller = getattr(app, "live_surface", None)
        if controller is None or not controller.find_on_current_monitor():
            return False
        widget = controller.widget
        if widget is not None:
            widget.flash_find()
        return True

    screen = _current_screen()
    indicator = getattr(app, "listening_indicator", None)
    if screen is None or indicator is None:
        return False
    area = screen.availableGeometry()
    rect = indicator.frameGeometry()
    if rect.width() <= 0 or rect.height() <= 0:
        return False
    custom = (getattr(app, "config", {}).get("listening_indicator_custom_position") or {})
    # Preserve a reachable live position; when the saved location was lost,
    # use the requested default corner on the monitor under the pointer.
    cx, cy = _compact_anchor(rect, area, custom)
    position = {"screen": screen.name(), "cx": cx, "cy": cy}
    app.update_config_and_save({"listening_indicator_position": "custom",
                                "listening_indicator_custom_position": position})
    app.apply_listening_indicator_settings()
    indicator.flash_find()
    return True
