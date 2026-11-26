"""Deterministic Markdown formatting. These transforms preserve content order."""
from __future__ import annotations

from itertools import count
import re


_NEWLINE = re.compile(r'(\r\n|\n|\r)')
_INDENT = re.compile(r'^[ \t]*')
_LIST_MARKER = re.compile(r'^([ \t]*)(?:[-+*•‣◦]|[0-9]{1,9}[.)])[ \t]+(?=\S)')
_QUOTE_MARKER = re.compile(r'^ {0,3}>(?:[ \t]|$)')
_SENTENCE_GAP = re.compile(r'''([.!?…]+)["'”’\)\]]*([ \t]+)(?=\S)''')
_ABBREVIATIONS = frozenset('dr mr mrs ms mx prof sr jr st mt rev gen sgt capt lt col vs etc no fig dept inc ltd approx e.g i.e a.m p.m'.split())
_DOTTED_WORD = re.compile(r'([\w.]+)$')
_INLINE_CODE = re.compile(r'(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)')


def _eol(text, default):
    match = _NEWLINE.search(text)
    return match[0] if match else default


def _items(text, newline):
    if _NEWLINE.search(text) or _LIST_MARKER.match(text):
        return text
    code = iter(_INLINE_CODE.finditer(text))
    inline = next(code, None)
    items, start = [], 0
    for gap in _SENTENCE_GAP.finditer(text):
        while inline and inline.end() <= gap.start():
            inline = next(code, None)
        if inline and inline.start() <= gap.start():
            continue
        if gap[1] == '.':
            word = _DOTTED_WORD.search(text, start, gap.start())
            if word and (word[0].lower() in _ABBREVIATIONS
                         or re.fullmatch(r'(?:[A-Za-z]\.)*[A-Za-z]', word[0])):
                continue
        if text[start:gap.start()].strip():
            items.append(text[start:gap.start(2)])
            start = gap.end(2)
    if not items:
        return text
    return newline.join([*items, text[start:]])


def _list(text, marker, newline):
    lines = _NEWLINE.split(_items(text, newline))
    for index in range(0, len(lines), 2):
        line = lines[index]
        if line.strip():
            match = _LIST_MARKER.match(line)
            indent = match[1] if match else _INDENT.match(line)[0]
            start = match.end() if match else len(indent)
            lines[index] = indent + marker() + line[start:]
    return ''.join(lines)


def to_bullets(text: str, *, newline: str = '\n') -> str:
    """Prefix each nonblank item with a Markdown bullet."""
    return _list(text, lambda: '- ', newline)


def to_numbered(text: str, *, newline: str = '\n') -> str:
    """Number nonblank items in their original order."""
    numbers = count(1)
    return _list(text, lambda: f'{next(numbers)}. ', newline)


def to_code_block(text: str, language: str = '', *, newline: str = '\n') -> str:
    """Wrap the literal body in a fence longer than any run of backticks."""
    if not text.strip():
        return text
    if language and not re.fullmatch(r'[\w+.#-]+', language):
        raise ValueError('Expected a single Markdown language identifier')
    fence = '`' * max(3, 1 + max(map(len, re.findall(r'`+', text)), default=0))
    eol = _eol(text, newline)
    trailing_break = text.endswith(('\n', '\r'))
    body = text if trailing_break else text + eol
    closing = fence + eol if trailing_break else fence
    return f'{fence}{language}{eol}{body}{closing}'


def to_quote(text: str, *, newline: str = '\n') -> str:
    """Wrap the existing lines of a passage in a Markdown quote."""
    if not text.strip():
        return text
    lines = _NEWLINE.split(text)
    for index in range(0, len(lines), 2):
        line = lines[index]
        if (line or index < len(lines) - 1) and not _QUOTE_MARKER.match(line):
            lines[index] = '> ' + line if line else '>'
    return ''.join(lines)


def strip_list_markers(text: str, *, newline: str = '\n') -> str:
    """Remove one list marker from each marked line, retaining its content."""
    lines = _NEWLINE.split(text)
    for index in range(0, len(lines), 2):
        lines[index] = _LIST_MARKER.sub(r'\1', lines[index], count=1)
    return ''.join(lines)


def editor_span(state, last_proof=None, last_text=''):
    """Use the current selection, or the unchanged end of a delivered range."""
    start, end = state[3:5]
    if start < end:
        return start, end
    if last_proof is None or state[:5] != last_proof[:5] or not last_text:
        return None
    payload = last_text.encode('utf-16-le')
    start = end - len(payload) // 2
    if start >= 0 and state[2][start * 2:end * 2] == payload:
        return start, end
    return None


def draft_span(document, span, pending, top):
    if pending and (coords := pending.get('selection')):
        from itertools import takewhile
        from samsara.session_corrections import _selection_text
        text = _selection_text(document, coords)
        if text is None:
            return None, True
        prefix = takewhile(lambda part: part.id != coords['segment_id'], document.segments)
        start = sum(len(part.text) for part in prefix) + int(coords['start'])
        return (start, start + len(text)), True
    if span[0] != span[1]:
        return span, True
    prior = top.extra.get('formatted_range') if top and top.kind == 'draft_edit_apply' else None
    if prior and prior[0] == (document.document_id, document.revision):
        return prior[1], False
    return document.latest_dictation_range, False


def rewrite_span(text, span, transform, *, newline='\n'):
    start, end = span
    selected = text[start:end]
    rewritten = transform(selected, newline=newline)
    if rewritten == selected or transform is strip_list_markers:
        return rewritten
    eol = _eol(text, newline)
    if start and text[start - 1] not in '\r\n' and not rewritten.startswith(('\r', '\n')):
        rewritten = eol + rewritten
    if end < len(text) and text[end] not in '\r\n' and not rewritten.endswith(('\r', '\n')):
        rewritten += eol
    return rewritten


def editor_rewrite(state, transform, last_proof, last_text):
    span = editor_span(state, last_proof, last_text)
    if span is None:
        return None
    start, end = span
    before = state[2][:start * 2].decode('utf-16-le')
    original = state[2][start * 2:end * 2].decode('utf-16-le')
    if not original.strip():
        return None
    after = state[2][end * 2:].decode('utf-16-le')
    rewritten = rewrite_span(before + original + after, (len(before), len(before) + len(original)),
                             transform, newline='\r\n')
    return start, end, original, rewritten


COMMANDS = {
    'make that a bulleted list': (to_bullets, ('bullet that',), 'Make a bulleted list'),
    'number that': (to_numbered, ('make that a numbered list',), 'Make a numbered list'),
    'make that a code block': (to_code_block, (), 'Wrap in a fenced code block'),
    'quote that': (to_quote, (), 'Wrap in a quote'),
    'remove the bullets': (strip_list_markers, (), 'Remove list markers'),
}
PHRASES = frozenset(phrase for name, (_, aliases, _) in COMMANDS.items() for phrase in (name, *aliases))
