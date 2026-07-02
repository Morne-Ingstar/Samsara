"""Tests for samsara.session_modes: the unified toggle-command-mode state
machine (switch-word matcher, seam-join heuristic, anti-hallucination gate,
unit-of-work stack, focus-lock, and SessionModeManager dispatch).

All of session_modes.py is pure orchestration -- no audio/Whisper/pyautogui/
Qt mocking needed. Side effects are plain Mock() callables.
"""
import pytest
from unittest.mock import Mock

from samsara.session_modes import (
    SessionMode,
    SwitchMatch,
    UtteranceSignals,
    StackItem,
    UnitOfWorkStack,
    SessionModeManager,
    normalize_utterance,
    is_scratch_that,
    match_switch_word,
    passes_switch_anti_hallucination_gate,
    seam_join,
    chunk_ends_terminal,
    check_focus_lock,
    detect_stage_reference,
    SWITCH_WORD_MAX_COMPRESSION_RATIO,
)


# ---------------------------------------------------------------------------
# normalize_utterance
# ---------------------------------------------------------------------------

class TestNormalizeUtterance:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_utterance("Command Mode!") == "command mode"

    def test_strips_leading_fillers(self):
        assert normalize_utterance("um, command mode") == "command mode"
        assert normalize_utterance("So dictate mode") == "dictate mode"

    def test_strips_multiple_leading_fillers(self):
        assert normalize_utterance("um uh so dictate mode") == "dictate mode"

    def test_empty_and_whitespace(self):
        assert normalize_utterance("") == ""
        assert normalize_utterance("   ") == ""
        assert normalize_utterance(None) == ""

    def test_fillers_not_stripped_mid_utterance(self):
        # "so" here is not a LEADING filler -- it's mid-sentence content.
        assert normalize_utterance("open chrome so I can browse") == "open chrome so i can browse"


# ---------------------------------------------------------------------------
# match_switch_word: whole, prefix-with-payload, mid-utterance non-match
# ---------------------------------------------------------------------------

class TestSwitchWordMatcher:
    @pytest.mark.parametrize("text,expected_mode", [
        ("command mode", SessionMode.COMMAND),
        ("Command Mode", SessionMode.COMMAND),
        ("dictate mode", SessionMode.DICTATE),
        ("dictation mode", SessionMode.DICTATE),
        ("dictate", SessionMode.DICTATE),
        ("Dictate!", SessionMode.DICTATE),
        ("ava", SessionMode.AVA),
        ("Ava", SessionMode.AVA),
        ("ava mode", SessionMode.AVA),
        ("Ava Mode!", SessionMode.AVA),
    ])
    def test_whole_utterance_match(self, text, expected_mode):
        m = match_switch_word(text)
        assert m is not None
        assert m.target_mode is expected_mode
        assert m.is_prefix is False
        assert m.payload == ""

    def test_whole_utterance_match_with_leading_filler(self):
        m = match_switch_word("um, command mode")
        assert m is not None
        assert m.target_mode is SessionMode.COMMAND

    def test_prefix_form_with_payload(self):
        m = match_switch_word("dictate hello world")
        assert m is not None
        assert m.target_mode is SessionMode.DICTATE
        assert m.is_prefix is True
        assert m.payload == "hello world"

    def test_ava_prefix_form_with_payload(self):
        m = match_switch_word("ava what time is it")
        assert m is not None
        assert m.target_mode is SessionMode.AVA
        assert m.is_prefix is True
        assert m.payload == "what time is it"

    def test_ava_prefix_form_preserves_original_casing_and_punctuation(self):
        m = match_switch_word("ava What's the weather?")
        assert m is not None
        assert m.payload == "What's the weather?"

    def test_prefix_form_preserves_original_casing_and_punctuation(self):
        m = match_switch_word("dictate Hello, World!")
        assert m is not None
        assert m.payload == "Hello, World!"

    def test_prefix_form_with_leading_filler(self):
        m = match_switch_word("so dictate write this down")
        assert m is not None
        assert m.is_prefix is True
        assert m.payload == "write this down"

    @pytest.mark.parametrize("text", [
        "we should use dictate mode later",   # switch word not utterance-initial
        "please dictate this for me",          # "dictate" not utterance-initial
        "the command mode is useful",          # "command mode" not utterance-initial
        "I heard a command mode reference",
        "tell ava later",                      # "ava" not utterance-initial
        "we should try ava mode sometime",
    ])
    def test_mid_utterance_substring_never_matches(self, text):
        assert match_switch_word(text) is None

    def test_unrelated_text_does_not_match(self):
        assert match_switch_word("open chrome") is None
        assert match_switch_word("") is None

    def test_bare_dictate_word_no_trailing_content_is_whole_match_not_prefix(self):
        m = match_switch_word("dictate")
        assert m is not None
        assert m.is_prefix is False

    def test_prefix_word_with_only_trailing_whitespace_does_not_match(self):
        # "dictate " with nothing meaningful after it -- no payload to deliver.
        assert match_switch_word("dictate    ") is None or match_switch_word("dictate    ").payload == ""


