"""Startup output interlock and the wake consumer's hold-suppression decision."""

import time
from contextlib import nullcontext

from samsara.log import get_logger

logger = get_logger(__name__)


def hold_capture_active(app):
    """Include the consumer's drain interval after the hotkey flags clear."""
    consumer = getattr(app, '_dictation_consumer', None)
    return any(value is True for value in (
        getattr(app, 'recording', False), getattr(app, '_hotkey_recording', False),
        getattr(consumer, '_active', False),
        getattr(consumer, '_streaming_capturing', False)))


def _on_wake_ready(app):
    """1052b: serialize startup output with hold admission, without delaying arming."""
    _startup_output(app, 'wake')


def continuous_ready_output(app):
    """Continuous mode can also auto-start on the asynchronous model loader."""
    _startup_output(app, 'continuous')


def _startup_output(app, mode):
    pending = f'_{mode}_ready_output_pending'
    with getattr(app, '_hold_capture_lifecycle_lock', None) or nullcontext():
        if hold_capture_active(app):
            setattr(app, pending, True)
            logger.info('%s ready: cue skipped, hold active', mode)
            return
        setattr(app, pending, False)
        if mode == 'wake':
            app._start_hands_free_idle_duck()
        app.play_sound('start', use_winsound=True)
        time.sleep(.15)


def resume_wake_ready_output(app, mode='wake'):
    """The existing wake poll services the deferred output once, at the next idle."""
    pending = f'_{mode}_ready_output_pending'
    if getattr(app, pending, False) is not True:
        return
    with getattr(app, '_hold_capture_lifecycle_lock', None) or nullcontext():
        if getattr(app, pending, False) is not True:
            return
        # An already-running toggle consumer can poll before startup publishes
        # wake_word_active. Toggle-off explicitly clears pending output.
        active = 'wake_word_active' if mode == 'wake' else 'continuous_active'
        if getattr(app, active, False) and not hold_capture_active(app):
            _startup_output(app, mode)


def wake_hold_suppressed(consumer, app):
    """Preserve the configured toggle exemption while a hotkey hold is active."""
    toggle_cmd_exempt = consumer._is_toggle_cmd(app) and not consumer._suspend_on_hold_enabled(app)
    return app._hotkey_recording and not toggle_cmd_exempt
