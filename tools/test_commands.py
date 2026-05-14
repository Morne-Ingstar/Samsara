#!/usr/bin/env python3
"""
tools/test_commands.py  --  Samsara command routing test harness

Loads the matcher and registry exactly as dictation.py does at startup,
then validates every registered command without executing anything.

Checks per command:
  1. Structural validity (keys, target paths, method names, text fields, etc.)
  2. Matcher integrity  -- the canonical phrase resolves back to itself.
  3. Handler availability -- get_handler(type) returns a known handler.

Exit code: 0 if all commands are OK, 1 if any [BROKEN] entries exist.

Usage:
    F:\\envs\\sami\\python.exe tools/test_commands.py
"""

import contextlib
import io
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from pynput.keyboard import Key
from samsara.command_registry import CommandMatcher
from samsara.command_packs import get_enabled_packs, PACKS
from samsara import plugin_commands
from samsara.handlers import get_handler

# ---------------------------------------------------------------------------
# Key validation constants
# ---------------------------------------------------------------------------

# Names Samsara aliases in CommandExecutor.KEY_MAP (commands.py)
_SAMSARA_KEY_MAP = {
    'ctrl', 'shift', 'alt', 'win', 'enter', 'esc', 'space', 'tab',
    'backspace', 'delete', 'home', 'end', 'pageup', 'pagedown',
    'up', 'down', 'left', 'right',
    *[f'f{n}' for n in range(1, 13)],
}

# All pynput Key enum attributes
_PYNPUT_KEYS = {n.lower() for n in dir(Key) if not n.startswith('_')}

# Valid mouse actions in MouseHandler
_VALID_MOUSE_ACTIONS = {'click', 'double_click', 'right_click', 'scroll'}

# Handler types that don't need special field validation
_SIMPLE_TYPES = {'release_all'}


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _is_valid_key(k: str) -> bool:
    """True if k is a key string that Samsara / pynput can press."""
    kl = k.lower()
    if len(kl) == 1:
        return True          # single char — sent as a KeyCode character
    if kl in _SAMSARA_KEY_MAP:
        return True          # Samsara alias (maps to a Key enum value)
    if kl in _PYNPUT_KEYS:
        return True          # direct pynput Key attribute
    return False


def _validate_keys(keys) -> list:
    errors = []
    for k in (keys or []):
        if not _is_valid_key(k):
            errors.append(f"unknown key: '{k}'")
    return errors


def _validate_launch(target: str):
    """Return (status, detail) for a launch target string.

    status: 'ok' | 'broken' | 'manual'
    """
    if not target:
        return 'broken', 'empty target'
    if target.startswith(('http://', 'https://')):
        return 'ok', target
    if target.startswith('ms-settings:'):
        return 'manual', target          # cannot verify programmatically
    p = Path(target)
    if p.is_absolute() or ('\\' in target or '/' in target):
        return ('ok', target) if p.exists() else ('broken', f'not found: {target}')
    if shutil.which(target):
        return 'ok', f'on PATH: {target}'
    return 'broken', f'bare exe not on PATH: {target}'


def _validate_macro_step(step: dict) -> list:
    errors = []
    action = step.get('action')
    if action == 'hotkey':
        errors.extend(_validate_keys(step.get('keys', [])))
    elif action == 'press':
        key = step.get('key', '')
        if key:
            errors.extend(_validate_keys([key]))
        else:
            errors.append('press step missing key')
    elif action in ('delay', 'text', 'mouse'):
        pass  # these are always structurally OK at this validation level
    else:
        errors.append(f"unknown macro action: '{action}'")
    return errors


