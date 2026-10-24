"""617: opt-in facts, advisory bands, both setup routes and isolated real-turn seams."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from samsara import ai_preferences, cloud_llm, hardware_check as hw
from samsara.ui import ava_consent_qt as consent, ava_guide_qt as ag


def facts(vram=8, ram=16, name="NVIDIA test", cores=4, avx2=True):
    return hw.Hardware((hw.GPU(name, int(vram * hw.GIB)),), ram * hw.GIB, "Test CPU", cores, avx2)


@pytest.mark.parametrize("vram,ram,name,cores,avx2,route,model", [
    (8, 16, "NVIDIA", 4, True, "local", "qwen2.5:7b"),
    (24, 32, "NVIDIA", 8, True, "local", "qwen2.5:7b"),
    (7.99, 16, "NVIDIA", 4, True, "local", "qwen2.5:3b"),
    (4, 12, "NVIDIA", 4, True, "local", "qwen2.5:3b"),
    (8, 12, "NVIDIA", 4, True, "local", "qwen2.5:3b"),
    (3.99, 32, "NVIDIA", 4, True, "cloud", "qwen2.5:3b"),
    (24, 8, "NVIDIA", 8, True, "cloud", "qwen2.5:3b"),
    (8, 16, "NVIDIA", 2, True, "cloud", "qwen2.5:3b"),
    (8, 16, "NVIDIA", 4, False, "cloud", "qwen2.5:3b"),
    (0, 16, "Intel integrated", 4, True, "cloud", "qwen2.5:3b"),
    (16, 32, "AMD", 8, True, "cloud", "qwen2.5:3b"),
])
def test_advisory_bands(vram, ram, name, cores, avx2, route, model):
    verdict = hw.recommend(facts(vram, ram, name, cores, avx2))
    assert (verdict.route, verdict.model) == (route, model)
    assert verdict.reason


def test_check_requires_permission_before_any_reader(monkeypatch):
    reader = Mock(side_effect=AssertionError("read before opt-in"))
    monkeypatch.setattr(hw, "_read_windows", reader)
    for decision in (False, None, 1):
        with pytest.raises(PermissionError):
            hw.check_hardware(consent=decision)
    reader.assert_not_called()
    expected = facts()
    assert hw.check_hardware(consent=True, reader=lambda: expected) is expected


@pytest.mark.parametrize("value", [8 * hw.GIB, (12 * hw.GIB).to_bytes(8, "little")])
def test_vram_does_not_truncate_at_four_gib(value):
    assert hw.dedicated_bytes(value) > 4 * hw.GIB


@pytest.mark.parametrize("value", [b'\0' * 4, -1, None, True])
def test_invalid_memory_is_unknown(value):
    with pytest.raises(ValueError):
        hw.dedicated_bytes(value)


def test_registry_uses_active_display_and_only_qword_memory():
    calls = []
    path = r'SYSTEM\CurrentControlSet\Control\Video\active\0000'
    @contextmanager
    def open_key(_root, key):
        yield key
    def enum_value(_key, index):
        if index > 0:
            exc = OSError()
            exc.winerror = 259
            raise exc
        return r'\Device\Video0', '\\Registry\\Machine\\' + path, 1
    def value(_key, name):
        calls.append(name)
        return ("NVIDIA Test", 1) if name == "DriverDesc" else ((12 * hw.GIB).to_bytes(8, "little"), 3)
    registry = SimpleNamespace(HKEY_LOCAL_MACHINE=0, REG_QWORD=11, REG_BINARY=3,
                               OpenKey=open_key, EnumValue=enum_value, QueryValueEx=value)
    assert hw._registry_gpus(registry) == (hw.GPU("NVIDIA Test", 12 * hw.GIB),)
    assert calls == ["DriverDesc", "HardwareInformation.qwMemorySize"]


@pytest.mark.parametrize("metrics", [
    {"done": True, "eval_count": 48, "eval_duration": 2_000_000_000},
    {"done": False, "eval_count": 48, "eval_duration": 2_000_000_000},
    {"done": True, "eval_count": 0, "eval_duration": 0},
    {"done": True, "eval_count": 48, "eval_duration": float("nan")},
])
def test_speed_uses_chosen_model_and_real_metrics(metrics):
    response = Mock()
    response.json.return_value = metrics
    request = Mock(return_value=response)
    if metrics["done"] and metrics["eval_duration"] == 2_000_000_000:
        assert hw.measure_local({"ollama": {"model": "chosen"}}, request=request) == 24
    else:
        with pytest.raises(ValueError):
            hw.measure_local({"ollama": {"model": "chosen"}}, request=request)
    assert request.call_args.kwargs["json"]["model"] == "chosen"
    assert request.call_args.kwargs["timeout"] == 10
    assert "messages" not in request.call_args.kwargs["json"]
    response.close.assert_called_once()


def test_speed_transport_refuses_remote_host(monkeypatch):
    from samsara import ai_transport
    request = Mock(side_effect=AssertionError("HTTP boundary reached"))
    monkeypatch.setattr(ai_transport, "_request", request)
    with pytest.raises(ai_transport.AITransportRefused):
        hw.measure_local({"ollama": {"host": "http://192.0.2.1:11434", "model": "test"}})
    request.assert_not_called()


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', Mock(side_effect=AssertionError('Real HTTP forbidden')))
    app = SimpleNamespace(config={})
    app.update_config = lambda changes, save=True: app.config.update(deepcopy(changes))
    monkeypatch.setattr(ag.thread_registry, 'spawn', lambda _name, fn, **kw: fn())
    monkeypatch.setattr(ag, '_ping_ollama', lambda **kw: (False, []))
    monkeypatch.setattr(hw, '_read_windows', Mock(side_effect=AssertionError('Real hardware forbidden')))
    w = ag._WizardWindow(app)
    w.show()
    qapp.processEvents()
    yield w
    w.close()
    w.deleteLater()
    qapp.processEvents()


def setup_cloud(w):
    w._route = 'cloud'
    w._api_key.setText('fixture-only')
    w._accepted = ('cloud', w._provider.currentData())
    w._go_to(w._STEP_TEST)


def test_intro_starts_with_explanation_and_does_no_work(window, monkeypatch):
    probe = Mock()
    monkeypatch.setattr(hw, 'check_hardware', probe)
    assert window._current_step == window._STEP_INTRO
    assert window._next_btn.text() == 'Get started'
    window._go_next()
    assert window._current_step == window._STEP_CHECK
    probe.assert_not_called()
    assert window._app.config == {}


def test_permission_fills_same_editable_fields_without_navigation(window, monkeypatch):
    probe = Mock(return_value=facts(12, 32))
    monkeypatch.setattr(hw, 'check_hardware', probe)
    window._go_to(window._STEP_CHECK)
    window._check_btn.click()
    probe.assert_called_once_with(consent=True)
    assert window._current_step == window._STEP_CHECK
    assert window._vram.value() == 12 and window._ram.value() == 32
    window._ram.setValue(24)
    assert window._facts is None
    window._go_next()
    assert window._local_choice.isChecked()
    assert window._local_choice.isVisible() and window._cloud_choice.isVisible()
    assert window._app.config == {}


def test_failed_check_preserves_entries_and_allows_continue(window, monkeypatch):
    window._gpu.setText('Entered graphics card')
    monkeypatch.setattr(hw, 'check_hardware', Mock(side_effect=OSError('private failure')))
    window._go_to(window._STEP_CHECK)
    window._check_pc()
    assert window._gpu.text() == 'Entered graphics card'
    assert window._hardware_status.text() == ag.COPY['check_failed']
    assert window._next_btn.isEnabled()
    assert 'private' not in window._hardware_status.text()


def test_blank_details_continue_without_invented_verdict(window):
    window._go_to(window._STEP_CHECK)
    window._go_next()
    assert window._hardware_verdict.text() == ag.COPY['unknown']
    assert window._current_step == window._STEP_CHOOSE


@pytest.mark.parametrize('route', ['cloud','local'])
def test_one_flow_branches_only_at_setup(window, route):
    window._go_to(window._STEP_CHOOSE)
    (window._cloud_choice if route=='cloud' else window._local_choice).click()
    window._go_next()
    assert window._current_step == (window._STEP_CLOUD if route=='cloud' else window._STEP_OLLAMA)
    assert window._app.config == {}


def test_local_next_requires_ollama_and_selected_installed_model(window):
    window._go_to(window._STEP_OLLAMA)
    assert not window._next_btn.isEnabled()
    window._on_ollama_status(True, [])
    assert not window._next_btn.isEnabled()
    window._on_ollama_status(True, [window._selected_model_name])
    assert window._next_btn.isEnabled()
    window._go_next()
    assert window._current_step == window._STEP_CONSENT
    assert not window._next_btn.isEnabled()


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
def test_cloud_turn_uses_chosen_provider_without_speech_or_memory(window, monkeypatch, provider):
    window._provider.setCurrentIndex(window._provider.findData(provider))
    setup_cloud(window)
    send = Mock(return_value='ready')
    monkeypatch.setattr(cloud_llm, 'send', send)
    window._test_route()
    assert send.call_count == 1
    assert send.call_args.args[:2] == ('Reply only with the word ready.', 'Setup test.')
    assert send.call_args.args[2].config['cloud_llm']['provider'] == provider
    assert send.call_args.kwargs == {'timeout':10}
    assert window._next_btn.isEnabled()
    assert window._app.config == {}
    window._go_next()
    assert window._current_step == window._STEP_DONE


def test_key_check_is_not_a_real_turn_or_ready(window, monkeypatch):
    window._provider.setCurrentIndex(3)
    setup_cloud(window)
    window._go_to(window._STEP_CLOUD)
    checker = Mock(return_value=SimpleNamespace(failure=None))
    monkeypatch.setattr(cloud_llm, 'check_openrouter', checker)
    monkeypatch.setattr(ag.openrouter_catalog, 'load_catalogue',
                        Mock(return_value=ag.openrouter_catalog.Catalogue(
                            ag._load_models('openrouter').models)))
    window._check_key()
    checker.assert_called_once()
    assert window._tested is None
    assert window._cloud_status.text() == ag.COPY['key_good']


def test_consent_required_before_any_key_or_turn_request(window, monkeypatch):
    send = Mock()
    monkeypatch.setattr(cloud_llm, 'send', send)
    window._route = 'cloud'
    window._api_key.setText('fixture-only')
    window._test_route()
    send.assert_not_called()
    assert window._current_step == window._STEP_CONSENT
    assert not window._next_btn.isEnabled()


@pytest.mark.parametrize('kind,copy_key', [('bad_key','bad_key'),('out_of_credit','no_credit'),('unreachable','network'),('model_unavailable','model_unavailable'),('rate_limited','rate_limited'),('blocked','blocked')])
def test_typed_cloud_failures_keep_next_blocked(window, monkeypatch, kind, copy_key):
    setup_cloud(window)
    monkeypatch.setattr(cloud_llm,'send',lambda *a,**kw:cloud_llm.CloudError(cloud_llm.make_failure(kind,'deepseek')))
    window._test_route()
    assert window._test_status.text() == ag.COPY[copy_key]
    assert not window._next_btn.isEnabled()
    assert window._tested is None


def test_local_speed_uses_existing_bounded_measurement(window, monkeypatch):
    window._accepted = ('local',window._provider.currentData())
    window._go_to(window._STEP_TEST)
    measure = Mock(return_value=12.5)
    monkeypatch.setattr(hw, 'measure_local', measure)
    window._test_route()
    assert measure.call_args.args[0]['ollama']['model'] == window._selected_model_name
    assert window._next_btn.isEnabled()
    assert '12.5' in window._test_status.text()


def test_closing_discards_pending_success_and_hides(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(ag.thread_registry,'spawn',lambda *a,**kw:None)
    window._test_route()
    token=window._generation
    window.close()
    window._receive(token,'test',True)
    assert not window.isVisible()
    assert window._tested is None
    assert window._app.config == {}


def test_editing_key_invalidates_pass_and_switching_provider_clears_key(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(cloud_llm,'send',lambda *a,**kw:'ready')
    window._test_route()
    assert window._tested
    window._api_key.setText('replacement-fixture')
    assert window._tested is None
    window._provider.setCurrentIndex(1)
    assert not window._api_key.text()
    assert window._accepted is None


def test_changed_app_settings_reject_worker_result(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(ag.thread_registry,'spawn',lambda *a,**kw:None)
    window._test_route()
    window._app.config['outside_change']=True
    window._receive(window._generation,'test',True)
    assert window._tested is None
    assert window._test_status.text() == ag.COPY['changed']


def test_only_use_ava_persists_tested_selection(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(cloud_llm,'send',lambda *a,**kw:'ready')
    window._test_route()
    window._go_next()
    assert window._app.config == {}
    window._go_next()
    assert window._app.config['ava']['provider_policy'] == 'cloud'
    assert window._app.config['cloud_llm']['model'] == window._cloud_model.text()
    assert not window.isVisible()


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
def test_bundled_models_load_offline_with_recommended_default(window, monkeypatch, provider):
    get = Mock(side_effect=AssertionError('offline browsing must not use HTTP'))
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    monkeypatch.setattr(ag.openrouter_catalog, 'load_catalogue',
                        Mock(return_value=ag.openrouter_catalog.Catalogue(reason='offline')))
    window._provider.setCurrentIndex(window._provider.findData(provider))
    window._go_to(window._STEP_CLOUD)
    snapshot = ag.ai_models.SNAPSHOT[provider]
    assert len(snapshot['models']) > 5
    assert snapshot['default'] in {row['id'] for row in snapshot['models']}
    assert window._cloud_models.currentData() == snapshot['default']
    # 816: long model lists expose contains-match type filtering.
    assert window._cloud_models.isEditable() == (len(snapshot['models']) > 30)
    assert not window._cloud_model.isVisible()
    assert window._model_id.text() == snapshot['default']
    assert window._cloud_models.itemText(window._cloud_models.count() - 1) == 'Custom model id'
    window._open_picker()
    from samsara.ui.settings.ava_cloud_qt import _OpenRouterPicker
    assert isinstance(window._catalogue, _OpenRouterPicker)
    assert window._catalogue.models.count() == len(snapshot['models']) + 1
    assert not window._catalogue.free.isHidden()
    assert not window._catalogue.capabilities.isHidden()
    get.assert_not_called()


def catalogue_response(rows, **extra):
    import json
    payload = {'data': rows, **extra}
    return Mock(content=json.dumps(payload).encode(), json=lambda: payload)


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
def test_refresh_merges_caches_and_reloads_without_key(window, monkeypatch, provider):
    default = ag.ai_models.SNAPSHOT[provider]['default']
    # 816: OpenRouter's public endpoint provides `name`, not `display_name`.
    response_rows = [{'id': default}, {'id': 'new-model', 'display_name': 'A friendly name',
        'context_length': 64000, 'supported_parameters': ['tools', 'response_format'],
        'pricing': {'prompt': '0', 'completion': '0'}, 'private_response_field': 'must be discarded'}]
    if provider == 'openrouter':
        response_rows[1]['name'] = response_rows[1].pop('display_name')
    response = catalogue_response(response_rows)
    get = Mock(return_value=response)
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    result = ag._load_models(provider, refresh=True, api_key='fixture-only')
    assert not result.reason
    by_id = {m['id']: m for m in result.models}
    bundled = {row['id']: row for row in ag.ai_models.SNAPSHOT[provider]['models']}
    assert by_id[default]['name'] == bundled[default]['display_name']
    assert by_id['new-model']['name'] == 'A friendly name'
    assert by_id['new-model']['context_length'] == 64000
    if provider == 'openrouter':
        # 816: OpenRouter shares Settings' public, keyless catalogue endpoint.
        assert get.call_args.args == ('https://openrouter.ai/api/v1/models',)
        assert 'headers' not in get.call_args.kwargs
    else:
        assert get.call_args.args == (cloud_llm.BUILTIN_PROVIDERS[provider]['base_url'] + '/models',)
        headers = get.call_args.kwargs['headers']
        assert headers == ({'x-api-key': 'fixture-only', 'anthropic-version': '2023-06-01'} if provider == 'anthropic'
                           else {'Authorization': 'Bearer fixture-only'})
    response.close.assert_called_once()
    raw = ag._model_cache_path(provider).read_text()
    assert 'fixture-only' not in raw and 'private_response_field' not in raw
    assert ag._load_models(provider).models == result.models
    assert get.call_count == 1
    other = 'openai' if provider != 'openai' else 'deepseek'
    assert 'new-model' not in {m['id'] for m in ag._load_models(other).models}


def test_anthropic_refresh_follows_bounded_pagination(window, monkeypatch):
    get = Mock(side_effect=[catalogue_response([{'id': 'first', 'display_name': 'First'}], has_more=True, last_id='first'),
                            catalogue_response([{'id': 'second', 'display_name': 'Second'}], has_more=False)])
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    result = ag._load_models('anthropic', refresh=True, api_key='fixture-only')
    assert {'first', 'second'} <= {m['id'] for m in result.models}
    assert get.call_args.args[0].endswith('/models?after_id=first&limit=100')


@pytest.mark.parametrize('failure', ['network', 'malformed', 'empty', 'disk'])
def test_refresh_failure_preserves_usable_models(window, monkeypatch, failure):
    get = Mock(return_value=catalogue_response([{'id': 'new-model'}]))
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    first = ag._load_models('openai', refresh=True, api_key='fixture-only')
    old_cache = ag._model_cache_path('openai').read_bytes()
    if failure == 'network':
        get.side_effect = OSError('private failure text')
    elif failure == 'malformed':
        get.return_value.json = Mock(side_effect=ValueError('private failure text'))
    elif failure == 'empty':
        get.return_value = catalogue_response([])
    else:
        monkeypatch.setattr(ag.openrouter_catalog, '_write_cache', Mock(side_effect=OSError('disk')))
    result = ag._load_models('openai', refresh=True, api_key='fixture-only')
    assert result.reason == ('cache' if failure == 'disk' else 'offline' if failure == 'network' else 'invalid')
    assert result.models == first.models
    assert ag._model_cache_path('openai').read_bytes() == old_cache


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
def test_key_success_refreshes_only_after_consent_and_keeps_selection(window, monkeypatch, provider):
    window._provider.setCurrentIndex(window._provider.findData(provider))
    window._route = 'cloud'
    window._api_key.setText('fixture-only')
    checker = Mock(return_value=(True, provider))
    monkeypatch.setattr(cloud_llm, 'check_available', checker)
    router = Mock(return_value=SimpleNamespace(failure=None))
    monkeypatch.setattr(cloud_llm, 'check_openrouter', router)
    response = catalogue_response([{'id': 'new-model', 'name': 'New Model'}])
    get = Mock(return_value=response)
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    window._check_key()
    get.assert_not_called()
    checker.assert_not_called()
    router.assert_not_called()
    window._accepted = ('cloud', provider)
    selected = window._cloud_model.text()
    window._go_to(window._STEP_CLOUD)
    window._check_key()
    assert get.call_count == 1
    assert window._cloud_models.findData('new-model') >= 0
    assert window._cloud_model.text() == selected
    assert window._tested is None


def test_bad_key_does_not_refresh(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(cloud_llm, 'check_available', lambda _app: (False, 'The key was rejected.'))
    get = Mock()
    monkeypatch.setattr(ag.ai_transport, 'get_cloud', get)
    window._check_key()
    get.assert_not_called()
    assert window._cloud_status.text() == ag.COPY['bad_key']


def test_custom_id_saves_and_picker_can_return_to_named_model(window, monkeypatch):
    setup_cloud(window)
    window._go_to(window._STEP_CLOUD)
    window._cloud_models.setCurrentIndex(window._cloud_models.count() - 1)
    window._cloud_models.activated.emit(window._cloud_models.currentIndex())
    assert window._cloud_model.isVisible()
    assert not window._next_btn.isEnabled()
    window._cloud_model.setText('my-custom-model')
    assert window._next_btn.isEnabled()
    window._open_picker()
    window._catalogue.models.setCurrentRow(0)
    window._catalogue.choose.click()
    assert not window._cloud_model.isVisible()
    assert window._cloud_models.currentData() == ag.ai_models.SNAPSHOT['deepseek']['default']
    window._cloud_models.setCurrentIndex(window._cloud_models.count() - 1)
    window._cloud_models.activated.emit(window._cloud_models.currentIndex())
    window._cloud_model.setText('my-custom-model')
    monkeypatch.setattr(cloud_llm, 'send', lambda *a, **kw: 'ready')
    window._go_to(window._STEP_TEST)
    window._test_route()
    window._go_next()
    window._go_next()
    assert window._app.config['cloud_llm']['model'] == 'my-custom-model'


def test_shared_picker_search_filters_and_custom_last(window):
    rows = ag.openrouter_catalog.parse_models({'data': [
        {'id': 'paid', 'name': 'Paid model', 'pricing': {'prompt': '1', 'completion': '1'}},
        {'id': 'free', 'name': 'Friendly free model', 'pricing': {'prompt': '0', 'completion': '0'},
         'supported_parameters': ['tools', 'response_format']}]})
    window._go_to(window._STEP_CLOUD)
    window._open_picker()
    picker = window._catalogue
    picker.receive(ag.openrouter_catalog.Catalogue(rows))
    assert not picker.free.isHidden() and not picker.capabilities.isHidden()
    picker.free.setChecked(True)
    picker.capabilities.setChecked(True)
    picker.search.setText('FRIENDLY')
    assert picker.models.count() == 2
    assert picker.models.item(0).text().startswith('Friendly free model\nfree')
    assert picker.models.item(1).text() == 'Custom model id'
    picker.search.setText('no match')
    assert picker.models.count() == 1


def test_stale_key_refresh_cannot_replace_new_provider(window, monkeypatch):
    setup_cloud(window)
    monkeypatch.setattr(ag.thread_registry, 'spawn', lambda *a, **kw: None)
    window._check_key()
    token = window._generation
    window._provider.setCurrentIndex(1)
    result = ag.openrouter_catalog.Catalogue(ag.openrouter_catalog.parse_models({'data': [{'id': 'stale-model'}]}))
    window._receive(token, 'key', result)
    assert window._cloud_models.findData('stale-model') == -1


def generator_row(model_id, *, created=1, price='0.000001', parameters=None, output=None):
    return {'id': model_id, 'name': 'Friendly ' + model_id, 'created': created,
            'pricing': {'prompt': price, 'completion': price}, 'context_length': 64000,
            'supported_parameters': ['tools', 'response_format'] if parameters is None else parameters,
            'architecture': {'output_modalities': ['text'] if output is None else output}}


@pytest.mark.parametrize('provider,source,expected', [
    ('openai', 'openai/gpt-6-luna', 'gpt-6-luna'),
    ('deepseek', 'deepseek/deepseek-v4.1-flash', 'deepseek-v4.1-flash'),
    ('anthropic', 'anthropic/claude-sonnet-5.5', 'claude-sonnet-5-5'),
])
def test_generator_native_id_rules(provider, source, expected):
    from tools.gen_ai_models import native_id
    assert native_id(provider, source) == expected
    with pytest.raises(ValueError):
        native_id(provider, 'unrelated/model')


@pytest.mark.parametrize('provider,stable,variant', [
    ('openai', 'gpt-4o', 'gpt-4o-2024-11-20'),
    ('openai', 'gpt-4-turbo', 'gpt-4-turbo-preview'),
    ('anthropic', 'claude-sonnet-5.5', 'claude-sonnet-5.5-20261001'),
    ('deepseek', 'deepseek-v4.1-flash', 'deepseek-v4.1-flash-1017'),
    ('deepseek', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp'),
])
def test_generator_prefers_stable_but_preserves_only_available_variant(provider, stable, variant):
    from tools.gen_ai_models import build_snapshot, native_id
    old = generator_row(provider + '/' + stable)
    new = generator_row(provider + '/' + variant, created=2)
    both = build_snapshot({'data': [new, old]})[provider]['models']
    assert [row['id'] for row in both] == [native_id(provider, old['id'])]
    only = build_snapshot({'data': [new]})[provider]['models']
    assert [row['id'] for row in only] == [native_id(provider, new['id'])]


def test_generator_openrouter_complete_deduplicated_and_metadata_exact():
    from tools.gen_ai_models import build_snapshot
    source = [generator_row('vendor/paid', created=1),
              generator_row('vendor/paid:batch'), generator_row('vendor/paid:free'),
              generator_row('vendor/zero', created=3, price='0'),
              generator_row('vendor/unknown', created=2, price=None, parameters=[]),
              generator_row('vendor/image', created=4, output=['image'])]
    result = build_snapshot({'data': source})['openrouter']['models']
    # 816: image output is excluded, free suffixes stay, and names sort alphabetically.
    assert [r['id'] for r in result] == ['vendor/paid', 'vendor/paid:free', 'vendor/unknown',
                                         'vendor/zero', 'openrouter/auto']
    by_id = {r['id']: r for r in result}
    assert by_id['vendor/zero']['free'] is True
    assert by_id['vendor/paid']['free'] is False
    assert by_id['vendor/unknown']['free'] is None
    assert by_id['vendor/zero']['tools'] and by_id['vendor/zero']['json']
    assert not by_id['vendor/unknown']['tools'] and not by_id['vendor/unknown']['json']
    assert by_id['vendor/zero']['context'] == 64000
    assert by_id['openrouter/auto']['display_name'] == 'Auto (OpenRouter chooses)'


def test_generator_direct_providers_exclude_non_chat_and_keep_multiple_models():
    from tools.gen_ai_models import build_snapshot
    rows = [generator_row('openai/gpt-6-luna'), generator_row('openai/gpt-6-sol'),
            generator_row('openai/gpt-image', output=['image', 'text']),
            generator_row('openai/gpt-audio', output=['audio', 'text']),
            generator_row('openai/gpt-3.5-turbo-instruct'),
            generator_row('openai/text-embedding'),
            generator_row('other/chat')]
    assert {r['id'] for r in build_snapshot({'data': rows})['openai']['models']} == {'gpt-6-luna', 'gpt-6-sol'}


def test_generator_reproducible_cli_without_external_input(tmp_path, monkeypatch):
    import hashlib
    import json
    from tools.gen_ai_models import DEFAULTS, main, render_module
    rows = [generator_row(provider + '/' + model_id.replace('haiku-5-5', 'haiku-5.5'))
            for provider, model_id in DEFAULTS.items() if provider != 'openrouter']
    payload = {'data': rows}
    source = tmp_path / 'catalogue.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    output = tmp_path / 'generated.py'
    # No subprocess, Git, network or absolute owner/report paths in this test.
    main([str(source), '--output', str(output)])
    expected = render_module(payload, source.name, hashlib.sha256(source.read_bytes()).hexdigest())
    assert output.read_text(encoding='utf-8') == expected
    assert render_module({'data': list(reversed(rows))}, source.name, 'same-hash') == render_module(payload, source.name, 'same-hash')
    with pytest.raises(ValueError, match='recommended tool-capable model missing'):
        render_module({'data': []}, 'empty.json', 'hash')


def test_generated_snapshot_coverage_defaults_and_order():
    from tools.gen_ai_models import sort_key
    # 816: the regenerated source keeps text free variants and drops non-chat rows.
    expected_counts = {'openrouter': 357, 'openai': 53, 'anthropic': 15, 'deepseek': 9}
    for provider, entry in ag.ai_models.SNAPSHOT.items():
        rows = entry['models']
        assert len(rows) == expected_counts[provider]
        assert len({r['id'] for r in rows}) == len(rows)
        # 816: only OpenRouter's :free IDs remain; the rest sort by display name.
        assert not any(':' in r['id'] and not r['id'].endswith(':free') for r in rows)
        alphabetical = [r for r in rows if r['id'] not in {'openrouter/auto', entry['default']}]
        assert alphabetical == sorted(alphabetical, key=sort_key)
        assert rows[0]['id'] == entry['default']
        default = next(r for r in rows if r['id'] == entry['default'])
        assert default['tools'] and default['json']
        assert default['created'] >= 1788220800  # September 2026 or later in supplied snapshot.
        if provider != 'openrouter':
            assert all('/' not in r['id'] for r in rows)
    assert ag.ai_models.SNAPSHOT['openai']['default'] != 'gpt-4o-mini'


@pytest.mark.parametrize('provider', list(cloud_llm.BUILTIN_PROVIDERS))
def test_expanded_picker_search_filters_and_scroll(window, provider):
    from decimal import Decimal
    window._provider.setCurrentIndex(window._provider.findData(provider))
    window._go_to(window._STEP_CLOUD)
    window._open_picker()
    picker = window._catalogue
    default = ag.ai_models.SNAPSHOT[provider]['default']
    picker.search.setText(default)
    assert any(picker.models.item(i).data(ag.Qt.ItemDataRole.UserRole) == default
               for i in range(picker.models.count()))
    picker.search.clear()
    picker.free.setChecked(True)
    expected = sum(ag.openrouter_catalog.matches(m, free_only=True) for m in picker.catalogue.models)
    assert picker.models.count() == expected + 1  # Custom is always available.
    picker.free.setChecked(False)
    picker.capabilities.setChecked(True)
    expected = sum('tools' in m['supported_parameters'] and
                   bool({'response_format', 'structured_outputs'} & set(m['supported_parameters']))
                   for m in picker.catalogue.models)
    assert picker.models.count() == expected + 1
    picker.capabilities.setChecked(False)
    picker.models.scrollToBottom()
    assert picker.models.verticalScrollBar().maximum() > 0
    assert picker.models.item(picker.models.count() - 1).text() == 'Custom model id'


def test_cloud_consent_says_samsara_is_free_without_charge_caveat():
    assert ag.COPY['consent_cloud'].endswith('Samsara is free.')
    assert 'charge' not in ag.COPY['consent_cloud']
