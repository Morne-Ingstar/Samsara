"""Private kept-take evidence with synthetic PCM; never boot the desktop app."""
import ast
import json
import logging
from pathlib import Path
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock
import wave
import zipfile

import numpy as np
import pytest

from samsara import kept_dictations as kept
from samsara.session_modes import SessionMode, SessionModeManager, UtteranceSignals


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path / 'profile'))
    app = NS(config={'kept_dictations': {'enabled': False, 'maximum': 50},
                     'model_size': 'base', 'device': 'cpu'}, model_rate=16000)
    yield app
    store = getattr(app, '_kept_dictations', None)
    if store and store.worker:
        store.worker.shutdown(wait=True)


def pcm(size=1600):
    return np.linspace(-.2, .2, size, dtype=np.float32)


def take(app, raw='Synthetic raw', final='Synthetic typed', **kw):
    return kept.capture(app, pcm(), raw, final, 'hold', **kw)


def rows(app):
    store = kept.store_for(app)
    store.flush()
    return store.list()


def test_defaults_are_off_and_unassigned():
    from samsara.config_schema import SETTINGS_SCHEMA
    assert SETTINGS_SCHEMA['kept_dictations.enabled']['default'] is False
    assert SETTINGS_SCHEMA['kept_dictations.maximum']['default'] == 50
    assert SETTINGS_SCHEMA['keep_dictation_hotkey']['default'] == ''


def test_off_keeps_ram_only_and_does_not_create_directory(owner):
    take(owner)
    assert not kept.store_for(owner).folder.exists()
    assert kept.store_for(owner).last['metadata']['raw_transcript'] == 'Synthetic raw'


def test_keep_last_while_off_writes_exact_pcm_and_opaque_names(owner):
    take_id = take(owner)
    kept.request_keep(owner, voice=True)
    row, = rows(owner)
    assert row['id'] == take_id and len(take_id) == 32
    assert row['raw_transcript'] == 'Synthetic raw'
    assert row['final_text'] == 'Synthetic typed'
    assert (row['path'], row['model'], row['device']) == ('hold', 'base', 'cpu')
    assert row['duration_s'] == .1
    store = kept.store_for(owner)
    assert {p.name for p in store.folder.iterdir()} == {take_id + '.wav', take_id + '.json'}
    with wave.open(str(store.audio_path(take_id)), 'rb') as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getnframes()) == (16000, 1, 1600)
        assert wav.readframes(1600) == kept._to_int16_pcm(pcm()).tobytes()


def test_hotkey_before_dictation_arms_next_even_with_a_previous_take(owner):
    old = take(owner)
    kept.request_keep(owner)
    new = take(owner)
    assert {row['id'] for row in rows(owner)} == {old, new}
    assert not kept.store_for(owner).armed


def test_hotkey_during_recording_keeps_current_instead_of_previous(owner):
    take(owner)
    owner.recording = True
    kept.request_keep(owner)
    current = take(owner)
    assert [r['id'] for r in rows(owner)] == [current]


def test_voice_command_does_not_arm_an_unrelated_next_take(owner):
    first = take(owner)
    kept.request_keep(owner, voice=True)
    take(owner)
    assert [r['id'] for r in rows(owner)] == [first]


def test_expired_last_arms_next(owner):
    store = kept.store_for(owner)
    store.clock = lambda: 10
    take(owner)
    store.clock = lambda: 10 + kept.LAST_TTL_S + 1
    assert 'next' in store.keep()
    assert store.last is None and store.armed


def test_auto_retention_is_newest_n_and_removes_both_files(owner):
    owner.config['kept_dictations'].update(enabled=True, maximum=2)
    ids = [take(owner) for _ in range(4)]
    assert {row['id'] for row in rows(owner)} == set(ids[-2:])
    assert len(list(kept.store_for(owner).folder.iterdir())) == 4


def test_repeated_keep_is_idempotent(owner):
    take(owner)
    for _ in range(3):
        kept.request_keep(owner, voice=True)
    assert len(rows(owner)) == 1


