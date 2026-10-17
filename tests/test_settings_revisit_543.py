"""Retained Settings window visit and provider-specific voice regressions (543)."""
from copy import deepcopy
from types import SimpleNamespace
from threading import RLock
from time import perf_counter
from PySide6.QtTest import QTest
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QDialog

from samsara.ui import settings_qt, tutorial_try_preview, ava_consent_qt
from tests.test_settings_pages import _StubApp, _schema_defaults


def _wait_catalog(window):
    """619: enumeration is asynchronous; retain every catalog assertion."""
    deadline = perf_counter() + 5
    while getattr(window, '_tts_voices_pending', False) and perf_counter() < deadline:
        QTest.qWait(10)
    assert not window._tts_voices_pending, 'voice catalog worker did not finish'


@pytest.fixture
def window(qapp, monkeypatch):
    import sounddevice
    from samsara import autostart, ava_readiness, listening_display
    from samsara.ui.first_run_qt import ComponentsPage
    monkeypatch.setattr(sounddevice, 'query_devices', lambda *a, **k: [])
    monkeypatch.setattr(sounddevice, 'query_hostapis', lambda *a, **k: [])
    monkeypatch.setattr(autostart, 'sync_run_entry', lambda *a, **k: None)
    monkeypatch.setattr(ComponentsPage, 'refresh_async', lambda *a: None)
    monkeypatch.setattr(ava_readiness, 'recheck_after_settings', lambda *a: None)
    monkeypatch.setattr(listening_display, 'request_restart_once', lambda *a: None)
    monkeypatch.setattr(ava_consent_qt.AvaConsentDialog, 'exec', lambda self: QDialog.DialogCode.Accepted)
    config = deepcopy(_schema_defaults())
    config['tts'].update(engine='winrt', voice_id='local-1', network_voice_accepted=True,
                         enabled=True, feedback_level='brief')
    config['ava']['provider_policy'] = 'local'
    # Establish both grants through the production consent helper, with fake config only.
    config['ava']['consent'] = ava_consent_qt.accepted_consent(config)
    config['auto_capitalize'] = True
    app = _StubApp(config)
    app.save_config = Mock()
    app.audio_coordinator = SimpleNamespace(engine=object(), speak=Mock(), _dispatch_lock=RLock())
    made = []
    def engine(provider):
        voices = [SimpleNamespace(voice_id=f'{provider}-{n}', display_name=f'{provider} {n}', language='en-US') for n in (1, 2)]
        return SimpleNamespace(list_voices=Mock(return_value=voices), shutdown=Mock())
    app.tts_engine = engine('local')
    def make_engine(_app, provider):
        e = engine('network' if provider == 'edge' else 'local')
        made.append((provider, e))
        return e
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', make_engine)
    def replace(self, cfg):
        app.tts_engine = engine('network' if cfg['engine'] == 'edge' else 'local')
    monkeypatch.setattr(settings_qt._SettingsWindow, '_replace_tts_runtime', replace)
    win = settings_qt._SettingsWindow(app)
    win._test_made = made
    win.show_tab('Audio')
    _wait_catalog(win)
    qapp.processEvents()
    yield win
    win.hide()
    win.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


def reopen(win, qapp, page='Audio'):
    win.show_tab(page)
    _wait_catalog(win)
    qapp.processEvents()


def test_one_window_applies_checkbox_then_original_value(window, qapp):
    identity = id(window)
    window._widgets['auto_capitalize'].setChecked(False)
    window._apply_and_close()
    assert window.app.config['auto_capitalize'] is False
    reopen(window, qapp)
    assert id(window) == identity
    assert window._widgets['auto_capitalize'].isChecked() is False
    window._widgets['auto_capitalize'].setChecked(True)
    window._apply_and_close()
    assert window.app.config['auto_capitalize'] is True
    assert window.app.save_config.call_count == 2


def test_one_window_local_cloud_local_applies_privacy_choice(window, qapp):
    reopen(window, qapp, 'Ava')
    window._widgets['providerRoute'].setCurrentIndex(2)
    window._apply_and_close()
    assert window.app.config['ava']['provider_policy'] == 'cloud'
    reopen(window, qapp, 'Ava')
    assert window._config_at_open['ava']['provider_policy'] == 'cloud'
    assert window._widgets['providerRoute'].currentData() == 'cloud'
    window._widgets['providerRoute'].setCurrentIndex(1)
    window._apply_and_close()
    assert window.app.config['ava']['provider_policy'] == 'local'


