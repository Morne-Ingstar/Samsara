"""Explicit cleanup, with test doubles at the provider boundary."""
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import cloud_llm
from samsara.ava_edit import model, pause_cleanup, sanitize, selection, session
from samsara.ava_edit.proposal import Proposal


FENCE = chr(96) * 3


@pytest.mark.parametrize("before,after", [
    pytest.param("It helps me. Live?", "It helps me live.", id="join-and-question-mark"),
    pytest.param("we left they stayed", "We left.  They stayed.", id="split-and-casing"),
    pytest.param("I'd go if they can't.", "I would go if they cannot.", id="expand"),
    pytest.param("She has left.", "She\u2019s left.", id="contract"),
    pytest.param("They are ready.", "They're ready.", id="are"),
    pytest.param("Their going too sea.", "They're going to see.", id="homophones-below-old-floor"),
    pytest.param("Some advise.", "Some advice.", id="one-sound-confusion"),
    pytest.param("The microphonne worked.", "The microphone worked.", id="doubled-letter"),
    pytest.param("Cafe\u0301 au lait.", "CAF\u00c9 au lait.", id="unicode"),
])
def test_word_lock_accepts_only_aligned_corrections(before, after):
    assert sanitize.word_lock(before, after)
    assert sanitize.check(after, before, cleanup=True) is None


@pytest.mark.parametrize("before,after", [
    pytest.param("We went home.", "We finally went home.", id="insertion"),
    pytest.param("It really works.", "It works.", id="deletion"),
    pytest.param("I saw her leave.", "I saw her depart.", id="synonym"),
    pytest.param("I saw their note there.", "I saw there note their.", id="homophone-reorder"),
    pytest.param("I saw their note.", "I saw there their note.", id="near-insertion"),
    pytest.param("The cat saw the dog.", "The dog saw the cat.", id="reordered-nouns"),
    pytest.param("We tried twice.", "We try twice.", id="tense"),
    pytest.param("Send the note.", "Sent the note.", id="near-but-different-tense"),
    pytest.param("I might go.", "I might not go.", id="negation"),
    pytest.param("The price is $3.14.", "The price is $314.", id="decimal"),
    pytest.param("Read https://example.test/a.", "Read https://example.test/a/.", id="url"),
    pytest.param("Stop at -12.", "Stop at 12.", id="sign"),
    pytest.param("The device is a microphxne.", "The device is a microphone.", id="arbitrary-spelling"),
    pytest.param("A long paragraph contains the same words through the very last one.",
                 "A long paragraph contains the same words through the very final one.", id="high-overlap"),
    pytest.param("The rapist left.", "The therapist left.", id="word-boundary"),
])
def test_word_lock_rejects_rewording(before, after):
    assert not sanitize.word_lock(before, after)
    assert sanitize.check(after, before, cleanup=True)


def test_hygiene_unwraps_complete_model_packaging_before_the_lock():
    before = "I need to. Leave?"
    after = "I need to leave."
    raw = ("<think>I should check the rules.</think>\n"
           f"{FENCE}text\n<<<PASSAGE\nHere\u2019s the corrected text:\n"
           f"{after}\nPASSAGE>>>\n{FENCE}")
    assert sanitize.clean(raw, before, cleanup=True) == after
    assert sanitize.check(raw, before, cleanup=True) is None
    assert sanitize.check(raw, before) == "model went off-script"


@pytest.mark.parametrize("raw", [
    "Here is the corrected text: This changes the words.",
    "<think>Unterminated thoughts. I need to leave.",
    "<<<PASSAGE\nI need to leave.",
    f"{FENCE}text\nI need to leave.",
    "I need <think>internal</think> to leave.",
    "I need to leave.\nCan I help with anything else?",
])
def test_hygiene_refuses_unmatched_wrappers_and_explanation(raw):
    assert sanitize.check(raw, "I need to. Leave.", cleanup=True)


def test_cleanup_keeps_real_source_quotation_and_preface():
    source = '"Here is the corrected text: I need to. Leave."'
    result = '"Here is the corrected text: I need to leave."'
    assert sanitize.clean(result, source, cleanup=True) == result
    assert sanitize.check(result, source, cleanup=True) is None


