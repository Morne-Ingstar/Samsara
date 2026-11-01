"""First-run microphone choices: system default plus an explicit preference."""
from samsara.audio_devices import pick_index_by_name
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)
SYSTEM_DEFAULT = 'System default'


def current_device(owner):
    combo = owner._mic_combo
    if combo is None:
        return None
    name = combo.currentText()
    if name != SYSTEM_DEFAULT:
        return next((m['id'] for m in owner._mics if m['name'] == name), None)
    try:
        import sounddevice as sd
        info = sd.query_devices(kind='input')
        index = pick_index_by_name(owner._mics, info['name'])
        if index is not None:
            return owner._mics[index]['id']
        device = int(sd.default.device[0])
        return device if device >= 0 else None
    except Exception:
        logger.debug('No system default input available', exc_info=True)
        return None


def remember_choice(owner):
    name = owner._mic_combo.currentText()
    owner._mic_preference = next(
        ((m['id'], name) for m in owner._mics if m['name'] == name), None)


def collect_choice(owner):
    preference = getattr(owner, '_mic_preference', None)
    if preference is None:
        owner._config.update(microphone=None, microphone_name=SYSTEM_DEFAULT)
    else:
        owner._config.update(microphone=preference[0], microphone_name=preference[1])


def populate(owner, preserve_selection=False):
    if owner._mic_combo is None:
        return
    combo = owner._mic_combo
    previous = combo.currentText()
    old_names = getattr(owner, '_known_mic_names', None)
    if preserve_selection and owner._mic_scan_error and old_names is not None:
        # A scan error is not evidence of a disconnected input.
        owner._mics = list(owner._last_mic_list)
        if owner._mic_page.isVisible():
            owner._start_meter()
        owner._mic_status.setText("Couldn't scan audio devices — press Refresh")
        return
    if not hasattr(owner, '_mic_preference'):
        saved = getattr(owner, '_existing_config', {})
        name, device = saved.get('microphone_name'), saved.get('microphone')
        match = next((m for m in owner._mics if
                      (m['name'] == name if name and name != SYSTEM_DEFAULT
                       else device is not None and m['id'] == device)), None)
        owner._mic_preference = ((match['id'], match['name']) if match else
                                 (device, name) if name and name != SYSTEM_DEFAULT else None)
    names = {m['name'] for m in owner._mics}
    removed = preserve_selection and old_names is not None and previous in old_names - names
    combo.blockSignals(True)
    try:
        combo.clear()
        combo.addItem(SYSTEM_DEFAULT)
        combo.addItems([m['name'] for m in owner._mics])
        preference = owner._mic_preference
        index = pick_index_by_name(owner._mics, preference[1]) if preference else None
        combo.setCurrentIndex(index + 1 if index is not None else 0)
        if index is not None:
            mic = owner._mics[index]
            owner._mic_preference = (mic['id'], mic['name'])
        combo.setEnabled(bool(owner._mics))
    finally:
        combo.blockSignals(False)
    if hasattr(combo, 'setToolTip'):
        combo.setToolTip(combo.currentText())
    if owner._mic_status:
        message = (f'Mic scan failed ({owner._mic_scan_error})' if owner._mic_scan_error
                   else 'Speak to test your microphone' if owner._mics else 'No microphones detected')
        owner._mic_status.setText(message)
        owner._mic_status.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
    owner._known_mic_names = names
    owner._last_mic_list = list(owner._mics)
    if hasattr(owner, '_mic_notice'):
        if removed:
            owner._offered_mic = None
            owner._show_mic_notice(f'{previous} was disconnected. Using the system default microphone.')
        elif old_names is not None and names - old_names:
            owner._offered_mic = next(m['name'] for m in owner._mics if m['name'] not in old_names)
            owner._show_mic_notice(f'{owner._offered_mic} was plugged in.')
        elif owner._offered_mic not in names:
            owner._offered_mic = None
            owner._mic_notice.hide()
    return True
