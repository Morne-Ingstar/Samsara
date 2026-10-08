"""Cancellation contracts after retirement of the separate Ava capture session.

4d29f9f replaced session generations/queue cancellation with request tokens;
d5a0b73 moved interpretation into ordinary command misses. Capture leases now
belong to the normal toggle session (test_wake_consumer_lifecycle.py).
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ai_preferences, ava_command_session as resolver, ava_readiness
from plugins.commands import ask_ollama


@pytest.fixture(autouse=True)
def _isolated_requests():
    resolver.reset_cancellations()
    yield
    resolver.reset_cancellations()


@pytest.fixture
def rig(monkeypatch):
    app = SimpleNamespace(config={}, command_executor=Mock(), audio_coordinator=Mock())
    app.command_executor.ava_menu.return_value = ['screenshot']
    monkeypatch.setattr(ai_preferences, 'runtime_state', lambda *_args: SimpleNamespace(
        allowed=True, provider=SimpleNamespace(identity='ollama')))
    monkeypatch.setattr(ava_readiness, 'readiness_for', lambda _app: SimpleNamespace(ready=True))
    monkeypatch.setattr(ask_ollama, 'get_system_prompt', lambda _app: '{COMMAND_LIST}')
    model = Mock(return_value='CONFIRM Take a screenshot.\nACTION screenshot')
    dispatch = Mock()
    monkeypatch.setattr(ask_ollama, 'ask_ollama', model)
    monkeypatch.setattr(ask_ollama, 'handle_response', dispatch)
    now = [1.0]
    monkeypatch.setattr(resolver.time, 'monotonic', lambda: now[0])
    return SimpleNamespace(app=app, model=model, dispatch=dispatch, now=now,
                           run=lambda: resolver.resolve(app, 'grab the screen', 'request', 3.5))


class TestRequestCancellation:
    def test_reset_rearms_requests(self):
        resolver.cancel_all()
        resolver.reset_cancellations()
        assert not resolver.is_cancelled('request')

    def test_cancel_stays_set_until_reset(self):
        resolver.cancel('request')
        assert resolver.is_cancelled('request')
        assert resolver.is_cancelled('request')
        assert not resolver.is_cancelled('other')
        resolver.reset_cancellations()
        assert not resolver.is_cancelled('request')

    def test_cancel_all_covers_existing_and_new_requests_until_reset(self):
        resolver.cancel_all()
        assert resolver.is_cancelled('existing')
        assert resolver.is_cancelled('new')
        resolver.reset_cancellations()
        assert not resolver.is_cancelled('existing')
        assert not resolver.is_cancelled('new')


@pytest.mark.parametrize('reason', ['request', 'all', 'deadline'])
def test_invalid_request_never_calls_model(rig, reason):
    if reason == 'request':
        resolver.cancel('request')
    elif reason == 'all':
        resolver.cancel_all()
    else:
        rig.now[0] = 4.0
    assert rig.run() is None
    rig.model.assert_not_called()
    rig.app.command_executor.ava_menu.assert_not_called()


@pytest.mark.parametrize('reason', ['request', 'all', 'deadline'])
def test_invalidation_during_model_call_discards_answer_without_effects(rig, reason):
    def late_answer(*_args, **_kwargs):
        if reason == 'request':
            resolver.cancel('request')
        elif reason == 'all':
            resolver.cancel_all()
        else:
            rig.now[0] = 4.0
        return 'CONFIRM Take a screenshot.\nACTION screenshot'

    rig.model.side_effect = late_answer
    assert rig.run() is None
    rig.model.assert_called_once()
    rig.dispatch.assert_not_called()
    rig.app.command_executor.execute_canonical.assert_not_called()
    rig.app.audio_coordinator.speak.assert_not_called()


def test_fresh_answer_returns_proposal_without_dispatch_or_speech(rig):
    proposal = rig.run()
    assert proposal == resolver.Proposal('screenshot', provider='ollama')
    rig.model.assert_called_once()
    rig.dispatch.assert_not_called()
    rig.app.command_executor.execute_canonical.assert_not_called()
    rig.app.audio_coordinator.speak.assert_not_called()


@pytest.mark.parametrize('retired_key', [
    'key', 'miss_limit', 'queue_depth_cap', 'inactivity_timeout_s',
    'ready_cue_enabled', 'ready_cue_dir',
])
def test_retired_capture_config_is_removed_without_changing_interpretation(retired_key):
    # Bind the exact production migration without importing dictation/bootstrap.
    path = Path(__file__).resolve().parents[1] / 'dictation.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'DictationApp')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                  and n.name == '_migrate_retired_ava_command_session_config')
    namespace = {'logger': logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
    kept = {'interpret_misses': False, 'model': 'chosen-model', 'backend': 'ollama'}
    app = SimpleNamespace(config={'ava_command_session': {**kept, retired_key: 'old'}},
                          save_config=Mock())
    migrate = namespace[method.name]
    migrate(app)
    assert app.config['ava_command_session'] == kept
    app.save_config.assert_called_once_with()
    app.save_config.reset_mock()
    migrate(app)
    assert app.config['ava_command_session'] == kept
    app.save_config.assert_not_called()
