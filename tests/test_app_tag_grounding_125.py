"""Queue 125: the app-verb tag is APP, and a model may not open a program
the user did not name.

Queue 117 measured `ACTION2` at 0/40 and found the failure mode that matters:

    "Bring up Blender."  ->  ACTION open firefox

That line was not an invention. `open firefox` is a real, in-menu,
policy-allowed command, so no membership check could ever have caught it --
**membership is not grounding**. And the closed-world gate
(ava_command_session._closed_world_selection_ok) is only on the
command-session route; handle_ask_ava and handle_is_it_safe call
handle_response directly, with nothing between the model and the executor.

125 closes both:

  * the tag is APP, which shares no prefix with ACTION, so a 3B model is not
    being asked to hold a one-character distinction;
  * an ACTION whose verb names an application, and every APP argument, must
    be GROUNDED in what the user actually said.

These tests use the real parser and the real router. The executor is a spy,
so a test that expects a refusal fails loudly if anything reaches it.
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import plugins.commands.ask_ollama as ask_ollama
from plugins.commands.app_verbs import ActionResult
from samsara.command_registry import DispatchState


class _Result:
    def __init__(self, state=DispatchState.COMPLETED, detail=None):
        self.state = state
        self.detail = detail or {}


class _Executor:
    """Records every execute_canonical call. A refusal test asserts this
    stayed empty -- 'it was refused' is only true if nothing ran."""

    def __init__(self, menu=(), result=None):
        self.calls = []
        self._menu = list(menu)
        self._result = result or _Result()

    def ava_menu(self, utterance="", **kw):
        return list(self._menu)

    def execute_canonical(self, command_id, app=None, **kw):
        self.calls.append(command_id)
        return self._result


MENU = ["open firefox", "open chrome", "next tab", "minimize all", "snap right",
        "volume up", "scroll down", "open spotify"]


def _app(menu=MENU, **over):
    app = MagicMock()
    app.config = {}
    app.command_executor = _Executor(menu)
    app.audio_coordinator = MagicMock()
    for k, v in over.items():
        setattr(app, k, v)
    return app


def _spoken(app):
    return " ".join(str(c.args[0]) for c in app.audio_coordinator.speak.call_args_list
                    if c.args)


@pytest.fixture(autouse=True)
def _clear_pending():
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


@pytest.fixture(autouse=True)
def _real_command_exists(monkeypatch):
    """command_exists() is the invention gate. Pin it to the test menu so a
    test does not depend on the machine's live registry."""
    monkeypatch.setattr(ask_ollama.execution_policy, "command_exists",
                        lambda cid, **kw: cid in MENU)


# ---------------------------------------------------------------------------
# 1. The tag
# ---------------------------------------------------------------------------

class TestTheTag:
    def test_the_tag_shares_no_prefix_with_action(self):
        """The whole point. ACTION vs ACTION2 is one token plus a digit;
        ACTION vs APP diverges at the second character."""
        assert ask_ollama.APP_TAG == "APP"
        assert not ask_ollama.APP_TAG.startswith("ACTION")
        assert ask_ollama.APP_TAG[:2] != "ACTION"[:2]

    def test_the_new_tag_parses(self):
        parsed = ask_ollama._parse_structured_response(
            "CONFIRM Focus Claude.\nAPP focus | the claude desktop app")
        assert parsed["type"] == "action2"
        assert parsed["verb"] == "focus"
        assert parsed["argument"] == "the claude desktop app"
        assert parsed["legacy_tag"] is False

    def test_the_old_tag_still_parses_for_one_release(self):
        """A conversation already in a model's context, or a cached
        response, must not start failing on the day the tag changes."""
        parsed = ask_ollama._parse_structured_response(
            "CONFIRM Open Notepad.\nACTION2 open | notepad")
        assert parsed["type"] == "action2"
        assert parsed["verb"] == "open"
        assert parsed["legacy_tag"] is True

    def test_the_old_tag_is_logged_so_we_can_see_it_stop(self, caplog):
        import logging
        caplog.set_level(logging.INFO)
        ask_ollama._parse_structured_response("CONFIRM x.\nACTION2 open | notepad")
        assert any("ACTION2" in r.message and "legacy" in r.message.lower()
                   for r in caplog.records), [r.message for r in caplog.records]

    def test_the_new_tag_is_not_logged_as_legacy(self, caplog):
        import logging
        caplog.set_level(logging.INFO)
        ask_ollama._parse_structured_response("CONFIRM x.\nAPP open | notepad")
        assert not any("legacy" in r.message.lower() for r in caplog.records)

    def test_app_wins_when_both_tags_appear(self):
        parsed = ask_ollama._parse_structured_response(
            "CONFIRM x.\nAPP open | notepad\nACTION2 close | spotify")
        assert parsed["argument"] == "notepad" and parsed["legacy_tag"] is False

    @pytest.mark.parametrize("prompt_name", ["relaxed", "strict", "default"])
    def test_the_prompt_teaches_the_new_tag_and_not_the_old(self, prompt_name):
        prompts = {
            "relaxed": ask_ollama.RELAXED_SYSTEM_PROMPT + ask_ollama._SHARED_MODES,
            "strict": ask_ollama.STRICT_SYSTEM_PROMPT + ask_ollama._SHARED_MODES,
            "default": ask_ollama.DEFAULT_SYSTEM_PROMPT,
        }
        p = prompts[prompt_name]
        assert "APP <verb> | <argument>" in p
        assert "ACTION2" not in p, "the old tag must not be taught any more"


