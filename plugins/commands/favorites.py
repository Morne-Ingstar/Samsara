"""Voice and Ava access to a numbered, profile-local quick list."""
from __future__ import annotations

import re

from samsara.command_registry import DispatchResult, DispatchState
from samsara.command_scope import register_tag_source
from samsara.favorites_store import FavoritesStore, normalize_target, open_target
from samsara.plugin_commands import command
from samsara.spoken_numbers import parse_spoken_number

_app = None
_rows = []
_visible = False
_ARGS = {'remainder': {'type': 'str', 'required': True}}
_OPTIONAL_ARGS = {'remainder': {'type': 'str', 'required': False}}
_LIST = r'(?:favorites|quick list)[.!?]*'


def active(app=None):
    return _visible and (app is None or app is _app)


def _panel():
    from samsara.ui.favorites_overlay_qt import get_panel
    return get_panel()


def _feedback(app, text, failed=False):
    # PLACEHOLDER COPY (958): feedback remains visible at Silent speech level.
    show = getattr(app, '_show_outcome_chip', None)
    if callable(show):
        show(text, 'error' if failed else 'success', 4500)
    coordinator = getattr(app, 'audio_coordinator', None)
    if coordinator is not None:
        coordinator.speak(text, category='agent_response')
    if active(app):
        _panel().post(lambda window: window.status.setText(text))
    if failed:
        return DispatchResult(DispatchState.FAILED, detail={'reason': text})
    return True


def _closed():
    global _visible
    _visible = False


def _settings(app, values):
    current = dict((getattr(app, 'config', {}) or {}).get('favorites') or {})
    current.update(values)
    if current == (getattr(app, 'config', {}) or {}).get('favorites'):
        return
    updater = getattr(app, 'update_config', None)
    if callable(updater):
        updater({'favorites': current}, save=True)
    else:
        app.config['favorites'] = current
        save = getattr(app, 'save_config', None)
        if callable(save):
            save()


def _refresh(app):
    global _rows
    if active(app):
        _rows = FavoritesStore().load()
        rows = list(_rows)
        _panel().post(lambda window: window.set_rows(rows))


@command('show favorites', aliases=['quick list', 'show quick list', 'list favorites'],
         pack='core', risk_class='ui', side_effects=['ui'])
def show_favorites(app, remainder=''):
    """Shows numbered files, folders, shortcuts and websites. Say a number to open."""
    global _app, _rows, _visible
    if remainder:
        return False
    from samsara.ui import qt_runtime
    if not qt_runtime.is_alive():
        return _feedback(app, 'Favorites is unavailable until the interface is ready.', True)
    try:
        rows = FavoritesStore().load()
        _app, _rows, _visible = app, rows, True
        _panel().show(rows, lambda number: pick(app, number), _closed,
                      (getattr(app, 'config', {}) or {}).get('favorites'),
                      lambda values: _settings(app, values))
    except (ValueError, OSError):
        _closed()
        return _feedback(app, 'Could not show favorites. The saved list has been preserved.', True)
    return True


@command('hide favorites', aliases=['hide quick list', 'close favorites', 'close quick list'],
         pack='core', risk_class='ui', side_effects=['ui'])
def hide_favorites(app, remainder=''):
    """Closes the quick list."""
    if remainder:
        return False
    if active(app):
        _closed()
        _panel().hide()
    return True


def pick(app, number, remainder=''):
    if not active(app) or remainder:
        return False
    if not 1 <= number <= len(_rows):
        return _feedback(app, 'Choose a favorite number from the list.', True)
    try:
        # Open the snapshot the owner saw, never rebind a number after an edit.
        open_target(_rows[number - 1]['target'])
    except (ValueError, OSError) as exc:
        return _feedback(app, str(exc) if isinstance(exc, ValueError) else 'Could not open this favorite.', True)
    hide_favorites(app)
    return True


def _explorer_target(hwnd):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch('Shell.Application')
        for window in shell.Windows():
            if int(window.HWND) != hwnd:
                continue
            selected = window.Document.SelectedItems()
            if selected.Count > 1:
                raise ValueError('Select one file or folder to add to favorites.')
            if selected.Count:
                return selected.Item(0).Path
            return window.Document.Folder.Self.Path
    finally:
        pythoncom.CoUninitialize()
    raise ValueError('Could not read this Explorer folder. Say its full path instead.')


def _browser_target(hwnd):
    # Use the existing apartment-confined UIA service; never copy the address
    # bar, send keys, or mistake a document's URL-looking text for browser chrome.
    from samsara.uia import get_service
    service = get_service()
    snapshot = service.snapshot(refresh=True)
    if snapshot.hwnd != hwnd:
        raise ValueError('The focused window changed. Try again.')
    for node in snapshot.nodes:
        if (node.control_type == 'EditControl' and not node.in_document
                and node.enabled and not node.offscreen
                and re.search(r'address|url|location', node.name, re.I)):
            return normalize_target(service.act(node, 'read').strip())
    raise ValueError('Could not read this browser address. Say the website instead.')


def current_target():
    from samsara.handlers import _get_foreground_hwnd, _get_foreground_exe_lower
    import win32gui
    hwnd = _get_foreground_hwnd()
    exe = _get_foreground_exe_lower()
    try:
        if exe == 'explorer.exe' and win32gui.GetClassName(hwnd) in {'CabinetWClass', 'ExploreWClass'}:
            target = _explorer_target(hwnd)
        elif exe in {'brave.exe', 'chrome.exe', 'msedge.exe', 'firefox.exe', 'vivaldi.exe', 'opera.exe'}:
            target = _browser_target(hwnd)
        else:
            raise ValueError('Focus an Explorer folder or browser, or say the full path or website.')
        if _get_foreground_hwnd() != hwnd:
            raise ValueError('The focused window changed. Try again.')
        return normalize_target(target)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('Could not read this target. Say its full path or website instead.') from exc


