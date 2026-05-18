"""Interface seam tests for AudioCaptureEngine (ACE-01).

Verifies that:
  1. start() and stop() raise NotImplementedError with a message naming ACE-02.
     This proves the seam exists and is clearly signed, not silently absent.
  2. metrics() returns the documented dict shape with all required keys present.
     Placeholder values (0 / empty) are acceptable; missing keys are not.

No audio hardware is required. No sounddevice import.
"""

import pytest

from samsara.audio_engine import AudioCaptureEngine, FrameBus


@pytest.fixture
def engine():
    return AudioCaptureEngine(FrameBus())


class TestNotImplementedSeams:
    def test_start_raises_not_implemented(self, engine):
        with pytest.raises(NotImplementedError) as exc_info:
            engine.start()
        assert "ACE-02" in str(exc_info.value), (
            "NotImplementedError message must name ACE-02 so callers "
            "know where the implementation lands"
        )

    def test_stop_raises_not_implemented(self, engine):
        with pytest.raises(NotImplementedError) as exc_info:
            engine.stop()
        assert "ACE-02" in str(exc_info.value)

    def test_start_and_stop_are_distinct_stubs(self, engine):
        """Both stubs must be independently callable (not the same function)."""
        with pytest.raises(NotImplementedError):
            engine.start()
        with pytest.raises(NotImplementedError):
            engine.stop()


class TestMetricsSchema:
    """metrics() must return the full documented schema even before ACE-02."""

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

    def test_placeholder_histograms_are_zero_before_ace02(self, engine):
        """Histogram placeholders must be 0.0 until ACE-02 populates them."""
        m = engine.metrics()
        for key in ('cb_duration_p50_ms', 'cb_duration_p95_ms',
                    'cb_duration_p99_ms', 'cb_duration_max_ms'):
            assert m[key] == 0.0, (
                f"{key} should be 0.0 placeholder in ACE-01, got {m[key]}"
            )
