"""Named-layout parsing and restore coordination, using the window plugin's adapters."""
import ctypes
import ntpath
import os
import re
import subprocess
import time
from urllib.parse import urlsplit


WINDOW_WAIT_SECONDS = 10.0


def spoken_text(text):
    from samsara.command_registry import view_tokens
    return ' '.join(token for token, _start, _end in view_tokens(text))


def save_name(text):
    # A name cannot swallow a second instruction or a trailing prose clause.
    if re.search(r'[\r\n,;.!?]', text.strip().rstrip('.!?')):
        return None
    match = re.fullmatch(r'(?:save layout|save window layout|save this layout)\s+(.+)', spoken_text(text))
    if match:
        name = re.sub(r'^(?:as|called|named|the)\s+', '', match[1])
        if (name and len(name) <= 30 and name not in {'default', 'current', 'none'}
                and not {'and', 'then', 'after', 'before', 'please'}.intersection(name.split())):
            return name
    return None


def restore_name(text, profiles):
    text = spoken_text(text)
    for name in profiles:
        if not isinstance(name, str):
            continue
        key = spoken_text(name)
        phrases = {f'{key} layout', f'restore {key} layout'}
        for prefix in ('restore layout', 'load layout', 'restore window layout'):
            phrases.update(f'{prefix} {filler}{key}' for filler in ('', 'as ', 'called ', 'named ', 'the '))
        if text in phrases:
            return name
    return None


def browser_urls(values):
    """Accept web URLs only; a profile never becomes a shell/protocol command."""
    if not isinstance(values, list):
        raise ValueError('Browser tabs must be a list of web addresses.')
    result = []
    for value in values:
        if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError('Browser tabs must contain valid http or https addresses.')
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
            raise ValueError('Browser tabs must contain valid http or https addresses.')
        if value not in result:
            result.append(value)
    return result


def default_browser_exe():
    """Ask Windows for the registered HTTP executable; do not read browser profiles."""
    size = ctypes.c_ulong(32768)
    output = ctypes.create_unicode_buffer(size.value)
    query = ctypes.windll.shlwapi.AssocQueryStringW
    query.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p,
                      ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
    query.restype = ctypes.c_long
    if query(0, 2, 'http', 'open', output, ctypes.byref(size)) != 0:
        raise OSError('Could not resolve the default browser.')
    return output.value


def open_browser_tabs(urls, *, resolve=default_browser_exe, popen=subprocess.Popen):
    urls = browser_urls(urls)
    if not urls:
        return
    exe = resolve()
    if ntpath.basename(exe).lower() not in {
        'chrome.exe', 'msedge.exe', 'firefox.exe', 'brave.exe', 'vivaldi.exe', 'opera.exe',
    }:
        raise ValueError('The default browser does not support a saved tab window.')
    popen([exe, '--new-window', *urls], shell=False)


def launch_app(entry):
    exe = entry.get('exe_path')
    if not isinstance(exe, str) or not ntpath.isabs(exe) or not exe.lower().endswith('.exe'):
        raise ValueError('No saved executable path; save this layout again with the app open.')
    if not os.path.isfile(exe):
        raise ValueError('The saved executable is no longer installed at that path.')
    subprocess.Popen([exe], shell=False)


def restore_rect(entry, monitors):
    if not monitors:
        raise ValueError('No monitor is available.')
    device = entry.get('monitor_device')
    target = next((m for m in monitors if m.get('device') == device), None) if device else next(
        (m for m in monitors if m['index'] == entry.get('monitor_index', 1)), None)
    fallback = target is None
    target = target or next((m for m in monitors if m.get('primary')), monitors[0])
    left, top, right, bottom = target['rect']
    x, y, r, b = entry.get('rect', [100, 100, 900, 700])
    width, height = min(max(1, r - x), right - left), min(max(1, b - y), bottom - top)
    if fallback:
        x, y = left + (right - left - width) // 2, top + (bottom - top - height) // 2
    return max(left, min(x, right - width)), max(top, min(y, bottom - height)), width, height


def restore_profile(profile, adapter, *, clock=time.monotonic, sleep=time.sleep):
    """Wait once per app, use each hwnd once, and continue after individual failures."""
    entries = profile.get('windows', [])
    result = dict(total=len(entries), restored=0, launched=0, failed=0, tabs=0, tab_failed=False)
    monitors = adapter.get_monitors()
    used, attempted = set(), set()
    for entry in entries:
        try:
            rect = restore_rect(entry, monitors)
            app_key = (entry.get('exe_path') or entry['app']).casefold()
            available = [h for h in adapter.find(entry) if h not in used]
            if not available and app_key not in attempted:
                attempted.add(app_key)
                if not adapter.running(entry):
                    adapter.launch(entry)
                    result['launched'] += 1
                deadline = clock() + WINDOW_WAIT_SECONDS
                while not available and clock() < deadline:
                    sleep(min(.1, max(0, deadline - clock())))
                    available = [h for h in adapter.find(entry) if h not in used]
            if not available:
                raise ValueError('No unused main window appeared within the layout wait.')
            title = entry.get('title_pattern')
            hwnd = next((h for h in available if title and adapter.title(h) == title), available[0])
            used.add(hwnd)
            adapter.place(hwnd, rect, entry.get('maximized', False))
            result['restored'] += 1
        except Exception:
            # Never log window titles, URLs, or command-line data.
            result['failed'] += 1
    try:
        urls = browser_urls(profile.get('browser_tabs', []))
        if urls:
            adapter.open_tabs(urls)
            result['tabs'] = len(urls)
    except Exception:
        result['tab_failed'] = True
    return result
