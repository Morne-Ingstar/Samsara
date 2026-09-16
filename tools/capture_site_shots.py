"""Capture the morneis.com/samsara screenshots from the REAL running app.

Launches dictation.py against a fresh scratch SAMSARA_HOME_DIR seeded with
synthetic data only, drives it from outside with Windows UI Automation plus
one F19 toggle (cancelled with F20, so nothing is ever transcribed
or pasted), and grabs each window by its exact DWM frame bounds.

    F:\\envs\\sami\\python.exe tools\\capture_site_shots.py --out <dir>

Fence and privacy rules (queue 43):
  * Refuses to run if THIS checkout's dictation.py is already running or
    %TEMP%\\samsara.lock exists. Other installs (e.g. a daily build on its own
    home) may keep running: the instance mutex is keyed by home directory.
    The scratch instance uses F19 (record, toggle) and F20 (cancel) with
    command mode off, so a concurrently running build never sees a key the
    capture sends.
  * The scratch home holds only a seeded config, synthetic history and
    synthetic dictionary entries. It is created fresh each run.
  * The one in-process change: microphone DISPLAY NAMES are aliased to
    "Microphone", "Microphone 2", ... (samsara.audio_devices.list_microphones
    is wrapped before dictation.py imports it). The main window's status bar
    otherwise shows the real device name on every page. Capture still uses
    the real device.
  * Before each shot the window's whole UI Automation text is scanned for the
    real mic names, the Windows user/computer name, the real home path and the
    owner's real dictionary/history text. Only match counts and the matching
    APP-side strings are printed, never the real term; any match fails the run.
  * Never presses Alt (Left-Alt opens an Ava command session) and never
    clicks a command-reference row (a click runs the command).
  * Owns the desktop while it runs: it takes window focus.

Output files (deterministic): samsara-home.png, samsara-home-recording.png,
samsara-history.png, samsara-dictionary.png, samsara-settings.png,
samsara-cheatsheet.png, samsara-indicator.png.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYTHON = Path(sys.executable)
MAX_BYTES = 300 * 1024
BACKDROP = (15, 17, 21)          # flat dark ground behind frameless/translucent windows

SHOTS = (
    "samsara-home.png", "samsara-home-recording.png", "samsara-history.png",
    "samsara-dictionary.png", "samsara-settings.png", "samsara-cheatsheet.png",
    "samsara-indicator.png",
)


# --------------------------------------------------------------------------
# Child mode: the real app with aliased microphone display names
# --------------------------------------------------------------------------
def child_main() -> None:
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    import samsara.audio_devices as audio_devices

    real_list = audio_devices.list_microphones

    def aliased(show_all: bool = False):
        mics = real_list(show_all)
        return [dict(m, name="Microphone" if i == 0 else f"Microphone {i + 1}")
                for i, m in enumerate(mics)]

    audio_devices.list_microphones = aliased
    sys.argv = [str(REPO / "dictation.py")]
    runpy.run_path(str(REPO / "dictation.py"), run_name="__main__")


# --------------------------------------------------------------------------
# Fence
# --------------------------------------------------------------------------
def running_samsara() -> list[str]:
    """This checkout's dictation.py processes (other installs are allowed)."""
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'dictation\\.py' } | "
         "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    repo = str(REPO).lower()
    hits = []
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip() != str(os.getpid()) and repo in cmd.lower():
            hits.append(pid.strip())
    return hits


