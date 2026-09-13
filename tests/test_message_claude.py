"""plugins/commands/message_claude.py with the Claude window adapter and the
resolver mocked: grammar forms, app not running, prepare -> draft + "Send?",
submit refusals (conversation changed, composer read-back mismatch), the
observed-node success, and the unknown outcome (timeout) that keeps the draft
and refuses a second submit."""
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from plugins.commands import ask_ollama  # noqa: E402
from plugins.commands import message_claude as mc  # noqa: E402
from samsara import execution_policy as ep  # noqa: E402
from samsara.command_registry import DispatchState  # noqa: E402

UTTERANCE = "the mode switch fix landed and ask what's next"
SHAPED = "The mode switch fix landed. What should I do next?"


class FakeAdapter:
    """A scripted Claude window: what the resolver returns, what the composer
    reads back after a paste, whether Send exists, and which messages appear."""

    def __init__(self, hwnd=4242, title="Claude", conversation="Demo chat - Claude"):
        self.window = (hwnd, title, conversation)
        self.composer = object()
        self.composer_text = ""
        self.composer_reads_back = True      # paste lands as-is
        self.has_send = True
        self.messages = []                   # texts visible in the chat
        self.append_on_send = True           # sending makes the message appear
        self.calls = []

    def resolve_window(self):
        self.calls.append("resolve")
        return self.window

    def find_composer(self, hwnd):
        self.calls.append("find_composer")
        return self.composer

    def focus(self, hwnd):
        self.calls.append("focus")
        return True

    def paste(self, body, hwnd):
        self.calls.append(("paste", body))
        self.composer_text = body if self.composer_reads_back else body[:-5] + "XXXXX"
        return True

    def read_composer(self, composer):
        self.calls.append("read")
        return self.composer_text

    def find_send(self, composer):
        self.calls.append("find_send")
        return object() if self.has_send else None

    def invoke(self, button):
        self.calls.append("invoke")
        if self.append_on_send:
            self.messages.append(self.composer_text)
            self.composer_text = ""
        return True

    def message_texts(self, hwnd):
        self.calls.append("observe")
        return list(self.messages)


@pytest.fixture
def adapter():
    fake = FakeAdapter()
    mc.set_adapter(fake)
    yield fake
    mc.set_adapter(None)


@pytest.fixture
def app():
    a = types.SimpleNamespace()
    a.config = {}
    a.audio_coordinator = Mock()
    a._show_outcome_chip = Mock()
    a._ava_cmd_generation = 3
    return a


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # conftest clears the plugin registry around tests; put this module's
    # commands back so the policy can resolve "claude message submit".
    from samsara import plugin_commands
    plugin_commands._reinstall_module_commands(mc)
    ask_ollama.clear_pending_action()
    mc.clear_drafts()
    monkeypatch.setattr(mc, "OBSERVE_TIMEOUT_S", 0.4)
    yield
    ask_ollama.clear_pending_action()
    mc.clear_drafts()


def _spoken(app):
    return [c.args[0] for c in app.audio_coordinator.speak.call_args_list]


def _chips(app):
    return [c.args for c in app._show_outcome_chip.call_args_list]


# ---------------------------------------------------------------------------
# Grammar and shaping
# ---------------------------------------------------------------------------