def app_has_method(name: str, dictation_src: str) -> bool:
    return f'def {name}(' in dictation_src


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    # ------------------------------------------------------------------
    # Load sources
    # ------------------------------------------------------------------
    dictation_src = (PROJECT_ROOT / 'dictation.py').read_text(encoding='utf-8')

    with open(PROJECT_ROOT / 'commands.json', encoding='utf-8') as f:
        builtin_cmds = json.load(f)['commands']

    config_path = PROJECT_ROOT / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    enabled_packs = get_enabled_packs(config)

    # ------------------------------------------------------------------
    # Load plugins + build matcher (suppress registry chatter)
    # ------------------------------------------------------------------
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        plugin_commands.load_plugins(str(PROJECT_ROOT / 'plugins' / 'commands'))
        matcher = CommandMatcher()
        matcher.load_builtins(builtin_cmds)
        matcher.load_plugins(plugin_commands._REGISTRY)
        matcher.freeze()

    # Deduplicated entry list
    unique_entries = list({id(e): e for e in matcher._entries.values()}.values())
    builtin_count = sum(1 for e in unique_entries if e.source == 'builtin')
    plugin_count  = sum(1 for e in unique_entries if e.source == 'plugin')
    total         = builtin_count + plugin_count

    # Phrases that exist in both builtins and plugins (shadow detection)
    plugin_phrases = {
        entry_data.get('phrase', phrase)
        for phrase, entry_data in plugin_commands._REGISTRY.items()
    }
    shadowed_by_builtin = plugin_phrases & set(builtin_cmds.keys())

    # Known handler types
    known_types = {
        'hotkey', 'press', 'key_down', 'key_up', 'release_all',
        'mouse', 'launch', 'text', 'macro', 'method',
    }

    # ------------------------------------------------------------------
    # Validate each unique entry
    # ------------------------------------------------------------------
    results = []  # (status, phrase, display_type, detail)

    seen_ids = set()
    for entry in matcher._sorted:
        if id(entry) in seen_ids:
            continue
        seen_ids.add(id(entry))

        phrase    = entry.phrase
        pack      = entry.pack
        enabled   = pack in enabled_packs
        cmd_type  = entry.cmd_type

        # --- matcher integrity (all packs enabled in this matcher) ---
        found, _ = matcher.match(phrase)
        if found is None:
            results.append(('broken', phrase, cmd_type,
                             'matcher returned no match for its own phrase'))
            continue
        if found.phrase != phrase:
            results.append(('shadow', phrase, cmd_type,
                             f'shadowed by: {found.phrase!r}'))
            continue

        # --- handler availability ---
        if cmd_type not in known_types and entry.source == 'builtin':
            # 'plugin' type in commands.json shadows the real plugin handler
            if cmd_type == 'plugin':
                results.append(('broken', phrase, cmd_type,
                                 "type 'plugin' in commands.json shadows the "
                                 "@command plugin; no builtin handler exists"))
            else:
                results.append(('broken', phrase, cmd_type,
                                 f'no handler registered for type: {cmd_type!r}'))
            continue

        # --- plugin source: no further structural checks needed ---
        if entry.source == 'plugin':
            mod = getattr(entry.handler, '__module__', '?')
            results.append(('ok', phrase, 'plugin', f'-> {mod}'))
            continue

        # --- builtin structural validation ---
        cmd = builtin_cmds.get(phrase, {})

        if cmd_type == 'hotkey':
            errs = _validate_keys(cmd.get('keys', []))
            if errs:
                results.append(('broken', phrase, cmd_type, '; '.join(errs)))
            else:
                results.append(('ok', phrase, cmd_type,
                                 ' + '.join(cmd.get('keys', []))))

        elif cmd_type in ('press', 'key_down', 'key_up'):
            key = cmd.get('key', '')
            errs = _validate_keys([key]) if key else ['missing key field']
            if errs:
                results.append(('broken', phrase, cmd_type, '; '.join(errs)))
            else:
                results.append(('ok', phrase, cmd_type, repr(key)))

        elif cmd_type == 'release_all':
            results.append(('ok', phrase, cmd_type, 'releases all held keys'))

        elif cmd_type == 'launch':
            target = cmd.get('target', '')
            status, detail = _validate_launch(target)
            results.append((status, phrase, cmd_type, detail))

        elif cmd_type == 'method':
            mname = cmd.get('method', '')
            if not mname:
                results.append(('broken', phrase, cmd_type, 'missing method field'))
            elif not app_has_method(mname, dictation_src):
                results.append(('broken', phrase, cmd_type,
                                 f'app has no method: {mname!r}'))
            else:
                results.append(('ok', phrase, cmd_type, f'-> {mname}'))

        elif cmd_type == 'text':
            text = cmd.get('text', '')
            if not text:
                results.append(('broken', phrase, cmd_type, 'empty text field'))
            else:
                preview = (text[:50] + '...') if len(text) > 50 else text
                results.append(('ok', phrase, cmd_type, repr(preview)))

        elif cmd_type == 'macro':
            step_errors = []
            for i, step in enumerate(cmd.get('steps', [])):
                errs = _validate_macro_step(step)
                if errs:
                    step_errors.append(f'step {i}: {"; ".join(errs)}')
            if step_errors:
                results.append(('broken', phrase, cmd_type,
                                 ' | '.join(step_errors)))
            else:
                n = len(cmd.get('steps', []))
                results.append(('ok', phrase, cmd_type, f'{n} steps'))

        elif cmd_type == 'mouse':
            action = cmd.get('action', '')
            if not action:
                results.append(('broken', phrase, cmd_type, 'missing action field'))
            elif action not in _VALID_MOUSE_ACTIONS:
                results.append(('?', phrase, cmd_type,
                                 f'unrecognized action: {action!r}'))
            else:
                results.append(('ok', phrase, cmd_type, action))

        else:
            results.append(('?', phrase, cmd_type,
                             f'unrecognized command type: {cmd_type!r}'))

    # ------------------------------------------------------------------
    # Print output
    # ------------------------------------------------------------------
    SEP = '=' * 70
    _STATUS_ORDER = {'broken': 0, 'shadow': 1, '?': 2, 'manual': 3, 'ok': 4}
    results.sort(key=lambda r: (_STATUS_ORDER.get(r[0], 5), r[1]))

    counts = {k: 0 for k in ('ok', 'broken', 'shadow', '?', 'manual')}
    for status, *_ in results:
        counts[status] = counts.get(status, 0) + 1

    _TAG = {
        'ok':     '[OK]    ',
        'broken': '[BROKEN]',
        'shadow': '[SHADOW]',
        '?':      '[?]     ',
        'manual': '[MANUAL]',
    }

    print(SEP)
    print('COMMAND ROUTING TEST HARNESS')
    print(SEP)
    print(f'Loaded: {builtin_count} builtin commands, {plugin_count} plugin commands, {total} total')
    print(f'Enabled packs: {", ".join(sorted(enabled_packs))}')
    print()

    for status, phrase, cmd_type, detail in results:
        tag = _TAG.get(status, '[?]     ')
        print(f'{tag}  {phrase:<38} {cmd_type:<14} {detail}')

    print()
    print(SEP)
    parts = [
        f'{counts["ok"]} ok',
        f'{counts["broken"]} broken',
        f'{counts["shadow"]} shadowed',
        f'{counts["?"]} weird',
        f'{counts["manual"]} manual-check',
    ]
    print(f'SUMMARY: {", ".join(parts)}')
    print(SEP)

    return 1 if counts['broken'] > 0 else 0


if __name__ == '__main__':
    sys.exit(main())
