"""Queue 51: the mic setup guide's wake-word step judges attempts against
levels derived from the measured background floor, and says what happened.

  * an attempt whose audio stays below the derived speech level reports
    "never reached the detector", not a bare miss
  * a user at 0.01 RMS with a positive detector score passes the step
  * re-arm works when the room sits just under (or even over) the re-arm level
  * with no calibration the step falls back to the constants and says so
Never imports dictation.py; the OpenWakeWord model is replaced by a fake.
"""
import logging
from unittest.mock import Mock

import numpy as np
import pytest

from samsara.constants import ADAPTIVE_SPEECH_FLOOR_RATIO


class _Detector:
    """Fake WakeWordDetector: returns queued scores (default 0.0) and
    records the RMS of every chunk it is fed."""

    is_available = True
    _threshold = 0.2

    def __init__(self, *args, **kwargs):
        self.scores = []
        self.fed_rms = []
        self.resets = 0

    def process_audio(self, chunk):
        self.fed_rms.append(float(np.sqrt(np.mean(np.square(chunk)))))
        return self.scores.pop(0) if self.scores else 0.0

    def reset(self):
        self.resets += 1


class _App:
    def __init__(self, floor=None):
        audio = {} if floor is None else {"measured_noise_floor": floor}
        self.config = {"microphone": 1, "microphone_name": "Mic",
                       "wake_word_config": {"phrase": "jarvis", "oww_threshold": 0.2, "audio": audio}}

    def get_available_microphones(self):
        return [{"id": 1, "name": "Mic"}]


def _chunk(rms):
    return np.full(1600, rms, dtype=np.float32)


@pytest.fixture
def step(qapp, monkeypatch):
    from samsara import wake_detector
    from samsara.ui import mic_setup_wizard_qt as wizard

    monkeypatch.setattr(wizard._WizardWindow, "_ensure_audio_running", lambda self: None)
    monkeypatch.setattr(wake_detector, "WakeWordDetector", _Detector)
    windows = []

    def make(floor=None, measured_now=None):
        window = wizard._WizardWindow(_App(floor))
        window._current_step = window._STEP_WAKE
        window._wake_floor = measured_now
        window._setup_oww_test()
        windows.append(window)
        return window

    yield wizard, make
    for w in windows:
        w._finish_oww_test()
        w.close()


def test_levels_are_derived_from_the_measured_floor():
    from samsara.ui import mic_setup_wizard_qt as wizard
    levels = wizard._derive_wake_levels(0.00643)
    assert levels["source"] == "measured"
    assert levels["speech_rms"] == pytest.approx(0.00643 * ADAPTIVE_SPEECH_FLOOR_RATIO)
    assert levels["rearm_rms"] == levels["speech_rms"]
    for bad in (None, 0, -1, float("nan"), "x"):
        assert wizard._derive_wake_levels(bad)["source"] == "defaults"


def test_audio_below_the_derived_speech_level_reports_never_reached_the_detector(step, caplog):
    wizard, make = step
    window = make(measured_now=0.00643)                 # speech level 0.0096
    for _ in range(40):
        window._feed_wake_detector(_chunk(0.0060))

    with caplog.at_level(logging.INFO):
        window._advance_attempt(hit=False)

    note = window._oww_tip.text()
    assert "Attempt 1:" in note and "never reached the detector" in note
    assert "40 chunks, 40 scored" in note and "threshold 0.20" in note
    line = next(r.getMessage() for r in caplog.records if "Wake attempt 1/3" in r.getMessage())
    assert "reason=below_speech_level" in line and "frames=40 fed=40" in line
    assert "rms_peak=0.00600" in line and "max_score=0.000" in line and "threshold=0.2" in line


def test_speech_that_scores_below_threshold_says_so(step, caplog):
    wizard, make = step
    window = make(measured_now=0.00643)
    window._oww_detector.scores = [0.05] * 10 + [0.13] + [0.02] * 9
    for rms in [0.006] * 10 + [0.02] * 10:
        window._feed_wake_detector(_chunk(rms))
    with caplog.at_level(logging.INFO):
        window._advance_attempt(hit=False)
    assert "speech arrived, but the wake-word score stayed below the threshold" in window._oww_tip.text()
    assert "best score 0.13" in window._oww_tip.text()
    assert any("reason=score_below_threshold" in r.getMessage() and "max_score=0.130" in r.getMessage()
               for r in caplog.records)


