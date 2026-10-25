"""818: real local dispatch/store/History with fake speech and microphone owners."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from plugins.commands import ask_ollama as ava, dictionary as teaching
from samsara import ava_pending, command_catalog, plugin_commands, teach_patterns
from samsara.commands import CommandExecutor
from samsara.dictionary_store import DictionaryStore
from samsara.history import HistoryManager
from samsara.letter_spelling import parse_letters
from samsara.ui.dictionary_panel_qt import DictionaryPanelQt


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(tmp_path))
    ava.clear_pending_action()
    teach_patterns.pop_last_action()
    (tmp_path / 'training_data.json').write_text(
        json.dumps({'vocabulary': [], 'corrections': {}}), encoding='utf-8')
    commands_path = tmp_path / 'commands.json'
    commands_path.write_text(json.dumps({'commands': {}}), encoding='utf-8')
    entries = plugin_commands._MODULE_ENTRIES[teaching.__name__]
    monkeypatch.setattr(plugin_commands, '_REGISTRY', dict(entries))
    executor = CommandExecutor(commands_path, plugins_dir=tmp_path / 'no_plugins')
    spoken, callbacks, timers = [], [], []
    now = [100.0]
    app = SimpleNamespace(config={}, config_path=tmp_path / 'config.json',
        history=[], command_executor=executor, command_mode_active=False,
        command_matching_enabled=True, app_state='asleep', wake_word_triggered=False,
        _ava_cmd_generation=0, play_sound=Mock(), listening_indicator=Mock(),
        _ensure_wake_consumer=Mock(), _release_wake_consumer=Mock(),
        _schedule_ui=lambda fn, *args: fn(*args))
    app.dictionary_store = DictionaryStore(app)
    db = HistoryManager(tmp_path / 'history.db')
    app._log_history = lambda **kw: db.add(**kw)

    def speak(_app, text, **kw):
        spoken.append(text)
        if kw.get('on_done'):
            callbacks.append(kw['on_done'])
            if not getattr(app, 'defer_speech', False):
                kw['on_done']()
        return True

    monkeypatch.setattr(ava, 'speak', speak)
    monkeypatch.setattr(ava, 'ask_model', lambda *a, **kw: pytest.fail('local teaching reached a provider'))
    monkeypatch.setattr(ava, '_ai_state', lambda *a: pytest.fail('local teaching reached AI guard'))
    monkeypatch.setattr(teach_patterns, 'is_known_dictionary_word', lambda w: w.lower() in {'hello', 'atlas'})
    monkeypatch.setattr(ava_pending, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    def timer(_name, delay, fn, **kw):
        item = SimpleNamespace(cancel=Mock(), fire=fn)
        timers.append(item)
        return item

    monkeypatch.setattr(ava_pending.thread_registry, 'timer', timer)
    yield SimpleNamespace(app=app, store=app.dictionary_store, db=db, spoken=spoken,
                          callbacks=callbacks, now=now, executor=executor, timers=timers)
    q = getattr(app, '_ava_question', None)
    if q is not None:
        ava_pending._clear(app, q)
    ava.clear_pending_action()
    db._conn.close()


@pytest.mark.parametrize('letters', ['M O R N E', 'M-O-R-N-E', 'M.O.R.N.E.',
    'M. O. R. N. E.', 'M,O,R,N,E', 'em oh are en ee', 'Mike Oscar Romeo November Echo'])
def test_letter_shapes_share_the_existing_parser(letters):
    assert parse_letters(letters) == 'Morne'


def test_lowercase_and_shape_only_logging(caplog):
    assert parse_letters('lowercase M-O-R-N-E') == 'morne'
    with caplog.at_level('INFO', logger='samsara.letter_spelling'):
        assert parse_letters('M private-token E') is None
    assert 'tokens=3 failed_index=1' in caplog.text
    assert 'private-token' not in caplog.text


def test_nato_xray_name_is_not_split_as_joined_letters():
    assert parse_letters('X-ray Alpha Yankee') == 'Xay'


@pytest.mark.parametrize('phrase', [
    'add the word Morne to my vocabulary spelled M O R N E',
    'add Morne to the dictionary spelled M-O-R-N-E',
    'learn the word Morne spelled M.O.R.N.E.',
    'add word spelled M O R N E', 'spell word Mike Oscar Romeo November Echo',
    'teach word Morne spelled M O R N E',
])
@pytest.mark.parametrize('lane', ['command_key', 'handsfree', 'ava'])
def test_spelled_teaching_routes_and_yes_saves_without_ai(rig, phrase, lane):
    if lane == 'ava':
        ava.handle_ask_ava(rig.app, remainder=phrase)
    else:
        result = rig.executor.process_text(phrase, rig.app, force_commands=True, capture_path=lane)
        assert result.claimed and result.state.value == 'completed'
    assert rig.store.custom_vocab == []
    assert rig.app._ava_question.opened
    assert rig.app._ava_question.deadline == 110.0  # 830: ten seconds after readback TTS.
    rig.app._ensure_wake_consumer.assert_called_with('ava_question')
    # 846: keep the spelling readback alongside the answer instruction.
    assert rig.app.listening_indicator.show_outcome.call_args.args[0] == rig.spoken[0] + ' Say yes or no'
    ava.handle_ask_ava(rig.app, remainder='yes.')
    assert rig.store.custom_vocab == ['Morne']
    assert rig.app._ava_question is None
    assert ava._pending_action is None
    rig.app._release_wake_consumer.assert_called_with('ava_question')
    assert rig.db.recent()[0]['display_text'] == 'Dictionary: saved'
    assert rig.spoken[-1] == 'Added Morne to your dictionary.'


def test_answer_with_command_key_is_single_use(rig):
    rig.executor.process_text('spell word M O R N E', rig.app, force_commands=True)
    result = rig.executor.process_text('yes', rig.app, force_commands=True)
    assert result.claimed
    assert rig.store.custom_vocab == ['Morne']
    assert not ava_pending.consume(rig.app, 'yes')


def test_short_add_duplicate_empty_and_bounds(rig):
    rig.executor.process_text('add word Hello.', rig.app, force_commands=True)
    assert rig.store.custom_vocab == ['Hello']
    rig.executor.process_text('add word hello', rig.app, force_commands=True)
    assert rig.store.custom_vocab == ['Hello']
    assert 'already in your dictionary' in rig.spoken[-1]
    rig.executor.process_text('add word', rig.app, force_commands=True)
    assert rig.spoken[-1] == 'Which word?'
    assert ava_pending.consume(rig.app, 'Atlas.')
    assert rig.store.custom_vocab == ['Hello', 'Atlas']
    rig.executor.process_text('add word one two three four five', rig.app, force_commands=True)
    assert rig.db.recent()[0]['display_text'] == 'Dictionary: failed to parse'
    assert rig.store.custom_vocab == ['Hello', 'Atlas']
    rig.executor.process_text('spell word ' + 'A ' * 205, rig.app, force_commands=True)
    assert ava_pending.consume(rig.app, 'yes')
    assert rig.store.custom_vocab == ['Hello', 'Atlas']


def test_save_failure_is_not_reported_as_duplicate_or_left_in_memory(rig, monkeypatch):
    monkeypatch.setattr(rig.store, 'save_training_data', lambda: False)
    ava._check_teaching_intent(rig.app, 'add word Hello')
    assert rig.store.custom_vocab == []
    assert 'Could not save' in rig.spoken[-1]
    assert rig.db.recent()[0]['display_text'] == 'Dictionary: failed to save'


@pytest.mark.parametrize('answer', ['no', 'cancel', 'never mind'])
def test_rejection_never_saves(rig, answer):
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    assert ava_pending.consume(rig.app, answer)
    assert rig.store.custom_vocab == []
    assert ava._pending_action is None
    assert 'not saved' in rig.spoken[-1].lower()
    assert 'cancelled' in rig.db.recent()[0]['display_text']


def test_timeout_releases_mic_and_late_speech_callback_cannot_reopen(rig):
    rig.app.defer_speech = True
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    q = rig.app._ava_question
    assert not q.opened
    rig.now[0] = 120.0  # 830: ten seconds start after TTS, with one retry.
    rig.callbacks[-1]()
    assert q.deadline == 130.0
    rig.now[0] = 131.0
    ava_pending._tick(rig.app, q)
    assert rig.spoken[-1] == 'Say yes or no'
    rig.callbacks[-1]()
    rig.now[0] = 142.0
    ava_pending._tick(rig.app, q)
    assert rig.spoken[-1] == 'Not saved.'
    assert rig.app._ava_question is None and ava._pending_action is None
    rig.callbacks[-1]()
    assert rig.app._ava_question is None
    assert rig.store.custom_vocab == []


def test_new_command_closes_window_and_can_be_routed(rig):
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    assert ava_pending.consume(rig.app, 'open chrome')  # 830: readback nonanswers are consumed.
    assert rig.app._ava_question is None and ava._pending_action is None
    assert rig.store.custom_vocab == []
    assert 'non-answer' in rig.db.recent()[0]['display_text']


def test_new_teaching_replaces_old_readback(rig):
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    old = rig.app._ava_question
    rig.executor.process_text('spell word A T L A S', rig.app, force_commands=True)
    # 830: a different decoded command first ends the yes/no readback as no.
    assert rig.app._ava_question is None
    rig.executor.process_text('spell word A T L A S', rig.app, force_commands=True)
    assert rig.app._ava_question is not old
    ava_pending._expire(rig.app, old)
    assert ava_pending.consume(rig.app, 'yes')
    assert rig.store.custom_vocab == ['Atlas']


def test_failed_parse_is_in_history_and_does_not_save(rig):
    ava._check_teaching_intent(rig.app, 'add word Morne spelled unparseable123')
    assert rig.db.recent()[0]['display_text'] == 'Dictionary: failed to parse'
    assert rig.store.custom_vocab == []


def test_selection_and_correction_reuse_existing_paths(rig, monkeypatch):
    monkeypatch.setattr(teach_patterns, 'grab_selection_text', lambda: 'ExactName')
    rig.executor.process_text('add the selected word to my vocabulary', rig.app, force_commands=True)
    assert rig.store.custom_vocab == ['ExactName']
    rig.app.history.append(('now', 'wrong', False))
    rig.executor.process_text('correct that to Morne spelled M O R N E', rig.app, force_commands=True)
    assert ava_pending.consume(rig.app, 'yes')
    assert rig.store.apply_corrections('wrong') == 'Morne'


def test_prefill_opens_dictionary_and_never_saves_until_add(rig, monkeypatch, qapp):
    from samsara.ui import dictionary_panel_qt as ui
    panel = DictionaryPanelQt(rig.app)
    panel.resize(760, 640)
    panel.show()
    qapp.processEvents()
    rig.app.show_main_window = Mock()
    rig.app.open_hub_page = Mock(return_value=True)
    monkeypatch.setattr(ui.thread_registry, 'spawn', lambda _n, fn, **kw: fn())
    monkeypatch.setattr('samsara.ui.qt_runtime.post', lambda fn: fn())
    rig.app.history.append(('now', 'Morn', False))
    rig.executor.process_text('add that to the dictionary', rig.app, force_commands=True)
    rig.app.open_hub_page.assert_called_once_with('Dictionary')
    rig.app.show_main_window.assert_called_once()
    assert panel._vocab_input.text() == 'Morn' and panel._vocab_input.hasFocus()
    assert rig.store.custom_vocab == []
    panel._vocab_input.setText('M-O-R-N-E')
    assert panel._vocab_input.text() == 'Morne'
    rig.executor.process_text('add', rig.app, force_commands=True)
    qapp.processEvents()
    assert rig.store.custom_vocab == ['Morne']
    assert panel._vocab_list.item(0).text() == 'Morne'
    assert rig.spoken[-1] == 'Added Morne to your dictionary.'
    assert rig.db.recent()[0]['display_text'] == 'Dictionary: saved'
    panel.close()


def test_voice_save_refreshes_visible_panel_and_normalization_is_focus_scoped(rig, qapp):
    from PySide6.QtWidgets import QLineEdit
    panel = DictionaryPanelQt(rig.app)
    panel.resize(760, 640)
    panel.show()
    panel._vocab_input.setFocus()
    qapp.processEvents()
    other = QLineEdit(panel)
    other.show()
    other.setFocus()
    qapp.processEvents()
    other.setText('M-O-R-N-E')
    panel._vocab_input.setText('M-O-R-N-E')
    assert other.text() == panel._vocab_input.text() == 'M-O-R-N-E'
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    assert ava_pending.consume(rig.app, 'yes')
    qapp.processEvents()
    assert panel._vocab_list.item(0).text() == 'Morne'
    panel.close()


def test_prefill_rejects_missing_and_too_long_last_dictation(rig):
    rig.app.show_main_window = Mock()
    ava._check_teaching_intent(rig.app, 'add that to the dictionary')
    rig.app.history.append(('now', 'one two three four five', False))
    ava._check_teaching_intent(rig.app, 'add that to the dictionary')
    rig.app.show_main_window.assert_not_called()
    assert rig.store.custom_vocab == []


def test_catalog_exposes_examples_in_dictionary(rig):
    specs = command_catalog.build_catalog(rig.executor)
    claims = command_catalog.raw_phrase_claims(rig.executor)
    assert not command_catalog.collisions(claims)
    words = next(s for s in specs if s.canonical_id == 'dictionary.add_word')
    assert words.plugin == 'dictionary'
    assert 'add the word Siobhan to my vocabulary spelled S I O B H A N' in words.examples
    assert 'Ava off' in words.precondition


def test_dictation_key_also_checks_teaching_window_without_bootstrap():
    tree = ast.parse((Path(__file__).parents[1] / 'dictation.py').read_text(encoding='utf-8'))
    conditions = [ast.unparse(n.test) for n in ast.walk(tree) if isinstance(n, ast.If)]
    assert any('teaching_reply' in c and 'consume(self, text' in c and 'dictation_key' in c
               for c in conditions)


def test_exact_command_ends_word_wait(rig, monkeypatch):
    ava._check_teaching_intent(rig.app, 'add word')
    monkeypatch.setattr(rig.executor, 'find_exact_command', lambda text: 'open chrome')
    assert not ava_pending.consume(rig.app, 'open chrome')
    assert ava._pending_action is None and rig.app._ava_question is None


def test_generation_retirement_cancels_teaching(rig):
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    rig.app._ava_cmd_generation += 1
    assert ava_pending.consume(rig.app, 'yes')
    assert ava._pending_action is None and rig.app._ava_question is None
    assert rig.store.custom_vocab == []


def test_other_pending_operation_is_preserved(rig, monkeypatch):
    operation = object()
    monkeypatch.setattr(ava.execution_policy, 'pending_operation', lambda: operation)
    ava._check_teaching_intent(rig.app, 'spell word M O R N E')
    assert rig.app.__dict__.get('_ava_question') is None
    assert ava._pending_action is None
    assert rig.store.custom_vocab == []
    assert 'Another confirmation' in rig.spoken[-1]
