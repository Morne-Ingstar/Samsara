"""Voice commands for opening web shortcuts and running web searches.

Triggers are deliberately NOT "open" -- that phrase is reserved for built-in
app launches (open chrome, open notepad). Using "go to" / "browse to" / "pull
up" / "show me" keeps web navigation separated from OS-level app launching.
"""

import urllib.parse
import webbrowser

from samsara.plugin_commands import command


def _clean(text):
    """Strip punctuation Whisper tends to add (periods, commas, etc.)."""
    return text.lower().strip().strip(".,!?;:'\"")


@command("go to", aliases=["browse to", "pull up", "show me"])
def open_site(app, remainder):
    """Open a web shortcut. 'go to youtube', 'pull up my orders'."""
    if not remainder:
        return False
    shortcuts = {}
    if app is not None and hasattr(app, 'config'):
        shortcuts = app.config.get('web_shortcuts', {}) or {}
    clean = _clean(remainder)
    for key, url in shortcuts.items():
        if key.lower() == clean:
            webbrowser.open(url)
            print(f"[WEB] Opened: {key} -> {url}")
            return True
    return False


@command("search for", aliases=["look up", "google"])
def search_web(app, remainder):
    """Search Google. 'search for cat toys', 'google Python tutorials'."""
    if not remainder:
        print("[WEB] Search for what?")
        return False
    clean = _clean(remainder)
    url = f"https://www.google.com/search?q={urllib.parse.quote(clean)}"
    webbrowser.open(url)
    print(f"[WEB] Searching: {clean}")
    return True