def test_one_window_engine_round_trip_and_lists(window, qapp):
    live = window.app.tts_engine
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    combo = window._widgets['tts_voice_combo']
    assert combo.currentText() == 'Default voice'
    assert combo.currentData() is None
    assert set(window._widgets['tts_voice_label_to_id'].values()) == {'network-1', 'network-2'}
    assert window.app.tts_engine is live
    assert window._test_made[-1][1].shutdown.call_count == 1
    combo.setCurrentIndex(combo.findData('network-2'))
    window._apply_and_close()
    assert window.app.config['tts']['engine'] == 'edge'
    assert window.app.config['tts']['voice_id'] == 'network-2'
    reopen(window, qapp)
    assert window._widgets['tts_voice_combo'].currentData() == 'network-2'
    window._widgets['tts_engine'].setCurrentText('winrt')
    _wait_catalog(window)
    assert set(window._widgets['tts_voice_label_to_id'].values()) == {'local-1', 'local-2'}
    assert window._widgets['tts_voice_combo'].currentData() is None
    window._apply_and_close()
    assert window.app.config['tts']['engine'] == 'winrt'
    assert window.app.config['tts']['voice_id'] is None
    reopen(window, qapp)
    assert window._widgets['tts_engine'].currentText() == 'winrt'
    assert set(window._widgets['tts_voice_label_to_id'].values()) == {'local-1', 'local-2'}


def test_close_discards_unapplied_edits_on_every_page(window, qapp):
    window._widgets['auto_capitalize'].setChecked(False)
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    window._widgets['tts_speed'].setValue(1.8)
    window.close()
    window.app.config['sound_volume'] = .65
    reopen(window, qapp)
    assert window._widgets['auto_capitalize'].isChecked()
    assert window._widgets['tts_engine'].currentText() == 'winrt'
    assert window._widgets['tts_speed'].value() == window.app.config['tts']['speed']
    window._apply_and_close()
    assert window.app.config['auto_capitalize'] is True
    assert window.app.config['tts']['engine'] == 'winrt'
    assert window.app.config['sound_volume'] == .65


def test_visible_navigation_keeps_edits_and_snapshot(window):
    before = deepcopy(window._config_at_open)
    window._widgets['auto_capitalize'].setChecked(False)
    window.app.config['sound_volume'] = .65
    window.show_tab('General')
    assert window._config_at_open == before
    assert window._widgets['auto_capitalize'].isChecked() is False
    window._apply_and_close()
    assert window.app.config['sound_volume'] == .65
    assert window.app.config['auto_capitalize'] is False


def test_hidden_minimized_reopen_discards_abandoned_edits(window, qapp):
    window._widgets['auto_capitalize'].setChecked(False)
    window.showMinimized()
    qapp.processEvents()
    window.close()
    window.app.config['sound_volume'] = .65
    assert window.isMinimized()
    reopen(window, qapp)
    assert not window.isMinimized()
    assert window._config_at_open['sound_volume'] == .65
    assert window._widgets['auto_capitalize'].isChecked()
    window._apply_and_close()
    assert window.app.config['auto_capitalize'] is True


def test_revisit_keeps_outside_changes_while_open(window, qapp):
    window._widgets['auto_capitalize'].setChecked(False)
    window._apply_and_close()
    reopen(window, qapp)
    window.app.config['auto_capitalize'] = True
    window._widgets['tts_speed'].setValue(1.4)
    window._apply_and_close()
    assert window.app.config['auto_capitalize'] is True
    assert window.app.config['tts']['speed'] == 1.4


def test_mismatched_pair_opens_default_and_never_saves_bad_id(window, qapp):
    window.close()
    window.app.config['tts']['voice_id'] = 'network-2'
    reopen(window, qapp)
    assert window._widgets['tts_voice_combo'].currentText() == 'Default voice'
    assert window._widgets['tts_voice_combo'].currentData() is None
    window._apply_and_close()
    assert window.app.config['tts']['voice_id'] is None


