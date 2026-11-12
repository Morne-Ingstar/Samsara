"""Microphone submenu, sharing the tray's action/error boundary."""
import logging

from PySide6.QtWidgets import QMenu, QMessageBox

from samsara.audio_devices import handsfree_warning, microphone_choice, microphone_label, preferred_microphone_name
from samsara.microphone_recovery import selection_name, select_microphone, select_system_default
from samsara.ui import theme

log = logging.getLogger(__name__)


def build_microphone_menu(app, add):
    changes = getattr(app, '_microphone_changes', None)
    if changes is not None and (changes._input_pending or changes._pending is not None
                                or getattr(changes, '_input_stale', False)):
        changes.request_input()  # 1206: keep stale-list intent queued during speech.
    if not app._mic_refresh_blocked():
        try:
            app.available_mics = app.get_available_microphones()
            app._reconcile_microphone_selection()
        except Exception as exc:
            log.debug('Microphone menu refresh: %s', exc)
    menu = QMenu(f'[MIC]  {selection_name(app)}')
    menu.setStyleSheet(f'QMenu::item:disabled {{ color: {theme.TEXT_DISABLED}; }}')
    current_name = selection_name(app)
    preferred = preferred_microphone_name(app.available_mics, app.config)
    default_selected = current_name == 'System default'
    def pick(mic):
        message = handsfree_warning(mic['name'], app.available_mics)
        if message:
            QMessageBox.information(None, 'Microphone quality', message)
        select_microphone(app, mic['id'], app.switch_microphone_and_refresh)

    add(menu, 'System default',
        lambda _checked: select_system_default(app, app.switch_microphone_and_refresh),
        checked=default_selected, report_as='Switch microphone')
    for mic in app.available_mics:
        selected = mic['name'] == current_name
        add(menu, microphone_label(mic['name'], preferred),
            lambda _checked, entry=mic: pick(entry),
            checked=not default_selected and selected, report_as='Switch microphone')
    if preferred and microphone_choice(app.available_mics, app.config) is None:
        missing = menu.addAction(microphone_label(preferred, preferred, connected=False))
        missing.setEnabled(False)
    return menu
