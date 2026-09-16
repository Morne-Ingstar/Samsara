"""Queue 135: a continuation fragment that STARTS with an ellipsis must join.

Whisper marks a trailed-off continuation by ending the previous fragment with an
ellipsis AND starting the next one with another. The leading one is a decoder
artifact, and it used to defeat the very clause meant to catch this case: the
first token was "...at", so `first[0].isalpha()` was False and the lowercase
clause never fired.

Cases below are the owner's real seams from the live log, 2026-09-16 12:09,
which committed as:

    So I was...... at the store. And then...... this guy, like...... just kept
    saying...... mean things.
"""
import pytest

from samsara.session_modes import decide_ellipsis_seam

JOINS = [
    ("So I was...", "...at the store."),
    ("So I was... at the store. And then...", "...this guy, like..."),
    ("...this guy, like...", "...just kept saying..."),
    ("...just kept saying...", "...mean things."),
    ("One thing I noticed is when you send a window...", "...to the left or right"),
    ("...it moves, but...", "...it."),
    ("the thing is…", "…it works"),          # unicode ellipsis, both sides
    ("so we tried..", "..again later"),      # two dots, the other real form
]

SEPARATES = [
    ("I thought...", "I told you about the problem."),
    ("We can't demo with that still being an issue, so...", "Get with it."),
    ("that was the plan...", "Anyway, it changed."),
]


@pytest.mark.parametrize("previous,new", JOINS)
def test_a_leading_ellipsis_continuation_joins(previous, new):
    seam = decide_ellipsis_seam(previous, new)
    assert seam is not None and seam.join, seam
    assert seam.lead_stripped, "the leading ellipsis must be reported as stripped"


@pytest.mark.parametrize("previous,new", SEPARATES)
def test_a_real_new_sentence_still_separates(previous, new):
    seam = decide_ellipsis_seam(previous, new)
    assert seam is not None and not seam.join, seam


def test_the_leading_ellipsis_is_stripped_even_when_separating():
    seam = decide_ellipsis_seam("that was the plan...", "...Anyway, it changed.")
    assert seam is not None
    assert seam.lead_stripped == "..."


def test_a_chunk_that_is_only_an_ellipsis_is_still_empty():
    seam = decide_ellipsis_seam("something...", "...")
    assert seam is not None and seam.clause == "empty" and not seam.join


def test_no_trailing_ellipsis_is_still_not_this_path():
    assert decide_ellipsis_seam("a normal sentence.", "Another one.") is None
