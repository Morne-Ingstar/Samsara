"""Cloud acceptance is durable before Apply; transports are always fake."""
import json
import threading
from types import SimpleNamespace
from copy import deepcopy
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog, QPushButton

from plugins.commands import ask_ollama
from samsara import ai_preferences as ai, ava_readiness, cloud_llm, ava_turn_queue, execution_policy
from samsara.ui import ava_consent_qt as consent, settings_qt
from tests.test_ava_route_master_343 import _config
from tests.test_settings_pages import _StubApp, _schema_defaults


@pytest.fixture
def harness(qapp, monkeypatch, tmp_path):
    from samsara import autostart, listening_display
    from samsara.ui import qt_runtime
    from samsara.ui.first_run_qt import ComponentsPage

    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda self: None)
    monkeypatch.setattr(ava_readiness, 'tracker', ava_readiness.ReadinessTracker())
    monkeypatch.setattr(ava_readiness, '_monitor', None)
    # Keep real readiness/cancellation code; schedule workers deterministically.
    from samsara.runtime import thread_registry
    monkeypatch.setattr(thread_registry, 'spawn', lambda *a, **k: None)
    monkeypatch.setattr(listening_display, 'run_display', lambda *a: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a: None)
    monkeypatch.setattr(qt_runtime, 'post', lambda *a: None)
    monkeypatch.setattr(settings_qt._SettingsWindow, '_replace_tts_runtime', lambda *a: None)
    monkeypatch.setattr(ask_ollama, '_check_ollama_available', lambda *a, **k: True)
    monkeypatch.setattr(ask_ollama.ava_profile, 'build_context_section', lambda *a: '')
    monkeypatch.setattr(ask_ollama.ava_corrections, 'build_context_section', lambda **k: '')
    windows, dialogs, sent = [], [], []

    def accept(dialog):
        assert {button.text() for button in dialog.findChildren(QPushButton)} == {
            'Not now', 'I understand, turn Ava on'}
        dialogs.append(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(consent.AvaConsentDialog, 'exec', accept)

    class Response:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {'choices': [{'message': {'content': 'Sunday.'}}]}

    def post(*args, **kwargs):
        sent.append(True)
        return Response()

    monkeypatch.setattr(cloud_llm.ai_transport, 'post_cloud', post)
    monkeypatch.setattr(ask_ollama.ai_transport, 'post_local',
                        lambda *a, **k: pytest.fail('unexpected local transport'))

    def make(config=None, app=None):
        if app is None:
            app = _StubApp(deepcopy(config if config is not None else _config()))
            path = tmp_path / ('config-%d.json' % len(windows))
            app.save_config = Mock(side_effect=lambda: path.write_text(
                json.dumps(app.config), encoding='utf-8'))
            app.state_file = path
        window = settings_qt._SettingsWindow(app)
        windows.append(window)
        return window

    yield make, dialogs, sent
    for window in windows:
        window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def local_accepted():
    config = _config()
    config['ava']['consent'] = consent.accepted_consent(config)
    return config


def ask(app):
    return ask_ollama.ask_model('What day is it?', app)


@pytest.mark.parametrize('finish', ['apply', 'close'])
def test_owners_ollama_then_cloud_sequence_answers_before_apply(harness, finish):
    make, dialogs, sent = harness
    cfg = local_accepted()
    cfg.setdefault('command_packs', {})['ai'] = False
    window = make(cfg)
    window.app.command_executor.rebuild_matcher = Mock()
    # The real dialog runs during the same selection handler as Settings.
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert len(dialogs) == 1 and sent == []
    window.app.command_executor.rebuild_matcher.assert_called_once_with()
    assert ai.runtime_state(window.app.config, 'ava').allowed
    assert ask(window.app).ok and sent == [True]
    accepted = deepcopy(window.app.config['ava']['consent'])
    if finish == 'apply':
        window._apply_and_close()
    else:
        window.close()
    assert window.app.config['ava']['consent'] == accepted
    persisted = json.loads(window.app.state_file.read_text(encoding='utf-8'))
    assert ai.runtime_state(persisted, 'ava').allowed
    if finish == 'close':
        assert persisted['ava']['provider_policy'] == 'local'
    assert not consent.consent_required(persisted, cloud_enabled=True)
    assert 'pending' not in ai.ava_route_status(window.app.config)
    assert window._widgets['ava_consent_button'].text() == 'Read this again'


def test_fresh_cloud_first_answers_without_apply(harness):
    make, dialogs, sent = harness
    window = make(_schema_defaults())
    window._widgets['cloud_api_key'].setText('synthetic')
    assert not ask(window.app).ok and sent == []
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert len(dialogs) == 1
    assert not ask(window.app).ok and sent == []  # Key is still unapplied.
    window._apply_and_close()
    assert ask(window.app).ok and sent == [True]


@pytest.mark.parametrize('finish', ['apply', 'close'])
def test_pending_replay_offers_accept_and_saves_immediately(harness, finish):
    make, dialogs, sent = harness
    config = _config()
    config['ava']['provider_policy'] = 'cloud'
    config['cloud_llm']['enabled'] = True
    window = make(config)
    assert 'consent pending' in ai.ava_route_status(window.app.config)
    assert not ask(window.app).ok and sent == []
    button = window._widgets['ava_consent_button']
    assert button.text() == 'Give your OK'
    button.click()  # Same action as the Read this again row.
    assert len(dialogs) == 1 and ask(window.app).ok and sent == [True]
    window._apply_and_close() if finish == 'apply' else window.close()
    assert ai.runtime_state(window.app.config, 'ava').allowed


def test_dialog_accept_with_another_settings_window_open_survives_apply(harness):
    make, _, sent = harness
    stale = make(local_accepted())
    accepting = make(app=stale.app)
    accepting._widgets['providerRoute'].setCurrentIndex(2)
    record = deepcopy(stale.app.config['ava']['consent'])
    stale._widgets['ava_personality'].setCurrentText('Strict')
    stale._apply_and_close()
    assert stale.app.config['ava']['consent'] == record
    assert stale.app.config['ava']['provider_policy'] == 'cloud'
    assert ask(stale.app).ok and sent == [True]


def test_grants_remain_independent_and_survive_route_round_trip(harness):
    make, dialogs, _ = harness
    window = make(local_accepted())
    local = deepcopy(window.app.config['ava']['consent'])
    assert not ai.consent_covers(local, features=('ava',),
                                provider=ai.provider_for(window.app.config, 'cloud'), cloud=True)
    window._widgets['providerRoute'].setCurrentIndex(2)
    for policy, index in [('local', 1), ('cloud', 2), ('local', 1), ('cloud', 2)]:
        window._widgets['providerRoute'].setCurrentIndex(index)
        assert window.app.config['ava']['provider_policy'] == policy
        assert ai.runtime_state(window.app.config, 'ava').allowed
        assert not ai.runtime_state(window.app.config, 'editing').allowed
    assert len(dialogs) == 1


def test_provider_round_trip_preserves_grants_but_new_provider_needs_accept(harness):
    make, dialogs, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    provider = window._widgets['cloud_provider']
    provider.setCurrentText('OpenAI')
    assert len(dialogs) == 2 and window.app.config['cloud_llm']['provider'] == 'openai'
    provider.setCurrentText('DeepSeek (default)')
    assert len(dialogs) == 2 and window.app.config['cloud_llm']['provider'] == 'deepseek'
    assert ai.runtime_state(window.app.config, 'ava').allowed


def test_declined_prompt_is_reachable_next_time_and_never_sends(harness, monkeypatch):
    make, dialogs, sent = harness
    window = make(local_accepted())
    real_exec = consent.AvaConsentDialog.exec
    monkeypatch.setattr(consent.AvaConsentDialog, 'exec', lambda self: QDialog.DialogCode.Rejected)
    original = deepcopy(window.app.config)
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert window.app.config == original and sent == []
    monkeypatch.setattr(consent.AvaConsentDialog, 'exec', real_exec)
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert len(dialogs) == 1 and ask(window.app).ok and sent == [True]


def test_turn_off_blocks_cloud_at_once_and_preserves_previous_acceptance(harness):
    make, dialogs, sent = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    window._widgets['providerRoute'].setCurrentIndex(0)
    assert not ask(window.app).ok and sent == []
    window._apply_and_close()
    assert not ai.runtime_state(window.app.config, 'ava').allowed
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert len(dialogs) == 1 and ask(window.app).ok and sent == [True]


def test_clearing_consent_revokes_all_remembered_destinations(harness):
    make, _, sent = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    window.app.config['ava']['consent'] = {}
    window._apply_and_close()
    assert not ask(window.app).ok and sent == []
    assert consent.consent_required(window.app.config)
    assert consent.consent_required(window.app.config, cloud_enabled=True)


def test_warmup_memory_network_voice_and_clock_do_not_replace_cloud_consent(harness):
    make, _, sent = harness
    config = local_accepted()
    config['ava'].update(provider_policy='cloud', warm_on_boot=True)
    config['cloud_llm']['enabled'] = True
    config['ava']['consent']['memory_cloud_accepted'] = True
    config['tts']['network_voice_accepted'] = True
    window = make(config)
    assert not ask(window.app).ok and sent == []
    window._widgets['ava_consent_button'].click()
    assert ask(window.app).ok and sent == [True]


def test_archived_grants_still_require_feature_version_and_exact_origin():
    config = local_accepted()
    cloud = consent.accepted_consent(config, cloud_enabled=True)
    provider = ai.provider_for(config, 'local')
    assert ai.consent_covers(cloud, features=('ava',), provider=provider, cloud=False)
    assert not ai.consent_covers(cloud, features=('editing',), provider=provider, cloud=False)
    assert not ai.consent_covers(cloud, features=('ava',), provider=provider, cloud=False, version=3)
    changed = ai.identify_provider('ollama', 'http://127.0.0.1:11435')
    assert not ai.consent_covers(cloud, features=('ava',), provider=changed, cloud=False)
    cloud['previous_grants'][0]['accepted_at'] = None
    assert not ai.consent_covers(cloud, features=('ava',), provider=provider, cloud=False)

# FINISH IT 2: use real Settings, readiness and cancellation boundaries.
@pytest.mark.parametrize('finish', ['apply', 'cancel'])
def test_stale_window_preserves_outside_model_and_provider_fields(harness, finish):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['ava_command_session']['model'] = 'synthetic-A'
    cfg['cloud_llm']['model'] = 'synthetic-cloud-A'
    window = make(cfg)  # The outside change occurs after the snapshot.
    window.app.config['ava_command_session']['model'] = 'synthetic-B'
    window.app.config['cloud_llm']['model'] = 'synthetic-cloud-B'
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert window.app.config['ava_command_session']['model'] == 'synthetic-B'
    assert window.app.config['cloud_llm']['model'] == 'synthetic-cloud-B'
    window._apply_and_close() if finish == 'apply' else window.close()
    assert window.app.config['ava_command_session']['model'] == 'synthetic-B'
    assert window.app.config['cloud_llm']['model'] == 'synthetic-cloud-B'


def test_intentional_model_edit_wins_on_apply_but_key_waits_for_apply(harness):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['ava_command_session']['model'] = 'synthetic-A'
    window = make(cfg)
    window.app.config['ava_command_session']['model'] = 'synthetic-B'
    window._widgets['ava_cmd_model'].setText('synthetic-C')
    window._widgets['cloud_api_key'].setText('synthetic-unapplied')
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert window.app.config['cloud_llm']['api_key'] == cfg['cloud_llm']['api_key']
    assert window.app.config['ava_command_session']['model'] == 'synthetic-B'
    window._apply_and_close()
    assert window.app.config['cloud_llm']['api_key'] == 'synthetic-unapplied'
    assert window.app.config['ava_command_session']['model'] == 'synthetic-C'


def test_cancel_discards_route_provider_and_key_but_retains_exact_consents(harness):
    make, _, _ = harness
    cfg = local_accepted()
    window = make(cfg)
    window._widgets['providerRoute'].setCurrentIndex(2)
    window._widgets['cloud_provider'].setCurrentText('OpenAI')
    window._widgets['cloud_api_key'].setText('synthetic-unapplied')
    window.close()
    assert window.app.config['ava']['provider_policy'] == 'local'
    assert window.app.config['cloud_llm']['provider'] == 'deepseek'
    assert window.app.config['cloud_llm']['api_key'] == cfg['cloud_llm']['api_key']
    for provider in ('deepseek', 'openai'):
        destination = ai.provider_for({'cloud_llm': {'provider': provider}}, 'cloud')
        assert ai.consent_covers(window.app.config['ava']['consent'],
                                 features=('ava',), provider=destination, cloud=True)


def test_cancel_preserves_outside_edits_after_route_choice(harness):
    make, _, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    window.app.config['cloud_llm']['model'] = 'synthetic-outside'
    window._widgets['providerRoute'].setCurrentIndex(0)
    window.app.config['ava']['warm_on_boot'] = True
    window.close()
    assert window.app.config['cloud_llm']['model'] == 'synthetic-outside'
    assert window.app.config['ava']['warm_on_boot'] is True


def test_two_dialog_accepts_keep_exact_destinations_purposes_and_versions(harness, monkeypatch):
    make, _, _ = harness
    window = make(local_accepted())
    def accept_first(dialog):
        other = deepcopy(window.app.config)
        other['cloud_llm']['provider'] = 'openai'
        second = ai.accept_consent(other, provider=ai.provider_for(other, 'cloud'),
                                   features=('editing',), cloud=True)
        window.app.update_config({'ava': {**window.app.config['ava'], 'consent': second}}, save=True)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(consent.AvaConsentDialog, 'exec', accept_first)
    window._widgets['providerRoute'].setCurrentIndex(2)
    record = window.app.config['ava']['consent']
    openai = ai.provider_for({'cloud_llm': {'provider': 'openai'}}, 'cloud')
    deepseek = ai.provider_for(window.app.config, 'cloud')
    assert ai.consent_covers(record, provider=openai, features=('editing',), cloud=True)
    assert not ai.consent_covers(record, provider=openai, features=('ava',), cloud=True)
    assert ai.consent_covers(record, provider=deepseek, features=('ava',), cloud=True)
    assert not ai.consent_covers(record, provider=deepseek, features=('editing',), cloud=True)
    assert not ai.consent_covers(record, provider=openai, features=('editing',), cloud=True, version=3)


def test_provider_keys_and_models_stay_with_their_host_and_readiness(harness, monkeypatch):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['cloud_llm']['model'] = 'synthetic-deepseek'
    cfg['cloud_llm']['provider_settings'] = {'openai': {'api_key': 'synthetic-openai', 'model': 'synthetic-openai-model'}}
    window = make(cfg)
    observed = []
    def get(url, **kw):
        observed.append((url, kw['headers']['Authorization']))
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(cloud_llm.ai_transport, 'get_cloud', get)
    window._widgets['providerRoute'].setCurrentIndex(2)
    ava_readiness._monitor.run_once()
    window._widgets['cloud_provider'].setCurrentText('OpenAI')
    assert window._widgets['cloud_api_key'].text() == 'synthetic-openai'
    assert window._widgets['cloud_model'].text() == 'synthetic-openai-model'
    ava_readiness._monitor.run_once()
    assert observed == [('https://api.deepseek.com/v1/models', 'Bearer synthetic'),
                        ('https://api.openai.com/v1/models', 'Bearer synthetic-openai')]
    window._widgets['cloud_provider'].setCurrentText('DeepSeek (default)')
    assert window._widgets['cloud_model'].text() == 'synthetic-deepseek'
    assert window.app.config['cloud_llm']['api_key'] == 'synthetic'


def test_new_provider_without_own_key_never_probes_or_uses_previous_key(harness, monkeypatch):
    make, _, sent = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    calls = []
    monkeypatch.setattr(cloud_llm.ai_transport, 'get_cloud', lambda *a, **kw: calls.append(a))
    window._widgets['cloud_provider'].setCurrentText('OpenAI')
    assert window._widgets['cloud_api_key'].text() == ''
    assert window.app.config['cloud_llm']['api_key'] == ''
    ava_readiness._monitor.run_once()
    assert calls == [] and not ask(window.app).ok and sent == []


def test_readiness_builds_host_and_header_from_one_snapshot(harness, monkeypatch):
    make, _, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    original = cloud_llm._get_provider_config
    def change_live(configured):
        window.app.config['cloud_llm'].update(provider='openai', api_key='synthetic-other')
        return original(configured)
    monkeypatch.setattr(cloud_llm, '_get_provider_config', change_live)
    observed = []
    def get(url, **kw):
        observed.append((url, kw['headers']['Authorization']))
        return SimpleNamespace(status_code=200)
    assert ava_readiness.probe_configured_provider(window.app, http_get=get) == ('deepseek', None)
    assert observed == [('https://api.deepseek.com/v1/models', 'Bearer synthetic')]


def test_route_acceptance_keeps_smart_corrections_and_checkbox(harness):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['smart_corrections'].update(enabled=True, allow_cloud_fallback=True)
    window = make(cfg)
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert window.app.config['smart_corrections']['enabled'] is True
    assert window.app.config['smart_corrections']['allow_cloud_fallback'] is True
    assert window._widgets['sc_enabled'].isChecked()
    window._widgets['providerRoute'].setCurrentIndex(0)
    assert window._widgets['sc_enabled'].isChecked()
    assert window.app.config['smart_corrections']['enabled'] is True


def test_memory_acceptance_is_per_destination_including_saved_round_trip(harness):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['ava_memory']['mode'] = 'assistant'
    cfg['ava']['consent'] = consent.accepted_consent(cfg, cloud_enabled=True)
    cfg['ava']['consent']['memory_cloud_accepted'] = True
    window = make(cfg)
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert ask_ollama._assistant_recall_allowed(window.app, ai.runtime_state(window.app.config, 'ava'))
    window._widgets['cloud_provider'].setCurrentText('OpenAI')
    assert not window.app.config['ava']['consent'].get('memory_cloud_accepted')
    state = ai.runtime_state(window.app.config, 'ava')
    assert not ask_ollama._assistant_recall_allowed(window.app, state)
    window._widgets['cloud_provider'].setCurrentText('DeepSeek (default)')
    assert window.app.config['ava']['consent']['memory_cloud_accepted'] is True


def test_invalid_local_endpoint_retains_consent_and_restores_route_combo(harness):
    make, _, _ = harness
    cfg = _config()
    cfg['ava']['provider_policy'] = 'off'
    cfg['ollama']['host'] = 'http://192.0.2.1:11434'
    window = make(cfg)
    window._widgets['providerRoute'].setCurrentIndex(1)
    assert window._widgets['providerRoute'].currentData() == 'off'
    assert window._ava_last_route == 'off'
    assert window.app.config['ava']['provider_policy'] == 'off'
    assert not consent.consent_required(window.app.config)


def test_off_resets_readiness_and_accept_wakes_real_monitor(harness):
    make, _, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert ava_readiness._monitor is not None
    ava_readiness.tracker.record_probe('deepseek', None)
    previous = execution_policy.current_generation(window.app)
    window._widgets['providerRoute'].setCurrentIndex(0)
    assert execution_policy.current_generation(window.app) > previous
    assert ava_readiness.tracker.snapshot().state == ava_readiness.UNKNOWN
    ava_readiness._monitor._wake.clear()
    window._widgets['providerRoute'].setCurrentIndex(2)
    assert ava_readiness._monitor._wake.is_set()


def test_off_on_drops_blocked_cloud_reply_and_queued_work_clears_waiting(harness, monkeypatch):
    make, _, _ = harness
    window = make(local_accepted())
    app = window.app
    window._widgets['providerRoute'].setCurrentIndex(2)
    app.config['ava']['catalog_answers'] = False
    app.config['ava_memory']['mode'] = 'last'
    app._schedule_ui = lambda fn, *a, **kw: fn(*a, **kw)
    app.listening_indicator = Mock()
    app.answers = []
    app.tts_engine = SimpleNamespace(speak=lambda text: app.answers.append(text))
    workers = []
    monkeypatch.setattr(ava_turn_queue.thread_registry, 'spawn', lambda name, fn, **kw: workers.append((name, fn)))
    monkeypatch.setattr(ava_turn_queue.thread_registry, 'timer', lambda *a, **kw: Mock())
    for name in ('_handle_persona_intent', '_handle_working_context_intent', '_handle_personal_memory_intent', '_check_teaching_intent'):
        monkeypatch.setattr(ask_ollama, name, lambda *a: False)
    monkeypatch.setattr(ask_ollama, 'get_system_prompt', lambda app: 'Synthetic instructions.')
    from plugins.commands import dispatch_voice, comfy
    monkeypatch.setattr(dispatch_voice, 'route_ava_utterance', lambda *a: False)
    monkeypatch.setattr(comfy, 'try_voice_command', lambda *a: None)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def blocked(*a, **kw):
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(5)
            return 'Synthetic old reply.'
        return 'Synthetic new reply.'
    monkeypatch.setattr(cloud_llm, 'send', blocked)
    def take_worker():
        index = next(i for i, (name, fn) in enumerate(workers) if name == 'ask_ollama._worker')
        return workers.pop(index)[1]
    ask_ollama.handle_ask_ava(app, remainder='Synthetic question one?')
    worker = threading.Thread(target=take_worker())
    worker.start()
    try:
        assert entered.wait(2)
        ask_ollama.handle_ask_ava(app, remainder='Synthetic queued question?')
        assert app.listening_indicator.set_thinking.call_args.args == (True,)
        window._widgets['providerRoute'].setCurrentIndex(0)
        assert app.ava_thinking_since is None
        assert app.listening_indicator.set_thinking.call_args.args == (False,)
        window._widgets['providerRoute'].setCurrentIndex(2)
        ask_ollama.handle_ask_ava(app, remainder='Synthetic replacement question?')
        take_worker()()
        assert app.answers == ['Synthetic new reply.']
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert app.answers == ['Synthetic new reply.'] and calls == [True, True]

@pytest.mark.parametrize('typed', [False, True])
def test_stale_provider_cannot_apply_another_providers_credential(harness, monkeypatch, typed):
    make, _, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    # The real window retains DeepSeek controls while an outside writer saves OpenAI.
    outside = deepcopy(window.app.config)
    outside['cloud_llm'].update(provider='openai', api_key='synthetic-openai', model='synthetic-openai-model')
    outside['ava']['consent'] = consent.accepted_consent(outside, cloud_enabled=True)
    window.app.update_config({'cloud_llm': outside['cloud_llm'], 'ava': outside['ava']}, save=True)
    if typed:
        window._widgets['cloud_api_key'].setText('synthetic-edited-deepseek')
        window._widgets['cloud_model'].setText('synthetic-edited-deepseek-model')
    window._apply_and_close()
    live = window.app.config['cloud_llm']
    assert live['provider'] == 'openai'
    assert live['api_key'] == 'synthetic-openai'
    assert live['model'] == 'synthetic-openai-model'
    if typed:
        assert live['provider_settings']['deepseek']['api_key'] == 'synthetic-edited-deepseek'
        assert live['provider_settings']['deepseek']['model'] == 'synthetic-edited-deepseek-model'
    observed = []
    def get(url, **kwargs):
        observed.append((url, kwargs['headers']['Authorization']))
        return SimpleNamespace(status_code=200)
    assert ava_readiness.probe_configured_provider(window.app, http_get=get) == ('openai', None)
    assert observed == [('https://api.openai.com/v1/models', 'Bearer synthetic-openai')]


def test_cloud_accept_does_not_widen_a_noncloud_editing_grant():
    cfg = _config()
    provider = ai.provider_for(cfg, 'cloud')
    old = ai.accept_consent(cfg, features=('editing',), provider=provider, cloud=False)
    old['memory_cloud_accepted'] = True  # A flag alone never authorizes the new purpose.
    cfg['ava']['consent'] = old
    record = ai.accept_consent(cfg, features=('ava',), provider=provider, cloud=True)
    assert ai.consent_covers(record, features=('ava',), provider=provider, cloud=True)
    assert not ai.consent_covers(record, features=('editing',), provider=provider, cloud=True)
    assert ai.consent_covers(record, features=('editing',), provider=provider, cloud=False)
    assert not record.get('memory_cloud_accepted')


@pytest.mark.parametrize('initial,target', [('deepseek', 'OpenAI'), ('openai', 'DeepSeek (default)')])
@pytest.mark.parametrize('accepted', [False, True])
@pytest.mark.parametrize('error', [TypeError, ValueError])
def test_failed_provider_persist_restores_controls_then_apply_keeps_key_owner(
        harness, monkeypatch, initial, target, accepted, error):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['ava']['provider_policy'] = 'cloud'
    cfg['cloud_llm'].update(enabled=True, provider=initial,
                            api_key='synthetic-' + initial, model='synthetic-' + initial)
    cfg['ava']['consent'] = consent.accepted_consent(cfg, cloud_enabled=True)
    target_code = 'openai' if initial == 'deepseek' else 'deepseek'
    cfg['cloud_llm']['provider_settings'] = {
        target_code: {'api_key': 'synthetic-' + target_code, 'model': 'synthetic-' + target_code}}
    if accepted:
        other = deepcopy(cfg)
        other['cloud_llm']['provider'] = target_code
        cfg['ava']['consent'] = consent.accepted_consent(other, cloud_enabled=True)
    window = make(cfg)
    provider = window._widgets['cloud_provider']
    before_display = provider.currentText()
    before_baseline = deepcopy(window._ava_cloud_field_baseline)
    fields = {name: window._widgets[name].text() for name in ('cloud_api_key', 'cloud_model')}
    original = consent.apply_accepted_ava
    def fail_new_provider(*args, **kwargs):
        if kwargs.get('config', {}).get('cloud_llm', {}).get('provider') == target_code:
            raise error('Synthetic persistence failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(consent, 'apply_accepted_ava', fail_new_provider)
    provider.setCurrentText(target)
    assert provider.currentText() == before_display
    assert window._ava_previous_provider_display == before_display
    assert window._ava_cloud_field_baseline == before_baseline
    assert {name: window._widgets[name].text() for name in fields} == fields
    assert window._widgets['providerRoute'].currentData() == window._ava_last_route == 'cloud'
    window._apply_and_close()
    saved = window.app.config['cloud_llm']
    assert saved['provider'] == initial
    assert saved['api_key'] == saved['model'] == 'synthetic-' + initial
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs['headers']['Authorization']))
        return SimpleNamespace(status_code=200)
    ava_readiness.probe_configured_provider(window.app, http_get=get)
    assert calls == [('https://api.' + initial + '.com/v1/models', 'Bearer synthetic-' + initial)]


