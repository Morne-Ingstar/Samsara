"""Context actions for the two listening displays."""
from PySide6.QtGui import QContextMenuEvent
from PySide6.QtWidgets import QMenu

from samsara import listening_display


def open_menu(app, surface, event):
    if app is None or not surface.isVisible():
        return False
    if event.reason() == QContextMenuEvent.Reason.Keyboard:
        point = surface.mapToGlobal(surface.mask().boundingRect().center())
    else:
        point = event.globalPos()
        if not surface.mask().contains(surface.mapFromGlobal(point)):
            return False

    full = listening_display.run_display(app).full
    other = "Compact" if full else "Full"
    menu = QMenu(surface)
    menu.aboutToHide.connect(menu.deleteLater)
    # PLACEHOLDER COPY (1383)
    menu.addAction(f"Switch to {other} in Settings (next launch)", lambda: _open_settings(app))
    menu.addAction("Hide indicator", lambda: _hide(app, surface))
    reset = getattr(getattr(app, "live_surface", None), "reset_placement", None) if full else None
    if not full:
        from samsara.ui.listening_indicator import reset_indicator_placement
        reset = lambda: reset_indicator_placement(app)
    if callable(reset):
        menu.addAction("Reset position", reset)
    menu.popup(point)
    event.accept()
    return True


def _open_settings(app):
    from samsara.ui.home_qt import open_settings_tab
    open_settings_tab(app, "General")


def _hide(app, surface):
    from samsara.ui.paste_again_qt import show_control
    show_control(surface.parentWidget() or surface, app, available=False)
    listening_display.set_display_visibility(app, False)
