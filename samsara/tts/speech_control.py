"""Speech-only controls. Never dispatch commands or resolve pending questions."""
from __future__ import annotations

import re

STOP_PHRASES = frozenset({"stop", "quiet", "stop talking", "that's enough", "stop speaking"})
LONG_READING_CHARS = 240
HINT_ID = "speech_stop_1149"
HINT_TEXT = "Say stop to stop"  # PLACEHOLDER COPY (1149)


def normalize(text):
    return " ".join(re.findall(r"[a-z]+(?:'[a-z]+)?", str(text).lower().replace("’", "'")))


def stop_speaking(app, *, expected=None):
    """Stop current/queued audio, retaining the session, draft and approvals.

    expected is the decoder's speech token: a late result must never cancel
    the next answer. UI actions deliberately target whatever is playing now.
    """
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None:
        return False
    with coordinator._dispatch_lock:
        if not coordinator.is_speaking:
            return False
        if expected is not None and speech_token(app) != expected:
            return False
        coordinator._speech_stop_serial += 1
        coordinator._speech_replay_stopped = True
        coordinator.cancel_speech()
    # Cancelled engines need not call on_done; release held captions ourselves.
    from samsara.ui import ava_captions_qt
    ava_captions_qt.release_caption(app)
    return True


def speech_token(app):
    coordinator = getattr(app, "audio_coordinator", None)
    if coordinator is None or coordinator.is_speaking is not True:
        return None
    return (id(coordinator), getattr(coordinator, "_speech_generation", 0),
            getattr(coordinator, "_speech_item_serial", 0), getattr(coordinator, "_speech_stop_serial", 0))


def repeat_speech(app, *, stopped_only=False):
    """Replay words only; never replay an action or a producer's callback."""
    coordinator = getattr(app, "audio_coordinator", None)
    replay = getattr(coordinator, "_speech_replay", None)
    if not isinstance(replay, dict) or not replay:
        return False
    if stopped_only and not coordinator._speech_replay_stopped:
        return False
    with coordinator._dispatch_lock:
        # Stop callbacks first, even when replacing a still-playing readout.
        if coordinator.is_speaking:
            stop_speaking(app)
        handle = coordinator.speak(**replay)
    return not str(handle.utterance_id).startswith("noop-")


def voice_control(app, text):
    """Only explicitly addressed command/Ava input calls this text entry."""
    phrase = normalize(text)
    if phrase in STOP_PHRASES:
        return stop_speaking(app)
    if phrase == "repeat that":
        return repeat_speech(app, stopped_only=True)
    return False


def announce_reading(coordinator, text, source):
    """Bind answer controls and offer the one-shot hint on the Qt thread.

    A long read with captions disabled gets a small control window without
    turning captions on or exposing the readback text.
    """
    from samsara.ui import qt_runtime
    if not qt_runtime.is_alive():
        return
    token = speech_token(coordinator.app)

    def show():
        if speech_token(coordinator.app) != token:
            return
        from samsara.ui import ava_captions_qt, ava_search_panel_qt
        ava_search_panel_qt.bind_speech(coordinator.app)
        if len(text) >= LONG_READING_CHARS:
            window = ava_captions_qt._ensure_window(coordinator.app)
            config = coordinator.app.config
            displayed = text if ava_captions_qt.captions_enabled(config) else "Reading aloud"  # PLACEHOLDER COPY (1149)
            window.show_text(displayed, True, ava_captions_qt.caption_linger_s(config), source)
            window._speech_controls.refresh()
            hints = getattr(coordinator.app, "hints", None)
            if hints is not None:
                hints.show_embedded_once(HINT_ID, window._speech_controls.show_hint)

    qt_runtime.post(show)
