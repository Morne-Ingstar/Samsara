"""Pure contracts for the single live feedback surface.

This package deliberately has no Qt or application imports.  Package A
freezes the state, geometry, and persisted-config boundary used by later
live-surface packages.
"""

from .model import LiveSurfaceModel, SurfaceEvent, SurfaceView
from .placement import Edge, Placement, Rect, Screen

__all__ = (
    "Edge", "LiveSurfaceModel", "Placement", "Rect", "Screen",
    "SurfaceEvent", "SurfaceView",
)
