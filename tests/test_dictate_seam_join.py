"""Queue 81: staged DICTATE fragments that Whisper marked as one sentence
split by a pause are joined when staged.

Fixtures are the owner's own staged fragments from the 2026-09-15 14:58-15:00
log (dictation pause 0.65 s). The join only decides seams whose previous
fragment ends in an ellipsis; every other seam keeps its existing handling.
"""

from unittest.mock import Mock

import pytest

from samsara.formatting_tokens import apply_formatting_tokens
from samsara.session_modes import (
    CommandDispatchResult,
    SessionMode,
    SessionModeManager,
    UtteranceSignals,
    decide_ellipsis_seam,
)

GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.3,))


def _manager(command_matches=None):
    inject = Mock(side_effect=lambda text, guard=None: text)
    result = (CommandDispatchResult(matched=True, phrase=command_matches)
              if command_matches else CommandDispatchResult(matched=False))
    mgr = SessionModeManager(
        abort_phrases=["cancel"],
        foreground_exe_resolver=Mock(return_value="notepad.exe"),
        foreground_hwnd_resolver=Mock(return_value=1),
        inject_fn=inject, remove_chars_fn=Mock(),
        command_dispatch_fn=Mock(return_value=result), agent_dispatch_fn=Mock(),
        buffer_dictate_until_commit=True, clock=lambda: 0.0,
    )
    mgr.force_mode(SessionMode.DICTATE)
    return mgr, inject


def _staged(*fragments):
    mgr, _ = _manager()
    for fragment in fragments:
        assert mgr.dispatch_utterance(fragment, GOOD).kind == "dictate_staged", fragment
    return mgr.dictate_pending_buffer


# -- the six evidence pairs ---------------------------------------------------

EVIDENCE = [
    pytest.param(
        ["I'm looking at the...", "Dictation pause length."],
        "I'm looking at the Dictation pause length.", "ellipsis_determiner", id="P1-the"),
    pytest.param(
        ["And it says, lowering it makes...", "hands-free, break sentences into more and more pieces."],
        "And it says, lowering it makes hands-free, break sentences into more and more pieces.",
        "ellipsis_lowercase", id="P2-makes"),
    pytest.param(
        ["So how do we...", "deal with that?"],
        "So how do we deal with that?", "ellipsis_lowercase", id="P3-do-we"),
    pytest.param(
        ["Cause like, I want it to be fast, but..", "I want the sentences to, you know, be cohesive."],
        "Cause like, I want it to be fast, but I want the sentences to, you know, be cohesive.",
        "ellipsis_conjunction_i", id="P4-but-I"),
    pytest.param(
        ["I thought, whisper, like..", "put the sentences together properly."],
        "I thought, whisper, like put the sentences together properly.",
        "ellipsis_lowercase", id="P5-like"),
    pytest.param(
        ["If it's just like a limitation of whisper, maybe we should look into that.",
         "other model we were talking about adopting."],
        "If it's just like a limitation of whisper, maybe we should look into that other model "
        "we were talking about adopting.",
        None, id="P6-period-lowercase"),
]


@pytest.mark.parametrize("fragments, expected, clause", EVIDENCE)
def test_evidence_pair_joins(fragments, expected, clause):
    assert _staged(*fragments) == expected
    decision = decide_ellipsis_seam(fragments[0], fragments[1], fragments[0])
    if clause is None:
        # No ellipsis: the pre-existing single-full-stop continuation joins it.
        assert decision is None
    else:
        assert decision.join and decision.clause == clause


def test_third_fragment_after_a_full_stop_and_capital_stays_its_own_sentence():
    """'put the sentences together properly.' + 'Like, intelligently.' -- a full
    stop and a capital agree on a boundary, so it is left alone. (Before queue
    81 the capital filler "Like" was skipped and the lower-case word after it
    counted as a continuation.)"""
    assert _staged("I thought, whisper, like..", "put the sentences together properly.",
                   "Like, intelligently.") == \
        "I thought, whisper, like put the sentences together properly. Like, intelligently."


@pytest.mark.parametrize("fragments, expected", [
    pytest.param(["The damage went to Verizon.", "So, he took the old phone."],
                 "The damage went to Verizon. So, he took the old phone.", id="So-comma"),
    pytest.param(["Instead of using their app or whatever.", "So, what's the solution there?"],
                 "Instead of using their app or whatever. So, what's the solution there?", id="So-question"),
    pytest.param(["Buh.", "Okay, now I'm reading the description."],
                 "Buh. Okay, now I'm reading the description.", id="Okay"),
    pytest.param(["Important parts of the app.", "Like, you're not gonna have the home screen up."],
                 "Important parts of the app. Like, you're not gonna have the home screen up.", id="Like"),
])
def test_capitalised_filler_after_a_full_stop_is_a_new_sentence(fragments, expected):
    """Owner-log seams the old filler skip welded together."""
    assert _staged(*fragments) == expected


def test_lowercase_filler_after_a_full_stop_still_continues():
    assert _staged("I went to the.", "um store yesterday") == "I went to the um store yesterday"


