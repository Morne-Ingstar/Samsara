"""Explicit draft cleanup repairs pauses; keeps words and review authority."""
from types import SimpleNamespace
from unittest.mock import Mock
import re

import pytest

from samsara.ava_edit import selection, session
from samsara.ava_edit.proposal import diff_changes
from samsara.cleanup import clean_text
from samsara.session_modes import SessionMode, SessionModeManager
from tools.voice_corpus.script import SCRIPT


# Assemble the supplied regression without copying a passage into diagnostics.
OWNER = ('Now I\'m gonna try. ' + 'The saying, this sort of Dictation. '
         + 'But with... Pauses. ' + 'And I wanna make sure... That... '
         + 'Cleanup. Actually works.')
OWNER_FIXED = ('Now I\'m gonna try the saying, this sort of dictation '
               + 'but with pauses and I wanna make sure that cleanup actually works.')

REPAIRS = [
    (OWNER, OWNER_FIXED),
    ('I need... To go.', 'I need to go.'),
    ('I need… To go.', 'I need to go.'),
    ('I need...to go.', 'I need to go.'),
    ('We paused... And continued.', 'We paused and continued.'),
    ('We paused… And continued.', 'We paused and continued.'),
    ('I waited... Then I left.', 'I waited then I left.'),
    ('I think... I know.', 'I think I know.'),
    ("I think… I'm ready.", "I think I'm ready."),
    ('We called... NASA today.', 'We called NASA today.'),
    ('I met... Alice today.', 'I met Alice today.'),
    ('I use... iPhone daily.', 'I use iPhone daily.'),
    ('I spoke... Élodie yesterday.', 'I spoke Élodie yesterday.'),
    ('I need...  To  go.', 'I need to go.'),
    ('We thought... That... It works.', 'We thought that it works.'),
    ('I want to try. The new tool.', 'I want to try the new tool.'),
    ('We discussed. The new plan.', 'We discussed the new plan.'),
    ('I went with. Pauses.', 'I went with pauses.'),
    ('I want to check that Cleanup. Actually works.',
     'I want to check that cleanup actually works.'),
    ('Cleanup. Actually works.', 'Cleanup actually works.'),
    ('The small blue parcel. Arrived today.', 'The small blue parcel arrived today.'),
    ('The tool. Works.', 'The tool works.'),
    ('This approach takes a little longer. But with less effort.',
     'This approach takes a little longer but with less effort.'),
    ('We stayed home. Because it rained.', 'We stayed home because it rained.'),
    ('We stayed home. When it rained.', 'We stayed home when it rained.'),
    ('We stayed home. If it rained.', 'We stayed home if it rained.'),
    ('We stayed home. With Alice.', 'We stayed home with Alice.'),
    ('I know the place. Where we met.', 'I know the place where we met.'),
    ('I know the tool. Which helps us.', 'I know the tool which helps us.'),
    ('I want to know. That it works.', 'I want to know that it works.'),
    ('I spoke to. Alice in London.', 'I spoke to Alice in London.'),
    ('I need to. Live in their world.', 'I need to live in their world.'),
    ('I went to the. Store and. Bought some milk.',
     'I went to the store and bought some milk.'),
    ('It helps me. Live with less pain.', 'It helps me live with less pain.'),
    ('I use Dictation with Pauses for Cleanup.', 'I use dictation with pauses for cleanup.'),
    ('I need... To go...', 'I need to go...'),
    ('I need… To go…', 'I need to go…'),
    ('I need to. Go. With Alice.', 'I need to go with Alice.'),
    ('I need to . Go.', 'I need to go.'),
    ('I need to,. Go.', 'I need to go.'),
    ('I need to, . Go.', 'I need to go.'),
    ('I waited. Instead of leaving.', 'I waited instead of leaving.'),
    ('I called… Cleanup yesterday.', 'I called Cleanup yesterday.'),
    ('I met... May and Will.', 'I met May and Will.'),
    ('We know… I’m ready.', 'We know I’m ready.'),
]

KEEP = [
    'It works. I checked.', 'Go now. Stop here.', 'I finished. Then I left.',
    'I am ready. Go now.', 'I saw him. He waved.', 'I saw it. That was odd.',
    'I know that. He told me.', 'I know who. Alice told me.',
    'I think so. Then we left.', 'There it is. I found it.',
    'Is it done. Yes.', 'Is it done? Yes.', 'I need to. Live here?',
    'I need to. Stop!', 'I am ready. Yes.', 'I like you. Live your life.',
    'Milk. Eggs. Bread.', 'Shopping: Milk. Eggs. Bread.',
    '- Milk. Eggs.', '1. Milk. Eggs.', 'a. Milk. Eggs.',
    'Milk.\nEggs.\nBread.', 'I need to.\nLive here.', 'I need to.\r\n\r\nLive here.',
    'I waited...', 'I waited…', 'I waited...  ',
    'I saw Dr. Smith. He waved.', 'I met Mr. Jones.', 'I met Prof. Jones.',
    'I spoke to J. Smith.', 'We live in the U.S. And we like it.',
    'It weighs 3.5 kilograms. It works.', 'Meet at 9 a.m. Then leave.',
    'Use e.g. this example.', 'Visit https://example.com/a.txt. It works.',
    'Open report.txt. It works.', 'Use my_file.py. Then go.',
    'Open C:\\notes\\draft.txt.', 'Email alice@example.com. Then go.',
    'He said "Wait... Then go."', 'He said “Wait… Then go.”',
    "He said 'Wait... Then go.'", 'I need to. "Live here."',
    'I met Alice in London.', 'I met May and Rose.', 'I use NASA and iPhone.',
    'I met Quilter on Tuesday.', 'I sent the file to New York.',
    'I said clean up the draft tomorrow.', 'It works. Clean up the draft.',
    'It works. Clear draft.', 'It works. Show the change.',
    'It works. Pause VLC.', 'It works. Cancel.',
    'I need to.\n\nLive in their world.', '', 'yes', 'stop',
    "I need to. We're ready.", "I need to. I'm ready.",
    'I need to. We’re ready.', 'Is it done. Because it works.',
    'I called Cleanup yesterday.', 'I use CleanUp daily.',
]