class TestScratchThat:
    @pytest.mark.parametrize("text", [
        "scratch that", "Scratch That!", "  scratch   that  ", "um, scratch that",
    ])
    def test_matches_whole_utterance(self, text):
        assert is_scratch_that(text) is True

    @pytest.mark.parametrize("text", [
        "let's scratch that idea", "I want to scratch that later", "scratch that itch",
        "", "scratch",
    ])
    def test_does_not_match_mid_utterance_or_partial(self, text):
        assert is_scratch_that(text) is False


# ---------------------------------------------------------------------------
# seam_join heuristic
# ---------------------------------------------------------------------------

class TestSeamJoin:
    def test_previous_ended_terminal_returns_unchanged(self):
        assert seam_join(True, "Paris is beautiful.") == "Paris is beautiful."

    def test_lowercases_ordinary_capitalized_seam_word(self):
        assert seam_join(False, "The weather is nice") == "the weather is nice"

    def test_leaves_already_lowercase_seam_word_alone(self):
        assert seam_join(False, "and then we left") == "and then we left"

    def test_preserves_capitalization_after_leading_filler(self):
        # "um" is Whisper's real first word; "Paris" landing capitalized
        # AFTER a filler is not explainable by automatic sentence-initial
        # capitalization -- treated as a genuine proper noun.
        assert seam_join(False, "um Paris is beautiful") == "um Paris is beautiful"

    def test_multiple_leading_fillers_before_proper_noun(self):
        assert seam_join(False, "so um Tokyo was amazing") == "so um Tokyo was amazing"

    def test_all_filler_chunk_returned_unchanged(self):
        assert seam_join(False, "um uh") == "um uh"

    def test_empty_chunk(self):
        assert seam_join(False, "") == ""
        assert seam_join(True, "   ") == ""

    def test_only_first_character_of_seam_word_is_touched(self):
        # Rest of a mixed-case word (e.g. an abbreviation) is left intact.
        assert seam_join(False, "USA is large") == "uSA is large"


class TestChunkEndsTerminal:
    @pytest.mark.parametrize("text,expected", [
        ("Hello there.", True),
        ("Wait, really?", True),
        ("Stop!", True),
        ("Section one:", True),
        ("Note the following;", True),
        ("Hello there", False),
        ("", False),
        ("   ", False),
    ])
    def test_terminal_detection(self, text, expected):
        assert chunk_ends_terminal(text) is expected


# ---------------------------------------------------------------------------
# Anti-hallucination gate
# ---------------------------------------------------------------------------