class TestGrammar:
    def test_four_phrases_registered_on_prepare(self):
        from samsara import plugin_commands
        entry = plugin_commands._REGISTRY["claude message prepare"]
        assert set(entry["aliases"]) == {"tell claude", "ask claude", "message claude", "send claude"}
        for phrase in ("tell claude", "ask claude", "message claude", "send claude"):
            assert plugin_commands._REGISTRY[phrase] is entry

    def test_risk_metadata(self):
        from samsara import plugin_commands
        prepare = plugin_commands._REGISTRY["claude message prepare"]
        submit = plugin_commands._REGISTRY["claude message submit"]
        assert prepare["risk_class"] == "write"
        assert submit["risk_class"] == "destructive" and submit["reversible"] is False
        # The decorator does not retain side_effect_category; what the
        # registry keeps is the irreversible flag and the side-effect list.
        assert submit["side_effects"] == ["message_sent"]
        assert submit["metadata"]["reversible"] is False

    def test_submit_requires_confirmation_under_the_policy(self, app):
        d = ep.authorize(ep.Invocation("claude message submit", {"remainder": "abc"}, ep.Route.EXACT, 3), app=app)
        assert isinstance(d, ep.NeedsConfirmation)
        d = ep.authorize(ep.Invocation("claude message prepare", {"remainder": "hi"}, ep.Route.EXACT, 3), app=app)
        assert isinstance(d, ep.Allowed)

    def test_idiom_rewrite_is_the_only_shaping(self):
        assert mc.shape_body(UTTERANCE) == SHAPED
        assert mc.shape_body("The mode switch fix landed and ask what is next") == SHAPED
        assert mc.shape_body("and ask what's next") == "What should I do next?"
        assert mc.shape_body("please look at the diff") == "please look at the diff"
        assert mc.shape_body("we ship tomorrow, and ask for next steps") == "We ship tomorrow. What are the next steps?"
        assert len(mc.IDIOMS) <= 5

    def test_preview_is_sixty_chars(self):
        long = "x" * 100
        assert mc.preview(long).startswith("x" * 60) and mc.preview(long).endswith("...")
        assert mc.preview("short") == "short"


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------

class TestPrepare:
    def test_app_not_running_fails(self, app, adapter):
        adapter.window = None
        result = mc.handle_claude_message_prepare(app, UTTERANCE)
        assert result.state is DispatchState.FAILED
        assert result.detail["reason"] == "Claude app not running"
        assert ask_ollama.get_pending_action() is None
        assert ("Claude app not running", "error") in _chips(app)

    def test_composer_missing_fails(self, app, adapter):
        adapter.composer = None
        result = mc.handle_claude_message_prepare(app, UTTERANCE)
        assert result.state is DispatchState.FAILED and "composer" in result.detail["reason"]

    def test_prepare_creates_draft_and_asks_send(self, app, adapter):
        result = mc.handle_claude_message_prepare(app, UTTERANCE)
        assert result.state is DispatchState.QUEUED
        assert result.detail["question"] == f"Send to Claude: {SHAPED}?"
        draft = mc.get_draft(result.detail["draft_id"])
        assert draft.body == SHAPED and draft.hash == mc.body_hash(SHAPED)
        assert draft.hwnd == 4242 and draft.conversation == "Demo chat - Claude"
        assert draft.state == "prepared"
        pending = ask_ollama.get_pending_action()
        assert pending is not None and pending["command"] == "claude message submit"
        assert pending["draft_id"] == draft.id and pending["generation"] == 3
        assert ("Send?", "pending") in _chips(app)
        assert any(s.startswith("Send to Claude:") for s in _spoken(app))
        assert not any(isinstance(c, tuple) and c[0] == "paste" for c in adapter.calls), "prepare never pastes"

    def test_empty_text_fails(self, app, adapter):
        result = mc.handle_claude_message_prepare(app, "")
        assert result.state is DispatchState.FAILED

    def test_question_previews_sixty_chars(self, app, adapter):
        result = mc.handle_claude_message_prepare(app, "word " * 40)
        assert len(result.detail["question"]) <= len("Send to Claude: ") + 60 + 4


# ---------------------------------------------------------------------------
# Submit
# ---------------------------------------------------------------------------

def _prepared(app):
    result = mc.handle_claude_message_prepare(app, UTTERANCE)
    return result.detail["draft_id"]


