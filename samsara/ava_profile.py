"""Static personal context Ava retains about the user.

Architecture mirrors ava_corrections.py: Python owns truth, LLM consumes
injected context block. Profile is persisted immediately on every write.
"""
import json
import os
import re
import threading
import time

from samsara.log import get_logger
from samsara.paths import samsara_home_dir

logger = get_logger(__name__)

_PROFILE_PATH = str(samsara_home_dir() / "ava_profile.json")
_profile = {}
_updated = {}
_profile_lock = threading.Lock()
_last_save_error = "the profile file could not be written"

MAX_FIELD_LEN = 200
MAX_NOTES_LEN = 500
DEFAULT_CONTEXT_MAX_CHARS = 600

KNOWN_FIELDS = (
    'name', 'name_pronunciation', 'answer_length', 'pace', 'repeat_back',
    'ask_before_long_answers', 'interests', 'location', 'pronouns',
    'occupation', 'notes',
)
COMMUNICATION_FIELDS = ('answer_length', 'pace', 'repeat_back', 'ask_before_long_answers')

_LABELS = {
    'name': 'Preferred name', 'name_pronunciation': 'Name pronunciation',
    'interests': 'Interests', 'answer_length': 'Answer length',
    'pace': 'Speaking pace', 'repeat_back': 'Repeat back',
    'ask_before_long_answers': 'Ask before long answers', 'location': 'Location',
    'pronouns': 'Pronouns', 'occupation': 'Occupation', 'notes': 'Notes',
}

# ---------------------------------------------------------------------------
# Teaching patterns — tried in order; first match wins.
#
# Explicit forms only (queue 57, 2026-09-14). The old broad fall-throughs
# stored ordinary speech as facts without asking: "I'm lonely." became the
# user's name, and "I'm in pain" / "I'm a bit tired" matched location and
# occupation. A pattern belongs here only if nobody says it about a feeling
# or state -- anything looser must go to the model, never straight to disk.
# Removed: "i am X" / "i'm X" (name), "i'm in X" (location),
# "i'm a/an X" / "i am a/an X" (occupation).
# ---------------------------------------------------------------------------

_P = re.IGNORECASE
_AVA = r'(?:hey ava,?\s+)?'
_DOT = r'\.?$'

_TEACHING_PATTERNS = [
    # Name pronunciation must precede the broad name pattern.
    (re.compile(rf'^{_AVA}my name is pronounced (.+?){_DOT}', _P), 'name_pronunciation'),
    (re.compile(rf'^{_AVA}pronounce my name (.+?){_DOT}', _P), 'name_pronunciation'),
    # name
    (re.compile(rf'^{_AVA}my name is (.+?){_DOT}', _P), 'name'),
    (re.compile(rf'^{_AVA}call me (.+?){_DOT}', _P), 'name'),
    # location
    (re.compile(rf'^{_AVA}i live in (.+?){_DOT}', _P), 'location'),
    (re.compile(rf"^{_AVA}i'?m from (.+?){_DOT}", _P), 'location'),
    (re.compile(rf'^{_AVA}my location is (.+?){_DOT}', _P), 'location'),
    # pronouns
    (re.compile(rf'^{_AVA}my pronouns are (.+?){_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}use (.+?) pronouns for me{_DOT}', _P), 'pronouns'),
    # occupation
    (re.compile(rf'^{_AVA}i work as (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}my job is (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}i do (.+?) for work{_DOT}', _P), 'occupation'),
    # Interests and explicit communication preferences.
    (re.compile(rf"^{_AVA}i'?m into (.+?){_DOT}", _P), 'interests'),
    (re.compile(rf'^{_AVA}my interests are (.+?){_DOT}', _P), 'interests'),
    (re.compile(rf'^{_AVA}(?:keep answers|keep your answers) (short|normal|detailed){_DOT}', _P), 'answer_length'),
    (re.compile(rf'^{_AVA}i prefer (short|normal|detailed) answers{_DOT}', _P), 'answer_length'),
    (re.compile(rf'^{_AVA}keep (?:things|it) (short|normal|detailed){_DOT}', _P), 'answer_length'),
    (re.compile(rf'^{_AVA}(?:speak|talk) (slowly|at a normal pace|quickly){_DOT}', _P), 'pace'),
    (re.compile(rf'^{_AVA}(?:please )?repeat (?:that|long answers) back to me{_DOT}', _P), 'repeat_back_yes'),
    (re.compile(rf"^{_AVA}(?:don'?t|do not) repeat (?:that|long answers) back to me{_DOT}", _P), 'repeat_back_no'),
    (re.compile(rf"^{_AVA}(?:don'?t|do not) repeat instructions i know{_DOT}", _P), 'repeat_back_no'),
    (re.compile(rf'^{_AVA}ask before long answers{_DOT}', _P), 'ask_before_long_answers_yes'),
    (re.compile(rf"^{_AVA}(?:don'?t|do not) ask before long answers{_DOT}", _P), 'ask_before_long_answers_no'),
    # notes (free-form append)
    (re.compile(rf'^{_AVA}remember about me that (.+?){_DOT}', _P), 'notes'),
    (re.compile(rf'^{_AVA}note about me[:\s]+(.+?){_DOT}', _P), 'notes'),
    (re.compile(rf'^{_AVA}about me[:\s]+(.+?){_DOT}', _P), 'notes'),
]

