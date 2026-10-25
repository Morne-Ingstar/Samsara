"""Synthetic, metadata-only evidence. No owner profiles, model loads or speech."""
from datetime import date
import ast
import json
import os
from pathlib import Path
import shutil
import sys
import types

import pytest

from tools.accuracy_audit import audit, main, render, safe_config


@pytest.fixture
def copies(tmp_path):
    logs = tmp_path / 'logs'
    logs.mkdir()
    shutil.copyfile(Path(__file__).parent / 'fixtures/845/metadata_log.txt',
                    logs / 'daily_samsara.log')
    backups = tmp_path / 'samsara-daily/config_backups'
    backups.mkdir(parents=True)
    for stamp, model, performance in [('164901', 'base.en', 'balanced'),
                                      ('174227', 'small.en', 'accurate')]:
        (backups / f'config-20261024-{stamp}.json').write_text(json.dumps({
            'model_size': model, 'performance_mode': performance,
            'initial_prompt': 'synthetic private prompt', 'api_key': 'synthetic private credential',
            'microphone_name': 'synthetic private microphone',
        }), encoding='utf-8')
    return tmp_path


def test_counts_distinct_events_and_ignores_grace_peak(copies):
    row = audit(copies, end=date(2026, 10, 24))['daily']['2026-10-24']
    assert row['delivered'] == 1
    assert row['peaks'] == [0.9961, 0.5]
    assert row['preview_corrections'] == 1
    assert row['loads'] == {'small.en / cuda / float16': 1,
                            'unknown / unknown / unknown': 1}


def test_missing_days_are_unknown_and_metadata_never_exports_text(copies):
    result = audit(copies, end=date(2026, 10, 24))
    assert len(result['daily']) == 10
    report = render(result)
    assert '| 2026-10-23 | unavailable | unavailable |' in report
    assert 'base.en | balanced' in report and 'small.en | accurate' in report
    for private in ('synthetic', 'private-model', 'private-device', 'private-precision', 'api_key'):
        assert private not in report
    assert 'clipped-sample fraction: unavailable' in report


def test_projection_rejects_nested_and_arbitrary_values():
    result = safe_config({'model_size': ['private'], 'language': 'private',
                          'compute_type': {'private': 1}, 'builtin_vocabulary_enabled': 'private'})
    assert result['model_size'] == result['language'] == result['compute_type'] == 'unknown'
    assert result['builtin_vocabulary_enabled'] == 'unknown'


def test_cli_requires_explicit_inputs_and_handles_invalid_copy(copies):
    config = copies / 'samsara/config.json'
    config.parent.mkdir()
    config.write_text('not JSON: synthetic private content', encoding='utf-8')
    output = copies / 'out/audit.md'
    assert main(['--fixtures', str(copies), '--end', '2026-10-24', '--output', str(output)]) == 0
    assert 'unreadable' in output.read_text(encoding='utf-8')
    assert 'synthetic' not in output.read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='days'):
        audit(copies, end=date(2026, 10, 24), days=0)


@pytest.mark.parametrize('failure', ['missing', 'corrupt'])
@pytest.mark.parametrize('keyword', [False, True])
def test_832_fallback_keeps_requested_small_model(monkeypatch, tmp_path, failure, keyword):
    # Extract only this pure loader; executing/importing dictation would boot the app.
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_create_whisper_model')
    namespace = {'os': os}
    exec(compile(ast.Module(body=[function], type_ignores=[]), 'dictation.py', 'exec'), namespace)
    events = []
    cached = str(tmp_path / 'cached-small')

    def resolve(model, **kwargs):
        assert model == 'small.en' and kwargs['local_files_only'] is True
        if failure == 'missing':
            raise FileNotFoundError('synthetic cache miss')
        return cached

    def construct(*args, **kwargs):
        model = args[0] if args else kwargs['model_size_or_path']
        if kwargs.get('local_files_only'):
            assert model == cached
            raise ValueError('synthetic corrupt snapshot')
        events.append(model)
        return model

    whisper = types.ModuleType('faster_whisper')
    whisper.WhisperModel = construct
    utils = types.ModuleType('faster_whisper.utils')
    utils.download_model = resolve
    monkeypatch.setitem(sys.modules, 'faster_whisper', whisper)
    monkeypatch.setitem(sys.modules, 'faster_whisper.utils', utils)
    load = namespace['_create_whisper_model']
    kwargs = dict(device='cuda', compute_type='float16',
                  _on_download=lambda: events.append('announced'))
    result = load(model_size_or_path='small.en', **kwargs) if keyword else load('small.en', **kwargs)
    assert result == 'small.en'
    assert events == ['announced', 'small.en']


def test_copied_performance_choices_change_decode_params_not_model():
    from samsara.transcription_params import get_transcription_params
    before = {'model_size': 'base.en', 'performance_mode': 'balanced'}
    after = {'model_size': 'small.en', 'performance_mode': 'accurate'}
    params = [get_transcription_params(c, language='en', initial_prompt=None,
                                      no_speech_threshold=0.6, log_prob_threshold=-1.0)
              for c in (before, after)]
    assert [p['beam_size'] for p in params] == [3, 5]
    assert [p['vad_parameters']['speech_pad_ms'] for p in params] == [200, 300]
    assert all(p['vad_filter'] and not p['without_timestamps'] for p in params)
    assert before == {'model_size': 'base.en', 'performance_mode': 'balanced'}
    assert after == {'model_size': 'small.en', 'performance_mode': 'accurate'}
