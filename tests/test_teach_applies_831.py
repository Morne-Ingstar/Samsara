"""831: real local teaching, persistence, next dictations and Dictionary refresh."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava, dictionary as teaching
from samsara import ava_pending, plugin_commands, teach_patterns
from samsara.commands import CommandExecutor
from samsara.dictionary_store import DictionaryStore, get_dictionary_store


def make_store(tmp_path, corrections=None, vocabulary=None):
    (tmp_path / 'training_data.json').write_text(json.dumps({
        'vocabulary': vocabulary or [], 'corrections': corrections or {}}), encoding='utf-8')
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    return app, DictionaryStore(app)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    ava.clear_pending_action()
    teach_patterns.pop_last_action()
    app, store = make_store(tmp_path)
    app.dictionary_store = store
    # A stale legacy alias must never divert teaching from dictation's store.
    app.voice_training_window = Mock()
    app.history = [('now', 'Mourn.', False)]
    app._ava_cmd_generation = 0
    app.command_mode_active = False
    app.command_matching_enabled = True
    app.app_state = 'asleep'
    app.wake_word_triggered = False
    app.play_sound = Mock()
    app.listening_indicator = Mock()
    app._ensure_wake_consumer = Mock()
    app._release_wake_consumer = Mock()
    app._schedule_ui = lambda fn, *args: fn(*args)
    app._log_history = Mock()
    commands = tmp_path / 'commands.json'
    commands.write_text('{"commands": {}}', encoding='utf-8')
    monkeypatch.setattr(plugin_commands, '_REGISTRY',
                        dict(plugin_commands._MODULE_ENTRIES[teaching.__name__]))
    app.command_executor = CommandExecutor(commands, plugins_dir=tmp_path / 'no_plugins')
    spoken = []

    def speak(_app, text, **kwargs):
        spoken.append(text)
        if kwargs.get('on_done'):
            kwargs['on_done']()
        return True

    monkeypatch.setattr(ava, 'speak', speak)
    monkeypatch.setattr(ava, 'ask_model', lambda *a, **k: pytest.fail('local teaching called AI'))
    monkeypatch.setattr(ava, '_ai_state', lambda *a: pytest.fail('local teaching checked AI'))
    monkeypatch.setattr(ava_pending.thread_registry, 'timer',
                        lambda *a, **k: SimpleNamespace(cancel=lambda: None))
    yield SimpleNamespace(app=app, store=store, spoken=spoken, path=tmp_path)
    question = getattr(app, '_ava_question', None)
    if question is not None:
        ava_pending._clear(app, question)
    ava.clear_pending_action()
    teach_patterns.pop_last_action()


def teach_correction(rig):
    result = rig.app.command_executor.process_text(
        'correct that to Morne, spelled M O R N E', rig.app,
        force_commands=True, capture_path='command_key')
    assert result.claimed
    assert rig.store.corrections_dict == {}
    assert rig.app._ava_question is not None
    assert ava_pending.consume(rig.app, 'yes')


def test_real_confirm_then_next_transcriptions_keep_their_punctuation(rig):
    teach_correction(rig)
    assert get_dictionary_store(rig.app) is rig.store
    assert rig.store.corrections_dict == {'Mourn': 'Morne'}
    assert rig.store.custom_vocab == ['Morne']
    for raw, delivered in [('Mourn.', 'Morne.'), ('Mourn', 'Morne'),
                           ("I'm Mourn, hi", "I'm Morne, hi"),
                           ('My name is Mourn.', 'My name is Morne.')]:
        assert get_dictionary_store(rig.app).apply_corrections(raw) == delivered
    assert rig.spoken[-1] == "Added Morne. 'Mourn' will now come out as 'Morne'."
    assert rig.app._log_history.call_args.kwargs['display_text'] == 'Dictionary: saved'
    reloaded = DictionaryStore(rig.app)
    assert reloaded.custom_vocab == ['Morne']
    assert reloaded.apply_corrections('Mourn, hi.') == 'Morne, hi.'
    # Do not silently teach plausible, unobserved alternative spellings.
    assert reloaded.apply_corrections('Morn. Mourne.') == 'Morn. Mourne.'


@pytest.mark.parametrize('mode', ['correction', 'add word', 'spell word'])
def test_each_voice_path_refreshes_the_real_dictionary_panel(rig, qapp, mode):
    from samsara.ui.dictionary_panel_qt import DictionaryPanelQt
    panel = DictionaryPanelQt(rig.app)
    try:
        if mode == 'correction':
            teach_correction(rig)
        else:
            handler = teaching.add_word if mode == 'add word' else teaching.spell_word
            handler(rig.app, remainder='spelled M O R N E' if mode == 'add word' else 'M O R N E')
            assert ava_pending.consume(rig.app, 'yes')
        qapp.processEvents()
        assert rig.store.custom_vocab == ['Morne']
        assert [panel._vocab_list.item(i).text()
                for i in range(panel._vocab_list.count())] == ['Morne']
    finally:
        panel.close()


@pytest.mark.parametrize('wrong', ['Mourn.', '  “Mourn.”  ', 'Mourn,', '(Mourn)', 'Mourn!?'])
def test_store_normalizes_surrounding_punctuation_and_matches_word_boundaries(tmp_path, wrong):
    _, store = make_store(tmp_path)
    assert store.add_correction(wrong, 'Morne')
    assert store.corrections_dict == {'Mourn': 'Morne'}
    assert store.apply_corrections('Mourn. mourn, MOURN! Mournful unmourn') == (
        'Morne. Morne, MORNE! Mournful unmourn')


def test_legacy_migration_backs_up_exact_bytes_and_is_idempotent(tmp_path, caplog):
    original = b'{"vocabulary": ["Other"], "corrections": {"Mourn.": "Morne", "  wrng! ": "right", "teh": "the"}}\n'
    training = tmp_path / 'training_data.json'
    training.write_bytes(original)
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    with caplog.at_level('INFO', logger='samsara.dictionary_store'):
        store = DictionaryStore(app)
    backup = tmp_path / 'training_data.json.pre-831.bak'
    assert backup.read_bytes() == original
    assert store.corrections_dict == {'Mourn': 'Morne', 'wrng': 'right', 'teh': 'the'}
    assert 'Normalized 2 correction keys (831)' in caplog.text
    assert json.loads(training.read_text())['corrections'] == store.corrections_dict
    once = training.read_bytes()
    DictionaryStore(app)
    assert training.read_bytes() == once
    assert backup.read_bytes() == original
    assert store.apply_corrections('Mourn, wrng!') == 'Morne, right!'


@pytest.mark.parametrize('pairs', [
    [('Mourn.', 'old'), ('mourn', 'chosen')],
    [('mourn', 'chosen'), ('Mourn.', 'old')],
])
def test_migration_keeps_explicit_bare_key_on_collision(tmp_path, pairs):
    _, store = make_store(tmp_path, dict(pairs))
    assert store.corrections_dict == {'mourn': 'chosen'}
    assert store.apply_corrections('Mourn.') == 'Chosen.'


def test_backup_failure_prevents_disk_migration(tmp_path, monkeypatch):
    original = {'Mourn.': 'Morne'}
    make_store(tmp_path)
    training = tmp_path / 'training_data.json'
    training.write_text(json.dumps({'vocabulary': [], 'corrections': original}), encoding='utf-8')
    # A directory at the backup path cannot serve as a valid backup.
    (tmp_path / 'training_data.json.pre-831.bak').mkdir()
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json')
    store = DictionaryStore(app)
    assert json.loads(training.read_text())['corrections'] == original
    assert store.corrections_dict == original


def test_failed_voice_save_rolls_back_both_collections_and_pattern(rig, monkeypatch):
    before = (rig.path / 'training_data.json').read_bytes()
    monkeypatch.setattr(rig.store, 'save_training_data', lambda: False)
    teach_correction(rig)
    assert rig.store.custom_vocab == []
    assert rig.store.corrections_dict == {}
    assert rig.store.apply_corrections('Mourn.') == 'Mourn.'
    assert (rig.path / 'training_data.json').read_bytes() == before
    assert rig.spoken[-1] == 'Could not save that correction.'
    assert rig.app._log_history.call_args.kwargs['display_text'] == 'Dictionary: failed to save'


def test_correction_vocab_is_deduplicated_and_saved_together(rig, monkeypatch):
    assert rig.store.add_vocab_word('morne')
    save = Mock(wraps=rig.store.save_training_data)
    monkeypatch.setattr(rig.store, 'save_training_data', save)
    teach_correction(rig)
    save.assert_called_once()
    assert rig.store.custom_vocab == ['morne']
    assert rig.spoken[-1] == "Morne is already in your dictionary. 'Mourn' will now come out as 'Morne'."
    assert 'morne' in rig.store.get_initial_prompt()
    assert rig.store.get_initial_prompt(include_vocabulary=False) is None


def test_draft_learning_and_direct_rebuild_share_normalized_matching(tmp_path):
    _, store = make_store(tmp_path)
    assert store.learn_draft_correction('Mourn.', 'Morne')
    assert store.corrections_dict == {'Mourn': 'Morne'}
    assert store.apply_corrections('Mourn, hi.') == 'Morne, hi.'
    store.corrections_dict['wrng.'] = 'right'
    store._rebuild_corrections_pattern()
    assert store.apply_corrections('wrng, hi.') == 'right, hi.'
    assert not store.learn_draft_correction('Morne.', 'Morne')


def test_interior_punctuation_longest_match_single_pass_and_boundaryless_script(tmp_path):
    _, store = make_store(tmp_path)
    for wrong, right in [("can't", 'cannot'), ('going', 'moving'),
                          ('going to', 'gonna'), ('gonna', 'will'), ('字', '文')]:
        assert store.add_correction(wrong, right)
    assert store.apply_corrections("can't, going to. going! 汉字句") == 'cannot, gonna. moving! 汉文句'


def test_normalized_duplicate_replaces_old_key_without_chained_match(tmp_path):
    _, store = make_store(tmp_path, {'Mourn': 'Other'})
    assert store.add_correction('  mourn. ', 'Morne', vocabulary=True)
    assert store.corrections_dict == {'mourn': 'Morne'}
    assert store.apply_corrections('Mourn.') == 'Morne.'


@pytest.mark.parametrize('wrong', ['', '...', '  “ ”  '])
def test_empty_normalized_correction_does_not_add_vocabulary(tmp_path, wrong):
    _, store = make_store(tmp_path)
    assert not store.add_correction(wrong, 'Morne', vocabulary=True)
    assert store.custom_vocab == []
    assert store.corrections_dict == {}
