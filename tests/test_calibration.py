"""Tests for samsara.calibration -- IQR-based threshold calibration."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.calibration import calibrate_threshold


class TestCalibrateThreshold:
    def test_normal_ambient(self):
        """Typical quiet room: ambient ~0.005 RMS."""
        samples = [0.005] * 15
        thresh = calibrate_threshold(samples, multiplier=3.0)
        assert 0.014 < thresh < 0.016  # 0.005 * 3 = 0.015

    def test_noisy_room(self):
        """Noisy environment: ambient ~0.02."""
        samples = [0.02] * 15
        thresh = calibrate_threshold(samples, multiplier=3.0)
        assert 0.055 < thresh < 0.065  # 0.02 * 3 = 0.06

    def test_outlier_rejection(self):
        """Spike (cough) should be rejected by IQR fence."""
        samples = [0.005] * 14 + [0.5]  # one loud spike
        thresh = calibrate_threshold(samples, multiplier=3.0)
        # Should be close to 0.015, not pulled up by the spike
        assert thresh < 0.03

    def test_floor_enforced(self):
        """Pure silence should hit the floor, not return 0."""
        samples = [0.0001] * 15
        thresh = calibrate_threshold(samples, multiplier=3.0, floor=0.0005)
        assert thresh == 0.0005

    def test_ceiling_enforced(self):
        """Extremely loud ambient should be capped."""
        samples = [0.1] * 15
        thresh = calibrate_threshold(samples, multiplier=3.0, ceiling=0.15)
        assert thresh == 0.15

    def test_too_few_samples(self):
        """Fewer than 3 samples should return default."""
        assert calibrate_threshold([0.01, 0.02]) == 0.03
        assert calibrate_threshold([]) == 0.03

    def test_custom_multiplier(self):
        """Higher multiplier = less sensitive."""
        samples = [0.01] * 15
        t3 = calibrate_threshold(samples, multiplier=3.0)
        t5 = calibrate_threshold(samples, multiplier=5.0)
        assert t5 > t3

    def test_floor_not_001(self):
        """Critical: floor must NOT be 0.01 (the old bug)."""
        samples = [0.002] * 15
        thresh = calibrate_threshold(samples, multiplier=3.0, floor=0.0005)
        # 0.002 * 3 = 0.006 -- should NOT be clamped to 0.01
        assert thresh == pytest.approx(0.006, abs=0.001)