# ---------------------------------------------------------------------------
# 2. ACTION and APP are unambiguously distinguished
# ---------------------------------------------------------------------------

class TestTheTwoGrammarsAreDistinct:
    def test_a_plain_action_is_an_action(self):
        parsed = ask_ollama._parse_structured_response("CONFIRM x.\nACTION next tab")
        assert parsed["type"] == "action" and parsed["command"] == "next tab"

    def test_an_action_line_carrying_a_pipe_is_refused_not_split(self):
        """117's measured hybrid: ACTION's tag with APP's grammar. Splitting
        it and running the halves is how a malformed line becomes an effect."""
        parsed = ask_ollama._parse_structured_response(
            "CONFIRM Focus Claude.\nACTION focus | claude desktop app")
        assert parsed["type"] == "hybrid"

    def test_the_hybrid_runs_nothing(self):
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Focus Claude.\nACTION focus | claude desktop app",
            original_text="focus the claude desktop app")
        assert outcome.state == "refused" and outcome.reason == "hybrid_grammar"
        assert app.command_executor.calls == []

    def test_no_pipe_heuristic_is_needed_to_tell_them_apart(self):
        """117 needed a "|" tell because the tags collided. They no longer
        do: the TAG alone decides, and a pipe on an ACTION line is an error
        rather than a signal to reinterpret it."""
        assert ask_ollama._parse_structured_response(
            "CONFIRM x.\nAPP open | spotify")["type"] == "action2"
        assert ask_ollama._parse_structured_response(
            "CONFIRM x.\nACTION open spotify")["type"] == "action"


# ---------------------------------------------------------------------------
# 3. Invention is refused
# ---------------------------------------------------------------------------

class TestInventionIsRefused:
    def test_a_command_that_is_not_a_command_is_refused(self):
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Frobnicate.\nACTION frobnicate the widget",
            original_text="frobnicate the widget")
        assert outcome.state == "refused" and outcome.reason == "not_a_command"
        assert app.command_executor.calls == []
        assert "don't have a command" in _spoken(app).lower()

    def test_a_real_but_unavailable_command_keeps_the_executors_own_reason(self):
        app = _app(menu=["next tab"])     # volume up exists but is not offered
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Turn it up.\nACTION volume up",
            original_text="turn the volume up")
        assert app.command_executor.calls == []
        assert outcome.state == "refused" and outcome.reason == "not_offered"

    def test_an_unlisted_app_verb_is_refused(self):
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Delete it.\nAPP delete | spotify",
            original_text="delete spotify")
        assert outcome.state == "refused" and outcome.reason == "unknown_verb"


# ---------------------------------------------------------------------------
# 4. Grounding -- the one that stops the wrong application opening
# ---------------------------------------------------------------------------

