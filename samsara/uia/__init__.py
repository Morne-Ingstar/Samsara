"""Apartment-confined shared Windows UI Automation discovery and actions."""
from .service import (
    ControlProxy, Node, Snapshot, StaleTarget, UIAError, UIAService, UIATimeout,
    get_service, normalize_name,
)

__all__ = [
    'ControlProxy', 'Node', 'Snapshot', 'StaleTarget', 'UIAError', 'UIAService',
    'UIATimeout', 'get_service', 'normalize_name',
]