# ---------------------------------------------------------------------------
# Forget patterns
# ---------------------------------------------------------------------------

_FORGET_PATTERNS = [
    (re.compile(rf'^{_AVA}forget what you know about me{_DOT}', _P), 'all'),
    (re.compile(rf'^{_AVA}forget my name{_DOT}', _P), 'name'),
    (re.compile(rf'^{_AVA}forget how to pronounce my name{_DOT}', _P), 'name_pronunciation'),
    (re.compile(rf'^{_AVA}forget my interests{_DOT}', _P), 'interests'),
    (re.compile(rf'^{_AVA}forget my answer length preference{_DOT}', _P), 'answer_length'),
    (re.compile(rf'^{_AVA}forget my pace preference{_DOT}', _P), 'pace'),
    (re.compile(rf'^{_AVA}forget my repetition preference{_DOT}', _P), 'repeat_back'),
    (re.compile(rf'^{_AVA}forget (?:that i )?know those instructions{_DOT}', _P), 'repeat_back'),
    # Voice correction of a wrongly stored name (queue 57).
    (re.compile(rf"^{_AVA}that(?:'s| is) not my name{_DOT}", _P), 'name'),
    (re.compile(rf"^{_AVA}(?:don'?t|do not) call me that{_DOT}", _P), 'name'),
    (re.compile(rf'^{_AVA}forget my location{_DOT}', _P), 'location'),
    (re.compile(rf'^{_AVA}forget my pronouns{_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}forget my occupation{_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}forget my notes{_DOT}', _P), 'notes'),
]

# ---------------------------------------------------------------------------
# Query patterns
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = [
    (re.compile(rf'^{_AVA}what do you (?:know|remember) about me\??{_DOT}', _P), 'all'),
    (re.compile(rf"^{_AVA}what'?s my name\??{_DOT}", _P), 'name'),
    (re.compile(rf'^{_AVA}where do i live\??{_DOT}', _P), 'location'),
    (re.compile(rf'^{_AVA}what are my pronouns\??{_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}what(?:\'s| is) my occupation\??{_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}what(?:\'s| is) my job\??{_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}how do you talk to me\??{_DOT}', _P), 'communication'),
    (re.compile(rf'^{_AVA}what are my communication preferences\??{_DOT}', _P), 'communication'),
]

# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

