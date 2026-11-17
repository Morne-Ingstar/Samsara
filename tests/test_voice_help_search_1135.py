"""1135: frozen everyday acceptance questions, real command lanes, offline UI."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from samsara import help_intents as hi, help_search as hs
from samsara.command_registry import DispatchState
from samsara.ui import context_help_qt as ui
from tests._theme_stub_app import StubApp
# Exercise the same production registry/policy setup as the 943 safety tests.
from tests.test_help_intents_943 import loaded_registry, executor, rig


CASES = [
    ('What is a wake word?', ('term', 'wake-word'),
     'A phrase that tells Samsara to listen for a command without holding a key.'),
    ('What is a hands-free session?', ('term', 'hands-free'),
     'An open session for dictation and commands without holding a key.'),
    ('How do I choose my interface size and theme?', ('page', 'General'),
     'Choose your microphone, listening display, interface size and theme.'),
    ('Where do I choose sounds and spoken feedback?', ('page', 'Audio'),
     'Choose sounds and spoken feedback.'),
    ('What does command mode do?', ('quick_reference', 'command'),
     'Legacy command-only lane retained for compatibility.'),
    ('What does Ava mode do?', ('quick_reference', 'ava'),
     'Everything you say goes to the local AI agent as natural language.'),
    ('Where can I search for an action and see its wording?', ('term', 'command-reference'),
     'The list of commands you can use in Samsara.'),
    ('How do I change my mic?', None, 'Opening General.'),
    ('What is dictation and audio settings?', ('page', 'Dictation'),
     'Choose how dictation and hands-free sessions work. Showing Dictation help. Another match is Audio help.'),
    ('How do I fly a purple kite?', None, hi.UNKNOWN_REPLY),
]


def test_acceptance_phrases_frozen_before_tuning():
    assert len(CASES) == 10
    assert [q for q, _, _ in CASES if hi.normalize(q) in hi.PHRASES] == ['How do I change my mic?']


@pytest.mark.parametrize('question,expected,spoken', CASES)
def test_ten_command_questions_find_exact_answer_and_say_exact_text(rig, monkeypatch, question, expected, spoken):
    command_executor, app, places = rig
    cards = []
    monkeypatch.setattr(ui, 'open_help_entry', lambda app, entry: cards.append((entry.kind, entry.id)))
    assert command_executor.process_text(question, force_commands=True).succeeded
    assert cards == ([expected] if expected else [])
    assert app.audio_coordinator.speak.call_args.args[0] == spoken
    if question == 'How do I change my mic?':
        assert places == ['general']
        assert not app.cheat_sheet.show_scoped.called
    elif question == 'How do I fly a purple kite?':
        assert not places
        assert app.cheat_sheet.show_scoped.call_args.kwargs['query'] == ''
    else:
        assert not places and not app.cheat_sheet.show_scoped.called


@pytest.mark.parametrize('question,expected,spoken', CASES)
def test_ten_questions_remain_literal_in_dictation(rig, monkeypatch, question, expected, spoken):
    command_executor, app, places = rig
    opener = Mock()
    monkeypatch.setattr(ui, 'open_help_entry', opener)
    assert hi.match_help(question, command_mode=False) is None
    result = command_executor.process_text(question)
    assert result.state is DispatchState.MISS and result.result == question
    assert not places and not opener.called and not app.audio_coordinator.speak.called


@pytest.mark.parametrize('lane', ['active', 'prefix', 'hands-free-dictation', 'ava-hold'])
def test_search_obeys_existing_lane_admission(rig, monkeypatch, lane):
    command_executor, app, places = rig
    opener = Mock()
    monkeypatch.setattr(ui, 'open_help_entry', opener)
    app.command_mode_active = lane in {'active', 'hands-free-dictation'}
    if lane == 'hands-free-dictation':
        app._session_mode_manager = SimpleNamespace(mode=SimpleNamespace(name='DICTATE'))
    result = command_executor.process_text(
        ('command ' if lane == 'prefix' else '') + 'what does command mode do',
        capture_path='ava_hold' if lane == 'ava-hold' else None)
    assert opener.called == (lane in {'active', 'prefix'})
    assert result.succeeded == (lane in {'active', 'prefix'})


def test_index_sources_determinism_bounds_and_no_io(monkeypatch):
    import builtins
    import socket
    from samsara.help_terms import TERMS, PAGE_HELP
    hs.entries.cache_clear()
    index = hs.entries()
    assert {e.id for e in index if e.kind == 'term'} == set(TERMS)
    assert {e.id for e in index if e.kind == 'page'} == set(PAGE_HELP)
    assert {e.id for e in index if e.kind == 'place'} == set(hi.TARGETS)
    assert {e.id for e in index if e.kind == 'quick_reference'} == {'command', 'dictate', 'ava'}
    def forbidden(*a, **kw):
        pytest.fail('Search attempted file/network I/O')
    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    for question, expected, _ in CASES:
        assert hs.search(question) == hs.search(question)
        if expected:
            assert (hs.search(question)[0].entry.kind, hs.search(question)[0].entry.id) == expected
    assert hs.search('help') == hs.search('purple kite') == ()
    assert hs.search('how do I wake my neighbor') == ()
    assert hs.search('wake word', limit=0) == ()
    assert len(hs.short_summary('word ' * 100)) <= 180
    assert hs.short_summary('One sentence. Another sentence.') == 'One sentence.'


def test_definition_exception_and_exact_navigation_preserved(rig, monkeypatch):
    command_executor, app, places = rig
    opener = Mock()
    monkeypatch.setattr(ui, 'open_help_entry', opener)
    assert hi.PHRASES['what is the wake word'].target == 'wake_word'
    assert command_executor.process_text('what is the wake word', force_commands=True).succeeded
    assert opener.call_args.args[1].id == 'wake-word'
    assert not places
    opener.reset_mock()
    assert command_executor.process_text('how can I set up the wake word', force_commands=True).succeeded
    assert places == ['dictation'] and not opener.called


def test_spoken_wake_summary_uses_same_current_facts_as_explanation(rig, monkeypatch):
    command_executor, app, _ = rig
    monkeypatch.setattr(ui, 'open_help_entry', Mock())
    app.config = {'command_mode': {'mode': 'toggle'}, 'wake_word_config': {'opens_session': True}}
    assert command_executor.process_text('what is the wake word', force_commands=True).succeeded
    assert app.audio_coordinator.speak.call_args.args[0] == (
        'A phrase that tells Samsara to start a hands-free session without holding a key.')


def settle(qapp):
    for _ in range(8):
        qapp.processEvents()


@pytest.fixture
def offline(monkeypatch):
    import socket
    from PySide6.QtGui import QDesktopServices
    from samsara.ui import qt_runtime, window_activation
    def forbidden(*a, **kw):
        pytest.fail('Help used a browser or network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(QDesktopServices, 'openUrl', forbidden)
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    monkeypatch.setattr(window_activation, 'surface_requested', lambda w: w.show())
    app = StubApp({'wake_word_enabled': False, 'command_mode': {'enabled': False}})
    return app


@pytest.mark.parametrize('kind,key', [('term', 'wake-word'), ('term', 'command-reference'),
                                     ('page', 'General'), ('page', 'Audio'),
                                     ('quick_reference', 'command'), ('quick_reference', 'ava'),
                                     ('place', 'microphone')])
def test_real_opener_escape_close_reopen_without_widget_growth(offline, qapp, qt_widget_scope, kind, key):
    entry = next(e for e in hs.entries() if (e.kind, e.id) == (kind, key))
    assert ui.open_help_entry(offline, entry)
    wrapper = offline._quick_reference_qt if kind == 'quick_reference' else offline._settings_qt
    window = wrapper._window
    with qt_widget_scope(window):
        counts = []
        for attempt in range(3):
            if attempt:
                assert ui.open_help_entry(offline, entry)
            window.activateWindow()
            settle(qapp)
            # Offscreen can deliver a stale deactivation after a hidden window
            # reopens. Pin activation; the production handoff may be refused by
            # the OS and is covered separately by window_activation's tests.
            qapp.setActiveWindow(window)
            settle(qapp)
            if kind == 'quick_reference':
                controller = window._voice_help
            elif kind == 'page':
                assert window._stack.currentWidget().title == key
                controller = window._local_page_help.controller
            else:
                assert window._stack.currentWidget().title == 'Help'
                controller = window._stack.currentWidget()._offline_help
            card = controller.active
            assert card is not None and card.isVisible()
            assert card.heading.text() == entry.title
            assert card.close_button.hasFocus()
            origin = controller._origin()
            counts.append(len(window.findChildren(ui.ExplanationCard)))
            QTest.keyClick(card.close_button, Qt.Key.Key_Escape)
            assert controller.active is None and card.isHidden()
            assert origin.hasFocus()
            window.close()
            settle(qapp)
            assert controller.active is None and not controller._installed
        assert counts == [counts[0]] * 3


def test_opener_posts_before_constructing_windows(monkeypatch):
    from samsara.ui import qt_runtime
    calls = []
    monkeypatch.setattr(qt_runtime, 'post', calls.append)
    app = SimpleNamespace()
    assert ui.open_help_entry(app, hs.entries()[0])
    assert len(calls) == 1 and not hasattr(app, '_settings_qt')


def test_hub_page_selection_finishes_before_its_explanation(monkeypatch):
    from samsara.ui import qt_runtime
    queue, opened = [], []
    monkeypatch.setattr(qt_runtime, 'post', queue.append)
    host = SimpleNamespace()
    host._local_page_help = SimpleNamespace(open=lambda origin: opened.append(host.selected))
    app = SimpleNamespace()
    app.show_main_window = lambda: queue.append(lambda: setattr(app, 'main_window', SimpleNamespace(_window=host)))
    def select(name):
        queue.append(lambda: setattr(host, 'selected', name))
        return True
    app.open_hub_page = select
    entry = next(e for e in hs.entries() if (e.kind, e.id) == ('page', 'Dictionary'))
    ui.open_help_entry(app, entry)
    while queue:
        queue.pop(0)()
    assert opened == ['Dictionary']
