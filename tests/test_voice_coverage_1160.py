"""Approved 1160 phrases: production routing plus existing local action seams."""
from functools import wraps
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from samsara.command_scope import MatchContext


ROUTES = [
    ('show on-screen messages', 'review_messages', ''),
    ('show on screen messages', 'review_messages', ''),
    ('mute Ava voice', 'mute_ava', ''),
    ('unmute Ava voice', 'unmute_ava', ''),
    ('use continuous dictation', 'continuous_mode', ''),
    ('use hold to talk', 'hold_mode', ''),
    ('search samsara history for Project Notes', 'search_history', 'Project Notes'),
    ('search samsara commands for volume', 'search_commands', 'volume'),
    ('search saved snippets for meeting', 'search_snippets', 'meeting'),
    ('show latest history entry', 'latest_history_entry', ''),
    ('load older history entries', 'older_history_entries', ''),
    ('copy selected text', 'copy_selected_text', ''),
    ('copy text', 'copy_selected_text', ''),
    ('play last memo', 'play_last_memo', ''),
    ('play selected memo', 'play_selected_memo', ''),
    ('play memo', 'play_memo', ''),
    ('stop memo', 'stop_video_memo', ''),
    ('pause memo', 'stop_video_memo', ''),
    ("select today's memos", 'select_today_memos', ''),
    ('reload samsara commands', 'reload_commands', ''),
    ('show dictionary corrections', 'dictionary_corrections', ''),
    ('show vocabulary', 'vocabulary_voice_guide', ''),
    ('pin selected command', 'pin_reference_command', ''),
    ('pin command', 'pin_reference_command', ''),
    ('recalibrate my microphone', 'recalibrate_mic', ''),
    ('recalibrate my mic', 'recalibrate_mic', ''),
    ('recalibrate mic', 'recalibrate_mic', ''),
    ('recalibrate microphone', 'recalibrate_mic', ''),
    ('microphone recalibrate', 'recalibrate_mic', ''),
    ('release stuck mouse buttons', 'release_mouse_buttons', ''),
    ('release stuck buttons', 'release_stuck_buttons', ''),
    ('cancel file transcription', 'cancel_file_transcription', ''),
]


@pytest.fixture(scope='module')
def router():
    from tools.dump_command_metadata import build_executor
    from samsara import plugin_commands
    executor = build_executor()
    executor._matcher.set_context_provider(lambda: MatchContext.for_app('notepad.exe'))
    executor._grammar_catalog()
    return executor, dict(plugin_commands._REGISTRY)


@pytest.fixture
def executor(router, monkeypatch):
    from samsara import plugin_commands
    executor, registry = router
    plugin_commands._REGISTRY.update(registry)
    monkeypatch.setattr(executor._matcher, '_enabled_packs', None)
    return executor


@pytest.mark.parametrize('spoken,handler,query', ROUTES)
def test_approved_phrase_routes_through_production_executor(executor, monkeypatch, spoken, handler, query):
    from plugins.commands import samsara_features as features, video_memo
    entry, remainder = executor._matcher.match(spoken)
    assert entry is not None
    assert entry.handler is getattr(video_memo if handler == 'stop_video_memo' else features, handler)
    assert remainder == query
    calls = []

    @wraps(entry.handler)
    def effect(app, remainder):
        calls.append(remainder)
        return True
    monkeypatch.setattr(entry, 'handler', effect)
    app = NS(config={'commands': {'grammar_tier': False}}, command_executor=executor,
             command_mode_active=False, command_matching_enabled=True)
    result = executor.process_text(spoken, app, force_commands=True)
    assert result.succeeded, result
    assert calls == [query]


@pytest.mark.parametrize('spoken,handler,query', ROUTES)
def test_hands_free_admission_is_exact_and_never_takes_a_phrase_from_prose(executor, spoken, handler, query):
    # This is the exact admission method used by _probe_hands_free_command.
    entry, _ = executor._matcher.match(spoken)
    assert executor.find_exact_command(spoken) == (None if query else entry.phrase)
    assert executor.find_exact_command('I said ' + spoken) is None
    assert executor.find_exact_command(spoken + ' while writing this sentence') is None
    for text in ('I said ' + spoken, spoken + ' while writing this sentence'):
        candidate, _ = executor._matcher.match(text)
        if not query:
            assert candidate is None or candidate.handler is not entry.handler