@pytest.mark.parametrize("source,expected", [
    pytest.param("Dr. Smith called me. Because my test came back.",
                 "Dr. Smith called me because my test came back.", id="title"),
    pytest.param("Here is the issue: whenever I pause. It drops a period.",
                 "Here is the issue: whenever I pause, it drops a period.", id="colon"),
    pytest.param("My hands. Hurt all afternoon.", "My hands hurt all afternoon.", id="predicate"),
    pytest.param("I want to fix. The selection bug.", "I want to fix the selection bug.", id="object"),
    pytest.param("I was talking about. The settings page.", "I was talking about the settings page.",
                 id="preposition"),
    pytest.param("I need to talk to. Someone at the. Clinic.", "I need to talk to someone at the clinic.",
                 id="successive"),
    pytest.param("The plan. Go home and rest.", "The plan. Go home and rest.", id="imperative"),
    pytest.param("My mistake. Said the wrong thing.", "My mistake. Said the wrong thing.", id="omitted-subject"),
    pytest.param("I saw Dr. Smith. He waved.", "I saw Dr. Smith. He waved.", id="real-period"),
    pytest.param("Is it done. Yes.", "Is it done. Yes.", id="question-and-answer"),
    pytest.param("The nurses' notes help me. Live with less pain.",
                 "The nurses' notes help me live with less pain.", id="plural-possessive"),
    pytest.param('She said "Wait... Then go." I was working on. The layout.',
                 'She said "Wait... Then go." I was working on the layout.', id="quotation"),
    pytest.param("Use e.g. 3.14 or visit https://example.org/a.txt. I need to. Leave.",
                 "Use e.g. 3.14 or visit https://example.org/a.txt. I need to leave.", id="number-and-url"),
    pytest.param(r"Open C:\Notes\draft.txt and/or check report.txt. I need to. Leave.",
                 r"Open C:\Notes\draft.txt and/or check report.txt. I need to leave.", id="path-and-slash"),
    pytest.param("I finished... She left.", "I finished... She left.", id="real-ellipsis"),
    pytest.param("I need to. Leave before. Monday.", "I need to leave before Monday.", id="calendar"),
    pytest.param("I need to. Call Alice.", "I need to call Alice.", id="proper-name"),
    pytest.param("I set the timer. Ten minutes passed.", "I set the timer. Ten minutes passed.",
                 id="next-sentence"),
    pytest.param("I haven't called in a while. I'm going to call her.",
                 "I haven't called in a while. I'm going to call her.", id="terminal-while"),
    pytest.param("I could start another one. If I do, it will run for an hour.",
                 "I could start another one. If I do, it will run for an hour.", id="new-conditional"),
    pytest.param("We were going the other way around. In fact, we were early.",
                 "We were going the other way around. In fact, we were early.", id="new-discourse-marker"),
    pytest.param("I can send it to Spark. If not, then It's ready to print. You choose.",
                 "I can send it to Spark. If not, then it's ready to print. You choose.",
                 id="elliptical-condition"),
    pytest.param("If it works, we can ship it. You choose.",
                 "If it works, we can ship it. You choose.", id="after-completed-condition"),
    pytest.param("The radio test is complete. Over.",
                 "The radio test is complete. Over.", id="sign-off"),
    pytest.param("I sent A note To Codex And Desktop Commander.",
                 "I sent a note to Codex and Desktop Commander.", id="inline-function-words-and-names"),
    pytest.param("I asked Will Smith if he Will join.",
                 "I asked Will Smith if he will join.", id="name-and-modal"),
    pytest.param("I stayed home. Because I had the day off, I finished my book.",
                 "I stayed home. Because I had the day off, I finished my book.",
                 id="new-causal-sentence"),
    pytest.param("I can finally make the layout. flexible.",
                 "I can finally make the layout flexible.", id="lowercase-complement"),
    pytest.param("I waited by the door. she opened it.",
                 "I waited by the door. she opened it.", id="lowercase-independent"),
    pytest.param("It opens Quick Reference. And it works in Helper.",
                 "It opens Quick Reference and it works in Helper.", id="raw-name-evidence"),
    pytest.param("I sent it to. Desktop Commander.", "I sent it to Desktop Commander.",
                 id="title-case-after-pause"),
    pytest.param("It seems like... You're nearly ready...",
                 "It seems like you're nearly ready...", id="ellipsis-before-contraction"),
    pytest.param("It's... It's working now.", "It's it's working now.", id="repeated-word"),
    pytest.param("It came out by... Um, turning the handle.",
                 "It came out by um, turning the handle.", id="ellipsis-before-hesitation"),
    pytest.param("All we need is to actually... Get the schedule lined up.",
                 "All we need is to actually get the schedule lined up.", id="paused-infinitive"),
    pytest.param("We are ready... They went home.", "We are ready... They went home.",
                 id="independent-ellipsis"),
    pytest.param("The book makes you laugh, makes you cry. Makes you wonder.",
                 "The book makes you laugh, makes you cry, makes you wonder.", id="parallel-list"),
])
def test_offline_joins_safely_and_idempotently(source, expected):
    result = selection.deterministic_cleanup(source)
    assert result == expected
    assert sanitize.word_lock(source, result)
    assert selection.deterministic_cleanup(result) == result