@pytest.mark.parametrize('initial,target', [('deepseek', 'OpenAI'), ('openai', 'DeepSeek (default)')])
@pytest.mark.parametrize('own_key', ['', 'synthetic-own'])
@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('outside', [False, True])
def test_apply_provider_merge_never_inherits_other_providers_untouched_key(
        harness, initial, target, own_key, typed, outside):
    make, _, _ = harness
    cfg = local_accepted()
    cfg['ava']['provider_policy'] = 'cloud'
    cfg['cloud_llm'].update(enabled=True, provider=initial,
                            api_key='' if outside else 'synthetic-other')
    cfg['ava']['consent'] = consent.accepted_consent(cfg, cloud_enabled=True)
    target_code = 'openai' if initial == 'deepseek' else 'deepseek'
    cfg['cloud_llm']['provider_settings'] = {target_code: {'api_key': own_key, 'model': ''}}
    other = deepcopy(cfg)
    other['cloud_llm']['provider'] = target_code
    cfg['ava']['consent'] = consent.accepted_consent(other, cloud_enabled=True)
    window = make(cfg)
    if outside:
        window.app.config['cloud_llm']['api_key'] = 'synthetic-outside-other'
    # Defend Apply itself against a stale/programmatic combo as well as
    # restoring the ordinary slot's failed immediate transaction.
    provider = window._widgets['cloud_provider']
    blocked = provider.blockSignals(True)
    provider.setCurrentText(target)
    provider.blockSignals(blocked)
    window._ava_cloud_field_baseline.update(api_key=own_key, model='')
    window._widgets['cloud_api_key'].setText('synthetic-typed-own' if typed else own_key)
    window._widgets['cloud_model'].setText('')
    window._apply_and_close()
    saved = window.app.config['cloud_llm']
    assert saved['provider'] == target_code
    assert saved['api_key'] == ('synthetic-typed-own' if typed else own_key)


@pytest.mark.parametrize('preview', [False, True])
def test_apply_then_cancel_kept_open_retains_applied_route_and_cancels_later_preview(
        harness, monkeypatch, preview):
    make, _, _ = harness
    window = make(local_accepted())
    window._widgets['providerRoute'].setCurrentIndex(2)
    window._widgets['cloud_api_key'].setText('synthetic-applied')
    # Use real Apply, suppress only its final Close to exercise a kept visit.
    with monkeypatch.context() as keep_open:
        keep_open.setattr(window, 'close', lambda: None)
        window._apply_and_close()
    assert window.app.config['ava']['provider_policy'] == 'cloud'
    if preview:
        window._widgets['providerRoute'].setCurrentIndex(1)
        assert window.app.config['ava']['provider_policy'] == 'local'
    window._widgets['cloud_api_key'].setText('synthetic-cancelled')
    accepted = deepcopy(window.app.config['ava']['consent'])
    window.close()
    assert window.app.config['ava']['provider_policy'] == 'cloud'
    assert window.app.config['cloud_llm']['api_key'] == 'synthetic-applied'
    assert window.app.config['ava']['consent'] == accepted