class TestAntiHallucinationGate:
    def test_passes_with_good_signals(self):
        sig = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2, 1.5))
        assert passes_switch_anti_hallucination_gate(sig) is True

    def test_fails_closed_when_speech_gate_unavailable(self):
        sig = UtteranceSignals(has_contiguous_speech=None, compression_ratios=(1.2,))
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_fails_when_speech_gate_false(self):
        sig = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.2,))
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_fails_closed_with_no_segments(self):
        sig = UtteranceSignals(has_contiguous_speech=True, compression_ratios=())
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_fails_closed_with_unavailable_compression_ratio(self):
        sig = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.2, None))
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_fails_on_exploded_compression_ratio(self):
        sig = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(45.0,))
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_stricter_than_general_backstop_threshold(self):
        # 2.5 is BELOW the general dictation.py backstop (3.0) but ABOVE
        # this module's stricter switch-word threshold (2.4) -- must fail.
        assert SWITCH_WORD_MAX_COMPRESSION_RATIO < 3.0
        sig = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(2.5,))
        assert passes_switch_anti_hallucination_gate(sig) is False

    def test_passes_at_exactly_the_threshold(self):
        sig = UtteranceSignals(has_contiguous_speech=True,
                                compression_ratios=(SWITCH_WORD_MAX_COMPRESSION_RATIO,))
        assert passes_switch_anti_hallucination_gate(sig) is True


# ---------------------------------------------------------------------------
# Hallucination regression: known near-silent hallucination shapes must
# never fire a switch or scratch-that. Structured as a parametrized list so
# real captured buffers (raw transcript + measured signals) can be appended
# later without changing the test body.
# ---------------------------------------------------------------------------

KNOWN_HALLUCINATION_SHAPES = [
    # (raw_transcript, has_contiguous_speech, compression_ratios)
    ("click click click click", False, (38.0,)),
    ("bloop bloop bloop", False, (31.5,)),
    ("click click click click click", None, (53.0,)),   # gate unavailable AND exploded ratio
    ("thank you for watching", False, (2.0,)),            # sub-floor energy, plausible-looking text
    ("dictate mode", False, (45.0,)),                      # hallucinated transcript that HAPPENS to read as a switch phrase
    ("scratch that", True, (30.0,)),                       # speech gate ok but compression ratio exploded
    ("command mode", None, ()),                            # both signals unavailable
    ("ava ava ava", False, (36.0,)),                       # repeated-word hallucination shape, sub-floor energy
    ("ava", False, (2.1,)),                                 # sub-floor energy, plausible-looking short word
    ("ava mode", None, (60.0,)),                            # gate unavailable AND exploded ratio
]


class TestHallucinationRegression:
    @pytest.mark.parametrize("raw_transcript,has_contig,ratios", KNOWN_HALLUCINATION_SHAPES)
    def test_gate_rejects_known_hallucination_shapes(self, raw_transcript, has_contig, ratios):
        sig = UtteranceSignals(has_contiguous_speech=has_contig, compression_ratios=ratios)
        assert passes_switch_anti_hallucination_gate(sig) is False

    @pytest.mark.parametrize("raw_transcript,has_contig,ratios", KNOWN_HALLUCINATION_SHAPES)
    def test_dispatch_never_switches_or_scratches_on_bad_signals(self, raw_transcript, has_contig, ratios, manager_factory):
        mgr, mocks = manager_factory()
        sig = UtteranceSignals(has_contiguous_speech=has_contig, compression_ratios=ratios)
        outcome = mgr.dispatch_utterance(raw_transcript, sig)
        assert outcome.kind not in ("mode_switch", "scratch_success", "scratch_refuse")
        assert mgr.mode is SessionMode.COMMAND  # never left COMMAND from a bad-signal "dictate mode"


# ---------------------------------------------------------------------------
# Unit-of-work stack semantics
# ---------------------------------------------------------------------------