def test_search_query_is_data_and_preserves_case_and_punctuation(executor, monkeypatch):
    entry, query = executor._matcher.match('search samsara history for Project Notes then delete everything!')
    assert entry.handler.__name__ == 'search_history'
    assert query == 'Project Notes then delete everything!'
    for lead in ('search samsara history for', 'search samsara commands for', 'search saved snippets for'):
        assert executor._matcher._entries[lead].handler.match_utterance(lead) is None
        assert executor._matcher._entries[lead].handler.match_utterance('please ' + lead + ' hello') is None
        entry = executor._matcher._entries[lead]
        calls = []

        @wraps(entry.handler)
        def capture(app, remainder):
            calls.append(remainder)
            return True
        monkeypatch.setattr(entry, 'handler', capture)
        app = NS(config={'commands': {'grammar_tier': True}}, command_executor=executor,
                 command_mode_active=True, command_matching_enabled=True)
        result = executor.process_text(lead + ' Project Notes then copy', app, force_commands=True)
        assert result.succeeded and result.phrase == lead, result
        assert calls == ['Project Notes then copy']


def test_all_new_commands_have_catalogue_help_and_no_duplicate_claims(executor):
    from samsara import command_catalog as cc
    records = [s for s in cc.build_catalog(executor) if s.plugin == 'samsara_features']
    assert len(records) == 23
    assert all(s.description and s.examples and s.precondition for s in records)
    claims = cc.raw_phrase_claims(executor)
    for spoken, _, query in ROUTES:
        if not query:
            assert len(claims[cc.normalize_phrase(spoken)]) == 1


@pytest.fixture
def features(monkeypatch):
    from plugins.commands import samsara_features
    from samsara.ui import qt_runtime
    monkeypatch.setattr(qt_runtime, 'post', lambda callback: callback())
    return samsara_features


@pytest.mark.parametrize('function,method,args', [
    ('continuous_mode', 'switch_mode_from_tray', ('continuous',)),
    ('hold_mode', 'switch_mode_from_tray', ('hold',)),
    ('recalibrate_mic', 'recalibrate_mic', ()),
    ('release_mouse_buttons', 'release_mouse_buttons', ()),
])
def test_app_actions_reuse_existing_handlers(features, function, method, args):
    app = NS(**{method: Mock()})
    assert getattr(features, function)(app)
    getattr(app, method).assert_called_once_with(*args)


def test_message_review_uses_existing_window(features, monkeypatch):
    from samsara.ui import passive_review
    open_review = Mock()
    monkeypatch.setattr(passive_review, 'open_review', open_review)
    app = NS()
    features.review_messages(app)
    open_review.assert_called_once_with(app)


@pytest.mark.parametrize('muted', [True, False])
def test_ava_mute_changes_only_its_existing_preference(features, muted):
    config = {'accessibility': {'ava_captions_enabled': True}, 'tts': {'engine': 'piper'}}
    app = NS(config=config, save_config=Mock(), _show_outcome_chip=Mock())
    (features.mute_ava if muted else features.unmute_ava)(app)
    assert app.config == {'accessibility': {'ava_captions_enabled': True, 'ava_mute_audio': muted},
                          'tts': {'engine': 'piper'}}
    app.save_config.assert_called_once()


@pytest.mark.parametrize('name,function,query,method,args', [
    ('History', 'search_history', 'Project', '_search.setText', ('Project',)),
    ('Commands', 'search_commands', 'volume', 'set_view', (None, ('All', 'volume'))),
    ('Snippets', 'search_snippets', 'meeting', '_search.setText', ('meeting',)),
    ('Dictionary', 'dictionary_corrections', '', '_tabs.setCurrentIndex', (1,)),
    ('Memos', 'select_today_memos', '', 'select_today_batch', ()),
])
def test_navigation_and_search_use_existing_page_actions(features, monkeypatch, name, function, query, method, args):
    page = Mock()
    opened = []
    monkeypatch.setattr(features, '_hub', lambda app, label, action: opened.append(label) or action(page) or True)
    getattr(features, function)(NS(), query)
    assert opened == [name]
    target = page
    for part in method.split('.'):
        target = getattr(target, part)
    target.assert_called_once_with(*args)


def test_selected_actions_decline_hidden_pages(features):
    app = NS(_show_outcome_chip=Mock())
    features.play_selected_memo(app)
    features.older_history_entries(app)
    features.pin_reference_command(app)
    assert app._show_outcome_chip.call_count == 3


