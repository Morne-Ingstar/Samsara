"""GIF search plugin.

Say "Samsara, search for a gif of dancing cat" and it opens Giphy.

Trigger phrases are deliberately distinct from screen_gif.py:
  - This plugin: "search gif" / "find a gif" / "gif of" (finding existing GIFs)
  - screen_gif.py: "record my screen" / "capture screen" (creating new GIFs)
"""

import webbrowser
import urllib.parse

from samsara.plugin_commands import command


@command("search for a gif of", aliases=[
    "find a gif of", "find me a gif of",
    "search gif", "gif me",
    "get me a gif of", "look up a gif of",
    "gif of"
])
def handle_gif(app, remainder):
    """Search Giphy for a GIF. Usage: 'Samsara, search for a gif of dancing cat'"""
    if not remainder or not remainder.strip():
        print("[GIF] No search term provided")
        return True
    
    query = remainder.strip()
    encoded = urllib.parse.quote(query)
    url = f"https://giphy.com/search/{encoded}"
    
    print(f"[GIF] Searching for: {query}")
    webbrowser.open(url)
    
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
        except Exception:
            pass
    
    return True
