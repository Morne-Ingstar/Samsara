"""Compact docking geometry and placement adapter; no window ownership.

All coordinates are work-area DIPs. Integer centering deliberately matches
Compact's pill layout, including odd label/chip widths.
"""
from PySide6.QtWidgets import QApplication
from samsara.live_surface.placement import (Edge, Placement, Rect, Screen,
    PlacedForm, placement_for_drop, place_inline_form, SNAP_DISTANCE_DIP)

_PILL_H = 36
_CHIP_H = 26
_CHIP_GAP = 6
_EDGE_MARGIN = 8


def reachable_position(x, y, width, height, area):
    """Recover any drop, including arbitrarily distant off-screen coordinates."""
    inset = min(_EDGE_MARGIN, max((area.width - width) // 2, 0),
                max((area.height - height) // 2, 0))
    return (min(max(x, area.x + inset), area.right - width - inset),
            min(max(y, area.y + inset), area.bottom - height - inset))


def snap_preview(x, y, width, height, monitor):
    """Snap visible edges and clamp both preview and release to reachable bounds."""
    area = monitor.work_area
    bounds = Rect(x, y, width, height)
    dock = placement_for_drop(bounds, (x + 22, y + 22), monitor)
    if dock.edge in (Edge.TOP, Edge.BOTTOM) and 0 < dock.t < 1:
        if abs(x - (area.x + (area.width - width) / 2)) <= SNAP_DISTANCE_DIP:
            dock = Placement(dock.edge, .5)
    if dock.edge is Edge.FREE:
        px, py = reachable_position(x, y, width, height, area)
        bounds = Rect(px, py, width, height)
        return dock, PlacedForm(monitor.id, dock.edge, Rect(px, py, 44, 44), bounds)
    placed = place_inline_form(dock, monitor, width=width, height=height, mark_offset=(0, 0))
    px, py = reachable_position(placed.bounds.x, placed.bounds.y, width, height, area)
    bounds = Rect(px, py, width, height)
    return dock, PlacedForm(monitor.id, dock.edge, Rect(px, py, 44, 44), bounds)


class IndicatorDocking:
    """Placement methods for the existing indicator; never creates a widget."""
    def _commit_drag_position(self):
        """Save a preset dock or an exactly reproducible custom drop."""
        screen = QApplication.screenAt(self.mapToGlobal(self.rect().center()))
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            self.exit_move_mode(cancel=True)
            return
        from samsara.live_surface.placement import Edge
        geom = screen.availableGeometry()
        dock, placed = self._snap_drag(self.x(), self.y(), screen)
        x, y = round(placed.bounds.x), round(placed.bounds.y)
        # Compute the ordinary layout at the NEW drop point. A chip may grow
        # below here even though it grew above at the previous bottom dock.
        self._custom_position = {'screen': screen.name(),
            'cx': (x + self.width()/2) / max(1, geom.width()) - geom.x()/max(1, geom.width()),
            'cy': (y + _PILL_H/2 - geom.y()) / max(1, geom.height())}
        self._corner = None
        if dock.edge is not Edge.FREE:
            self._custom_position.update(edge=dock.edge.value, t=dock.t)
        # Exit changes the temporary drag label. Save the ordinary pill centre,
        # rather than the centre of that temporary, wider label.
        self._unlocked = False
        self._resize_to_label()
        _, _, pill, _ = self._layout()
        if pill is None:
            px, py = self.width() / 2, self.height() / 2
        else:
            px, py = pill.center().x(), pill.center().y()
        cx = min(1., max(0., (x + px - geom.x()) / max(1, geom.width())))
        cy = min(1., max(0., (y + py - geom.y()) / max(1, geom.height())))
        preset = None
        if dock.edge in (Edge.TOP, Edge.BOTTOM) and screen is QApplication.primaryScreen():
            suffix = {0.: 'left', .5: 'center', 1.: 'right'}.get(dock.t)
            if suffix: preset = dock.edge.value + '-' + suffix
        self._corner = preset
        self._custom_position = None if preset else {'screen': screen.name(), 'cx': cx, 'cy': cy}
        if not preset and dock.edge is not Edge.FREE:
            self._custom_position.update(edge=dock.edge.value, t=dock.t)
        self._apply_static_position()  # Also place a hidden indicator before locking.
        payload = ({'type': 'preset', 'position': preset} if preset else
                   {'type': 'custom', **self._custom_position})
        self._unlocked = True
        self.exit_move_mode()
        self.placement_committed.emit(payload)


    def _snap_drag(self, x, y, screen):
        geom = screen.availableGeometry()
        monitor = Screen(screen.name(), Rect(geom.x(), geom.y(), geom.width(), geom.height()))
        return snap_preview(x, y, self.width(), self.height(), monitor)

    def _chip_h_align(self) -> str:
        """'left' / 'right' / 'center' -- which edge the pill hugs."""
        if self._custom_position is not None:
            edge = self._custom_position.get('edge')
            if edge in ('left', 'right'):
                return edge
            if edge in ('top', 'bottom'):
                t = self._custom_position.get('t', .5)
                if t == 0: return 'left'
                if t == 1: return 'right'
            cx = self._custom_position.get('cx', 0.5)
            if cx < 1 / 3:
                return "left"
            if cx > 2 / 3:
                return "right"
            return "center"
        corner = str(self._corner or "")
        if corner.endswith("left"):
            return "left"
        if corner.endswith("right"):
            return "right"
        return "center"


    def _chip_goes_above(self) -> bool:
        """Bottom grows upward; side/free grows down while there is room."""
        if self._custom_position is not None:
            custom = self._custom_position
            edge = custom.get('edge')
            if edge in ('top', 'bottom'):
                return edge == 'bottom'
            screen = self._resolve_custom_screen()
            if screen is not None:
                geom = screen.availableGeometry()
                if edge in ('left', 'right'):
                    y = geom.y() + 8 + max(0, geom.height() - 44 - 16) * custom['t']
                else:
                    y = geom.y() + custom['cy'] * geom.height() - _PILL_H / 2
                return y + _PILL_H + _CHIP_GAP + _CHIP_H > geom.y() + geom.height() - 8
            return custom.get('cy', 1.0) >= .5
        return not str(self._corner or "").startswith("top")


    def _pill_anchor(self, pill_w: int):
        """Where the PILL's top-left goes, exactly as before the chip existed.
        Returns (x, y, geom) or None when there is no screen."""
        pill_h = _PILL_H
        if self._custom_position is not None:
            screen = self._resolve_custom_screen()
            if screen is not None:
                geom = screen.availableGeometry()
                cx = geom.x() + self._custom_position['cx'] * geom.width()
                cy = geom.y() + self._custom_position['cy'] * geom.height()
                x = int(round(cx - pill_w / 2))
                y = int(round(cy - pill_h / 2))
                x, y = self._reachable_position(x, y, pill_w, pill_h, geom)
                return x, y, geom

        screen = QApplication.primaryScreen()
        if screen is None:
            return None
        geom = screen.availableGeometry()
        wa_x, wa_y = geom.x(), geom.y()
        wa_w, wa_h = geom.width(), geom.height()
        m = _EDGE_MARGIN
        cx = wa_x + (wa_w - pill_w) // 2

        corner = self._corner
        if corner == "top-left":
            x, y = wa_x + m, wa_y + m
        elif corner == "top-center":
            x, y = cx, wa_y + m
        elif corner == "top-right":
            x, y = wa_x + wa_w - pill_w - m, wa_y + m
        elif corner == "bottom-left":
            x, y = wa_x + m, wa_y + wa_h - pill_h - m
        elif corner == "bottom-right":
            x, y = wa_x + wa_w - pill_w - m, wa_y + wa_h - pill_h - m
        else:  # bottom-center default
            x, y = cx, wa_y + wa_h - pill_h - m
        return x, y, geom


    def _apply_static_position(self):
        """Position per self._custom_position (if set) or the preset
        self._corner. Called only when not unlocked.

        The pill's on-screen position is computed exactly as it always was;
        the widget is then offset so the pill stays put and any chip grows
        away from the anchored edge. In chip-only mode the chip takes the
        pill's place, so it follows the same placement and move-mode logic.
        """
        from samsara.live_surface.placement import Edge, Placement, Rect, Screen, place_inline_form
        custom = self._custom_position
        if custom is not None and custom.get('edge') in ('top', 'bottom', 'left', 'right'):
            screen = self._resolve_custom_screen()
            dock = Placement(Edge(custom['edge']), custom['t'])
        elif custom is None:
            screen = QApplication.primaryScreen()
            parts = str(self._corner or 'bottom-center').split('-')
            dock = Placement(Edge(parts[0]), {'left': 0., 'center': .5, 'right': 1.}[parts[1]])
        else:
            screen = None
        if screen is not None:
            g = screen.availableGeometry()
            monitor = Screen(screen.name(), Rect(g.x(), g.y(), g.width(), g.height()))
            _, _, pill, _ = self._layout()
            oy = pill.y() if pill is not None and dock.edge in (Edge.LEFT, Edge.RIGHT) else 0
            placed = place_inline_form(dock, monitor, width=self.width(), height=self.height(), mark_offset=(0, oy))
            x = round(placed.bounds.x)
            if dock.edge in (Edge.TOP, Edge.BOTTOM) and dock.t == .5:
                # Preserve the pill's integer centre when wider chips appear.
                if pill is None:
                    x = g.x() + (g.width() - self.width()) // 2
                else:
                    x = g.x() + (g.width() - int(pill.width())) // 2 - int(pill.x())
            self.move(x, round(placed.bounds.y))
            return
        _, _, label, show_dot = self._resolve_colors()
        pill_w = self._pill_width(label, show_dot)
        anchor = self._pill_anchor(pill_w)
        if anchor is None:
            return
        pill_x, pill_y, geom = anchor
        total_w, total_h, pill_rect, chip_rect = self._layout()

        if pill_rect is None:  # chip-only: centre the chip on the pill's spot
            x = int(round(pill_x + (pill_w - total_w) / 2.0))
            y = int(round(pill_y + (_PILL_H - total_h) / 2.0))
        else:
            x = int(round(pill_x - pill_rect.x()))
            y = int(round(pill_y - pill_rect.y()))
        x, y = self._reachable_position(x, y, int(total_w), int(total_h), geom)
        self.move(x, y)


    @staticmethod
    def _reachable_position(x, y, width, height, geom):
        return reachable_position(x, y, width, height,
            Rect(geom.x(), geom.y(), geom.width(), geom.height()))