def test_the_whole_owner_message_stages_cohesively():
    fragments = [
        "And yet, all of a sudden, just that one message...", "was really bad, in that regard.",
        "I'm looking at the...", "Dictation pause length.", "It's at .65 seconds.",
        "And it says, lowering it makes...",
        "hands-free, break sentences into more and more pieces. Which is also not good.",
        "Um...", "So how do we...", "deal with that?",
    ]
    assert _staged(*fragments) == (
        "And yet, all of a sudden, just that one message was really bad, in that regard. "
        "I'm looking at the Dictation pause length. It's at .65 seconds. "
        "And it says, lowering it makes hands-free, break sentences into more and more pieces. "
        "Which is also not good. Um So how do we deal with that?"
    )


# -- boundaries that must NOT join -------------------------------------------

@pytest.mark.parametrize("fragments, expected", [
    pytest.param(["It's at .65 seconds.", "And it says, lowering it makes..."],
                 "It's at .65 seconds. And it says, lowering it makes...", id="stop-then-capital"),
    pytest.param(["Speaking of which...", "We need to put..."],
                 "Speaking of which... We need to put...", id="ellipsis-then-capital"),
    pytest.param(["the...", "There's the six little squares showing apps."],
                 "the... There's the six little squares showing apps.", id="determiner-then-restart"),
    pytest.param(["the...", "I think that is fine."],
                 "the... I think that is fine.", id="determiner-then-pronoun"),
    pytest.param(["It was great...", "I loved it."],
                 "It was great... I loved it.", id="I-after-non-conjunction"),
    pytest.param(["That is all?", "deal with it later"],
                 "That is all? deal with it later", id="question-mark-kept"),
])
def test_genuine_boundary_does_not_join(fragments, expected):
    assert _staged(*fragments) == expected


def test_seams_without_an_ellipsis_are_not_decided_here():
    assert decide_ellipsis_seam("I went to the.", "store yesterday") is None
    assert decide_ellipsis_seam("This is one thought", "And it continues.") is None
    assert decide_ellipsis_seam("", "anything") is None


def test_name_after_the_keeps_its_capital():
    assert _staged("And when I looked at the..", "GitHub for the SDK, it doesn't look like an app.") == \
        "And when I looked at the GitHub for the SDK, it doesn't look like an app."


def test_unicode_ellipsis_character_is_a_continuation_marker():
    assert _staged("So how do we…", "deal with that?") == "So how do we deal with that?"


# -- the last fragment keeps its punctuation ---------------------------------

def test_trailing_ellipsis_at_the_end_of_the_buffer_survives_commit():
    mgr, inject = _manager()
    mgr.dispatch_utterance("So how do we...", GOOD)
    mgr.dispatch_utterance("deal with it and then...", GOOD)
    assert mgr.dictate_pending_buffer == "So how do we deal with it and then..."
    outcome = mgr.dispatch_utterance("end", GOOD)
    assert outcome.kind == "dictate_committed"
    delivered = inject.call_args.args[0]
    assert delivered == "So how do we deal with it and then..."


def test_scratch_that_restores_the_exact_ellipsis():
    mgr, _ = _manager()
    mgr.dispatch_utterance("So how do we..", GOOD)
    mgr.dispatch_utterance("deal with that?", GOOD)
    assert mgr.dictate_pending_buffer == "So how do we deal with that?"
    mgr.dispatch_utterance("scratch that", GOOD)
    assert mgr.dictate_pending_buffer == "So how do we.."
    # And the next continuation joins again from the restored state.
    mgr.dispatch_utterance("handle it?", GOOD)
    assert mgr.dictate_pending_buffer == "So how do we handle it?"


# -- formatting tokens (queue 53) --------------------------------------------

@pytest.mark.parametrize("fragments, expected", [
    # No ellipsis seam: staging is unchanged, so the tokens see what they saw before.
    pytest.param(["first line new line", "second line"], "first line\nsecond line", id="inline-new-line"),
    pytest.param(["he said open quote hello", "world close quote"], 'he said "hello world"', id="quote-pair"),
    pytest.param(["This is the title in quotes"], '"This is the title"', id="trailing-in-quotes"),
    pytest.param(["This is the title.", "And so it is in bold"], "**This is the title. And so it is**",
                 id="trailing-in-bold-whole-thought"),
    # An ellipsis seam before a token: the tokens apply to the joined text.
    pytest.param(["he said open quote hello...", "world close quote"], 'he said "hello world"',
                 id="quote-pair-across-ellipsis"),
    pytest.param(["That is the whole...", "title in quotes"], '"That is the whole title"',
                 id="trailing-in-quotes-across-ellipsis"),
    pytest.param(["Ends like this...", "In quotes"], '"Ends like this..."', id="capital-trigger-kept-separate"),
])
def test_formatting_tokens_over_the_staged_thought(fragments, expected):
    assert apply_formatting_tokens(_staged(*fragments)) == expected


# -- commands are not dictation ----------------------------------------------

def test_command_mode_utterance_is_passed_through_untouched():
    mgr, _ = _manager(command_matches="open chrome")
    mgr.force_mode(SessionMode.COMMAND)
    outcome = mgr.dispatch_utterance("open chrome...", GOOD)
    assert outcome.kind == "command_executed"
    mgr._command_dispatch_fn.assert_called_once_with("open chrome...")
    assert mgr.dictate_pending_buffer == ""


def test_commit_word_after_an_ellipsis_is_not_joined_into_the_text():
    mgr, inject = _manager()
    mgr.dispatch_utterance("I'm looking at the...", GOOD)
    outcome = mgr.dispatch_utterance("end", GOOD)
    assert outcome.kind == "dictate_committed"
    assert inject.call_args.args[0] == "I'm looking at the..."
