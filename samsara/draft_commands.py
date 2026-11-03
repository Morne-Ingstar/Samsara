"""1041: exact hands-free text commands belong to the pending document.

No keyboard delivery or policy timer lives here. App-scoped commands and
explicit selection/Word requests retain their existing dispatcher. Coordinates
are invalidated on every document revision made outside this editor.
"""
from __future__ import annotations

from copy import deepcopy
import re


INSERTIONS = {
    'period': '.', 'full stop': '.', 'comma': ',', 'question mark': '?',
    'exclamation mark': '!', 'exclamation point': '!', 'colon': ':',
    'semicolon': ';', 'apostrophe': "'", 'quote': '"', 'open quote': '"',
    'close quote': '"', 'open parenthesis': '(', 'close parenthesis': ')',
    'open bracket': '[', 'close bracket': ']', 'hyphen': '-', 'dash': ' - ',
    'ellipsis': '...', 'new line': '\n', 'new paragraph': '\n\n', 'space': ' ',
}
CASE_COMMANDS = {
    'cap that': str.capitalize, 'capitalize that': str.capitalize,
    'capitalise that': str.capitalize, 'uppercase that': str.upper,
    'lowercase that': str.lower, 'title case that': str.title,
}
LOCAL_COMMANDS = frozenset(INSERTIONS) | frozenset(CASE_COMMANDS) | {
    'backspace', 'delete word', 'delete next word', 'delete line', 'delete',
    'delete selection', 'delete that', 'undo', 'redo', 'select all',
    'select that', 'select last word', 'select previous word', 'select word',
    'select word left', 'select word right', 'select sentence', 'select line',
    'select paragraph', 'select to start', 'select to end',
    'select to document start', 'select to document end', 'unselect', 'deselect',
    'go to document start', 'go to document end', 'word left', 'word right',
    'select previous', 'insert',
}


def _app(manager):
    return getattr(manager._agent_dispatch_fn, '__self__', None)


def _active(manager):
    from samsara.session_modes import SessionMode
    top = manager._stack.peek()
    return (manager.mode is SessionMode.DICTATE
            and manager._buffer_dictate_until_commit
            and (bool(manager._draft.text)
                 or (top is not None and top.kind == 'draft_edit_apply'
                     and top.payload == manager._draft.text
                     and getattr(manager, '_closed_empty_draft', None) != (
                         manager._draft.document_id, manager._draft.revision)
                     and top.extra['document'].document_id == manager._draft.document_id)))


def _outcome(manager, phrase, *, message='', failed=False):
    from samsara.session_modes import DispatchOutcome
    # PLACEHOLDER COPY (1041): result shown by the existing command chip.
    return DispatchOutcome('hands_free_command_failed' if failed else 'hands_free_command_executed', {
        'phrase': phrase, 'target': 'draft', 'pending_chars': manager._draft.chars,
        'mode_retained': manager.mode, 'state': 'failed' if failed else 'completed',
        'message': message,
    })


def _editor(manager):
    editor = getattr(manager, '_draft_command_editor', None)
    key = (manager._draft.document_id, manager._draft.revision)
    if editor is None or editor.key != key:
        editor = _Editor(key, len(manager._draft.text))
        manager._draft_command_editor = editor
    return editor


class _Editor:
    def __init__(self, key, cursor):
        self.key, self.cursor, self.anchor = key, cursor, cursor

    @property
    def span(self):
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def move(self, position, *, select=False):
        self.cursor = position
        if not select:
            self.anchor = position

    def replace(self, manager, replacement, span=None):
        from samsara.session_modes import StackItem, SessionMode, chunk_ends_terminal
        text = manager._draft.text
        start, end = self.span if span is None else span
        rewritten = text[:start] + replacement + text[end:]
        if rewritten != text:
            before = deepcopy(manager._draft)
            manager._draft.replace_text(rewritten, edited=True)
            # Keep exactly the displayed text at delivery, even in legacy
            # embedders that installed only format_dictate_fn.
            manager._draft.delivery_prepared = True
            manager._stack.push(StackItem('draft_edit_apply', rewritten,
                SessionMode.DICTATE, manager._clock(), {'document': before}))
            manager._clear_draft_undo.drop()
            manager._scratch_redo = None
            manager._last_dictate_ended_terminal = chunk_ends_terminal(rewritten) if rewritten else None
        self.move(start + len(replacement))
        self.key = (manager._draft.document_id, manager._draft.revision)


def _word_left(text, cursor):
    match = re.search(r'\S+\s*$', text[:cursor])
    return match.start() if match else 0


def _word_right(text, cursor):
    match = re.match(r'\s*\S+\s*', text[cursor:])
    return cursor + match.end() if match else len(text)


