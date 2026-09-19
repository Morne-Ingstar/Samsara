"""Voice command to search and switch Chrome/Edge tabs via Ctrl+Shift+A.

Critical: the browser must be foregrounded BEFORE any keystrokes fire.
Otherwise Ctrl+Shift+A lands in whatever app was focused (e.g. VS Code
command palette) and the search term gets typed into the user's document.
_focus_browser() runs first and aborts if no Chrome/Edge window exists.
"""

import time

import pyautogui
import pyperclip
import win32con
import win32gui

from samsara.plugin_commands import command


def _clean(text):
    """Strip punctuation Whisper tends to add."""
    return text.strip().strip(".,!?;:'\"")


def _browser_tab_number(value):
    """Return Chromium's tab shortcut number (1..9), or None.

    Nine deliberately means the last tab, matching Brave, Chromium and
    Firefox.  Keep this small and explicit: a phrase such as ``tab twelve``
    must never silently select a different tab.
    """
    words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9,
    }
    cleaned = _clean(value).lower()
    if cleaned in words:
        return words[cleaned]
    try:
        number = int(cleaned)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= 9 else None


def _focus_browser():
    """Find and focus the first Chrome or Edge window.

    Returns True if a browser was found and focused, False otherwise.
    """
    target_hwnd = None

    def enum_callback(hwnd, _):
        nonlocal target_hwnd
        if target_hwnd:
            return True  # already found one
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).lower()
        if 'chrome' in title or 'edge' in title or 'chromium' in title:
            target_hwnd = hwnd
        return True

    win32gui.EnumWindows(enum_callback, None)

    if not target_hwnd:
        print("[TABS] No Chrome or Edge window found.")
        return False

    try:
        if win32gui.IsIconic(target_hwnd):
            win32gui.ShowWindow(target_hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(target_hwnd)
        time.sleep(0.2)  # let Windows finish the focus switch
        return True
    except Exception as e:
        print(f"[TABS] Failed to focus browser: {e}")
        return False


@command("find tab", aliases=["find the tab", "switch to tab",
                              "where is", "find my"], pack="browsers",
                              risk_class="ui", param_schema={"query": {"type": "text", "required": False}},
)
def find_tab(app, remainder):
    """Finds the browser tab matching what you say and switches to it.

    'find tab github', 'where is slack'.
    """
    if not remainder:
        print("[TABS] Find which tab? Say a keyword.")
        return False

    search_term = _clean(remainder)

    # MUST focus browser first -- otherwise keystrokes go to wrong app
    if not _focus_browser():
        return False

    pyautogui.hotkey('ctrl', 'shift', 'a')
    time.sleep(0.4)  # wait for search panel to open

    # Clipboard paste is faster than typewrite and Unicode-safe. We deliberately
    # do NOT use _paste_preserving_clipboard -- this paste lands in the browser's
    # own search UI, not the user's document, so clipboard clobber is fine.
    pyperclip.copy(search_term)
    pyautogui.hotkey('ctrl', 'v')

    time.sleep(0.3)
    pyautogui.press('enter')

    print(f"[TABS] Searched for: {search_term}")
    return True


@command(
    "find text", aliases=["find"], pack="browsers", risk_class="ui",
    param_schema={"text": {"type": "text", "required": True}},
)
def find_text(app, remainder):
    """Open the browser find bar and enter the requested page text."""
    query = _clean(remainder)
    if not query:
        return False
    pyautogui.hotkey("ctrl", "f")
    pyautogui.write(query, interval=0.02)
    # Do not log the searched text: it can be private page content.
    return True


@command(
    "tab number", aliases=["tab"], pack="browsers", risk_class="ui",
    param_schema={"number": {"type": "int", "required": True, "min": 1, "max": 9}},
)
def tab_number(app, remainder):
    """Switch to browser tab 1 through 8; tab 9 means the last tab."""
    number = _browser_tab_number(remainder)
    if number is None:
        return False
    pyautogui.hotkey("ctrl", str(number))
    return True
