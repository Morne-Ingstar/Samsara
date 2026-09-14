"""Mouse 4/5 hook probe: does the low-level mouse hook deliver the button at all?

Installs the SAME samsara.mouse_hook.MouseHook the app installs, bound to
--button, and prints every raw XBUTTON event with a timestamp. Nothing else
runs: no dictation.py, no recorder, no guards. If a press prints here and
the app's log never shows "[HOTKEY] Main hotkey (mouse) pressed", the fault
is downstream in DictationApp._on_main_hotkey_mouse (see its guard lines);
if nothing prints here, the hook is not receiving the button on this machine
(vendor mouse software remapping it, another hook consuming it, or the
hook failing to install -- the install result is printed too).

    F:\\envs\\sami\\python.exe tools\\mouse_hotkey_probe.py --button mouse4
    F:\\envs\\sami\\python.exe tools\\mouse_hotkey_probe.py --button mouse4 --inject 2 --seconds 3

--inject N sends N synthetic Mouse 4/5 clicks through SendInput as a
self-test of the hook code path; they arrive flagged injected=True. A
physical press shows injected=False. Ctrl+C exits.

Logging is configured to stdout BEFORE samsara is imported, so samsara.log
reuses it and never attaches the app's rotating file handler (a second
writer on ~/.samsara/logs/samsara.log is the landmine this avoids).
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                    format="%(asctime)s %(levelname)s %(message)s")

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from samsara.mouse_hook import MouseHook, XBUTTON1, XBUTTON2  # noqa: E402  (after logging setup)

MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
INPUT_MOUSE = 0
VK_XBUTTON1 = 0x05
VK_XBUTTON2 = 0x06


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_ulong), ("u", _U)]


def inject_click(button: str) -> None:
    """Synthesize one press + release of Mouse 4/5 through SendInput."""
    data = XBUTTON1 if button == 'mouse4' else XBUTTON2
    for flag in (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP):
        inp = _INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = _MOUSEINPUT(0, 0, data, flag, 0, None)
        sent = ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        if sent != 1:
            print(f"  SendInput failed (sent={sent}, GetLastError={ctypes.GetLastError()})")
        time.sleep(0.05)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--button", default="mouse4", choices=("mouse4", "mouse5"),
                    help="the button the app would bind (default mouse4)")
    ap.add_argument("--no-suppress", action="store_true",
                    help="let the OS see the button too (the app suppresses its main hotkey)")
    ap.add_argument("--inject", type=int, default=0, metavar="N",
                    help="self-test: send N synthetic clicks of --button through SendInput")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="exit after this many seconds (default: run until Ctrl+C)")
    args = ap.parse_args()

    seen = []
    lock = threading.Lock()
    t0 = time.perf_counter()

    def on_event(name: str, pressed: bool) -> None:
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        rel = time.perf_counter() - t0
        vk = VK_XBUTTON1 if name == 'mouse4' else VK_XBUTTON2
        down = bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
        with lock:
            seen.append((name, pressed))
        print(f"{stamp} +{rel:8.3f}s  EVENT {name} {'PRESS  ' if pressed else 'RELEASE'}"
              f"  (GetAsyncKeyState says down={down}){'  <-- matches --button' if name == args.button else ''}",
              flush=True)

    suppress = () if args.no_suppress else (args.button,)
    hook = MouseHook(on_button_event=on_event, suppress_buttons=suppress)
    hook.start()
    installed = bool(hook._hook_id)
    print(f"hook installed={installed} id={hook._hook_id} thread={hook._thread_id}"
          f" bound={args.button} suppress={sorted(suppress)}", flush=True)
    if not installed:
        print("VERDICT: the hook did not install on this machine -- nothing downstream can run.")
        return 2

    print(f"Press {args.button} now (Ctrl+C to stop)...", flush=True)
    try:
        if args.inject:
            time.sleep(0.2)
            for i in range(args.inject):
                print(f"injecting click {i + 1}/{args.inject}", flush=True)
                inject_click(args.button)
                time.sleep(0.2)
        deadline = time.monotonic() + args.seconds if args.seconds else None
        while deadline is None or time.monotonic() < deadline:
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        hook.stop()

    with lock:
        presses = sum(1 for n, p in seen if n == args.button and p)
        releases = sum(1 for n, p in seen if n == args.button and not p)
    print(f"seen: {presses} press(es), {releases} release(s) of {args.button}; {len(seen)} XBUTTON event(s) total")
    if args.inject:
        ok = presses >= args.inject and releases >= args.inject
        print("VERDICT (self-test):", "the hook DELIVERS injected " + args.button + " press and release"
              if ok else "the hook did NOT deliver the injected clicks -- the hook code itself is at fault")
        return 0 if ok else 1
    print("VERDICT:", f"the hook delivers {args.button}; if the app never logs the press, the fault is downstream"
          if presses else f"no {args.button} press reached the hook -- the fault is upstream of the app")
    return 0 if presses else 1


if __name__ == "__main__":
    sys.exit(main())