def _add_suffix(text):
    match = re.fullmatch(r'(.+?)\s+to\s+' + _LIST, text.strip(), re.I)
    if not match:
        raise ValueError('Say add a path or website to favorites.')
    return match[1]


@command('add favorite', aliases=['add to favorites', 'add to quick list',
         'add this to favorites', 'add this to quick list'], pack='core',
         risk_class='write', side_effects=['file'], param_schema=_OPTIONAL_ARGS)
def add_favorite(app, remainder=''):
    """Adds a full path or website; an empty argument or 'this' reads focused Explorer/browser."""
    target = (remainder or '').strip()
    if re.search(r'\s+to\s+' + _LIST + r'$', target, re.I):
        target = _add_suffix(target)
    try:
        if not target or target.casefold() == 'this':
            target = current_target()
        number, added = FavoritesStore().add(target)
        _refresh(app)
    except (ValueError, OSError) as exc:
        return _feedback(app, str(exc) if isinstance(exc, ValueError) else 'Could not save favorites.', True)
    return _feedback(app, f'Favorite {number} added.' if added else f'Already saved as favorite {number}.')


add_favorite.spoken_argument_prefixes = {'add': _add_suffix}
add_favorite.verbatim_argument = True


def _remove_suffix(text):
    match = re.fullmatch(r'(.+?)\s+from\s+' + _LIST, text.strip(), re.I)
    number = parse_spoken_number(match[1]) if match else None
    if number is None or number < 1:
        raise ValueError('Say remove a number from favorites.')
    return number


@command('remove favorite', aliases=['remove from favorites', 'remove from quick list'],
         pack='core', risk_class='write', side_effects=['file'], param_schema=_ARGS)
def remove_favorite(app, remainder=''):
    """Removes a numbered favorite, leaving the actual file or website untouched."""
    try:
        number = (_remove_suffix(remainder) if re.search(r'\s+from\s+' + _LIST + r'$', remainder, re.I)
                  else parse_spoken_number(remainder))
        if number is None:
            raise ValueError('Choose a favorite number to remove.')
        FavoritesStore().edit(number)
        _refresh(app)
    except (ValueError, OSError) as exc:
        return _feedback(app, str(exc) if isinstance(exc, ValueError) else 'Could not save favorites.', True)
    return _feedback(app, f'Favorite {number} removed.')


remove_favorite.spoken_argument_prefixes = {'remove': _remove_suffix}


def _rename(text):
    match = re.fullmatch(r'(.+?)\s+to\s+(.+)', text.strip(), re.I)
    number = parse_spoken_number(match[1]) if match else None
    if number is None or number < 1:
        raise ValueError('Say rename favorite, its number, to its new name.')
    return number, match[2]


def _visible_rename(text):
    if not active():
        raise ValueError('Show favorites first.')
    return _rename(text)


@command('rename favorite', pack='core', risk_class='write', side_effects=['file'], param_schema=_ARGS)
def rename_favorite(app, remainder=''):
    """Renames a favorite: rename favorite 2 to Project notes. With the list open, rename 2 to Project notes."""
    try:
        number, name = _rename(remainder)
        FavoritesStore().edit(number, name=name)
        _refresh(app)
    except (ValueError, OSError) as exc:
        return _feedback(app, str(exc) if isinstance(exc, ValueError) else 'Could not save favorites.', True)
    return _feedback(app, f'Favorite {number} renamed.')


rename_favorite.spoken_argument_prefixes = {'rename': _visible_rename}
rename_favorite.verbatim_argument = True


@command('favorites font size', aliases=['quick list font size'], pack='core', risk_class='ui',
         param_schema=_ARGS, side_effects=['config', 'ui'])
def font_size(app, remainder=''):
    """Sets favorites text size from 14 to 32 pixels, including while the list is closed."""
    size = parse_spoken_number(remainder)
    if size is None or not 14 <= size <= 32:
        return _feedback(app, 'Choose a favorites font size from 14 to 32.', True)
    _settings(app, {'font_size': size})
    if active(app):
        _panel().post(lambda window: window.set_font_size(size))
    return True


@command('favorites window size', aliases=['quick list window size'], pack='core', risk_class='ui',
         param_schema=_ARGS, side_effects=['config', 'ui'])
def window_size(app, remainder=''):
    """Sets favorites width by height in pixels, for example 900 by 700."""
    parts = re.split(r'\s+(?:by|x)\s+', remainder.strip(), flags=re.I)
    width, height = (map(parse_spoken_number, parts) if len(parts) == 2 else (None, None))
    if width is None or height is None or not (480 <= width <= 1600 and 320 <= height <= 1200):
        return _feedback(app, 'Choose a width from 480 to 1600 by a height from 320 to 1200.', True)
    _settings(app, {'width': width, 'height': height})
    if active(app):
        def resize(window):
            screen = window.screen().availableGeometry()
            window.resize(min(width, screen.width()), min(height, screen.height()))
            window.save_settings()
        _panel().post(resize)
    return True


def pick_numbered_item(app, number, remainder=''):
    """Route the shared picker to the active favorites snapshot or overview."""
    if active(app):
        return pick(app, number, remainder)
    from plugins.commands.window_switcher import handle_overview_number
    return handle_overview_number(app, number, remainder)


def numbered_list_visible():
    from plugins.commands.window_switcher import overview_active
    return active() or overview_active()


register_tag_source('numbered_list.visible', numbered_list_visible)