@pytest.mark.parametrize('source,expected', REPAIRS, ids=[f'repair-{i:02}' for i in range(len(REPAIRS))])
def test_repairs_and_idempotence(source, expected):
    actual = selection.deterministic_cleanup(source)
    assert actual == expected
    assert selection.deterministic_cleanup(actual) == actual
    assert re.findall(r"\w+(?:['’]\w+)*", actual.casefold()) == re.findall(r"\w+(?:['’]\w+)*", source.casefold())


@pytest.mark.parametrize('source', KEEP, ids=[f'keep-{i:02}' for i in range(len(KEEP))])
def test_keeps_and_idempotence(source):
    assert selection.deterministic_cleanup(source) == source
    assert selection.deterministic_cleanup(selection.deterministic_cleanup(source)) == source


TAGGED = [item for item in SCRIPT if {'hands_free', 'pause', 'joins', 'long'} & set(item['tags'])]


@pytest.mark.parametrize('item', TAGGED, ids=lambda item: item['id'])
def test_clean_corpus_references_are_byte_identical(item):
    for field in ('intended_text', 'expected_text'):
        text = item[field]
        assert selection.deterministic_cleanup(text).encode() == text.encode()


def test_automatic_held_cleanup_does_not_gain_pause_rules():
    source = 'I need... To go.'
    assert clean_text(source) == source
    assert clean_text(source, mode='verbatim') == source


@pytest.fixture
def app(monkeypatch):
    session.clear()
    monkeypatch.setattr(session.thread_registry, 'spawn', lambda _name, fn, **_: fn())
    value = SimpleNamespace(config={'ava_edit': {'enabled': False}},
        _show_outcome_chip=Mock(), _speak_session_notice=Mock(), _agent=lambda *_: None)
    manager = SessionModeManager(abort_phrases=['cancel'], inject_fn=lambda *_: False,
        foreground_exe_resolver=lambda: 'fake.exe', remove_chars_fn=lambda *_: None,
        command_dispatch_fn=lambda *_: None, agent_dispatch_fn=value._agent,
        buffer_dictate_until_commit=True, live_surface_enabled=True)
    manager.reset(SessionMode.DICTATE)
    manager._dictate_pending_buffer = OWNER
    value._session_mode_manager = manager
    yield value
    session.clear()


@pytest.mark.parametrize('answer', ['apply', 'discard'])
def test_larger_rewrite_keeps_truthful_preview_and_apply_authority(app, monkeypatch, answer):
    from samsara.ava_edit import model
    rewrite = Mock(side_effect=AssertionError('offline cleanup must not call a model'))
    monkeypatch.setattr(model, 'rewrite', rewrite)
    assert session.handle_utterance(app, 'clean up')
    proposal = session.pending().proposal
    assert proposal.original == OWNER and proposal.rewritten == OWNER_FIXED
    assert proposal.changes == diff_changes(OWNER, OWNER_FIXED)
    assert proposal.change_count == len(proposal.changes) > 0
    assert app._session_mode_manager.dictate_pending_buffer == OWNER
    text = app._show_outcome_chip.call_args.args[0]
    assert f'{proposal.change_count} changes' in text and 'your draft' in text
    assert session.handle_utterance(app, answer)
    assert session.pending() is None
    assert app._session_mode_manager.dictate_pending_buffer == (OWNER_FIXED if answer == 'apply' else OWNER)
    rewrite.assert_not_called()


def test_cleanup_logging_contains_counts_only(app, monkeypatch):
    info = Mock()
    monkeypatch.setattr(session.logger, 'info', info)
    proposal = session.propose(OWNER, selection.DEFAULT_INSTRUCTION, app, offline=True, cleanup=True)
    assert proposal.ok and proposal.rewritten == OWNER_FIXED
    summary = info.call_args.args[0]
    assert 'path=offline' in summary and 'joins_made=' in summary
    assert OWNER not in summary and OWNER_FIXED not in summary


def test_show_change_displays_whole_before_and_after(app, qapp, monkeypatch):
    from PySide6.QtWidgets import QTextBrowser
    from samsara.ava_edit import draft_review
    from samsara.ui import qt_runtime
    monkeypatch.setattr(qt_runtime, 'is_alive', lambda: True)
    monkeypatch.setattr(qt_runtime, 'post', lambda fn: fn())
    assert session.handle_utterance(app, 'clean up')
    staged = session.pending()
    try:
        assert draft_review.show(app)
        bodies = app._ava_edit_preview.findChildren(QTextBrowser)
        assert [body.toPlainText() for body in bodies] == [OWNER, OWNER_FIXED]
        assert session.pending() is staged
        assert app._session_mode_manager.dictate_pending_buffer == OWNER
    finally:
        draft_review.close()


def test_changed_draft_refuses_the_larger_rewrite(app):
    assert session.handle_utterance(app, 'clean up')
    app._session_mode_manager._dictate_pending_buffer = 'New words.'
    assert not session.apply(app)['applied']
    assert app._session_mode_manager.dictate_pending_buffer == 'New words.'
