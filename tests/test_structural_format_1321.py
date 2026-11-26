"""Structural-format commands through the real registry and scratch stack."""
from collections import Counter
import ctypes
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.commands import structural_format as format_plugin
from samsara import delivery_verify as dv, plugin_commands, structural_format as fmt
from samsara.ava_edit import selection
from samsara.commands import CommandExecutor
from samsara.headless import ResourcePort, ResourcePorts
from samsara.session_modes import (
    CommandDispatchResult, HandsFreeCommandMatch, SessionMode, SessionModeManager,
    StackItem, UtteranceSignals,
)


GOOD = UtteranceSignals(has_contiguous_speech=True, compression_ratios=(1.0,))
SOURCE = 'First 🍎 item!  Second item.\r\n\r\nThird item?\r\n'
NOTICE = 'Select some text or dictate first.'


@pytest.mark.parametrize('transform, undo_shape', [
    (fmt.to_bullets, fmt.strip_list_markers),
    (fmt.to_numbered, fmt.strip_list_markers),
    (fmt.to_code_block, lambda text: text.split('\r\n', 1)[1].rsplit('```', 1)[0]),
    (fmt.to_quote, lambda text: '\r\n'.join(line.removeprefix('>').removeprefix(' ')
                                            for line in text.split('\r\n'))),
])
def test_round_trip_preserves_content_order_and_punctuation(transform, undo_shape):
    original_words = SOURCE.split()
    actual = undo_shape(transform(SOURCE)).split()
    assert Counter(actual) == Counter(original_words)
    assert actual == original_words


@pytest.mark.parametrize('text,bullets', [
    ('One line', '- One line'),
    ('One. Two! Three?', '- One.\n- Two!\n- Three?'),
    ('See Dr. Abdi. Then go.', '- See Dr. Abdi.\n- Then go.'),
    ('Use `x. y`. Then leave.', '- Use `x. y`.\n- Then leave.'),
    ('“Go!” She left.', '- “Go!”\n- She left.'),
    ('One. Two.\nThree. Four.', '- One. Two.\n- Three. Four.'),
    ('\n\t \n', '\n\t \n'),
])
def test_line_and_sentence_boundaries(text, bullets):
    assert fmt.to_bullets(text) == bullets


def test_existing_markers_replace_once_and_keep_indentation():
    original = '+ Alpha!\r\n  8) Beta?\r\n\r\n\t• Gamma.\r\n'
    text = 'Alpha!\r\n  Beta?\r\n\r\n\tGamma.\r\n'
    assert fmt.strip_list_markers(original) == text
    assert fmt.to_bullets(original) == '- Alpha!\r\n  - Beta?\r\n\r\n\t- Gamma.\r\n'
    numbered = fmt.to_numbered(original)
    assert numbered == '1. Alpha!\r\n  2. Beta?\r\n\r\n\t3. Gamma.\r\n'
    assert fmt.to_numbered(numbered) == numbered
    assert fmt.to_quote('> One\nTwo') == '> One\n> Two'


def test_literal_code_and_language():
    assert fmt.to_code_block('  print("```")', 'python') == '````python\n  print("```")\n````'
    assert fmt.to_code_block('hello') == '```\nhello\n```'
    with pytest.raises(ValueError):
        fmt.to_code_block('hello', 'python\nhello')


