"""Standalone LAN phone remote for Stremio.

Serves one embedded HTML page with five huge touch buttons over plain HTTP
on the local network. Not a Samsara plugin -- no imports from the samsara
package or plugins/. Stdlib only (http.server), so this runs with any
Python interpreter, no dependencies to install.

Structured as a seed for a future in-app companion: all the actual control
logic lives in tools/stremio_control.py, shared with the voice-command
plugin (plugins/commands/stremio.py) so both surfaces stay in sync forever.

Usage:
    python tools/stremio_remote.py
    (or tools/stremio_remote.bat)

On first run, generates tools/stremio_remote_token.txt (gitignored) with a
random 8-char token, then prints one bookmarkable URL per LAN IPv4:

    http://192.168.1.23:8377/r/<TOKEN>/

The token is a LAN-only convenience gate, not real security -- it just
keeps other devices on the network from poking the remote by accident.
"""

import json
import logging
import secrets
import socket
import string
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import stremio_control  # noqa: E402 -- must follow the sys.path bootstrap above

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("stremio_remote")

PORT = 8377
TOKEN_FILE = _HERE / "stremio_remote_token.txt"

ACTIONS = {
    "play_pause":     stremio_control.pause_play,
    "back":           stremio_control.skip_back,
    "forward":        stremio_control.skip_forward,
    "fullscreen":     stremio_control.fullscreen,
    "mute":           stremio_control.mute,
    "volume_up":      stremio_control.volume_up,
    "volume_down":    stremio_control.volume_down,
    "switch_monitor": stremio_control.switch_monitor,
}


# ── Token ──────────────────────────────────────────────────────────────────────

def load_or_create_token() -> str:
    """Read the existing token, or generate + persist a new random 8-char
    one on first run."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    alphabet = string.ascii_letters + string.digits
    token = "".join(secrets.choice(alphabet) for _ in range(8))
    TOKEN_FILE.write_text(token, encoding="utf-8")
    logger.info(f"Generated new remote token -> {TOKEN_FILE}")
    return token


# ── LAN address discovery ─────────────────────────────────────────────────────

def local_ipv4_addresses() -> list:
    """Best-effort enumeration of this machine's non-loopback IPv4
    addresses, for printing bookmarkable URLs at startup."""
    addrs = set()
    hostname = socket.gethostname()
    try:
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        for ip in ip_list:
            if not ip.startswith("127."):
                addrs.add(ip)
    except Exception as e:
        logger.debug(f"gethostbyname_ex failed: {e}")

    try:
        infos = socket.getaddrinfo(hostname, None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.add(ip)
    except Exception as e:
        logger.debug(f"getaddrinfo failed: {e}")

    # Fallback: open a UDP socket to a public address (no packets actually
    # sent) to learn which local interface the OS would route through.
    if not addrs:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if not ip.startswith("127."):
                    addrs.add(ip)
        except Exception as e:
            logger.debug(f"UDP-route fallback failed: {e}")

    return sorted(addrs)


# ── HTML page ──────────────────────────────────────────────────────────────────

def render_page(token: str) -> bytes:
    base = f"/r/{token}"
    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Stremio Remote</title>
<style>
  html, body {{
    margin: 0; padding: 0; min-height: 100%;
    background: #0b0b0f; color: #f2f2f2;
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-tap-highlight-color: transparent;
    overscroll-behavior: none;
  }}
  .remote {{
    display: flex; flex-direction: column;
    min-height: 100vh; padding: 2vh 4vw; gap: 2.5vh;
    box-sizing: border-box;
  }}
  button {{
    flex: 1 1 18vh;
    min-height: 18vh;
    font-size: 8vh;
    line-height: 1;
    border: none;
    border-radius: 24px;
    background: #1e1e28;
    color: #f2f2f2;
    box-shadow: 0 4px 0 #05050a;
    display: flex; align-items: center; justify-content: center;
    gap: 0.4em;
    touch-action: manipulation;
  }}
  button .label {{
    font-size: 0.28em;
    font-weight: 600;
    letter-spacing: 0.02em;
  }}
  button.pressed {{
    background: #33334a;
    box-shadow: 0 1px 0 #05050a;
    transform: translateY(3px);
  }}
  button.error {{
    background: #5a1a1a !important;
    box-shadow: 0 4px 0 #300a0a !important;
  }}
  .row {{
    flex: 1 1 18vh;
    min-height: 18vh;
    display: flex; flex-direction: row;
    gap: 2.5vw;
  }}
  .row button {{
    flex: 1 1 0;
    min-height: 100%;
  }}
  #status {{
    text-align: center;
    min-height: 3vh;
    font-size: 2vh;
    color: #999;
  }}
</style>
</head>
<body>
<div class="remote">
  <button data-action="play_pause">&#9199; <span class="label">Play / Pause</span></button>
  <button data-action="back">&#9194; <span class="label">Back 10s</span></button>
  <button data-action="forward">&#9193; <span class="label">Forward 10s</span></button>
  <button data-action="fullscreen">&#9974; <span class="label">Fullscreen</span></button>
  <button data-action="mute">&#128263; <span class="label">Mute</span></button>
  <div class="row">
    <button data-action="volume_down" class="hold-repeat">&#128265; <span class="label">Vol&minus;</span></button>
    <button data-action="volume_up" class="hold-repeat">&#128266; <span class="label">Vol+</span></button>
  </div>
  <button data-action="switch_monitor">&#128421; <span class="label">Switch Monitor</span></button>
</div>
<div id="status"></div>
<script>
  const base = {json.dumps(base)};
  const statusEl = document.getElementById('status');

  function press(btn) {{
    const action = btn.dataset.action;
    btn.classList.add('pressed');
    setTimeout(() => btn.classList.remove('pressed'), 150);
    return fetch(base + '/key/' + action, {{ method: 'POST' }})
      .then(resp => resp.json())
      .then(data => {{
        if (!data.ok) {{
          flashError(btn, data.err || 'failed');
        }} else {{
          statusEl.textContent = '';
        }}
      }})
      .catch(() => {{
        flashError(btn, 'connection lost');
      }});
  }}

  function flashError(btn, msg) {{
    btn.classList.add('error');
    statusEl.textContent = msg;
    setTimeout(() => btn.classList.remove('error'), 600);
  }}

  // Ordinary single-shot buttons: one press() per click.
  document.querySelectorAll('button[data-action]:not(.hold-repeat)').forEach(btn => {{
    btn.addEventListener('click', () => press(btn));
  }});

  // Volume buttons: press-and-hold repeat. pointerdown fires immediately
  // and starts a ~180ms interval; pointerup/pointercancel/pointerleave
  // (finger drags off the button, or the browser cancels the gesture)
  // stops it. `inFlight` skips a scheduled fire if the previous request
  // hasn't resolved yet, so a slow network/AHK response can't pile up a
  // backlog of queued volume steps.
  document.querySelectorAll('button.hold-repeat').forEach(btn => {{
    let intervalId = null;
    let inFlight = false;

    const fire = () => {{
      if (inFlight) return;
      inFlight = true;
      press(btn).finally(() => {{ inFlight = false; }});
    }};

    const start = (e) => {{
      e.preventDefault();
      fire();
      if (intervalId === null) {{
        intervalId = setInterval(fire, 180);
      }}
    }};
    const stop = () => {{
      if (intervalId !== null) {{
        clearInterval(intervalId);
        intervalId = null;
      }}
    }};

    btn.addEventListener('pointerdown', start);
    btn.addEventListener('pointerup', stop);
    btn.addEventListener('pointercancel', stop);
    btn.addEventListener('pointerleave', stop);
  }});
</script>
</body>
</html>
"""
    return html.encode("utf-8")


