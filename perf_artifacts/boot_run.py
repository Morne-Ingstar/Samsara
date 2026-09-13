"""Boot Samsara to "Startup complete" in a temp profile and read the stage times.

Usage:
  F:\\envs\\sami\\python.exe perf_artifacts\\boot_run.py <code_dir> <label> [--wake-on] [--hold]
      [--home <existing temp home>]  (reuse a home -> the "warm" run)

Never touches the live profile: SAMSARA_HOME_DIR is a temp dir holding a COPY
of ~/.samsara/config.json (optionally with wake_word_enabled forced on). The
live Samsara keeps running. The child (boot_run_child.py) installs no global
input hooks and is torn down with os._exit once startup completes.

Markers, all as wall-clock ms from the parent's Popen() call, read from the
temp profile's log:
  tray_window   boot thread done: "[CONFIG] File watcher started", the last line
                before create_tray_icon() schedules the tray + main window
  hotkey_ready  max(ACE engine started, Whisper model loaded, key listener set up)
  wake_ready    [WAKE] wake_ready (lazy load) / last [OWW] Loaded before
                startup complete (old eager load) / "off"
  total         [INIT] Startup complete.
Prints one JSON line (prefixed RUN_JSON) and appends it to perf_artifacts/boot_runs.jsonl.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable
LIVE_CONFIG = Path(os.path.expanduser("~")) / ".samsara" / "config.json"

ap = argparse.ArgumentParser()
ap.add_argument("code_dir")
ap.add_argument("label")
ap.add_argument("--wake-on", action="store_true")
ap.add_argument("--hold", action="store_true")
ap.add_argument("--home")
args = ap.parse_args()

if args.home:
    home = Path(args.home)
else:
    home = Path(tempfile.mkdtemp(prefix="samsara_bootrun_"))
    cfg = json.loads(LIVE_CONFIG.read_text(encoding="utf-8"))   # read-only copy
    if args.wake_on:
        cfg["wake_word_enabled"] = True
    (home / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
assert home.resolve() != LIVE_CONFIG.parent.resolve()

results = home / f"child_{int(time.time())}.json"
env = dict(os.environ, SAMSARA_HOME_DIR=str(home), PYTHONIOENCODING="utf-8")
t0_wall = time.time()
proc = subprocess.Popen([PY, str(HERE / "boot_run_child.py"), args.code_dir, str(results),
                         "1" if args.hold else "0"],
                        cwd=args.code_dir, env=env, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
try:
    _out, err = proc.communicate(timeout=420)
except subprocess.TimeoutExpired:
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
    _out, err = proc.communicate()
wall_s = time.time() - t0_wall

child = json.loads(results.read_text(encoding="utf-8")) if results.exists() else {"error": err[-2000:]}

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) - \w+ - (.*)$")
events = []
log_path = home / "logs" / "samsara.log"
for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
    m = LINE.match(raw)
    if not m:
        continue
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").timestamp()
    if ts + 0.002 < t0_wall:
        continue   # an earlier run in the same (warm) home
    events.append((round((ts - t0_wall) * 1000), m.group(2)))


def first(pattern, before=None):
    for ms, msg in events:
        if before is not None and ms > before:
            break
        if re.search(pattern, msg):
            return ms
    return None


def last(pattern, before=None):
    hit = None
    for ms, msg in events:
        if before is not None and ms > before:
            break
        if re.search(pattern, msg):
            hit = ms
    return hit


total = first(r"\[INIT\] Startup complete")
ace = first(r"\[BOOT-DIAG\] ACE engine\.start\(\) returned")
model = first(r"\[OK\] Model loaded in")
keys = first(r"\[BOOT\] keyboard/mouse listener setup")
lazy_wake = first(r"\[WAKE\] wake_ready")
eager_wake = last(r"\[OWW\] Loaded", before=total)
if lazy_wake is not None:
    wake = lazy_wake
elif eager_wake is not None:
    wake = eager_wake
else:
    wake = "off"

run = {
    "label": args.label,
    "code_dir": args.code_dir,
    "home": str(home),
    "wake_on": args.wake_on,
    "when": datetime.fromtimestamp(t0_wall).isoformat(timespec="seconds"),
    "first_log_line_ms": events[0][0] if events else None,
    "tray_window_ms": first(r"\[CONFIG\] File watcher (started|unavailable)"),
    "ace_started_ms": ace,
    "model_loaded_ms": model,
    "hotkey_ready_ms": max(v for v in (ace, model, keys) if v is not None) if None not in (ace, model) else None,
    "wake_ready_ms": wake,
    "wake_loaded_although_off": (not args.wake_on and eager_wake is not None),
    "total_ms": total,
    "process_wall_s": round(wall_s, 1),
    "migrate_lines": sum(1 for _, msg in events if "[MIGRATE]" in msg),
    "filter_cache": [msg for _, msg in events if "Polyphase filter" in msg],
    "calibration": [msg for _, msg in events if "[BOOT-DIAG] mic calibration" in msg or msg.startswith("[CAL] Reusing")],
    "boot_lines": [f"{ms:>6} {msg}" for ms, msg in events
                   if msg.startswith(("[BOOT]", "[WAKE]", "[TTS]")) or "ACE engine.start() returned" in msg
                   or "[BOOT-DIAG] mic calibration" in msg],
    "child": child,
}
if child.get("error") or total is None:
    run["stderr_tail"] = err[-3000:]
print("RUN_JSON " + json.dumps(run))
with open(HERE / "boot_runs.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(run) + "\n")
