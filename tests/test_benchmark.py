"""Tests for the personal WER benchmark harness: samsara.benchmark_store
(sample collection) and tools/benchmark_eval.word_error_rate (pure WER
function).

Pure logic only -- no model loads, no audio hardware. benchmark_store
tests use a tmp_path SAMSARA_HOME_DIR and small synthetic float32 arrays
in place of real captured audio.
"""

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara import benchmark_store
import tools.benchmark_eval as benchmark_eval


def _make_app(collect_samples=True, max_samples=200):
    app = types.SimpleNamespace()
    app.config = {
        'benchmark': {'collect_samples': collect_samples, 'max_samples': max_samples},
    }
    return app


def _audio(seconds=0.5, sample_rate=16000):
    n = int(seconds * sample_rate)
    t = np.linspace(0, seconds, n, endpoint=False)
    return (0.1 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


@pytest.fixture(autouse=True)
def _reset_cap_notice():
    benchmark_store._cap_notice_shown = False
    yield
    benchmark_store._cap_notice_shown = False


# ============================================================================
# word_error_rate -- pure function, word-level Levenshtein
# ============================================================================

class TestWordErrorRate:
    def test_identity_is_zero(self):
        result = benchmark_eval.word_error_rate("the quick brown fox", "the quick brown fox")
        assert result == {
            'wer': 0.0, 'substitutions': 0, 'deletions': 0,
            'insertions': 0, 'ref_words': 4,
        }

    def test_both_empty_is_zero(self):
        result = benchmark_eval.word_error_rate("", "")
        assert result['wer'] == 0.0
        assert result['ref_words'] == 0

    def test_empty_reference_nonempty_hypothesis_is_one(self):
        result = benchmark_eval.word_error_rate("", "hello world")
        assert result['wer'] == 1.0
        assert result['insertions'] == 2
        assert result['ref_words'] == 0

    def test_single_substitution(self):
        result = benchmark_eval.word_error_rate("the quick brown fox", "the slow brown fox")
        assert result['substitutions'] == 1
        assert result['deletions'] == 0
        assert result['insertions'] == 0
        assert result['wer'] == pytest.approx(0.25)

    def test_single_deletion(self):
        result = benchmark_eval.word_error_rate("the quick brown fox", "the quick brown")
        assert result['deletions'] == 1
        assert result['substitutions'] == 0
        assert result['insertions'] == 0
        assert result['wer'] == pytest.approx(0.25)

    def test_single_insertion(self):
        result = benchmark_eval.word_error_rate(
            "the quick brown fox", "the quick brown fox jumps",
        )
        assert result['insertions'] == 1
        assert result['substitutions'] == 0
        assert result['deletions'] == 0
        assert result['wer'] == pytest.approx(0.25)

    def test_deletion_and_insertion_combined(self):
        result = benchmark_eval.word_error_rate("i like new york", "i new york city")
        assert result['deletions'] == 1
        assert result['insertions'] == 1
        assert result['substitutions'] == 0
        assert result['wer'] == pytest.approx(0.5)

    def test_substitution_and_insertion_combined(self):
        result = benchmark_eval.word_error_rate("a b c d", "a x c d e")
        assert result['substitutions'] == 1
        assert result['insertions'] == 1
        assert result['deletions'] == 0
        assert result['wer'] == pytest.approx(0.5)

    def test_completely_different_same_length(self):
        result = benchmark_eval.word_error_rate("a b c", "x y z")
        assert result['substitutions'] == 3
        assert result['wer'] == pytest.approx(1.0)


# ============================================================================
# benchmark_store.append_sample -- round-trip, gating, cap
# ============================================================================

class TestAppendSampleRoundTrip:
    def test_append_then_list_returns_the_row(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()

        ok = benchmark_store.append_sample(
            app, _audio(), 16000, "raw text", "final text", "small",
        )

        assert ok is True
        rows = benchmark_store.list_samples()
        assert len(rows) == 1
        row = rows[0]
        assert row['raw_transcript'] == "raw text"
        assert row['final_text'] == "final text"
        assert row['model'] == "small"
        assert row['gold'] is None
        assert row['duration_s'] == pytest.approx(0.5, abs=0.01)
        assert benchmark_store.audio_path(row).exists()

    def test_multiple_samples_all_persisted(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()

        for i in range(3):
            benchmark_store.append_sample(app, _audio(), 16000, f"raw{i}", f"final{i}", "small")

        rows = benchmark_store.list_samples()
        assert len(rows) == 3
        assert [r['raw_transcript'] for r in rows] == ["raw0", "raw1", "raw2"]
        # Each sample gets a distinct wav file.
        assert len({r['wav'] for r in rows}) == 3


class TestCollectionDisabled:
    def test_disabled_writes_nothing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app(collect_samples=False)

        ok = benchmark_store.append_sample(app, _audio(), 16000, "raw", "final", "small")

        assert ok is False
        assert benchmark_store.list_samples() == []
        assert not benchmark_store.audio_dir().exists()

    def test_missing_benchmark_config_defaults_to_disabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = types.SimpleNamespace(config={})

        ok = benchmark_store.append_sample(app, _audio(), 16000, "raw", "final", "small")

        assert ok is False
        assert benchmark_store.list_samples() == []


class TestSampleCap:
    def test_cap_stops_collection_silently(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app(max_samples=2)

        results = [
            benchmark_store.append_sample(app, _audio(), 16000, f"raw{i}", f"final{i}", "small")
            for i in range(3)
        ]

        assert results == [True, True, False]
        assert len(benchmark_store.list_samples()) == 2

    def test_cap_notice_logged_once(self, monkeypatch, tmp_path, caplog):
        import logging
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app(max_samples=1)

        benchmark_store.append_sample(app, _audio(), 16000, "raw0", "final0", "small")
        with caplog.at_level(logging.INFO):
            benchmark_store.append_sample(app, _audio(), 16000, "raw1", "final1", "small")
            benchmark_store.append_sample(app, _audio(), 16000, "raw2", "final2", "small")

        cap_messages = [r for r in caplog.records if "Sample cap reached" in r.message]
        assert len(cap_messages) == 1


# ============================================================================
# set_gold / discard_sample / stats
# ============================================================================

class TestGoldSetting:
    def test_set_gold_updates_row(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()
        benchmark_store.append_sample(app, _audio(), 16000, "raw", "final", "small")
        sample_id = benchmark_store.list_samples()[0]['id']

        ok = benchmark_store.set_gold(sample_id, "corrected gold text")

        assert ok is True
        row = benchmark_store.list_samples()[0]
        assert row['gold'] == "corrected gold text"

    def test_set_gold_unknown_id_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert benchmark_store.set_gold("nonexistent", "text") is False

    def test_set_gold_none_clears_it(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()
        benchmark_store.append_sample(app, _audio(), 16000, "raw", "final", "small")
        sample_id = benchmark_store.list_samples()[0]['id']
        benchmark_store.set_gold(sample_id, "gold text")

        benchmark_store.set_gold(sample_id, None)

        assert benchmark_store.list_samples()[0]['gold'] is None


class TestDiscardSample:
    def test_discard_removes_row_and_wav(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()
        benchmark_store.append_sample(app, _audio(), 16000, "raw", "final", "small")
        row = benchmark_store.list_samples()[0]
        wav_path = benchmark_store.audio_path(row)
        assert wav_path.exists()

        ok = benchmark_store.discard_sample(row['id'])

        assert ok is True
        assert benchmark_store.list_samples() == []
        assert not wav_path.exists()

    def test_discard_unknown_id_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert benchmark_store.discard_sample("nonexistent") is False


class TestStats:
    def test_stats_counts(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        app = _make_app()
        for i in range(3):
            benchmark_store.append_sample(app, _audio(), 16000, f"raw{i}", f"final{i}", "small")
        rows = benchmark_store.list_samples()
        benchmark_store.set_gold(rows[0]['id'], "gold0")

        st = benchmark_store.stats()

        assert st == {'total': 3, 'gold_confirmed': 1, 'pending_review': 2}

    def test_stats_empty_store(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SAMSARA_HOME_DIR", str(tmp_path))
        assert benchmark_store.stats() == {'total': 0, 'gold_confirmed': 0, 'pending_review': 0}