def test_test_voice_refuses_pending_engine_and_uses_each_selected_voice(window):
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    window._test_tts()
    window.app.audio_coordinator.speak.assert_not_called()
    assert 'Apply' in window._widgets['tts_test_status'].text()
    window._widgets['tts_engine'].setCurrentText('winrt')
    _wait_catalog(window)
    combo = window._widgets['tts_voice_combo']
    for voice_id in ('local-1', 'local-2'):
        combo.setCurrentIndex(combo.findData(voice_id))
        window._test_tts()
        assert window.app.audio_coordinator.speak.call_args.kwargs['voice_id'] == voice_id


def test_network_catalog_visible_before_consent_but_speech_gated(window, monkeypatch, qapp):
    from samsara.ui import network_voice_notice
    window.close()
    window.app.config['tts'].update(engine='edge', voice_id='network-2', network_voice_accepted=False)
    # Model a legacy Network runtime, not a config-only provider change.
    live = SimpleNamespace(list_voices=Mock(return_value=[
        SimpleNamespace(voice_id='network-2', display_name='Network 2', language='en-US')]),
        shutdown=Mock())
    window.app.tts_engine = live
    window._test_made.clear()
    reopen(window, qapp)
    live.list_voices.assert_called_once()
    assert window._test_made == []
    assert window._widgets['tts_voice_combo'].currentData() == 'network-2'
    monkeypatch.setattr(network_voice_notice.NetworkVoiceNotice, 'exec', lambda self: QDialog.DialogCode.Rejected)
    window._test_tts()
    window.app.audio_coordinator.speak.assert_not_called()
    live.list_voices.assert_called_once()
    window._resolve_existing_voice(False)
    _wait_catalog(window)
    assert live.list_voices.call_count == 2
    assert window._widgets['tts_voice_combo'].currentData() == 'network-2'
    live.shutdown.assert_not_called()


def test_private_engine_disposed_on_list_failure(window, monkeypatch):
    failing = SimpleNamespace(list_voices=Mock(side_effect=RuntimeError()), shutdown=Mock())
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', lambda *a: failing)
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    failing.shutdown.assert_called_once()
    assert window._source_saves['tts'][0]({})['tts']['voice_id'] == 'local-1'


def test_default_preview_requires_apply_instead_of_using_saved_voice(window, qapp):
    window._widgets['tts_voice_combo'].setCurrentIndex(0)
    window._test_tts()
    window.app.audio_coordinator.speak.assert_not_called()
    assert 'Apply' in window._widgets['tts_test_status'].text()
    window._apply_and_close()
    reopen(window, qapp)
    window._test_tts()
    assert window.app.audio_coordinator.speak.call_args.kwargs['voice_id'] is None


def test_voice_identity_is_not_its_display_label(window):
    voices = [SimpleNamespace(voice_id=f'local-{n}', display_name='Same name', language='en-US')
              for n in (1, 2)]
    window.app.tts_engine.list_voices.return_value = voices
    window._refresh_tts_voices('local-1')
    _wait_catalog(window)
    window._test_tts()
    assert window.app.audio_coordinator.speak.call_args.kwargs['voice_id'] == 'local-1'
    window._apply_and_close()
    assert window.app.config['tts']['voice_id'] == 'local-1'


def test_private_disposal_failure_still_shows_default(window, monkeypatch):
    failing = SimpleNamespace(list_voices=Mock(side_effect=RuntimeError()),
                              shutdown=Mock(side_effect=RuntimeError()))
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', lambda *a: failing)
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    failing.shutdown.assert_called_once()
    assert window._widgets['tts_voice_combo'].currentData() is None


def test_outside_engine_and_valid_voice_are_preserved_when_untouched(window):
    window.app.config['tts'].update(engine='edge', voice_id='network-2')
    window.app.tts_engine = SimpleNamespace(list_voices=lambda: [
        SimpleNamespace(voice_id='network-2', display_name='Network 2', language='en-US')])
    window._widgets['auto_capitalize'].setChecked(False)
    window._apply_and_close()
    assert window.app.config['tts']['engine'] == 'edge'
    assert window.app.config['tts']['voice_id'] == 'network-2'