def _unit(text, cursor, unit):
    if unit == 'word':
        start = _word_left(text, cursor)
        return start, len(text[:_word_right(text, start)].rstrip())
    if unit == 'sentence':
        ranges = [(m.start(), m.end()) for m in re.finditer(r'[^.!?]+[.!?]*\s*', text)]
        return next(((a, b) for a, b in ranges if a <= max(0, cursor - 1) < b), (0, len(text)))
    separator = '\n\n' if unit == 'paragraph' else '\n'
    start = text.rfind(separator, 0, cursor)
    end = text.find(separator, cursor)
    return (start + len(separator) if start >= 0 else 0,
            end if end >= 0 else len(text))


def dispatch_local_control(manager, text):
    """The small missing case vocabulary is a whole-utterance draft control.

    Existing catalog commands still enter through the pack/scope-aware probe.
    Argument-bearing commands are deliberately not recognized from prose.
    """
    spelling = getattr(_app(manager), '_draft_spelling_input', None)
    if spelling is not None and spelling.active:
        return None
    phrase = text.strip().lower().rstrip('.!?,;:')
    if phrase in CASE_COMMANDS and _active(manager):
        return dispatch_draft_command(manager, phrase, text)
    return None


def probe_text(manager, text):
    """Whisper's punctuation-only transcript still uses real pack admission."""
    spelling = getattr(_app(manager), '_draft_spelling_input', None)
    if _active(manager) and not (spelling is not None and spelling.active):
        from samsara.phonetic_wash import _SYMBOL_TO_WORD
        return _SYMBOL_TO_WORD.get(text.strip(), text)
    return text


def probe_hands_free(manager, text, signals, fragment_guard):
    """Admit local edits before the paced-prose guard, retaining audio gates."""
    from samsara.session_modes import (
        DispatchOutcome, passes_switch_anti_hallucination_gate, log,
    )
    from samsara.command_catalog import hands_free_media_phrases
    from samsara.log import describe_text
    if passes_switch_anti_hallucination_gate(signals):
        local = dispatch_local_control(manager, text)
        if local is not None:
            return None, local
    probe = manager._hands_free_command_probe_fn
    match = probe(probe_text(manager, text)) if probe is not None else None
    if match is not None:
        if (fragment_guard and match.dispatch_text not in hands_free_media_phrases()
                and not targets_draft(manager, match.phrase)):
            log.info("[SESSION] paced fragment kept as dictation: %r", describe_text(text))
            return None, manager._dispatch_dictate(text)
        if not passes_switch_anti_hallucination_gate(signals):
            return None, DispatchOutcome(kind="hands_free_command_refused", detail={
                "phrase": match.phrase, "pending_chars": len(manager._dictate_pending_buffer),
            })
    return match, None


def undo_local_edit(manager):
    """Return None for other stack items; never mutate a saved redo preimage."""
    from samsara.session_modes import chunk_ends_terminal
    top = manager._stack.peek()
    if top is None or top.kind != 'draft_edit_apply':
        return None
    before = deepcopy(top.extra['document'])
    if manager._draft.document_id != before.document_id or manager._draft.text != top.payload:
        return False
    before.revision = manager._draft.revision + 1
    manager._draft = before
    manager._last_dictate_ended_terminal = chunk_ends_terminal(before.text) if before.text else None
    manager._stack.pop()
    return True