def _load():
    global _profile, _updated
    try:
        with open(_PROFILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            _profile = data.get('profile', {}) if isinstance(data, dict) else {}
            _updated = data.get('updated', {}) if isinstance(data, dict) else {}
            if not isinstance(_profile, dict):
                _profile = {}
            if not isinstance(_updated, dict):
                _updated = {}
    except Exception:
        _profile, _updated = {}, {}


def _save_locked():
    """Persist to disk and return whether the write actually succeeded.

    Caller MUST hold ``_profile_lock``.  Mutating callers roll their state
    back when this is false, so a spoken receipt never outruns the file.
    """
    global _last_save_error
    tmp = _PROFILE_PATH + '.tmp'
    try:
        os.makedirs(os.path.dirname(_PROFILE_PATH), exist_ok=True)
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'profile': _profile, 'updated': _updated}, f, indent=2)
        os.replace(tmp, _PROFILE_PATH)
        _last_save_error = ''
        return True
    except Exception as e:
        _last_save_error = ('the file is locked' if isinstance(e, PermissionError)
                            else 'the profile file could not be written')
        logger.error("[AVA PROFILE] Save failed: %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _validate_field(field, value):
    """Returns (cleaned_value, None) on success, (None, error_str) on failure."""
    if field not in KNOWN_FIELDS:
        return None, 'unknown field'
    if not isinstance(value, str):
        return None, 'invalid value'
    value = _CTRL_RE.sub('', value).strip()
    if not value:
        return None, 'empty value'
    if field == 'answer_length':
        value = value.lower()
        if value not in {'short', 'normal', 'detailed'}:
            return None, 'answer length must be short, normal, or detailed'
    if field == 'pace':
        value = {'slowly': 'slow', 'at a normal pace': 'normal', 'quickly': 'quick'}.get(
            value.lower(), value.lower())
        if value not in {'slow', 'normal', 'quick'}:
            return None, 'pace must be slow, normal, or quick'
    if field in {'repeat_back', 'ask_before_long_answers'}:
        value = value.lower()
        if value not in {'yes', 'no'}:
            return None, 'preference must be yes or no'
    limit = MAX_NOTES_LEN if field == 'notes' else MAX_FIELD_LEN
    if len(value) > limit:
        return None, f'value too long (max {limit} characters)'
    return value, None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_teaching(text):
    """Return (field, value) if text is a profile-teaching command, else None."""
    text = text.strip()
    for pattern, field in _TEACHING_PATTERNS:
        m = pattern.match(text)
        if m:
            if field == 'repeat_back_yes':
                return ('repeat_back', 'yes')
            if field == 'repeat_back_no':
                return ('repeat_back', 'no')
            if field == 'ask_before_long_answers_yes':
                return ('ask_before_long_answers', 'yes')
            if field == 'ask_before_long_answers_no':
                return ('ask_before_long_answers', 'no')
            value = m.group(1).strip()
            if value:
                return (field, value)
    return None


def parse_forget(text):
    """Return field name, 'all', or None."""
    text = text.strip()
    for pattern, field in _FORGET_PATTERNS:
        if pattern.match(text):
            return field
    return None


def parse_query(text):
    """Return field name, 'all', or None."""
    text = text.strip()
    for pattern, field in _QUERY_PATTERNS:
        if pattern.match(text):
            return field
    return None


def set_field(field, value):
    """Write a profile field.

    Returns ('set'|'appended', stored_value), ('rejected', reason), or
    ('failed', reason). Notes append with a ' | ' separator.
    """
    cleaned, err = _validate_field(field, value)
    if err:
        return ('rejected', err)

    with _profile_lock:
        previous_profile, previous_updated = dict(_profile), dict(_updated)
        if field == 'notes' and _profile.get('notes'):
            _profile['notes'] = _profile['notes'] + ' | ' + cleaned
            result = 'appended'
        else:
            _profile[field] = cleaned
            result = 'set'
        _updated[field] = time.time_ns()
        if not _save_locked():
            _profile.clear()
            _profile.update(previous_profile)
            _updated.clear()
            _updated.update(previous_updated)
            return ('failed', _last_save_error)
    return (result, cleaned)


def clear_field(field):
    """Remove a field, reporting absence and disk failure distinctly."""
    with _profile_lock:
        if field not in _profile:
            return ('missing', None)
        previous_profile, previous_updated = dict(_profile), dict(_updated)
        removed = _profile.pop(field)
        _updated.pop(field, None)
        if not _save_locked():
            _profile.clear()
            _profile.update(previous_profile)
            _updated.clear()
            _updated.update(previous_updated)
            return ('failed', _last_save_error)
    return ('cleared', removed)


def clear_all():
    """Wipe the profile only when the deletion reached disk."""
    with _profile_lock:
        if not _profile:
            return ('empty', None)
        previous_profile, previous_updated = dict(_profile), dict(_updated)
        count = len(_profile)
        _profile.clear()
        _updated.clear()
        if not _save_locked():
            _profile.update(previous_profile)
            _updated.update(previous_updated)
            return ('failed', _last_save_error)
    return ('cleared', count)


def get(field):
    """Return field value or None."""
    with _profile_lock:
        return _profile.get(field)


def get_all():
    """Return a shallow copy of the profile dict."""
    with _profile_lock:
        return dict(_profile)


def get_communication_preferences():
    """Return only the explicit response-style preferences."""
    with _profile_lock:
        return {field: _profile[field] for field in COMMUNICATION_FIELDS if field in _profile}


def field_label(field):
    return _LABELS.get(field, field.replace('_', ' '))


def build_context_section(max_chars=DEFAULT_CONTEXT_MAX_CHARS):
    """Build a capped block: name, preferences, then most-recent other facts."""
    try:
        max_chars = max(0, int(max_chars))
    except (TypeError, ValueError):
        max_chars = DEFAULT_CONTEXT_MAX_CHARS
    with _profile_lock:
        data = dict(_profile)
        updated = dict(_updated)

    if not data or max_chars < len('ABOUT THE USER:'):
        return ''
    lines = ['ABOUT THE USER:']
    priority = ['name', 'name_pronunciation', *COMMUNICATION_FIELDS]
    other_fields = [field for field in data if field not in priority]
    other_fields.sort(key=lambda field: updated.get(field, 0), reverse=True)
    for field in [*priority, *other_fields]:
        if field in data:
            candidate = '\n'.join([*lines, f'- {field_label(field)}: {data[field]}'])
            if len(candidate) <= max_chars:
                lines.append(f'- {field_label(field)}: {data[field]}')
    return '\n'.join(lines) if len(lines) > 1 else ''


_load()
