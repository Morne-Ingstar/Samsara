"""C1/C2: synthetic loaded registries, no OS effects or second plugin loader."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import ava_capabilities as cap, command_catalog, plugin_commands
from samsara import execution_policy as ep
from samsara.command_registry import CommandMatcher
from samsara.commands import CommandExecutor


def _handler(_app, _remainder):
    """Perform a synthetic navigation operation."""
    raise AssertionError('discovery must never execute a handler')


@pytest.fixture
def make_executor(monkeypatch):
    def build(extra=(), *, count=35):
        registry = {}
        for i in range(count):
            phrase = f'fixture operation {i:02d}'
            registry[phrase] = dict(phrase=phrase, source='fixtures.navigation', func=_handler,
                                    aliases=[f'fixture alias {i:02d}'], risk_class='safe', pack='core')
        for item in extra:
            registry[item['phrase']] = dict({'source': 'fixtures.contracts', 'func': _handler,
                                            'aliases': [], 'pack': 'core', 'risk_class': 'safe'}, **item)
        # The policy and matcher must see exactly the supplied runtime registry.
        monkeypatch.setattr(plugin_commands, '_REGISTRY', registry)
        ex = CommandExecutor.__new__(CommandExecutor)
        ex.commands = {'fixture home': {'type': 'hotkey', 'keys': ['home']}}
        ex._plugin_registry = registry
        ex._capability_generation = 1
        ex._app = SimpleNamespace(config={}, _ava_cmd_generation=0, command_executor=ex)
        matcher = CommandMatcher()
        matcher.load_builtins(ex.commands)
        matcher.load_plugins(registry)
        matcher.set_enabled_packs({'core'})
        matcher.freeze()
        ex._matcher = matcher
        return ex
    return build


def _by_phrase(index):
    assert not index.error
    return {r.record()['canonical_phrase']: r.record() for r in index.records}


def test_inventory_covers_entire_supplied_registry_with_contract_or_exact_reason(make_executor, monkeypatch):
    ex = make_executor([
        dict(phrase='required remainder', param_schema={'remainder': {'type': 'str', 'required': True, 'max_len': 99}}),
        dict(phrase='typed volume', param_schema={'level': {'type': 'int', 'min': 0, 'max': 100, 'units': 'percent'}}),
        dict(phrase='hidden apply', ai_visible=False),
        dict(phrase='disabled pack', pack='off'),
        dict(phrase='wrong scope', scope={'apps': ['fixture.exe']}),
        dict(phrase='unvalidated write', risk_class='unknown'),
        dict(phrase='policy denied', risk_class='destructive'),
        dict(phrase='missing handler', func=None),
        dict(phrase='malformed schema', param_schema={'remainder': {'type': 'str', 'required': 'yes'}}),
    ])
    monkeypatch.setattr(ep, 'authorize', Mock(side_effect=AssertionError('not a discovery operation')))
    monkeypatch.setattr(plugin_commands, 'load_plugins', Mock(side_effect=AssertionError('no second loader')))
    index = ex.capability_index()
    records = _by_phrase(index)
    assert set(records) == {e.phrase for e in ex._matcher._entries.values()}
    reasons = {'hidden apply': 'ai_hidden', 'disabled pack': 'pack_disabled',
               'wrong scope': 'out_of_scope', 'typed volume': 'adapter_unavailable',
               'unvalidated write': 'unvalidated', 'policy denied': 'not_allowed_for_model',
               'missing handler': 'no_handler',
               'malformed schema': 'invalid_schema'}
    for phrase, record in records.items():
        assert record['available'] or record['reason']
        assert record['reason'] == reasons.get(phrase, '')
        assert record['schema_version'] == cap.SCHEMA_VERSION
    required = records['required remainder']
    assert required['available'] and required['argument_schema']['remainder']['required'] is True
    assert required['adapter'] == 'literal_remainder/v1'
    for record in records.values():
        assert index.executor_ids[record['id']] == record['canonical_phrase']
        assert index.documentation_ids[record['documentation_id']] == record['canonical_phrase']


def test_required_argument_is_discovered_without_args_then_validated_at_bind(make_executor):
    ex = make_executor([dict(phrase='required remainder', param_schema={
        'remainder': {'type': 'str', 'required': True, 'max_len': 99}})])
    request = ex.capability_request('required-test')
    result = request.initial('required remainder')
    record = result['records'][0]
    assert record['available']
    with pytest.raises(cap.CapabilityError, match='invalid_args'):
        request.bind(record['id'])
    invocation = request.bind(record['id'], {'remainder': 'literal fixture target'})
    assert invocation.command_id == 'required remainder'
    assert invocation.args == {'remainder': 'literal fixture target'}
    assert invocation.route is ep.Route.MODEL
    with pytest.raises(cap.CapabilityError, match='invalid_args'):
        request.bind(record['id'], {'remainder': 'literal', 'shell': 'invented'})


def test_keyword_retrieval_finds_an_unoffered_command_beyond_first_page(make_executor):
    ex = make_executor(count=60)
    request = ex.capability_request('keyword-test')
    first = request.initial()
    assert len(first['records']) <= 12 and not first['search_complete']
    omitted = next(r.record() for r in request.index.records if r.id not in request.offered)
    with pytest.raises(cap.CapabilityError, match='unoffered'):
        request.bind(omitted['id'])
    found = request.lookup(query=omitted['canonical_phrase'])
    assert found['records'][0]['id'] == omitted['id']
    assert request.bind(omitted['id']).command_id == omitted['canonical_phrase']
    assert request.bind_legacy(omitted['canonical_phrase']).command_id == omitted['canonical_phrase']
    with pytest.raises(cap.CapabilityError, match='unoffered'):
        request.bind_legacy('navigation.fixture_home')


def test_category_and_page_cursors_rank_the_entire_index_and_remember_query(make_executor):
    request = make_executor(count=50).capability_request('category-test')
    initial = request.initial()
    category = initial['categories'][0]['id']
    first = request.lookup(query='', category=category)
    second = request.lookup(cursor=first['next_cursor'])
    assert first['total_matches'] == second['total_matches'] == 51
    assert {r['id'] for r in first['records']}.isdisjoint(r['id'] for r in second['records'])
    request.lookup(query='absent fixture concept')
    exhausted = request.lookup(query='fixture home')
    assert exhausted['reason'] == 'lookup_budget_exhausted'
    assert exhausted['search_complete'] is False


def test_deterministic_weighted_ranking_and_normalization(make_executor):
    ex = make_executor([dict(phrase='volume control', aliases=['sound loudness'])])
    index = ex.capability_index()
    volume = _by_phrase(index)['volume control']['id']
    assert index.rank('SOUND, LOUDNESS!')[0] == volume
    assert index.rank('louder')[0] == volume
    # Stable IDs break equal-score ties, irrespective of registry iteration order.
    ranked = index.rank('fixture')
    # The builtin also hits its description (8+3); plugins hit names (8).
    assert index.by_id[ranked[0]].record()['canonical_phrase'] == 'fixture home'
    assert ranked[1:] == sorted(ranked[1:])
    assert index.revision == ex.capability_index().revision
    assert cap._tokens('please editing selected windows') == {'edit', 'selection', 'window'}


@pytest.mark.parametrize('change', ['schema', 'handler', 'pack', 'generation', 'matcher', 'description', 'policy', 'scope'])
def test_contract_or_availability_changes_refuse_old_offers(make_executor, change):
    ex = make_executor()
    request = ex.capability_request('old-turn')
    offered = request.initial('fixture operation 00')['records'][0]
    entry = ex._registry_entry(offered['canonical_phrase'])
    if change == 'schema':
        ex._plugin_registry[entry.phrase]['param_schema'] = {'remainder': {'type': 'str', 'required': True}}
    elif change == 'handler':
        ex._plugin_registry[entry.phrase]['func'] = lambda *_a: True
    elif change == 'pack':
        ex._matcher.set_enabled_packs(set())
    elif change == 'generation':
        ex._app._ava_cmd_generation += 1
    elif change == 'matcher':
        ex._capability_generation += 1
    elif change == 'description':
        entry.description = 'Changed effect explanation.'
    elif change == 'policy':
        ex._app.config['execution_policy'] = {'model_tool_allowlist': []}
    else:
        from samsara.command_scope import parse_scope
        entry.scope = parse_scope({'apps': ['unavailable.exe']})
    with pytest.raises(cap.CapabilityError, match='stale'):
        request.bind(offered['id'])


def test_maps_and_cursors_are_request_owned_and_immutable(make_executor):
    ex = make_executor()
    one, two = ex.capability_request('one'), ex.capability_request('two')
    initial = one.initial()
    with pytest.raises(cap.CapabilityError, match='invalid_cursor'):
        two.lookup(cursor=initial['next_cursor'])
    with pytest.raises(cap.CapabilityError, match='unoffered'):
        two.bind(initial['records'][0]['id'])
    with pytest.raises(TypeError):
        one.offered['forged'] = initial['records'][0]
    original = initial['records'][0]['canonical_phrase']
    initial['records'][0]['canonical_phrase'] = 'forged'
    assert one.bind(initial['records'][0]['id']).command_id == original


def test_failed_registry_offers_no_static_fallback(make_executor, monkeypatch):
    ex = make_executor()
    monkeypatch.setattr(command_catalog, 'capability_metadata', Mock(side_effect=RuntimeError('broken registry')))
    monkeypatch.setattr(command_catalog, 'load_catalog_json', Mock(side_effect=AssertionError('no fallback')))
    index = ex.capability_index(documentation=[{'canonical_id': 'fixture.static', 'phrase': 'open chrome'}])
    assert index.error == 'registry_unavailable' and not index.records
    request = ex.capability_request('failed')
    result = request.initial()
    assert not result['records'] and not result['search_complete'] and not request.offered
    with pytest.raises(cap.CapabilityError, match='unoffered'):
        request.bind_legacy('open chrome')


def test_documentation_only_rows_never_become_runtime_authority(make_executor):
    ex = make_executor()
    index = ex.capability_index(documentation=[dict(canonical_id='fixture.static_command',
        phrase='static command', description='Static documentation only.', aliases=['static command'])])
    record = _by_phrase(index)['static command']
    assert record['documentation_only'] and not record['available']
    assert record['reason'] == 'documentation_only'
    assert record['id'] not in index.executor_ids


@pytest.mark.parametrize('size,context,expected', [(6500,16000,True), (6500,1000,False), (17000,20000,False)])
def test_oversize_schema_requires_full_lookup_and_survives_unsplit(make_executor, size, context, expected):
    schema = {'remainder': {'type': 'str', 'required': True, 'description': 'x' * size}}
    ex = make_executor([dict(phrase='large fixture contract', param_schema=schema)], count=0)
    request = ex.capability_request('large')
    initial = request.initial('large fixture contract')
    stub = initial['records'][0]
    assert stub['reason'] == 'full_descriptor_required' and stub['id'] not in request.offered
    full = request.lookup(cursor=stub['descriptor_cursor'], remaining_context_chars=context)
    if expected:
        assert full['records'][0]['argument_schema'] == schema
        assert stub['id'] in request.offered
    else:
        assert not full['records'] and full['reason'] == 'descriptor_too_large'
        assert stub['id'] not in request.offered


def test_every_page_honors_record_and_index_budgets(make_executor):
    request = make_executor(count=80).capability_request('budgets')
    result = request.initial()
    for _ in range(4):
        assert len(cap._json(result['records'])) <= cap.DESCRIPTOR_BUDGET
        assert len(result['records']) <= cap.PAGE_SIZE
        assert len(cap._json({k:v for k,v in result.items() if k != 'records'})) <= cap.INDEX_BUDGET
        if not result.get('next_cursor'):
            break
        result = request.lookup(cursor=result['next_cursor'])


@pytest.mark.parametrize('schema', [None, {}, {'x': None}, {'x': {'type': []}},
    {'x': {'type': 'semantic'}}, {'x': {'type': 'int', 'min': 10, 'max': 2}},
    {'x': {'type': 'float', 'max': float('inf')}}, {'x': {'type': 'str', 'choices': []}},
    {'x': {'type': 'int', 'choices': ['bad']}}, {'x': {'type': 'str', 'regex': '.*'}}])
def test_incomplete_schemas_cannot_offer_a_contract(schema):
    assert cap.schema_problem(schema)


def test_disallowed_and_unvalidated_records_cannot_bind_even_after_lookup(make_executor):
    ex = make_executor([dict(phrase='disabled fixture', pack='off'),
                        dict(phrase='unknown fixture', risk_class='unknown')])
    request = ex.capability_request('denied')
    initial = request.initial('disabled fixture')
    disabled = next(r for r in initial['records'] if r['canonical_phrase'] == 'disabled fixture')
    assert disabled['reason'] == 'pack_disabled'
    result = request.lookup(query='unknown fixture')
    unknown = next(r for r in result['records'] if r['canonical_phrase'] == 'unknown fixture')
    assert unknown['reason'] == 'unvalidated'
    for record in (disabled, unknown):
        with pytest.raises(cap.CapabilityError, match='unoffered'):
            request.bind(record['id'])


def test_lookup_validates_advertised_categories_query_and_cursor_conflicts(make_executor):
    request = make_executor().capability_request('invalid-lookup')
    first = request.initial()
    with pytest.raises(cap.CapabilityError, match='unadvertised_category'):
        request.lookup(category='invented')
    with pytest.raises(cap.CapabilityError, match='invalid_query'):
        request.lookup(query='x' * 201)
    with pytest.raises(cap.CapabilityError, match='cursor_query_conflict'):
        request.lookup(query='fixture', cursor=first['next_cursor'])


def test_category_metadata_is_paged_in_its_own_budget(make_executor):
    ex = make_executor([dict(phrase=f'category fixture {i}', pack=f'category-{i:03d}')
                        for i in range(90)], count=0)
    request = ex.capability_request('many-categories')
    first = request.initial()
    assert first['next_category_cursor']
    second = request.lookup(cursor=first['next_category_cursor'])
    assert {c['id'] for c in first['categories']}.isdisjoint(c['id'] for c in second['categories'])
    assert len(cap._json({k:v for k,v in first.items() if k != 'records'})) <= cap.INDEX_BUDGET
    assert len(cap._json(second)) <= cap.INDEX_BUDGET
    category = second['categories'][0]['id']
    found = request.lookup(category=category)
    assert found['records'] and all(r['category'] == category for r in found['records'])
    assert not request.offered or all(d.record()['available'] for d in request.offered.values())


def test_discovery_never_resolves_an_effect_or_creates_confirmation(make_executor, monkeypatch):
    from samsara import click_effect_policy
    ex = make_executor([dict(phrase='required fixture', param_schema={
        'remainder': {'type': 'str', 'required': True}})])
    monkeypatch.setattr(click_effect_policy, 'handling_for', Mock(side_effect=AssertionError('effect probe')))
    monkeypatch.setattr(ep, '_emit', Mock(side_effect=AssertionError('logged invocation')))
    description = ep.describe_capability('required fixture', executor=ex)
    assert description['schema']['remainder']['required']
    index = ex.capability_index()
    assert not index.error
    assert _by_phrase(index)['required fixture']['available']


def test_lookup_cursor_refuses_changed_index_and_cannot_be_replayed(make_executor):
    ex = make_executor()
    request = ex.capability_request('cursor-replay')
    first = request.initial()
    cursor = first['next_cursor']
    request.lookup(cursor=cursor)
    with pytest.raises(cap.CapabilityError, match='invalid_cursor'):
        request.lookup(cursor=cursor)
    ex._capability_generation += 1
    with pytest.raises(cap.CapabilityError, match='stale_index'):
        request.lookup(query='fixture')


def test_policy_risk_changes_invalidate_even_equivalent_low_risk_contract(make_executor):
    ex = make_executor()
    request = ex.capability_request('risk-change')
    record = request.initial('fixture operation 00')['records'][0]
    ex._plugin_registry[record['canonical_phrase']]['risk_class'] = 'read'
    with pytest.raises(cap.CapabilityError, match='stale_index'):
        request.bind(record['id'])


def test_inferred_documentation_arguments_never_create_callable_schema(make_executor, monkeypatch):
    ex = make_executor(count=0)
    original = command_catalog.capability_metadata
    def with_hint(*args, **kwargs):
        records = original(*args, **kwargs)
        for record in records:
            record['args'] = [{'name': 'invented', 'type': 'str', 'required': True}]
        return records
    monkeypatch.setattr(command_catalog, 'capability_metadata', with_hint)
    # A plugin with no schema, even read/UI, cannot acquire an inferred slot.
    ex = make_executor([dict(phrase='inferred fixture')], count=0)
    record = _by_phrase(ex.capability_index())['inferred fixture']
    assert not record['available'] and record['reason'] == 'inferred_arguments_only'
    assert record['argument_schema'] is None


def test_config_pack_change_cancels_offer_before_matcher_rebuild(make_executor):
    ex = make_executor()
    request = ex.capability_request('pack-config-change')
    record = request.initial('fixture home')['records'][0]
    ex._app.config['command_packs'] = {'core': False}
    with pytest.raises(cap.CapabilityError, match='stale_index'):
        request.bind(record['id'])


def test_registry_used_by_policy_must_match_supplied_executor(make_executor, monkeypatch):
    ex = make_executor()
    monkeypatch.setattr(plugin_commands, '_REGISTRY', {})
    record = _by_phrase(ex.capability_index())['fixture operation 00']
    assert not record['available'] and record['reason'] == 'policy_registry_mismatch'


def test_shadowed_and_not_yet_indexed_registrations_are_inventoried_without_authority(make_executor):
    ex = make_executor([dict(phrase='fixture home')], count=0)
    ex._plugin_registry['late fixture'] = dict(phrase='late fixture', func=_handler, source='fixtures.late')
    index = ex.capability_index()
    assert not index.error
    records = [r.record() for r in index.records]
    shadowed = next(r for r in records if r['reason'] == 'shadowed_registration')
    late = next(r for r in records if r['reason'] == 'unindexed_registration')
    assert shadowed['canonical_phrase'] == 'fixture home'
    assert late['canonical_phrase'] == 'late fixture'
    assert not shadowed['available'] and not late['available']
    assert shadowed['id'] not in index.executor_ids and late['id'] not in index.executor_ids
