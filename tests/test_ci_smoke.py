"""Tests for tools.ci_smoke's crash/benign-traceback classification
(tools.ci_smoke.LogScanner).

2026-07-10: a real CI run failed the smoke check on a mic-less runner
because the single old benign marker ("[ACE] Engine failed to start")
didn't match the ACTUAL log lines the current audio-init code path
produces (three separate sounddevice.PortAudioError tracebacks under
different ERROR-level prefixes) -- a false positive. The SAME run also had
a genuine bug (plugins/commands/stremio.py's frozen import failure, fixed
separately). Both cases are covered here: synthetic minimal reproductions
of each pattern, plus a regression test against the real downloaded CI log
that originally surfaced both issues in one run.

2026-07-10 (later same day): a SEPARATE CPU-build release CI run on a
driverless windows-latest runner hit a 4th benign pattern -- a handled,
caught-and-continuing GPU-detection failure (dictation.py's CUDA probe
raises, is caught, falls back to CPU) that still writes a "Traceback"
block. Added as its own synthetic fixture below, same treatment as the
mic-less case.
"""
from pathlib import Path

import pytest

from tools import ci_smoke

REAL_CI_LOG = Path(
    r"C:\Users\Morne\Documents\Claude\ci_smoke_dl\ci-smoke-log-dev-f3dd59a\samsara.log"
)


def _lines(text: str) -> list:
    return text.strip("\n").splitlines()


# Minimal, hand-built reproduction of the mic-less PortAudioError pattern
# actually seen in the real CI log (one of the three -- device-rate query).
SYNTHETIC_MIC_LESS_TRACEBACK = _lines("""
2026-07-10 16:15:28,859 - ERROR - [WARN] Could not query device 0 rate: Error querying device 0
Traceback (most recent call last):
  File "dictation.py", line 2816, in _detect_capture_rate
  File "sounddevice.py", line 577, in query_devices
sounddevice.PortAudioError: Error querying device 0
2026-07-10 16:15:28,989 - INFO - [INIT] Startup complete.
""")

# Minimal reproduction of Fix 1's real bug -- a frozen import failure.
SYNTHETIC_MODULE_NOT_FOUND_TRACEBACK = _lines(r"""
2026-07-10 16:15:28,938 - ERROR - Failed to load plugin stremio.py: No module named 'stremio_control'
Traceback (most recent call last):
  File "samsara\plugin_commands.py", line 213, in load_plugins
  File "plugins\commands\stremio.py", line 29, in <module>
    import stremio_control
ModuleNotFoundError: No module named 'stremio_control'
""")

# Minimal reproduction of the CPU-build release CI pattern: a driverless
# windows-latest runner's handled-and-continuing GPU-detect fallback.
SYNTHETIC_CPU_GPU_FALLBACK_TRACEBACK = _lines("""
2026-07-10 16:15:29,102 - ERROR - [CPU] Could not detect GPU: CUDA failed with error CUDA driver version is insufficient for CUDA runtime version
Traceback (most recent call last):
  File "dictation.py", line 3482, in load
RuntimeError: CUDA failed with error CUDA driver version is insufficient for CUDA runtime version
2026-07-10 16:15:29,989 - INFO - [INIT] Startup complete.
""")


class TestLogScannerSyntheticCases:
    def test_mic_less_portaudio_traceback_is_benign(self):
        scanner = ci_smoke.LogScanner()
        scanner.feed(SYNTHETIC_MIC_LESS_TRACEBACK)
        assert scanner.unexplained_crash_line is None
        assert len(scanner.benign_seen) == 1
        assert scanner.outcome == "boot"  # scanning continued past the traceback

    def test_cpu_build_gpu_detect_fallback_traceback_is_benign(self):
        """CPU-build release CI, driverless windows-latest runner: the
        handled CUDA-detect-failure-then-fallback-to-CPU traceback must
        not fail the build."""
        scanner = ci_smoke.LogScanner()
        scanner.feed(SYNTHETIC_CPU_GPU_FALLBACK_TRACEBACK)
        assert scanner.unexplained_crash_line is None
        assert len(scanner.benign_seen) == 1
        assert scanner.outcome == "boot"  # scanning continued past the traceback

    def test_module_not_found_traceback_is_fatal(self):
        scanner = ci_smoke.LogScanner()
        scanner.feed(SYNTHETIC_MODULE_NOT_FOUND_TRACEBACK)
        assert scanner.unexplained_crash_line is not None
        assert "Traceback" in scanner.unexplained_crash_line
        assert scanner.benign_seen == []

    def test_bare_traceback_with_no_context_is_fatal(self):
        """No preceding benign-marker line at all -- must not somehow
        default to benign."""
        scanner = ci_smoke.LogScanner()
        scanner.feed(["Traceback (most recent call last):", "  File \"x.py\", line 1", "ValueError: boom"])
        assert scanner.unexplained_crash_line is not None

    def test_boot_marker_reached_with_no_traceback(self):
        scanner = ci_smoke.LogScanner()
        scanner.feed(["2026-07-10 - INFO - [INIT] Startup complete."])
        assert scanner.outcome == "boot"
        assert scanner.unexplained_crash_line is None
        assert scanner.benign_seen == []

    def test_already_running_marker(self):
        scanner = ci_smoke.LogScanner()
        scanner.feed(["2026-07-10 - INFO - Samsara is already running"])
        assert scanner.outcome == "already_running"


class TestLogScannerRealCiLog:
    """Regression fixture: the actual downloaded CI log that surfaced both
    the real ModuleNotFoundError bug (Fix 1) and the false-positive
    mic-less PortAudio tracebacks (Fix 2) in the same run."""

    @pytest.mark.skipif(not REAL_CI_LOG.exists(), reason="real CI log fixture not present on this machine")
    def test_classifies_mic_less_tracebacks_as_benign_but_still_catches_real_crash(self):
        text = REAL_CI_LOG.read_text(encoding="utf-8", errors="replace")
        scanner = ci_smoke.LogScanner()
        scanner.feed(text.splitlines())

        # 3 PortAudioError tracebacks (device-rate query, calibration,
        # sound-stream start) -- all benign on a mic-less runner.
        assert len(scanner.benign_seen) == 3
        for line in scanner.benign_seen:
            assert "Traceback" in line

        # The genuine bug (Fix 1's frozen-import ModuleNotFoundError) is
        # NOT swallowed by the broadened benign-marker list -- this is the
        # regression this fixture exists to catch: a benign list broad
        # enough to hide the mic-less noise but still narrow enough to
        # catch a real crash later in the very same log.
        assert scanner.unexplained_crash_line is not None
        assert "Traceback" in scanner.unexplained_crash_line
