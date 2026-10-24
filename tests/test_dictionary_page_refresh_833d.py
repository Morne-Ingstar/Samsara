"""833d: both dictionary lists follow store changes, including worker saves."""

import gc
import json
import threading
import weakref
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel, QLineEdit

from samsara.dictionary_store import DictionaryStore
from samsara.ui import dictionary_panel_qt as ui


@pytest.fixture
def page(tmp_path, monkeypatch, qapp):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    monkeypatch.setattr(ui, 'QSettings', lambda *args: QSettings(
        str(tmp_path / 'ui.ini'), QSettings.Format.IniFormat))
    monkeypatch.setattr(ui.thread_registry, 'spawn', lambda _name, fn, **kw: fn())
    (tmp_path / 'training_data.json').write_text(json.dumps({
        'vocabulary': ['Other'], 'corrections': {'old': 'original'}}), encoding='utf-8')
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    app.dictionary_store = DictionaryStore(app)
    panel = ui.DictionaryPanelQt(app)
    panel.resize(760, 640)
    panel.show()
    qapp.processEvents()
    yield panel, app.dictionary_store
    import shiboken6
    if shiboken6.isValid(panel):
        panel.close()
        shiboken6.delete(panel)
    qapp.processEvents()


def vocabulary(panel):
    return [panel._vocab_list.item(i).text() for i in range(panel._vocab_list.count())]


def corrections(panel):
    table = panel._corrections_table
    return {table.item(row, 0).text(): table.item(row, 1).text()
            for row in range(table.rowCount())}


def teach(store):
    # Same atomic store method used by voice correction confirmation (831).
    assert store.add_correction('Mourn.', 'Morne', vocabulary=True)


def test_worker_teaching_updates_both_lists_on_qt_thread(page, qapp, monkeypatch):
    panel, store = page
    main_thread = threading.get_ident()
    reload_threads = []
    original = panel._kv_load

    def record(*args):
        reload_threads.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(panel, '_kv_load', record)
    results = []
    worker = threading.Thread(target=lambda: results.append(
        store.add_correction('Mourn.', 'Morne', vocabulary=True)))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive() and results == [True]
    # A worker never edits Qt widgets itself.
    assert reload_threads == []
    qapp.processEvents()
    assert vocabulary(panel) == ['Other', 'Morne']
    assert corrections(panel) == {'old': 'original', 'Mourn': 'Morne'}
    assert reload_threads == [main_thread]
    panel._tabs.setCurrentIndex(1)
    assert corrections(panel)['Mourn'] == 'Morne'


def test_same_page_reopens_and_continues_receiving_changes(page, qapp):
    panel, store = page
    panel.close()
    teach(store)
    qapp.processEvents()  # Notification while closed may be ignored.
    panel.show()
    qapp.processEvents()
    assert panel._alive
    assert 'Morne' in vocabulary(panel)
    assert corrections(panel)['Mourn'] == 'Morne'
    assert store.add_correction('next', 'NextName', vocabulary=True)
    qapp.processEvents()
    assert 'NextName' in vocabulary(panel)
    assert corrections(panel)['next'] == 'NextName'


def test_show_refreshes_both_lists_even_without_a_notification(page, qapp):
    panel, store = page
    panel.hide()
    # Simulate an already-updated service without delivering its change hook.
    store.custom_vocab.append('Morne')
    store.corrections_dict['Mourn'] = 'Morne'
    panel.show()
    qapp.processEvents()
    assert 'Morne' in vocabulary(panel)
    assert corrections(panel)['Mourn'] == 'Morne'


def test_destroyed_page_disconnects_and_new_page_reads_current_store(page, qapp):
    import shiboken6
    panel, store = page
    app = panel._app
    shiboken6.delete(panel)
    assert store._change_listeners == []
    teach(store)
    reopened = ui.DictionaryPanelQt(app)
    try:
        reopened.show()
        qapp.processEvents()
        assert 'Morne' in vocabulary(reopened)
        assert corrections(reopened)['Mourn'] == 'Morne'
    finally:
        shiboken6.delete(reopened)


def test_stale_vocabulary_removal_preserves_voice_taught_entry(page, qapp):
    panel, store = page
    teach(store)
    # Deliberately act before the queued notification refreshes the old rows.
    assert vocabulary(panel) == ['Other']
    panel._vocab_list.item(0).setSelected(True)
    panel._vocab_remove()
    qapp.processEvents()
    assert vocabulary(panel) == store.custom_vocab == ['Morne']
    assert corrections(panel)['Mourn'] == 'Morne'
    saved = json.loads((store.app.config_path.parent / 'training_data.json').read_text())
    assert saved['vocabulary'] == ['Morne']
    assert saved['corrections']['Mourn'] == 'Morne'


