"""1213: memory-only casing at ordinary hold delivery boundaries.

Only Samsara's last dispatched hold is retained. No editor text, clipboard,
audio, or transcript is read or logged. A delivered sentence stop is final.
"""
from dataclasses import dataclass
import re
import time


EXPIRY_SECONDS = 90.0
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*", re.UNICODE)


@dataclass(frozen=True)
class Delivery:
    text: str
    target: tuple
    finished: float
    sequence: int


@dataclass(frozen=True)
class Pending:
    target: tuple
    sequence: int


def _target():
    """Window/control metadata only; UIA identity disambiguates web fields."""
    try:
        from samsara.clipboard import _paste_target, _user32
        import ctypes
        target = _paste_target()
        if not target or not all(target):
            return None
        name = ctypes.create_unicode_buffer(256)
        if not _user32.GetClassNameW(target[2], name, len(name)):
            return None
        if name.value.lower() == 'edit' or name.value.lower().startswith('richedit'):
            return target
        from samsara.uia import get_service
        service = get_service()
        def identity():
            control = service._get_backend().focused()
            if control is None or not control.element.CurrentHasKeyboardFocus:
                return None
            return tuple(control.element.GetRuntimeId())
        runtime_id = service._call(identity)
        return (*target, runtime_id) if runtime_id and _paste_target() == target else None
    except Exception:
        return None


def _sequence(app):
    # Existing per-capture sequence also fences intervening commands, empty
    # takes, parked holds and failures which never reach these two call sites.
    value = getattr(getattr(app, '_hold_evidence_1121', None), 'hold_seq_id', '')
    try:
        return int(value.split('-', 1)[0])
    except (ValueError, AttributeError):
        return None


def _enabled(app, verbatim, tutorial):
    config = app.config
    return (not verbatim and not tutorial
            and not getattr(app, '_skip_cleanup', False)
            and config.get('cleanup_mode', 'clean') == 'clean'
            and config.get('auto_capitalize', True)
            and config.get('mode', 'hold') == 'hold'
            and not any(getattr(app, name, False) for name in (
                'command_mode_recording', 'command_mode_active',
                'ava_mode_recording', '_memo_recording')))


def _unfinished(text):
    end = text.rstrip()
    # Closing quotes/brackets do not hide real sentence punctuation.
    end = end.rstrip('"\'”’)]}')
    return bool(end) and (end.endswith(('...', '…')) or end[-1] not in '.!?')


def _preserve(word, decoded):
    letters = ''.join(c for c in word if c.isalpha())
    if word.replace('’', "'").split("'", 1)[0] == 'I':
        return True
    if len(letters) > 1 and (letters.isupper() or any(c.isupper() for c in letters[1:])):
        return True
    # A name needs the decoder's own mid-sentence capitalization evidence.
    tokens = list(_WORD.finditer(decoded))
    return any(token[0] == word and token[0][0].isupper()
               and not re.search(r'[.!?]\s*["\'“‘(]*$', decoded[:token.start()])
               for token in tokens[1:])


def format_seam(previous, text, decoded):
    """Change only the undelivered hold; callers prove target/time continuity."""
    if not text:
        return text
    result = text
    # The previous punctuation is immutable. Remove only a duplicate new
    # separator (", ." / ". ,"), never a new question/exclamation mark.
    if previous.rstrip().endswith((',', '.', '…')):
        result = re.sub(r'^\s*[,.]+\s*', '', result)
    if not _unfinished(previous):
        if result != text and result and previous and not previous[-1].isspace():
            result = ' ' + result
        return result
    first = _WORD.search(result)
    if first and not result[:first.start()].strip(' \t"\'“‘([{'):
        if not _preserve(first[0], decoded):
            result = result[:first.start()] + first[0][:1].lower() + result[first.start()+1:]
    # Cleanup turned this take's trailing comma into a period. Restore the
    # comma only in an established continuation, before anything is sent.
    if decoded.rstrip().endswith(','):
        result = re.sub(r'(?<!\.)\.(\s*)$', r',\1', result)
    if result and previous and not previous[-1].isspace() and not result[0].isspace():
        result = ' ' + result
    return result


def prepare(app, text, decoded, *, verbatim=False, tutorial=False):
    """Final formatting before History/paste; absence of context is byte-neutral."""
    previous = getattr(app, '_hold_continuation_1213', None)
    app._hold_continuation_1213 = None
    app._hold_continuation_pending_1213 = None
    if not _enabled(app, verbatim, tutorial):
        return text
    target, sequence = _target(), _sequence(app)
    if target is None or sequence is None:
        return text
    app._hold_continuation_pending_1213 = Pending(target, sequence)
    started = getattr(getattr(app, '_speech_presence_capture', None), 'requested_at', None)
    if (not isinstance(previous, Delivery) or target != previous.target
            or sequence != previous.sequence + 1
            or not isinstance(started, (int, float))
            or not 0 <= started - previous.finished <= EXPIRY_SECONDS):
        return text
    return format_seam(previous.text, text, decoded)


def remember(app, text, delivered):
    """Remember only a successful dispatch still aimed at the prepared control."""
    pending = getattr(app, '_hold_continuation_pending_1213', None)
    app._hold_continuation_pending_1213 = None
    app._hold_continuation_1213 = None
    if (not isinstance(pending, Pending) or delivered is not True or not text
            or pending.target != _target()):
        return
    app._hold_continuation_1213 = Delivery(text, pending.target, time.perf_counter(), pending.sequence)
