"""DictationSessionConsumer seam-contiguity test — 2026-07-10 hotkey
word-loss investigation.

Constructs a synthetic pre-buffer (frames written before activate()) and
live buffer (frames written by a REAL concurrent writer thread while the
real background drain thread is running, mirroring production: one
PortAudio capture callback thread, one drain thread) with a known,
frame-indexed tone crossing the prebuffer/live seam, then asserts the
buffer drain() assembles is exactly sample-contiguous -- no missing or
duplicated frames anywhere, including at the seam.

This exercises REAL production code (FrameBus, Reader, DictationSession
Consumer) with a REAL background thread — not a hand-rolled reimplementation
of the ring. Per the investigation task: this test may legitimately FAIL;
if so, xfail with a comment rather than silently "fixing" it.
"""
import threading
import time

import numpy as np
import pytest

from samsara.audio_engine import (
    FrameBus,
    FRAME_SIZE,
    FRAME_MS,
    PREBUFFER_FRAMES,
    DictationSessionConsumer,
)


def _pcm(value: int) -> np.ndarray:
    """FRAME_SIZE int16 array filled with `value % 32767` (matches the
    write-index convention used in tests/audio_engine/test_ring.py)."""
    return np.full(FRAME_SIZE, value % 32767, dtype=np.int16)


class _FakeApp:
    """Minimal app stand-in: only the attributes DictationSessionConsumer
    actually reads via getattr(..., default). No Mock() here deliberately
    -- Mock() auto-creates truthy attributes (e.g. audio_coordinator.
    is_speaking) that would silently change activate()'s TTS-guard branch."""
    audio_coordinator = None
    _tts_last_speaking = 0.0
    echo_canceller = None


class _FakeEngine:
    """Wraps a real FrameBus with the register/unregister_consumer
    interface DictationSessionConsumer expects from `engine` -- the real
    AudioCaptureEngine adds thread-registry bookkeeping irrelevant here."""
    def __init__(self, bus: FrameBus) -> None:
        self._bus = bus

    def register_consumer(self, name=None):
        return self._bus.new_reader(name)

    def unregister_consumer(self, reader) -> None:
        reader.invalidate()


class TestSeamContiguity:
    def test_prebuffer_to_live_seam_is_sample_contiguous(self):
        bus = FrameBus()
        engine = _FakeEngine(bus)
        app = _FakeApp()
        consumer = DictationSessionConsumer(engine, app)

        # Pre-hotkey ambient audio: frames 0..19, more than PREBUFFER_FRAMES
        # (15 at the current 1.5s/100ms-frame config) so activate()'s
        # rewind lands inside real history, not clamped to frame 0.
        n_pre = PREBUFFER_FRAMES + 5
        for i in range(n_pre):
            bus.write(_pcm(i), float(i), device_epoch=0)

        # Hotkey press: snap_to_head (cursor -> n_pre) + rewind(PREBUFFER_FRAMES)
        # (cursor -> n_pre - PREBUFFER_FRAMES), spawns the real drain thread.
        consumer.activate()

        # Live hold audio: frames n_pre..n_pre+19, written by THIS thread
        # (standing in for the PortAudio capture callback) while the real
        # background drain thread is concurrently reading -- the realistic
        # concurrency shape, not a single-threaded simulation.
        n_live = 20
        for i in range(n_pre, n_pre + n_live):
            bus.write(_pcm(i), float(i), device_epoch=0)
            time.sleep(0.001)  # encourage real thread interleaving

        # Let the drain thread fully catch up before release.
        time.sleep(0.2)

        audio = consumer.drain()
        assert audio is not None, "drain() returned None -- no audio captured"

        expected_first_seq = n_pre - PREBUFFER_FRAMES
        expected_last_seq  = n_pre + n_live - 1
        expected_n_frames  = expected_last_seq - expected_first_seq + 1
        expected = np.concatenate([
            _pcm(i).astype(np.float32) / 32767.0
            for i in range(expected_first_seq, expected_last_seq + 1)
        ])

        assert len(audio) == expected_n_frames * FRAME_SIZE, (
            f"assembled buffer is {len(audio)} samples "
            f"({len(audio) / FRAME_SIZE:.1f} frames), expected "
            f"{expected_n_frames * FRAME_SIZE} samples "
            f"({expected_n_frames} frames) -- frames were lost or "
            f"duplicated somewhere in [{expected_first_seq}..{expected_last_seq}]"
        )
        # Per-frame comparison (not just np.array_equal on the whole thing)
        # so a failure pinpoints exactly which frame index diverged --
        # i.e. exactly where the seam broke, if it did.
        n_frames = len(audio) // FRAME_SIZE
        mismatches = []
        for frame_idx in range(n_frames):
            seq = expected_first_seq + frame_idx
            got = audio[frame_idx * FRAME_SIZE:(frame_idx + 1) * FRAME_SIZE]
            want = _pcm(seq).astype(np.float32) / 32767.0
            if not np.array_equal(got, want):
                mismatches.append((frame_idx, seq))
        assert not mismatches, (
            f"{len(mismatches)}/{n_frames} frames diverged from the known "
            f"tone at (buffer_index, expected_seq): {mismatches[:10]}"
            + (" ..." if len(mismatches) > 10 else "")
        )

    def test_seam_falls_exactly_at_prebuffer_frames_boundary(self):
        """Sanity check on the test's own model of the seam position --
        NOT a claim about DictationSessionConsumer's internal structure
        (it has no discrete prebuffer/live split; see
        _log_seam_diagnostics's docstring). Just confirms PREBUFFER_FRAMES
        frames precede the live portion in the assembled buffer, matching
        what _log_seam_diagnostics assumes when it logs the seam."""
        bus = FrameBus()
        engine = _FakeEngine(bus)
        app = _FakeApp()
        consumer = DictationSessionConsumer(engine, app)

        n_pre = PREBUFFER_FRAMES + 3
        for i in range(n_pre):
            bus.write(_pcm(i), float(i), device_epoch=0)
        consumer.activate()
        bus.write(_pcm(n_pre), float(n_pre), device_epoch=0)
        time.sleep(0.1)
        audio = consumer.drain()
        assert audio is not None

        n_frames = len(audio) // FRAME_SIZE
        assert n_frames == PREBUFFER_FRAMES + 1, (
            f"expected PREBUFFER_FRAMES ({PREBUFFER_FRAMES}) + 1 live frame "
            f"= {PREBUFFER_FRAMES + 1} frames, got {n_frames}"
        )


