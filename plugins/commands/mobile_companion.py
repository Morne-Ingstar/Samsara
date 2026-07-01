"""Mobile Companion voice command (Phase 3 thin plugin).

Talks only to the app's already-running samsara.mobile.supervisor.Supervisor
instance -- this plugin starts nothing itself and never touches COM/SMTC
directly (that's the whole point of the subsystem: this file used to BE the
web server + COM backend, bound at import time, which could crash the host;
see mobile_companion.py.disabled for the quarantined original).

"Jarvis, mobile companion" -- prints the connect URL and, if the `qrcode`
package is installed, opens a locally-generated QR code image so no typing
is needed on the phone.
"""

import logging
import os
import tempfile

from samsara.plugin_commands import command

logger = logging.getLogger(__name__)


@command("mobile companion", aliases=["phone remote", "mobile remote", "show qr code"],
         pack="media", risk_class="safe", side_effects=["network"])
def handle_mobile_companion(app, remainder):
    """Show the mobile companion connection URL (and QR code, if available)."""
    supervisor = getattr(app, "mobile_supervisor", None)
    if supervisor is None or not supervisor.enabled:
        print("[MOBILE] Mobile companion is not running (enable mobile_companion.enabled "
              "in config and restart Samsara)")
        return True

    url = supervisor.connect_url()
    print("\n[MOBILE] Samsara Mobile Companion")
    print(f"    Open on your phone: {url}")
    print("    (the API token is embedded in the page automatically)\n")

    try:
        from samsara.mobile.qr import generate_png
        png = generate_png(url)
    except Exception as e:
        png = None
        logger.debug("[MOBILE] QR generation failed: %s", e)

    if not png:
        print("[MOBILE] QR code unavailable (qrcode package not installed) -- "
              "type the URL above into your phone's browser")
        return True

    fd, path = tempfile.mkstemp(suffix=".png", prefix="samsara_mobile_qr_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        os.startfile(path)
    except Exception as e:
        print(f"[MOBILE] Could not open QR image: {e}")

    return True