def test_snapshot_does_not_follow_mutated_pcm(owner):
    owner.config['kept_dictations']['enabled'] = True
    audio = pcm()
    kept.capture(owner, audio, 'raw', 'final', 'hold')
    audio[:] = 0
    row, = rows(owner)
    with wave.open(str(kept.store_for(owner).audio_path(row['id'])), 'rb') as wav:
        assert wav.readframes(1600) == kept._to_int16_pcm(pcm()).tobytes()


def test_delete_one_and_all_clear_ram_and_preserve_unrelated_files(owner):
    owner.config['kept_dictations']['enabled'] = True
    ids = [take(owner) for _ in range(2)]
    rows(owner)
    store = kept.store_for(owner)
    unrelated = store.folder / 'unrelated.txt'
    unrelated.write_text('unrelated', encoding='utf-8')
    store.delete(ids[0])
    assert [r['id'] for r in store.list()] == [ids[1]]
    store.clear()
    assert store.list() == [] and store.last is None
    assert unrelated.exists()


@pytest.mark.parametrize('operation', ['clear', 'delete', 'disable'])
def test_queued_write_cannot_restore_deleted_take(owner, operation):
    owner.config['kept_dictations']['enabled'] = True
    store = kept.store_for(owner)
    entered, release = threading.Event(), threading.Event()
    from concurrent.futures import ThreadPoolExecutor
    store.worker = ThreadPoolExecutor(max_workers=1)
    def block():
        entered.set()
        assert release.wait(5)
    store.worker.submit(block)
    assert entered.wait(5)
    try:
        take_id = take(owner)
        if operation == 'clear':
            store.clear()
        elif operation == 'delete':
            store.delete(take_id)
        else:
            owner.config['kept_dictations']['enabled'] = False
    finally:
        release.set()
    assert rows(owner) == []


def test_export_is_explicit_copies_audio_and_text_and_never_overwrites(owner, tmp_path):
    take_id = take(owner)
    kept.request_keep(owner, voice=True)
    rows(owner)
    store = kept.store_for(owner)
    target = store.export(take_id, tmp_path / 'exports')
    again = store.export(take_id, tmp_path / 'exports')
    assert target != again
    assert (target / (take_id + '.wav')).read_bytes() == store.audio_path(take_id).read_bytes()
    assert json.loads((target / (take_id + '.json')).read_text())['final_text'] == 'Synthetic typed'


@pytest.mark.parametrize('take_id', ['../escape', '/absolute', 'not-an-id'])
def test_invalid_ids_cannot_escape_private_folder(owner, take_id, tmp_path):
    with pytest.raises(ValueError):
        kept.store_for(owner).export(take_id, tmp_path)


def test_failed_write_never_logs_content_or_exception_details(owner, monkeypatch, caplog):
    owner.config['kept_dictations']['enabled'] = True
    def fail(*a, **kw):
        raise OSError('SYNTHETIC_PRIVATE_FAILURE')
    monkeypatch.setattr(kept.wave, 'open', fail)
    with caplog.at_level(logging.DEBUG):
        take(owner, raw='SYNTHETIC_PRIVATE_RAW', final='SYNTHETIC_PRIVATE_TYPED')
        assert rows(owner) == []
    assert '[KEPT] operation_failed=1' in caplog.text
    assert 'SYNTHETIC_PRIVATE' not in caplog.text
    assert not list(kept.store_for(owner).folder.glob('*'))


def test_retained_failed_hold_uses_original_audio_and_clear_barrier(owner):
    from samsara.hold_recording_recovery import begin_incident, prepare_final
    owner.config['kept_dictations']['enabled'] = True
    session = NS(app=owner, _decode_audio=pcm(), _hold_final_audit=True)
    begin_incident(session)
    prepare_final(session, '', '', .1)
    row, = rows(owner)
    assert row['status'] == 'audio retained' and row['final_text'] == ''
    new = NS(app=owner, _decode_audio=pcm(), _hold_final_audit=True)
    begin_incident(new)
    kept.store_for(owner).clear()
    prepare_final(new, 'synthetic final', 'synthetic raw', .1)
    assert rows(owner) == []