def test_no_audio_attempt_is_reported_as_such(step):
    wizard, make = step
    window = make(measured_now=0.00643)
    window._advance_attempt(hit=False)
    assert "no audio arrived from the microphone" in window._oww_tip.text()


def test_user_at_0_01_rms_with_a_positive_score_passes_the_step(step):
    """A voice peaking at 0.01 RMS in a 0.00643 room: every chunk is fed
    (amplified toward 0.10 like the live path), three positive detections
    with room tone between them pass the step."""
    wizard, make = step
    window = make(measured_now=0.00643)
    det = window._oww_detector
    for attempt in range(3):
        det.scores = [0.0] * 5 + [0.0, 0.6]            # room, room..., speech, detection
        for rms in [0.006] * 5 + [0.010, 0.010]:
            window._feed_wake_detector(_chunk(rms))
        for _ in range(3):                             # room tone after the hit re-arms
            window._feed_wake_detector(_chunk(0.006))

    assert window._oww_hits == 3
    assert "wake word is working" in window._oww_result_lbl.text()
    assert max(det.fed_rms) == pytest.approx(0.10, rel=1e-3), "0.01 RMS speech is amplified like the live path"
    assert "heard it" in window._oww_tip.text()


def test_rearm_works_when_the_room_sits_just_under_the_rearm_level(step):
    """Floor 0.0068 -> re-arm level 0.0102. The room at 0.0101 is ABOVE the
    old fixed _OWW_REARM_RMS (re-arm needed rms <= 0.010, so it could never
    re-arm there) but 1 % under the derived level, so the second detection counts."""
    wizard, make = step
    window = make(measured_now=0.0068)
    assert 0.0101 > wizard._OWW_REARM_RMS
    det = window._oww_detector
    det.scores = [0.7]
    window._feed_wake_detector(_chunk(0.02))                 # hit 1 -> disarmed
    assert window._oww_armed is False
    for _ in range(wizard._OWW_REARM_CHUNKS):
        window._feed_wake_detector(_chunk(0.0101))          # room, 1 % under the level
    assert window._oww_armed is True and det.resets >= 2            # setup reset + re-arm reset
    det.scores = [0.7]
    window._feed_wake_detector(_chunk(0.02))                 # hit 2
    assert window._oww_hits == 2


def test_rearm_does_not_need_the_room_to_get_quieter_than_it_can(step, monkeypatch):
    wizard, make = step
    window = make(measured_now=0.0068)
    import time as real_time
    from types import SimpleNamespace
    clock = {"t": 1000.0}
    monkeypatch.setattr(wizard, "time", SimpleNamespace(monotonic=lambda: clock["t"], sleep=real_time.sleep))
    det = window._oww_detector
    det.scores = [0.7]
    window._feed_wake_detector(_chunk(0.02))                 # hit -> disarmed
    for _ in range(10):                                             # room LOUDER than the re-arm level
        window._feed_wake_detector(_chunk(0.015))
    assert window._oww_armed is False
    clock["t"] += wizard._OWW_REARM_MAX_S
    window._feed_wake_detector(_chunk(0.015))
    assert window._oww_armed is True


def test_no_calibration_falls_back_to_the_constants_and_says_so(step, caplog):
    wizard, make = step
    with caplog.at_level(logging.INFO):
        window = make(floor=None, measured_now=None)
    assert window._oww_levels["source"] == "defaults"
    assert window._oww_levels["speech_rms"] == wizard._OWW_REARM_RMS
    assert "No background calibration available -- using default levels" in window._oww_tip.text()
    assert any("Wake test levels (defaults)" in r.getMessage() for r in caplog.records)


def test_saved_floor_is_used_when_this_run_did_not_measure(step):
    wizard, make = step
    window = make(floor=0.0059, measured_now=None)
    assert window._oww_levels["source"] == "measured" and window._oww_levels["floor"] == 0.0059
    assert "from your measured background 0.0059" in window._oww_tip.text()


# 67: the literal-matching gain test that stood here is superseded by
# tests/test_wizard_wake_signal_path.py, which requires the step and the live
# consumer to call the one shared pre-filter instead of holding copies.
