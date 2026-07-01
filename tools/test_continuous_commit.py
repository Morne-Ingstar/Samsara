"""Unit tests for ContinuousConsumer's configurable commit trigger (variant A).

Exercises _process_frame / _flush / commit_now directly with synthetic
frames and a mock app -- no real audio hardware, no Qt, no full Samsara boot.

_flush() dispatches app.transcribe_continuous_buffer() on a background
daemon thread (same as production), so assertions that expect a call use a
short poll-with-timeout rather than checking immediately after the
triggering _process_frame() call.

Run with: F:\\envs\\sami\\python.exe tools\\test_continuous_commit.py
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from samsara.audio_engine.continuous_consumer import ContinuousConsumer
from samsara.audio_engine.frame import Frame, FRAME_SIZE, SAMPLE_RATE

SPEECH_THRESHOLD = 0.03
SILENCE_THRESHOLD = 2.0
MIN_SPEECH = 0.3


def _make_frame(seq, amplitude):
    """A synthetic 100ms frame at a given int16-scale amplitude."""
    pcm = np.full(FRAME_SIZE, int(amplitude * 32767), dtype=np.int16)
    return Frame(seq=seq, t_capture=time.perf_counter(), pcm=pcm, device_epoch=0)


def _speech_frame(seq):
    return _make_frame(seq, amplitude=0.5)  # well above SPEECH_THRESHOLD


def _silence_frame(seq):
    return _make_frame(seq, amplitude=0.0)  # below SPEECH_THRESHOLD


def _make_consumer(trigger, max_buffer_s=60.0):
    engine = MagicMock()
    engine.register_consumer.return_value = MagicMock()
    app = MagicMock()
    app.config = {
        'continuous_commit_trigger': trigger,
        'continuous_speech_threshold': SPEECH_THRESHOLD,
        'silence_threshold': SILENCE_THRESHOLD,
        'min_speech_duration': MIN_SPEECH,
        'continuous_max_buffer_s': max_buffer_s,
    }
    app.continuous_active = True
    app.echo_canceller = None
    app.transcribe_continuous_buffer = MagicMock()
    consumer = ContinuousConsumer(engine=engine, app=app)
    return consumer, app


def _wait_until(predicate, timeout=1.0, interval=0.01):
    """Poll predicate() until True or timeout. _flush() dispatches on a
    background thread, so callers expecting a commit must not check
    immediately."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def check(name, condition):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    return condition


def test_key_mode_no_commit_on_silence():
    print("\n--- (a) key mode: no commit fires on silence ---")
    consumer, app = _make_consumer(trigger='key')
    seq = 0
    for _ in range(5):  # 500ms speech, above min_speech
        consumer._process_frame(_speech_frame(seq)); seq += 1
    for _ in range(8):  # long silence -- would trigger a timeout flush in 'silence' mode
        consumer._process_frame(_silence_frame(seq)); seq += 1
    time.sleep(0.2)  # give a would-be background dispatch time to fire if it were going to

    ok = check("no transcribe call fired during silence", not app.transcribe_continuous_buffer.called)
    ok &= check("speech_frames retained (not reset)", len(consumer._speech_frames) == 5)
    ok &= check("session still open (_is_speaking True)", consumer._is_speaking is True)
    return ok


def test_commit_now_fires_and_resets():
    print("\n--- (b) commit_now() fires transcribe with speech-only frames and resets ---")
    consumer, app = _make_consumer(trigger='key')
    seq = 0
    for _ in range(4):
        consumer._process_frame(_speech_frame(seq)); seq += 1
    for _ in range(3):  # dead air -- must NOT be appended, must NOT be in the committed buffer
        consumer._process_frame(_silence_frame(seq)); seq += 1
    for _ in range(4):
        consumer._process_frame(_speech_frame(seq)); seq += 1

    consumer.commit_now()
    fired = _wait_until(lambda: app.transcribe_continuous_buffer.called)

    ok = check("transcribe_continuous_buffer called once", fired and app.transcribe_continuous_buffer.call_count == 1)
    if fired:
        args, _ = app.transcribe_continuous_buffer.call_args
        buffer_arg, rate_arg = args
        ok &= check("committed buffer is speech-only (8 frames, no dead air)", len(buffer_arg) == 8)
        ok &= check("sample rate arg is SAMPLE_RATE", rate_arg == SAMPLE_RATE)
    ok &= check("state reset after commit (speech_frames empty)", len(consumer._speech_frames) == 0)
    ok &= check("state reset after commit (_is_speaking False)", consumer._is_speaking is False)
    return ok


def test_commit_now_below_min_speech_is_noop():
    print("\n--- (c) commit_now() below min_speech is a no-op ---")
    consumer, app = _make_consumer(trigger='key')
    consumer._process_frame(_speech_frame(0))  # 100ms, below MIN_SPEECH (0.3s)

    consumer.commit_now()
    time.sleep(0.2)

    ok = check("transcribe_continuous_buffer NOT called", not app.transcribe_continuous_buffer.called)
    return ok


def test_max_buffer_auto_commits():
    print("\n--- (d) exceeding continuous_max_buffer_s auto-commits ---")
    consumer, app = _make_consumer(trigger='key', max_buffer_s=0.5)  # small cap for a fast test
    seq = 0
    for _ in range(4):  # 400ms < 0.5s cap
        consumer._process_frame(_speech_frame(seq)); seq += 1
    time.sleep(0.1)
    ok = check("no auto-commit before cap reached", not app.transcribe_continuous_buffer.called)

    for _ in range(2):  # now 600ms > cap
        consumer._process_frame(_speech_frame(seq)); seq += 1
    # The cap is only checked on the silence branch (pauses evaluate it) --
    # feed one silence frame to trigger the check.
    consumer._process_frame(_silence_frame(seq)); seq += 1

    fired = _wait_until(lambda: app.transcribe_continuous_buffer.called)
    ok &= check("auto-commit fired once cap exceeded", fired)
    ok &= check("state reset after auto-commit", len(consumer._speech_frames) == 0)
    return ok


def test_silence_mode_timeout_still_commits():
    print("\n--- (e) silence mode: old timeout still commits (regression guard) ---")
    consumer, app = _make_consumer(trigger='silence')
    seq = 0
    for _ in range(4):
        consumer._process_frame(_speech_frame(seq)); seq += 1
    consumer._process_frame(_silence_frame(seq)); seq += 1  # starts _silence_start
    time.sleep(0.1)
    ok = check("no commit yet (silence just started)", not app.transcribe_continuous_buffer.called)

    # Backdate _silence_start past the threshold, then feed one more silence frame.
    consumer._silence_start = time.time() - (SILENCE_THRESHOLD + 0.1)
    consumer._process_frame(_silence_frame(seq)); seq += 1

    fired = _wait_until(lambda: app.transcribe_continuous_buffer.called)
    ok &= check("commit fired after silence timeout", fired)
    if fired:
        args, _ = app.transcribe_continuous_buffer.call_args
        buffer_arg, _ = args
        # Silence mode APPENDS dead-air frames too (unchanged behavior):
        # 4 speech + 2 silence frames appended before flush = 6.
        ok &= check("silence-mode buffer includes dead-air frames (unchanged behavior)",
                    len(buffer_arg) == 6)
    return ok


def main():
    results = [
        test_key_mode_no_commit_on_silence(),
        test_commit_now_fires_and_resets(),
        test_commit_now_below_min_speech_is_noop(),
        test_max_buffer_auto_commits(),
        test_silence_mode_timeout_still_commits(),
    ]
    print(f"\nRESULT: {sum(results)}/{len(results)} test groups passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
