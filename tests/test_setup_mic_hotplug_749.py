"""749: setup hot-plug behavior with fake enumeration and endpoint notifications."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtTest import QTest

from samsara.ui import first_run_wizard_qt as wizard


REALTEK = {'id': 1, 'name': 'Microphone (Realtek Audio)'}
HEADSET = {'id': 2, 'name': 'Microphone (USB Headset)'}
OTHER = {'id': 3, 'name': 'Other microphone'}


@pytest.fixture
def setup_mic(qapp, tmp_path, monkeypatch):
    devices = [dict(REALTEK)]
    notifications = []
    unregister = Mock()
    streams = []

    def register(callback):
        notifications.append(callback)
        return unregister

    def stream(_win, device):
        result = Mock()
        streams.append((device, result))
        return result

    monkeypatch.setattr(wizard.thread_registry, 'spawn', Mock())
    monkeypatch.setattr(wizard, 'register_notifications', register)
    monkeypatch.setattr(wizard, 'capture_endpoints', lambda: frozenset(
        (d['id'], d['name']) for d in devices))
    monkeypatch.setattr(wizard._WizardWindow, '_open_meter_stream', stream)
    app = SimpleNamespace(config={}, _mic_refresh_blocked=lambda: False,
                          refresh_audio_devices=Mock(side_effect=lambda: list(devices)))
    win = wizard._WizardWindow(tmp_path / 'config.json', app)
    win._mics = list(devices)
    win._on_mic_result('_load_done_', '')
    win._step = 1
    win._show_step()
    win.show()
    qapp.processEvents()
    win._mic_page._debounce.stop()
    app.refresh_audio_devices.reset_mock()

    def flush():
        qapp.processEvents()
        win._mic_page._debounce.stop()
        win._mic_page._debounce.timeout.emit()
        qapp.processEvents()

    yield SimpleNamespace(win=win, devices=devices, notify=lambda: notifications[-1](),
                          app=app, streams=streams, flush=flush,
                          unregister=unregister, qapp=qapp)
    win.close()
    win.deleteLater()
    qapp.processEvents()


def test_initial_devices_have_no_plugged_in_notice(setup_mic):
    s = setup_mic
    assert s.win._mic_notice.isHidden()
    assert s.win._mic_combo.currentText() == REALTEK['name']


def test_notification_refreshes_list_offers_new_input_without_switching(setup_mic):
    s = setup_mic
    s.devices.append(dict(HEADSET))
    s.notify()
    QTest.qWait(450)
    assert s.app.refresh_audio_devices.call_count == 1
    assert s.win._mic_combo.count() == 2
    assert s.win._mic_combo.currentText() == REALTEK['name']
    assert s.win._mic_notice.isVisible()
    assert s.win._mic_notice_text.text() == f"{HEADSET['name']} was plugged in."
    assert s.win._mic_use_btn.isVisible()


def test_use_it_selects_device_and_reopens_actual_meter_on_it(setup_mic):
    s = setup_mic
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    previous = s.streams[-1][1]
    s.win._mic_use_btn.click()
    assert s.win._get_current_mic_id() == HEADSET['id']
    assert s.streams[-1][0] == HEADSET['id']
    previous.stop.assert_called_once()
    previous.close.assert_called_once()
    assert s.win._mic_notice.isHidden()
    assert s.win._mic_capture_ready
    s.win._collect_step()
    assert s.win._config['microphone'] == HEADSET['id']


def test_output_notification_and_unchanged_inputs_do_not_refresh_or_notice(setup_mic):
    s = setup_mic
    s.notify()
    s.qapp.processEvents()
    assert not s.win._mic_page._debounce.isActive()
    s.app.refresh_audio_devices.assert_not_called()
    assert s.win._mic_notice.isHidden()


def test_reindexed_devices_keep_selection_and_do_not_count_as_new(setup_mic):
    s = setup_mic
    s.devices[0] = {**REALTEK, 'id': 9}
    s.notify()
    s.flush()
    assert s.win._get_current_mic_id() == 9
    assert s.win._mic_notice.isHidden()


def test_selected_removal_uses_system_default_not_first_item(setup_mic, monkeypatch):
    import sounddevice as sd
    s = setup_mic
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    s.win._mic_use_btn.click()
    s.devices[:] = [dict(OTHER), dict(REALTEK)]
    monkeypatch.setattr(sd, 'query_devices', lambda **kw: {'name': REALTEK['name']})
    s.notify()
    s.flush()
    assert s.win._get_current_mic_id() == REALTEK['id']
    assert s.streams[-1][0] == REALTEK['id']
    assert s.win._mic_notice.isVisible()
    assert 'Using the system default microphone.' in s.win._mic_notice_text.text()
    assert s.win._mic_use_btn.isHidden()


def test_selected_removal_with_no_inputs_blocks_next(setup_mic):
    s = setup_mic
    s.devices.clear()
    s.notify()
    s.flush()
    assert s.win._get_current_mic_id() is None
    assert not s.win._next_btn.isEnabled()
    assert 'disconnected' in s.win._mic_notice_text.text()
    assert 'system default' not in s.win._mic_notice_text.text()


def test_removed_offer_is_cleared_without_changing_selected_input(setup_mic):
    s = setup_mic
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    s.devices.pop()
    s.notify()
    s.flush()
    assert s.win._mic_notice.isHidden()
    assert s.win._get_current_mic_id() == REALTEK['id']


@pytest.mark.parametrize('hide_window', [False, True])
def test_hidden_page_ignores_even_queued_notifications_and_stops_watch(setup_mic, hide_window):
    s = setup_mic
    s.devices.append(dict(HEADSET))
    s.notify()
    if hide_window:
        s.win.hide()
    else:
        s.win._step = 0
        s.win._show_step()
    s.qapp.processEvents()
    assert not s.win._mic_page._debounce.isActive()
    assert not s.win._mic_page._poll.isActive()
    s.unregister.assert_called_once()
    s.notify()
    s.qapp.processEvents()
    s.app.refresh_audio_devices.assert_not_called()
    assert s.win._mic_combo.count() == 1


def test_hidden_changes_are_reconciled_when_page_is_shown_again(setup_mic):
    s = setup_mic
    s.win._step = 0
    s.win._show_step()
    s.devices.append(dict(HEADSET))
    s.win._step = 1
    s.win._show_step()
    s.flush()
    assert s.win._mic_combo.count() == 2
    assert s.win._mic_combo.currentText() == REALTEK['name']
    assert s.win._mic_notice.isVisible()


def test_notification_failure_polls_at_two_seconds_only_while_visible(setup_mic, monkeypatch):
    s = setup_mic
    s.win.hide()
    monkeypatch.setattr(wizard, 'register_notifications', Mock(side_effect=OSError('unavailable')))
    s.win.show()
    s.qapp.processEvents()
    page = s.win._mic_page
    page._debounce.stop()
    assert page._poll.isActive()
    assert page._poll.interval() == 2000
    s.devices.append(dict(HEADSET))
    page._poll.timeout.emit()
    s.flush()
    assert s.win._mic_notice.isVisible()
    assert s.win._mic_combo.currentText() == REALTEK['name']
    s.win.hide()
    assert not page._poll.isActive()
    s.app.refresh_audio_devices.reset_mock()
    page._poll.timeout.emit()
    s.qapp.processEvents()
    s.app.refresh_audio_devices.assert_not_called()


def test_new_device_meter_does_not_read_old_running_engine(setup_mic):
    s = setup_mic
    s.app._ace_engine = SimpleNamespace(_running=True, capture_name=REALTEK['name'])
    # Keep the refresh from trying to meter that synthetic engine.
    s.app._ace_engine._running = False
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    s.app._ace_engine._running = True
    s.win._mic_use_btn.click()
    assert s.streams[-1][0] == HEADSET['id']
    assert s.win._meter_ace_reader is None


def test_first_run_without_app_rescans_via_fake_enumerator(setup_mic, monkeypatch):
    s = setup_mic
    s.win._samsara_app = None
    rescan = Mock()
    monkeypatch.setattr(wizard, 'force_rescan', rescan)
    monkeypatch.setattr(wizard, 'list_microphones', lambda: list(s.devices))
    monkeypatch.setattr(wizard.thread_registry, 'spawn', lambda name, target, **kw: target())
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    rescan.assert_called_once()
    assert s.win._mic_combo.currentText() == REALTEK['name']
    assert s.win._mic_notice.isVisible()
    s.win._mic_use_btn.click()
    assert s.streams[-1][0] == HEADSET['id']


def test_default_unavailable_does_not_silently_choose_first_input(setup_mic, monkeypatch):
    import sounddevice as sd
    s = setup_mic
    monkeypatch.setattr(sd, 'query_devices', Mock(side_effect=RuntimeError('no default')))
    s.devices[:] = [dict(OTHER)]
    s.notify()
    s.flush()
    assert s.win._mic_combo.currentIndex() == -1
    assert not s.win._next_btn.isEnabled()
    assert 'Using the system default' not in s.win._mic_notice_text.text()


def test_scan_failure_is_not_reported_as_unplugging(setup_mic):
    s = setup_mic
    s.app.refresh_audio_devices.side_effect = RuntimeError('scan unavailable')
    s.win._enumerate_mics = Mock(side_effect=RuntimeError('scan unavailable'))
    s.devices.append(dict(HEADSET))
    s.notify()
    s.flush()
    assert s.win._mic_combo.currentText() == REALTEK['name']
    assert s.win._get_current_mic_id() == REALTEK['id']
    assert s.win._mic_notice.isHidden()
    assert 'press Refresh' in s.win._mic_status.text()


def test_callback_from_worker_is_marshaled_to_ui(setup_mic):
    import threading
    from PySide6.QtCore import QThread
    s = setup_mic
    threads = []
    s.app.refresh_audio_devices.side_effect = lambda: (threads.append(QThread.currentThread())
                                                      or list(s.devices))
    s.devices.append(dict(HEADSET))
    worker = threading.Thread(target=s.notify)
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    s.app.refresh_audio_devices.assert_not_called()
    s.flush()
    assert threads == [s.win.thread()]
    assert s.win._mic_notice.isVisible()