def manager_for(owner, *, staged=True):
    class App:
        def redecode(self, text, refs):
            return text
    app = App()
    app.__dict__.update(owner.__dict__)
    manager = SessionModeManager(abort_phrases=[], foreground_exe_resolver=lambda: 'editor.exe',
        foreground_hwnd_resolver=lambda: 123, inject_fn=lambda text, guard=None: text,
        remove_chars_fn=lambda n: True, command_dispatch_fn=lambda text: None,
        agent_dispatch_fn=lambda text: None, commit_redecode_fn=app.redecode,
        buffer_dictate_until_commit=staged)
    manager.reset(initial_mode=SessionMode.DICTATE)
    manager._dictate_target_process = 'editor.exe'
    manager._dictate_target_hwnd = 123
    return app, manager


def test_real_handsfree_staging_then_commit_updates_one_kept_take(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    result = manager.dispatch_utterance('Synthetic sentence.', UtteranceSignals(True, audio_ref=pcm()))
    assert result.kind == 'dictate_staged'
    staged, = rows(app)
    assert staged['final_text'] == '' and staged['status'] == 'staged'
    result = manager.dispatch_utterance('send', UtteranceSignals(True, (1.0,)))
    assert result.kind == 'dictate_committed'
    row, = rows(app)
    assert row['id'] == staged['id']
    assert row['final_text'] == result.detail['text']
    assert row['raw_transcript'] == 'Synthetic sentence.'


def test_real_session_keep_phrase_saves_last_and_does_not_stage_command(owner):
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    take(app)
    result = manager.dispatch_utterance('keep that', UtteranceSignals(True, audio_ref=pcm(800)))
    assert result.kind == 'dictation_kept'
    assert not manager.dictate_pending_buffer
    row, = rows(app)
    assert row['duration_s'] == .1  # original take, not the keep command


def test_immediate_handsfree_saves_actual_formatted_bytes(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner, staged=False)
    owner._kept_dictations = kept.store_for(app)
    result = manager.dispatch_utterance('Synthetic sentence.', UtteranceSignals(True, audio_ref=pcm()))
    assert result.kind == 'dictate_injected'
    row, = rows(app)
    assert row['final_text'] == result.detail['text']


def test_clear_while_staged_prevents_commit_resurrecting_old_take(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    manager.dispatch_utterance('Synthetic sentence.', UtteranceSignals(True, audio_ref=pcm()))
    rows(app)
    kept.store_for(app).clear()
    manager.dispatch_utterance('send', UtteranceSignals(True, (1.0,)))
    assert rows(app) == []


def test_wake_context_joins_audio_chunks_and_resets(owner):
    owner.config['kept_dictations']['enabled'] = True
    owner.capture_rate = 16000
    owner.wake_dictation_buffer = []
    @kept.wake_audio
    def decode(app, buffer, src_rate=None, *, finish=False):
        if finish:
            kept.wake_history(app, 'Synthetic raw', 'Synthetic final', 'wake', 'dictation')
            app.wake_dictation_buffer.clear()
        else:
            app.wake_dictation_buffer.append('synthetic')
    decode(owner, [pcm(800)])
    decode(owner, [pcm(1600)], finish=True)
    row, = rows(owner)
    assert row['duration_s'] == .15 and row['path'] == 'wake'
    assert owner._kept_wake_parts == []


def test_whole_voice_phrases_leave_prose_alone():
    from plugins.commands.kept_dictations import keep_dictation
    assert kept.is_keep('Save that dictation!')
    assert not kept.is_keep('Please keep that sentence in my letter.')
    assert keep_dictation(NS(), remainder='sentence') is False


def test_support_bundle_does_not_include_kept_folder(owner, tmp_path, monkeypatch):
    from samsara import support_bundle
    take(owner)
    kept.request_keep(owner, voice=True)
    rows(owner)
    monkeypatch.setattr(support_bundle, '_system_text', lambda: 'Synthetic system')
    bundle = support_bundle.build_bundle(home_dir=kept.store_for(owner).folder.parent,
                                         desktop_dir=tmp_path / 'bundle')
    with zipfile.ZipFile(bundle) as archive:
        assert not any('kept' in name or name.endswith('.wav') for name in archive.namelist())
        assert b'Synthetic raw' not in b''.join(archive.read(n) for n in archive.namelist())


def test_settings_real_host_saves_switch_limit_and_unassigned_hotkey(qapp, owner):
    from samsara.ui.settings_qt import _SettingsWindow
    from tests.test_settings_pages import _StubApp, _schema_defaults
    app = _StubApp(_schema_defaults())
    window = _SettingsWindow(app)
    try:
        enabled = window._widgets['keep_dictations']
        maximum = window._widgets['kept_dictations_maximum']
        hotkey = window._widgets['keep_dictation_hotkey']
        assert not enabled.isChecked() and maximum.value() == 50 and not hotkey.combo
        enabled.setChecked(True)
        maximum.setValue(7)
        hotkey._combo = 'ctrl+alt+k'
        updates = window._collect_destination_updates({})
        assert updates['kept_dictations'] == {'enabled': True, 'maximum': 7}
        assert updates['keep_dictation_hotkey'] == 'ctrl+alt+k'
    finally:
        window.deleteLater()


def test_kept_view_is_plain_text_and_delete_all_is_one_action(qapp, owner):
    from samsara.ui.kept_dictations_qt import KeptView
    take(owner, raw='<b>Synthetic raw</b>', final='Synthetic typed')
    kept.request_keep(owner, voice=True)
    rows(owner)
    view = KeptView(kept.store_for(owner))
    try:
        assert view.entries.count() == 1
        assert view.raw.toPlainText() == '<b>Synthetic raw</b>'
        assert view.typed.toPlainText() == 'Synthetic typed'
        view.clear_button.click()
        assert view.entries.count() == 0
        assert not view.raw.toPlainText()
    finally:
        view.deleteLater()


def test_standalone_history_kept_button_opens_real_reviewer(qapp, owner):
    from samsara.ui.history_qt import _HistoryWindow
    host = _HistoryWindow(owner)
    try:
        host._view.kept_button.click()
        assert host._view.kept_panel.entries.count() == 0
    finally:
        host.deleteLater()


def test_direct_commit_hotkey_updates_kept_staged_take(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    manager.dispatch_utterance('Synthetic sentence.', UtteranceSignals(True, audio_ref=pcm()))
    first, = rows(app)
    result = manager._commit_dictate_buffer(target_mode=None)
    row, = rows(app)
    assert result.kind == 'dictate_committed'
    assert row['id'] == first['id'] and row['final_text'] == result.detail['text']


def test_paste_receipt_updates_actual_typed_text_and_failure(owner):
    from samsara.hold_diagnostics import receipt
    owner.config['kept_dictations']['enabled'] = True
    session = NS(app=owner, _decode_audio=pcm())
    kept.capture_held(session, 'proposed', 'raw')
    receipt(session, 'actual synthetic bytes', True, True)
    row, = rows(owner)
    assert row['final_text'] == 'actual synthetic bytes' and row['status'] == 'typed'
    receipt(session, 'actual synthetic bytes', False, False)
    row, = rows(owner)
    assert row['final_text'] == '' and row['status'] == 'not typed'


def test_real_stream_delivery_does_not_type_keep_command(owner, monkeypatch, tmp_path):
    from tests.test_benchmark_capture_1106 import capture as _capture_fixture
    # Reuse the existing source-method harness without invoking its fixture.
    session, jobs, deliver = _capture_fixture.__wrapped__(tmp_path, monkeypatch)
    session.app.config['kept_dictations'] = {'enabled': False}
    session.app._kept_dictations = kept.store_for(owner)
    take(owner)
    deliver(text='keep that', raw='keep that')
    row, = rows(owner)
    assert row['raw_transcript'] == 'Synthetic raw'
    assert not jobs


def test_kept_view_play_uses_selected_opaque_wav(qapp, owner):
    from samsara.ui.kept_dictations_qt import KeptView
    take_id = take(owner)
    kept.request_keep(owner, voice=True)
    rows(owner)
    view = KeptView(kept.store_for(owner))
    try:
        view.player = Mock()
        view.play.click()
        url = view.player.setSource.call_args.args[0]
        assert Path(url.toLocalFile()) == kept.store_for(owner).audio_path(take_id)
        view.player.play.assert_called_once()
    finally:
        view.deleteLater()


def test_clear_removes_corrupt_and_orphan_private_files(owner):
    store = kept.store_for(owner)
    store.folder.mkdir(parents=True)
    (store.folder / ('a' * 32 + '.wav')).write_bytes(b'orphan')
    (store.folder / ('b' * 32 + '.json')).write_text('{', encoding='utf-8')
    store.clear()
    assert list(store.folder.iterdir()) == []


def test_delayed_wake_output_keeps_audio_and_preprocessing_transcript(owner):
    owner.config['kept_dictations']['enabled'] = True
    owner._kept_wake_parts = [([pcm()], 16000)]
    owner._kept_wake_generation = kept.store_for(owner).generation
    @kept.wake_output
    def output(app, text):
        kept.wake_history(app, 'intermediate', 'Synthetic final', 'wake', 'dictation')
        return True
    assert output(owner, 'Synthetic original') is True
    row, = rows(owner)
    assert row['raw_transcript'] == 'Synthetic original'
    assert row['final_text'] == 'Synthetic final'
    assert owner._kept_wake_parts == []


def test_runtime_wake_observation_restores_context_after_decode_error(owner):
    owner.wake_dictation_buffer = ['Synthetic pending']
    token = kept.begin_wake(owner, [pcm()], 16000)
    try:
        with pytest.raises(RuntimeError):
            raise RuntimeError('Synthetic decoder failure')
    finally:
        kept.end_wake(owner, token)
    assert kept._wake.get() is None
    assert len(owner._kept_wake_parts) == 1
    assert not kept.store_for(owner).folder.exists()


def test_runtime_wake_observation_keeps_raw_and_typed_without_decorators(owner):
    owner.config['kept_dictations']['enabled'] = True
    token = kept.begin_wake(owner, [pcm()], 16000)
    try:
        kept.note_wake_output(owner, 'Synthetic original')
        kept.wake_history(owner, 'intermediate', 'Synthetic final', 'wake', 'dictation')
    finally:
        kept.end_wake(owner, token)
    row, = rows(owner)
    assert (row['raw_transcript'], row['final_text']) == ('Synthetic original', 'Synthetic final')
    assert kept._wake.get() is None and owner._kept_wake_parts == []


def test_runtime_delayed_wake_output_consumes_audio_once(owner):
    owner.config['kept_dictations']['enabled'] = True
    owner.wake_dictation_buffer = ['Synthetic pending']
    token = kept.begin_wake(owner, [pcm()], 16000)
    kept.end_wake(owner, token)
    kept.note_wake_output(owner, 'Synthetic original')
    kept.wake_history(owner, 'intermediate', 'Synthetic final', 'wake', 'dictation')
    kept.wake_history(owner, 'unrelated', 'unrelated', 'wake', 'dictation')
    row, = rows(owner)
    assert (row['raw_transcript'], row['final_text']) == ('Synthetic original', 'Synthetic final')
    assert owner._kept_wake_parts == []
    assert '_kept_wake_raw' not in owner.__dict__


def test_new_session_does_not_reuse_previous_staged_take(owner):
    owner.config['kept_dictations']['enabled'] = True
    app, manager = manager_for(owner)
    owner._kept_dictations = kept.store_for(app)
    manager.dispatch_utterance('Synthetic first.', UtteranceSignals(True, audio_ref=pcm()))
    manager.reset(initial_mode=SessionMode.DICTATE)
    manager.dispatch_utterance('Synthetic second.', UtteranceSignals(True, audio_ref=pcm()))
    assert {r['raw_transcript'] for r in rows(app)} == {'Synthetic first.', 'Synthetic second.'}


def test_real_hotkey_handler_keeps_and_ignores_key_repeat(owner, monkeypatch):
    from samsara.hotkeys import HotkeyCluster
    from samsara import speech_readiness
    monkeypatch.setattr(speech_readiness, 'show_for_keyboard_hotkey', lambda app: None)
    owner.config.update(hotkey='ctrl+shift', keep_dictation_hotkey='ctrl+alt+k')
    owner.current_keys = set()
    owner.key_press_times = {}
    owner.snoozed = False
    owner._check_command_mode_key = lambda *a, **kw: None
    owner.get_key_name = lambda key: key
    owner.check_hotkey_state = lambda combo: combo == 'ctrl+alt+k'
    HotkeyCluster.on_key_press.__wrapped__(owner, 'k')
    store = kept.store_for(owner)
    assert store.armed
    store.armed = False
    HotkeyCluster.on_key_press.__wrapped__(owner, 'k')
    assert not store.armed
