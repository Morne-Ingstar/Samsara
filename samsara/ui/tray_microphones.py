"""Microphone submenu, sharing the tray's action/error boundary."""
import logging

from PySide6.QtWidgets import QMenu, QMessageBox

from samsara.audio_devices import handsfree_warning, microphone_choice, microphone_label, preferred_microphone_name
from samsara.microphone_recovery import display_microphones, selection_name, select_microphone, select_system_default
from samsara.ui import theme

log = logging.getLogger(__name__)


def build_microphone_menu(app, add):
    changes = vars(app).get('_microphone_changes')
    if (changes is not None and getattr(changes, '_input_stale', False)
            and not changes._input_pending):
        # 1218: retry a given-up scan without shortening a pending burst's wait.
        changes.request_input()
    if not app._mic_refresh_blocked():
        try:
            # 1218: keep 901's cached-index/name reconciliation before the
            # label. Reading PortAudio's table does not reset or close capture.
            app.available_mics = app.get_available_microphones()
            if changes is not None:
                changes._backend_mics = list(app.available_mics)
            app._reconcile_microphone_selection()
        except Exception as exc:
            log.debug('Microphone menu refresh: %s', exc)
    display_microphones(app)
    menu = QMenu(f'[MIC]  {selection_name(app)}')
    menu.setStyleSheet(f'QMenu::item:disabled {{ color: {theme.TEXT_DISABLED}; }}')
    current_name = selection_name(app)
    preferred = preferred_microphone_name(app.available_mics, app.config)
    default_selected = current_name == 'System default'
    def pick(mic):
        message = handsfree_warning(mic['name'], app.available_mics)
        if message:
            QMessageBox.information(None, 'Microphone quality', message)
        select_microphone(app, mic['id'], app.switch_microphone_and_refresh, name=mic['name'])

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