def test_play_memo_uses_selection_or_newest_and_stop_never_opens_hub(features, monkeypatch):
    page = Mock(selected={'id': 'selected'}, _records=[{'id': 'newest'}])
    monkeypatch.setattr(features, '_hub', lambda app, name, action: action(page))
    app = NS(main_window=NS(_window=NS(_panel_cache={'Memos': page})))
    features.play_memo(app)
    page.play_selected.assert_called_once()
    page._apply_filter.assert_not_called()
    page.selected = None
    features.play_memo(app)
    page._list.setCurrentRow.assert_called_once_with(0)
    features.stop_memo(app)
    page.stop_playback.assert_called_once()


def test_existing_stop_command_keeps_video_capture_and_stops_audio_otherwise(features, monkeypatch):
    from plugins.commands.video_memo import stop_video_memo
    controller = NS(active=True, finish=Mock(return_value=True))
    app = NS(_video_memo_controller=controller)
    stop_audio = Mock(return_value=True)
    monkeypatch.setattr(features, 'stop_memo', stop_audio)
    assert stop_video_memo(app, '')
    controller.finish.assert_called_once()
    stop_audio.assert_not_called()
    controller.active = False
    assert stop_video_memo(app, '')
    stop_audio.assert_called_once_with(app)


def test_release_general_buttons_reuses_key_and_mouse_release_handlers(features):
    from samsara.handlers import CommandContext
    keyboard = Mock()
    context = CommandContext(keyboard_controller=keyboard, mouse_controller=Mock(),
                             held_keys={'shift': 'shift'}, key_map={})
    app = NS(release_mouse_buttons=Mock(), key_macro_manager=Mock(),
             command_executor=NS(_build_context=Mock(return_value=context)))
    features.release_stuck_buttons(app)
    app.release_mouse_buttons.assert_called_once()
    keyboard.release.assert_called_once_with('shift')
    assert context.held_keys == {}
    app.key_macro_manager.release_all_held.assert_called_once()


def test_reload_and_cancel_reuse_existing_actions(features):
    app = NS(command_executor=NS(reload_commands=Mock()), _transcribe_file_qt=NS(cancel=Mock()))
    features.reload_commands(app)
    features.cancel_file_transcription(app)
    app.command_executor.reload_commands.assert_called_once()
    app._transcribe_file_qt.cancel.assert_called_once()


@pytest.fixture
def qt():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_cold_hub_selects_only_after_window_construction(features, monkeypatch):
    from samsara.ui import qt_runtime, window_activation
    queue, calls = [], []
    monkeypatch.setattr(qt_runtime, 'post', queue.append)
    monkeypatch.setattr(window_activation, 'surface_requested', lambda win: calls.append('raised'))
    page = object()
    window = NS(_activate=lambda name: calls.append(name) or page)
    app = NS(main_window=NS(_window=None))
    app.show_main_window = lambda: queue.append(lambda: setattr(app.main_window, '_window', window))
    features._hub(app, 'History', lambda target: calls.append(target))
    while queue:
        queue.pop(0)()
    assert calls == ['History', 'raised', page]


def test_visible_current_and_standalone_pages_are_the_only_selection_targets(features, qt):
    from PySide6.QtWidgets import QWidget, QStackedWidget
    window, stack, page, other = QWidget(), QStackedWidget(), QWidget(), QWidget()
    window._panel_cache, window._stack = {'Memos': page}, stack
    stack.addWidget(page)
    stack.addWidget(other)
    app = NS(main_window=NS(_window=window))
    window.show()
    window.activateWindow()
    qt.processEvents()
    assert features._current_page(app, 'Memos') is page
    stack.setCurrentWidget(other)
    assert features._current_page(app, 'Memos') is None
    window.hide()
    standalone = QWidget()
    standalone._view = other
    app._history_qt = NS(_window=standalone)
    standalone.show()
    standalone.activateWindow()
    qt.processEvents()
    assert features._current_page(app, 'History') is other
    standalone.hide()


def test_latest_history_waits_for_matching_refresh_and_skips_headers(features, qt, monkeypatch):
    from samsara.ui.history_view import HistoryView
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    monkeypatch.setattr(HistoryView, '_reload', lambda self: setattr(self, '_request_generation', self._request_generation + 1))
    page = HistoryView(None)
    page._search.setText('old search')
    features._latest_history(page)
    expected = page._request_generation
    assert not page._search.text()
    assert page._scope.currentText() == 'All time'
    assert page._filter.currentText() == 'All'
    # Existing view renders rows first; fake that effect to avoid a background DB.
    page._rows_ready.disconnect(page._on_rows_ready)
    page._list.addItem(QListWidgetItem('Today'))
    item = QListWidgetItem('newest')
    item.setData(Qt.ItemDataRole.UserRole, {'text': 'newest', 'timestamp': ''})
    page._list.addItem(item)
    page._rows_ready.emit([], False, expected - 1)
    assert page._list.currentItem() is None
    page._rows_ready.emit([], False, expected)
    assert page._list.currentItem() is item
    page.close()


