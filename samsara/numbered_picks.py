"""Bounded voice choices for visible Show Numbers labels (queue 781)."""
from samsara.spoken_numbers import parse_spoken_number


def parse_pick(text):
    """Return the existing click handler's argument, only for a whole pick.

    Keep number words intact for existing probe consumers. Never search a
    sentence for a number: extra words make the entire utterance dictation.
    """
    words = text.lower().strip().rstrip('.!?,;:').replace('-', ' ').split()
    keys = [key for key in ('ctrl', 'shift', 'alt') if key in words]
    double = any(word in words for word in ('twice', 'double'))
    right = 'right' in words
    if double and right:
        return None
    words = [word for word in words
             if word not in {'ctrl', 'shift', 'alt', 'twice', 'double', 'right'}]
    if words and words[0] in {'click', 'tap', 'press', 'pick', 'choose'}:
        words = words[1:]
    if words and words[0] in {'number', 'num'}:
        words = words[1:]
    number_text = ' '.join(words)
    number = parse_spoken_number(number_text)
    if number is None or not 0 <= number <= 99:
        return None
    return ' '.join([number_text, *keys,
                     *(['twice'] if double else ['right'] if right else [])])


def pick_remainder(text, *, allow_grid=False):
    """Only visible labels own this grammar; the grid keeps its bare-number path."""
    from plugins.commands import show_numbers
    if not show_numbers.is_overlay_active():
        return None
    if show_numbers.grid_active():
        if not allow_grid:
            return None
        # Preserve the pre-existing hands-free grid spelling and vocabulary.
        text = text.lower().strip().rstrip('.!?,;:')
        words = text.split()
        if not words or not all(word.isdigit() or word in show_numbers._WORD_TO_NUM
                                for word in words):
            return None
        return text if show_numbers._parse_spoken_number(text) is not None else None
    return parse_pick(text)


def dispatch_session_pick(manager, text, signals):
    """Let labels keep ownership in Ava and unbuffered dictation as well."""
    from samsara.session_modes import (
        DispatchOutcome, SessionMode, match_literal_payload,
        passes_switch_anti_hallucination_gate,
    )
    if manager.mode is SessionMode.COMMAND or (
            manager.mode is SessionMode.DICTATE and manager._buffer_dictate_until_commit):
        return None  # These lanes already use the ordinary/hands-free dispatcher.
    if match_literal_payload(text) is not None or pick_remainder(text) is None:
        return None
    probe = manager._hands_free_command_probe_fn
    match = probe(text) if probe is not None else None
    if match is None or match.phrase != 'click':
        return None
    if not passes_switch_anti_hallucination_gate(signals):
        return DispatchOutcome(kind='hands_free_command_refused', detail={'phrase': 'click'})
    return manager._dispatch_command(match.dispatch_text)