class TestUnitOfWorkStack:
    def _item(self, n):
        return StackItem(kind="command", payload=f"cmd{n}", mode=SessionMode.COMMAND, timestamp=float(n))

    def test_push_pop_is_lifo(self):
        stack = UnitOfWorkStack()
        stack.push(self._item(1))
        stack.push(self._item(2))
        assert stack.pop().payload == "cmd2"
        assert stack.pop().payload == "cmd1"
        assert stack.pop() is None

    def test_peek_does_not_remove(self):
        stack = UnitOfWorkStack()
        stack.push(self._item(1))
        assert stack.peek().payload == "cmd1"
        assert len(stack) == 1

    def test_pop_empty_returns_none(self):
        stack = UnitOfWorkStack()
        assert stack.pop() is None

    def test_bounded_to_five_evicts_oldest(self):
        stack = UnitOfWorkStack()
        for n in range(1, 8):  # push 7 items into a max-5 stack
            stack.push(self._item(n))
        assert len(stack) == 5
        # Oldest (cmd1, cmd2) evicted; newest-first pop order is 7,6,5,4,3.
        popped = [stack.pop().payload for _ in range(5)]
        assert popped == ["cmd7", "cmd6", "cmd5", "cmd4", "cmd3"]

    def test_items_newest_first_ordering(self):
        stack = UnitOfWorkStack()
        stack.push(self._item(1))
        stack.push(self._item(2))
        stack.push(self._item(3))
        assert [i.payload for i in stack.items_newest_first()] == ["cmd3", "cmd2", "cmd1"]


# ---------------------------------------------------------------------------
# check_focus_lock decision function (mocked foreground resolver at the
# call site, since this is a pure function of two already-resolved names)
# ---------------------------------------------------------------------------

class TestCheckFocusLock:
    def test_matching_process_names(self):
        assert check_focus_lock("notepad.exe", "notepad.exe") is True

    def test_case_insensitive_match(self):
        assert check_focus_lock("Notepad.EXE", "notepad.exe") is True

    def test_mismatched_process_names(self):
        assert check_focus_lock("notepad.exe", "chrome.exe") is False

    def test_none_target_fails_closed(self):
        assert check_focus_lock(None, "notepad.exe") is False

    def test_none_foreground_fails_closed(self):
        assert check_focus_lock("notepad.exe", None) is False

    def test_both_none_fails_closed(self):
        assert check_focus_lock(None, None) is False

    def test_empty_string_fails_closed(self):
        assert check_focus_lock("", "notepad.exe") is False


# ---------------------------------------------------------------------------
# detect_stage_reference (AVA mode, Phase 2)
# ---------------------------------------------------------------------------

class TestDetectStageReference:
    @pytest.mark.parametrize("text", [
        "submit that",
        "send that",
        "send the text",
        "use this",
        "attach that",
        "what I dictated",
        "the dictation",
        "check the text please",
        "Submit That!",             # case + punctuation
        "um, submit that",          # leading filler stripped before the position check
        "please send this over",
    ])
    def test_positive_explicit_references(self, text):
        assert detect_stage_reference(text) is True

    @pytest.mark.parametrize("text", [
        "that was fun",             # "that" sentence-initial -- subject, not object
        "this is great",
        "that's nice",
        "hello there",
        "",
        "this",                     # bare word, still sentence-initial (index 0)
        "that",
        "open chrome",
    ])
    def test_negative_non_references(self, text):
        assert detect_stage_reference(text) is False

    def test_accepted_ambiguity_object_shaped_non_reference(self):
        """Documented trade-off: rule 2 is a position heuristic, not
        semantic understanding, so an object-shaped non-reference like "was
        that clear" also matches. Accepted because the failure mode is only
        ever "extra harmless context attached", never an unwanted action --
        see detect_stage_reference's docstring."""
        assert detect_stage_reference("was that clear") is True

    def test_phrase_rule_ignores_position(self):
        # Unlike the that/this rule, the explicit noun-phrase rule fires
        # regardless of where it appears in the utterance.
        assert detect_stage_reference("the text needs work") is True


# ---------------------------------------------------------------------------
# SessionModeManager integration: fixtures + dispatch behavior
# ---------------------------------------------------------------------------

GOOD_SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


