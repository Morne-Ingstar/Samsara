"""Small atomic favorites store; no profile access at import time."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

from samsara.paths import samsara_home_dir

_lock = threading.RLock()
MAX_ITEMS = 99


def normalize_target(value):
    """Accept absolute local/UNC paths or HTTP(S) sites, never shell commands."""
    value = str(value or '').strip().strip('"')
    if not value or any(ord(ch) < 32 for ch in value):
        raise ValueError('Give a file, folder, shortcut or website.')
    # Windows drive paths must be considered before URL schemes.
    if re.match(r'^[A-Za-z]:[\\/]', value) or value.startswith(('\\\\', '/')):
        return value
    value = os.path.expandvars(os.path.expanduser(value))
    if Path(value).is_absolute():
        return value
    if '://' not in value and re.match(r'^(?:localhost(?::\d+)?|[^\s/:]+\.[^\s/:]+)(?:[:/?#]|$)', value):
        value = 'https://' + value
    parts = urlsplit(value)
    if (parts.scheme.lower() in {'http', 'https'} and parts.hostname
            and not parts.username and not parts.password and not any(ch.isspace() for ch in value)):
        return value
    raise ValueError('Use an absolute path or an http or https website.')


def is_url(value):
    return value.lower().startswith(('https://', 'http://'))


def _name(value):
    value = str(value or '').strip()
    if not value or len(value) > 200 or any(ord(ch) < 32 for ch in value):
        raise ValueError('Use a name between 1 and 200 characters.')
    return value


class FavoritesStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else samsara_home_dir() / 'favorites.json'

    def load(self):
        with _lock:
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
            except FileNotFoundError:
                return []
            except (ValueError, OSError) as exc:
                raise ValueError('Could not read favorites. The saved file has been preserved.') from exc
            try:
                if not isinstance(data, dict) or data.get('version') != 1:
                    raise ValueError('version')
                items = data['items']
                if not isinstance(items, list) or len(items) > MAX_ITEMS:
                    raise ValueError('items')
                return [dict(name=_name(row['name']), target=normalize_target(row['target'])) for row in items]
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError('Could not read favorites. The saved file has been preserved.') from exc

    def _save(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='favorites-', suffix='.tmp', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(dict(version=1, items=items), stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def add(self, target, name=None):
        target = normalize_target(target)
        with _lock:
            items = self.load()
            for index, row in enumerate(items, 1):
                same = row['target'] == target if is_url(target) else os.path.normcase(row['target']) == os.path.normcase(target)
                if same:
                    return index, False
            if len(items) >= MAX_ITEMS:
                raise ValueError('The quick list holds 99 favorites. Remove one before adding another.')
            default = urlsplit(target).hostname if is_url(target) else target.rstrip('\\/').replace('\\', '/').rsplit('/', 1)[-1]
            items.append(dict(name=_name(name or default or target), target=target))
            self._save(items)
            return len(items), True

    def edit(self, number, *, name=None):
        with _lock:
            items = self.load()
            if not 1 <= number <= len(items):
                raise ValueError('Choose a favorite number from the list.')
            if name is None:
                del items[number - 1]
            else:
                items[number - 1]['name'] = _name(name)
            self._save(items)
            return items


def open_target(target):
    target = normalize_target(target)
    if is_url(target):
        # The web commands use Python's browser-open helper too.
        import webbrowser
        if not webbrowser.open(target):
            raise ValueError('Could not open this website. Check your default browser.')
    else:
        if not Path(target).exists():
            raise ValueError('This favorite is missing. Its file, folder or shortcut could not be found.')
        from samsara.platform import open_file_or_folder
        if not open_file_or_folder(Path(target)):
            raise ValueError('Could not open this favorite with its default application.')
