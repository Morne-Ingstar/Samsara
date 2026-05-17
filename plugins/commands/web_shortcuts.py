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


@command("go to", aliases=["browse to", "pull up", "show me"], pack="utilities")
def open_site(app, remainder):
    """Open a web shortcut, URL, or fall back to Google search.
    
    'go to youtube'        -> config shortcut
    'go to latimes.com'    -> opens URL directly
    'go to meatball stuff' -> Google search fallback
    """
    if not remainder:
        return False
    shortcuts = {}
    if app is not None and hasattr(app, 'config'):
        shortcuts = app.config.get('web_shortcuts', {}) or {}
    clean = _clean(remainder)

    # 1. Check saved shortcuts first
    for key, url in shortcuts.items():
        if key.lower() == clean:
            webbrowser.open(url)
            print(f"[WEB] Opened shortcut: {key} -> {url}")
            return True

    # 2. Check if it looks like a URL (contains a dot + TLD pattern)
    # Whisper often transcribes "latimes.com" as "latimes.com" or "la times dot com"
    url_text = clean.replace(" dot ", ".").replace(" slash ", "/")
    # Common TLDs that indicate a URL
    tlds = ['.com', '.org', '.net', '.io', '.dev', '.gov', '.edu', '.co',
            '.us', '.uk', '.app', '.ai', '.me', '.info', '.tv', '.gg']
    if any(url_text.endswith(tld) or tld + '/' in url_text for tld in tlds):
        if not url_text.startswith(('http://', 'https://')):
            url_text = 'https://' + url_text
        # Remove spaces that Whisper might have added within the domain
        # "la times.com" -> "latimes.com" is hard, but "latimes .com" -> "latimes.com"
        url_text = url_text.replace(' .', '.').replace('. ', '.')
        webbrowser.open(url_text)
        print(f"[WEB] Opened URL: {url_text}")
        return True

    # 3. Fall back to Google search
    url = f"https://www.google.com/search?q={urllib.parse.quote(clean)}"
    webbrowser.open(url)
    print(f"[WEB] No shortcut match, searching: {clean}")
    return True


@command("search for", aliases=["look up", "google"], pack="utilities")
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
