"""1218: ACTIVE display truth, event policy, and real guarded fake-clock gaps."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara import microphone_recovery as recovery
from tests.test_audio_hotplug_recovery import dev
from tests.test_microphone_refresh_real_sequence_1217 import (
    CHOSEN, HEADSET, audits, sequence,  # noqa: F401 - shared registered-stream fixture
)


def advance(changes, now, target):
    while now[0] < target:
        now[0] = min(target, now[0] + .5)
        changes.tick()


@pytest.mark.parametrize('relation', ['selected', 'default', 'unrelated_named', 'unrelated_default'])
@pytest.mark.parametrize('event', ['added', 'removed', 'disabled', 'enabled', 'default'])
def test_event_truth_table_and_capture_gap(sequence, monkeypatch, record_property, relation, event):
    audio, app, changes, now, endpoints, guard, lives = sequence
    arriving = event in ('added', 'enabled')
    # Events affect the selected/current default endpoint in the first two
    # columns; the other columns affect an unrelated input.
    target = CHOSEN if relation == 'default' else HEADSET
    initial = {('chosen', CHOSEN)}
    if not arriving or target == CHOSEN:
        initial.add(('headset', HEADSET))
    if arriving and target == CHOSEN:
        initial.discard(('chosen', CHOSEN))
    audio.devices = [dev(name) for _, name in sorted(initial)]
    audio.default.device = (0, -1)
    app.available_mics = app.get_available_microphones()
    changes._backend_mics = list(app.available_mics)
    endpoints[0] = frozenset(initial)
    default = ['chosen' if ('chosen', CHOSEN) in initial else 'headset']
    changes.input_id = lambda: default[0]
    if relation in ('default', 'unrelated_default'):
        app.config.update(microphone=None, microphone_name='System default')
    elif relation == 'selected':
        app.config.update(microphone=1, microphone_name=HEADSET)
        app._ace_engine._device_name = CHOSEN if arriving else HEADSET
    changes.prime()
    saved_name = app.config['microphone_name']
    final = set(initial)
    if event == 'default':
        default[0] = 'headset' if default[0] == 'chosen' else 'chosen'
    elif arriving:
        final.add(('chosen' if target == CHOSEN else 'headset', target))
    else:
        final.discard(('chosen' if target == CHOSEN else 'headset', target))
    if relation == 'default' and event != 'default':
        default[0] = 'chosen' if arriving else 'headset'
    endpoints[0] = frozenset(final)
    audio.pending = [dev(name) for _, name in sorted(final)]
    audio.default.device = (0, -1)
    original = audio._initialize
    def initialize():
        now[0] += .240
        original()
    monkeypatch.setattr(audio, '_initialize', initialize)
    lives.clear()
    changes.notify('state' if event in ('enabled', 'disabled') else event)
    advance(changes, now, 1)
    expected_scan = (relation == 'default' or (relation == 'selected' and event != 'default')
                     or (relation == 'unrelated_default' and event == 'default'))
    if not expected_scan:
        assert not changes._input_pending
    if event not in ('added', 'enabled'):
        assert not audio.events  # Departures/default changes wait for the burst to settle.
    offered = {mic['name'] for mic in app.available_mics}
    assert offered == {name for _, name in final}
    advance(changes, now, 4)
    assert audio.events == (['terminate', 'initialize'] if expected_scan else [])
    closes = [at for action, kind, at in lives if action == 'close' and kind == 'InputStream']
    opens = [at for action, kind, at in lives if action == 'open' and kind == 'InputStream']
    gap_ms = (opens[0] - closes[0]) * 1000 if closes else 0
    record_property('capture_gap_ms', round(gap_ms))
    assert gap_ms == pytest.approx(240 if expected_scan else 0)
    assert app.config['microphone_name'] == saved_name


def test_owner_disable_then_reenable_while_focusrite_stays_selected(sequence, caplog):
    audio, app, changes, now, endpoints, guard, lives = sequence
    audio.devices = [dev(CHOSEN), dev(HEADSET)]
    audio.pending = None
    app.available_mics = app.get_available_microphones()
    changes._backend_mics = list(app.available_mics)
    changes.prime()
    lives.clear()
    saved = dict(app.config)
    with caplog.at_level('INFO'):
        endpoints[0] = frozenset({('chosen', CHOSEN)})
        changes.notify('state')
        advance(changes, now, 1)
        assert [m['name'] for m in app.available_mics] == [CHOSEN]
        endpoints[0] |= {('headset', HEADSET)}
        changes.notify('state')
        advance(changes, now, 2)
    assert {m['name'] for m in app.available_mics} == {CHOSEN, HEADSET}
    assert app.config == saved and not audio.events and not lives
    assert len(audits(caplog)) == 2
    assert f'removed={[HEADSET]}' in audits(caplog)[0]
    assert f'added={[HEADSET]}' in audits(caplog)[1]
    assert all('trigger=state' in line and 'selection_changed=False' in line for line in audits(caplog))


@pytest.mark.parametrize('selected', [False, True])
def test_disable_enable_burst_coalesces_final_state(sequence, selected):
    audio, app, changes, now, endpoints, guard, lives = sequence
    target = ('chosen', CHOSEN) if selected else ('headset', HEADSET)
    changes.prime()
    endpoints[0] -= {target}
    changes.notify('state')
    advance(changes, now, 1)
    endpoints[0] |= {target}
    changes.notify('state')
    advance(changes, now, 2)
    assert not audio.events
    advance(changes, now, 5)
    assert audio.events.count('initialize') == int(selected)
    assert {m['name'] for m in app.available_mics} == {CHOSEN, HEADSET}


@pytest.mark.parametrize('surface', ['tray', 'settings'])
def test_menu_or_picker_open_reads_live_state_even_during_speech(sequence, monkeypatch, qapp, surface):
    audio, app, changes, now, endpoints, guard, lives = sequence
    app.recording = True
    app.switch_microphone_and_refresh = Mock()
    if surface == 'tray':
        from samsara.ui import tray_microphones as tray
        monkeypatch.setattr(tray, 'QMenu', lambda label: SimpleNamespace(setStyleSheet=Mock()))
        labels = []
        tray.build_microphone_menu(app, lambda menu, label, *a, **kw: labels.append(label))
        assert any(HEADSET in label for label in labels)
        endpoints[0] -= {('headset', HEADSET)}
        labels.clear()
        tray.build_microphone_menu(app, lambda menu, label, *a, **kw: labels.append(label))
        assert not any(HEADSET in label for label in labels)
    else:
        from samsara.ui.settings.general_qt import _MicrophoneComboBox, _populate_microphone_combo
        combo = _MicrophoneComboBox(app)
        _populate_microphone_combo(combo, app.available_mics, app.config)
        # Invoke real Python popup override, suppress only platform popup IO.
        monkeypatch.setattr(_MicrophoneComboBox.__bases__[0], 'showPopup', lambda self: None)
        combo.showPopup()
        assert any(HEADSET in combo.itemText(i) for i in range(combo.count()))
        endpoints[0] -= {('headset', HEADSET)}
        combo.showPopup()
        assert not any(HEADSET in combo.itemText(i) for i in range(combo.count()))
        combo.deleteLater()
    assert not audio.events and not changes._input_pending


@pytest.mark.parametrize('surface', ['tray', 'settings'])
@pytest.mark.parametrize('removed', [False, True])
def test_picking_unknown_or_just_removed_device_rescans_by_name(sequence, surface, removed):
    audio, app, changes, now, endpoints, guard, lives = sequence
    recovery.display_microphones(app)
    entry = next(m for m in app.available_mics if m['name'] == HEADSET)
    assert isinstance(entry['id'], str)  # Not in PortAudio yet.
    if removed:
        endpoints[0] -= {('headset', HEADSET)}
        audio.pending = [dev(CHOSEN)]
    if surface == 'settings':
        from samsara.ui.settings.services import apply_microphone_choice
        updates = {'microphone': entry['id'], 'microphone_name': HEADSET}
        apply_microphone_choice(app, updates)
        assert not isinstance(updates['microphone'], str)
    else:
        recovery.select_microphone(app, entry['id'], name=entry['name'])
    assert audio.events == ['terminate', 'initialize']
    assert app.config['microphone_name'] == HEADSET  # Keep reconnect preference, 550/993.
    assert app._using_default_mic == removed
    assert app._ace_engine.capture_name == (CHOSEN if removed else HEADSET)
    assert app._capture_device == (0 if removed else 1)


def test_cached_numeric_choice_removed_after_menu_open_falls_back(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    audio.devices = [dev(CHOSEN), dev(HEADSET)]
    app.available_mics = app.get_available_microphones()
    changes._backend_mics = list(app.available_mics)
    entry = app.available_mics[1]
    endpoints[0] -= {('headset', HEADSET)}
    audio.pending = [dev(CHOSEN)]
    recovery.select_microphone(app, entry['id'], name=HEADSET)
    assert app._using_default_mic and app.config['microphone_name'] == HEADSET
    assert app._ace_engine.capture_name == CHOSEN


def test_endpoint_failure_keeps_last_list_and_truncation_is_not_guessed(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    changes._backend_mics = [{'id': 7, 'name': HEADSET[:31], 'channels': 1}]
    displayed = recovery.display_microphones(app)
    assert next(m for m in displayed if m['id'] == 7)['name'] == HEADSET[:31]
    endpoints[0] |= {('alias', HEADSET + ' #2')}
    displayed = recovery.display_microphones(app)
    assert not any(m['id'] == 7 for m in displayed)
    changes.endpoints = Mock(side_effect=OSError('no COM'))
    assert recovery.display_microphones(app) is displayed
    assert not audio.events


def test_missed_callback_poll_updates_removal_without_reset(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    recovery.display_microphones(app)
    changes.prime()
    endpoints[0] -= {('headset', HEADSET)}
    now[0] = 5
    changes.tick()
    assert [m['name'] for m in app.available_mics] == [CHOSEN]
    assert not audio.events and not changes._input_pending


def test_explicit_refresh_without_observed_windows_snapshot_uses_backend_list(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    changes._capture = None
    changes.endpoints = Mock(side_effect=AssertionError('no native query during reset'))
    result = recovery.request_refresh(app)
    assert [mic['name'] for mic in result] == [CHOSEN, HEADSET]
    changes.endpoints.assert_not_called()
    assert audio.events == ['terminate', 'initialize']


def test_return_to_preferred_input_waits_for_speech_but_no_extra_settle(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    app.config.update(microphone=9, microphone_name=HEADSET)
    app._using_default_mic = True
    app.recording = True
    changes.notify('added')
    advance(changes, now, 1)
    assert changes._input_pending and not audio.events
    app.recording = False
    advance(changes, now, 2)
    assert audio.events == ['terminate', 'initialize']
    assert app._ace_engine.capture_name == HEADSET and not app._using_default_mic


def test_endpoint_display_enumeration_balances_callers_com(monkeypatch):
    import ctypes
    import sys
    from samsara import audio_devices
    initialize, uninitialize = Mock(return_value=True), Mock()
    monkeypatch.setattr(audio_devices, '_ensure_com_initialized', initialize)
    monkeypatch.setattr(ctypes.windll.ole32, 'CoUninitialize', uninitialize)
    enumerate_active = Mock(return_value=[SimpleNamespace(id='usb', FriendlyName=HEADSET)])
    monkeypatch.setitem(sys.modules, 'pycaw.pycaw', SimpleNamespace(
        AudioUtilities=SimpleNamespace(GetAllDevices=enumerate_active)))
    assert recovery.capture_endpoints() == frozenset({('usb', HEADSET)})
    enumerate_active.assert_called_once_with(data_flow=1, device_state=1)
    initialize.assert_called_once_with()
    uninitialize.assert_called_once_with()


def test_stale_menu_index_reused_for_another_active_input_keeps_clicked_name(sequence):
    audio, app, changes, now, endpoints, guard, lives = sequence
    app.available_mics = [{'id': 1, 'name': CHOSEN}]
    recovery.select_microphone(app, 1, name=HEADSET)
    assert audio.events == ['terminate', 'initialize']
    assert app.config['microphone_name'] == HEADSET
    assert app._ace_engine.capture_name == HEADSET


def test_picker_before_watcher_keeps_saved_raw_choices_without_native_query(monkeypatch):
    app = SimpleNamespace(available_mics=[{'id': 4, 'name': HEADSET}],
                          config={'microphone': 4, 'microphone_name': HEADSET})
    monkeypatch.setattr(recovery, 'changes_for', Mock(side_effect=AssertionError('no observer')))
    assert recovery.display_microphones(app) is app.available_mics
    assert vars(app).get('_microphone_changes') is None
    assert app.config == {'microphone': 4, 'microphone_name': HEADSET}


def test_opening_tray_during_selected_disable_enable_burst_keeps_one_reset(sequence, qapp):
    from tests.test_mic_missing_at_start_993 import tray_menu

    audio, app, changes, now, endpoints, guard, lives = sequence
    app.switch_microphone_and_refresh = Mock()
    changes.prime()
    endpoints[0] -= {('chosen', CHOSEN)}
    changes.notify('state')
    advance(changes, now, 1)
    deadline = changes._input_not_before
    menu = tray_menu(app)
    menu.deleteLater()
    assert changes._input_not_before == deadline and changes._input_trigger == 'state'
    advance(changes, now, 2)
    assert not audio.events
    endpoints[0] |= {('chosen', CHOSEN)}
    changes.notify('state')
    advance(changes, now, 3)
    deadline = changes._input_not_before
    menu = tray_menu(app)
    menu.deleteLater()
    assert changes._input_not_before == deadline and changes._input_trigger == 'state'
    assert not audio.events
    advance(changes, now, 6)
    assert audio.events == ['terminate', 'initialize']
    assert {mic['name'] for mic in app.available_mics} == {CHOSEN, HEADSET}
    assert app.config['microphone_name'] == CHOSEN


def test_live_picker_keeps_raw_handsfree_preference_warning_and_reconnect(qapp, monkeypatch):
    from samsara.ui.settings import general_qt
    from samsara.ui.settings.general_qt import _MicrophoneComboBox, _populate_microphone_combo, _microphone_updates
    from samsara.ui import tray_microphones
    from tests.test_dell_mic_997b import HFP, GOOD, MICS
    from tests.test_mic_missing_at_start_993 import tray_menu

    app = SimpleNamespace(available_mics=list(MICS),
                          config={'microphone': 1, 'microphone_name': HFP},
                          _mic_refresh_blocked=lambda: True,
                          switch_microphone_and_refresh=Mock())
    endpoints = [frozenset({('hfp', HFP), ('good', GOOD)})]
    recovery.DeviceChanges(app, endpoints=lambda: endpoints[0])
    combo = _MicrophoneComboBox(app)
    monkeypatch.setattr(_MicrophoneComboBox.__bases__[0], 'showPopup', lambda self: None)
    warning = Mock()
    monkeypatch.setattr(general_qt.QMessageBox, 'information', warning)
    monkeypatch.setattr(tray_microphones, 'select_microphone', Mock())
    _populate_microphone_combo(combo, recovery.display_microphones(app), app.config)
    try:
        combo.showPopup()
        row = next(i for i in range(combo.count()) if combo.itemData(i) == 1)
        combo.setCurrentIndex(row)
        warning.assert_not_called()
        combo.activated.emit(row)
        warning.assert_called_once()
        assert _microphone_updates(combo) == {'microphone': 1, 'microphone_name': HFP}
        menu = tray_menu(app)
        try:
            action = next(a for a in menu.actions() if HFP in a.text())
            assert action.isEnabled()
            action.trigger()
            assert warning.call_count == 2  # One more warning for this deliberate tray pick.
        finally:
            menu.deleteLater()
        endpoints[0] = frozenset({('good', GOOD)})
        combo.showPopup()
        assert _microphone_updates(combo) == {}
        row = next(i for i in range(combo.count()) if HFP in combo.itemText(i))
        assert not combo.model().item(row).isEnabled()
        assert app.config == {'microphone': 1, 'microphone_name': HFP}
        endpoints[0] |= {('hfp', HFP)}
        combo.showPopup()
        assert _microphone_updates(combo) == {'microphone': 1, 'microphone_name': HFP}
        assert warning.call_count == 2
    finally:
        combo.deleteLater()
