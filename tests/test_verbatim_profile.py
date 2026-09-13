"""VERBATIM dictation profile -- the transform, the tables, and the WHEN rules.

Reproduces the three live failures from 2026-09-11 (Warp and the browser
address bar), then covers every symbol in the map, the whitespace rule,
spell/cap, contextual digits, the process list, address-bar detection and
toggle precedence.

No module-level `import dictation`: the pure profile lives in
samsara/verbatim.py and needs nothing from the app, and the two tests that
exercise the app-side precedence bind the real DictationApp methods onto a
stub inside the helper that builds it.
"""
import types

import pytest

from samsara import verbatim


# ---------------------------------------------------------------------------
# The three live failures
# ---------------------------------------------------------------------------

class TestLiveFailures:
    def test_warp_echo_hello(self):
        """Injected as "Echo, hello?" -- auto-capital plus invented comma and
        question mark."""
        assert verbatim.apply("Echo, hello?") == "echo hello"

    def test_warp_git_log(self):
        """Injected as "Git log dash dash 1 line dash 5." -- spoken symbols
        never mapped, and number formatting turned "one" into "1"."""
        assert verbatim.apply("Git log dash dash oneline dash five.") == "git log --oneline -5"

    def test_browser_address_bar_url(self):
        """Injected as "Github. com." -- whitespace around the dot, plus a
        trailing sentence period. Whisper renders the spoken "dot" as "."."""
        assert verbatim.apply("Github. com.") == "github.com"

    def test_url_when_whisper_keeps_the_word_dot(self):
        assert verbatim.apply("github dot com") == "github.com"


# ---------------------------------------------------------------------------
# Every symbol in the map
# ---------------------------------------------------------------------------

class TestSymbolMap:
    @pytest.mark.parametrize("spoken,char", sorted(verbatim.SYMBOLS.items()))
    def test_every_single_word_symbol(self, spoken, char):
        assert verbatim.apply(f"alpha {spoken} beta").startswith("alpha")
        assert char in verbatim.apply(f"alpha {spoken} beta")

    @pytest.mark.parametrize("phrase,char", sorted(verbatim.PHRASE_SYMBOLS.items()))
    def test_every_phrase_symbol(self, phrase, char):
        assert char in verbatim.apply(f"alpha {phrase} beta")

    def test_symbols_are_case_insensitive(self):
        assert verbatim.apply("alpha DASH beta") == verbatim.apply("alpha dash beta")

    def test_phrase_beats_two_single_words(self):
        assert verbatim.apply("run dash dash watch") == "run --watch"
        assert verbatim.apply("run double dash watch") == "run --watch"

    def test_at_sign_beats_bare_at(self):
        assert verbatim.apply("user at sign host") == "user@host"


# ---------------------------------------------------------------------------
# Whitespace rule
# ---------------------------------------------------------------------------

class TestWhitespaceRule:
    @pytest.mark.parametrize("spoken,expected", [
        ("a dot b", "a.b"),
        ("a slash b", "a/b"),
        ("a backslash b", "a\\b"),
        ("a underscore b", "a_b"),
        ("a colon b", "a:b"),
        ("a at sign b", "a@b"),
    ])
    def test_no_space_either_side(self, spoken, expected):
        assert verbatim.apply(spoken) == expected

    def test_ordinary_words_get_one_space(self):
        assert verbatim.apply("alpha beta gamma") == "alpha beta gamma"

    def test_dash_run_starts_a_new_flag_after_a_word(self):
        """"oneline -5", not "oneline-5" -- in a terminal a dash after a word
        is overwhelmingly a flag."""
        assert verbatim.apply("ls dash la") == "ls -la"
        assert verbatim.apply("python dash m pytest") == "python -m pytest"

    def test_brackets_hug_their_contents(self):
        assert verbatim.apply("open paren x close paren") == "(x)"

    def test_sigils_bind_to_what_follows(self):
        assert verbatim.apply("dollar HOME slash bin") == "$HOME/bin"
        assert verbatim.apply("tilde slash projects") == "~/projects"

    def test_spoken_space_separates(self):
        """The escape hatch for "cd /usr": say the space."""
        assert verbatim.apply("cd space slash usr slash local") == "cd /usr/local"