@pytest.mark.parametrize("preposition", [
    "before", "after", "without", "through", "during", "between", "around", "since",
    "under", "over", "until", "while", "although",
])
def test_links_after_a_pause(preposition):
    source = f"I was talking {preposition}. The meeting."
    assert selection.deterministic_cleanup(source) == f"I was talking {preposition} the meeting."


def test_offline_proposal_preserves_names_in_dictionary():
    app = SimpleNamespace(dictionary_store=SimpleNamespace(custom_vocab=["Clinic"]))
    proposal = session.propose("I talked to the. Clinic.", selection.DEFAULT_INSTRUCTION,
                               app, offline=True, cleanup=True)
    assert proposal.ok and proposal.rewritten == "I talked to the Clinic."


def test_missing_inflection_package_uses_the_builtin_verbs(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "lemminflect", None)
    repair = runpy.run_path(pause_cleanup.__file__)["repair"]
    assert repair("My hands. Hurt all afternoon.") == "My hands hurt all afternoon."
    assert repair("I need to. Leave.") == "I need to leave."
    assert "LemmInflect unavailable" in caplog.text


@pytest.mark.parametrize("raw,reason,logged", [
    (None, "no model available", "no model available"),
    (None, "the model took too long", "the model took too long"),
    (None, "the model could not be reached", "the model could not be reached"),
    (None, "the model request was refused", "the model request was refused"),
    (None, "the model returned nothing", "the model returned nothing"),
    ("I should go.", None, "the model changed the wording"),
    (f"{FENCE}broken wrapper", None, "model went off-script"),
])
def test_model_failures_try_offline_and_only_report_reason_when_unchanged(
        monkeypatch, raw, reason, logged):
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    monkeypatch.setattr(model, "rewrite", lambda *_: (raw, reason))
    instruction = selection.DEFAULT_INSTRUCTION
    result = session.propose("I need to. Leave.", instruction, None, cleanup=True)
    assert result.ok and result.rewritten == "I need to leave."
    assert logged not in result.describe()
    assert any(logged in call.args[0] for call in info.call_args_list)
    unchanged = session.propose("I need to leave.", instruction, None, cleanup=True)
    assert not unchanged.ok and unchanged.rewritten == "I need to leave."
    assert logged in unchanged.describe()


def test_authority_refusal_does_not_prepare_an_offline_proposal(monkeypatch):
    monkeypatch.setattr(model, "rewrite", lambda *_a, **_kw: (None, "the text changed"))
    result = session.propose("I need to. Leave.", selection.DEFAULT_INSTRUCTION, None,
                             cleanup=True, before_submit=lambda: False)
    assert not result.ok and result.reason == "the text changed"


@pytest.mark.parametrize("provider,model_name,expected", [
    ("deepseek", "deepseek-flash", {"thinking": {"type": "disabled"}}),
    ("openrouter", "google/gemini-3.1-flash-lite", {"reasoning": {"enabled": False}}),
    ("anthropic", "claude-sonnet-5-5", {"thinking": {"type": "between_tools"}}),
    ("anthropic", "claude-opus-5-5", {}),
    ("openai", "gpt-5.2", {"reasoning_effort": "none"}),
    ("openai", "gpt-4o-mini", {}),
])
def test_cloud_edit_reasoning_is_provider_specific(monkeypatch, provider, model_name, expected):
    app = SimpleNamespace(config={"cloud_llm": {
        "provider": provider, "model": model_name, "api_key": "fixture"}})
    response = Mock(status_code=200)
    response.json.return_value = {
        "choices": [{"message": {"content": "I need to leave."}}],
        "content": [{"type": "text", "text": "I need to leave."}],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(cloud_llm.ai_transport, "post_cloud", post)
    assert model._call_cloud(app, "synthetic message", 12) == ("I need to leave.", None)
    payload = post.call_args.kwargs["json"]
    assert {key: payload[key] for key in ("thinking", "reasoning", "reasoning_effort")
            if key in payload} == expected
    assert cloud_llm.send_ex("system", "message", app) == ("I need to leave.", None)
    generic = post.call_args.kwargs["json"]
    assert "thinking" not in generic and "reasoning_effort" not in generic
    if provider == "openrouter":
        assert generic["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("before,after,description", [
    ("It helps me. Live.", "It helps me live.", ("removed a period", "capitalization")),
    ("It is ready?", "It is ready.", ("removed a question mark", "added a period")),
    ("I Went home.", "I went home.", ("capitalization",)),
    ("I went too sea.", "I went to see.", ("t o", "s e e")),
])
def test_spoken_changes_describe_what_is_audible(before, after, description):
    spoken = Proposal.build(before, after).describe()
    assert all(part in spoken for part in description)
    assert "becomes" not in spoken
    assert spoken.endswith("Say apply to make it so.")