def test_outside_engine_change_cannot_save_a_dialog_voice_from_old_engine(window):
    combo = window._widgets['tts_voice_combo']
    combo.setCurrentIndex(combo.findData('local-2'))
    window.app.config['tts'].update(engine='edge', voice_id='network-1')
    window._apply_and_close()
    assert window.app.config['tts']['engine'] == 'edge'
    assert window.app.config['tts']['voice_id'] is None


@pytest.mark.parametrize('failed', (False, True))
def test_unavailable_catalog_preserves_saved_voice_on_unrelated_apply(window, failed):
    if failed:
        window.app.tts_engine.list_voices.side_effect = RuntimeError('Unavailable')
    else:
        window.app.tts_engine.list_voices.return_value = []
    window._refresh_tts_voices('local-1')
    _wait_catalog(window)
    assert window._widgets['tts_voice_combo'].currentData() is None
    window._widgets['auto_capitalize'].setChecked(False)
    window._apply_and_close()
    assert window.app.config['tts']['voice_id'] == 'local-1'
    assert window.app.config['auto_capitalize'] is False


def test_switch_away_and_back_before_apply_keeps_saved_voice(window):
    window._widgets['tts_engine'].setCurrentText('edge')
    _wait_catalog(window)
    window._widgets['tts_engine'].setCurrentText('winrt')
    _wait_catalog(window)
    assert window._widgets['tts_voice_combo'].currentData() == 'local-1'
    window._apply_and_close()
    assert window.app.config['tts']['voice_id'] == 'local-1'


def test_constructor_and_first_show_enumerate_once(window):
    window.app.tts_engine.list_voices.assert_called_once()


def test_builtin_network_catalog_needs_no_network_or_consent(window, monkeypatch):
    import socket
    from samsara.tts import edge_tts_engine
    network = Mock(side_effect=AssertionError('Network is forbidden'))
    monkeypatch.setattr(socket.socket, 'connect', network)
    monkeypatch.setattr(socket, 'getaddrinfo', network)
    monkeypatch.setattr(edge_tts_engine, '_import_edge_tts', lambda: SimpleNamespace(
        Communicate=network, list_voices=network))
    private = edge_tts_engine.EdgeTTSEngine()
    monkeypatch.setattr(tutorial_try_preview, 'make_engine', lambda *a: private)
    window._network_voice_accepted = False
    window._widgets['tts_engine'].blockSignals(True)
    window._widgets['tts_engine'].setCurrentText('edge')
    window._widgets['tts_engine'].blockSignals(False)
    window._refresh_tts_voices('en-US-JennyNeural')
    _wait_catalog(window)
    assert window._widgets['tts_voice_combo'].currentData() == 'en-US-JennyNeural'
    assert window._tts_catalog_ids == {v.voice_id for v in edge_tts_engine._BUILTIN_VOICES}
    network.assert_not_called()
    window.app.audio_coordinator.speak.assert_not_called()


def test_todoist_completion_after_rebuild_does_not_touch_retired_label(window, monkeypatch, qapp):
    from samsara.ui.settings import commands_qt
    from samsara import todoist_client
    from PySide6.QtWidgets import QPushButton, QLabel
    from shiboken6 import isValid
    workers, posted = [], []
    spawn = commands_qt.thread_registry.spawn
    monkeypatch.setattr(commands_qt.thread_registry, 'spawn',
                        lambda name, fn, **kw: workers.append(fn) if name == 'settings-todoist-test' else spawn(name, fn, **kw))
    monkeypatch.setattr(commands_qt.qt_runtime, 'post', posted.append)
    monkeypatch.setattr(todoist_client, 'today', lambda token: [])
    window._widgets['todoist_token'].setText('synthetic-test-value')
    page = window._tool_pages['Integrations']
    next(b for b in page.findChildren(QPushButton) if b.text() == 'Test connection').click()
    label = next(l for l in page.findChildren(QLabel) if l.text() == 'Testing Todoist…')
    assert len(workers) == 1
    window.close()
    reopen(window, qapp)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(label)
    workers.pop()()
    posted.pop()()  # Must neither raise nor update the next visit.