@pytest.fixture
def manager_factory():
    """Returns a (manager, mocks) builder so each test can override callables."""
    def _build(foreground="notepad.exe", abort_phrases=None, command_matches=None):
        mocks = {
            "foreground": Mock(return_value=foreground),
            "inject": Mock(),
            "remove_chars": Mock(),
            "command_dispatch": Mock(return_value=CommandDispatchResultFor(command_matches)),
            "agent_dispatch": Mock(),
            "on_mode_change": Mock(),
            "on_focus_lock_revert": Mock(),
            "on_scratch_result": Mock(),
            "on_abort": Mock(),
        }
        mgr = SessionModeManager(
            abort_phrases=abort_phrases if abort_phrases is not None else ["cancel", "abort"],
            foreground_exe_resolver=mocks["foreground"],
            inject_fn=mocks["inject"],
            remove_chars_fn=mocks["remove_chars"],
            command_dispatch_fn=mocks["command_dispatch"],
            agent_dispatch_fn=mocks["agent_dispatch"],
            on_mode_change=mocks["on_mode_change"],
            on_focus_lock_revert=mocks["on_focus_lock_revert"],
            on_scratch_result=mocks["on_scratch_result"],
            on_abort=mocks["on_abort"],
            clock=lambda: 1000.0,
        )
        return mgr, mocks
    return _build


def CommandDispatchResultFor(matched):
    """Helper: default mock command_dispatch_fn always misses unless overridden per-test."""
    from samsara.session_modes import CommandDispatchResult
    if matched is None:
        return CommandDispatchResult(matched=False)
    return CommandDispatchResult(matched=True, phrase=matched)


