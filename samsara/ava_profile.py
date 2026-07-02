"""Static personal context Ava retains about the user.

Architecture mirrors ava_corrections.py: Python owns truth, LLM consumes
injected context block. Profile is persisted immediately on every write.
"""
import json
import os
import re
import threading

from samsara.log import get_logger

logger = get_logger(__name__)

_PROFILE_PATH = os.path.join(
    os.path.expanduser('~'), '.samsara', 'ava_profile.json'
)
_profile = {}
_profile_lock = threading.Lock()

MAX_FIELD_LEN = 200
MAX_NOTES_LEN = 500

KNOWN_FIELDS = ('name', 'location', 'pronouns', 'occupation', 'notes')

# ---------------------------------------------------------------------------
# Teaching patterns — tried in order; first match wins.
# The "i am" / "i'm" name patterns are deliberately last (most broad).
# ---------------------------------------------------------------------------

_P = re.IGNORECASE
_AVA = r'(?:hey ava,?\s+)?'
_DOT = r'\.?$'

_TEACHING_PATTERNS = [
    # name (specific)
    (re.compile(rf'^{_AVA}my name is (.+?){_DOT}', _P), 'name'),
    (re.compile(rf'^{_AVA}call me (.+?){_DOT}', _P), 'name'),
    # location (all before broad name patterns)
    (re.compile(rf'^{_AVA}i live in (.+?){_DOT}', _P), 'location'),
    (re.compile(rf"^{_AVA}i'?m in (.+?){_DOT}", _P), 'location'),
    (re.compile(rf"^{_AVA}i'?m from (.+?){_DOT}", _P), 'location'),
    (re.compile(rf'^{_AVA}my location is (.+?){_DOT}', _P), 'location'),
    # pronouns
    (re.compile(rf'^{_AVA}my pronouns are (.+?){_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}use (.+?) pronouns for me{_DOT}', _P), 'pronouns'),
    # occupation (i'm a / i'm an before generic i'm)
    (re.compile(rf"^{_AVA}i'?m a (.+?){_DOT}", _P), 'occupation'),
    (re.compile(rf"^{_AVA}i'?m an (.+?){_DOT}", _P), 'occupation'),
    (re.compile(rf'^{_AVA}i am a (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}i am an (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}i work as (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}my job is (.+?){_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}i do (.+?) for work{_DOT}', _P), 'occupation'),
    # notes (free-form append)
    (re.compile(rf'^{_AVA}remember about me that (.+?){_DOT}', _P), 'notes'),
    (re.compile(rf'^{_AVA}note about me[:\s]+(.+?){_DOT}', _P), 'notes'),
    (re.compile(rf'^{_AVA}about me[:\s]+(.+?){_DOT}', _P), 'notes'),
    # name (broad — last)
    (re.compile(rf'^{_AVA}i am (.+?){_DOT}', _P), 'name'),
    (re.compile(rf"^{_AVA}i'?m (.+?){_DOT}", _P), 'name'),
]

# ---------------------------------------------------------------------------
# Forget patterns
# ---------------------------------------------------------------------------

_FORGET_PATTERNS = [
    (re.compile(rf'^{_AVA}forget what you know about me{_DOT}', _P), 'all'),
    (re.compile(rf'^{_AVA}forget my name{_DOT}', _P), 'name'),
    (re.compile(rf'^{_AVA}forget my location{_DOT}', _P), 'location'),
    (re.compile(rf'^{_AVA}forget my pronouns{_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}forget my occupation{_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}forget my notes{_DOT}', _P), 'notes'),
]

# ---------------------------------------------------------------------------
# Query patterns
# ---------------------------------------------------------------------------

_QUERY_PATTERNS = [
    (re.compile(rf'^{_AVA}what do you know about me\??{_DOT}', _P), 'all'),
    (re.compile(rf"^{_AVA}what'?s my name\??{_DOT}", _P), 'name'),
    (re.compile(rf'^{_AVA}where do i live\??{_DOT}', _P), 'location'),
    (re.compile(rf'^{_AVA}what are my pronouns\??{_DOT}', _P), 'pronouns'),
    (re.compile(rf'^{_AVA}what(?:\'s| is) my occupation\??{_DOT}', _P), 'occupation'),
    (re.compile(rf'^{_AVA}what(?:\'s| is) my job\??{_DOT}', _P), 'occupation'),
]

# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------

def _load():
    global _profile
    try:
        with open(_PROFILE_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            _profile = data.get('profile', {})
    except Exception:
        _profile = {}


def _save_locked():
    """Persist to disk. Caller MUST hold _profile_lock."""
    os.makedirs(os.path.dirname(_PROFILE_PATH), exist_ok=True)
    tmp = _PROFILE_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'profile': _profile}, f, indent=2)
        os.replace(tmp, _PROFILE_PATH)
    except Exception as e:
        print(f"[AVA PROFILE] Save failed: {e}")
        try:
            os.remove(tmp)
        except OSError as e:
            logger.debug(f"_save_locked: {e}")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')


def _validate_field(field, value):
    """Returns (cleaned_value, None) on success, (None, error_str) on failure."""
    value = _CTRL_RE.sub('', value).strip()
    if not value:
        return None, 'empty value'
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

    Returns ('set', None) | ('appended', None) | ('rejected', reason).
    Notes field appends with ' | ' separator instead of replacing.
    """
    cleaned, err = _validate_field(field, value)
    if err:
        return ('rejected', err)

    with _profile_lock:
        if field == 'notes' and _profile.get('notes'):
            _profile['notes'] = _profile['notes'] + ' | ' + cleaned
            _save_locked()
            return ('appended', None)
        _profile[field] = cleaned
        _save_locked()
    return ('set', None)


def clear_field(field):
    """Remove a single field. Returns True if it existed."""
    with _profile_lock:
        if field in _profile:
            del _profile[field]
            _save_locked()
            return True
    return False


def clear_all():
    """Wipe the entire profile."""
    with _profile_lock:
        _profile.clear()
        _save_locked()


def get(field):
    """Return field value or None."""
    with _profile_lock:
        return _profile.get(field)


def get_all():
    """Return a shallow copy of the profile dict."""
    with _profile_lock:
        return dict(_profile)


def build_context_section():
    """Return the ABOUT THE USER block for injection into the system prompt.

    Returns empty string when the profile is empty (fresh install).
    """
    with _profile_lock:
        data = dict(_profile)

    if not data:
        return ''

    _LABELS = {
        'name':       'Name',
        'location':   'Location',
        'pronouns':   'Pronouns',
        'occupation': 'Occupation',
        'notes':      'Notes',
    }

    lines = ['ABOUT THE USER:']
    for field in KNOWN_FIELDS:
        if field in data:
            lines.append(f'- {_LABELS[field]}: {data[field]}')

    lines.append('')
    lines.append(
        'Address the user by name when natural. Use this context when interpreting '
        'requests, but never bring up personal details unprompted unless directly '
        'relevant to what the user is asking.'
    )
    return '\n'.join(lines)


_load()
