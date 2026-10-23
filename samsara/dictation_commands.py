"""415: dictation owns argument-bearing speech; explicit command lanes keep it."""
import re

from .config_defaults import cfg_get

KEY = 'command_mode.dictation_literal_commands_only'


def command_lane(app, force_commands=False):
    if force_commands:
        return True
    if getattr(app, 'command_mode_active', False) is not True:
        return False
    manager = getattr(app, '_session_mode_manager', None)
    return manager is None or getattr(getattr(manager, 'mode', None), 'name', '') == 'COMMAND'


def literal_only(app, force_commands=False):
    """A saved False restores opportunistic matching; forced lanes ignore it."""
    return (not command_lane(app, force_commands)
            and cfg_get(getattr(app, 'config', {}), KEY) is not False)


def command_payload(text, app):
    """A complete leading command word is explicit intent, even with matching off."""
    # Keep the existing whole session controls ahead of the default prefix.
    whole = text.lower().strip().rstrip('.!?,;: ')
    if whole in {'command mode on', 'command mode off', 'command mode enable', 'command mode disable'}:
        return None
    executor = getattr(app, 'command_executor', None)
    find_exact = getattr(executor, 'find_exact_command', None)
    if callable(find_exact) and find_exact(text) is not None:
        return None
    config = getattr(app, 'config', {})
    intent = config.get('intent', {}) if isinstance(config, dict) else {}
    prefix = str(intent.get('command_prefix') or 'command').strip()
    match = re.match(r'^\s*' + re.escape(prefix) + r'\s+(.+)$', text, re.I | re.S)
    return match.group(1) if match else None


def accepts_dictation(executor, text, app, entry=None, context=None):
    """Read-only check shared by ordinary dispatch and the hands-free probe."""
    if cfg_get(getattr(app, 'config', {}), KEY) is False or command_payload(text, app) is not None:
        return True
    context = context if context is not None else executor._matcher.current_context()
    from .numbered_picks import pick_remainder
    pick = pick_remainder(text, allow_grid=True)
    if pick is not None:
        click, _ = executor._matcher.match('click ' + pick, context)
        return click is not None and click.phrase == 'click'
    literal, remainder = executor._matcher.match(text, context)
    return (literal is not None and (entry is None or literal is entry)
            and not remainder and not executor._takes_argument(literal))
