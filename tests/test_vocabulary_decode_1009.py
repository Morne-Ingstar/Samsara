"""1009: real decode seams reach a spy model; no model load or app bootstrap."""
import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.builtin_vocabulary import word_key
from samsara.command_packs import PACKS
from samsara.dictionary_store import DictionaryStore
from samsara import vocabulary_decode as vd

ROOT = Path(__file__).resolve().parents[1]
FILES = ('dictation.py', 'samsara/streaming.py', 'samsara/command_fast_path.py',
         'samsara/decode_recovery.py', 'samsara/ui/wake_word_debug_qt.py')


def app_store(tmp_path, config=None, users=('PersonalTerm',)):
    config_path = tmp_path / 'config.json'
    config_path.write_text('{}', encoding='utf-8')
    (tmp_path / 'training_data.json').write_text(json.dumps({
        'vocabulary': list(users), 'corrections': {}}), encoding='utf-8')
    # Costs one token per character: deliberately stresses the token budget.
    model = SimpleNamespace(hf_tokenizer=SimpleNamespace(
        encode=lambda text, **kw: SimpleNamespace(ids=list(text))),
        transcribe=Mock(return_value=([], SimpleNamespace(language='en'))))
    app = SimpleNamespace(config=config or {}, config_path=config_path, model=model)
    app.dictionary_store = DictionaryStore(app)
    return app, app.dictionary_store


def seams():
    result = []
    for filename in FILES:
        tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'transcribe_with_vocabulary':
                path = next(kw.value.value for kw in call.keywords if kw.arg == 'path')
                result.append(pytest.param(filename, call, path, id=path))
    return result