def test_pin_is_idempotent_and_uses_the_current_detail_selection(features, qt):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidget, QListWidgetItem
    listing = QListWidget()
    item = QListWidgetItem('first')
    item.setData(Qt.ItemDataRole.UserRole, 'first')
    listing.addItem(item)
    listing.setCurrentItem(item)
    page = NS(_list=listing, _detail_card=NS(isVisible=lambda: True, phrase='second'),
              _all=[{'phrase': 'first'}, {'phrase': 'second'}], _pinned=set())
    page._toggle_pin = lambda phrase: page._pinned.add(phrase)
    features._pin(NS(), page)
    features._pin(NS(), page)
    assert page._pinned == {'second'}


@pytest.mark.parametrize('surface', ['history', 'memo'])
def test_copy_selected_text_uses_real_history_and_memo_text_selection(features, qt, monkeypatch, tmp_path, surface):
    from samsara import handlers
    from PySide6.QtWidgets import QMainWindow
    if surface == 'history':
        from samsara.ui.history_view import HistoryView
        monkeypatch.setattr(HistoryView, '_reload', lambda self: None)
        page = HistoryView(None)
        editor = page._detail.text
        page._detail.show()
    else:
        from samsara.ui.memos_qt import MemosPage
        page = MemosPage(NS(config={}), home=tmp_path)
        editor = page._detail
        page._editor_pane.show()
    window = QMainWindow()
    window.setCentralWidget(page)
    editor.setPlainText('alpha selected words omega')
    cursor = editor.textCursor()
    cursor.setPosition(6)
    cursor.setPosition(20, cursor.MoveMode.KeepAnchor)
    editor.setTextCursor(cursor)
    window.show()
    window.activateWindow()
    editor.setFocus()
    qt.processEvents()
    copied = []
    monkeypatch.setattr(editor, 'copy', lambda: copied.append(editor.textCursor().selectedText()))
    monkeypatch.setattr(handlers, '_get_foreground_hwnd', lambda: 7)
    app = NS(command_executor=Mock())
    features.copy_selected_text(app)
    assert copied == ['selected words']
    app.command_executor._build_context.assert_not_called()
    window.close()


def test_external_copy_uses_existing_hotkey_handler_and_refuses_focus_changes(features, qt, monkeypatch):
    from samsara import handlers
    from samsara.ui import qt_runtime
    for window in qt.topLevelWidgets():
        window.hide()
    qt.processEvents()
    queue, calls = [], []
    hwnd = [7]
    monkeypatch.setattr(qt_runtime, 'post', queue.append)
    monkeypatch.setattr(handlers, '_get_foreground_hwnd', lambda: hwnd[0])
    monkeypatch.setattr(handlers.HotkeyHandler, 'execute', lambda self, cmd, ctx: calls.append((cmd, ctx)))
    app = NS(command_executor=NS(_build_context=lambda app: 'context'), _show_outcome_chip=Mock())
    features.copy_selected_text(app)
    queue.pop(0)()
    assert calls == [({'type': 'hotkey', 'keys': ['ctrl', 'c']}, 'context')]
    calls.clear()
    features.copy_selected_text(app)
    hwnd[0] = 8
    queue.pop(0)()
    assert not calls
    app._show_outcome_chip.assert_called_once()


def test_quick_reference_uses_live_catalogue_examples(executor, qt):
    from samsara import command_catalog as cc
    from samsara.ui.quick_reference_qt import _QuickReferenceWindow
    from PySide6.QtWidgets import QLabel
    records = cc.catalog_from_registry_rows(executor._matcher.list_commands())
    host = _QuickReferenceWindow(NS(config={}, command_executor=executor))
    card = host._build_samsara_actions_section(records)
    text = '\n'.join(label.text() for label in card.findChildren(QLabel))
    for record in records:
        if record['plugin'] == 'samsara_features':
            for example in record['examples']:
                assert example in text
    unavailable = host._build_samsara_actions_section(None)
    assert any('unavailable' in label.text() for label in unavailable.findChildren(QLabel))
    host.close()