# ---------------------------------------------------------------------------
# spell / cap
# ---------------------------------------------------------------------------

class TestSpelling:
    def test_spell_joins_letters(self):
        assert verbatim.apply("spell g i t") == "git"

    def test_cap_capitalises_the_next_letter_only(self):
        assert verbatim.apply("spell cap g i t") == "Git"

    def test_spelled_word_survives_the_sentence_capital_undo(self):
        """The leading-capital undo must not eat a capital the user asked for."""
        assert verbatim.apply("spell cap a b c") == "Abc"

    def test_spelling_run_ends_at_a_non_letter(self):
        assert verbatim.apply("spell g i t status") == "git status"

    def test_spell_with_no_letters_is_dropped(self):
        assert verbatim.apply("spell") == ""


# ---------------------------------------------------------------------------
# Digits -- adjacent vs not
# ---------------------------------------------------------------------------

class TestDigits:
    def test_digit_next_to_a_symbol_becomes_a_numeral(self):
        assert verbatim.apply("dash five") == "-5"

    def test_consecutive_digits_join(self):
        assert verbatim.apply("port colon eight zero eight zero") == "port:8080"

    def test_lone_number_word_stays_a_word(self):
        """"one line" must not become "1 line" -- that was the git log bug."""
        assert verbatim.apply("one line") == "one line"
        assert verbatim.apply("zero one two") == "zero one two"


# ---------------------------------------------------------------------------
# Inactive profile must not touch ordinary prose
# ---------------------------------------------------------------------------

class TestOrdinaryProse:
    def test_prose_keeps_its_words(self):
        assert verbatim.apply("the quick brown fox") == "the quick brown fox"

    def test_only_the_sentence_capital_is_undone(self):
        assert verbatim.apply("This is ordinary prose.") == "this is ordinary prose"

    def test_real_casing_is_preserved(self):
        assert "GitHub" in verbatim.apply("i use GitHub daily")
        assert "URL" in verbatim.apply("the URL is long")

    def test_empty_input(self):
        assert verbatim.apply("") == ""
        assert verbatim.apply("   ") == ""


# ---------------------------------------------------------------------------
# Process list
# ---------------------------------------------------------------------------

class TestProcessMatching:
    @pytest.mark.parametrize("name", [
        "warp", "Warp.exe", "WINDOWSTERMINAL.EXE", "cmd.exe", "powershell.exe",
        "pwsh.exe", "conhost.exe", "alacritty.exe", "wezterm.exe", "Code.exe",
    ])
    def test_default_targets_match(self, name):
        assert verbatim.matches_process(name) is True

    @pytest.mark.parametrize("name", ["notepad.exe", "chrome.exe", "explorer.exe", ""])
    def test_non_targets_do_not_match(self, name):
        assert verbatim.matches_process(name) is False

    def test_full_path_is_tolerated(self):
        assert verbatim.matches_process(r"C:\Program Files\Warp\warp.exe") is True

    def test_configured_list_overrides_the_default(self):
        assert verbatim.matches_process("notepad.exe", ["notepad"]) is True
        assert verbatim.matches_process("warp.exe", ["notepad"]) is False


# ---------------------------------------------------------------------------
# Address bar
# ---------------------------------------------------------------------------

class TestAddressBar:
    def test_chromium_omnibox_by_automation_id(self):
        assert verbatim.is_address_bar("EditControl", "view_1000", "") is True

    def test_firefox_urlbar_by_automation_id(self):
        assert verbatim.is_address_bar("EditControl", "urlbar-input", "") is True

    @pytest.mark.parametrize("name", [
        "Address and search bar",
        "Search or enter web address",
        "Search with Google or enter address",
    ])
    def test_matched_by_visible_name(self, name):
        assert verbatim.is_address_bar("EditControl", "", name) is True

    def test_non_edit_control_is_not_an_address_bar(self):
        assert verbatim.is_address_bar("DocumentControl", "view_1000", "") is False

    def test_ordinary_edit_field_is_not_an_address_bar(self):
        assert verbatim.is_address_bar("EditControl", "search_box", "Search") is False

    def test_missing_properties_fail_closed(self):
        assert verbatim.is_address_bar("", "", "") is False


