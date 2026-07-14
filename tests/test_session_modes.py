"""Tests for samsara.session_modes: the unified toggle-command-mode state
machine (switch-word matcher, seam-join heuristic, anti-hallucination gate,
unit-of-work stack, focus-lock, and SessionModeManager dispatch).

All of session_modes.py is pure orchestration -- no audio/Whisper/pyautogui/
Qt mocking needed. Side effects are plain Mock() callables.
"""
import pytest
from unittest.mock import Mock, call

from samsara.session_modes import (
    SessionMode,
    SwitchMatch,
    UtteranceSignals,
    StackItem,
    UnitOfWorkStack,
    SessionModeManager,
    normalize_utterance,
    is_scratch_that,
    is_dictate_commit,
    match_switch_word,
    passes_switch_anti_hallucination_gate,
    seam_join,
    chunk_ends_terminal,
    check_focus_lock,
    detect_stage_reference,
    is_substantive_utterance,
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

    def test_prefix_form_preserves_internal_whitespace_runs(self):
        # Payload reconstruction is a SLICE of the original text, not a
        # token split/join -- a double space (or tab) inside the payload
        # must survive verbatim per the preserve-formatting contract.
        m = match_switch_word("dictate hello  world")
        assert m is not None
        assert m.payload == "hello  world"

    def test_prefix_form_preserves_tabs_in_payload(self):
        m = match_switch_word("dictate col1\tcol2")
        assert m is not None
        assert m.payload == "col1\tcol2"


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
# is_substantive_utterance (AVA mode substance gate, Phase 2.5)
# ---------------------------------------------------------------------------

class TestIsSubstantiveUtterance:
    @pytest.mark.parametrize("text", [
        "uh",
        "you",
        "um uh",         # 2 tokens, both still filler
        "hm",
        "cat",           # 3-char string
        "xyz",           # 3-char string
        "",
        "   ",
        "um",
        "the a",         # 2 tokens, both filler
    ])
    def test_rejects_micro_utterances(self, text):
        assert is_substantive_utterance(text) is False

    @pytest.mark.parametrize("text", [
        "no",
        "stop",
        "yes",
        "continue",
        "why",
        "how",
        "sure",
        "wait",
        "thanks",
        "maybe",
        "hello",
        "hi",
        "what time is it",
        "submit that",
        "tell me a joke",
        "How Are You",     # case-insensitive, still 3 real words
    ])
    def test_accepts_substantive_utterances(self, text):
        assert is_substantive_utterance(text) is True

    def test_one_word_allowlist_overrides_length_rule(self):
        # "no" is 2 characters -- shorter than the 4-char minimum -- but the
        # allowlist exception must win; it's a complete, meaningful turn.
        assert is_substantive_utterance("no") is True

    def test_one_word_non_allowlisted_short_word_rejected(self):
        assert is_substantive_utterance("go") is False

    def test_two_short_words_still_rejected_by_length(self):
        # 2 tokens (passes the word-count rule) but only 3 characters total --
        # the length rule catches what the word-count rule alone would miss.
        assert is_substantive_utterance("a b") is False

    def test_punctuation_and_case_do_not_affect_result(self):
        assert is_substantive_utterance("Submit That!") is True
        assert is_substantive_utterance("Uh...") is False

    def test_yeah_okay_is_a_legitimate_assent_turn(self):
        # "okay" was deliberately removed from _SUBSTANCE_FILLER_TOKENS --
        # an assent/ack like "yeah okay" is a real AVA turn, not a stray
        # syllable, even though "yeah" alone is still filler.
        assert is_substantive_utterance("yeah okay") is True
        assert is_substantive_utterance("okay yeah") is True

    def test_um_uh_still_rejected(self):
        # Both tokens remain in the filler set -- unlike "yeah okay", this
        # has no assent content and must still be rejected.
        assert is_substantive_utterance("um uh") is False

    def test_none_input_rejected(self):
        assert is_substantive_utterance(None) is False


# ---------------------------------------------------------------------------
# SessionModeManager integration: fixtures + dispatch behavior
# ---------------------------------------------------------------------------

GOOD_SIGNALS = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


@pytest.fixture
def manager_factory():
    """Returns a (manager, mocks) builder so each test can override callables."""
    def _build(foreground="notepad.exe", foreground_hwnd=12345, abort_phrases=None,
               command_matches=None, format_dictate_fn=None,
               buffer_dictate_until_commit=False):
        mocks = {
            "foreground": Mock(return_value=foreground),
            "foreground_hwnd": Mock(return_value=foreground_hwnd),
            "inject": Mock(),
            "remove_chars": Mock(),
            "command_dispatch": Mock(return_value=CommandDispatchResultFor(command_matches)),
            "agent_dispatch": Mock(),
            "on_mode_change": Mock(),
            "on_focus_lock_revert": Mock(),
            "on_scratch_result": Mock(),
            "on_abort": Mock(),
            "on_switch_dispatch_error": Mock(),
        }
        mgr = SessionModeManager(
            abort_phrases=abort_phrases if abort_phrases is not None else ["cancel", "abort"],
            foreground_exe_resolver=mocks["foreground"],
            foreground_hwnd_resolver=mocks["foreground_hwnd"],
            inject_fn=mocks["inject"],
            format_dictate_fn=format_dictate_fn,
            remove_chars_fn=mocks["remove_chars"],
            command_dispatch_fn=mocks["command_dispatch"],
            agent_dispatch_fn=mocks["agent_dispatch"],
            on_mode_change=mocks["on_mode_change"],
            on_focus_lock_revert=mocks["on_focus_lock_revert"],
            on_scratch_result=mocks["on_scratch_result"],
            on_abort=mocks["on_abort"],
            on_switch_dispatch_error=mocks["on_switch_dispatch_error"],
            buffer_dictate_until_commit=buffer_dictate_until_commit,
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

    def test_word_boundary_abort_matches_even_with_bad_gate_signals_from_all_lanes(self, manager_factory):
        # Abort must be reachable, gate-free, from every lane -- a user with
        # degraded audio can never be gated out of escaping a stuck session.
        bad_signals = UtteranceSignals(has_contiguous_speech=None, compression_ratios=())
        for mode in (SessionMode.COMMAND, SessionMode.DICTATE, SessionMode.AVA):
            mgr, mocks = manager_factory()
            mgr.force_mode(mode)
            outcome = mgr.dispatch_utterance("abort", bad_signals)
            assert outcome.kind == "abort", f"abort not reachable from {mode!r}"
            mocks["on_abort"].assert_called_once()

    def test_ordinary_dictation_word_is_not_mistaken_for_abort(self, manager_factory):
        # "report" was flagged as a plausible substring false-positive during
        # review; regardless of the exact mechanics it must never abort.
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("report", GOOD_SIGNALS)
        assert outcome.kind != "abort"
        mocks["on_abort"].assert_not_called()
        mocks["inject"].assert_called_once_with("report")

    def test_word_that_contains_abort_phrase_as_substring_does_not_abort(self, manager_factory):
        # Old behavior: phrase.lower() in text_lower -- "cancelation" (a
        # real, if informal, misspelling) contains "cancel" as a literal
        # substring and would incorrectly abort. Word-boundary matching
        # must not fire here.
        mgr, mocks = manager_factory(foreground="notepad.exe", abort_phrases=["cancel", "abort"])
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("file a cancelation request", GOOD_SIGNALS)
        assert outcome.kind != "abort"
        mocks["on_abort"].assert_not_called()

    def test_word_boundary_abort_still_matches_the_exact_word(self, manager_factory):
        mgr, mocks = manager_factory(abort_phrases=["cancel", "abort"])
        outcome = mgr.dispatch_utterance("please cancel", GOOD_SIGNALS)
        assert outcome.kind == "abort"
        mocks["on_abort"].assert_called_once()

    def test_multi_word_abort_phrase_matches_at_word_boundaries(self, manager_factory):
        mgr, mocks = manager_factory(abort_phrases=["cancel dictation"])
        outcome = mgr.dispatch_utterance("please cancel dictation now", GOOD_SIGNALS)
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

    def test_prefix_switch_payload_double_space_survives_verbatim(self, manager_factory):
        # Payload delivery must preserve internal formatting exactly, same
        # contract as match_switch_word's own payload extraction.
        mgr, mocks = manager_factory(foreground="notepad.exe")
        outcome = mgr.dispatch_utterance("dictate hello  there", GOOD_SIGNALS)
        assert outcome.kind == "dictate_injected"
        mocks["inject"].assert_called_once_with("hello  there")

    def test_prefix_switch_dispatch_exception_reverts_mode_and_reports_failure(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mocks["inject"].side_effect = RuntimeError("paste failed")
        outcome = mgr.dispatch_utterance("dictate hello there", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.COMMAND  # reverted to the mode active before the switch
        assert outcome.kind == "prefix_switch_failed"
        mocks["on_switch_dispatch_error"].assert_called_once()

    def test_prefix_switch_dispatch_exception_reverts_to_prior_non_command_mode(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.AVA)
        mocks["inject"].side_effect = RuntimeError("paste failed")
        outcome = mgr.dispatch_utterance("dictate hello there", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.AVA  # reverted to whatever was active, not just COMMAND
        assert outcome.kind == "prefix_switch_failed"

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

    # -- format_dictate_fn wiring (formatting-tokens feature) -------------

    def test_format_dictate_fn_applied_after_seam_join_before_injection(self, manager_factory):
        upper = lambda t: t.upper()
        mgr, mocks = manager_factory(foreground="notepad.exe", format_dictate_fn=upper)
        mgr.force_mode(SessionMode.DICTATE)
        outcome = mgr.dispatch_utterance("hello world", GOOD_SIGNALS)
        assert outcome.kind == "dictate_injected"
        mocks["inject"].assert_called_once_with("HELLO WORLD")

    def test_format_dictate_fn_result_used_for_stage_buffer(self, manager_factory):
        replace_with_marker = lambda t: "<<FORMATTED>>"
        mgr, mocks = manager_factory(foreground="notepad.exe", format_dictate_fn=replace_with_marker)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("hello world", GOOD_SIGNALS)
        assert mgr.stage_buffer == "<<FORMATTED>>"

    def test_format_dictate_fn_result_used_for_scratch_that_undo_length(self, manager_factory):
        # Scratch-that must backspace the length of what was ACTUALLY typed
        # (post-formatting), not the raw spoken words.
        shorten = lambda t: "X"
        mgr, mocks = manager_factory(foreground="notepad.exe", format_dictate_fn=shorten)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.COMMAND)
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_success"
        mocks["remove_chars"].assert_called_once_with(1)  # len("X"), not len("hello world")

    def test_format_dictate_fn_defaults_to_identity_when_not_supplied(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")  # no format_dictate_fn
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("hello world", GOOD_SIGNALS)
        mocks["inject"].assert_called_once_with("hello world")

    def test_format_dictate_fn_applied_to_suppressed_payload_for_later_retype(self, manager_factory):
        upper = lambda t: t.upper()
        mgr, mocks = manager_factory(foreground="notepad.exe", format_dictate_fn=upper)
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "chrome.exe"  # focus drifted -- suppressed
        mgr.dispatch_utterance("lost text", GOOD_SIGNALS)
        mocks["inject"].assert_not_called()

        mocks["foreground"].return_value = "notepad.exe"  # focus restored
        result = mgr.retype_last_suppressed()
        assert result is True
        mocks["inject"].assert_called_once_with("LOST TEXT")  # formatted, not raw

    def test_seam_join_resets_after_terminal_punctuation(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello there.", GOOD_SIGNALS)
        mgr.dispatch_utterance("How are you", GOOD_SIGNALS)
        assert mocks["inject"].call_args_list[1].args[0] == " How are you"

    def test_focus_lock_suppresses_and_retains_dictate(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        assert mgr.mode is SessionMode.DICTATE
        # foreground drifts to a different app before the injection attempt
        mocks["foreground"].return_value = "chrome.exe"
        outcome = mgr.dispatch_utterance("this should not land", GOOD_SIGNALS)
        assert outcome.kind == "dictate_suppressed_focus_lock"
        mocks["inject"].assert_not_called()
        mocks["on_focus_lock_revert"].assert_called_once()
        assert mgr.mode is SessionMode.DICTATE
        assert outcome.detail["target_process"] == "notepad.exe"
        assert outcome.detail["foreground"] == "chrome.exe"
        assert outcome.detail["mode_retained"] is SessionMode.DICTATE
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

    def test_scratch_that_refuses_on_hwnd_mismatch_even_with_same_exe(self, manager_factory):
        # Same exe name (notepad.exe) on both sides -- the exe-name check
        # alone would pass -- but a DIFFERENT window handle (e.g. a second
        # Notepad window) must still refuse the destructive undo rather
        # than delete text in the wrong window.
        mgr, mocks = manager_factory(foreground="notepad.exe", foreground_hwnd=111)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.COMMAND)
        mocks["foreground_hwnd"].return_value = 222  # different window, same exe
        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)
        assert outcome.kind == "scratch_refuse"
        mocks["remove_chars"].assert_not_called()
        mocks["on_scratch_result"].assert_called_once_with(False)

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

    # -- substance gate (Phase 2.5) --------------------------------------

    def test_non_substantive_utterance_never_reaches_agent(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("uh", GOOD_SIGNALS)
        assert outcome.kind == "ava_rejected_not_substantive"
        mocks["agent_dispatch"].assert_not_called()

    def test_substantive_utterance_dispatches_normally(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("what time is it", GOOD_SIGNALS)
        assert outcome.kind == "ava_dispatched"
        mocks["agent_dispatch"].assert_called_once_with("what time is it", None)

    def test_rejected_utterance_does_not_consume_stage_buffer(self, manager_factory):
        """A rejected micro-utterance must not clear the stage buffer --
        it never even reaches the stage-reference check."""
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Hello world", GOOD_SIGNALS)
        mgr.force_mode(SessionMode.AVA)
        mgr.dispatch_utterance("uh", GOOD_SIGNALS)
        assert mgr.stage_buffer == "Hello world"
        mocks["agent_dispatch"].assert_not_called()

    def test_one_word_allowlisted_utterance_dispatches(self, manager_factory):
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("no", GOOD_SIGNALS)
        assert outcome.kind == "ava_dispatched"
        mocks["agent_dispatch"].assert_called_once_with("no", None)

    def test_switch_word_in_ava_not_consumed_by_substance_gate(self, manager_factory):
        """Dispatch order is unchanged: switch words are matched in
        dispatch_utterance() BEFORE _dispatch_in_mode()/_dispatch_ava() ever
        runs, so the substance gate never even sees "command mode" -- it
        can't eat it, regardless of how short it might otherwise look."""
        mgr, mocks = manager_factory()
        mgr.force_mode(SessionMode.AVA)
        outcome = mgr.dispatch_utterance("command mode", GOOD_SIGNALS)
        assert outcome.kind == "mode_switch"
        assert mgr.mode is SessionMode.COMMAND
        mocks["agent_dispatch"].assert_not_called()


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

    def test_reset_after_focus_lock_mismatch_returns_next_session_to_command(self, manager_factory):
        mgr, mocks = manager_factory(foreground="notepad.exe")
        mgr.force_mode(SessionMode.DICTATE)
        mocks["foreground"].return_value = "chrome.exe"
        mgr.dispatch_utterance("lost", GOOD_SIGNALS)
        assert mgr.mode is SessionMode.DICTATE

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

# ---------------------------------------------------------------------------
# Buffered, explicit-commit DICTATE lane
# ---------------------------------------------------------------------------

class TestBufferedDictateCommit:
    def test_end_is_whole_utterance_only(self):
        assert is_dictate_commit("end") is True
        assert is_dictate_commit("End!") is True
        assert is_dictate_commit("the end is near") is False
        assert is_dictate_commit("weekend") is False

    def test_pauses_stage_chunks_without_pasting(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)

        first = mgr.dispatch_utterance("This is one thought", GOOD_SIGNALS)
        second = mgr.dispatch_utterance("And it continues.", GOOD_SIGNALS)

        assert first.kind == "dictate_staged"
        assert second.kind == "dictate_staged"
        assert mgr.dictate_pending_buffer == "This is one thought and it continues."
        mocks["inject"].assert_not_called()

    def test_end_pastes_complete_thought_once_and_stays_in_dictate(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("First sentence.", GOOD_SIGNALS)
        mgr.dispatch_utterance("Second sentence.", GOOD_SIGNALS)

        outcome = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        assert outcome.kind == "dictate_committed"
        assert outcome.detail["text"] == "First sentence. Second sentence."
        mocks["inject"].assert_called_once_with("First sentence. Second sentence.")
        assert mgr.mode is SessionMode.DICTATE
        assert mgr.dictate_pending_buffer == ""
        assert mgr.stage_buffer == "First sentence. Second sentence."

    def test_next_thought_relocks_to_newly_focused_window(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("First box", GOOD_SIGNALS)
        first = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        mocks["foreground"].return_value = "chrome.exe"
        mocks["foreground_hwnd"].return_value = 99999
        mgr.dispatch_utterance("Second box", GOOD_SIGNALS)
        second = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        assert first.kind == "dictate_committed"
        assert second.kind == "dictate_committed"
        assert mgr.mode is SessionMode.DICTATE
        assert mocks["inject"].call_args_list == [
            call("First box"),
            call("Second box"),
        ]

    def test_empty_end_keeps_persistent_dictate_active(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)

        outcome = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        assert outcome.kind == "dictate_committed"
        assert outcome.detail["empty"] is True
        assert mgr.mode is SessionMode.DICTATE
        mocks["inject"].assert_not_called()

    def test_distrusted_end_is_not_added_to_pending_text(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Keep this thought", GOOD_SIGNALS)
        distrusted = UtteranceSignals(has_contiguous_speech=False, compression_ratios=(1.2,))

        outcome = mgr.dispatch_utterance("end", distrusted)

        assert outcome.kind == "dictate_commit_refused"
        assert mgr.mode is SessionMode.DICTATE
        assert mgr.dictate_pending_buffer == "Keep this thought"
        mocks["inject"].assert_not_called()

    def test_commit_focus_mismatch_retains_thought_and_stays_dictate(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Do not lose this", GOOD_SIGNALS)
        mocks["foreground_hwnd"].return_value = 99999

        outcome = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        assert outcome.kind == "dictate_commit_blocked_focus_lock"
        assert mgr.mode is SessionMode.DICTATE
        assert mgr.dictate_pending_buffer == "Do not lose this"
        mocks["inject"].assert_not_called()
        mocks["on_focus_lock_revert"].assert_called_once()

    def test_reported_paste_failure_retains_thought(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mocks["inject"].return_value = False
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Keep this safe", GOOD_SIGNALS)

        outcome = mgr.dispatch_utterance("end", GOOD_SIGNALS)

        assert outcome.kind == "dictate_commit_failed"
        assert mgr.mode is SessionMode.DICTATE
        assert mgr.dictate_pending_buffer == "Keep this safe"
        mocks["on_switch_dispatch_error"].assert_called_once()

    def test_scratch_removes_last_staged_chunk_without_editor_keystrokes(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Keep this", GOOD_SIGNALS)
        mgr.dispatch_utterance("Remove this", GOOD_SIGNALS)

        outcome = mgr.dispatch_utterance("scratch that", GOOD_SIGNALS)

        assert outcome.kind == "scratch_success"
        assert mgr.dictate_pending_buffer == "Keep this"
        mocks["remove_chars"].assert_not_called()
        mocks["inject"].assert_not_called()

    def test_command_switch_commits_pending_text_before_switching(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Commit before switching", GOOD_SIGNALS)

        outcome = mgr.dispatch_utterance("command mode", GOOD_SIGNALS)

        assert outcome.kind == "mode_switch"
        mocks["inject"].assert_called_once_with("Commit before switching")
        assert mgr.mode is SessionMode.COMMAND

    def test_pending_edit_compare_and_swap_commits_replacement(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("This sentence is much too long", GOOD_SIGNALS)

        outcome = mgr.apply_and_commit_pending_edit(
            expected_source="This sentence is much too long",
            replacement="This is concise.",
        )

        assert outcome.kind == "dictate_committed"
        assert outcome.detail["edited"] is True
        assert outcome.detail["source_chars"] == len("This sentence is much too long")
        mocks["inject"].assert_called_once_with("This is concise.")
        assert mgr.dictate_pending_buffer == ""
        assert mgr.stage_buffer == "This is concise."
        assert mgr.mode is SessionMode.DICTATE

    def test_pending_edit_rejects_stale_source_without_mutation(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Original thought", GOOD_SIGNALS)
        mgr.dispatch_utterance("More speech arrived", GOOD_SIGNALS)

        outcome = mgr.apply_and_commit_pending_edit(
            expected_source="Original thought",
            replacement="Stale rewrite",
        )

        assert outcome.kind == "dictate_edit_stale"
        assert outcome.detail["reason"] == "source_changed"
        assert mgr.dictate_pending_buffer == "Original thought more speech arrived"
        mocks["inject"].assert_not_called()

    def test_pending_edit_rejects_empty_replacement_without_mutation(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Keep the original", GOOD_SIGNALS)

        outcome = mgr.apply_and_commit_pending_edit(
            expected_source="Keep the original",
            replacement="   ",
        )

        assert outcome.kind == "dictate_edit_refused"
        assert outcome.detail["reason"] == "empty_replacement"
        assert mgr.dictate_pending_buffer == "Keep the original"
        mocks["inject"].assert_not_called()

    def test_pending_edit_focus_failure_retains_replacement_for_retry(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Rewrite this", GOOD_SIGNALS)
        mocks["foreground_hwnd"].return_value = 99999

        outcome = mgr.apply_and_commit_pending_edit(
            expected_source="Rewrite this",
            replacement="Rewritten safely.",
        )

        assert outcome.kind == "dictate_commit_blocked_focus_lock"
        assert outcome.detail["edited"] is True
        assert mgr.dictate_pending_buffer == "Rewritten safely."
        mocks["inject"].assert_not_called()

    def test_pending_edit_paste_failure_retains_replacement_for_retry(self, manager_factory):
        mgr, mocks = manager_factory(buffer_dictate_until_commit=True)
        mocks["inject"].return_value = False
        mgr.force_mode(SessionMode.DICTATE)
        mgr.dispatch_utterance("Rewrite this", GOOD_SIGNALS)

        outcome = mgr.apply_and_commit_pending_edit(
            expected_source="Rewrite this",
            replacement="Rewritten safely.",
        )

        assert outcome.kind == "dictate_commit_failed"
        assert outcome.detail["edited"] is True
        assert mgr.dictate_pending_buffer == "Rewritten safely."