@pytest.mark.parametrize('filename,call,path', seams())
def test_every_application_decode_seam_delivers_vocabulary(tmp_path, filename, call, path):
    """Check source wiring and spy boundary kwargs without executing app source.

    Resolve only names/attributes/constants from the checked-in call expression.
    App method execution is covered separately by the approved bootstrap adapter
    in test_transcription_params; never compile/exec snippets from dictation.py.
    """
    app, _ = app_store(tmp_path)
    params = {'language': 'en', 'initial_prompt': None, 'vad_filter': False}
    env = dict(app=app, self=app, audio=object(), chunk=object(),
               audio_faded=object(), concatenated_audio=object(), params=params,
               transcribe_params=params, initial_prompt=None,
               resolve_transcribe_language=lambda _: 'en',
               transcribe_with_vocabulary=vd.transcribe_with_vocabulary)
    app._app = app  # Wake-word debug window's app reference.
    def value(node):
        if isinstance(node, ast.Name):
            return env[node.id]
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Attribute):
            return getattr(value(node.value), node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'resolve_transcribe_language':
            return env[node.func.id](*(value(arg) for arg in node.args))
        pytest.fail(f'Unrecognized decode argument: {ast.dump(node)}')
    kwargs = {}
    for kw in call.keywords:
        if kw.arg is None:
            kwargs.update(value(kw.value))
        else:
            kwargs[kw.arg] = value(kw.value)
    vd.transcribe_with_vocabulary(*(value(arg) for arg in call.args), **kwargs)
    kwargs = app.model.transcribe.call_args.kwargs
    words = kwargs['hotwords'].split(', ')
    assert 'PersonalTerm' in words
    assert 'wake word' in words
    assert not kwargs.get('initial_prompt')
    assert vd._token_count(app.model, kwargs['hotwords']) <= 223


def test_no_active_application_call_bypasses_helper():
    found = set()
    for filename in FILES:
        tree = ast.parse((ROOT / filename).read_text(encoding='utf-8'))
        assert any(isinstance(node, ast.ImportFrom) and node.module == 'samsara.vocabulary_decode'
                   and any(alias.name == 'transcribe_with_vocabulary' for alias in node.names)
                   for node in ast.walk(tree)), filename
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr != 'transcribe', (filename, node.lineno)
            if isinstance(node.func, ast.Name) and node.func.id == 'transcribe_with_vocabulary':
                found.add(next(kw.value.value for kw in node.keywords if kw.arg == 'path'))
    assert found == set(vd.PATHS)


def test_user_then_core_then_other_packs_and_complete_terms(tmp_path):
    app, store = app_store(tmp_path, users=[f'Personal{i}' for i in range(16)])
    params, count = vd.vocabulary_params(app, {})
    terms = params['hotwords'].split(', ')
    assert terms[:16] == store.custom_vocab
    assert terms[16:18] == ['jarvis', 'Samsara']
    assert 'stremio' not in terms
    assert count == len(terms)
    assert vd._token_count(app.model, params['hotwords']) <= 223


def test_oversize_user_term_does_not_evict_short_user_terms(tmp_path):
    app, _ = app_store(tmp_path, users=['x' * 300, 'First', 'Second'])
    params, _ = vd.vocabulary_params(app, {})
    assert params['hotwords'].startswith('First, Second, jarvis, Samsara')
    assert 'x' * 300 not in params['hotwords']


def test_disabled_pack_word_and_master_excluded_user_spelling_wins(tmp_path):
    app, store = app_store(tmp_path, {'command_packs': {name: False for name in PACKS}},
                           users=['sAmSaRa'])
    terms = vd._terms(app)
    assert terms[0] == 'sAmSaRa'
    assert sum(word_key(w) == 'samsara' for w in terms) == 1
    assert 'Hermes' not in terms and 'stremio' not in terms
    assert 'wake word' in terms
    assert store.set_builtin_word_enabled('wake word', False)
    assert 'wake word' not in vd._terms(app)
    app.config['builtin_vocabulary_enabled'] = False
    assert vd._terms(app) == ['sAmSaRa']


@pytest.mark.parametrize('prompt', [None, '', 'User context.', 'word ' * 300, 'x' * 300])
def test_combined_budget_preserves_prompt_when_it_fits(tmp_path, prompt):
    app, _ = app_store(tmp_path)
    original = {'initial_prompt': prompt, 'temperature': 0.0, 'vad_filter': False}
    result, _ = vd.vocabulary_params(app, original)
    assert original['initial_prompt'] == prompt
    if not prompt or vd._token_count(app.model, prompt) <= 223:
        assert result['initial_prompt'] == prompt
    assert vd._token_count(app.model, result.get('initial_prompt')) + vd._token_count(app.model, result['hotwords']) <= 223
    assert result['temperature'] == 0.0 and result['vad_filter'] is False


def test_no_tokenizer_uses_conservative_utf8_budget(tmp_path):
    app, _ = app_store(tmp_path, users=['你好' * 50, 'ShortTerm'])
    app.model.hf_tokenizer = None
    result, _ = vd.vocabulary_params(app, {})
    assert result['hotwords'].startswith('ShortTerm')
    assert len((' ' + result['hotwords']).encode('utf-8')) <= 223


def test_log_once_per_session_path_and_effective_change_without_terms(tmp_path, caplog):
    app, store = app_store(tmp_path)
    records = lambda: [r for r in caplog.records if r.name == vd.__name__]
    with caplog.at_level(logging.DEBUG, logger=vd.__name__):
        vd.log_vocabulary(app)
        for _ in range(3):
            vd.transcribe_with_vocabulary(app, [], path='wake')
        assert len(records()) == 2  # Summary and one actual-path count.
        store.add_vocab_word('NewPersonalTerm')
        assert len(records()) == 3  # Store change, before another utterance.
        vd.transcribe_with_vocabulary(app, [], path='wake')
        vd.transcribe_with_vocabulary(app, [], path='wake')
        assert len(records()) == 4
        vd.log_vocabulary(app)
        assert len(records()) == 5  # Next session, even with identical terms.
    assert 'PersonalTerm' not in caplog.text
    assert 'paths=hold,hold_long' in caplog.text
    assert 'reason=change' in caplog.text and 'terms=' in caplog.text


def test_config_changes_detected_at_decode_without_spam(tmp_path, caplog):
    app, _ = app_store(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=vd.__name__):
        vd.transcribe_with_vocabulary(app, [], path='wake')
        app.config['builtin_vocabulary_enabled'] = False
        vd.transcribe_with_vocabulary(app, [], path='wake')
        vd.transcribe_with_vocabulary(app, [], path='wake')
    assert len(caplog.records) == 4
    assert app.model.transcribe.call_args.kwargs['hotwords'] == 'PersonalTerm'


def test_prefix_cannot_silently_suppress_hotwords(tmp_path):
    app, _ = app_store(tmp_path)
    with pytest.raises(ValueError, match='prefix'):
        vd.transcribe_with_vocabulary(app, [], path='hold', prefix='forced')
    app.model.transcribe.assert_not_called()
