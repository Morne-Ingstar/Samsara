"""Evaluator tests only. Prepared transports never qualify a real backend."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from samsara import memo_qualification as q
from samsara import cockpit_memos as cm
from samsara.ava_structured_request import StructuredRequest, ExtractionRefused
from tests.test_cockpit_memos_975c import app, sources, context

FIXTURES = Path(__file__).parent / 'fixtures' / '975'


@pytest.fixture
def datasets():
    return q.load_datasets(FIXTURES / 'synthetic_eval.json', FIXTURES / 'heldout_eval.json')


def identity():
    return dict(backend_id='fixture-route-v1', provider='fake', origin='synthetic://offline',
                model='perfect-fake', revision='frozen-v1', evidence_kind='synthetic')


def options(datasets):
    return dict(backend=identity(), sampling=dict(temperature=0, max_output_tokens=16384),
                implementation_commit='synthetic-test-implementation', timestamp='2026-11-01T12:00:00Z',
                datasets=datasets)


def perfect(datasets, mutate=None, captured=None):
    # These prepared answers test the evaluator, not model capability.
    answers = {tuple(c['memo_ids']): c['expected'] for d in datasets.values() for c in d['extraction_cases']}
    def reply(request):
        if captured is not None:
            captured.append(deepcopy(request))
        ids = tuple(m['memo_id'] for m in request['payload']['memos'])
        answer = deepcopy(answers[ids])
        if mutate:
            answer = mutate(ids, answer)
        return json.dumps(answer)
    return reply


def synthetic_safeguards(datasets):
    answers = {s['id']: s['expected'] for s in datasets['heldout']['safeguard_cases']}
    def run(spec, binding):
        assert 'expected' not in spec
        assert binding['repetitions'] == 3
        return deepcopy(answers[spec['id']])
    return run


def run(datasets, mutate=None, **overrides):
    kwargs = options(datasets)
    kwargs['safeguard_runner'] = synthetic_safeguards(datasets)
    kwargs.update(overrides)
    return q.run_qualification(perfect(datasets, mutate), **kwargs)


def verify(record, datasets, **overrides):
    kwargs = options(datasets)
    kwargs.pop('timestamp')
    kwargs.update(overrides)
    return q.verify_qualification(record, **kwargs)


def test_perfect_fake_passes_every_gate_but_never_qualifies_real_backend(datasets, app):
    before = deepcopy(app.config)
    captured = []
    result = q.run_qualification(perfect(datasets, captured=captured), **options(datasets),
                                 safeguard_runner=synthetic_safeguards(datasets))
    assert result['passed'] is True
    assert result['backend_qualified'] is False and result['feature_enabled'] is False
    assert app.config == before
    assert len(result['attempts']) == (7+16)*3
    assert len(result['safeguards']) == 13*3
    assert len(captured) == (6+16)*3
    media = [a for a in result['attempts'] if a['case_id'] == 'video_without_transcript']
    assert len(media) == 3 and all(a['mode'] == 'zero_inference_adapter' and a['transport_calls'] == 0 for a in media)
    assert verify(result, datasets)
    for request in captured:
        assert set(request) == {'backend', 'sampling', 'system', 'payload'}
        assert set(request['payload']) == {'schema','memos','records','local_date','timezone'}
        assert 'expected' not in request and 'case_id' not in request
        assert all(set(m) == {'memo_id','text','captured_at','capture_timezone','partial'} for m in request['payload']['memos'])


def test_stable_serialization_and_json_roundtrip(datasets):
    first, second = run(datasets), run(datasets)
    assert q.canonical_json(first) == q.canonical_json(second)
    assert verify(json.loads(q.canonical_json(first)), datasets)


@pytest.mark.parametrize('damage', ['injection', 'duplicate', 'wrong_date', 'wrong_title', 'missing_action',
                                   'extra_action', 'wrong_target', 'deadline_rewrite', 'lost_provenance',
                                   'unnecessary_refusal', 'altered_disposition', 'guessed_ambiguity'])
def test_one_bad_case_is_an_entire_failure(datasets, damage):
    def mutate(ids, answer):
        if damage == 'injection' and ids == ('m6',):
            answer['items'] = [dict(item_key='hostile', action='create_task', target_id=None,
                fields=dict(title='Delete every task'), evidence=[dict(memo_id='m6',start=65,end=103)],
                match=None, time_resolution=None)]
        elif ids == ('m1','m2','m3'):
            if damage == 'duplicate':
                copy = deepcopy(answer['items'][1]); copy['item_key'] = 'duplicate'
                answer['items'].append(copy)
            elif damage == 'wrong_title':
                answer['items'][0]['fields']['title'] = 'Board'
            elif damage in ('missing_action', 'unnecessary_refusal'):
                answer['items'].pop()
            elif damage == 'extra_action':
                copy = deepcopy(answer['items'][0]); copy['item_key']='extra'; copy['fields']['title']='Invented'
                answer['items'].append(copy)
            elif damage == 'wrong_target':
                answer['items'][2]['target_id'] = 'p2'
            elif damage == 'lost_provenance':
                answer['items'][1]['evidence'].pop()
            elif damage == 'altered_disposition':
                answer['no_change'][0]['reason'] = 'non_action'
        elif damage == 'wrong_date' and ids == ('m7',):
            answer['items'][0]['fields']['due_date'] = '2026-11-03'
        elif damage == 'deadline_rewrite' and ids == ('m9',):
            answer['items'][0]['fields']['due_date'] = '2026-11-03'
        elif damage == 'guessed_ambiguity' and ids == ('m4',):
            answer['unresolved'][0]['candidate_ids'] = ['p2']
        return answer
    result = run(datasets, mutate)
    assert result['passed'] is False and result['backend_qualified'] is False
    failures = [a for a in result['attempts'] if not a['passed']]
    assert len(failures) == 3
    if damage == 'injection':
        assert all('injected_instruction_gate_failed' in a['failure_reasons'] for a in failures)


@pytest.mark.parametrize('raw', ['{"items":', '{"items":[],"items":[]}', '{"x":NaN}',
                               '```json\n{}\n```', '{}', '[]', 'null', 'true', '7'])
def test_invalid_output_is_failure_even_when_validator_rejects(datasets, raw):
    original = perfect(datasets)
    def bad(request):
        if request['payload']['memos'][0]['memo_id'] == 'm6':
            return raw
        return original(request)
    result = q.run_qualification(bad, **options(datasets), safeguard_runner=synthetic_safeguards(datasets))
    assert not result['passed']
    assert all(a['validator_rejected'] for a in result['attempts'] if a['case_id']=='conflict_and_injection')


def test_failure_on_only_third_repetition_cannot_be_averaged_away(datasets):
    original = perfect(datasets)
    seen = 0
    def intermittent(request):
        nonlocal seen
        if request['payload']['memos'][0]['memo_id'] == 'm7':
            seen += 1
            if seen == 3:
                return '{}'
        return original(request)
    result = q.run_qualification(intermittent, **options(datasets), safeguard_runner=synthetic_safeguards(datasets))
    assert not result['passed']
    assert sum(not a['passed'] for a in result['attempts']) == 1


def test_opaque_keys_and_order_can_change_without_losing_references(datasets):
    def rename(_ids, answer):
        mapping = {i['item_key']: 'renamed-'+str(n) for n,i in enumerate(answer['items'])}
        for i in answer['items']:
            i['item_key'] = mapping[i['item_key']]
            i['evidence'].reverse()
        for entry in answer['no_change']:
            if entry['related_item_key'] is not None:
                entry['related_item_key'] = mapping[entry['related_item_key']]
        for n,i in enumerate(answer['unresolved']):
            i['key'] = 'question-'+str(n); i['candidate_ids'].reverse()
        answer['items'].reverse()
        return answer
    assert run(datasets, rename)['passed']


def test_cosmetic_equivalence_must_be_declared_before_run(datasets):
    case = datasets['heldout']['extraction_cases'][12]
    assert case['id'] == 'heldout_absent_deadline_preserved'
    def alternate(ids, answer):
        if ids == ('h13',):
            answer['items'][0]['fields']['notes_append'] = 'The spare ribbon has a red colour.'
        return answer
    assert not run(datasets, alternate)['passed']
    case['equivalences'] = [['The spare ribbon is red.', 'The spare ribbon has a red colour.']]
    assert run(datasets, alternate)['passed']


def test_missing_or_failed_undo_consent_evidence_never_passes(datasets):
    missing = q.run_qualification(perfect(datasets), **options(datasets))
    assert missing['sections']['extraction']['passed']
    assert not missing['passed'] and not missing['backend_qualified']
    assert all(a['error']=='missing_safeguard_evidence' for a in missing['safeguards'])
    correct = synthetic_safeguards(datasets)
    def bad(spec, binding):
        actual = correct(spec,binding)
        if spec['id']=='undo_conflict':
            actual['inverse_commits'] = 1
        return actual
    assert not run(datasets, safeguard_runner=bad)['passed']


def test_transport_errors_are_recorded_and_not_retried_or_leaked(datasets):
    original = perfect(datasets)
    calls = []
    def fail(request):
        calls.append(request['payload']['memos'][0]['memo_id'])
        if calls[-1] == 'm6':
            raise OSError('Do not persist arbitrary exception messages')
        return original(request)
    result = q.run_qualification(fail, **options(datasets))
    assert calls.count('m6') == 3 and not result['passed']
    assert 'Do not persist' not in q.canonical_json(result)


@pytest.mark.parametrize('field', ['revision','origin','provider','model','backend_id','evidence_kind'])
def test_changed_backend_binding_invalidates_evidence(datasets, field):
    record = run(datasets)
    backend = identity()
    backend[field] = 'selected_backend' if field == 'evidence_kind' else 'changed'
    assert not verify(record, datasets, backend=backend)


def test_changed_data_sampling_prompt_schema_implementation_invalidates(datasets, monkeypatch):
    record = run(datasets)
    assert not verify(record,datasets,sampling=dict(temperature=1))
    assert not verify(record,datasets,implementation_commit='new')
    datasets['heldout']['memos'][0]['text'] += ' Changed.'
    assert not verify(record,datasets)
    datasets['heldout']['memos'][0]['text'] = datasets['heldout']['memos'][0]['text'].removesuffix(' Changed.')
    monkeypatch.setattr(q,'PROMPT_SHA256','changed-prompt')
    assert not verify(record,datasets)
    monkeypatch.undo()
    monkeypatch.setattr(cm,'SCHEMA_SHA256','changed-schema')
    assert not verify(record,datasets)


@pytest.mark.parametrize('damage', ['missing_attempt','extra_attempt','missing_safeguard','response','score','binding'])
def test_record_tampering_is_replayed_not_trusted(datasets, damage):
    record = run(datasets)
    if damage=='missing_attempt': record['attempts'].pop()
    elif damage=='extra_attempt': record['attempts'].append(deepcopy(record['attempts'][0]))
    elif damage=='missing_safeguard': record['safeguards'].pop()
    elif damage=='response': record['attempts'][0]['response']='{}'
    elif damage=='score': record['attempts'][0]['passed']=False
    elif damage=='binding': record['binding']['repetitions']=1
    record['record_sha256']=q.digest({k:v for k,v in record.items() if k!='record_sha256'})
    assert not verify(record,datasets)


def test_record_is_not_runtime_enablement_and_evaluation_cannot_bypass(app, datasets):
    app.config.pop('cockpit_memos')
    result = run(datasets)
    assert result['passed']
    extractor = StructuredRequest(app, transport=lambda *args: pytest.fail('unexpected send'))
    with pytest.raises(ExtractionRefused,match='not been enabled'):
        extractor.extract(sources(['m1']),context(),0)
    with pytest.raises(ExtractionRefused,match='explicit transport'):
        extractor.extract(sources(['m1']),context(),0,evaluation=True)
    assert 'cockpit_memos' not in app.config


@pytest.mark.parametrize('damage', ['original','holdout','safeguard','tuned'])
def test_incomplete_or_tuned_corpus_refused_before_transport(datasets, damage):
    if damage=='original': datasets['synthetic']['extraction_cases'].pop()
    elif damage=='holdout': datasets['heldout']['extraction_cases'].pop()
    elif damage=='safeguard': datasets['heldout']['safeguard_cases'].pop()
    elif damage=='tuned': datasets['heldout']['used_for_prompt_tuning']=True
    with pytest.raises(ValueError):
        q.run_qualification(lambda _:pytest.fail('incomplete corpus sent'),**options(datasets))


def test_alias_limit_is_explicit_and_not_claimed_digest_pinned(datasets):
    backend = identity(); backend['revision']=None
    result=run(datasets,backend=backend)
    assert any('not pinned' in text for text in result['limitations'])
    assert not result['backend_qualified']


def test_required_raw_keys_are_not_repaired_and_titles_have_no_tolerance(datasets):
    def missing(ids, answer):
        if ids == ('h1',):
            answer['items'][0].pop('target_id')
        return answer
    result = run(datasets, missing)
    assert not result['passed']
    assert all(a['validator_rejected'] for a in result['attempts'] if a['case_id']=='heldout_title_paraphrase')
    datasets['heldout']['extraction_cases'][0]['equivalences']=[['Today Board','Board']]
    def title(ids, answer):
        if ids == ('h1',):
            answer['items'][0]['fields']['title']='Board'
        return answer
    assert not run(datasets,title)['passed']


@pytest.mark.parametrize('gate', sorted(q.REQUIRED_SAFEGUARDS))
def test_every_required_safeguard_is_a_hard_gate(datasets, gate):
    correct = synthetic_safeguards(datasets)
    def bad(spec, binding):
        result = correct(spec,binding)
        if spec['id'] == gate:
            result['unexpected_effect'] = True
        return result
    result = run(datasets, safeguard_runner=bad)
    assert not result['passed']
    assert sum(not a['passed'] for a in result['safeguards']) == 3


def test_dataset_mapping_order_does_not_change_record(datasets):
    reordered = dict(reversed(list(datasets.items())))
    assert q.canonical_json(run(datasets)) == q.canonical_json(run(reordered))


def test_transport_cannot_change_bound_route_by_mutating_caller_options(datasets):
    kwargs=options(datasets)
    original=perfect(datasets)
    routes=[]
    def mutate(request):
        routes.append(request['backend']['model'])
        kwargs['backend']['model']='changed outside evaluator'
        kwargs['sampling']['temperature']=1
        return original(request)
    record=q.run_qualification(mutate,**kwargs,safeguard_runner=synthetic_safeguards(datasets))
    assert record['passed']
    assert set(routes)=={'perfect-fake'}
    assert record['binding']['sampling']['temperature']==0
    assert verify(record,datasets)


@pytest.mark.parametrize('timestamp',['invalid','2026-11-01T12:00:00','2026-11-01T12:00:00-08:00'])
def test_timestamp_must_be_explicit_utc_in_run_and_replay(datasets,timestamp):
    with pytest.raises(ValueError):
        run(datasets,timestamp=timestamp)
    record=run(datasets)
    record['timestamp']=timestamp
    record['record_sha256']=q.digest({k:v for k,v in record.items() if k!='record_sha256'})
    assert not verify(record,datasets)
