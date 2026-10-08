"""Pure display values from configuration and plain application state."""


def mode_display(config, wake_models_state):
    """Build a display string for the current mode + wake word state."""
    mode = config.get('mode', 'hold').title()
    if config.get('wake_word_enabled', False):
        wake_state = wake_models_state
        if wake_state != 'ready':
            return f"{mode} + Wake (loading)"
        return f"{mode} + Wake"
    return mode


def idle_animation_enabled(config) -> bool:
    """config ui.idle_animation (default True): the indicator's idle blink
    and glance only -- never state animation, never the tray."""
    ui_cfg = config.get('ui', {})
    if not isinstance(ui_cfg, dict):
        return True
    return bool(ui_cfg.get('idle_animation', True))


def tray_mark(state):
    """(capture, eye) the tray shows for the live app state.

    Capture (wheel): recording > Ava > listening (command mode,
    continuous, or the wake listener armed) > idle. Eye (hands-free):
    the heard animation's keyframe while it plays > asleep (snoozed) >
    armed (wake listener running) > off. See tray_qt.MARK_STATES.
    """
    wake_armed = bool(state.get('wake_word_active', False))
    snoozed = bool(state.get('snoozed', False))
    if state.get('recording', False):
        capture = 'recording'
    elif state.get('ava_mode_active', False):
        capture = 'ava'
    elif (state.get('command_mode_active', False)
            or state.get('continuous_active', False) or wake_armed):
        capture = 'listening'
    else:
        capture = 'idle'

    heard_eye = state.get('_tray_heard_eye', None)
    if heard_eye is not None:
        eye = heard_eye
    elif snoozed:
        eye = 'asleep'
    elif wake_armed:
        eye = 'armed'
    else:
        eye = 'off'
    return capture, eye
