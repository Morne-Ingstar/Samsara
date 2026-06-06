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

# Optional shared matcher. When set by a CommandExecutor at startup, find_command
# delegates to its longest-match algorithm so standalone callers (e.g. external
# helpers) see the same routing as the executor's own dispatch.
_shared_matcher = None


def set_shared_matcher(matcher):
    """Register a CommandMatcher so find_command can delegate to it."""
    global _shared_matcher
    _shared_matcher = matcher


def clear_shared_matcher():
    """Drop the shared matcher reference (used by tests)."""
    global _shared_matcher
    _shared_matcher = None


def command(phrase, aliases=None, pack='core', debounce=0.0, app_overrides=None,
            ai_visible=True,
            risk_class='safe', ai_composable=False, side_effects=None,
            preconditions=None, voice_triggerable=True, param_schema=None,
            reversible=False, preview_template='',
            side_effect_category=None):
    """Decorator: register a function as a voice command.

    The decorated function is called as `func(app, remainder)` where `remainder`
    is text after the matched phrase (empty string if none). Return True if the
    command handled the input, False to fall through to the next handler.

    Args:
        phrase: primary trigger phrase
        aliases: list of alternative trigger phrases
        pack: command pack this command belongs to (default 'core')
        debounce: seconds to suppress re-execution in command mode (0 = no debounce)
        app_overrides: dict mapping lowercase exe names to key strings or None.
            Example: {"code.exe": "ctrl+shift+n", "notepad.exe": None}
            None means the command is disabled in that app.
        ai_visible: if False, excluded from Ava's injected command list (default True)

        -- AI Config Assistant safety metadata (all optional, default to safe) --
        risk_class: 'safe' | 'reversible' | 'destructive' (default 'safe')
        ai_composable: if True, may be included in AI-generated macros.
            Defaults to FALSE -- explicit opt-in per ARC narrow-subset requirement.
        side_effects: list of side-effect category strings documenting what the
            command touches, e.g. ['audio', 'ui', 'keystrokes', 'file',
            'clipboard', 'launch', 'network', 'system'].
        side_effect_category: alias for side_effects; if both provided, side_effects
            takes precedence.
        preconditions: list of machine-checkable condition id strings that must hold
            before execution, e.g. ['no_unsaved_changes', 'expected_app'].
            Enforcement is a later phase; this field captures the requirement.
        voice_triggerable: if False, command must not fire from voice transcription.
            Destructive commands should set this False to require hotkey/UI. Default True.
        param_schema: dict mapping param names to constraint specs, e.g.
            {"level": {"type": "int", "min": 0, "max": 100, "required": True}}.
            Empty dict (default) means only free-text remainder is accepted.
        reversible: True if the command's effects can be undone (default False).
            Separate from risk_class -- a reversible command may still be destructive
            but have an undo path.
        preview_template: human-readable template describing what will happen, e.g.
            "Increase volume to {current+20}%". Empty string if not provided.
    """
    def decorator(func):
        resolved_side_effects = list(side_effects or side_effect_category or [])
        entry = {
            'func': func,
            'phrase': phrase.lower().strip(),
            'aliases': [a.lower().strip() for a in (aliases or [])],
            'source': getattr(func, '__module__', 'unknown'),
            'pack': pack,
            'debounce': float(debounce),
            'app_overrides': dict(app_overrides) if app_overrides else {},
            'ai_visible': bool(ai_visible),
            'risk_class': risk_class,
            'ai_composable': bool(ai_composable),
            'side_effects': resolved_side_effects,
            'preconditions': list(preconditions or []),
            'voice_triggerable': bool(voice_triggerable),
            'param_schema': dict(param_schema or {}),
            'reversible': bool(reversible),
            'preview_template': str(preview_template),
        }
        _REGISTRY[entry['phrase']] = entry
        for alias in entry['aliases']:
            _REGISTRY[alias] = entry
        return func
    return decorator


def find_command(text):
    """Return (entry, remainder) if text matches a registered plugin command, else (None, '').

    When a shared matcher is installed, defers to it so longest-match and
    built-in-priority rules apply. In that mode we still only return plugin
    matches -- builtin hits map to (None, '') because this function is
    plugin-scoped by contract.

    Otherwise falls back to the legacy standalone matching (exact / startswith /
    endswith / word-bounded middle) for callers that construct the registry
    without an executor.
    """
    if not text:
        return None, ''

    if _shared_matcher is not None:
        entry, remainder = _shared_matcher.match(text)
        if entry is None or entry.source != 'plugin':
            return None, ''
        # Reconstruct the legacy plugin-entry dict so callers that expect
        # {'func', 'phrase', 'aliases'} keep working unchanged.
        return {
            'func': entry.handler,
            'phrase': entry.phrase,
            'aliases': entry.aliases,
        }, remainder

    text_lower = text.lower().strip()

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
            'pack': entry.get('pack', 'core'),
            'ai_visible': entry.get('ai_visible', True),
            'risk_class': entry.get('risk_class', 'safe'),
            'ai_composable': entry.get('ai_composable', False),
            'side_effects': entry.get('side_effects', []),
            'side_effect_category': entry.get('side_effects', []),
            'preconditions': entry.get('preconditions', []),
            'voice_triggerable': entry.get('voice_triggerable', True),
            'param_schema': entry.get('param_schema', {}),
            'reversible': entry.get('reversible', False),
            'preview_template': entry.get('preview_template', ''),
        })
    return sorted(result, key=lambda x: x['phrase'])