# ── HTTP handler ───────────────────────────────────────────────────────────────

class RemoteHandler(BaseHTTPRequestHandler):
    server_version = "StremioRemote/1.0"

    def log_message(self, fmt, *args):
        logger.info("%s - %s" % (self.address_string(), fmt % args))

    def _token(self):
        return self.server.token  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_not_found(self) -> None:
        body = b"Not Found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_path(self):
        """Return (token, rest) where rest is the path after /r/<token>,
        e.g. '/r/abc123XY/key/mute' -> ('abc123XY', '/key/mute'). None on
        any malformed path (caller responds 404)."""
        path = self.path.split("?", 1)[0]
        parts = path.split("/", 3)  # ['', 'r', '<token>', 'rest...']
        if len(parts) < 3 or parts[1] != "r" or not parts[2]:
            return None, None
        token = parts[2]
        rest = "/" + parts[3] if len(parts) > 3 else "/"
        return token, rest

    def _check_token(self, token) -> bool:
        import hmac
        return token is not None and hmac.compare_digest(token, self._token())

    def do_GET(self):
        token, rest = self._parse_path()
        if not self._check_token(token) or rest not in ("/", ""):
            self._send_not_found()
            return
        body = render_page(self._token())
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        token, rest = self._parse_path()
        if not self._check_token(token):
            self._send_not_found()
            return
        prefix = "/key/"
        if not rest.startswith(prefix):
            self._send_not_found()
            return
        action = rest[len(prefix):]
        fn = ACTIONS.get(action)
        if fn is None:
            self._send_not_found()
            return

        ok = fn()
        if ok:
            self._send_json(200, {"ok": True})
            return

        if not stremio_control.is_stremio_running():
            err = "stremio not found"
        else:
            err = "stremio window not responding"
        self._send_json(200, {"ok": False, "err": err})


def main() -> None:
    token = load_or_create_token()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), RemoteHandler)
    server.token = token  # type: ignore[attr-defined]

    addrs = local_ipv4_addresses()
    logger.info("Stremio LAN remote running.")
    if not addrs:
        logger.warning("Could not determine a LAN IP -- server is still "
                        f"listening on 0.0.0.0:{PORT}.")
    for ip in addrs:
        logger.info(f"  http://{ip}:{PORT}/r/{token}/")
    logger.info(f"  (token file: {TOKEN_FILE})")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
