"""
Plugin-based voice command system for Samsara.

Commands register themselves via @command decorator. The registry is scanned
by find_command() using the same matching rules as the JSON command system
(exact, start, end, word-bounded middle). execute_command() invokes the
function with the app instance and any remainder text after the matched phrase.

Plugins live in plugins/commands/*.py. Drop a file in, it's loaded at startup.

Example plugin:

    from samsara.plugin_commands import command

    @command("open browser", aliases=["launch browser", "start browser"])
    def open_browser(app, remainder):
        import webbrowser
        webbrowser.open("https://duckduckgo.com")
        return True

    @command("search for")
    def search(app, remainder):
        # remainder = "cats" when user says "search for cats"
        if not remainder:
            return False
        import webbrowser, urllib.parse
        webbrowser.open(f"https://duckduckgo.com/?q={urllib.parse.quote(remainder)}")
        return True
"""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Global registry: phrase -> {func, aliases, source}
# Aliases are also keys in the registry (pointing to the same entry) for O(1) lookup.
_REGISTRY = {}


def command(phrase, aliases=None):
    """Decorator: register a function as a voice command.

    The decorated function is called as `func(app, remainder)` where `remainder`
    is text after the matched phrase (empty string if none). Return True if the
    command handled the input, False to fall through to the next handler.
    """
    def decorator(func):
        entry = {
            'func': func,
            'phrase': phrase.lower().strip(),
            'aliases': [a.lower().strip() for a in (aliases or [])],
            'source': getattr(func, '__module__', 'unknown'),
        }
        _REGISTRY[entry['phrase']] = entry
        for alias in entry['aliases']:
            _REGISTRY[alias] = entry
        return func
    return decorator


def find_command(text):
    """Return (entry, remainder) if text matches a registered command, else (None, '').

    Matching rules (same as the JSON command system):
      - exact match
      - text starts with phrase (remainder = rest of text)
      - text ends with phrase (remainder = text before phrase)
      - phrase appears as whole-word match in middle (remainder = text minus phrase)
    """
    if not text:
        return None, ''
    text_lower = text.lower().strip()

    # Exact match
    if text_lower in _REGISTRY:
        return _REGISTRY[text_lower], ''

    # Longest-phrase-first prevents "open" matching before "open browser"
    for phrase in sorted(_REGISTRY, key=len, reverse=True):
        entry = _REGISTRY[phrase]

        if text_lower.startswith(phrase + ' '):
            return entry, text[len(phrase):].strip()
        if text_lower.startswith(phrase):
            return entry, text[len(phrase):].strip()
        if text_lower.endswith(' ' + phrase):
            return entry, text[:-len(phrase)].strip()
        if f' {phrase} ' in f' {text_lower} ':
            idx = text_lower.find(phrase)
            remainder = (text[:idx] + ' ' + text[idx + len(phrase):]).strip()
            return entry, remainder

    return None, ''


def execute_command(text, app=None):
    """Find and execute a command matching text. Returns (phrase, success) or (None, False)."""
    entry, remainder = find_command(text)
    if entry is None:
        return None, False

    try:
        result = entry['func'](app, remainder)
        return entry['phrase'], bool(result)
    except Exception as e:
        logger.exception(f"Plugin command '{entry['phrase']}' failed: {e}")
        return entry['phrase'], False


def load_plugins(plugins_dir):
    """Auto-load every .py file in plugins_dir. Imports trigger @command decorators."""
    plugins_path = Path(plugins_dir)
    if not plugins_path.exists():
        logger.info(f"Plugin directory does not exist, skipping: {plugins_path}")
        return 0

    loaded = 0
    for py_file in plugins_path.glob("*.py"):
        if py_file.name.startswith('_'):
            continue  # skip __init__.py, private files
        try:
            spec = importlib.util.spec_from_file_location(
                f"samsara_plugin_{py_file.stem}", py_file
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded += 1
            logger.info(f"Loaded plugin: {py_file.name}")
        except Exception as e:
            logger.exception(f"Failed to load plugin {py_file.name}: {e}")

    unique_commands = len({id(e) for e in _REGISTRY.values()})
    logger.info(f"Plugin system: {loaded} file(s), {unique_commands} command(s) registered")
    return loaded


def list_commands():
    """Return a sorted list of unique registered commands (for debug / listing)."""
    seen = set()
    result = []
    for phrase, entry in _REGISTRY.items():
        if id(entry) in seen:
            continue
        seen.add(id(entry))
        result.append({
            'phrase': entry['phrase'],
            'aliases': entry['aliases'],
            'source': entry['source'],
        })
    return sorted(result, key=lambda x: x['phrase'])