# ---------------------------------------------------------------------------
# Toggle phrases and precedence
# ---------------------------------------------------------------------------

class TestToggle:
    @pytest.mark.parametrize("phrase", ["literal on", "verbatim on", "Literal On.", "LITERAL ON"])
    def test_on_phrases(self, phrase):
        assert verbatim.match_toggle(phrase) is True

    @pytest.mark.parametrize("phrase", ["literal off", "verbatim off", "Verbatim off!"])
    def test_off_phrases(self, phrase):
        assert verbatim.match_toggle(phrase) is False

    def test_ordinary_text_is_not_a_toggle(self):
        assert verbatim.match_toggle("use the literal on-ramp") is None
        assert verbatim.match_toggle("literal") is None
        assert verbatim.match_toggle("") is None


def _app(*, process="notepad.exe", address_bar=False, forced=False, config=None):
    """DictationApp stub carrying the REAL precedence methods.

    dictation is imported here, inside the helper, never at module level."""
    import dictation as _d

    class _Stub:
        _verbatim_rule = _d.DictationApp._verbatim_rule
        _verbatim_forced = _d.DictationApp._verbatim_forced
        _verbatim_target_process = _d.DictationApp._verbatim_target_process
        _apply_verbatim_if_active = _d.DictationApp._apply_verbatim_if_active
        _consume_verbatim_toggle = _d.DictationApp._consume_verbatim_toggle
        set_verbatim_forced = _d.DictationApp.set_verbatim_forced

        def __init__(self):
            self.config = config if config is not None else {}
            self._verbatim_force = forced
            self._dictate_preview = None
            self.sounds = []

        def _foreground_process_name(self):
            return process

        def _verbatim_address_bar(self):
            return address_bar

        def play_sound(self, name, **_kw):
            self.sounds.append(name)

    return _Stub()


class TestPrecedence:
    def test_toggle_beats_everything(self):
        app = _app(process="notepad.exe", address_bar=False, forced=True)
        assert app._verbatim_rule() == "toggle"

    def test_process_list_beats_address_bar(self):
        app = _app(process="warp.exe", address_bar=True)
        assert app._verbatim_rule() == "process:warp.exe"

    def test_address_bar_when_process_does_not_match(self):
        app = _app(process="chrome.exe", address_bar=True)
        assert app._verbatim_rule() == "address_bar"

    def test_normal_when_nothing_matches(self):
        app = _app(process="notepad.exe", address_bar=False)
        assert app._verbatim_rule() is None

    def test_inactive_profile_returns_none_and_leaves_text_alone(self):
        app = _app(process="notepad.exe")
        assert app._apply_verbatim_if_active("Echo, hello?") is None

    def test_active_profile_transforms(self):
        app = _app(process="warp.exe")
        assert app._apply_verbatim_if_active("Echo, hello?") == "echo hello"

    def test_disabled_by_config(self):
        app = _app(process="warp.exe", config={"verbatim": {"enabled": False}})
        assert app._verbatim_rule() is None

    def test_configured_process_list_is_honoured(self):
        app = _app(process="notepad.exe", config={"verbatim": {"processes": ["notepad"]}})
        assert app._verbatim_rule() == "process:notepad.exe"

    def test_toggle_phrase_is_consumed_and_flips_the_flag(self):
        app = _app()
        assert app._consume_verbatim_toggle("literal on") is True
        assert app._verbatim_forced() is True
        assert app._consume_verbatim_toggle("literal off") is True
        assert app._verbatim_forced() is False

    def test_non_toggle_text_is_not_consumed(self):
        app = _app()
        assert app._consume_verbatim_toggle("echo hello") is False
        assert app._verbatim_forced() is False