@pytest.mark.parametrize('operation', ['add', 'remove'])
def test_unrelated_correction_edit_preserves_voice_teaching(page, qapp, operation):
    panel, store = page
    teach(store)
    assert corrections(panel) == {'old': 'original'}
    table = panel._corrections_table
    if operation == 'add':
        wrong, right = QLineEdit(), QLineEdit()
        wrong.setText('old')
        right.setText('edited')
        panel._kv_add(table, wrong, right, 'corrections', QLabel())
    else:
        table.selectRow(0)
        panel._kv_remove(table, 'corrections', QLabel())
    qapp.processEvents()
    assert 'Morne' in vocabulary(panel)
    assert corrections(panel)['Mourn'] == 'Morne'
    reloaded = DictionaryStore(store.app)
    assert reloaded.custom_vocab == ['Other', 'Morne']
    assert reloaded.corrections_dict['Mourn'] == 'Morne'
    assert reloaded.corrections_dict.get('old') == ('edited' if operation == 'add' else None)


def test_vocabulary_import_merges_with_voice_teaching(page, qapp, tmp_path, monkeypatch):
    panel, store = page
    teach(store)
    path = tmp_path / 'vocab.json'
    path.write_text('{"vocabulary": ["Imported"]}', encoding='utf-8')
    monkeypatch.setattr(ui.QFileDialog, 'getOpenFileName', lambda *a: (str(path), ''))
    panel._vocab_import()
    qapp.processEvents()
    assert vocabulary(panel) == store.custom_vocab == ['Other', 'Morne', 'Imported']
    assert DictionaryStore(store.app).custom_vocab == store.custom_vocab


def test_notifications_cover_add_remove_and_legacy_direct_saves(page, qapp):
    panel, store = page
    snapshots = []
    disconnect = store.add_change_listener(lambda: snapshots.append(json.loads(
        (store.app.config_path.parent / 'training_data.json').read_text())))
    assert store.add_vocab_word('Added')
    assert store.remove_vocab_word('Other')
    assert store.add_correction('new', 'correct')
    assert store.remove_correction('old')
    store.custom_vocab.append('Legacy')
    assert store.save_training_data()
    assert len(snapshots) == 5
    assert snapshots[0]['vocabulary'] == ['Other', 'Added']
    assert snapshots[-1] == {'vocabulary': ['Added', 'Legacy'], 'corrections': {'new': 'correct'}}
    qapp.processEvents()
    assert vocabulary(panel) == ['Added', 'Legacy']
    assert corrections(panel) == {'new': 'correct'}
    disconnect()
    assert store.add_vocab_word('Disconnected')
    assert len(snapshots) == 5


def test_failed_save_does_not_publish_a_change(page, monkeypatch):
    from pathlib import Path
    _, store = page
    calls = []
    store.add_change_listener(lambda: calls.append(True))
    monkeypatch.setattr(Path, 'write_bytes', lambda *a: (_ for _ in ()).throw(OSError('refused')))
    assert not store.add_correction('Mourn', 'Morne', vocabulary=True)
    assert calls == []
    assert store.custom_vocab == ['Other'] and store.corrections_dict == {'old': 'original'}


def test_refresh_preserves_selected_entry_identities(page, qapp):
    panel, store = page
    panel._vocab_list.item(0).setSelected(True)
    panel._corrections_table.selectRow(0)
    teach(store)
    qapp.processEvents()
    assert [item.text() for item in panel._vocab_list.selectedItems()] == ['Other']
    table = panel._corrections_table
    assert [table.item(i.row(), 0).text()
            for i in table.selectionModel().selectedRows()] == ['old']


def test_observer_failure_does_not_break_save_and_bound_observers_are_weak(page):
    _, store = page
    calls = []

    def broken():
        raise RuntimeError('observer failure')

    class Observer:
        def changed(self):
            calls.append(True)

    observer = Observer()
    ref = weakref.ref(observer)
    store.add_change_listener(broken)
    store.add_change_listener(observer.changed)
    assert store.add_vocab_word('First')
    assert calls == [True]
    del observer
    gc.collect()
    assert ref() is None
    assert store.add_vocab_word('Second')
    assert calls == [True]
