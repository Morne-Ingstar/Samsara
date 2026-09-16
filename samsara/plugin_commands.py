"""
Plugin-based voice command system for Samsara.

Commands register themselves via @command decorator. The registry is scanned
by find_command() using the same matching rules as the JSON command system
(exact, start, end, word-bounded middle). execute_command() invokes the
function with the app instance and any remainder text after the matched phrase.

Plugins live in plugins/commands/*.py. Drop a file in, it's loaded at startup.

Module identity: load_plugins() imports each file ONCE under its canonical
package name (plugins/commands/ask_ollama.py -> plugins.commands.ask_ollama)
and registers it in sys.modules, so discovery and an ordinary
`from plugins.commands import ask_ollama` share one module object and one
copy of its module-global state. Import must be side-effect free apart from
@command registration; background work belongs in an optional module-level
`start_services(app)` hook, run once per module by start_plugin_services().

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

import importlib
import importlib.util
import logging
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Global registry: phrase -> {func, aliases, source}
# Aliases are also keys in the registry (pointing to the same entry) for O(1) lookup.
_REGISTRY = {}

# Module objects discovery has loaded, by resolved file path, and the modules
# whose start_services(app) hook has already run.
_LOADED_MODULES = {}
_STARTED_SERVICES = set()
# Entries each module's decorators produced, by module name, so a reused
# module can re-install its commands after the registry was cleared.
_MODULE_ENTRIES = {}
_SERVICES_LOCK = threading.Lock()

# Marks a metadata argument the author did not pass, so the registry can
# report it as unknown instead of as the decorator's default.
_UNSET = object()

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
            ai_visible=_UNSET,
            risk_class=_UNSET, ai_composable=_UNSET, side_effects=_UNSET,
            preconditions=_UNSET, voice_triggerable=_UNSET, param_schema=_UNSET,
            reversible=_UNSET, preview_template=_UNSET,
            side_effect_category=_UNSET, scope=None):
    """Decorator: register a function as a voice command.

    The decorated function is called as `func(app, remainder)` where `remainder`
    is the ORIGINAL text after the matched phrase (empty string if none) --
    case, quotes and punctuation intact; only a trailing sentence terminator
    is dropped. The return value is adapted by
    command_registry.adapt_handler_return:
        True                     -> completed
        None                     -> queued (work scheduled; never a miss)
        False                    -> declined: fall through as if unmatched
        a DispatchState / Result -> exactly that state (e.g. FAILED)
    Raising is a FAILED command -- never re-offered as dictation.

    Args:
        phrase: primary trigger phrase
        aliases: list of alternative trigger phrases
        pack: command pack this command belongs to (default 'core')
        debounce: seconds to suppress re-execution in command mode (0 = no debounce)
        app_overrides: dict mapping lowercase exe names to key strings or None.
            Example: {"code.exe": "ctrl+shift+n", "notepad.exe": None}
            None means the command is disabled in that app.
        ai_visible: if False, excluded from Ava's injected command list (default True)

        -- AI Config Assistant safety metadata (all optional) --
        Whatever is passed is kept verbatim in entry['metadata']; anything not
        passed is recorded there as 'unknown' (never as safe). The flat keys
        below keep their historical defaults for existing readers.
        risk_class: 'safe' | 'reversible' | 'destructive' (flat default 'safe')
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
        scope: when the command is a candidate (queue 68, samsara.command_scope):
            {"apps": ["obsidian.exe"], "title": r"regex", "tags": ["window_cube.visible"]}.
            Omitted = global (live everywhere). A malformed scope raises here,
            at import, so it can never silently scope a command out.
    """
    from samsara.command_registry import UNKNOWN  # noqa: PLC0415 -- no import cycle at module load
    from samsara.command_scope import parse_scope  # noqa: PLC0415
    parsed_scope = parse_scope(scope)

    if side_effects is _UNSET:
        side_effects = side_effect_category
    declared = {
        'ai_visible': ai_visible,
        'risk_class': risk_class,
        'ai_composable': ai_composable,
        'side_effects': side_effects,
        'preconditions': preconditions,
        'voice_triggerable': voice_triggerable,
        'param_schema': param_schema,
        'reversible': reversible,
        'preview_template': preview_template,
    }
    metadata = {name: (UNKNOWN if value is _UNSET else value)
                for name, value in declared.items()}

    def _given(value, default):
        return default if value is _UNSET else value

    def decorator(func):
        entry = {
            'func': func,
            'phrase': phrase.lower().strip(),
            'aliases': [a.lower().strip() for a in (aliases or [])],
            'source': getattr(func, '__module__', 'unknown'),
            'pack': pack,
            'debounce': float(debounce),
            'app_overrides': dict(app_overrides) if app_overrides else {},
            'ai_visible': bool(_given(ai_visible, True)),
            'risk_class': _given(risk_class, 'safe'),
            'ai_composable': bool(_given(ai_composable, False)),
            'side_effects': list(_given(side_effects, None) or []),
            'preconditions': list(_given(preconditions, None) or []),
            'voice_triggerable': bool(_given(voice_triggerable, True)),
            'param_schema': dict(_given(param_schema, None) or {}),
            'reversible': bool(_given(reversible, False)),
            'preview_template': str(_given(preview_template, '')),
            'metadata': dict(metadata),
            'scope': parsed_scope,
        }
        _register(entry)
        return func
    return decorator


def _register(entry):
    """Install one entry under its phrase and aliases, idempotently.

    Re-running the same decorator (importlib.reload, or a module executed
    twice) replaces that command's previous entry -- including aliases it no
    longer declares -- instead of leaving a stale duplicate behind.
    """
    func = entry['func']
    identity = (entry['source'], getattr(func, '__qualname__', None))
    previous = _REGISTRY.get(entry['phrase'])
    if previous is not None and previous is not entry:
        prev_identity = (previous['source'], getattr(previous['func'], '__qualname__', None))
        if prev_identity == identity:
            for key in [k for k, v in _REGISTRY.items() if v is previous]:
                del _REGISTRY[key]
        else:
            logger.warning("[PLUGINS] Phrase %r from %s replaces the one from %s",
                           entry['phrase'], entry['source'], previous['source'])
    _REGISTRY[entry['phrase']] = entry
    for alias in entry['aliases']:
        _REGISTRY[alias] = entry
    produced = _MODULE_ENTRIES.setdefault(entry['source'], {})
    produced[entry['phrase']] = entry


def _reinstall_module_commands(module):
    """Put back a reused module's commands whose phrase is no longer
    registered (e.g. a test cleared _REGISTRY). Never displaces a phrase
    some other registration currently owns."""
    for phrase, entry in list(_MODULE_ENTRIES.get(module.__name__, {}).items()):
        if phrase not in _REGISTRY:
            _register(entry)


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
    # Queue 68: the legacy path honours scopes too (tags only -- it has no
    # foreground provider, so app-scoped commands are not candidates here).
    from samsara.command_scope import MatchContext, UNRESOLVED_NO_PROVIDER, active_tags, scope_live  # noqa: PLC0415
    context = MatchContext.unresolved(UNRESOLVED_NO_PROVIDER, active_tags())

    def _live(entry):
        return scope_live(entry.get('scope'), context)[0]

    if text_lower in _REGISTRY:
        entry = _REGISTRY[text_lower]
        return (entry, '') if _live(entry) else (None, '')

    # Longest-phrase-first prevents "open" matching before "open browser"
    for phrase in sorted(_REGISTRY, key=len, reverse=True):
        entry = _REGISTRY[phrase]
        if not _live(entry):
            continue

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
    """Find and execute a command matching text. Returns (phrase, success) or (None, False).

    success follows command_registry.adapt_handler_return: completed or
    queued (a None-returning async handler) is success; a decline is not.
    """
    from samsara.command_registry import DispatchState, adapt_handler_return  # noqa: PLC0415

    entry, remainder = find_command(text)
    if entry is None:
        return None, False

    try:
        state = adapt_handler_return(entry['func'](app, remainder))
        return entry['phrase'], state in (DispatchState.COMPLETED, DispatchState.QUEUED)
    except Exception as e:
        logger.exception(f"Plugin command '{entry['phrase']}' failed: {e}")
        return entry['phrase'], False


def _canonical_module_name(py_file):
    """Dotted import name under which importing yields exactly py_file, or
    None when it is not importable from sys.path.

    Parent directories may be regular or namespace packages (plugins/ has no
    __init__.py). The longest candidate wins -- plugins.commands.x rather
    than commands.x should both resolve -- and each candidate is confirmed
    with find_spec, so a name is only used when it really maps to this file.
    """
    py_file = Path(py_file).resolve()
    roots = set()
    for entry in sys.path:
        try:
            roots.add(Path(entry or '.').resolve())
        except (OSError, ValueError):
            continue
    if not py_file.stem.isidentifier():
        return None
    candidates = []
    parts = [py_file.stem]
    directory = py_file.parent
    while True:
        if directory in roots:
            candidates.append('.'.join(parts))
        if directory.parent == directory or not directory.name.isidentifier():
            break
        parts.insert(0, directory.name)
        directory = directory.parent
    for name in reversed(candidates):
        existing = sys.modules.get(name)
        if existing is not None:
            if _same_file(existing, py_file):
                return name
            continue
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is not None and spec.origin and Path(spec.origin).resolve() == py_file:
            return name
    return None


def _same_file(module, py_file):
    try:
        return Path(module.__file__).resolve() == Path(py_file).resolve()
    except (AttributeError, TypeError, OSError):
        return False


def _import_plugin(py_file):
    """Import one plugin file exactly once and return its module.

    Canonical package name when there is one (so `import plugins.commands.x`
    elsewhere yields this same object); otherwise the historical
    `samsara_plugin_<stem>` name -- registered in sys.modules either way, and
    the legacy name is kept as an alias of the canonical module for code that
    looks plugins up by it.
    """
    py_file = Path(py_file)
    resolved = py_file.resolve()
    legacy_name = f"samsara_plugin_{py_file.stem}"
    name = _canonical_module_name(py_file)

    module = None
    if name is not None:
        module = sys.modules.get(name) or importlib.import_module(name)
    if module is None:
        existing = sys.modules.get(legacy_name)
        if existing is not None and _same_file(existing, py_file):
            module = existing
        else:
            spec = importlib.util.spec_from_file_location(legacy_name, py_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[legacy_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(legacy_name, None)
                raise
    elif sys.modules.get(legacy_name) is not module:
        sys.modules[legacy_name] = module

    _reinstall_module_commands(module)
    _LOADED_MODULES[str(resolved)] = module
    return module


def start_plugin_services(app):
    """Run each loaded plugin's optional `start_services(app)` hook once.

    This is the explicit app call that replaces import-time side effects
    (ask_ollama's health monitor used to start as soon as the module was
    executed -- once per module copy). Idempotent per module object.
    """
    with _SERVICES_LOCK:
        pending = [m for m in _LOADED_MODULES.values()
                   if id(m) not in _STARTED_SERVICES
                   and callable(getattr(m, 'start_services', None))]
        _STARTED_SERVICES.update(id(m) for m in pending)
    for module in pending:
        try:
            module.start_services(app)
        except Exception as e:
            logger.exception(f"Plugin {module.__name__} start_services failed: {e}")


def load_plugins(plugins_dir):
    """Auto-load every .py file in plugins_dir. Imports trigger @command decorators.

    Safe to call repeatedly: a plugin already imported (by discovery or by a
    plain canonical import) is reused, never executed a second time.
    """
    plugins_path = Path(plugins_dir)
    if not plugins_path.exists():
        logger.info(f"Plugin directory does not exist, skipping: {plugins_path}")
        return 0

    import time as _time
    loaded = 0
    for py_file in plugins_path.glob("*.py"):
        if py_file.name.startswith('_'):
            continue  # skip __init__.py, private files
        _pt0 = _time.monotonic()
        try:
            _import_plugin(py_file)
            loaded += 1
            _pms = (_time.monotonic() - _pt0) * 1000
            if _pms > 50:
                print(f"[BOOT] plugin {py_file.name}: {_pms:.0f}ms  *** SLOW ***")
            else:
                logger.info(f"Loaded plugin: {py_file.name} ({_pms:.0f}ms)")
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
            'metadata': dict(entry.get('metadata') or {}),
        })
    return sorted(result, key=lambda x: x['phrase'])
