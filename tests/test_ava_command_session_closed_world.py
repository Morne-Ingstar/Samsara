"""Closed-menu resolver regressions after the separate Ava session retired.

4d29f9f replaced the executing waterfall with proposal-only resolve(). Model
commands must still belong to the offered menu; APP arguments must be grounded
in the user's request. No resolver result executes or speaks by itself.
"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ai_preferences, ava_command_session, ava_readiness
from plugins.commands import ask_ollama


NONSENSE_FABRICATIONS = [
    ('banana weather umbrella',
     'CONFIRM Do a banana weather umbrella thing.\nACTION banana weather umbrella'),
    ('purple thinking clouds today',
     'CONFIRM Set your wallpaper to purple thinking clouds.\nACTION set wallpaper to "purple thinking clouds"'),
    ('asdkfj random gibberish text',
     'CONFIRM Insert random gibberish text.\nACTION insert random gibberish text'),
    ('the quick brown fox jumps over',
     'CONFIRM Type the quick brown fox jumps over.\nACTION type the quick brown fox jumps over'),
    ('nothing here matches anything useful',
     'CONFIRM Do nothing here matches anything useful.\nACTION nothing here matches anything useful'),
]
SHORTLIST = ['screenshot', 'close tab', 'scroll up', 'mute', 'maximize', 'focus', 'open', 'close']


@pytest.fixture(autouse=True)
def _isolated_resolver(monkeypatch):
    ava_command_session.reset_cancellations()
    monkeypatch.setattr(ai_preferences, 'runtime_state', lambda *_args: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity='ollama')))
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda _app: SimpleNamespace(ready=True))
    monkeypatch.setattr(ask_ollama, 'get_system_prompt', lambda _app: '{COMMAND_LIST}')
    yield
    ava_command_session.reset_cancellations()


def _make_app():
    app = SimpleNamespace(config={}, command_executor=Mock(), audio_coordinator=Mock())
    app.command_executor.ava_menu.return_value = list(SHORTLIST)
    return app


def _resolve(app, utterance='grab the screen'):
    return ava_command_session.resolve(app, utterance, 'closed-world', 10**12)


class TestNonsenseFabricationsAreRejectedAsMiss:
    @pytest.mark.parametrize('utterance,fabricated_response', NONSENSE_FABRICATIONS)
    def test_fabricated_action_never_reaches_handle_response(self, monkeypatch, utterance, fabricated_response):
        app = _make_app()
        model = Mock(return_value=fabricated_response)
        dispatch = Mock()
        monkeypatch.setattr(ask_ollama, 'ask_ollama', model)
        monkeypatch.setattr(ask_ollama, 'handle_response', dispatch)

        assert _resolve(app, utterance) is None

        model.assert_called_once()
        dispatch.assert_not_called()
        app.command_executor.execute_command.assert_not_called()
        app.command_executor.execute_canonical.assert_not_called()

    @pytest.mark.parametrize('utterance,fabricated_response', NONSENSE_FABRICATIONS)
    def test_fabricated_action_returns_no_proposal_or_resolver_feedback(self, monkeypatch, utterance, fabricated_response):
        app = _make_app()
        monkeypatch.setattr(ask_ollama, 'ask_ollama', Mock(return_value=fabricated_response))

        assert _resolve(app, utterance) is None

        # Miss feedback belongs to the ordinary dispatcher; this resolver
        # neither owns a session miss counter nor emits speech.
        app.audio_coordinator.speak.assert_not_called()
        assert not hasattr(app, '_ava_cmd_miss_count')


class TestLegitimateSelectionsStillResolve:
    def test_verbatim_shortlist_selection_still_resolves(self, monkeypatch):
        app = _make_app()
        model = Mock(return_value='CONFIRM Take a screenshot.\nACTION screenshot')
        dispatch = Mock()
        monkeypatch.setattr(ask_ollama, 'ask_ollama', model)
        monkeypatch.setattr(ask_ollama, 'handle_response', dispatch)

        assert _resolve(app) == ava_command_session.Proposal('screenshot', provider='ollama')

        assert model.call_args.kwargs['system'] == ', '.join(SHORTLIST)
        dispatch.assert_not_called()
        app.command_executor.execute_canonical.assert_not_called()
        app.audio_coordinator.speak.assert_not_called()

    def test_valid_app_fallback_still_resolves(self, monkeypatch):
        app = _make_app()
        model = Mock(return_value='CONFIRM Open Notepad.\nAPP open | notepad')
        monkeypatch.setattr(ask_ollama, 'ask_ollama', model)

        assert _resolve(app, 'I need notepad please') == ava_command_session.Proposal(
            'open', {'remainder': 'notepad'}, provider='ollama')
        model.assert_called_once()  # exercise the model fallback, not app grammar
        app.command_executor.execute_canonical.assert_not_called()
        app.audio_coordinator.speak.assert_not_called()


def _resolve_parsed(monkeypatch, parsed, menu=None):
    app = _make_app()
    if menu is not None:
        app.command_executor.ava_menu.return_value = menu
    monkeypatch.setattr(ask_ollama, 'ask_ollama', Mock(return_value='test response'))
    monkeypatch.setattr(ask_ollama, '_parse_structured_response', lambda _response: parsed)
    return _resolve(app, 'I need notepad please')


class TestClosedWorldSelectionOkUnit:
    """Exercise selection gates through the supported resolver API."""

    def test_action_verbatim_in_shortlist_passes(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {'type': 'action', 'command': 'screenshot'}) == (
            ava_command_session.Proposal('screenshot', provider='ollama'))

    def test_action_not_in_shortlist_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action', 'command': 'set wallpaper to purple thinking clouds'}) is None

    def test_action_trailing_punctuation_is_tolerated(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {'type': 'action', 'command': 'screenshot.'}) == (
            ava_command_session.Proposal('screenshot', provider='ollama'))

    def test_action_with_trailing_extra_words_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action', 'command': 'screenshot of my desktop'}) is None

    def test_action_empty_command_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {'type': 'action', 'command': ''}) is None

    @pytest.mark.parametrize('verb', ['focus', 'open', 'close'])
    def test_action2_valid_verb_passes(self, monkeypatch, verb):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action2', 'verb': verb, 'argument': 'notepad'}) == (
            ava_command_session.Proposal(verb, {'remainder': 'notepad'}, provider='ollama'))

    def test_action2_invalid_verb_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action2', 'verb': 'delete', 'argument': 'notepad'}) is None

    def test_app_verb_absent_from_menu_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action2', 'verb': 'open', 'argument': 'notepad'}, menu=['screenshot']) is None

    def test_app_argument_not_in_request_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action2', 'verb': 'open', 'argument': 'calculator'}) is None

    def test_app_empty_argument_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'action2', 'verb': 'open', 'argument': ''}) is None

    def test_schedule_type_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {
            'type': 'schedule', 'command': 'screenshot', 'interval_seconds': 60, 'key': None}) is None

    def test_conversation_type_fails(self, monkeypatch):
        assert _resolve_parsed(monkeypatch, {'type': 'conversation'}) is None


class TestNormalizeCommandName:
    def test_strips_surrounding_quotes(self):
        assert ava_command_session._normalise_command_id('"screenshot"') == 'screenshot'

    def test_strips_trailing_punctuation(self):
        assert ava_command_session._normalise_command_id('screenshot.') == 'screenshot'

    def test_collapses_internal_whitespace(self):
        assert ava_command_session._normalise_command_id('close   tab') == 'close tab'

    def test_none_input_returns_empty_string(self):
        assert ava_command_session._normalise_command_id(None) == ''
