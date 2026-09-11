"""Measure text delivery strategies against browser UI targets."""
from __future__ import annotations
import argparse, ctypes, json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
import psutil, pyperclip, uiautomation as auto
from samsara.clipboard import paste_with_preservation, type_text_unicode

TEXT = "Will the Witcher 3 Remastered include the DLCs?"
TARGETS = ("omnibox", "textarea", "ce", "cetb")
VK_CONTROL, VK_SHIFT = 0x11, 0x10
user32 = ctypes.windll.user32
_BROWSER_CANDIDATES = {
    "brave": (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe", r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    "chrome": (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    "edge": (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
}

def compare_sentence(got: str, expected: str = TEXT) -> bool:
    return got == expected

def summarize(got: str, expected: str = TEXT) -> str:
    return "PASS" if compare_sentence(got, expected) else "FAIL"

def _experimental_strategies(text: str):
    """Original low-level experiments, retained for diagnosis only."""
    import pyautogui
    def char_gap(gap):
        for char in text:
            pyautogui.write(char)
            if gap: time.sleep(gap)
    return {
        "experiment_uni_char_gap0": lambda: char_gap(0),
        "experiment_uni_char_gap5ms": lambda: char_gap(.005),
        "experiment_uni_char_gap15ms": lambda: char_gap(.015),
        "experiment_clipboard_ctrl_v": lambda: (pyperclip.copy(text), time.sleep(.05), pyautogui.hotkey("ctrl", "v")),
    }

def build_strategy_table(text: str = TEXT):
    return {"prod_paste_with_preservation": lambda: paste_with_preservation(text),
            "prod_type_text_unicode": lambda: type_text_unicode(text),
            **_experimental_strategies(text)}

def strategy_names(gate: bool, requested: str | None, table=None):
    table = table or build_strategy_table()
    if gate: return ("prod_paste_with_preservation",)
    return tuple(name for name in (requested or ",".join(table)).split(",") if name in table)

def detect_browser_path(browser: str = "auto", exists=None) -> str:
    exists = exists or os.path.exists
    names = ("brave", "chrome", "edge") if browser == "auto" else (browser.lower(),)
    for name in names:
        for raw in _BROWSER_CANDIDATES.get(name, ()):
            path = os.path.expandvars(raw)
            if exists(path): return path
    raise FileNotFoundError(f"No browser executable found for --browser {browser}")

def hold_hotkey_down(get_state=None) -> bool:
    get_state = get_state or user32.GetAsyncKeyState
    return bool(get_state(VK_CONTROL) & 0x8000 or get_state(VK_SHIFT) & 0x8000)

def _ensure_hotkey_up():
    if hold_hotkey_down(): raise SystemExit("aborted: Ctrl+Shift hold hotkey is physically down")

def _close_probe_windows():
    for proc in psutil.process_iter(["cmdline"]):
        try:
            if "inject_matrix_probe" in " ".join(proc.info.get("cmdline") or []): proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass

def _write_probe_page(path: Path):
    path.write_text("""<!doctype html><title>inject_matrix_probe</title>
<textarea id=textarea></textarea><div id=ce contenteditable=true></div>
<div id=cetb contenteditable=true role=textbox></div>
<script>document.title='inject_matrix_probe';</script>""", encoding="utf-8")

def _find_window():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        hwnd = auto.FindWindow("Chrome_WidgetWin_1", "inject_matrix_probe")
        if hwnd: return hwnd
        time.sleep(.25)
    raise RuntimeError("probe browser window did not appear")

def _focus_target(hwnd, target):
    window = auto.ControlFromHandle(hwnd)
    if target == "omnibox": window.SetFocus(); auto.SendKeys("^l"); return
    control = window.EditControl(AutomationId=target)
    if not control.Exists(2): control = window.DocumentControl(AutomationId=target)
    if not control.Exists(2): raise RuntimeError(f"target not found: {target}")
    control.Click(); control.SetFocus()

def _read_target(_target):
    control = auto.GetFocusedControl()
    try: return control.GetValuePattern().Value or ""
    except Exception: return getattr(control, "Name", "") or ""

def _run_matrix(browser, targets, strats, reps, quiet=False):
    saved_clipboard = pyperclip.paste(); page = Path(__file__).with_name("inject_matrix_probe.html")
    results, hwnd = [], None
    try:
        _ensure_hotkey_up(); _close_probe_windows(); _write_probe_page(page)
        subprocess.Popen([browser, "--new-window", page.as_uri()]); hwnd = _find_window()
        table = build_strategy_table()
        for target in targets:
            for name in strats:
                for rep in range(reps):
                    started = time.perf_counter()
                    try:
                        _ensure_hotkey_up(); _focus_target(hwnd, target); table[name](); got = _read_target(target)
                        verdict, error = summarize(got), None
                    except Exception as exc: got, verdict, error = "", "FAIL", repr(exc)
                    row = {"target": target, "strategy": name, "rep": rep, "ms": round((time.perf_counter()-started)*1000, 1), "got": got, "verdict": verdict}
                    if error: row["error"] = error
                    results.append(row)
                    if not quiet: print(f"{target:8} {name:35} {verdict}")
    finally:
        pyperclip.copy(saved_clipboard)
        if hwnd:
            try: auto.ControlFromHandle(hwnd).Close()
            except Exception: pass
        try: page.unlink()
        except FileNotFoundError: pass
    return results

def _result_path(gate, label):
    result_dir = Path(__file__).resolve().parent / "results"; result_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return result_dir / f"inject_matrix_{'gate' if gate else label}_{stamp}.json"

def main(argv=None):
    parser = argparse.ArgumentParser(); parser.add_argument("--browser", default="auto", choices=("auto", "brave", "chrome", "edge")); parser.add_argument("--strats"); parser.add_argument("--targets", default=",".join(TARGETS)); parser.add_argument("--reps", type=int, default=2); parser.add_argument("--label", default="manual"); parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    try:
        _ensure_hotkey_up(); browser = detect_browser_path(args.browser); table = build_strategy_table()
        targets = TARGETS if args.gate else tuple(args.targets.split(",")); strats = strategy_names(args.gate, args.strats, table)
        results = _run_matrix(browser, targets, strats, args.reps, quiet=args.gate)
    except SystemExit: raise
    except Exception as exc:
        if args.gate: print(f"INJECT_MATRIX_GATE FAIL ({exc})"); return 1
        raise
    output = _result_path(args.gate, args.label)
    output.write_text(json.dumps({"expected": TEXT, "browser": browser, "gate": args.gate, "targets": list(targets), "strategies": list(strats), "results": results}, indent=2), encoding="utf-8")
    passed = bool(results) and all(row["verdict"] == "PASS" for row in results)
    if args.gate: print(f"INJECT_MATRIX_GATE {'PASS' if passed else 'FAIL'}"); return 0 if passed else 1
    print(f"wrote {output}"); return 0

if __name__ == "__main__": sys.exit(main())
