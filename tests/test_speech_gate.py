"""Hotkey presence gate over the WHOLE capture (queue 39).

Before: a capture longer than _GATE_MAX_BUFFER_S (8 s) whose overall RMS was
below _SANITY_RMS_FLOOR_DB had only its first 8 seconds scanned, so a quiet
toggle take whose speech came later was discarded ("No contiguous speech in
quiet buffer window (26.50s)", "(69.80s)"). Now every chunk is scanned and
the longest contiguous run anywhere decides. The real DictationApp methods
are bound onto a stand-in with a deterministic fake VAD; dictation is
imported inside the helper, never at module level.
"""

import logging
import threading
import time

import numpy as np
import pytest

SR = 16000
FRAME = 512
QUIET_SPEECH_AMP = 0.05      # speech that alone is -29 dBFS, but diluted by silence below -40 dBFS


class _EnergyVAD:
    """One probability per 512-sample frame: 0.9 where the frame RMS says
    'speech', 0.05 otherwise. Counts calls and the samples each call saw."""

    def __init__(self, threshold=0.02):
        self.threshold = threshold
        self.calls = []

    def __call__(self, audio):
        audio = np.asarray(audio, dtype=np.float32)
        self.calls.append(len(audio))
        frames = audio.reshape(-1, FRAME)
        rms = np.sqrt(np.mean(frames ** 2, axis=1))
        return np.where(rms > self.threshold, 0.9, 0.05).astype(np.float32)


class _CountingLock:
    def __init__(self):
        self._lock = threading.Lock()
        self.acquired = 0

    def __enter__(self):
        self._lock.acquire()
        self.acquired += 1
        return self

    def __exit__(self, *exc):
        self._lock.release()


@pytest.fixture(scope="module")
def d():
    import dictation
    return dictation


def _gate(d, vad=None, *, vad_available=True):
    names = ("_buffer_should_skip_decode", "_gate_scan", "_speech_run_scan", "_buffer_has_contiguous_speech",
             "_zcr_speech_run", "_zcr_energy_contiguous_speech", "_vad_probabilities")

    class _Stub:
        pass

    for name in names:
        setattr(_Stub, name, getattr(d.DictationApp, name))
    stub = _Stub()
    stub._vad_available = vad_available
    stub._vad_model = vad if vad is not None else _EnergyVAD()
    stub._vad_lock = _CountingLock()
    return stub


def _silence(seconds, noise=0.0005, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * SR)) * noise).astype(np.float32)


def _with_speech(total_s, spans, amp=QUIET_SPEECH_AMP):
    audio = _silence(total_s)
    for start, end in spans:
        t = np.arange(int((end - start) * SR)) / SR
        a, b = int(start * SR), int(start * SR) + len(t)
        audio[a:b] = amp * np.sin(2 * np.pi * 220 * t)
    return audio


def _rms_db(audio):
    return 20 * np.log10(np.sqrt(np.mean(audio.astype(np.float64) ** 2)) + 1e-12)


# ---------------------------------------------------------------------------
# The owner's cases: quiet long toggle takes
# ---------------------------------------------------------------------------

def test_speech_in_first_two_seconds_of_a_70s_capture_passes(d, caplog):
    audio = _with_speech(70, [(0.3, 2.0)])
    assert _rms_db(audio) < d._SANITY_RMS_FLOOR_DB           # quiet overall: the chunked path
    stub = _gate(d)
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        decision = stub._buffer_should_skip_decode(audio, SR, head_grace_ms=180)
    assert not decision and decision.path == "chunked"
    assert decision.best_ms >= 1500 and decision.offset_s == pytest.approx(0.3, abs=0.04)
    assert decision.scanned_s == pytest.approx(70.0, abs=0.05) and decision.chunks == 9


@pytest.mark.parametrize("label, spans, where", [
    ("middle", [(33.0, 35.0)], 33.0),
    ("end", [(67.5, 69.6)], 67.5),
    ("just past the old 8 s window", [(9.0, 10.0)], 9.0),
])
def test_speech_only_later_in_a_70s_capture_passes(d, label, spans, where):
    audio = _with_speech(70, spans)
    assert _rms_db(audio) < d._SANITY_RMS_FLOOR_DB
    decision = _gate(d)._buffer_should_skip_decode(audio, SR, head_grace_ms=180)
    assert not decision, label
    assert decision.offset_s == pytest.approx(where, abs=0.04)


