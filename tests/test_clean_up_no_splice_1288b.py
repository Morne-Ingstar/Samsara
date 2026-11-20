"""Ordinary sentence breaks and pause continuations through offline cleanup."""

import pytest

from samsara.ava_edit import sanitize, selection


@pytest.mark.parametrize("source,expected", [
    pytest.param(
        "The form has a little arrow that you click on. Could we move the arrow.",
        "The form has a little arrow that you click on. Could we move the arrow.",
        id="question-after-relative-with-particle",
    ),
    pytest.param(
        "When the train arrived, tired from standing all day, I went home. It's on the north side.",
        "When the train arrived, tired from standing all day, I went home. It's on the north side.",
        id="contraction-after-fronted-clause-with-aside",
    ),
    pytest.param(
        "And I wrote down all of this. Let me know if you need a copy.",
        "And I wrote down all of this. Let me know if you need a copy.",
        id="imperative-after-coordinated-statement",
    ),
    pytest.param(
        "When you open the panel, at the bottom it gives you two buttons, choose one. "
        "There is a second panel below. I can open it for you.",
        "When you open the panel, at the bottom it gives you two buttons, choose one. "
        "There is a second panel below. I can open it for you.",
        id="two-breaks-after-fronted-clause",
    ),
    pytest.param(
        "Can you send a list of... places to visit. Ah Like. parks nearby. or whatever.",
        "Can you send a list of... places to visit. Ah like parks nearby or whatever.",
        id="question-and-spoken-fragments",
    ),
    pytest.param(
        "It showed a notice. And asked if I wanted to restart. It seemed strange, like... I can wait.",
        "It showed a notice and asked if I wanted to restart. It seemed strange, like... I can wait.",
        id="coordinated-predicate-and-ellipsis",
    ),
    pytest.param(
        "Check one, two. And... and i sent the copy before noon.",
        "Check one, two. And and i sent the copy before noon.",
        id="ellipsis-after-repeated-coordinator",
    ),
    pytest.param(
        "But before she reached the gate, Nora lost her keys. But he found them later.",
        "But before she reached the gate, Nora lost her keys. But he found them later.",
        id="coordinator-starting-new-sentence",
    ),
    pytest.param(
        "We see a warning on every update. If you open the panel, you'll see an explanation.",
        "We see a warning on every update. If you open the panel, you'll see an explanation.",
        id="new-if-sentence",
    ),
    pytest.param(
        "I'm, I'm waiting on this... Stubborn.",
        "I'm, I'm waiting on this stubborn.",
        id="ellipsis-before-adjective",
    ),
    pytest.param(
        "The list covers spare parts and stuff like that. While I'm out, please count the screws.",
        "The list covers spare parts and stuff like that. While I'm out, please count the screws.",
        id="new-while-sentence",
    ),
    pytest.param(
        "So this looks good. So I think unless we spot an error, all we need to do is send it.",
        "So this looks good. So I think unless we spot an error, all we need to do is send it.",
        id="new-so-with-condition",
    ),
    pytest.param(
        "The old scanner is unreliable. So let's replace it before Friday.",
        "The old scanner is unreliable. So let's replace it before Friday.",
        id="new-so-imperative",
    ),
    pytest.param(
        "It's like I'm trying to be... Whatever version of me Helps me. "
        "Live with them. Instead of being the person. That wants to stay here.",
        "It's like I'm trying to be... Whatever version of me helps me live with them "
        "instead of being the person that wants to stay here.",
        id="infinitive-and-relative-across-pauses",
    ),
    pytest.param(
        "I switched it off. check the fuse.",
        "I switched it off. check the fuse.",
        id="lowercase-imperative-with-direct-object",
    ),
    pytest.param(
        "I watched you leave. Who drove the truck.",
        "I watched you leave. Who drove the truck.",
        id="wh-subject-question",
    ),
    pytest.param(
        "Put it down. Do what I say.",
        "Put it down. Do what I say.",
        id="successive-imperatives",
    ),
    pytest.param(
        "I needed to. Have my radio repaired.",
        "I needed to have my radio repaired.",
        id="lexical-have-with-past-participle",
    ),
    pytest.param(
        "I went to the. Store and. Bought some milk.",
        "I went to the store and bought some milk.",
        id="determiner-before-ambiguous-verb",
    ),
    pytest.param(
        "The radiator is made of. Cast iron.",
        "The radiator is made of cast iron.",
        id="preposition-before-ambiguous-verb",
    ),
    pytest.param(
        "We can either leave or. Wait till noon.",
        "We can either leave or wait till noon.",
        id="coordinator-before-ambiguous-verb",
    ),
    pytest.param(
        "The job is. Complete.",
        "The job is complete.",
        id="auxiliary-before-ambiguous-verb",
    ),
    pytest.param(
        "When we reached the locked gate, the driver had no idea what to do. It's open now.",
        "When we reached the locked gate, the driver had no idea what to do. It's open now.",
        id="lexical-do-finishes-left-clause",
    ),
])
def test_cleanup_sentence_boundary_regressions(source, expected):
    result = selection.deterministic_cleanup(source)
    assert result == expected
    assert sanitize.word_lock(source, result)
    assert selection.deterministic_cleanup(result) == result


@pytest.mark.parametrize("separate,paused,joined", [
    pytest.param(
        "If the package is late, we can wait. She's still here.",
        "If the package is late. She's still here.",
        "If the package is late, she's still here.",
        id="complete-conditional-or-dependent-lead-in",
    ),
    pytest.param(
        "Before the bus came, even with bags to carry, we walked. Maya left.",
        "We asked for. Maya's umbrella.",
        "We asked for Maya's umbrella.",
        id="ambiguous-past-tense-or-proper-name",
    ),
    pytest.param(
        "I left it on. Open the hatch.",
        "I needed to. Open the hatch.",
        "I needed to open the hatch.",
        id="imperative-or-to-infinitive",
    ),
    pytest.param(
        "It will take time to sort out. Can they finish after noon.",
        "My assistants. Can finish after noon.",
        "My assistants can finish after noon.",
        id="modal-question-or-split-subject",
    ),
    pytest.param(
        "I took the first shift. Do you have the keys.",
        "I had to. Do the inventory.",
        "I had to do the inventory.",
        id="do-question-or-lexical-verb",
    ),
    pytest.param(
        "I came in. Was Maya driving home.",
        "I knew she was. Driving after dark.",
        "I knew she was driving after dark.",
        id="copula-question-or-progressive",
    ),
    pytest.param(
        "I switched the oven off. Do not touch the handle.",
        "You should. Not touch the handle.",
        "You should not touch the handle.",
        id="negative-imperative-or-modal-complement",
    ),
    pytest.param(
        "I left it out. Please include a label.",
        "I asked her to. Include a label.",
        "I asked her to include a label.",
        id="polite-request-or-infinitive",
    ),
    pytest.param(
        "When it gets late, we all turn in. The pilots fly home.",
        "When it gets late. We all turn in.",
        "When it gets late, we all turn in.",
        id="plural-subject-or-main-clause-of-condition",
    ),
    pytest.param(
        "I called to find out. Which tray should I open.",
        "I found the same tray. Which I had set aside.",
        "I found the same tray which I had set aside.",
        id="wh-question-or-relative",
    ),
])
def test_cleanup_distinguishes_sentences_from_pauses(separate, paused, joined):
    for source, expected in ((separate, separate), (paused, joined)):
        result = selection.deterministic_cleanup(source)
        assert result == expected
        assert sanitize.word_lock(source, result)
        assert selection.deterministic_cleanup(result) == result
