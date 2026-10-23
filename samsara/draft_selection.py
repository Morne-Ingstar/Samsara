"""Revision-bound word ranges shared by the Full and Compact draft displays."""
from __future__ import annotations

import re


WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)


def word_targets(document):
    targets, counts = [], {}
    for segment in document.segments:
        for match in WORD.finditer(segment.text):
            word = match.group()
            folded = word.casefold()
            occurrence = counts.get(folded, 0)
            counts[folded] = occurrence + 1
            targets.append(dict(document_id=document.document_id, revision=document.revision,
                segment_id=segment.id, start=match.start(), end=match.end(),
                word=word, occurrence=occurrence))
    return targets


def select_range(targets, index, segments, current=None, *, extend=False):
    """Extend from the original anchor; stale anchors start a fresh selection."""
    if not 0 <= index < len(targets):
        return None
    target = targets[index]
    same = current and all(current.get(k) == target.get(k) for k in ('document_id', 'revision'))
    anchor = index
    if extend and same:
        anchor = int(current.get('anchor_index', next((i for i, t in enumerate(targets)
            if t['segment_id'] == current.get('segment_id') and t['start'] == current.get('start')), index)))
    if not 0 <= anchor < len(targets):
        anchor = index
    low, high = sorted((anchor, index))
    first, last = targets[low], targets[high]
    selected = dict(first, end=last['end'], end_segment_id=last['segment_id'],
                    anchor_index=anchor, first_index=low, last_index=high)
    pieces, inside = [], False
    for segment_id, text in segments:
        if segment_id == first['segment_id']:
            inside = True
            text = text[first['start']:]
            if segment_id == last['segment_id']:
                text = text[:last['end'] - first['start']]
        elif segment_id == last['segment_id']:
            text = text[:last['end']]
        if inside:
            pieces.append(text)
        if segment_id == last['segment_id']:
            break
    selected['word'] = ''.join(pieces)
    # Keep the proposal span monotonic across segments. The second endpoint
    # retains its own local offset for the authoritative document edit.
    if first['segment_id'] != last['segment_id']:
        selected['end_segment_offset'] = last['end']
        selected['end'] = first['start'] + len(selected['word'])
    return selected


def highlighted(index, selection):
    return selection is not None and selection.get('first_index', -1) <= index <= selection.get('last_index', -1)


def common_phrase(text):
    words = WORD.findall(text.casefold())
    common = set('a an and are as at be but by for from he her his i in is it its me my '
                 'not of on or our she so that the their them then there these they this '
                 'to up us was we were what when who will with you your'.split())
    return len(words) > 1 and all(word in common for word in words)


def compact_word_clicked(preview, index, extend=False):
    """Resolve Compact's rendered index against the authoritative document."""
    if preview._closed or not 0 <= index < len(preview._words):
        return
    manager = preview._manager()
    if manager is None:
        return
    document = manager.draft_document
    targets = word_targets(document)
    if [t['word'] for t in targets] != [w for w, _ in preview._words]:
        return  # A decode has arrived since this transcript was painted.
    current = getattr(preview, '_span_selection', None) if manager.pending_word_correction() else None
    selection = select_range(targets, index, [(s.id, s.text) for s in document.segments],
                             current, extend=extend)
    word = selection['word']
    phrase = selection['first_index'] != selection['last_index']
    # Retain the existing single-word/audio path; phrases carry exact coordinates.
    if phrase:
        from samsara.session_corrections import request_word_correction
        result = request_word_correction(manager, word, selection=selection, source='preview_click')
    else:
        result = manager.request_word_correction(word, targets[index]['occurrence'],
            expected_count=sum(t['word'].casefold() == word.casefold() for t in targets),
            source='preview_click')
    if not result.get('ok'):
        return
    selection['revision'] = document.revision
    preview._span_selection = selection
    preview._correction_armed = True
    candidates = preview._correction_candidates(word)
    preview._show_correction_choices(word, [word] + candidates, no_match=not candidates)
    overlay = preview._overlay
    wrapper = getattr(overlay, '_widget', overlay)
    widget = getattr(wrapper, '_w', None)
    if widget is not None:
        from samsara.ui import theme
        # Rewrite only anchor styles in the current transcript: badges and partials stay intact.
        text = widget._label.text()
        text = re.sub(r'background-color:[^;"\']+;', '', text)
        def paint(match):
            number = int(match.group(1))
            style = f'background-color:{theme.ACCENT_DIM};' if highlighted(number, selection) else ''
            return f'<a href="w:{number}" style="{style}color:{theme.TEXT_PRIMARY};text-decoration:none;">'
        widget._label.setText(re.sub(r'<a href="w:(\d+)"[^>]*>', paint, text))


def compact_link_clicked(widget, href, prefix):
    if widget._on_word_clicked is None or not href.startswith(prefix):
        return
    try:
        index = int(href[len(prefix):])
    except ValueError:
        return
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
        widget._on_word_clicked(index, True)
    else:
        widget._on_word_clicked(index)


def clear_compact_selection(preview):
    preview._span_selection = None
    wrapper = getattr(preview._overlay, '_widget', preview._overlay)
    widget = getattr(wrapper, '_w', None)
    if widget is not None:
        widget._label.setText(re.sub(r'background-color:[^;"\']+;', '', widget._label.text()))
