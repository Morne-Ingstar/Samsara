"""Microphone submenu, sharing the tray's action/error boundary."""
import logging

from PySide6.QtWidgets import QMenu

from samsara.microphone_recovery import selection_name, select_system_default

log = logging.getLogger(__name__)


def build_microphone_menu(app, add):
    if not app._mic_refresh_blocked():
        try:
            app.available_mics = app.get_available_microphones()
            app._reconcile_microphone_selection()
        except Exception as exc:
            log.debug('Microphone menu refresh: %s', exc)
    menu = QMenu(f'[MIC]  {selection_name(app)}')
    current_id = app.config.get('microphone')
    current_name = app.config.get('microphone_name')
    default_selected = current_name in ('System default', 'Default') or (
        not current_name and current_id is None)
    add(menu, 'System default',
        lambda _checked: select_system_default(app, app.switch_microphone_and_refresh),
        checked=default_selected, report_as='Switch microphone')
    for mic in app.available_mics:
        selected = mic['name'] == current_name if current_name else mic['id'] == current_id
        add(menu, mic['name'],
            lambda _checked, mid=mic['id']: app.switch_microphone_and_refresh(mid),
            checked=not default_selected and selected, report_as='Switch microphone')
    return menu