def dispatch_draft_command(manager, phrase, text):
    """Return None only for an external command or when no draft is owned."""
    if not _active(manager):
        return None
    app = _app(manager)
    commands = getattr(getattr(app, 'command_executor', None), 'commands', {})
    metadata = commands.get(phrase, {})
    if metadata.get('scope'):
        return None  # Explicit app commands keep their app target.
    # Builtin/custom literal text commands can carry arbitrary symbol text.
    insertion = metadata.get('text') if metadata.get('type') == 'text' else INSERTIONS.get(phrase)
    if phrase == 'greet me':
        from plugins.commands.example_greet import GREETING_TEXT
        insertion = GREETING_TEXT
    if phrase not in LOCAL_COMMANDS and insertion is None:
        return None
    editor = _editor(manager)
    body = manager._draft.text
    start, end = editor.span
    if phrase in {'undo', 'delete that'}:
        if phrase == 'delete that' and start != end:
            editor.replace(manager, '')
            return _outcome(manager, phrase)
        ok = bool(manager._do_scratch_that_n(1)['scratched'])
        return _outcome(manager, phrase, failed=not ok)
    if phrase == 'redo':
        outcome = manager._redo_scratch_outcome()
        return _outcome(manager, phrase, failed=outcome.kind != 'scratch_success')
    if insertion is not None:
        span = None
        if start == end == len(body) and phrase in INSERTIONS and phrase != 'space':
            # Whisper often supplied a final period already. Explicit spoken
            # punctuation corrects it instead of producing '.?' or '.,'.
            trimmed = body.rstrip(' ')
            if insertion in {'.', ',', '?', '!', ':', ';', '...'}:
                trimmed = re.sub(r'(?<!\.)[.!?]$', '', trimmed)
            span = len(trimmed), len(body)
        editor.replace(manager, insertion, span)
    elif phrase in CASE_COMMANDS:
        if start == end:
            start, end = _unit(body, editor.cursor, 'word')
        editor.replace(manager, CASE_COMMANDS[phrase](body[start:end]), (start, end))
    elif phrase == 'insert':
        from samsara import snippets
        payload = _payload(app, text)
        match = re.match(r'(?:insert snippet|paste snippet|insert)\s+(.+)', payload, re.I | re.S)
        record = snippets.find_snippet(match[1].strip()) if match else None
        if record is None:
            return _outcome(manager, phrase, failed=True, message='No matching snippet')
        editor.replace(manager, record['text'])
    elif phrase.startswith('select ') or phrase == 'select all':
        if phrase == 'select all':
            start, end = 0, len(body)
        elif phrase == 'select that':
            top = manager._stack.peek()
            start, end = (len(body) - len(top.payload), len(body)) if (
                top is not None and top.kind == 'dictation_staged_chunk'
                and body.endswith(top.payload)) else (0, len(body))
        elif phrase in {'select word left', 'select last word', 'select previous word', 'select previous'}:
            count = 1
            if phrase == 'select previous':
                from samsara.spoken_numbers import parse_spoken_number
                match = re.search(r'select previous\s+(.+?)\s+words?\s*[.!?]?$', _payload(app, text), re.I)
                count = parse_spoken_number(match[1]) if match else None
                if count is None or not 1 <= count <= 100:
                    return _outcome(manager, phrase, failed=True, message='Choose 1 to 100 words')
            start, end = editor.cursor, editor.cursor
            for _ in range(count):
                start = _word_left(body, start)
        elif phrase == 'select word right':
            start, end = editor.cursor, len(body[:_word_right(body, editor.cursor)].rstrip())
        elif phrase in {'select word', 'select line', 'select sentence', 'select paragraph'}:
            start, end = _unit(body, editor.cursor, phrase.split()[1])
        else:
            target = (0 if phrase == 'select to document start' else len(body)
                      if phrase == 'select to document end' else
                      _unit(body, editor.cursor, 'line')[0 if phrase == 'select to start' else 1])
            start, end = editor.anchor, target
        editor.anchor, editor.cursor = start, end
    elif phrase in {'unselect', 'deselect', 'go to document start', 'go to document end', 'word left', 'word right'}:
        position = (0 if phrase == 'go to document start' else len(body)
                    if phrase == 'go to document end' else _word_left(body, editor.cursor)
                    if phrase == 'word left' else _word_right(body, editor.cursor)
                    if phrase == 'word right' else end)
        editor.move(position)
    else:
        if start == end:
            if phrase == 'backspace':
                start = max(0, start - 1)
            elif phrase == 'delete word':
                start = _word_left(body, start)
            elif phrase == 'delete next word':
                end = _word_right(body, end)
            elif phrase == 'delete line':
                start, end = _unit(body, editor.cursor, 'line')
            elif phrase == 'delete':
                end = min(len(body), end + 1)
        editor.replace(manager, '', (start, end))
    return _outcome(manager, phrase)


def targets_draft(manager, phrase):
    """Read-only check for the paced-fragment guard, before any mutation."""
    if phrase == 'draft spelling':
        return True  # The active replacement field owns every utterance.
    if not _active(manager):
        return False
    commands = getattr(getattr(_app(manager), 'command_executor', None), 'commands', {})
    metadata = commands.get(phrase, {})
    return not metadata.get('scope') and (phrase in LOCAL_COMMANDS or phrase == 'greet me'
                                         or metadata.get('type') == 'text')


def stage_at_cursor(manager, prepared):
    """Continue dictation at a locally selected span or an explicit separator."""
    editor = getattr(manager, '_draft_command_editor', None)
    if editor is None or editor.key != (manager._draft.document_id, manager._draft.revision):
        return None
    body = manager._draft.text
    if editor.span == (len(body), len(body)) and (not body or body[-1] not in ' \t\n([\"\''):
        return None
    from samsara.session_modes import DispatchOutcome
    editor.replace(manager, prepared.strip())
    return DispatchOutcome('dictate_staged', {'pending_chars': manager._draft.chars})


def _payload(app, text):
    config = getattr(app, 'config', {})
    prefix = (config.get('intent') or {}).get('command_prefix') or 'command'
    return re.sub(r'^\s*' + re.escape(prefix) + r'\s+', '', text, count=1, flags=re.I)
