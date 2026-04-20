"""Tests for samsara.phonetic_wash.apply_phonetic_wash."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from samsara.phonetic_wash import apply_phonetic_wash


class TestPhraseCorrections:
    def test_fine_tab_becomes_find_tab(self):
        assert apply_phonetic_wash("fine tab github") == "find tab github"

    def test_get_hub_becomes_github(self):
        assert apply_phonetic_wash("get hub") == "github"

    def test_use_mike_becomes_use_mic(self):
        assert apply_phonetic_wash("use mike") == "use mic"

    def test_switch_two_becomes_switch_to(self):
        assert apply_phonetic_wash("switch two speakers") == "switch to speakers"

    def test_find_the_tab_becomes_find_tab(self):
        assert apply_phonetic_wash("find the tab github") == "find tab github"


class TestPunctuationScrub:
    def test_trailing_period_stripped(self):
        assert apply_phonetic_wash("refresh page.") == "refresh page"

    def test_interior_comma_stripped(self):
        assert apply_phonetic_wash("switch to, speakers") == "switch to speakers"

    def test_question_mark_stripped(self):
        assert apply_phonetic_wash("where is slack?") == "where is slack"


class TestSymbolToWord:
    def test_period_symbol_becomes_period_word(self):
        assert apply_phonetic_wash(".") == "period"

    def test_comma_symbol_becomes_comma_word(self):
        assert apply_phonetic_wash(",") == "comma"

    def test_ellipsis_maps(self):
        assert apply_phonetic_wash("...") == "ellipsis"

    def test_question_symbol_maps(self):
        assert apply_phonetic_wash("?") == "question mark"

    def test_open_paren_maps(self):
        assert apply_phonetic_wash("(") == "open parenthesis"


class TestPassthrough:
    def test_scroll_down_unchanged(self):
        assert apply_phonetic_wash("scroll down") == "scroll down"

    def test_open_chrome_unchanged(self):
        assert apply_phonetic_wash("open chrome") == "open chrome"

    def test_empty_string_unchanged(self):
        assert apply_phonetic_wash("") == ""

    def test_none_unchanged(self):
        assert apply_phonetic_wash(None) is None


class TestWordCorrections:
    def test_fine_word_becomes_find(self):
        assert apply_phonetic_wash("fine") == "find"

    def test_mike_becomes_mic(self):
        # Standalone "mike" (not preceded by "use") maps via word correction
        assert apply_phonetic_wash("mike") == "mic"

    def test_tabs_becomes_tab(self):
        assert apply_phonetic_wash("close tabs") == "close tab"


class TestOrderingGuarantees:
    def test_phrase_wins_over_word(self):
        # "fine tab" (phrase) should produce "find tab", NOT re-corrupt
        # via word correction of "fine" -> "find" plus " tab"
        assert apply_phonetic_wash("fine tab") == "find tab"

    def test_symbol_check_before_punct_scrub(self):
        # If punctuation scrub ran first, "." would become "" and the
        # symbol mapping would never fire.
        assert apply_phonetic_wash(".") == "period"


class TestWashLogs:
    def test_correction_logs(self, capsys):
        apply_phonetic_wash("fine tab")
        out = capsys.readouterr().out
        assert "[WASH]" in out
        assert "fine tab" in out and "find tab" in out

    def test_no_change_no_log(self, capsys):
        apply_phonetic_wash("scroll down")
        out = capsys.readouterr().out
        assert "[WASH]" not in out
