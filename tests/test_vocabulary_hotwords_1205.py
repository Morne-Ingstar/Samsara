"""Prose decoding never receives hotwords, including existing enabled profiles."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara import vocabulary_decode as vd
from samsara.builtin_vocabulary import relevant_words
from samsara.config_defaults import DEFAULTS
from samsara.config_schema import SETTINGS_SCHEMA


def app(enabled=None, users=('PersonalApp',)):
    model = NS(transcribe=Mock(return_value=([], NS(language='en'))),
               hf_tokenizer=NS(encode=lambda text, **kw: NS(ids=text.split())))
    store = NS(custom_vocab=users, _builtin_preferences=NS(disabled=set()))
    config = {} if enabled is None else {'builtin_vocabulary_enabled': enabled}
    return NS(config=config, model=model,
              model_rate=16000, dictionary_store=store)


@pytest.mark.parametrize('path', sorted(vd.DICTATION_PATHS) + ['future_prose_path'])
@pytest.mark.parametrize('enabled', [None, False, True])
def test_prose_ignores_builtin_and_personal_hints_preserving_explicit_context(path, enabled):
    owner = app(enabled)
    audio = object()
    params = dict(initial_prompt='Explicit context.', hotwords='Caller supplied hint',
                  language='en', temperature=(0.0, 0.2), beam_size=5, vad_filter=False)
    original = dict(params)
    vd.transcribe_with_vocabulary(owner, audio, path=path, **params)
    owner.model.transcribe.assert_called_once_with(audio, **dict(params, hotwords=None))
    assert params == original


@pytest.mark.parametrize('path', sorted(vd.COMMAND_PATHS))
@pytest.mark.parametrize('enabled', [None, True])
def test_default_and_enabled_command_wake_paths_keep_bounded_hints(path, enabled):
    owner = app(enabled)
    vd.transcribe_with_vocabulary(owner, object(), path=path, language='en')
    hints = owner.model.transcribe.call_args.kwargs['hotwords'].split(', ')
    assert 'PersonalApp' in hints and 'Samsara' in hints
    assert vd._token_count(owner.model, ', '.join(hints)) <= 223


def test_new_profile_builtin_hints_stay_on_and_prose_ignores_enabled_preference():
    assert SETTINGS_SCHEMA['builtin_vocabulary_enabled']['default'] is True
    assert DEFAULTS['builtin_vocabulary_enabled'] is True
    assert 'Samsara' in relevant_words({}, [])
    enabled = app(True)
    for _ in range(3):
        vd.transcribe_with_vocabulary(enabled, object(), path='hold_final', language='en')
    assert all(call.kwargs['hotwords'] is None for call in enabled.model.transcribe.call_args_list)


def test_prose_reports_zero_effective_terms_and_never_mutates_caller_params():
    params = {'initial_prompt': 'Explicit context.', 'hotwords': 'netflix, cube'}
    result, count = vd.vocabulary_params(app(), params, path='hold_final')
    assert count == 0 and result['hotwords'] is None
    assert result['initial_prompt'] == params['initial_prompt']
    assert params['hotwords'] == 'netflix, cube'