class TestSubmit:
    def test_yes_runs_submit_and_observes_the_message(self, app, adapter):
        draft_id = _prepared(app)
        ask_ollama.handle_ava_confirm(app)                       # the policy's confirmed path
        draft = mc.get_draft(draft_id)
        assert draft.state == "sent" and draft.outcome == "sent: observed"
        assert ("paste", SHAPED) in adapter.calls
        assert adapter.calls.index("read") < adapter.calls.index("invoke") < adapter.calls.index("observe")
        assert ("Sent to Claude", "success") in _chips(app)
        assert ask_ollama.get_pending_action() is None

    def test_submit_result_detail(self, app, adapter):
        draft_id = _prepared(app)
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.COMPLETED
        assert result.detail["detail"] == "sent: observed" and result.detail["state"] == "sent"

    def test_conversation_changed_fails_without_pasting(self, app, adapter):
        draft_id = _prepared(app)
        adapter.window = (4242, "Claude", "Another chat - Claude")
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED
        assert result.detail["reason"] == "conversation changed, not sent"
        assert not any(isinstance(c, tuple) and c[0] == "paste" for c in adapter.calls)
        assert "invoke" not in adapter.calls

    def test_window_changed_fails_without_pasting(self, app, adapter):
        draft_id = _prepared(app)
        adapter.window = (9999, "Claude", "Demo chat - Claude")
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED and "conversation changed" in result.detail["reason"]
        assert not any(isinstance(c, tuple) and c[0] == "paste" for c in adapter.calls)

    def test_app_gone_at_submit_fails(self, app, adapter):
        draft_id = _prepared(app)
        adapter.window = None
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED and "not running" in result.detail["reason"]

    def test_composer_readback_mismatch_fails_without_send(self, app, adapter):
        draft_id = _prepared(app)
        adapter.composer_reads_back = False
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED
        assert "composer text differs" in result.detail["reason"]
        assert "find_send" not in adapter.calls and "invoke" not in adapter.calls
        assert adapter.composer_text, "the body is left in the composer"
        assert mc.get_draft(draft_id).state == "failed"

    def test_missing_send_control_fails_after_paste(self, app, adapter):
        draft_id = _prepared(app)
        adapter.has_send = False
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED and "send control" in result.detail["reason"]
        assert "invoke" not in adapter.calls

    def test_timeout_is_unknown_and_keeps_the_draft(self, app, adapter):
        draft_id = _prepared(app)
        adapter.append_on_send = False                            # sent, never appears
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.FAILED
        assert result.detail["state"] == "unknown"
        assert result.detail["reason"] == "submitted, not observed -- do not retry automatically"
        draft = mc.get_draft(draft_id)
        assert draft is not None and draft.state == "unknown"
        assert ("Outcome unknown", "warning") in _chips(app)
        assert "invoke" in adapter.calls and adapter.calls.count("invoke") == 1

    def test_second_submit_of_unknown_draft_is_refused(self, app, adapter):
        draft_id = _prepared(app)
        adapter.append_on_send = False
        mc.handle_claude_message_submit(app, draft_id)
        invokes = adapter.calls.count("invoke")
        again = mc.handle_claude_message_submit(app, draft_id)
        assert again.state is DispatchState.REJECTED and "not resubmitting" in again.detail["reason"]
        assert adapter.calls.count("invoke") == invokes, "no second Send"

    def test_second_submit_of_sent_draft_is_refused(self, app, adapter):
        draft_id = _prepared(app)
        mc.handle_claude_message_submit(app, draft_id)
        again = mc.handle_claude_message_submit(app, draft_id)
        assert again.state is DispatchState.REJECTED and again.detail["reason"] == "already sent"

    def test_unknown_draft_id(self, app, adapter):
        result = mc.handle_claude_message_submit(app, "nope")
        assert result.state is DispatchState.FAILED

    def test_yes_after_cancel_or_exit_sends_nothing(self, app, adapter):
        _prepared(app)
        app._ava_cmd_generation += 1                              # session moved on
        ask_ollama.handle_ava_confirm(app)
        assert "invoke" not in adapter.calls
        assert not any(isinstance(c, tuple) and c[0] == "paste" for c in adapter.calls)

    def test_ava_cancel_drops_the_question(self, app, adapter):
        draft_id = _prepared(app)
        ask_ollama.handle_ava_cancel(app)
        assert ask_ollama.get_pending_action() is None
        assert mc.get_draft(draft_id).state == "prepared"         # draft kept, nothing sent
        assert "invoke" not in adapter.calls

    def test_observation_matches_on_the_body_head(self, app, adapter):
        draft_id = _prepared(app)
        adapter.append_on_send = False
        adapter.messages = ["You said: " + SHAPED + " (edited)"]   # the node contains the body
        result = mc.handle_claude_message_submit(app, draft_id)
        assert result.state is DispatchState.COMPLETED