class _FakeEchoCanceller:
    """Stand-in for samsara.echo_cancel.EchoCanceller -- matches the real
    production shape (an object that always exists on app.echo_canceller,
    with is_active reflecting the config flag) rather than the None case
    _FakeApp uses elsewhere, since that's what a real disabled-by-default
    AEC actually looks like at this call site."""
    def __init__(self, is_active: bool) -> None:
        self.is_active = is_active
        self.process_calls = 0

    def process(self, pcm_f32):
        self.process_calls += 1
        return pcm_f32


class TestEchoCancellerBypassOnCapturePath:
    """2026-07-10: echo_cancellation.enabled defaults to False. Proves the
    disabled state is a true bypass AT THE CONSUMER'S OWN CAPTURE PATH
    (drain()'s per-frame loop), not just inside EchoCanceller in isolation
    (see tests/test_echo_cancel.py for that)."""

    def test_inactive_echo_canceller_process_never_called(self):
        bus = FrameBus()
        engine = _FakeEngine(bus)
        app = _FakeApp()
        app.echo_canceller = _FakeEchoCanceller(is_active=False)
        consumer = DictationSessionConsumer(engine, app)

        consumer.activate()
        bus.write(_pcm(0), 0.0, device_epoch=0)
        time.sleep(0.1)
        audio = consumer.drain()

        assert audio is not None
        assert app.echo_canceller.process_calls == 0

    def test_active_echo_canceller_process_called_unchanged(self):
        bus = FrameBus()
        engine = _FakeEngine(bus)
        app = _FakeApp()
        app.echo_canceller = _FakeEchoCanceller(is_active=True)
        consumer = DictationSessionConsumer(engine, app)

        consumer.activate()
        bus.write(_pcm(0), 0.0, device_epoch=0)
        time.sleep(0.1)
        audio = consumer.drain()

        assert audio is not None
        assert app.echo_canceller.process_calls > 0


