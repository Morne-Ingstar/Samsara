"""Fresh-profile placement and onboarding's existing Settings apply seam."""
from copy import deepcopy

from samsara import listening_display


def primary_monitor():
    # Resolve the Windows device id only; Qt consumers obtain work-area DIPs.
    import ctypes
    from ctypes import wintypes
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                    ('work', wintypes.RECT), ('flags', wintypes.DWORD),
                    ('device', wintypes.WCHAR * 32)]
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    info = MonitorInfo()
    info.size = ctypes.sizeof(info)
    if user32.GetMonitorInfoW(user32.MonitorFromPoint(wintypes.POINT(0, 0), 1), ctypes.byref(info)):
        return info.device
    raise RuntimeError('Primary display device could not be resolved')


def placement_updates(config, value, monitor=None):
    """Preserve other monitor records and UI preferences when choosing a preset."""
    from samsara.live_surface.placement import Edge, Placement, placement_record
    vertical, horizontal = value.split('-')
    edge = Edge.TOP if vertical == 'top' else Edge.BOTTOM
    fraction = {'left': 0.0, 'center': 0.5, 'right': 1.0}[horizontal]
    monitor = monitor or primary_monitor()
    ui = deepcopy(config.get('ui') or {})
    surface = ui.setdefault('live_surface', {})
    placement = surface.setdefault('placement', {})
    placement.setdefault('monitors', {})[monitor] = placement_record(Placement(edge, fraction))
    placement['preferred_monitor'] = monitor
    return {'listening_indicator_position': value, 'ui': ui}


def seed_fresh(config, *, fresh, monitor=None):
    """Called only at proven absent-file boundaries; legacy fallbacks stay intact."""
    if fresh:
        config.update(placement_updates(config, 'bottom-left', monitor))
    return config


def current_position(config, display=None):
    display = display or listening_display.display_choice(config).value
    if display == 'compact':
        return config.get('listening_indicator_position', 'bottom-center')
    placement = ((config.get('ui') or {}).get('live_surface') or {}).get('placement') or {}
    records = placement.get('monitors') or {}
    record = records.get(placement.get('preferred_monitor') or primary_monitor())
    if not record:
        return 'bottom-center'
    if record.get('edge') not in ('top', 'bottom'):
        return 'custom'
    column = {0.0: 'left', 0.5: 'center', 1.0: 'right'}.get(record.get('t', .5))
    return f"{record['edge']}-{column}" if column else 'custom'


def choose_for_app(app, value):
    """Persist with the app's shared writer, then use the Settings live apply call."""
    app.update_config_and_save(placement_updates(app.config, value))
    app.apply_listening_indicator_settings()


def apply_full(app):
    """Full's adapter ignores legacy set_position; apply its saved monitor record."""
    controller = getattr(app, 'live_surface', None)
    if controller is not None:
        app._schedule_ui(controller._place_widget)
