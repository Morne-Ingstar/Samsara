"""Video demo commands for the Samsara promotional video.

Custom commands for specific demo scenarios. These are real
working commands, not fakes — the video shows actual functionality.
"""

import webbrowser
import os
from pathlib import Path

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


@command("print me a gun", aliases=[
    "print a gun", "three d print a gun",
    "3d print a gun", "print me a weapon"
])
def handle_print_gun(app, remainder):
    """Start a 3D print job on the FlashForge AD5X.
    
    For the video: pre-load the toy gun gcode on the printer,
    this command triggers the print via the LAN HTTP API.
    
    Setup:
    1. Slice a toy gun STL in Orca-FlashForge
    2. Upload to printer via Orca (it stays in the printer's file list)
    3. Enable LAN mode on the printer (Settings → Network → LAN Mode)
    4. Set FLASHFORGE_IP in samsara_config.json
    """
    import requests
    
    # Printer config
    printer_ip = app.config.get('flashforge_ip', '')
    check_code = app.config.get('flashforge_check_code', '')
    print_file = app.config.get('flashforge_print_file', 'tactical_defense_weapon.3mf')
    
    if not printer_ip:
        print("[PRINT] No printer configured. Set flashforge_ip in config.")
        print("[PRINT] Simulating print start...")
        print(f"[PRINT] Starting print: {print_file}")
        if hasattr(app, 'play_sound'):
            try:
                app.play_sound("start")
            except Exception:
                pass
        return True
    
    try:
        # FlashForge LAN API — start print job
        url = f"http://{printer_ip}:8899/api/v1/print"
        headers = {"Check-Code": check_code}
        payload = {"file": print_file}
        
        print(f"[PRINT] Sending print job to {printer_ip}...")
        resp = requests.post(url, json=payload, headers=headers, timeout=5)
        
        if resp.status_code == 200:
            print(f"[PRINT] Print started: {print_file}")
        else:
            print(f"[PRINT] Printer responded: {resp.status_code}")
            
    except requests.ConnectionError:
        print(f"[PRINT] Cannot reach printer at {printer_ip}")
    except Exception as e:
        print(f"[PRINT] Error: {e}")
    
    if hasattr(app, 'play_sound'):
        try:
            app.play_sound("start")
        except Exception:
            pass
    
    return True
