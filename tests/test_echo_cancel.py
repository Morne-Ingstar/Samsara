"""Tests for samsara.echo_cancel's disabled-by-default bypass (2026-07-10).

The homegrown NLMS AEC filter converges to only 3-8% echo reduction and
adversarial review concluded it's likely net-negative (see
config_schema.py's echo_cancellation.enabled comment) -- retired to
default-off pending WebRTC AEC3 / OS-mode evaluation. These tests prove
disabling it is a TRUE bypass: no loopback (reference-stream) capture
starts, and the adaptive filter is never invoked -- not just "returns the
same values via extra processing," but the processing path itself is
skipped entirely. Enabled behavior is verified unchanged.
"""
from unittest.mock import patch, PropertyMock

import numpy as np
import pytest

from samsara.echo_cancel import EchoCanceller, LoopbackCapture, AdaptiveEchoCanceller


class TestDisabledIsATrueBypass:
    def test_is_active_false_when_disabled(self):
        ec = EchoCanceller(enabled=False)
        assert ec.is_active is False

    def test_start_never_touches_loopback_capture(self):
        ec = EchoCanceller(enabled=False)
        with patch.object(LoopbackCapture, "start") as mock_loopback_start:
            result = ec.start()
        mock_loopback_start.assert_not_called()
        assert result is False
        assert ec._started is False

    def test_process_returns_input_unchanged(self):
        ec = EchoCanceller(enabled=False)
        mic_audio = np.random.default_rng(0).uniform(-1, 1, 1024).astype(np.float32)
        result = ec.process(mic_audio)
        assert result is mic_audio  # same object -- true passthrough, not a copy

    def test_process_never_invokes_the_adaptive_filter(self):
        """The actual NLMS filter (the thing under review as net-negative)
        must never run at all when disabled -- not run-and-discard."""
        ec = EchoCanceller(enabled=False)
        mic_audio = np.zeros(1024, dtype=np.float32)
        with patch.object(AdaptiveEchoCanceller, "process") as mock_filter_process:
            ec.process(mic_audio)
        mock_filter_process.assert_not_called()

    def test_process_never_pulls_loopback_reference_audio(self):
        """Disabled AEC must not even query the reference buffer -- no
        get_recent() call, confirming no reference-stream dependency."""
        ec = EchoCanceller(enabled=False)
        mic_audio = np.zeros(1024, dtype=np.float32)
        with patch.object(LoopbackCapture, "get_recent") as mock_get_recent:
            ec.process(mic_audio)
        mock_get_recent.assert_not_called()

    def test_constructor_default_matches_config_schema_default(self):
        """EchoCanceller's own constructor default must agree with
        config_schema.py's echo_cancellation.enabled default (False) --
        callers that forget to pass `enabled` explicitly should still get
        the safe, retired-by-default behavior."""
        # NOTE: dictation.py's actual construction site always passes
        # enabled= explicitly from config (with its own False fallback);
        # this guards the class's OWN default in isolation.
        ec = EchoCanceller()
        assert ec.enabled is False


class TestEnabledBehaviorUnchanged:
    def test_start_attempts_loopback_capture(self):
        ec = EchoCanceller(enabled=True)
        with patch.object(LoopbackCapture, "start", return_value=True) as mock_start:
            with patch.object(LoopbackCapture, "is_running", new_callable=PropertyMock) as mock_running:
                mock_running.return_value = True
                result = ec.start()
                assert ec.is_active is True  # must check while is_running is still patched
        mock_start.assert_called_once()
        assert result is True

    def test_process_invokes_the_adaptive_filter_when_active(self):
        ec = EchoCanceller(enabled=True)
        mic_audio = np.random.default_rng(1).uniform(-1, 1, 1024).astype(np.float32)
        reference = np.random.default_rng(2).uniform(-1, 1, 4096).astype(np.float32)

        with patch.object(LoopbackCapture, "is_running", new_callable=PropertyMock) as mock_running, \
             patch.object(LoopbackCapture, "get_recent", return_value=reference) as mock_get_recent, \
             patch.object(AdaptiveEchoCanceller, "process", return_value=mic_audio.copy()) as mock_filter_process:
            mock_running.return_value = True
            ec._started = True  # simulate a prior successful start()
            ec.process(mic_audio)

        mock_get_recent.assert_called_once()
        mock_filter_process.assert_called_once()

    def test_process_skips_filter_on_silent_reference(self):
        """Existing behavior, unchanged: even when active, a silent
        reference (no system audio playing) short-circuits before the
        filter -- nothing to cancel."""
        ec = EchoCanceller(enabled=True)
        mic_audio = np.random.default_rng(3).uniform(-1, 1, 1024).astype(np.float32)
        silent_reference = np.zeros(4096, dtype=np.float32)

        with patch.object(LoopbackCapture, "is_running", new_callable=PropertyMock) as mock_running, \
             patch.object(LoopbackCapture, "get_recent", return_value=silent_reference), \
             patch.object(AdaptiveEchoCanceller, "process") as mock_filter_process:
            mock_running.return_value = True
            ec._started = True
            result = ec.process(mic_audio)

        mock_filter_process.assert_not_called()
        assert result is mic_audio
