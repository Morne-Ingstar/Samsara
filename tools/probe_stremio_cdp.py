"""INVESTIGATION-ONLY probe: can Stremio's WebView2 UI be driven over Chrome
DevTools Protocol (CDP)?

stremio-shell-ng.exe hosts its UI in a Microsoft Edge WebView2 control.
WebView2 is Chromium under the hood, so (in principle) it honors
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS to open a CDP debug port, the same
protocol Chrome/Edge DevTools use. This script:

  1. Kills any running Stremio (stremio-shell-ng.exe + stremio-runtime.exe).
  2. Relaunches stremio-shell-ng.exe with
     WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9222
     in the child process's environment only (never touches the user's
     persistent environment).
  3. Polls http://127.0.0.1:9222/json for CDP targets.
  4. If a page target exists, opens a websocket to it and runs a handful
     of read-only Runtime.evaluate probes: DOM sweep for player-bar-like
     controls, video element state, page title/URL.
  5. Checks whether the debug port is ALSO reachable from a non-loopback
     address (a real security concern if so).

This is a ONE-SHOT diagnostic tool, not a library and not a permanent
feature. It reuses tools/stremio_control.py's kill/process-name constants
(same tools/ directory, no samsara/plugins involvement) rather than
duplicating that logic. Findings get written to a separate markdown report
by hand after reviewing this script's output -- this script only prints.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Scraped DOM content (aria-labels, titles, class names) is arbitrary web
# text and may contain non-ASCII characters; the default Windows console
# codepage (cp1252) can't encode it and would crash mid-probe. Force UTF-8
# with replacement so the probe always finishes and prints what it found.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import stremio_control  # noqa: E402

CDP_PORT = 9222
CDP_HOST = "127.0.0.1"

# Fallback install locations, tried only if the running process's own path
# can't be queried (Stremio not running, or the CIM query fails).
_STANDARD_INSTALL_CANDIDATES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Stremio" / "stremio-shell-ng.exe",
    Path(r"C:\Program Files\Stremio\stremio-shell-ng.exe"),
    Path(r"C:\Program Files (x86)\Stremio\stremio-shell-ng.exe"),
]


def _log(msg: str) -> None:
    print(f"[PROBE] {msg}")


# ── Step 0: locate the Stremio executable ─────────────────────────────────────

def find_stremio_exe() -> "Path | None":
    """Prefer the currently-running process's own path (most authoritative
    -- reflects THIS machine's actual install, portable/custom or not).
    Falls back to standard install locations if Stremio isn't running or
    the query fails."""
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance Win32_Process -Filter \"Name='stremio-shell-ng.exe'\")"
                ".ExecutablePath",
            ],
            capture_output=True, timeout=10, text=True,
        )
        path_str = result.stdout.strip()
        if path_str and Path(path_str).exists():
            _log(f"Found Stremio exe from running process: {path_str}")
            return Path(path_str)
    except Exception as e:
        _log(f"CIM query for running process path failed: {e}")

    for candidate in _STANDARD_INSTALL_CANDIDATES:
        if candidate.exists():
            _log(f"Found Stremio exe at standard install location: {candidate}")
            return candidate

    return None


# ── Step 1-2: kill + relaunch with the CDP flag ───────────────────────────────

def kill_and_relaunch(exe_path: Path) -> subprocess.Popen:
    was_running = stremio_control.is_stremio_running()
    _log(f"Stremio running before probe: {was_running}")
    if was_running:
        _log("Killing stremio-shell-ng.exe / stremio-runtime.exe ...")
        stremio_control.kill_stremio()
        time.sleep(1.5)
        _log(f"Stremio running after kill: {stremio_control.is_stremio_running()}")

    child_env = os.environ.copy()
    child_env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={CDP_PORT}"
    _log(f"Relaunching {exe_path} with WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS="
         f"'--remote-debugging-port={CDP_PORT}' (child env only)")
    proc = subprocess.Popen([str(exe_path)], env=child_env, cwd=str(exe_path.parent))
    return proc


# ── Step 3: poll the CDP HTTP endpoint ────────────────────────────────────────

def poll_cdp_targets(host: str, port: int, attempts: int = 15, delay_s: float = 1.0):
    """GET http://host:port/json repeatedly until it responds or we give up.
    Returns the parsed target list, or None if it never came up."""
    url = f"http://{host}:{port}/json"
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                data = json.loads(resp.read())
                _log(f"CDP endpoint responded on attempt {i + 1}/{attempts}: "
                     f"{len(data)} target(s)")
                return data
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            _log(f"  attempt {i + 1}/{attempts}: not up yet ({type(e).__name__}: {e})")
            time.sleep(delay_s)
    return None


def check_nonloopback_exposure(port: int) -> None:
    """Security check: is the debug port ALSO reachable from a non-loopback
    address on this machine? That would mean it's bound to 0.0.0.0, not
    127.0.0.1 -- a real blocker (anyone on the LAN could drive Stremio, or
    worse, use CDP's Page.navigate/Runtime.evaluate for arbitrary code
    execution in the WebView2 process)."""
    import socket
    hostname = socket.gethostname()
    try:
        _, _, ip_list = socket.gethostbyname_ex(hostname)
    except Exception as e:
        _log(f"Could not enumerate LAN IPs for exposure check: {e}")
        return
    lan_ips = [ip for ip in ip_list if not ip.startswith("127.")]
    if not lan_ips:
        _log("No non-loopback IPv4 found to test exposure against.")
        return
    for ip in lan_ips:
        url = f"http://{ip}:{port}/json"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                resp.read()
            _log(f"*** SECURITY: CDP endpoint IS reachable at {url} "
                 f"(bound to 0.0.0.0, not 127.0.0.1 only) -- BLOCKER ***")
        except Exception:
            _log(f"CDP endpoint NOT reachable at {url} (good -- loopback-only, as expected)")


# ── Steps 2-3: websocket probe of a page target ───────────────────────────────

_DOM_PROBE_JS = r"""
(() => {
  const out = {};
  out.url = location.href;
  out.title = document.title;

  const video = document.querySelector('video');
  out.video = video ? {
    currentTime: video.currentTime,
    duration: video.duration,
    paused: video.paused,
    muted: video.muted,
    volume: video.volume,
  } : null;

  // Broad sweep: anything that LOOKS like a player control, by
  // aria-label, title attribute, or class-name keyword. Reports whatever
  // is actually present in the current DOM -- if Stremio isn't currently
  // playing a video, this will likely be sparse/empty, which is itself
  // useful information (the player bar may only exist in the DOM during
  // active playback).
  const keywords = ['play', 'pause', 'next', 'prev', 'subtitle', 'volume',
                     'mute', 'seek', 'fullscreen', 'control'];
  const candidates = [];
  document.querySelectorAll('button, [role="button"], [aria-label], [title]').forEach(el => {
    const label = (el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
    const cls = (el.className || '').toString().toLowerCase();
    const testid = (el.getAttribute('data-testid') || el.getAttribute('data-test') || '').toLowerCase();
    const haystack = label + ' ' + cls + ' ' + testid;
    if (keywords.some(k => haystack.includes(k))) {
      candidates.push({
        tag: el.tagName,
        ariaLabel: el.getAttribute('aria-label') || null,
        title: el.getAttribute('title') || null,
        className: (el.className || '').toString().slice(0, 200),
        testId: el.getAttribute('data-testid') || el.getAttribute('data-test') || null,
        outerHTMLSnippet: el.outerHTML.slice(0, 200),
      });
    }
  });
  out.controlCandidates = candidates.slice(0, 40);
  out.controlCandidateCount = candidates.length;
  out.totalButtons = document.querySelectorAll('button, [role="button"]').length;

  return JSON.stringify(out);
})()
"""


async def _ws_probe(ws_url: str) -> dict:
    import websockets

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        async def send(method, params=None, msg_id=1):
            await ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if msg.get("id") == msg_id:
                    return msg

        await send("Runtime.enable", msg_id=1)
        eval_resp = await send(
            "Runtime.evaluate",
            {"expression": _DOM_PROBE_JS, "returnByValue": True},
            msg_id=2,
        )
        return eval_resp


def probe_page_target(target: dict) -> None:
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        _log(f"Target {target.get('id')} has no webSocketDebuggerUrl -- skipping.")
        return
    _log(f"Connecting to {ws_url}")
    try:
        resp = asyncio.run(_ws_probe(ws_url))
    except ImportError:
        _log("`websockets` package not importable -- cannot run the DOM probe. "
             "(Checked: `websocket-client` is NOT installed in this env; "
             "`websockets` IS installed -- see findings doc.)")
        return
    except Exception as e:
        _log(f"Websocket probe failed: {type(e).__name__}: {e}")
        return

    result = resp.get("result", {})
    if result.get("exceptionDetails"):
        _log(f"Runtime.evaluate raised: {result['exceptionDetails']}")
        return
    value_str = result.get("result", {}).get("value")
    if value_str is None:
        _log(f"Unexpected Runtime.evaluate response shape: {json.dumps(resp)[:500]}")
        return

    try:
        data = json.loads(value_str)
    except json.JSONDecodeError:
        _log(f"Could not parse DOM probe JSON: {value_str[:500]}")
        return

    _log("---- DOM probe result ----")
    _log(f"  url:   {data.get('url')}")
    _log(f"  title: {data.get('title')}")
    _log(f"  video: {data.get('video')}")
    _log(f"  totalButtons on page: {data.get('totalButtons')}")
    _log(f"  control-keyword candidates found: {data.get('controlCandidateCount')}")
    for c in data.get("controlCandidates", []):
        _log(f"    <{c['tag']}> aria-label={c['ariaLabel']!r} title={c['title']!r} "
             f"testId={c['testId']!r} class={c['className']!r}")
        _log(f"      html: {c['outerHTMLSnippet']!r}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    _log("=== Stremio CDP viability probe (investigation only) ===")

    exe_path = find_stremio_exe()
    if exe_path is None:
        _log("Could not locate stremio-shell-ng.exe (not running, and no standard "
             "install location matched). Aborting.")
        return

    proc = kill_and_relaunch(exe_path)
    _log(f"Relaunched, pid={proc.pid}. Waiting for WebView2 + CDP endpoint to come up...")
    time.sleep(3.0)  # give the process + WebView2 host a moment to start before polling

    targets = poll_cdp_targets(CDP_HOST, CDP_PORT)
    if targets is None:
        _log("CDP endpoint never came up. Stremio behaves normally otherwise? "
             "Check the Stremio window manually.")
        return

    _log(f"Raw target list:\n{json.dumps(targets, indent=2)}")

    check_nonloopback_exposure(CDP_PORT)

    page_targets = [t for t in targets if t.get("type") == "page"]
    _log(f"{len(page_targets)} page-type target(s) found.")
    for t in page_targets:
        probe_page_target(t)

    _log("=== Probe complete. Stremio was left running with the debug port "
         "active for this session only -- restart it normally to clear the flag. ===")


if __name__ == "__main__":
    main()