def test_the_old_prefix_window_would_have_discarded_the_middle_case(d):
    """Regression witness: the first 8 seconds alone hold no speech."""
    audio = _with_speech(70, [(33.0, 35.0)])
    assert not _gate(d)._buffer_has_contiguous_speech(audio[:8 * SR], SR, head_grace_ms=180)


def test_run_that_straddles_a_chunk_boundary_is_counted_once(d):
    # 8 s chunk boundary at exactly 8.000 s; a 160 ms run half each side.
    audio = _with_speech(20, [(7.92, 8.08)])
    stub = _gate(d)
    decision = stub._buffer_should_skip_decode(audio, SR)
    assert not decision and decision.best_ms >= d._GATE_MIN_CONTIG_MS
    assert decision.chunks == 3


# ---------------------------------------------------------------------------
# Silence is still skipped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds, path", [(3, "short"), (30, "chunked"), (70, "chunked")])
def test_all_silence_is_skipped(d, seconds, path, caplog):
    audio = _silence(seconds)
    decision = _gate(d)._buffer_should_skip_decode(audio, SR, head_grace_ms=180)
    assert decision and decision.path == path and decision.best_ms == 0
    assert decision.scanned_s == pytest.approx(seconds, abs=0.05)
    line = decision.describe()
    assert line.startswith("[GATE] skip: no speech frames found < 150ms threshold")
    assert f"buffer {seconds:.2f}s" in line and "150ms threshold" in line
    assert "no contiguous speech anywhere in the capture" in line


def test_a_run_shorter_than_the_threshold_is_still_skipped(d):
    audio = _with_speech(30, [(12.0, 12.1)])                  # 100 ms < 150 ms
    decision = _gate(d)._buffer_should_skip_decode(audio, SR)
    assert decision and 0 < decision.best_ms < d._GATE_MIN_CONTIG_MS
    assert decision.offset_s == pytest.approx(12.0, abs=0.04)


# ---------------------------------------------------------------------------
# Paths preserved: short fast path, loud long captures
# ---------------------------------------------------------------------------

def _old_short_path_skip(stub, audio, grace, d):
    """The pre-39 short-buffer decision, verbatim in algorithm: one VAD call,
    longest contiguous run with head grace, skip if below the threshold."""
    frame_ms = 512 / 16000 * 1000.0
    min_frames = max(1, int(d._GATE_MIN_CONTIG_MS / frame_ms))
    grace_frames = max(0, int(round(grace / frame_ms)))
    with stub._vad_lock:
        probs = stub._vad_probabilities(np.asarray(audio, dtype=np.float32))
    contig = best = 0
    for idx, p in enumerate(probs):
        if p > d._GATE_VAD_PROB:
            contig += 1
            best = max(best, contig)
        elif idx < grace_frames:
            pass
        else:
            contig = 0
    return not (best >= min_frames)


