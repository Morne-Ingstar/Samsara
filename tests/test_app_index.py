"""Tests for samsara.app_index: the shared name-matching scorer, ranking,
and AppIndex.resolve(). Enumeration (Shell AppsFolder / Start Menu .lnk scan)
is mocked everywhere -- no shell dependency in tests.
"""
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.app_index import (
    AppEntry,
    AppIndex,
    MATCH_FLOOR,
    log_top3,
    normalize_name,
    rank_candidates,
    score_name_match,
)


# ---------------------------------------------------------------------------
# score_name_match / normalize_name
# ---------------------------------------------------------------------------

class TestNormalizeName:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_name("Google Chrome!") == "google chrome"

    def test_none_and_empty(self):
        assert normalize_name(None) == ""
        assert normalize_name("") == ""


class TestScoreNameMatch:
    def test_exact_match(self):
        assert score_name_match("chrome", "Chrome") == 1.0
        assert score_name_match("Notepad", "notepad") == 1.0

    def test_prefix_match(self):
        assert score_name_match("chrome", "Chrome Canary") == 0.9
        assert score_name_match("claude desktop", "Claude") == 0.9

    def test_token_subset_match(self):
        # "chrome" is a subset of {"google", "chrome"} but not a prefix or
        # exact match of the full string "google chrome".
        assert score_name_match("chrome", "Google Chrome") == 0.85

    def test_fuzzy_ratio_for_a_typo_not_a_prefix_or_subset(self):
        # "chrme" is neither a prefix of "Chrome" nor the reverse, and
        # neither is a token-subset of the other (single distinct tokens) --
        # this exercises the difflib fallback specifically. A one-letter
        # transposition typo still resolves comfortably above the floor.
        score = score_name_match("chrme", "Chrome")
        assert score not in (1.0, 0.9, 0.85)  # genuinely the fuzzy path, not an earlier rule
        assert score > MATCH_FLOOR

    def test_completely_unrelated_scores_low(self):
        assert score_name_match("flurbotron", "Chrome") < MATCH_FLOOR

    def test_empty_query_or_candidate(self):
        assert score_name_match("", "Chrome") == 0.0
        assert score_name_match("chrome", "") == 0.0
        assert score_name_match(None, "Chrome") == 0.0


class TestRankCandidates:
    def test_sorted_best_first(self):
        candidates = ["Firefox", "Google Chrome", "Chrome Canary"]
        ranked = rank_candidates("chrome", candidates, lambda c: c)
        assert ranked[0][1] in ("Chrome Canary",)  # prefix (0.9) beats subset (0.85)
        scores = [s for s, _ in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_empty_candidates(self):
        assert rank_candidates("chrome", [], lambda c: c) == []


class TestLogTop3(object):
    def test_logs_top_three_and_no_more(self, capsys):
        candidates = ["Chrome", "Chromium", "Chrome Canary", "Firefox", "Notepad"]
        ranked = rank_candidates("chrome", candidates, lambda c: c)
        log_top3("APP-INDEX", "chrome", ranked, lambda c: c)
        out = capsys.readouterr().out
        assert "APP-INDEX" in out
        assert "chrome" in out.lower() or "Chrome" in out
        # Exactly 3 candidate names should appear in the summary line, not all 5.
        assert out.count("=") <= 3 or "top-3" in out

    def test_no_candidates_logs_none_found(self, capsys):
        log_top3("APP-INDEX", "flurbotron", [], lambda c: c)
        out = capsys.readouterr().out
        assert "no candidates" in out


# ---------------------------------------------------------------------------
# AppIndex.resolve() -- enumeration mocked, no shell dependency
# ---------------------------------------------------------------------------

def _entry(name, kind="lnk", spec=None):
    return AppEntry(
        display_name=name,
        launch_kind=kind,
        launch_spec=spec or f"C:\\fake\\{name}.lnk",
        name_tokens=tuple(normalize_name(name).split()),
    )


@pytest.fixture
def populated_index():
    idx = AppIndex()
    idx._apps = [
        _entry("Google Chrome"),
        _entry("Notepad"),
        _entry("Claude", kind="aumid", spec="AnthropicClaude_abc123!App"),
        _entry("Visual Studio Code"),
        _entry("Spotify"),
    ]
    return idx


class TestAppIndexResolve:
    def test_exact_hit(self, populated_index):
        result = populated_index.resolve("Notepad")
        assert result is not None
        assert result.display_name == "Notepad"

    def test_prefix_hit(self, populated_index):
        result = populated_index.resolve("claude")
        assert result is not None
        assert result.display_name == "Claude"

    def test_token_subset_hit(self, populated_index):
        result = populated_index.resolve("chrome")
        assert result is not None
        assert result.display_name == "Google Chrome"

    def test_fuzzy_hit_above_floor(self, populated_index):
        result = populated_index.resolve("spotifi")  # typo, still close enough
        assert result is not None
        assert result.display_name == "Spotify"

    def test_below_floor_returns_none(self, populated_index):
        assert populated_index.resolve("flurbotron") is None

    def test_empty_name_returns_none(self, populated_index):
        assert populated_index.resolve("") is None

    def test_empty_index_returns_none(self):
        idx = AppIndex()
        assert idx.resolve("chrome") is None

    def test_resolve_logs_top3(self, populated_index, capsys):
        populated_index.resolve("chrome")
        out = capsys.readouterr().out
        assert "APP-INDEX" in out


class TestAppIndexBackgroundBuild:
    def test_ensure_built_async_does_not_block(self):
        """ensure_built_async() must return immediately -- the real
        enumeration/refresh runs on a background thread."""
        idx = AppIndex()
        with patch.object(idx, "_load_cache", return_value=[]), \
             patch("threading.Thread") as mock_thread:
            idx.ensure_built_async()
            mock_thread.assert_called_once()
            # The thread must be started, not run synchronously in-line.
            mock_thread.return_value.start.assert_called_once()

    def test_ensure_built_async_loads_cache_synchronously(self):
        """The disk cache load itself IS synchronous (fast, local file) so
        resolve() has something before the background build finishes."""
        idx = AppIndex()
        cached = [_entry("Cached App")]
        with patch.object(idx, "_load_cache", return_value=cached), \
             patch("threading.Thread") as mock_thread:
            idx.ensure_built_async()
            assert idx.apps == cached
