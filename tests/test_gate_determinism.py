"""ONNX VAD whole-buffer inference, lock scope, and head-grace regressions."""
import threading

import numpy as np

import dictation as _d


class _VectorFakeVAD:
    """Return one probability for each 512-sample NumPy frame."""

    def __init__(self):
        self.call_count = 0

    def __call__(self, audio):
        self.call_count += 1
        means = np.asarray(audio).reshape(-1, 512).mean(axis=1)
        return np.where(means > 0.5, 0.9, 0.1).astype(np.float32)


class _FixedProbVAD:
    def __init__(self, probs):
        self._probs = np.asarray(probs, dtype=np.float32)
        self.call_count = 0

    def __call__(self, audio):
        self.call_count += 1
        return self._probs.copy()


def _make_stub(vad_model=None):
    class _Stub:
        _vad_probabilities = _d.DictationApp._vad_probabilities
        _buffer_has_contiguous_speech = _d.DictationApp._buffer_has_contiguous_speech
        _zcr_energy_contiguous_speech = _d.DictationApp._zcr_energy_contiguous_speech

        def __init__(self):
            self._vad_available = True
            self._vad_model = vad_model if vad_model is not None else _VectorFakeVAD()
            self._vad_lock = threading.Lock()

    return _Stub()


def _window(value, n=512):
    return np.full(n, value, dtype=np.float32)


def _all_speech_buffer(n_windows=20):
    """20 consecutive "speech-like" windows at 16kHz (already matching the
    gate's internal target rate, so resample_audio short-circuits)."""
    return np.concatenate([_window(1.0) for _ in range(n_windows)])


class TestGateDeterminismUnderInterleavedForeignCalls:
    def test_two_scans_with_no_interference_agree(self):
        stub = _make_stub()
        buf = _all_speech_buffer()
        r1 = stub._buffer_has_contiguous_speech(buf, 16000, min_ms=1, prob_threshold=0.45)
        r2 = stub._buffer_has_contiguous_speech(buf, 16000, min_ms=1, prob_threshold=0.45)
        assert r1 == r2

    def test_scan_result_unchanged_while_a_foreign_thread_hammers_the_lock(self):
        """Wake and gate inference serialize through the same model lock."""
        stub = _make_stub()
        buf = _all_speech_buffer()

        baseline = stub._buffer_has_contiguous_speech(buf, 16000, min_ms=1, prob_threshold=0.45)
        assert stub._vad_model.call_count == 1

        stop = threading.Event()
        foreign_calls = {"n": 0}

        def _foreign_hammer():
            dummy = np.zeros(512, dtype=np.float32)
            while not stop.is_set():
                with stub._vad_lock:
                    stub._vad_model(dummy)
                    foreign_calls["n"] += 1

        t = threading.Thread(target=_foreign_hammer, daemon=True)
        t.start()
        try:
            for _ in range(20):
                result = stub._buffer_has_contiguous_speech(buf, 16000, min_ms=1, prob_threshold=0.45)
                assert result == baseline, (
                    "gate result changed under concurrent foreign VAD-lock "
                    "contention -- scan is not atomic"
                )
        finally:
            stop.set()
            t.join(timeout=2.0)
        assert foreign_calls["n"] > 0, "test didn't actually exercise contention"

    def test_head_grace_low_reading_in_grace_span_does_not_reset_contig(self):
        """Direct unit check of the grace bookkeeping itself, independent
        of threading: a low-probability window inside head_grace_ms must
        not zero out an in-progress contiguous run."""
        # 3 windows of good speech, then 1 low ("transient" inside grace),
        # then 3 more good speech windows. Without grace, the low window
        # resets contig, capping best_contig at 3 (96ms @ 32ms/window).
        # With a grace span covering that 4th window, the run should be
        # measured as unbroken (7 windows, ~224ms).
        probs = [0.9, 0.9, 0.9, 0.1, 0.9, 0.9, 0.9]
        stub = _make_stub(vad_model=_FixedProbVAD(probs))
        buf = np.concatenate([_window(1.0) for _ in range(len(probs))])

        frame_ms = 512 / 16000 * 1000.0  # 32ms
        grace_ms = frame_ms * 4  # covers the first 4 windows generously

        passed = stub._buffer_has_contiguous_speech(
            buf, 16000, min_ms=int(frame_ms * 6), prob_threshold=0.45,
            head_grace_ms=grace_ms,
        )
        assert passed is True

    def test_no_head_grace_low_reading_breaks_contig_as_before(self):
        """Regression guard: head_grace_ms defaults to 0 -- existing
        callers (session-mode switch gate) see unchanged behavior."""
        probs = [0.9, 0.9, 0.9, 0.1, 0.9, 0.9, 0.9]
        stub = _make_stub(vad_model=_FixedProbVAD(probs))
        buf = np.concatenate([_window(1.0) for _ in range(len(probs))])
        frame_ms = 512 / 16000 * 1000.0

        passed = stub._buffer_has_contiguous_speech(
            buf, 16000, min_ms=int(frame_ms * 6), prob_threshold=0.45,
        )
        assert passed is False  # best contiguous run is only 3 windows without grace


class TestHeadGraceTransientThenCleanSpeech:
    """TEST spec, verbatim: 'buffer with 150ms transient noise then clean
    speech passes.'"""

    def test_150ms_transient_then_clean_speech_passes(self):
        frame_ms = 512 / 16000 * 1000.0  # 32ms/window
        transient_windows = max(1, round(150.0 / frame_ms))  # ~5 windows @ 32ms
        clean_windows = 40  # ~1.28s of clean speech -- comfortably long

        probs = [0.05] * transient_windows + [0.9] * clean_windows
        stub = _make_stub(vad_model=_FixedProbVAD(probs))
        buf = np.concatenate([
            _window(1.0) for _ in range(transient_windows + clean_windows)
        ])

        passed = stub._buffer_has_contiguous_speech(
            buf, 16000, min_ms=_d._GATE_MIN_CONTIG_MS, prob_threshold=_d._GATE_VAD_PROB,
            head_grace_ms=150.0,
        )
        assert passed is True

    def test_same_buffer_without_grace_still_passes_on_clean_tail_alone(self):
        """Sanity check: the clean speech alone (well beyond min_ms) would
        pass even without grace -- grace's value shows up in the
        near-boundary cases covered above, not as a strict requirement for
        this generously-long buffer."""
        frame_ms = 512 / 16000 * 1000.0
        transient_windows = max(1, round(150.0 / frame_ms))
        clean_windows = 40

        probs = [0.05] * transient_windows + [0.9] * clean_windows
        stub = _make_stub(vad_model=_FixedProbVAD(probs))
        buf = np.concatenate([
            _window(1.0) for _ in range(transient_windows + clean_windows)
        ])
        passed = stub._buffer_has_contiguous_speech(
            buf, 16000, min_ms=_d._GATE_MIN_CONTIG_MS, prob_threshold=_d._GATE_VAD_PROB,
        )
        assert passed is True
