"""GIF search plugin.

Say "Samsara, make me a gif of dancing cat" and it opens Giphy
with search results. Simple, no API key needed.

Trigger phrases:
  "make me a gif of" / "gif of" / "find a gif of" /
  "search gif" / "gif me"
"""

import webbrowser
import urllib.parse

from samsara.plugin_commands import command


@command("make me a gif of", aliases=[
    "gif of", "find a gif of", "find me a gif of",
    "search gif", "gif me", "make a gif of",
    "get me a gif of", "show me a gif of"
])
def handle_gif(app, remainder):
    """Search Giphy for a GIF. Usage: 'Samsara, make me a gif of dancing cat'"""
    if not remainder or not remainder.strip():
        print("[GIF] No search term provided")
        return True
    
    query = remainder.strip()
    encoded = urllib.parse.quote(query)
    url = f"https://giphy.com/search/{encoded}"
    
    print(f"[GIF] Searching for: {query}")
    webbrowser.open(url)
    return True