class TestReleaseTail:
    def test_adapts_to_room_tone_above_fixed_threshold(self):
        bus = FrameBus()
        # Simulate the reported microphone: ambient RMS (~0.027) is well
        # above the fixed 0.008 fallback, but remains below actual speech.
        for index in range(PREBUFFER_FRAMES):
            bus.write(_pcm(900), float(index), device_epoch=0)

        consumer = DictationSessionConsumer(_FakeEngine(bus), _FakeApp())
        consumer.activate()
        time.sleep(0.1)  # let the real drain thread collect the prebuffer

        def writer():
            # Preserve two final speech frames, then stop after 300 ms of the
            # same above-threshold room tone instead of waiting for max_tail.
            for value in (2200, 2200, 900, 900, 900):
                bus.write(_pcm(value), time.monotonic(), device_epoch=0)
                time.sleep(0.1)

        thread = threading.Thread(target=writer)
        thread.start()
        started = time.monotonic()
        audio = consumer.drain_after_release(
            silence_ms=300,
            max_tail_ms=1200,
            speech_threshold=0.008,
        )
        elapsed = time.monotonic() - started
        thread.join(timeout=1.0)

        assert audio is not None
        assert elapsed < 0.9
        tail = audio[-5 * FRAME_SIZE:]
        expected = np.concatenate([
            _pcm(value).astype(np.float32) / 32767.0
            for value in (2200, 2200, 900, 900, 900)
        ])
        assert np.array_equal(tail, expected)

    def test_continued_speech_is_kept_until_consecutive_quiet_frames(self):
        bus = FrameBus()
        consumer = DictationSessionConsumer(_FakeEngine(bus), _FakeApp())
        consumer.activate()
        first_seq = bus.write_cursor

        def writer():
            time.sleep(0.02)
            for value in (12000, 12000, 0, 0):
                bus.write(_pcm(value), time.monotonic(), device_epoch=0)
                time.sleep(0.01)

        thread = threading.Thread(target=writer)
        thread.start()
        audio = consumer.drain_after_release(
            silence_ms=200,
            max_tail_ms=1000,
            speech_threshold=0.01,
        )
        thread.join(timeout=1.0)

        assert audio is not None
        assert len(audio) >= 4 * FRAME_SIZE
        tail = audio[-4 * FRAME_SIZE:]
        expected = np.concatenate([
            _pcm(value).astype(np.float32) / 32767.0
            for value in (12000, 12000, 0, 0)
        ])
        assert np.array_equal(tail, expected)
        assert bus.write_cursor >= first_seq + 4

    def test_continuous_speech_still_stops_at_hard_cap(self):
        bus = FrameBus()
        consumer = DictationSessionConsumer(_FakeEngine(bus), _FakeApp())
        consumer.activate()

        def writer():
            for index in range(20):
                bus.write(_pcm(12000 + index), time.monotonic(), device_epoch=0)
                time.sleep(0.01)

        thread = threading.Thread(target=writer)
        thread.start()
        started = time.monotonic()
        audio = consumer.drain_after_release(
            silence_ms=20,
            max_tail_ms=50,
            speech_threshold=0.01,
        )
        elapsed = time.monotonic() - started
        thread.join(timeout=1.0)

        assert audio is not None
        assert elapsed < 0.5


# 404: real ring and production accumulators, with deterministic scheduling.
def _onset_activate(consumer, streaming, monkeypatch):
    import samsara.audio_engine.dictation_consumer as module
    monkeypatch.setattr(module.thread_registry, 'spawn', lambda *args, **kwargs: None)
    assert (consumer.activate_streaming if streaming else consumer.activate)()


def _onset_pump(consumer, streaming, monkeypatch):
    import samsara.audio_engine.dictation_consumer as module
    stop = consumer._streaming_stop if streaming else consumer._drain_stop
    stop.clear()
    monkeypatch.setattr(module.time, 'sleep', lambda _: stop.set())
    (consumer._streaming_drain_loop if streaming else consumer._hold_drain_loop)(stop)


def _onset_audio(consumer, streaming):
    return (consumer.stop_streaming if streaming else consumer.drain)()


def _onset_app(eligible=True, request=10.0, confirmation=10.02):
    from samsara.hold_capture_onset import HoldOnset
    from unittest.mock import Mock
    app = _FakeApp()
    app._hold_capture_duck_token = -1
    app._hold_capture_duck_confirmed_at = confirmation
    app.onset_record = Mock()
    app._hold_capture_onset = HoldOnset(-1, request, confirmation, eligible, app.onset_record)
    return app


def _onset_write(bus, ends, first_value=100):
    for index, end in enumerate(ends):
        bus.write(_pcm(first_value + index), end, device_epoch=0)


def _onset_expected(values):
    return np.concatenate([_pcm(value).astype(np.float32) / 32767.0 for value in values])


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('phase', [0.0, 0.025, 0.05, 0.075, 0.099])
@pytest.mark.parametrize('eligible', [False, True])
def test_404_onset_phase_sweep_keeps_only_whole_blocks(monkeypatch, streaming, phase, eligible):
    app = _onset_app(eligible=eligible)
    boundary = app._hold_capture_onset.boundary
    # Ends are exactly 100 ms apart. The boundary falls at each chosen phase.
    ends = [boundary + phase + index / 10 for index in range(-3, 6)]
    starts = [end - FRAME_MS / 1000.0 for end in ends]
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    _onset_write(bus, ends)
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    expected_values = [100 + index for index, start in enumerate(starts) if start >= boundary]
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected(expected_values))
    app._hold_capture_onset.finish()
    record = app.onset_record.call_args.kwargs
    assert record['first_kept_block_start'] == next(start for start in starts if start >= boundary)
    assert record['opening_blocks_dropped'] == sum(start < boundary for start in starts)
    assert record['policy'] == ('eligible' if eligible else 'strict')
    assert record['boundary'] == boundary
    app.onset_record.assert_called_once()


