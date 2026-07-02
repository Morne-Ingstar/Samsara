"""Tests for the ACTION2 grammar in plugins.commands.ask_ollama: parsing
CONFIRM+ACTION2 responses, routing valid verbs to the deterministic
app_verbs resolvers, rejecting unlisted verbs (fail-closed, no guessing),
and never executing anything when the resolver reports no match.
"""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import plugins.commands.ask_ollama as ask_ollama
from plugins.commands.app_verbs import ActionResult


def _app():
    app = MagicMock()
    app.audio_coordinator = MagicMock()
    app.config = {}
    return app


@pytest.fixture(autouse=True)
def _clear_pending():
    """The confirm/pending-action flow is module-level global state --
    never let one test's pending action leak into the next."""
    ask_ollama.clear_pending_action()
    yield
    ask_ollama.clear_pending_action()


# ---------------------------------------------------------------------------
# _parse_structured_response
# ---------------------------------------------------------------------------

class TestParseAction2:
    def test_valid_action2_parses(self):
        response = "CONFIRM Focus Claude.\nACTION2 focus | the claude desktop app"
        parsed = ask_ollama._parse_structured_response(response)
        assert parsed["type"] == "action2"
        assert parsed["verb"] == "focus"
        assert parsed["argument"] == "the claude desktop app"
        assert parsed["confirm_text"] == "Focus Claude."

    def test_action2_verb_lowercased(self):
        response = "CONFIRM Open Notepad.\nACTION2 OPEN | notepad"
        parsed = ask_ollama._parse_structured_response(response)
        assert parsed["verb"] == "open"

    def test_legacy_action_still_parses_as_action_not_action2(self):
        response = "CONFIRM Open Chrome.\nACTION open chrome"
        parsed = ask_ollama._parse_structured_response(response)
        assert parsed["type"] == "action"
        assert parsed["command"] == "open chrome"

    def test_action2_line_does_not_leak_into_legacy_action_match(self):
        """A literal 'ACTION2 ...' line must never be picked up by the
        ACTION-prefix regex -- these are mutually exclusive response types."""
        response = "CONFIRM Close Spotify.\nACTION2 close | spotify"
        parsed = ask_ollama._parse_structured_response(response)
        assert parsed["type"] == "action2"

    def test_plain_prose_is_conversation(self):
        response = "I can't do that, sorry."
        parsed = ask_ollama._parse_structured_response(response)
        assert parsed["type"] == "conversation"


# ---------------------------------------------------------------------------
# handle_response -- ACTION2 routing
# ---------------------------------------------------------------------------

class TestHandleResponseAction2Routing:
    def test_valid_focus_verb_routes_to_resolver_and_executes_silently(self):
        app = _app()
        response = "CONFIRM Focus Claude.\nACTION2 focus | claude"
        with patch("plugins.commands.app_verbs.do_focus", return_value=ActionResult.DONE) as mock_focus:
            ask_ollama.handle_response(app, response, original_text="focus claude")
        mock_focus.assert_called_once_with("claude")
        app.audio_coordinator.speak.assert_not_called()

    def test_valid_open_verb_routes_to_resolver(self):
        app = _app()
        response = "CONFIRM Open Notepad.\nACTION2 open | notepad"
        with patch("plugins.commands.app_verbs.do_open", return_value=ActionResult.DONE) as mock_open:
            ask_ollama.handle_response(app, response, original_text="open notepad")
        mock_open.assert_called_once_with("notepad")

    def test_unlisted_verb_is_rejected_and_nothing_executes(self):
        """The model hallucinating a verb outside the whitelist must fail
        closed: speak a refusal, never call any resolver."""
        app = _app()
        response = "CONFIRM Delete Notepad.\nACTION2 delete | notepad"
        with patch("plugins.commands.app_verbs.do_focus") as mock_focus, \
             patch("plugins.commands.app_verbs.do_open") as mock_open, \
             patch("plugins.commands.app_verbs.do_close") as mock_close:
            ask_ollama.handle_response(app, response, original_text="delete notepad")
        mock_focus.assert_not_called()
        mock_open.assert_not_called()
        mock_close.assert_not_called()
        app.audio_coordinator.speak.assert_called_once()
        assert "don't know how to delete" in app.audio_coordinator.speak.call_args.args[0]

    def test_resolver_none_path_speaks_failure_and_executes_nothing(self):
        """End-to-end through the REAL do_open() -- resolve_window and
        get_app_index both report no match -- must speak a miss and never
        call the actual OS-mutating actions (launch/focus)."""
        app = _app()
        response = "CONFIRM Open flurbotron.\nACTION2 open | flurbotron"
        with patch("plugins.commands.app_verbs.resolve_window", return_value=None), \
             patch("plugins.commands.app_verbs.get_app_index") as mock_get_idx, \
             patch("plugins.commands.app_verbs.launch_app") as mock_launch, \
             patch("plugins.commands.app_verbs._force_focus") as mock_focus:
            mock_get_idx.return_value.resolve.return_value = None
            ask_ollama.handle_response(app, response, original_text="open flurbotron")
        mock_launch.assert_not_called()
        mock_focus.assert_not_called()
        app.audio_coordinator.speak.assert_called_once()
        assert "Couldn't find flurbotron" in app.audio_coordinator.speak.call_args.args[0]

    def test_not_running_gets_distinct_wording_from_not_found(self):
        app = _app()
        response = "CONFIRM Focus Claude.\nACTION2 focus | claude"
        with patch("plugins.commands.app_verbs.do_focus", return_value=ActionResult.NOT_RUNNING):
            ask_ollama.handle_response(app, response, original_text="focus claude")
        assert "claude is not running" in app.audio_coordinator.speak.call_args.args[0]


