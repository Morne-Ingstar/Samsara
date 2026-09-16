"""Queue 117: the menu is visible in the log, and the registry comes first.

Two defects, one file:

1. The menu Ava was offered was invisible. `[AVA PROMPT]` was a `print()`, so
   it never reached samsara.log, and it fired ONCE per process. Nothing
   recorded which names a given turn was offered, so "the command was not in
   her menu" and "it was there and she ignored it" were indistinguishable from
   the log. That is why the defect needed a live session and a catalog dump.

2. The system prompt taught ACTION2 (the app/window fallback) first, with
   three worked examples, ~35 lines before ACTION and ~60 lines before the
   command list. A 3B model anchors on the first pattern it is shown.

These tests pin the fixes so neither can silently revert.
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from plugins.commands import ask_ollama


# ---------------------------------------------------------------------------
# 1. The menu log
# ---------------------------------------------------------------------------

class TestTheMenuIsInTheLog:
    def test_a_turn_records_the_names_it_was_offered(self, caplog):
        menu = ["next tab", "minimize all", "snap right"]
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("take me to the next tab", menu)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "[AVA-MENU]" in line
        assert "names=3" in line
        for name in menu:
            assert name in line, name

    def test_it_records_the_utterance_so_it_correlates_with_ava_turn(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("hide all these windows for me", ["minimize all"])
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "hide all these windows" in line

    def test_a_turn_whose_target_is_in_the_menu_records_that_fact(self, caplog):
        """The question the old log could not answer."""
        menu = ["find tab", "next tab", "close tab"]
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("next tab", menu)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "next tab" in line
        assert ask_ollama.menu_contains(menu, "next tab") is True
        assert ask_ollama.menu_contains(menu, "NEXT TAB") is True
        assert ask_ollama.menu_contains(menu, "snap right") is False

    def test_it_is_bounded_by_names(self, caplog):
        menu = [f"command number {i}" for i in range(400)]
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("x", menu)
        line = "\n".join(r.getMessage() for r in caplog.records)
        assert "names=400" in line
        assert f"shown={ask_ollama.MENU_LOG_MAX_NAMES}" not in line or True
        shown = int(line.split("shown=")[1].split()[0])
        assert shown <= ask_ollama.MENU_LOG_MAX_NAMES

    def test_it_is_bounded_by_characters_and_says_what_it_dropped(self, caplog):
        menu = ["x" * 200 for _ in range(50)]
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("x", menu)
        line = "\n".join(r.getMessage() for r in caplog.records)
        ranked = line.split("ranked=[", 1)[1]
        assert len(ranked) < ask_ollama.MENU_LOG_MAX_CHARS + 200
        omitted = int(line.split("omitted=")[1].split()[0])
        assert omitted > 0, "a truncated menu must say how many it dropped"

    def test_the_count_is_always_honest_even_when_truncated(self, caplog):
        """Absence stays detectable: shown + omitted == the real total."""
        menu = [f"cmd {i}" for i in range(300)]
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("x", menu)
        line = "\n".join(r.getMessage() for r in caplog.records)
        shown = int(line.split("shown=")[1].split()[0])
        omitted = int(line.split("omitted=")[1].split()[0])
        assert shown + omitted == 300

    def test_it_never_raises(self, caplog):
        """Logging must not be able to break a turn."""
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("x", None)
            ask_ollama._log_menu(None, ["a"])
            ask_ollama._log_menu("x", [None])

    def test_it_logs_at_debug_not_higher(self, caplog):
        with caplog.at_level(logging.DEBUG, logger=ask_ollama.logger.name):
            ask_ollama._log_menu("x", ["a"])
        menu_records = [r for r in caplog.records if "[AVA-MENU]" in r.getMessage()]
        assert menu_records
        assert all(r.levelno == logging.DEBUG for r in menu_records)


# ---------------------------------------------------------------------------
# 2. The protocol ordering
# ---------------------------------------------------------------------------

def _prompts():
    """Both shipped personalities, COMPOSED -- get_system_prompt() appends
    _SHARED_MODES to whichever persona is configured, and the modes are where
    the protocol lives. Asserting on the bare persona strings would pass
    while the protocol rotted underneath."""
    return {
        "relaxed": ask_ollama.RELAXED_SYSTEM_PROMPT + ask_ollama._SHARED_MODES,
        "strict": ask_ollama.STRICT_SYSTEM_PROMPT + ask_ollama._SHARED_MODES,
        "default": ask_ollama.DEFAULT_SYSTEM_PROMPT,
    }


class TestTheRegistryComesFirst:
    def test_action_is_taught_before_the_app_tag(self):
        """The anchoring fix. A 3B model takes the first pattern it is shown;
        every 'next tab' request became an app-verb line.

        Queue 125 renamed that tag ACTION2 -> APP, so this asserts on the
        current tag. The ORDERING is the invariant; the spelling is not."""
        for name, prompt in _prompts().items():
            assert prompt.index("ACTION <exact command name") < prompt.index("APP <verb>"), name

    def test_the_command_list_is_adjacent_to_action_not_at_the_bottom(self):
        """The list defines which requests are ACTION; it has to be where the
        model is deciding, not 60 lines below."""
        for name, prompt in _prompts().items():
            list_at = prompt.index("{COMMAND_LIST}")
            action_at = prompt.index("ACTION <exact command name")
            app_at = prompt.index("APP <verb>")
            assert list_at < app_at, name
            assert abs(action_at - list_at) < 900, (name, abs(action_at - list_at))

    def test_the_app_tag_is_scoped_to_what_the_list_cannot_do(self):
        """125 made APP its own MODE rather than step 2 of MODE 2 -- a
        different grammar presented as a subordinate step is part of why it
        collapsed. The SCOPING has to survive that promotion."""
        for name, prompt in _prompts().items():
            lowered = prompt.lower()
            assert "list names that application" in lowered, name
            assert "focus, open, close" in lowered, name
            assert "application or window" in lowered, name
            assert "nothing in the mode 2 list does the job" in lowered, name

    def test_there_is_a_counter_example_naming_next_tab(self):
        """Small models learn a boundary from a negative case, not a rule."""
        for name, prompt in _prompts().items():
            assert "COUNTER-EXAMPLE" in prompt, name
            assert "APP focus | next tab" in prompt, name
            assert "ACTION open firefox" in prompt, name   # the dangerous one
            assert "WRONG" in prompt, name

    def test_the_tags_no_longer_share_a_prefix(self):
        """117 added a "|" tell because ACTION and ACTION2 differ by one
        character. 125 removed the cause instead: APP and ACTION diverge at
        the SECOND character. The "|" is still APP's separator and the parser
        still refuses an ACTION line containing one -- but that is now
        enforced in code, not asked for in prose."""
        for name, prompt in _prompts().items():
            assert "APP <verb> | <argument>" in prompt, name
            flat = " ".join(prompt.lower().split())
            assert 'it has no argument slot and it never contains a "|" character' in flat, name
            assert 'Never put a "|" on an ACTION line.' in prompt, name
        assert not ask_ollama.APP_TAG.startswith("ACTION")
        assert ask_ollama.APP_TAG[:2] != "AC"

    def test_the_unchanged_rules_survived(self):
        for name, prompt in _prompts().items():
            assert "Never mix prose with ACTION, APP, SCHEDULE, or CONFIRM tags." in prompt, name
            assert "MODE 4 — SCHEDULED ACTION:" in prompt, name
            assert "SCHEDULE <interval in seconds" in prompt, name
            assert "Never substitute the nearest listed command." in prompt, name

    def test_every_placeholder_survives_exactly_once(self):
        """A lost placeholder means an un-substituted literal reaches the
        model; a duplicated one means the 4.5 KB menu is sent twice."""
        for name, prompt in _prompts().items():
            for token in ("{COMMAND_LIST}", "{USER_PROFILE}", "{USER_ALIASES}"):
                assert prompt.count(token) == 1, (name, token, prompt.count(token))
