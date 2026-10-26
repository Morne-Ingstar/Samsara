"""Speech-model readiness shared by Home, hotkeys and both listening displays."""
from __future__ import annotations


LOADING = "Loading the speech model"  # PLACEHOLDER COPY (918)
DOWNLOADING = "Getting ready - downloading the speech model"  # PLACEHOLDER COPY (918)
FAILED = "Speech model unavailable - restart Samsara to try again"  # PLACEHOLDER COPY (918)


def status(app):
    if getattr(app, 'model_loaded', True):
        return ""
    return {'downloading': DOWNLOADING, 'failed': FAILED}.get(
        getattr(app, '_speech_model_phase', ''), LOADING)


def show(app):
    """A progress receipt with its own owner; never record it in History."""
    message = status(app)
    indicator = getattr(app, 'listening_indicator', None)
    schedule = getattr(app, '_schedule_ui', None)
    if not message or indicator is None or not callable(schedule):
        return bool(message)
    from samsara.live_surface.feedback import Feedback, FeedbackKind, FeedbackPurpose
    feedback = Feedback(FeedbackKind.RUNNING, FeedbackPurpose.RUNNING,
                        'speech-model', alive=lambda: bool(status(app)))
    if callable(getattr(type(indicator), 'show_feedback', None)):
        schedule(indicator.show_feedback, message, 'accent', None, feedback)
    else:
        schedule(indicator.show_outcome, message, 'accent', None)
    app._speech_model_indicator = indicator
    return True


def phase(update_splash, value):
    """Bound production splash callbacks supply the app; plain callbacks need none."""
    app = getattr(update_splash, '__self__', None)
    if app is None:
        return
    app._speech_model_phase = value
    if value == 'ready':
        indicator = getattr(app, '_speech_model_indicator', None)
        schedule = getattr(app, '_schedule_ui', None)
        if indicator is not None and callable(schedule):
            if callable(getattr(type(indicator), 'clear_feedback', None)):
                schedule(indicator.clear_feedback, 'speech-model')
            else:
                schedule(indicator.clear_outcome)
        app._speech_model_indicator = None
    else:
        show(app)


def show_for_keyboard_hotkey(app):
    """Explain a speech shortcut's wait without changing press/release ownership."""
    if not status(app) or getattr(app, 'snoozed', False):
        return False
    cfg = getattr(app, 'config', {}) or {}
    bindings = [('hotkey', 'ctrl+shift'), ('continuous_hotkey', 'ctrl+alt+d'),
                ('command_hotkey', 'ctrl+alt+c'), ('wake_word_hotkey', 'ctrl+alt+w'),
                ('memo_hotkey', 'ctrl+alt+m'), ('task_hotkey', 'ctrl+alt+t')]
    command = cfg.get('command_mode', {}) or {}
    if command.get('enabled', False):
        bindings.append(('command_button', command.get('button', 'rctrl')))
    from samsara.ai_preferences import ava_master_enabled
    if ava_master_enabled(cfg):
        bindings.append(('ava_mode_key', 'right_alt'))
    for key, default in bindings:
        binding = cfg.get(key, default)
        if key == 'hotkey':
            binding = getattr(app, '_main_hotkey_override', None) or binding
        if (binding and app.parse_hotkey(binding).issubset(app.current_keys)
                and app.check_hotkey_state(binding)):
            show(app)
            return True
    return False
