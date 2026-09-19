"""User-taught aliases for Ava's natural language interpretation.

Architecture: Python owns truth. The LLM consumes resolved context.
All phrase keys are stored and matched in lowercase — original casing is not preserved.
"""
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

from samsara.log import get_logger
from samsara.paths import quarantine_corrupt_file, samsara_home_dir

logger = get_logger(__name__)


def _corrections_path() -> Path:
    """Resolved lazily (not a module-level constant) so SAMSARA_HOME_DIR
    set after import (e.g. by a test fixture) is still honored -- see
    2026-07-16 test-isolation audit."""
    return samsara_home_dir() / "ava_corrections.json"


_aliases = {}
_aliases_lock = threading.Lock()
_dirty_count = False
_last_save_error = "the alias file could not be written"

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


def _read_aliases_file(path: Path) -> dict:
    """Read `path` and return its aliases dict WITHOUT touching the
    _aliases global -- shared by _load() (startup) and _save() (pre-write
    guard/delta read) so both use the identical quarantine-on-corrupt
    behavior. Missing file returns {}; a file that exists but fails to
    parse is quarantined (renamed aside, preserving the bytes) rather
    than silently treated as empty -- see 2026-07-16 correction-store
    hardening.
    """
    if not path.exists():
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('aliases', {}) if isinstance(data, dict) else {}
    except Exception as e:
        quarantine_corrupt_file(path, logger, e)
        return {}


def _load():
    global _aliases
    _aliases = _read_aliases_file(_corrections_path())


def _save(allow_empty: bool = False) -> bool:
    """Persist _aliases atomically. Returns True on success, False if
    refused or on write failure.

    Reads the previous on-disk state once (via _read_aliases_file, which
    also quarantines it if corrupt) and reuses that single read for both
    the empty-overwrite guard below and the success-log delta.

    allow_empty=False refuses to overwrite a non-empty on-disk store with
    an empty one (2026-07-09 loss pattern, same family of bug this store
    is also exposed to). remove() -- the only caller that can drive
    _aliases to {} -- is always a deliberate user "forget" action and
    passes allow_empty=True.
    """
    global _last_save_error
    path = _corrections_path()
    previous = _read_aliases_file(path)

    if not _aliases and previous and not allow_empty:
        _last_save_error = "the alias file could not be written"
        logger.error(
            f"[STORE] refused to overwrite {len(previous)} aliases with "
            f"empty dict -- pass allow_empty=True if intentional"
        )
        return False

    os.makedirs(path.parent, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_name(path.name + '.bak'))
        except OSError as e:
            logger.debug(f"[STORE] backup copy failed (non-fatal): {e}")

    tmp = str(path) + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump({'aliases': _aliases}, f, indent=2)
        os.replace(tmp, str(path))
    except Exception as e:
        _last_save_error = ('the file is locked' if isinstance(e, PermissionError)
                            else 'the alias file could not be written')
        logger.error(f"[AVA CORRECTIONS] Save failed: {e}")
        try:
            os.remove(tmp)
        except OSError as rm_exc:
            logger.debug(f"_save: {rm_exc}")
        return False

    _last_save_error = ''
    logger.info(f"[STORE] ava_corrections.json saved: {len(_aliases)} aliases")
    return True


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
    """Return an honest result after the alias has reached disk."""
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
        if not _save():
            if old is None:
                del _aliases[phrase]
            else:
                _aliases[phrase] = old
            return ('failed', _last_save_error)
        return ('replaced', old['expansion']) if old else ('added', None)


def remove(phrase):
    phrase = phrase.strip().lower()
    with _aliases_lock:
        if phrase in _aliases:
            old = _aliases.pop(phrase)
            # Deliberate user "forget X" action -- always allowed to drive
            # the store to empty (e.g. removing the last remaining alias).
            if not _save(allow_empty=True):
                _aliases[phrase] = old
                return ('failed', _last_save_error)
            return ('removed', old['expansion'])
        return ('missing', None)


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


def build_context_section(current_utterance='', recent_turns=(), max_chars=600):
    """Return only aliases relevant to this Ava turn, within ``max_chars``.

    The caller supplies the current utterance and at most its two preceding
    turns. Alias data stays local unless the local-provider path explicitly
    chooses to build this block.
    """
    try:
        max_chars = max(0, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 600
    spoken = ' '.join([current_utterance or '', *(recent_turns or ())]).lower()
    with _aliases_lock:
        if not _aliases:
            return ''
        relevant = [
            (phrase, data) for phrase, data in _aliases.items()
            if re.search(r'(?<!\w)' + re.escape(phrase) + r'(?!\w)', spoken)
        ]
    if not relevant or max_chars < len('USER-DEFINED ALIASES:'):
        return ''
    lines = ['USER-DEFINED ALIASES:']
    for phrase, data in relevant:
        line = f'- "{phrase}" means: {data["expansion"]}'
        candidate = '\n'.join([*lines, line])
        if len(candidate) <= max_chars:
            lines.append(line)
    return '\n'.join(lines) if len(lines) > 1 else ''


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