class TestGrounding:
    def test_the_measured_failure_is_refused(self):
        """THE test. 117 measured this exact line opening the wrong program."""
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Open Firefox.\nACTION open firefox",
            original_text="Bring up Blender.")
        assert outcome.state == "refused" and outcome.reason == "ungrounded"
        assert app.command_executor.calls == [], "Firefox must not open"
        assert "which app" in _spoken(app).lower()

    def test_the_same_command_runs_when_the_user_did_name_it(self):
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Open Firefox.\nACTION open firefox",
            original_text="can you open Firefox for me")
        assert app.command_executor.calls == ["open firefox"]
        assert outcome.state == "completed"

    @pytest.mark.parametrize("utterance,command,runs", [
        ("open chrome",            "open chrome",   True),
        ("could you open Chrome?", "open chrome",   True),
        ("bring up blender",       "open chrome",   False),
        ("play some music",        "open spotify",  False),
        ("open spotify please",    "open spotify",  True),
    ])
    def test_an_app_verb_needs_its_app_in_the_utterance(self, utterance, command, runs):
        app = _app()
        ask_ollama.handle_response(app, f"CONFIRM x.\nACTION {command}",
                                   original_text=utterance)
        assert (app.command_executor.calls == [command]) is runs

    @pytest.mark.parametrize("utterance,command", [
        ("hide all these windows for me", "minimize all"),
        ("take me to the next tab",       "next tab"),
        ("put this window on the right",  "snap right"),
        ("turn the volume up",            "volume up"),
        ("go down a bit",                 "scroll down"),
    ])
    def test_a_non_app_command_is_never_grounded_against(self, utterance, command):
        """The registry path must not regress. "hide all these windows" ->
        `minimize all` shares no word with the request and has to stay legal,
        which is why grounding is scoped to the four app verbs and is not a
        general similarity test."""
        app = _app(menu=MENU + [command])
        ask_ollama.handle_response(app, f"CONFIRM x.\nACTION {command}",
                                   original_text=utterance)
        assert app.command_executor.calls == [command]

    def test_the_app_argument_must_be_grounded_too(self):
        """Queue 107's review found this rule stated in the prompt and never
        enforced. "Bring up Blender" may not produce `APP open | firefox`."""
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Open Firefox.\nAPP open | firefox",
            original_text="bring up blender")
        assert outcome.state == "refused" and outcome.reason == "ungrounded"

    def test_a_grounded_app_argument_is_allowed_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(ask_ollama, "_execute_action2",
                            lambda app, verb, arg, **kw: seen.update(verb=verb, arg=arg)
                            or ActionResult.DONE)
        app = _app()
        outcome = ask_ollama.handle_response(
            app, "CONFIRM Open Blender.\nAPP open | blender",
            original_text="bring up Blender")
        assert seen == {"verb": "open", "arg": "blender"}
        assert outcome.state == "completed"

    def test_possessives_and_punctuation_still_ground(self):
        assert ask_ollama.grounded_in_utterance("blender", "bring up Blender's window")
        assert ask_ollama.grounded_in_utterance("chrome", "open Chrome.")
        assert ask_ollama.grounded_in_utterance("the claude desktop app",
                                                "focus the claude desktop app")

    def test_a_target_with_no_identifying_words_is_not_blocked(self):
        """"focus the window" has nothing to check; refusing it would be the
        gate inventing a problem."""
        assert ask_ollama.grounded_in_utterance("the window", "focus the window please")
        assert ask_ollama.grounded_in_utterance("the app", "focus it")

    def test_grounding_is_one_directional(self):
        """It can refuse a target the user never said. It never CHOOSES one."""
        assert ask_ollama.grounded_in_utterance("firefox", "open firefox") is True
        assert ask_ollama.grounded_in_utterance("firefox", "open blender") is False


# ---------------------------------------------------------------------------
# 5. The gate is on the path that was missing it
# ---------------------------------------------------------------------------

class TestTheGateCoversTheOrdinaryAvaPath:
    def test_handle_response_itself_enforces_it(self):
        """The finding: _closed_world_selection_ok is only reached from
        ava_command_session's stage (c). handle_ask_ava and handle_is_it_safe
        call handle_response directly. So the gate has to live HERE, which is
        the one place all three paths pass through."""
        app = _app()
        ask_ollama.handle_response(app, "CONFIRM x.\nACTION open firefox",
                                   original_text="bring up blender")
        assert app.command_executor.calls == []

    def test_a_missing_executor_does_not_turn_everything_into_a_refusal(self):
        app = _app()
        app.command_executor = None
        outcome = ask_ollama.handle_response(app, "CONFIRM x.\nACTION next tab",
                                             original_text="next tab")
        assert outcome.reason in ("no_executor", "not_a_command")

    def test_a_broken_menu_does_not_block_a_legitimate_command(self, monkeypatch):
        app = _app()
        app.command_executor.ava_menu = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        assert ask_ollama.offered_menu(app, "next tab") is None
        outcome = ask_ollama.handle_response(app, "CONFIRM x.\nACTION next tab",
                                             original_text="take me to the next tab")
        assert outcome.state == "refused" and outcome.reason == "not_offered"
        assert app.command_executor.calls == []


class TestGroundingIsNarrow:
    """The rule must not creep. It applies to a PROPER NOUN target under an
    app verb, and nothing else -- `close tab` is a UI command that happens to
    start with "close", and refusing it would break the interface to protect
    against a problem it does not have."""

    @pytest.mark.parametrize("command", [
        "close tab", "close window", "open settings", "open bookmarks",
        "open downloads", "open keyboard", "close quote", "open note",
        "open graph", "close magnifier", "open files", "open terminal",
    ])
    def test_a_ui_target_is_never_grounded_against(self, command):
        assert ask_ollama.action_is_grounded(command, "shut that thing")
        assert ask_ollama.action_is_grounded(command, "")

    @pytest.mark.parametrize("command", [
        "open firefox", "open chrome", "close spotify", "focus obsidian",
        "open steam", "open discord",
    ])
    def test_a_program_target_is_grounded_against(self, command):
        assert not ask_ollama.action_is_grounded(command, "bring up blender")
        assert ask_ollama.action_is_grounded(command, command)

    def test_a_single_word_command_is_never_grounded_against(self):
        for c in ("focus", "open", "close", "submit", "undo"):
            assert ask_ollama.action_is_grounded(c, "do the thing")