def test_cloud_completion_after_revisit_does_not_update_new_status(window, monkeypatch, qapp):
    from samsara.ui.settings import ava_cloud_qt
    from samsara import cloud_llm
    workers = []
    spawn = ava_cloud_qt.thread_registry.spawn
    monkeypatch.setattr(ava_cloud_qt.thread_registry, 'spawn',
                        lambda name, fn, **kw: workers.append(fn) if name == 'settings_qt._do' else spawn(name, fn, **kw))
    monkeypatch.setattr(cloud_llm, 'check_available', lambda app: (True, 'ok'))
    window._widgets['cloud_api_key'].setText('synthetic-test-value')
    window._run_test_connection()
    assert len(workers) == 1
    window.close()
    reopen(window, qapp)
    status = window._widgets['cloud_test_status']
    before = status.text()
    workers.pop()()
    assert status.text() == before


@pytest.mark.parametrize('result, expected', (
    ('windows', "The Network voice didn't answer, so the Windows voice spoke instead."),
    ('failed', "Couldn't play the voice sample."),
    ('cancelled', 'cancelled'),
), ids=(  # Preserve the HEAD node IDs while updating the expected wording.
    'windows-Spoken with the Windows voice',
    'failed-could not be spoken',
    'cancelled-cancelled',
))
def test_sample_outcome_is_polled_on_qt_thread(window, result, expected):
    handle = SimpleNamespace(voice_result='pending', network_failed=False)
    window.app.audio_coordinator.speak.return_value = handle
    window._test_tts()
    timer = window._tts_sample_timer
    assert timer.isActive()
    handle.network_failed = True
    timer.timeout.emit()
    assert 'Trying the Windows voice' in window._widgets['tts_test_status'].text()
    handle.voice_result = result
    timer.timeout.emit()
    assert expected in window._widgets['tts_test_status'].text()
    assert not timer.isActive()


def test_sample_poll_is_stopped_on_close_and_revisit(window, qapp):
    window.app.audio_coordinator.speak.return_value = SimpleNamespace(
        voice_result='pending', network_failed=False)
    window._test_tts()
    timer = window._tts_sample_timer
    window.close()
    assert not timer.isActive()
    reopen(window, qapp)
    assert window._widgets['tts_test_status'].text() == ''


def test_newer_sample_replaces_old_poll_and_legacy_handle_is_supported(window):
    first = SimpleNamespace(voice_result='pending', network_failed=False)
    window.app.audio_coordinator.speak.return_value = first
    window._test_tts()
    old_timer = window._tts_sample_timer
    window.app.audio_coordinator.speak.return_value = object()
    window._test_tts()
    assert not old_timer.isActive()
    first.voice_result = 'windows'
    old_timer.timeout.emit()
    assert 'Spoken with the Windows' not in window._widgets['tts_test_status'].text()


def test_command_completion_after_close_cannot_restore_settings(window, monkeypatch, qapp):
    import time
    from samsara.ui.settings import commands_qt
    workers, posted = [], []
    monkeypatch.setattr(commands_qt.thread_registry, 'spawn', lambda name, fn, **kw: workers.append(fn))
    monkeypatch.setattr(commands_qt.qt_runtime, 'post', posted.append)
    monkeypatch.setattr(time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(window, '_selected_phrase', lambda table: 'synthetic-command')
    window.app.command_executor.process_text = Mock(return_value=(None, True))
    window._test_selected_command(window._widgets['cmd_table'])
    window.close()
    workers.pop()()
    assert len(posted) == 2
    for callback in posted:
        callback()
    qapp.processEvents()
    assert not window.isVisible()


def test_queued_physical_capture_is_revoked_before_widgets_retire(window, monkeypatch, qapp):
    from samsara import hold_key_rules
    from samsara.ui import qt_runtime
    from shiboken6 import isValid
    posted = []
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', posted.append)
    button = window._widgets['cmd_tab_button']
    button._start_capture()
    assert hold_key_rules.route_capture('x', True)
    window.close()
    assert not button._capturing
    reopen(window, qapp)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert not isValid(button)
    current = window._widgets['cmd_tab_button']
    before = current.combo
    posted.pop()()
    assert current.combo == before
