"""User-taught aliases for Ava's natural language interpretation.

Architecture: Python owns truth. The LLM consumes resolved context.
All phrase keys are stored and matched in lowercase — original casing is not preserved.
"""
import json
import os
import re
import threading
from datetime import datetime

_CORRECTIONS_PATH = os.path.join(
    os.path.expanduser('~'), '.samsara', 'ava_corrections.json'
)
_aliases = {}
_aliases_lock = threading.Lock()
_dirty_count = False

MAX_ALIASES = 100
MAX_EXPANSION_LEN = 200
MAX_PHRASE_LEN = 50
_DIRTY_FLUSH_THRESHOLD = 10

TEACHING_PATTERNS = [
    re.compile(r'^(?:hey ava,?\s+)?when (?:i|I) say (.+?) (?:i|I) mean (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?remember that (.+?) means (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?remember (.+?) means (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?let\'?s call (.+?) the (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?from now on (.+?) is (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?(.+?) means (.+)$', re.IGNORECASE),  # lowest priority
]

FORGET_PATTERNS = [
    re.compile(r'^(?:hey ava,?\s+)?forget (.+)$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?(?:ava\s+)?delete (?:alias|correction) (.+)$', re.IGNORECASE),
]

QUERY_PATTERNS = [
    re.compile(r'^(?:hey ava,?\s+)?what does (.+?) mean\??$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?what is (.+?)\??$', re.IGNORECASE),
]

LIST_PATTERNS = [
    re.compile(r'^(?:hey ava,?\s+)?list (?:my )?aliases$', re.IGNORECASE),
    re.compile(r'^(?:hey ava,?\s+)?what have (?:i|I) taught you\??$', re.IGNORECASE),
]


def _load():
    global _aliases
    try:
        with open(_CORRECTIONS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
            _aliases = data.get('aliases', {})
    except Exception:
        _aliases = {}


def _save():
    os.makedirs(os.path.dirname(_CORRECTIONS_PATH), exist_ok=True)
    tmp = _CORRECTIONS_PATH + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'aliases': _aliases}, f, indent=2)
        os.replace(tmp, _CORRECTIONS_PATH)
    except Exception as e:
        print(f"[AVA CORRECTIONS] Save failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass


def flush_pending():
    """Called on app shutdown to persist accumulated use_count changes."""
    global _dirty_count
    with _aliases_lock:
        if _dirty_count:
            _save()
            _dirty_count = False


def parse_teaching(text):
    """Return (phrase, expansion) if text is a teaching command, else None."""
    text = text.strip()
    for pattern in TEACHING_PATTERNS:
        m = pattern.match(text)
        if m:
            phrase = m.group(1).strip().lower()
            expansion = m.group(2).strip()
            if 0 < len(phrase) <= MAX_PHRASE_LEN and 0 < len(expansion) <= MAX_EXPANSION_LEN:
                return (phrase, expansion)
    return None


def parse_forget(text):
    """Return phrase if text is a forget command, else None."""
    text = text.strip()
    for pattern in FORGET_PATTERNS:
        m = pattern.match(text)
        if m:
            return m.group(1).strip().lower()
    return None


def parse_query(text):
    """Return phrase if text is a query command, else None."""
    text = text.strip()
    for pattern in QUERY_PATTERNS:
        m = pattern.match(text)
        if m:
            return m.group(1).strip().lower()
    return None


def is_list_request(text):
    text = text.strip()
    return any(p.match(text) for p in LIST_PATTERNS)


def add(phrase, expansion):
    """Returns ('added', None) | ('replaced', old_expansion) | ('rejected', reason)."""
    phrase = phrase.strip().lower()
    if not phrase or len(phrase) > MAX_PHRASE_LEN:
        return ('rejected', 'invalid phrase')
    if not expansion or len(expansion) > MAX_EXPANSION_LEN:
        return ('rejected', 'invalid expansion')
    with _aliases_lock:
        if len(_aliases) >= MAX_ALIASES and phrase not in _aliases:
            return ('rejected', 'alias limit reached')
        old = _aliases.get(phrase)
        _aliases[phrase] = {
            'expansion': expansion,
            'created': datetime.utcnow().isoformat() + 'Z',
            'use_count': old['use_count'] if old else 0,
        }
        _save()
        return ('replaced', old['expansion']) if old else ('added', None)


def remove(phrase):
    phrase = phrase.strip().lower()
    with _aliases_lock:
        if phrase in _aliases:
            del _aliases[phrase]
            _save()
            return True
        return False


def get(phrase):
    with _aliases_lock:
        return _aliases.get(phrase.strip().lower())


def increment_use(phrase):
    """In-memory only. Flushed to disk on shutdown."""
    global _dirty_count
    with _aliases_lock:
        if phrase.lower() in _aliases:
            _aliases[phrase.lower()]['use_count'] += 1
            _dirty_count = True


def build_context_section():
    """Returns the USER-DEFINED ALIASES block for the system prompt, or empty string."""
    with _aliases_lock:
        if not _aliases:
            return ''
        lines = [
            'USER-DEFINED ALIASES:',
            'The user has taught you the following terminology:',
        ]
        for phrase, data in _aliases.items():
            lines.append(f'- "{phrase}" means: {data["expansion"]}')
        lines.append('')
        lines.append('When the user uses these terms, interpret them according to these')
        lines.append('aliases. Do not substitute them blindly -- apply them in context.')
        return '\n'.join(lines)


def list_top(n=5):
    """Returns sorted list of (phrase, expansion, use_count) by use_count desc."""
    with _aliases_lock:
        items = [(p, d['expansion'], d['use_count']) for p, d in _aliases.items()]
    items.sort(key=lambda x: x[2], reverse=True)
    return items[:n]


def total_count():
    with _aliases_lock:
        return len(_aliases)


def all_phrases():
    """Returns list of all alias phrase keys. Each is already lowercase."""
    with _aliases_lock:
        return list(_aliases.keys())


_load()