@pytest.mark.parametrize("seconds, spans", [
    (1.0, []), (3.0, [(1.0, 1.5)]), (5.1, [(0.0, 0.1)]), (8.0, [(7.5, 7.8)]), (6.0, [(0.05, 0.12), (3.0, 3.2)]),
])
@pytest.mark.parametrize("grace", [0, 180])
def test_short_capture_fast_path_decides_exactly_as_before(d, seconds, spans, grace):
    audio = _with_speech(seconds, spans)
    new = _gate(d)
    decision = new._buffer_should_skip_decode(audio, SR, head_grace_ms=grace)
    old = _gate(d)
    old_skip = not old._buffer_has_contiguous_speech(audio, SR, min_ms=d._GATE_MIN_CONTIG_MS,
                                                     prob_threshold=d._GATE_VAD_PROB, head_grace_ms=grace)
    reference = _gate(d)
    assert bool(decision) is old_skip is _old_short_path_skip(reference, audio, grace, d)
    assert decision.path == "short"
    assert new._vad_model.calls == old._vad_model.calls == [len(audio) // FRAME * FRAME]   # ONE call, whole buffer
    assert new._vad_lock.acquired == 1


def test_eight_second_threshold_is_unchanged(d):
    assert d._GATE_MAX_BUFFER_S == 8.0 and d._GATE_MIN_CONTIG_MS == 150
    exactly_eight = _silence(8.0)
    assert _gate(d)._buffer_should_skip_decode(exactly_eight, SR).path == "short"
    assert _gate(d)._buffer_should_skip_decode(_silence(8.05), SR).path == "chunked"


def test_loud_long_capture_decodes_without_touching_vad(d, caplog):
    audio = _with_speech(30, [(10.0, 30.0)], amp=0.2)
    assert _rms_db(audio) >= d._SANITY_RMS_FLOOR_DB
    stub = _gate(d)
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        decision = stub._buffer_should_skip_decode(audio, SR)
    assert not decision and decision.path == "loud"
    assert stub._vad_model.calls == [] and stub._vad_lock.acquired == 0
    assert "audible long capture, decoded without VAD" in caplog.text and "rms" in caplog.text


def test_chunked_scan_takes_the_vad_lock_per_chunk_not_for_the_whole_capture(d):
    audio = _silence(70)
    stub = _gate(d)
    stub._buffer_should_skip_decode(audio, SR)
    chunk = int(d._GATE_MAX_BUFFER_S * SR) // FRAME * FRAME
    assert stub._vad_lock.acquired == 9
    assert max(stub._vad_model.calls) == chunk
    assert sum(stub._vad_model.calls) == len(audio) // FRAME * FRAME


def test_head_grace_applies_only_at_the_head_of_the_capture(d):
    audio = _with_speech(30, [(12.0, 12.08), (12.12, 12.2)])   # 40 ms dip inside a later run
    grace = _gate(d)._buffer_should_skip_decode(audio, SR, head_grace_ms=180)
    assert grace and grace.best_ms < d._GATE_MIN_CONTIG_MS     # the dip breaks it: no grace mid-capture


# ---------------------------------------------------------------------------
# Logging: checkable numbers
# ---------------------------------------------------------------------------

def test_pass_logs_the_offset_where_speech_was_found(d, caplog):
    audio = _with_speech(70, [(33.0, 35.0)])
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        _gate(d)._buffer_should_skip_decode(audio, SR, head_grace_ms=180)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[GATE] pass")]
    import re
    offset = float(re.search(r"longest speech run \d+ms at ([\d.]+)s", line).group(1))
    assert offset == pytest.approx(33.0, abs=0.04)
    assert "buffer 70.00s" in line and re.search(r"scanned 69\.9\ds in 9 chunks", line)
    assert ">= 150ms threshold" in line and "path chunked, rms" in line and "head_grace=180ms" in line


def test_short_path_pass_logs_offset_too(d, caplog):
    audio = _with_speech(4, [(2.5, 3.0)])
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        assert not _gate(d)._buffer_should_skip_decode(audio, SR)
    assert any("at 2.4" in r.getMessage() or "at 2.5" in r.getMessage() for r in caplog.records
               if "path short" in r.getMessage())


def test_buffer_has_contiguous_speech_pass_line_carries_the_offset(d, caplog):
    audio = _with_speech(5, [(3.2, 3.6)])
    with caplog.at_level(logging.DEBUG, logger="Samsara"):
        assert _gate(d)._buffer_has_contiguous_speech(audio, SR)
    assert any("max contiguous speech" in r.getMessage() and "at 3.20s" in r.getMessage()
               for r in caplog.records)


def test_call_site_logs_the_decision_line_at_info(d):
    import inspect
    src = inspect.getsource(d.DictationApp)
    assert "logger.info(_gate.describe()" in src
    assert "No contiguous speech in quiet buffer window" not in src


# ---------------------------------------------------------------------------
# Fallbacks keep their guarantees over the whole capture
# ---------------------------------------------------------------------------

def test_zcr_fallback_scans_the_whole_capture(d):
    audio = _with_speech(30, [(20.0, 21.0)], amp=0.3)
    audio[: 10 * SR] *= 0.1
    stub = _gate(d, vad_available=False)
    run = stub._speech_run_scan(audio, SR)
    assert run.method == "zcr" and run.passed and run.offset_s == pytest.approx(20.0, abs=0.1)


def test_vad_failure_falls_back_and_never_raises(d):
    class _Broken:
        def __call__(self, audio):
            raise RuntimeError("onnx went away")

    audio = _with_speech(30, [(20.0, 21.0)])
    decision = _gate(d, _Broken())._buffer_should_skip_decode(audio, SR)
    assert decision.method == "zcr"


def test_scan_cost_on_a_70s_capture_is_bounded(d):
    audio = _silence(70)
    stub = _gate(d)
    t0 = time.perf_counter()
    stub._buffer_should_skip_decode(audio, SR)
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0                                      # fake VAD: the scan loop itself