class Editor:
    window = 42
    control = 999
    tick = 6
    process = 'editor.exe'
    style = 0x4
    limit = 32767
    native = True
    enabled = True
    interrupt_selection = False

    def __init__(self):
        self.set_text('')
        self.writes = []

    def set_text(self, text, span=None):
        self.contents = text.encode('utf-16-le')
        self.span = span if span is not None else (len(self.contents) // 2,) * 2

    @property
    def text(self):
        return self.contents.decode('utf-16-le')

    def snapshot(self):
        if self.enabled:
            return (self.window, self.control, self.contents, *self.span, self.tick), self.native

    def send(self, control, message, first=0, second=0):
        assert control == self.control
        if message == 0x00D5:
            return self.limit
        if message == 0x00B1:
            self.span = first, second
            if self.interrupt_selection:
                self.tick += 1
            return 0
        assert message == 0x00C2
        value = ctypes.wstring_at(second)
        start, end = self.span
        payload = value.encode('utf-16-le')
        self.contents = self.contents[:start * 2] + payload + self.contents[end * 2:]
        self.span = (start + len(payload) // 2,) * 2
        self.writes.append(value)


@pytest.fixture
def rig(monkeypatch):
    editor = Editor()
    monkeypatch.setattr(dv, 'snapshot', editor.snapshot)
    monkeypatch.setattr(dv, '_send', editor.send)
    monkeypatch.setattr(selection, '_window_class', lambda _hwnd: 'edit')
    api = SimpleNamespace(GetWindowLongW=lambda *_: editor.style, IsWindowUnicode=lambda _: True)
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(user32=api), raising=False)
    notices = []
    app = SimpleNamespace(config={}, _show_outcome_chip=lambda label, *_: notices.append(label))
    entries = {phrase: entry for entry in plugin_commands._MODULE_ENTRIES[format_plugin.__name__].values()
               for phrase in (entry['phrase'], *entry['aliases'])}
    for phrase, entry in entries.items():
        monkeypatch.setitem(plugin_commands._REGISTRY, phrase, entry)
    resources = ResourcePorts(ports={'plugin_registry': ResourcePort(entries)})
    executor = CommandExecutor(Path(__file__).parents[1] / 'commands.json', resources=resources)

    def dispatch(text):
        result = executor.process_text(text, app, force_commands=True)
        return CommandDispatchResult(result.claimed, result.phrase, result.state, detail=result.detail)

    def probe(text):
        phrase = executor.find_exact_command(text)
        return HandsFreeCommandMatch(text, phrase) if phrase else None

    manager = SessionModeManager(
        abort_phrases=[], foreground_exe_resolver=lambda: editor.process,
        foreground_hwnd_resolver=lambda: editor.window,
        inject_fn=lambda _: pytest.fail('unexpected dictation delivery'),
        remove_chars_fn=lambda _: pytest.fail('unexpected legacy deletion'),
        command_dispatch_fn=dispatch, agent_dispatch_fn=lambda *_: None,
        buffer_dictate_until_commit=True, hands_free_command_probe_fn=probe, fragment_clear_gap_s=2,
    )
    manager._require_unit_undo = True
    app._session_mode_manager = manager
    app.command_executor = executor
    manager.reset(SessionMode.COMMAND)

    def deliver(prefix, text):
        editor.set_text(prefix)
        before = editor.snapshot()[0]
        editor.set_text(prefix + text)
        after = editor.snapshot()[0]
        manager._stack.push(StackItem('dictation_chunk', text, SessionMode.DICTATE, 0, {
            'hwnd': editor.window, 'target_process': editor.process,
            'undo_unit': dv.ReadbackUnit(before, after, text),
        }))

    return SimpleNamespace(editor=editor, app=app, executor=executor, manager=manager, notices=notices,
                           deliver=deliver, say=lambda text: manager.dispatch_utterance(text, GOOD))


@pytest.mark.parametrize('phrase', sorted(fmt.PHRASES))
def test_phrases_are_whole_utterances(rig, phrase):
    assert rig.executor.find_exact_command(phrase.capitalize() + '.') in fmt.COMMANDS
    assert rig.executor.find_exact_command('please ' + phrase + ' for me') is None


@pytest.mark.parametrize('mode', [SessionMode.COMMAND, SessionMode.DICTATE])
def test_selection_wins_and_one_voice_undo_restores_exact_document(rig, mode):
    rig.manager.reset(mode)
    selected = '  Apple 🍏. Orange!'
    prefix = selected + '\r\n'
    rig.deliver(prefix, 'Last dictation.')
    rig.editor.span = 0, len(selected.encode('utf-16-le')) // 2
    assert rig.say('Bullet that.').detail['phrase'] == 'make that a bulleted list'
    assert rig.editor.text == '  - Apple 🍏.\r\n- Orange!\r\nLast dictation.'
    assert rig.say('scratch that').kind == 'scratch_success'
    assert rig.editor.text == prefix + 'Last dictation.'
    assert rig.editor.writes[-1] == selected


def test_last_dictation_can_be_numbered_and_restored(rig):
    original = 'Red.  Green! Blue?  '
    rig.deliver('Earlier.\r\n', original)
    rig.editor.tick += 1  # A voice button may change the last-input timestamp.
    assert rig.say('make that a numbered list').kind == 'command_executed'
    assert rig.editor.text == 'Earlier.\r\n1. Red.\r\n2. Green!\r\n3. Blue?  '
    assert rig.say('scratch that').kind == 'scratch_success'
    assert rig.editor.text == 'Earlier.\r\n' + original


@pytest.mark.parametrize('problem', ['empty', 'only spaces', 'different window', 'different control',
                                     'moved caret', 'read only', 'single line', 'too long', 'race',
                                     'unsupported selection'])
def test_refusal_never_writes_and_shows_one_line(rig, problem):
    editor = rig.editor
    if problem != 'empty':
        rig.deliver('  ', 'One. Two.')
    if problem == 'only spaces':
        editor.span = 0, 2
    elif problem == 'different window':
        editor.window += 1
    elif problem == 'different control':
        editor.control += 1
    elif problem == 'moved caret':
        editor.span = 2, 2
    elif problem == 'read only':
        editor.style |= 0x800
    elif problem == 'single line':
        editor.style = 0
    elif problem == 'too long':
        editor.limit = 12
    elif problem == 'race':
        editor.interrupt_selection = True
    elif problem == 'unsupported selection':
        editor.span = 0, 4
        editor.native = False
        rig.manager.draft_document.append('An unrelated draft.')
    original = editor.text
    assert rig.say('number that').kind == 'command_failed'
    assert editor.text == original and editor.writes == []
    assert rig.notices == [NOTICE]


def test_draft_only_formats_the_latest_segment_and_undo_restores_it(rig):
    rig.manager.reset(SessionMode.DICTATE)
    rig.editor.enabled = False
    rig.say('Earlier wording')
    rig.say('more wording')
    original = rig.manager.draft_document.text
    start, end = rig.manager.draft_document.latest_dictation_range
    assert start > 0
    assert rig.say('quote that').detail['phrase'] == 'quote that'
    assert rig.manager.draft_document.text == original[:start] + '\n> ' + original[start:end]
    assert not rig.editor.writes
    assert rig.say('scratch that').kind == 'scratch_success'
    assert rig.manager.draft_document.text == original


def test_formatting_uses_selected_draft_range_before_armed_correction(rig):
    from samsara.draft_selection import select_range, word_targets
    from samsara.session_corrections import request_word_correction

    rig.manager.reset(SessionMode.DICTATE)
    rig.editor.enabled = False
    rig.say('Alpha beta gamma')
    document = rig.manager.draft_document
    original = document.text
    targets = word_targets(document)
    coords = select_range(targets, 1, [(part.id, part.text) for part in document.segments])
    assert request_word_correction(rig.manager, coords['word'], selection=coords).get('ok')
    assert rig.say('bullet that').kind == 'command_executed'
    assert document.text == 'Alpha \n- beta\n gamma'
    assert rig.say('scratch that').kind == 'scratch_success'
    assert rig.manager.draft_document.text == original