class TestSessionModeManagerDispatch:
    def test_default_mode_is_command(self, manager_factory):
        mgr, _ = manager_factory()
        assert mgr.mode is SessionMode.COMMAND

    def test_abort_phrase_wins_regardless_of_mode(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("please cancel now", GOOD_SIGNALS)
        assert outcome.kind == "abort"
        mocks["on_abort"].assert_called_once()
        mocks["inject"].assert_not_called()

    def test_abort_phrase_does_not_require_hallucination_gate(self, manager_factory):
        mgr, mocks = manager_factory()
        bad_signals = UtteranceSignals(has_contiguous_speech=None, compression_ratios=())
        outcome = mgr.dispatch_utterance("abort", bad_signals)
        assert outcome.kind == "abort"
        mocks["on_abort"].assert_called_once()

    def test_command_mode_match_pushes_stack_and_calls_dispatch_fn(self, manager_factory):
        mgr, mocks = manager_factory(command_matches="open chrome")
        outcome = mgr.dispatch_utterance("open chrome", GOOD_SIGNALS)
        assert outcome.kind == "command_executed"
        assert outcome.detail["phrase"] == "open chrome"
        assert mgr.stack_depth == 1
        mocks["command_dispatch"].assert_called_once_with("open chrome")

    def test_command_mode_miss_does_not_push_stack(self, manager_factory):
        mgr, _ = manager_factory(command_matches=None)
        outcome = mgr.dispatch_utterance("gibberish nonsense", GOOD_SIGNALS)
        assert outcome.kind == "command_miss"
        assert mgr.stack_depth == 0

    def test_whole_utterance_switch_to_dictate(self, manager_factory):
        mgr, mocks = manager_factory()
        outcome = mgr.dispatch_utterance("dictate mode", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.DICTATE
        mocks["on_mode_change"].assert_called_once_with(SessionMode.DICTATE)

    def test_switch_back_to_command(self, manager_factory):
        mgr, _ = manager_factory()
        mgr.dispatch_utterance("dictate mode", GOOD_SIGNALS)
        outcome = mgr.dispatch_utterance("command mode", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.COMMAND

    def test_switch_gated_by_hallucination_check_falls_through_to_current_mode(self, manager_factory):
        mgr, mocks = manager_factory(command_matches=None)
        bad_signals = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(50.0,))
        outcome = mgr.dispatch_utterance("dictate mode", bad_signals)
        assert mgr.mode is SessionMode.COMMAND  # never switched
        assert outcome.kind == "command_miss"   # fell through to COMMAND dispatch instead
        mocks["command_dispatch"].assert_called_once_with("dictate mode")

    def test_prefix_switch_delivers_payload_as_first_dictate_chunk(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        outcome = mgr.dispatch_utterance("dictate hello there", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.DICTATE
        assert outcome.kind == "dictate_injected"
        mocks["inject"].assert_called_once_with("hello there")  # first chunk: no leading space

    def test_dictate_chunk_injected_when_focus_matches(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        assert outcome.kind == "dictate_injected"
        mocks["inject"].assert_called_once_with("Hello world")

    def test_second_dictate_chunk_gets_seam_joined_with_leading_space(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello there", GOOD_SIGNALS)   # no terminal punctuation
        mgr.dispatch_utterance("How are you", GOOD_SIGNALS)
        assert mocks["inject"].call_args_list[1].args[0] == " how are you"

    def test_seam_join_resets_after_terminal_punctuation(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello there.", GOOD_SIGNALS)
        mgr.dispatch_utterance("How are you", GOOD_SIGNALS)
        assert mocks["inject"].call_args_list[1].args[0] == " How are you"

    def test_focus_lock_suppresses_and_reverts_to_command(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        assert mgr.mode is SessionMode.DICTATE
        # foreground drifts to a different app before the injection attempt
        mocks["foreground"].return_value = "chrome.exe"
        outcome = mgr.dispatch_utterance("this should not land", GOOD_SIGNALS)
        assert outcome.kind == "dictate_suppressed_focus_lock"
        mocks["inject"].assert_not_called()
        mocks["on_focus_lock_revert"].assert_called_once()
        assert mgr.mode is SessionMode.COMMAND
        assert mgr.stack_depth == 1

    def test_scratch_that_undoes_dictation_chunk(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.COMMAND)
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_success"
        mocks["remove_chars"].assert_called_once_with(len("Hello world"))
        mocks["on_scratch_result"].assert_called_once_with(True)

    def test_scratch_that_refuses_when_focus_moved(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.COMMAND)
        mocks["foreground"].return_value = "chrome.exe"
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_refuse"
        mocks["remove_chars"].assert_not_called()

    def test_scratch_that_on_command_entry_is_noop_refuse(self, manager_factory):
        mgr, mocks = manager_factory(command_matches="open chrome")
        mgr.dispatch_utterance("open chrome", GOOD_SIGNALS)
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_refuse"
        mocks["remove_chars"].assert_not_called()

    def test_scratch_that_on_empty_stack_refuses(self, manager_factory):
        mgr, mocks = manager_factory()
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_refuse"

    def test_scratch_that_works_across_mode_switch(self, manager_factory):
        """The stack entry survives a DICTATE -> COMMAND switch so scratch
        that still finds it (this is the whole point of the global stack)."""
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Some text", GOOD_SIGNALS)
        outcome = mgr.dispatch_utterance("command mode", GOOD_SIGNALS)  # switch back
        assert outcome.kind == "mode_switch"
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_success"

    def test_retype_that_reinjects_suppressed_chunk_when_focus_restored(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "chrome.exe"
        mgr.dispatch_utterance("lost text", GOOD_SIGNALS)  # suppressed
        mocks["inject"].assert_not_called()

        mocks["foreground"].return_value = "notepad.exe"  # focus restored
        result = mgr.retype_last_suppressed()
        assert result is True
        mocks["inject"].assert_called_once_with("lost text")

    def test_retype_that_refuses_if_focus_still_wrong(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "chrome.exe"
        mgr.dispatch_utterance("lost text", GOOD_SIGNALS)
        result = mgr.retype_last_suppressed()
        assert result is False
        mocks["inject"].assert_not_called()

    def test_retype_that_with_nothing_suppressed_returns_false(self, manager_factory):
        mgr, mocks = manager_factory()
        assert mgr.retype_last_suppressed() is False

    def test_retype_that_does_not_repeat_after_success(self, manager_factory):
        mgr, mocks = manager_factory(foreground="chrome.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "notepad.exe"  # target locked to notepad
        mgr.force_mode(SessionMode.COMMAND)
        mgr.force_mode(SessionMode.DICTATE)  # re-lock target to whatever's foreground now
        # Simulate suppression directly via mismatched foreground at dispatch time
        mocks["foreground"].return_value = "chrome.exe"
        mgr.dispatch_utterance("once only", GOOD_SIGNALS)
        mocks["foreground"].return_value = "notepad.exe"
        assert mgr.retype_last_suppressed() is True
        mocks["inject"].reset_mock()
        assert mgr.retype_last_suppressed() is False  # already consumed
        mocks["inject"].assert_not_called()

    def test_empty_utterance_is_ignored(self, manager_factory):
        mgr, mocks = manager_factory()
        outcome = mgr.dispatch_utterance("   ", GOOD_SIGNALS)
        assert outcome.kind == "empty"
        mocks["command_dispatch"].assert_not_called()


# ---------------------------------------------------------------------------
# AVA mode dispatch (Phase 2): agent routing + stage-buffer contract
# ---------------------------------------------------------------------------

class TestSessionModeManagerAvaDispatch:
    def test_whole_utterance_switch_to_ava(self, manager_factory):
        mgr, mocks = manager_factory()
        outcome = mgr.dispatch_utterance("ava", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.AVA
        mocks["on_mode_change"].assert_called_once_with(SessionMode.AVA)

    def test_ava_prefix_switch_delivers_payload_to_agent(self, manager_factory):
        mgr, mocks = manager_factory()
        outcome = mgr.dispatch_utterance("ava what time is it", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.AVA
        assert outcome.kind == "ava_dispatched"
        mocks["agent_dispatch"].assert_called_once_with("what time is it", None)

    def test_plain_ava_utterance_routes_to_agent_dispatch(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("what is the capital of France", GOOD_SIGNALS)
        assert outcome.kind == "ava_dispatched"
        mocks["agent_dispatch"].assert_called_once_with(
            "what is the capital of France", None
        )

    def test_ava_utterance_never_pushes_scratch_stack(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("tell me a joke", GOOD_SIGNALS)
        assert mgr.stack_depth == 0  # agent turns are never undoable units of work

    def test_buffer_not_attached_when_no_reference_even_if_nonempty(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        assert mgr.stage_buffer == "Hello world"
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("what time is it", GOOD_SIGNALS)  # no reference word
        mocks["agent_dispatch"].assert_called_once_with("what time is it", None)
        assert mgr.stage_buffer == "Hello world"  # untouched

    def test_buffer_not_attached_when_reference_detected_but_buffer_empty(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("submit that", GOOD_SIGNALS)  # nothing was ever dictated
        mocks["agent_dispatch"].assert_called_once_with("submit that", None)

    def test_buffer_attached_only_when_reference_detected_and_nonempty(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("submit that", GOOD_SIGNALS)
        mocks["agent_dispatch"].assert_called_once_with("submit that", "Hello world")

    def test_buffer_cleared_after_explicit_send(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("submit that", GOOD_SIGNALS)
        assert mgr.stage_buffer == ""

    def test_no_code_path_sends_buffer_without_detected_reference(self, manager_factory):
        """Dispatch several non-reference AVA utterances while the buffer is
        non-empty -- the mock must NEVER see the buffer contents, only None
        as the second argument, every single time."""
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("sensitive staged content", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        for utt in ("what time is it", "tell me a joke", "how are you", "this is great"):
            mgr.dispatch_utterance(utt, GOOD_SIGNALS)
        for call in mocks["agent_dispatch"].call_args_list:
            assert call.args[1] is None, f"buffer leaked on call {call!r}"
        assert mgr.stage_buffer == "sensitive staged content"  # never consumed

    def test_scratch_that_in_ava_mode_pops_prior_dictation_entry(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_success"
        mocks["remove_chars"].assert_called_once_with(len("Hello world"))

    def test_scratch_that_in_ava_mode_refuses_when_stack_empty(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_refuse"

    def test_ava_switch_gated_by_hallucination_check_falls_through(self, manager_factory):
        mgr, mocks = manager_factory(command_matches=None)
        bad_signals = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(50.0,))
        outcome = mgr.dispatch_utterance("ava", bad_signals)
        assert mgr.mode is SessionMode.COMMAND  # never switched
        assert outcome.kind == "command_miss"   # fell through to COMMAND dispatch instead
        mocks["command_dispatch"].assert_called_once_with("ava")


# ---------------------------------------------------------------------------
# AVA mode transitions (Phase 2): any-to-any, matching Phase 1's contract
# ---------------------------------------------------------------------------

class TestSessionModeManagerAvaTransitions:
    def test_ava_to_dictate_via_prefix_delivers_text(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("dictate hello there", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.DICTATE
        assert outcome.kind == "dictate_injected"
        mocks["inject"].assert_called_once_with("hello there")

    def test_ava_to_dictate_mode_whole_switch(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("dictate mode", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.DICTATE

    def test_ava_to_command_mode(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("command mode", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.COMMAND

    def test_dictate_to_ava_prefix_with_payload(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("ava what time is it", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.AVA
        assert outcome.kind == "ava_dispatched"
        mocks["agent_dispatch"].assert_called_once_with("what time is it", None)

    def test_command_to_ava_and_back(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.dispatch_utterance("ava", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.AVA
        mgr.dispatch_utterance("command mode", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.COMMAND

    def test_fresh_dictate_entry_from_ava_starts_a_new_stage_buffer(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("old text", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        assert mgr.stage_buffer == "old text"
        mgr.force_mode(SessionMode.DICTATE)  # fresh DICTATE entry
        assert mgr.stage_buffer == ""
        mgr.dispatch_utterance("new text", GOOD_SIGNALS)
        assert mgr.stage_buffer == "new text"


# ---------------------------------------------------------------------------
# Mode-reset-on-session-timeout
# ---------------------------------------------------------------------------

class TestSessionReset:
    def test_reset_returns_to_command_and_clears_stack(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe", command_matches="open chrome")
        mgr.dispatch_utterance("open chrome", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.DICTATE)
        assert mgr.mode is SessionMode.DICTATE
        assert mgr.stack_depth == 1

        mgr.reset()

        assert mgr.mode is SessionMode.COMMAND
        assert mgr.stack_depth == 0

    def test_reset_clears_dictate_target_and_seam_state(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello there", GOOD_SIGNALS)  # no terminal punctuation

        mgr.reset()
        mgr.force_mode(SessionMode.DICTATE)
        # After reset, a fresh DICTATE entry must NOT seam-join onto the
        # pre-reset chunk (no leading space, no lowercase-first-word).
        mgr.dispatch_utterance("Fresh Start", GOOD_SIGNALS)
        assert mocks["inject"].call_args_list[-1].args[0] == "Fresh Start"

    def test_reset_after_focus_lock_revert_reverts_correctly_next_session(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "chrome.exe"
        mgr.dispatch_utterance("lost", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.COMMAND  # auto-reverted

        mgr.reset()  # simulates 30s inactivity_timeout_s session end
        assert mgr.mode is SessionMode.COMMAND
        assert mgr.stack_depth == 0
        assert mgr.retype_last_suppressed() is False  # suppressed item discarded too

    def test_session_timeout_from_ava_reenters_at_command(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("staged text", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        assert mgr.mode is SessionMode.AVA
        assert mgr.stage_buffer == "staged text"

        mgr.reset()  # simulates 30s inactivity_timeout_s session end

        assert mgr.mode is SessionMode.COMMAND
        assert mgr.stage_buffer == ""
        assert mgr.stack_depth == 0