# ---------------------------------------------------------------------------
# Unsafe verb ("close") -- confirm/pending-action flow
# ---------------------------------------------------------------------------

class TestUnsafeAction2ConfirmFlow:
    def test_close_requires_confirmation_before_executing(self):
        app = _app()
        response = "CONFIRM Close Notepad.\nACTION2 close | notepad"
        with patch("plugins.commands.app_verbs.do_close") as mock_close:
            ask_ollama.handle_response(app, response, original_text="close notepad")
            mock_close.assert_not_called()
        pending = ask_ollama.get_pending_action()
        assert pending is not None
        assert pending["type"] == "action2"
        assert pending["verb"] == "close"
        assert pending["argument"] == "notepad"

    def test_confirm_yes_executes_close_and_speaks_done_only_on_success(self):
        app = _app()
        response = "CONFIRM Close Notepad.\nACTION2 close | notepad"
        with patch("plugins.commands.app_verbs.do_close", return_value=ActionResult.DONE):
            ask_ollama.handle_response(app, response, original_text="close notepad")
        with patch("plugins.commands.app_verbs.do_close", return_value=ActionResult.DONE) as mock_close:
            ask_ollama.handle_ava_confirm(app, remainder="")
        mock_close.assert_called_once_with("notepad")
        assert app.audio_coordinator.speak.call_args.args[0] == "Done."

    def test_confirm_yes_stays_silent_on_success_wording_when_not_found(self):
        app = _app()
        response = "CONFIRM Close flurbotron.\nACTION2 close | flurbotron"
        with patch("plugins.commands.app_verbs.do_close", return_value=ActionResult.DONE):
            ask_ollama.handle_response(app, response, original_text="close flurbotron")
        with patch("plugins.commands.app_verbs.do_close", return_value=ActionResult.NOT_FOUND):
            ask_ollama.handle_ava_confirm(app, remainder="")
        # Never says "Done." when the resolver couldn't find the target.
        spoken = [c.args[0] for c in app.audio_coordinator.speak.call_args_list]
        assert "Done." not in spoken

    def test_expired_pending_action_is_dropped(self):
        app = _app()
        response = "CONFIRM Close Notepad.\nACTION2 close | notepad"
        with patch("plugins.commands.app_verbs.do_close"):
            ask_ollama.handle_response(app, response, original_text="close notepad")
        with ask_ollama._pending_action_lock:
            ask_ollama._pending_action["expires"] = time.time() - 1
        with patch("plugins.commands.app_verbs.do_close") as mock_close:
            ask_ollama.handle_ava_confirm(app, remainder="")
        mock_close.assert_not_called()
        assert ask_ollama.get_pending_action() is None
