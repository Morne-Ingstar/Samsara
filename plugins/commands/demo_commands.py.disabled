"""Video demo commands for the Samsara promotional video.

Custom commands for specific demo scenarios. These are real
working commands, not fakes — the video shows actual functionality.

Note: "print me a gun" moved to flashforge_printer.py plugin.
"""

import webbrowser
import subprocess
import tempfile
import os
from pathlib import Path
from datetime import datetime

from samsara.plugin_commands import command


@command("show me my portfolio", aliases=[
    "show my portfolio", "open portfolio",
    "show me my stocks", "open my stocks",
    "how are my stocks", "check my portfolio"
])
def handle_portfolio(app, remainder):
    """Open the portfolio dashboard."""
    portfolio_path = Path(__file__).parent.parent / "assets" / "portfolio.html"
    if portfolio_path.exists():
        webbrowser.open(f"file:///{portfolio_path.resolve()}")
        print("[DEMO] Opening portfolio dashboard...")
    else:
        print(f"[DEMO] Portfolio page not found at {portfolio_path}")
    return True


@command("open my will", aliases=[
    "my will", "open will", "last will",
    "will and testament"
])
def handle_will(app, remainder):
    """Open a will template in the browser with large readable font."""
    today = datetime.now().strftime("%B %d, %Y")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Last Will and Testament</title>
<style>
body {{ background: #1a1a1a; margin: 0; padding: 40px; display: flex; justify-content: center; }}
.page {{ background: #f5f0e8; max-width: 800px; width: 100%; padding: 60px 80px;
  font-family: 'Georgia', serif; color: #222; box-shadow: 0 4px 30px rgba(0,0,0,0.5);
  min-height: 90vh; }}
h1 {{ text-align: center; font-size: 32px; letter-spacing: 4px; margin-bottom: 40px;
  border-bottom: 2px solid #333; padding-bottom: 16px; }}
h2 {{ font-size: 20px; margin-top: 36px; margin-bottom: 12px; }}
p {{ font-size: 18px; line-height: 1.8; margin-bottom: 16px; }}
.date {{ text-align: right; font-style: italic; color: #555; font-size: 16px; }}
.bequests {{ min-height: 200px; }}
</style></head><body>
<div class="page">
<h1>LAST WILL AND TESTAMENT</h1>
<p class="date">{today}</p>
<p>I, Matthew Jackson, being of sound mind and body, do hereby declare this
to be my Last Will and Testament, revoking all previous wills and codicils.</p>
<h2>ARTICLE I &mdash; DECLARATIONS</h2>
<p>I am currently in possession of approximately $3.00 in crypto assets
and an outstanding debt of $247,891.33 to CryptoVault Pro (see attached
margin call).</p>
<h2>ARTICLE II &mdash; BEQUESTS</h2>
<div class="bequests" contenteditable="true" id="bequests"
  style="font-size: 18px; line-height: 1.8; outline: none; min-height: 200px;"></div>
</div>
<script>document.getElementById('bequests').focus();</script>
</body></html>"""

    will_path = os.path.join(tempfile.gettempdir(), "last_will.html")
    with open(will_path, 'w', encoding='utf-8') as f:
        f.write(html)

    webbrowser.open(f"file:///{will_path}")
    print("[DEMO] Opening last will and testament...")

    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
        except Exception:
            pass

    return True