@pytest.mark.parametrize('streaming', [False, True])
def test_404_exact_equality_and_end_cutoff(monkeypatch, streaming):
    # Binary-exact subtraction gives an exact equality at 0.0; adjacent
    # representable ends prove neither boundary is accidentally widened.
    app = _onset_app(request=0.25, confirmation=0.27)
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    ends = [np.nextafter(0.1, -np.inf), 0.1, 0.2, np.nextafter(0.2, np.inf)]
    _onset_write(bus, ends)
    _onset_activate(consumer, streaming, monkeypatch)
    import samsara.audio_engine.dictation_consumer as module
    monkeypatch.setattr(module.time, 'perf_counter', lambda: 0.2)
    consumer.finish_capture()
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected([101, 102]))
    assert consumer._capture_not_after == 0.2


@pytest.mark.parametrize('streaming', [False, True])
def test_404_earcon_delay_recovers_audio_since_request(monkeypatch, streaming):
    app = _onset_app()
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    # History at key-down, then blocks arriving during the unchanged earcon
    # wait. The consumer activates only after both groups have been written.
    _onset_write(bus, [9.7, 9.8, 9.9, 10.0])
    _onset_write(bus, [10.1, 10.2], first_value=104)
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected([102, 103, 104, 105]))
    assert app._hold_capture_duck_confirmed_at == 10.02


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('guard', ['active', 'recent'])
def test_404_tts_still_suppresses_rewind(monkeypatch, streaming, guard):
    from types import SimpleNamespace
    import samsara.audio_engine.dictation_consumer as module
    app = _onset_app()
    app.audio_coordinator = SimpleNamespace(is_speaking=guard == 'active')
    app._tts_last_speaking = 19.9 if guard == 'recent' else 0.0
    monkeypatch.setattr(module.time, 'monotonic', lambda: 20.0)
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    _onset_write(bus, [9.9, 10.0, 10.1])
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_write(bus, [10.2], first_value=103)
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected([103]))
    app._hold_capture_onset.finish()
    assert app.onset_record.call_args.kwargs['rewind_suppressed'] == 'tts_' + guard


@pytest.mark.parametrize('streaming', [False, True])
def test_404_second_hold_resets_policy_and_snaps_stale_cursor(monkeypatch, streaming):
    app = _onset_app()
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    _onset_write(bus, [9.7, 9.8, 9.9, 10.0, 10.1])
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected([102, 103, 104]))
    app._hold_capture_onset = None
    app._hold_capture_duck_token = -2
    app._hold_capture_duck_confirmed_at = 12.02
    _onset_write(bus, [10.2 + i / 10 for i in range(25)], first_value=200)
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected([220, 221, 222, 223, 224]))
    assert consumer._capture_not_before == 12.02


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('lane', ['command_mode_recording', 'ava_mode_recording', '_memo_recording'])
def test_404_special_lanes_ignore_stale_ordinary_onset(monkeypatch, streaming, lane):
    app = _onset_app()
    setattr(app, lane, True)
    # Same absence of a confirmation boundary as ordinary HEAD special lanes.
    app._hold_capture_duck_confirmed_at = None
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    _onset_write(bus, [9.0 + i / 10 for i in range(16)])
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected(range(101, 116)))
    app.onset_record.assert_not_called()
    assert consumer._capture_not_before is None


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('mismatch', ['token', 'confirmation'])
def test_404_stale_token_or_confirmation_cannot_relax_boundary(monkeypatch, streaming, mismatch):
    app = _onset_app()
    if mismatch == 'token':
        app._hold_capture_duck_token = -2
    else:
        app._hold_capture_duck_confirmed_at = 10.25
    bus = FrameBus()
    consumer = DictationSessionConsumer(_FakeEngine(bus), app)
    _onset_write(bus, [9.9, 10.0, 10.1, 10.2, 10.3, 10.4])
    _onset_activate(consumer, streaming, monkeypatch)
    _onset_pump(consumer, streaming, monkeypatch)
    values = [103, 104, 105] if mismatch == 'token' else [105]
    np.testing.assert_array_equal(_onset_audio(consumer, streaming), _onset_expected(values))