# --------------------------------------------------------------------------
# Scratch home
# --------------------------------------------------------------------------
def seed_home(home: Path, mic_id: int) -> None:
    home.mkdir(parents=True, exist_ok=False)
    config = {
        "first_run_complete": True,
        "tutorial_complete": True,
        "microphone": mic_id,
        "threshold_mode": "manual",
        "mode": "toggle",
        "hotkey": "f19",
        "cancel_hotkey": "f20",
        "command_mode": {"enabled": False},
        "model_size": "base",
        "language": "en",
        "listening_indicator_enabled": True,
        "listening_indicator_position": "bottom-center",
        "wake_word_enabled": False,
        "hints_enabled": False,
        "ducking": {"enabled": False},
        "window_width": 1040,
        "window_height": 700,
        "window_x": 80,
        "window_y": 80,
    }
    (home / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (home / "hints_shown.json").write_text(json.dumps(
        {"shown": ["ava_command_session_migration_v1"], "counters": {}}), encoding="utf-8")
    (home / "training_data.json").write_text(json.dumps({
        "vocabulary": ["Samsara", "Kubernetes", "PostgreSQL", "Ouroboros"],
        "corrections": {"post gress": "Postgres", "cooper netties": "Kubernetes"},
    }, indent=2), encoding="utf-8")
    (home / "user_corrections.json").write_text(json.dumps({
        "sam sara": "Samsara", "get hub": "GitHub", "pie torch": "PyTorch",
    }, indent=2), encoding="utf-8")

    db = sqlite3.connect(str(home / "history.db"))
    db.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
        app_context TEXT DEFAULT '', raw_text TEXT NOT NULL, display_text TEXT NOT NULL,
        duration_ms INTEGER DEFAULT 0, mode TEXT DEFAULT 'hold', status TEXT DEFAULT 'success',
        audio_path TEXT DEFAULT NULL, correction_source TEXT DEFAULT NULL,
        session_id TEXT DEFAULT '', entry_type TEXT DEFAULT 'dictation',
        log_prob REAL DEFAULT NULL, matched_command TEXT DEFAULT NULL)""")
    now = datetime.now()
    entries = [  # (days ago, hour, minute, entry_type, text, matched_command)
        (1, 16, 5, "dictation", "Draft reply: thanks, the shipping estimate works for us.", None),
        (1, 16, 40, "command", "maximize window", "maximize window"),
        (0, 9, 12, "dictation", "Good morning. Agenda for today is the release checklist.", None),
        (0, 9, 30, "command", "open notepad", "open notepad"),
        (0, 10, 2, "dictation", "The quarterly numbers look steady; I will send the summary after lunch.", None),
        (0, 10, 45, "command", "snap left", "snap left"),
        (0, 11, 20, "dictation", "Please add Kubernetes and PostgreSQL to the project vocabulary.", None),
    ]
    for days, hour, minute, entry_type, text, matched in entries:
        ts = (now - timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if ts > now:
            ts = now - timedelta(minutes=5 * (len(entries)))
        db.execute(
            "INSERT INTO history (timestamp, raw_text, display_text, duration_ms, mode, status,"
            " session_id, entry_type, matched_command) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts.isoformat(), text, text, 1800 + 40 * len(text), "toggle", "success",
             "site-shots", entry_type, matched))
    db.commit()
    db.close()


# --------------------------------------------------------------------------
# Privacy scan inputs (real data is compared, never printed)
# --------------------------------------------------------------------------
def forbidden_terms(real_mics: list[str]) -> dict[str, list[str]]:
    terms: dict[str, list[str]] = {"mic names": [m for m in real_mics if m.strip()]}
    user = os.environ.get("USERNAME", "")
    terms["identity"] = [t for t in (user, os.environ.get("COMPUTERNAME", ""),
                                     str(Path.home())) if len(t) >= 3]
    real_home = Path.home() / ".samsara"
    personal: list[str] = []
    try:
        data = json.loads((real_home / "training_data.json").read_text(encoding="utf-8"))
        personal += [v for v in data.get("vocabulary", []) if isinstance(v, str)]
        personal += [k for k in data.get("corrections", {}) if isinstance(k, str)]
    except Exception:
        pass
    try:
        personal += list(json.loads((real_home / "user_corrections.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    try:
        con = sqlite3.connect(f"file:{real_home / 'history.db'}?mode=ro", uri=True)
        personal += [r[0] for r in con.execute(
            "SELECT raw_text FROM history ORDER BY id DESC LIMIT 300") if r[0]]
        con.close()
    except Exception:
        pass
    synthetic = {"samsara", "kubernetes", "postgresql", "ouroboros", "postgres", "github", "pytorch"}
    terms["owner data"] = [p for p in personal
                           if isinstance(p, str) and len(p.strip()) >= 6 and p.strip().lower() not in synthetic]
    return terms


# --------------------------------------------------------------------------
# Windows plumbing
# --------------------------------------------------------------------------
user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi


def dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def pid_tree(root_pid: int) -> set[int]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId) $($_.ParentProcessId)\" }"],
        capture_output=True, text=True, timeout=60).stdout
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    seen, stack = set(), [root_pid]
    while stack:
        p = stack.pop()
        if p not in seen:
            seen.add(p)
            stack.extend(children.get(p, []))
    return seen


def top_windows(pids: set[int]) -> list[dict]:
    found = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            n = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            found.append({"hwnd": hwnd, "title": buf.value, "rect": frame_rect(hwnd)})
        return True

    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(proto(cb), 0)
    return found


def frame_rect(hwnd) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def wait_for(predicate, timeout: float, what: str, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"timed out waiting for {what}")


def focus(hwnd) -> None:
    fg = user32.GetForegroundWindow()
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    user32.AttachThreadInput(cur, fg_thread, True)
    user32.ShowWindow(hwnd, 9)                     # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(cur, fg_thread, False)
    time.sleep(0.4)


VK_F19, VK_F20 = 0x82, 0x83


def key(vk: int, up: bool) -> None:
    user32.keybd_event(vk, 0, 0x0002 if up else 0, 0)
    time.sleep(0.05)


def tap(vk: int) -> None:
    key(vk, False)
    time.sleep(0.25)
    key(vk, True)


# --------------------------------------------------------------------------
# UI Automation
# --------------------------------------------------------------------------
def uia():
    import uiautomation as auto
    auto.SetGlobalSearchTimeout(2)
    return auto


def walk_names(control, limit: int = 4000) -> list[str]:
    names, stack = [], [control]
    while stack and len(names) < limit:
        c = stack.pop()
        try:
            for value in (c.Name, getattr(c, "HelpText", "")):
                if value:
                    names.append(value)
            for getter in ("GetValuePattern", "GetLegacyIAccessiblePattern"):
                try:
                    pattern = getattr(c, getter)()
                    value = pattern.Value if pattern else ""
                    if value:
                        names.append(value)
                except Exception:
                    pass
            try:
                tp = c.GetTextPattern()
                if tp:
                    names.append(tp.DocumentRange.GetText(2000))
            except Exception:
                pass
            stack.extend(c.GetChildren())
        except Exception:
            continue
    return names


def find_named(root, name: str, *, contains: bool = False, depth: int = 40, kind: str | None = None):
    from uiautomation import WalkControl
    for c, _d in WalkControl(root, maxDepth=depth):
        try:
            n = c.Name or ""
            if kind and c.ControlTypeName != kind:
                continue
        except Exception:
            continue
        if (name in n) if contains else (n == name):
            return c
    return None


def dump_tree(root, path: Path, depth: int = 25) -> None:
    from uiautomation import WalkControl
    lines = []
    try:
        lines.append(f"ROOT {root.ControlTypeName} {root.Name!r} class={root.ClassName!r}")
        for c, d in WalkControl(root, maxDepth=depth):
            try:
                lines.append(f"{'  ' * d}{c.ControlTypeName} {c.Name!r} class={c.ClassName!r} aid={c.AutomationId!r}")
            except Exception as exc:
                lines.append(f"{'  ' * d}<error {exc}>")
    except Exception as exc:
        lines.append(f"walk failed: {exc}")
    path.write_text("\n".join(lines), encoding="utf-8")


def press(control) -> None:
    for getter in ("GetInvokePattern", "GetSelectionItemPattern", "GetTogglePattern"):
        try:
            pattern = getattr(control, getter)()
        except Exception:
            pattern = None
        if not pattern:
            continue
        try:
            if getter == "GetInvokePattern":
                pattern.Invoke()
            elif getter == "GetSelectionItemPattern":
                pattern.Select()
            else:
                pattern.Toggle()
            return
        except Exception:
            continue
    control.Click(simulateMove=False)


# --------------------------------------------------------------------------
# Grab + optimise
# --------------------------------------------------------------------------
def grab(rect, path: Path) -> int:
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=rect, all_screens=True).convert("RGB")
    return save_png(img, path)


def save_png(img, path: Path) -> int:
    img.save(path, "PNG", optimize=True)
    size = path.stat().st_size
    if size > MAX_BYTES:
        # Palette quantisation without dithering keeps flat dark UI crisp;
        # only used when lossless is over budget (reported by the caller).
        from PIL import Image
        q = img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        q.save(path, "PNG", optimize=True)
        size = path.stat().st_size
    return size


class Defocus:
    """A 1x1 window parked away from every captured window. Taking focus onto
    it makes the captured window render its INACTIVE frame, so the owner's
    Windows accent colour on the active title bar never reaches a shot."""

    def __init__(self, x: int = 2000, y: int = 1300):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.geometry(f"1x1+{x}+{y}")
        self.root.update()
        self.hwnd = int(self.root.wm_frame(), 16)

    def take_focus(self) -> None:
        focus(self.hwnd)
        self.root.update()


class Backdrop:
    """Flat window placed under a frameless always-on-top window so its
    translucent edges composite onto a known dark ground, not the desktop."""

    def __init__(self, rect, pad: int = 24):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        l, t, r, b = rect
        self.root.geometry(f"{r - l + 2 * pad}x{b - t + 2 * pad}+{l - pad}+{t - pad}")
        self.root.configure(bg="#%02x%02x%02x" % BACKDROP)
        self.root.attributes("-topmost", True)
        self.root.update()
        self.hwnd = int(self.root.wm_frame(), 16)

    def below(self, hwnd) -> None:
        # Insert the backdrop directly under the captured window in z-order.
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        user32.SetWindowPos(self.hwnd, hwnd, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        self.root.update()

    def close(self):
        self.root.destroy()


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--startup-timeout", type=float, default=240.0)
    args = parser.parse_args()
    if args.child:
        child_main()
        return 0

    sys.stdout.reconfigure(encoding="utf-8")
    dpi_aware()
    others = running_samsara()
    lock = Path(tempfile.gettempdir()) / "samsara.lock"
    if others or lock.exists():
        print(f"FENCE: Samsara appears to be running ({others or 'lock file present'}). "
              "Close it first; nothing was launched.")
        return 2

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.gettempdir()) / f"samsara_site_shots_{time.strftime('%Y%m%d_%H%M%S')}"
    evidence = out / "_capture_evidence"
    evidence.mkdir(exist_ok=True)

    sys.path.insert(0, str(REPO))
    from samsara.audio_devices import list_microphones
    wasapi = list_microphones(False)
    if not wasapi:
        print("No WASAPI microphone available; the recording shot needs one.")
        return 3
    real_mic_names = [m["name"] for m in list_microphones(True)]
    terms = forbidden_terms(real_mic_names)
    seed_home(home, wasapi[0]["id"])
    print(f"scratch home: {home}")
    print("  seeded: " + ", ".join(sorted(p.name for p in home.iterdir())))
    print("  privacy terms loaded: " + ", ".join(f"{k}={len(v)}" for k, v in terms.items()))

    env = dict(os.environ, SAMSARA_HOME_DIR=str(home), SAMSARA_DISABLE_UPDATE_CHECK="1")
    child_log = open(evidence / "app_stdout.txt", "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([str(PYTHON), str(Path(__file__).resolve()), "--child", "--out", str(out)],
                            cwd=str(REPO), env=env, stdout=child_log, stderr=subprocess.STDOUT)
    print(f"launched real app pid {proc.pid}")
    auto = uia()
    results: dict[str, object] = {}
    privacy: dict[str, dict] = {}
    try:
        log_path = home / "logs" / "samsara.log"

        def started():
            if proc.poll() is not None:
                raise RuntimeError(f"app exited early with code {proc.returncode}")
            try:
                return "[INIT] Startup complete." in log_path.read_text(encoding="utf-8", errors="replace")
            except FileNotFoundError:
                return False

        wait_for(started, args.startup_timeout, "startup complete", interval=1.0)
        print("startup complete")
        time.sleep(3.0)

        app_pids = pid_tree(proc.pid)

        def windows():
            return top_windows(app_pids)

        def main_window():
            return next((w for w in windows() if w["title"] == "Samsara"), None)

        mw = wait_for(main_window, 30, "main window")
        # A stray popup (e.g. "Microphone Changed") would be a defect; record every window.
        (evidence / "windows_at_start.json").write_text(json.dumps(windows(), indent=2), encoding="utf-8")
        root = auto.ControlFromHandle(mw["hwnd"])

        def check_privacy(label: str, control) -> None:
            names = walk_names(control)
            (evidence / f"uia_text_{label}.txt").write_text("\n".join(names), encoding="utf-8")
            lowered = [n.strip().lower() for n in names]
            counts, flagged = {}, set()
            for k, v in terms.items():
                hits = 0
                for t in v:
                    t = t.strip().lower()
                    # Exact UI-string match, or containment for long strings (short
                    # dictionary words would otherwise match ordinary UI labels).
                    matched = [n for n in lowered if n == t or (len(t) >= 15 and t in n)]
                    if matched:
                        hits += 1
                        flagged.update(matched)
                counts[k] = hits
            privacy[label] = {"counts": counts, "app_text_matched": sorted(flagged)}
            # Only the APP-side strings are printed (scratch-home text); never the real term.
            print(f"  privacy scan {label}: {counts}" + (f"  app text matched: {sorted(flagged)}" if flagged else ""))

        defocus = Defocus()

        def grab_window(w, path: Path, pad: int = 0) -> int:
            """Flat backdrop directly under the window (translucent Mica title bars
            and 1 px DWM borders would otherwise show whatever is behind), focus
            parked elsewhere so the frame renders inactive, then grab its bounds."""
            backdrop = Backdrop(w["rect"])
            try:
                backdrop.below(w["hwnd"])
                defocus.take_focus()
                backdrop.below(w["hwnd"])
                time.sleep(1.0)
                l, t, r, b = frame_rect(w["hwnd"])
                return grab((l - pad, t - pad, r + pad, b + pad), path)
            finally:
                backdrop.close()

        def shot_main(label: str, filename: str) -> None:
            w = main_window()
            focus(w["hwnd"])
            check_privacy(label, auto.ControlFromHandle(w["hwnd"]))
            size = grab_window(w, out / filename)
            results[filename] = size
            print(f"  saved {filename} {size} B  rect={w['rect']}")

        def sidebar(name: str) -> None:
            btn = find_named(root, name, depth=12, kind="CheckBoxControl")
            if btn is None:
                dump_tree(root, evidence / "uia_tree_on_failure.txt")
                raise RuntimeError(f"sidebar button {name!r} not found")
            btn.Click(simulateMove=False)
            time.sleep(1.2)

        # Home at rest
        sidebar("Home")
        shot_main("home", "samsara-home.png")
        sidebar("History")
        shot_main("history", "samsara-history.png")
        sidebar("Dictionary")
        shot_main("dictionary", "samsara-dictionary.png")

        # Recording: one toggle press, never a second one (that would transcribe + paste)
        sidebar("Home")
        focus(main_window()["hwnd"])
        tap(VK_F19)
        wait_for(lambda: find_named(auto.ControlFromHandle(main_window()["hwnd"]), "— recording",
                                    contains=True, depth=12), 15, "recording state")
        time.sleep(0.8)
        shot_main("home_recording", "samsara-home-recording.png")

        # Indicator (frameless, topmost, no title) while the live REC chip shows
        def indicator():
            cands = [w for w in windows() if w["title"] in ("", "python", "pythonw") and w["hwnd"] != main_window()["hwnd"]]
            cands = [w for w in cands if (w["rect"][2] - w["rect"][0]) < 900 and (w["rect"][3] - w["rect"][1]) < 300
                     and (w["rect"][2] - w["rect"][0]) > 20]
            return max(cands, key=lambda w: w["rect"][1]) if cands else None

        ind = wait_for(indicator, 10, "listening indicator")
        check_privacy("indicator", auto.ControlFromHandle(ind["hwnd"]))
        backdrop = Backdrop(ind["rect"])
        backdrop.below(ind["hwnd"])
        time.sleep(1.2)
        backdrop.below(ind["hwnd"])
        ind = indicator()
        backdrop.close()
        size = grab_window(ind, out / "samsara-indicator.png", pad=12)
        results["samsara-indicator.png"] = size
        print(f"  saved samsara-indicator.png {size} B  rect={ind['rect']}")

        # Cancel the recording (cancel key cancels without transcribing)
        focus(main_window()["hwnd"])
        tap(VK_F20)
        wait_for(lambda: find_named(auto.ControlFromHandle(main_window()["hwnd"]), "— recording",
                                    contains=True, depth=12) is None, 15, "recording cancelled")
        print("  recording cancelled")
        time.sleep(1.0)

        # Settings (separate window); Advanced matches the site's existing caption
        sidebar("Settings")
        sw = wait_for(lambda: next((w for w in windows() if w["title"] == "Samsara Settings"), None),
                      20, "settings window")
        sroot = auto.ControlFromHandle(sw["hwnd"])
        for tab, filename in (("Modes", "_alt-settings-modes.png"), ("Advanced", "samsara-settings.png")):
            item = find_named(sroot, tab, depth=12, kind="ListItemControl") or find_named(sroot, tab, depth=12)
            if item is None:
                dump_tree(sroot, evidence / "uia_tree_settings_on_failure.txt")
                raise RuntimeError(f"settings tab {tab!r} not found")
            item.Click(simulateMove=False)
            time.sleep(1.2)
            sw = next(w for w in windows() if w["title"] == "Samsara Settings")
            focus(sw["hwnd"])
            check_privacy(f"settings_{tab.lower()}", auto.ControlFromHandle(sw["hwnd"]))
            target = (evidence / filename) if filename.startswith("_") else (out / filename)
            size = grab_window(sw, target)
            if not filename.startswith("_"):
                results[filename] = size
            print(f"  saved {filename} {size} B  rect={sw['rect']}")
        user32.PostMessageW(sw["hwnd"], 0x0010, 0, 0)   # WM_CLOSE
        time.sleep(1.0)

        # Command reference via Home > "Control windows" card: opens the sheet filtered to the
        # generic window commands. The unfiltered list also shows app_lifecycle.py's
        # owner-specific project launchers, which do not belong on a public page.
        # No row is ever clicked (a click runs that command).
        sidebar("Home")
        root = auto.ControlFromHandle(main_window()["hwnd"])
        before = {w["hwnd"] for w in windows()}
        card = find_named(root, "Control windows", depth=14, kind="ButtonControl")
        if card is None:
            raise RuntimeError("Control windows card not found")
        press(card)          # Invoke, not a coordinate click

        def sheet():
            for w in windows():
                l, t, r, b = w["rect"]
                if (w["hwnd"] not in before and w["title"] not in ("Samsara", "Samsara Settings")
                        and (r - l) > 250 and (b - t) > 250):
                    return w
            return None

        try:
            cs = wait_for(sheet, 15, "command reference window")
        except TimeoutError:
            (evidence / "windows_on_sheet_failure.json").write_text(json.dumps(windows(), indent=2), encoding="utf-8")
            raise
        time.sleep(1.2)
        cs = sheet()
        check_privacy("cheatsheet", auto.ControlFromHandle(cs["hwnd"]))
        size = grab_window(sheet(), out / "samsara-cheatsheet.png")
        results["samsara-cheatsheet.png"] = size
        print(f"  saved samsara-cheatsheet.png {size} B  rect={cs['rect']}")

        (evidence / "windows_at_end.json").write_text(json.dumps(windows(), indent=2), encoding="utf-8")
        return_code = 0 if all(s in results for s in SHOTS) and not any(any(p["counts"].values()) for p in privacy.values()) else 1
    except Exception as exc:
        print(f"CAPTURE FAILED: {type(exc).__name__}: {exc}")
        return_code = 1
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        try:
            proc.wait(timeout=20)
        except Exception:
            pass
        child_log.close()
        (evidence / "summary.json").write_text(json.dumps(
            {"home": str(home), "sizes": results, "privacy": privacy}, indent=2), encoding="utf-8")
        print(f"app stopped; lock file present afterwards: {lock.exists()}")
    for name in SHOTS:
        print(f"{name:<30} {results.get(name, 'MISSING')}")
    return return_code


if __name__ == "__main__":
    sys.exit(main())
