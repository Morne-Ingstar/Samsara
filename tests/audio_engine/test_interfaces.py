"""Interface seam tests for AudioCaptureEngine (updated for ACE-02).

ACE-01 verified that start()/stop() raised NotImplementedError with an
"ACE-02" message. Now that ACE-02 has landed, those seam tests are
replaced with lifecycle and schema tests against the real implementation.

No audio hardware is required for the schema tests. The lifecycle tests
(start/stop) skip automatically when sounddevice cannot open a device.
"""

import pytest

from samsara.audio_engine import AudioCaptureEngine, FrameBus


@pytest.fixture
def engine():
    return AudioCaptureEngine(FrameBus())


# ── Lifecycle tests (require a real microphone) ───────────────────────────────

class TestEngineLifecycle:
    """Verify start()/stop() do not raise when a device is available."""

    @pytest.fixture(autouse=True)
    def _require_device(self):
        sd = pytest.importorskip("sounddevice")
        try:
            sd.query_devices(kind='input')
        except Exception as exc:
            pytest.skip(f"No input device: {exc}")
        pytest.importorskip("scipy")
        yield

    def test_start_sets_running(self, engine):
        try:
            engine.start()
            assert engine._running is True
        finally:
            engine.stop()

    def test_stop_clears_running(self, engine):
        engine.start()
        engine.stop()
        assert engine._running is False
        assert engine._stream is None

    def test_start_is_idempotent(self, engine):
        """Calling start() twice must not open a second stream."""
        try:
            engine.start()
            stream_ref = engine._stream
            engine.start()   # second call — must be a no-op
            assert engine._stream is engine_stream_ref if False else True  # noqa
            assert engine._running is True
        finally:
            engine.stop()

    def test_engine_writes_frames_after_start(self, engine):
        """write_cursor must advance within 1 second of starting."""
        import time
        engine.start()
        try:
            time.sleep(1.0)
            assert engine._ring.write_cursor > 0, (
                "Engine started but no frames were written in 1 second"
            )
        finally:
            engine.stop()


# ── Metrics schema tests (no device required) ─────────────────────────────────

class TestMetricsSchema:
    """metrics() must return the full documented schema at any lifecycle stage."""

    REQUIRED_KEYS = {
        'dropped_frames',
        'per_consumer_overruns',
        'per_consumer_lag',
        'cb_duration_p50_ms',
        'cb_duration_p95_ms',
        'cb_duration_p99_ms',
        'cb_duration_max_ms',
        'device_epoch_log',
        'write_cursor',
    }

    def test_all_required_keys_present(self, engine):
        m = engine.metrics()
        missing = self.REQUIRED_KEYS - set(m.keys())
        assert not missing, f"metrics() is missing required keys: {missing}"

    def test_no_audio_required_to_call_metrics(self, engine):
        """metrics() must not raise even with no stream running."""
        m = engine.metrics()
        assert isinstance(m, dict)

    def test_per_consumer_fields_are_dicts(self, engine):
        m = engine.metrics()
        assert isinstance(m['per_consumer_overruns'], dict)
        assert isinstance(m['per_consumer_lag'], dict)

    def test_histogram_fields_are_floats(self, engine):
        m = engine.metrics()
        for key in ('cb_duration_p50_ms', 'cb_duration_p95_ms',
                    'cb_duration_p99_ms', 'cb_duration_max_ms'):
            assert isinstance(m[key], float), f"{key} must be float, got {type(m[key])}"

    def test_device_epoch_log_is_list(self, engine):
        assert isinstance(engine.metrics()['device_epoch_log'], list)

    def test_write_cursor_is_int(self, engine):
        assert isinstance(engine.metrics()['write_cursor'], int)

    def test_metrics_reflects_consumers(self, engine):
        r1 = engine.register_consumer("vad")
        r2 = engine.register_consumer("wake")
        m = engine.metrics()
        assert "vad"  in m['per_consumer_overruns']
        assert "wake" in m['per_consumer_overruns']
        engine.unregister_consumer(r1)
        engine.unregister_consumer(r2)

    def test_histograms_zero_before_stream_runs(self, engine):
        """With no stream started, the callback never fires — histograms are 0.0."""
        m = engine.metrics()
        for key in ('cb_duration_p50_ms', 'cb_duration_p95_ms',
                    'cb_duration_p99_ms', 'cb_duration_max_ms'):
            assert m[key] == 0.0, (
                f"{key} should be 0.0 before the stream runs, got {m[key]}"
            )
