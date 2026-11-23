"""Advisory background-noise check on the existing microphone-health feed."""

import logging
import math

from samsara.audio_engine.frame import FRAME_MS
from samsara.constants import DEFAULT_SPEECH_THRESHOLD
from samsara.mic_check import NOISE_HIGH_DBFS
from samsara.notify_gate import saved_level

WINDOW_FRAMES = 1500 // FRAME_MS
REMINDER_SECONDS = 3600
HIGH_NOISE_RMS = 10 ** (NOISE_HIGH_DBFS / 20)
# PLACEHOLDER COPY (1324)
TITLE = "Background noise"
MESSAGE = (
    "The wake word may have trouble hearing you over background sound like music "
    "or video. Turning it down or using the hold key may help."
)
logger = logging.getLogger("Samsara")


def _positive_rms(value, fallback):
    try:
        rms = float(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return rms if not isinstance(value, bool) and math.isfinite(rms) and 0 < rms <= 1 else fallback


def noise_threshold(config):
    audio = (config.get('wake_word_config') or {}).get('audio') or {}
    speech = _positive_rms(audio.get('speech_threshold'), DEFAULT_SPEECH_THRESHOLD)
    floor = 0.
    if audio.get('calibrated_microphone', config.get('microphone')) == config.get('microphone'):
        floor = _positive_rms(audio.get('measured_noise_floor'), 0.)
    # Check my mic labels -35 dBFS as high. Require roughly 6 dB above a
    # known ambient floor, capped at the configured RMS speech threshold.
    return min(speech, max(HIGH_NOISE_RMS, floor * 2))


class WakeNoiseNotice:
    def __init__(self, app):
        self.app = app
        self.active_since = None
        self.last_notice = float('-inf')
        self.next_check = float('-inf')
        self.levels = []
        self.previous_frame = None

    def _reset_window(self):
        self.levels.clear()
        self.previous_frame = None

    def begin_poll(self, now, *, active):
        if not active or saved_level(getattr(self.app, 'config', {})) != 'all':
            self.active_since = None
            self._reset_window()
            return False
        if self.active_since is None:
            self.active_since = now
            self.next_check = self.last_notice + REMINDER_SECONDS
        return now >= self.next_check

    def observe(self, rms, stamp, seq, identity, *, skip):
        if stamp < max(self.active_since, self.next_check) or skip or not (
            math.isfinite(rms) and rms >= 0
        ):
            self._reset_window()
            return
        if self.previous_frame is not None:
            old_stamp, old_seq, old_identity = self.previous_frame
            if old_identity != identity or old_seq + 1 != seq or not 0 < stamp - old_stamp <= .25:
                self._reset_window()
        self.previous_frame = stamp, seq, identity
        self.levels.append(rms)
        if len(self.levels) < WINDOW_FRAMES:
            return
        # A sustained lower-quartile level rejects a short cue or utterance.
        background = sorted(self.levels)[(WINDOW_FRAMES - 1) // 4]
        self._reset_window()
        config = self.app.config
        if background >= noise_threshold(config) and self._deliver():
            self.last_notice = stamp
        self.next_check = stamp + REMINDER_SECONDS

    def _deliver(self):
        if not getattr(self.app, 'wake_word_active', False) or saved_level(self.app.config) != 'all':
            return False
        notify = getattr(getattr(self.app, 'tray_icon', None), 'notify_info', None)
        if not callable(notify):
            return False
        try:
            notify(TITLE, MESSAGE)
        except Exception:
            logger.debug("[WAKE] Background-noise notice delivery unavailable")
            return False
        return True
