"""Pause joins keep words and real sentence ends; model calls are fake."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.ava_edit import model, sanitize, selection, session


SOURCE = (
    "The garden redesign Helps me. Live near more greenery. Instead of being "
    "the project That stays on paper."
)
JOINED = (
    "The garden redesign helps me live near more greenery instead of being "
    "the project that stays on paper."
)



@pytest.mark.parametrize("source, expected", [
    pytest.param(SOURCE, JOINED, id="synthetic-passage-no-comma"),
    pytest.param("Whatever version of me. Helps me. Live in their world.",
                 "Whatever version of me helps me live in their world.",
                 id="noun-phrase-then-object-infinitive"),
    pytest.param("I went to the. Store and. Bought some milk.",
                 "I went to the store and bought some milk.", id="632"),
    pytest.param("I need to. Live in their world.",
                 "I need to live in their world.", id="preposition"),
    pytest.param("I asked because. The door was locked.",
                 "I asked because the door was locked.", id="conjunction"),
    pytest.param("The plan is. Being reviewed by Alice.",
                 "The plan is being reviewed by Alice.", id="auxiliary"),
    pytest.param("The work is. Done.", "The work is done.", id="auxiliary-adjective"),
    pytest.param("It helps me. Live with less pain.",
                 "It helps me live with less pain.", id="catenative"),
    pytest.param("It helps me. To live with less pain.",
                 "It helps me to live with less pain.", id="catenative-to"),
    pytest.param("The new tool. Helps me every day.",
                 "The new tool helps me every day.", id="noun-phrase-subject"),
    pytest.param("We went home. and then we ate dinner.",
                 "We went home and then we ate dinner.", id="632-lowercase"),
    pytest.param("I spoke to. Alice in London.",
                 "I spoke to Alice in London.", id="proper-name"),
    pytest.param("It connects to. NASA in Houston.",
                 "It connects to NASA in Houston.", id="acronym"),
    pytest.param("It helps me. LIVE in their world.",
                 "It helps me LIVE in their world.", id="all-caps"),
])
def test_offline_pause_joins(source, expected):
    assert selection.deterministic_cleanup(source) == expected
    assert sanitize.similarity(source, expected) == 1.0


@pytest.mark.parametrize("source", [
    pytest.param("I saw Dr. Smith. He waved.", id="dr-and-real-sentence"),
    pytest.param("Is it done. Yes.", id="question-answer"),
    pytest.param("I need to.\n\nLive in their world.", id="paragraph"),
    pytest.param("I need to. \nLive in their world.", id="spaced-line-break"),
    pytest.param("I need to.\r\n\r\nLive in their world.", id="crlf-paragraph"),
    pytest.param('I need to. "Live in their world."', id="quoted-line"),
    pytest.param("I need to. 'Live in their world.'", id="single-quoted-line"),
    pytest.param("I need to. " + chr(0x201C) + "Live in their world." + chr(0x201D),
                 id="curly-quoted-line"),
    pytest.param("The answer is. Live here?", id="right-question"),
    pytest.param("I met Mr. Jones.", id="mr"),
    pytest.param("I met Prof. Jones.", id="prof"),
    pytest.param("I spoke to J. Smith.", id="initial"),
    pytest.param("We live in the U.S. And we like it.", id="initialism"),
    pytest.param("I am ready. Go now.", id="short-imperative"),
    pytest.param("I finished. Then I left.", id="then-new-subject"),
    pytest.param("I waited. Instead I went home.", id="instead-new-subject"),
    pytest.param("I saw him. He waved.", id="object-pronoun-complete"),
    pytest.param("I like you. Live your life.", id="non-catenative-object"),
    pytest.param("I think so. Then we left.", id="so-is-complete"),
    pytest.param("There it is. I found it.", id="auxiliary-is-complete"),
    pytest.param("I know that. He told me.", id="that-is-complete"),
    pytest.param("I saw it. That was odd.", id="demonstrative-new-subject"),
    pytest.param("I know who. Alice told me.", id="named-new-subject"),
    pytest.param("I went home. she stayed there.", id="lowercase-independent-clause"),
    pytest.param("I am ready. Yes.", id="short-verbless-answer"),
    # 1215 repairs interior pause ellipses; only final ellipses remain.
    pytest.param("I waited...", id="ellipsis"),
])
def test_real_boundaries_are_preserved(source):
    assert selection.deterministic_cleanup(source) == source


@pytest.mark.parametrize("path", ["offline", "model", "fallback"])
def test_synthetic_proposal_passes_both_sanitizer_floors_and_logs_once(monkeypatch, path):
    def rewrite(text, instruction, app):
        assert text == SOURCE
        assert "A period followed by a capital letter" in instruction
        assert "Do not reword" in instruction
        return (None, "no model available") if path == "fallback" else (JOINED, None)

    call = Mock(side_effect=rewrite)
    monkeypatch.setattr(model, "rewrite", call)
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    proposal = session.propose(SOURCE, selection.DEFAULT_INSTRUCTION, SimpleNamespace(),
                               offline=path == "offline", cleanup=True)
    assert proposal.ok, proposal.reason
    assert proposal.rewritten == JOINED
    assert sanitize.check(JOINED, SOURCE, selection.DEFAULT_INSTRUCTION) is None
    assert sanitize.similarity(SOURCE, JOINED) == 1.0
    assert call.call_count == (path != "offline")
    actual_path = "model" if path == "model" else "offline"
    info.assert_called_once_with(
        f"[AVA-EDIT] cleanup path={actual_path} joins_made=2 "
        f"chars_before={len(SOURCE)} chars_after={len(JOINED)}")


@pytest.mark.parametrize("raw, reason", [
    (None, "the model took too long"),
    ("Completely unrelated words.", None),
    (SOURCE, None),
])
def test_refused_or_unchanged_cleanup_logs_zero_applied_joins(monkeypatch, raw, reason):
    monkeypatch.setattr(model, "rewrite", lambda *_: (raw, reason))
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    proposal = session.propose(SOURCE, selection.DEFAULT_INSTRUCTION, SimpleNamespace(), cleanup=True)
    assert not proposal.ok
    summaries = [call.args[0] for call in info.call_args_list if "cleanup path=" in call.args[0]]
    assert summaries == [f"[AVA-EDIT] cleanup path=model joins_made=0 "
                          f"chars_before={len(SOURCE)} chars_after={len(SOURCE)}"]


@pytest.mark.parametrize("source, instruction", [("", "clean up"), (SOURCE, "")])
def test_early_cleanup_refusal_still_logs_once(monkeypatch, source, instruction):
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    assert not session.propose(source, instruction, SimpleNamespace(), offline=True, cleanup=True).ok
    info.assert_called_once_with(f"[AVA-EDIT] cleanup path=offline joins_made=0 "
                                 f"chars_before={len(source)} chars_after={len(source)}")


def test_logging_counts_removed_boundaries_even_when_model_adds_a_period(monkeypatch):
    source = "The tool. Helps me I use it daily."
    rewritten = "The tool helps me. I use it daily."
    monkeypatch.setattr(model, "rewrite", lambda *_: (rewritten, None))
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    assert session.propose(source, selection.DEFAULT_INSTRUCTION, SimpleNamespace(), cleanup=True).ok
    info.assert_called_once_with(f"[AVA-EDIT] cleanup path=model joins_made=1 "
                                 f"chars_before={len(source)} chars_after={len(rewritten)}")


def test_generic_edit_does_not_emit_cleanup_summary(monkeypatch):
    monkeypatch.setattr(model, "rewrite", lambda *_: ("A shorter sentence.", None))
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    session.propose("A much longer sentence.", "Make this shorter", SimpleNamespace())
    info.assert_not_called()


@pytest.mark.parametrize("provider", ["ollama", "openai"])
@pytest.mark.parametrize("instruction", ["clean up selection", "make the selection shorter"])
def test_enabled_selection_without_draft_uses_authorized_provider(monkeypatch, provider, instruction):
    from samsara import ai_preferences, smart_corrections

    app = SimpleNamespace(config={"ava_edit": {"enabled": True}})
    assert session.source_text(app) == ""
    original = "The tool. Helps me every day."
    rewritten = "The tool helps me every day."
    target = (101, 102, 103)

    def runtime_state(config, purpose):
        assert config is app.config and purpose == "editing"
        return SimpleNamespace(allowed=True, provider=SimpleNamespace(identity=provider))

    monkeypatch.setattr(ai_preferences, "runtime_state", runtime_state)
    probe = Mock(return_value=True)
    monkeypatch.setattr(smart_corrections, "_ollama_reachable", probe)
    local = Mock(return_value=(rewritten, None))
    cloud = Mock(return_value=(rewritten, None))
    monkeypatch.setattr(model, "_call_ollama", local)
    monkeypatch.setattr(model, "_call_cloud", cloud)
    monkeypatch.setattr(selection, "capture", lambda: (original, target))
    monkeypatch.setattr(session, "_chip", Mock())
    stage = Mock(return_value=True)
    monkeypatch.setattr(session, "stage", stage)
    info = Mock()
    monkeypatch.setattr(session.logger, "info", info)
    monkeypatch.setattr(model.logger, "info", info)

    assert selection.propose_and_stage(app, instruction)
    chosen, other = (local, cloud) if provider == "ollama" else (cloud, local)
    chosen.assert_called_once()
    other.assert_not_called()
    if provider != "ollama":
        probe.assert_not_called()
    assert original in chosen.call_args.args[1]
    proposal = stage.call_args.args[1]
    assert proposal.ok and proposal.rewritten == rewritten
    assert stage.call_args.kwargs == {"selection_target": target}
    assert all(original not in str(call) and rewritten not in str(call)
               and instruction not in str(call) for call in info.call_args_list)
    summaries = [call.args[0] for call in info.call_args_list if "cleanup path=" in call.args[0]]
    assert len(summaries) == (instruction == "clean up selection")
    if summaries:
        assert "path=model joins_made=1" in summaries[0]
