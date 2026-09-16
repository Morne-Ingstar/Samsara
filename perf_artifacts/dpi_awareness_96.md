# DPI awareness of the Samsara process — queue 96

Measured 2026-09-15 on the owner's machine (Windows 11 Pro 10.0.26200), branch
`feature/v0.22`, HEAD `eeebf68`, dev tree at `C:\Users\Morne\Projects\Samsara-dev`.
Live app: `pythonw.exe dictation.py`, PID 26712.

---

## Answer

**The process is `SYSTEM_AWARE`. It is NOT Per-Monitor V2.**

The brief's hopeful reading of `SetProcessDpiAwarenessContext() failed: Access is denied`
— "that is what Windows says when awareness was *already* set, so we may already be V2" —
is half right and lands on the wrong conclusion. Awareness **was** already set. It was set
to `SYSTEM_AWARE`, by `pyautogui`, before Qt ever ran.

Read three independent ways:

| Read | Result |
|---|---|
| `shcore.GetProcessDpiAwareness(pid 26712)` from outside the process | `SYSTEM_AWARE` (hr 0x00000000) |
| `user32.GetWindowDpiAwarenessContext` on each of the live app's 23 top-level windows, resolved with `AreDpiAwarenessContextsEqual` | `SYSTEM_AWARE` — all 23, including both visible `Qt6111QWindowIcon` windows ("Samsara", "Samsara Settings") |
| In-process replay of dictation.py's import order (`import pyautogui`, then `QApplication([])`) | `SYSTEM_AWARE` |

Note that `GetAwarenessFromDpiAwarenessContext` **cannot** answer this question on its own:
it returns `PER_MONITOR_AWARE` (2) for both V1 and V2. Only
`AreDpiAwarenessContextsEqual(ctx, DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)` separates
them. Both are logged.

---

## How it got set — whoever calls first wins

Neither the manifest nor the spec declares anything, so the first runtime API call decides.

**Replay of the two possible orders, same machine, same interpreter:**

| step | `order-app` (dictation.py's real order) | `order-qt` (control) |
|---|---|---|
| process start | `UNAWARE` | `UNAWARE` |
| after `import pyautogui` | **`SYSTEM_AWARE`** | `PER_MONITOR_AWARE_V2` (unchanged — pyautogui's call fails) |
| after `QApplication([])` | `SYSTEM_AWARE` (unchanged — **Qt's call fails**) | **`PER_MONITOR_AWARE_V2`** |
| final | `SYSTEM_AWARE` | `PER_MONITOR_AWARE_V2` |

So Qt 6 *does* request `PER_MONITOR_AWARE_V2` at `QApplication` construction and *would* get
it — if it went first. In the app it does not.

**The ordering, in the tree:**

| what | where |
|---|---|
| `import pyautogui` → SYSTEM_AWARE | `dictation.py:288`, module level |
| the actual call: bare `ctypes.windll.user32.SetProcessDPIAware()` at module scope, under a comment reading *"Fixes the scaling issues where PyAutoGUI was reporting the wrong resolution"* | `site-packages/pyautogui/_pyautogui_win.py:17` (the same call also sits at `pyscreeze/__init__.py:44` and `mouseinfo/__init__.py:54`, both of which pyautogui pulls in — whichever lands first, the result is SYSTEM_AWARE) |
| `QApplication.setHighDpiScaleFactorRoundingPolicy(...)` + `QApplication([])` | `samsara/ui/qt_runtime.py:139-140`, inside `_run_loop`, on the `samsara-qt` thread |
| Qt's failure, routed verbatim into the log by `boot.install_qt_message_handler` (`boot.py:173`) | `[QT] SetProcessDpiAwarenessContext() failed: Access is denied.` |

`dictation.py:288` runs at module import; `qt_runtime._run_loop` runs when the Qt runtime
thread is started (via `SplashScreenQt.__init__` → `qt_runtime.ensure_started()`, i.e. at
`boot.create_splash()`). pyautogui wins by a wide margin.

**The warning is real and current**, not historical: 18 occurrences in
`~/.samsara/logs/samsara.log`, most recent 2026-09-15 20:47:24.

### Other claimants in the environment — none of which get there first today

A full sweep of `site-packages` for `SetProcessDPIAware` finds the call in five places. Only
the pyautogui family is reached by the app:

| holder | reached by the app? |
|---|---|
| `pyautogui/_pyautogui_win.py:17` | **YES**, at `dictation.py:288`. This is the claimant. |
| `pyscreeze/__init__.py:44` | yes — pulled in by pyautogui, same import, same result |
| `mouseinfo/__init__.py:54` | yes — same |
| `matplotlib/_c_internal_utils...pyd` | **no.** matplotlib is not imported anywhere in the app path, and its call (`Win32_SetProcessDpiAwareness_max()`, `backends/_backend_tk.py`) runs at *figure creation*, not at import. |
| `webview/platforms/winforms.py` | **no.** pywebview's WinForms backend is not imported in the app path. |

(`PySide6/Qt6WebEngineCore.dll` and `QtWebEngineProcess.exe` also contain the symbol; WebEngine
runs out of process and does not set this process's awareness.)

This matters for the fix: removing pyautogui from the race is sufficient **today**, but anyone
who later adds a matplotlib figure or a pywebview window to the app re-opens the same hole
unless awareness is claimed before them — which is the argument for setting it in the manifest
or on the very first line of `dictation.py`, rather than merely reordering two imports.

### Two comments in the tree disagree with each other. One is wrong.

- `samsara/screen_ocr.py:10-14` (queue 70): *"The running app is NOT per-monitor-v2 DPI
  aware: dictation.py imports pyautogui at module load, which calls SetProcessDPIAware()
  before Qt starts, so the process is system-aware and Qt's own PMv2 request fails."*
  — **CORRECT**, and the reason its whole coordinate strategy exists.
- `dictation.py:315-319`: *"Qt 6 declares PER_MONITOR_AWARE_V2 when QApplication is
  constructed. A second process-wide SetProcessDpiAwareness call here caused Qt's later call
  to fail with ERROR_ACCESS_DENIED on every healthy startup."*
  — **STALE.** Removing Samsara's own `SetProcessDpiAwareness` call did not hand the win to
  Qt, because pyautogui's import-time call was never the "second" call — it is the first.
  The comment's last clause ("process awareness has one owner") is true; the owner is
  pyautogui, not Qt. `dictation.py` is outside this prompt's OWNS, so the comment was left
  alone.

---

## Manifest / spec

`scripts/samsara.spec` declares **no** DPI awareness. Its `EXE(...)` block (line 459) passes
no `manifest=` and no `uac_admin`, so PyInstaller's default manifest is embedded. Extracted
from `dist/Samsara/Samsara.exe`:

```xml
<trustInfo><security><requestedPrivileges>
  <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
</requestedPrivileges></security></trustInfo>
<compatibility ...><application><supportedOS Id="..."/> x5 </application></compatibility>
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
  <longPathAware ...>true</longPathAware>
</windowsSettings></application>
```

Occurrences of `dpiAware`: **0**. `dpiAwareness`: **0**. `PerMonitorV2`: **0**.

There is no `.manifest` or `.xml` file anywhere in the tree (outside `dist/` and `build/`)
declaring DPI awareness either.

**Frozen vs dev.** The manifest is the only thing that could differ *before* Python runs, and
it declares nothing — so the frozen build is decided by the same Python call ordering as the
dev tree. `pyautogui` is in the spec's `hiddenimports` (`scripts/samsara.spec:274`) and is
present in the exe archive, and the frozen build runs the same `dictation.py`. **Expected to
be identical: `SYSTEM_AWARE`.** Not verified by launching `dist/Samsara/Samsara.exe`, because
Samsara is running and a second instance would collide with it (and the `dist/` build is from
2026-09-14 19:16, older than the current tree). This is the one claim in this document that
is reasoned rather than measured.

---

## The owner's monitors

```
\\.\DISPLAY1   PRIMARY  rect=(0, 0, 2560, 1440)        dpi=96  scale=100%
\\.\DISPLAY2            rect=(2560, 1, 4480, 1081)     dpi=96  scale=100%
\\.\DISPLAY3            rect=(4480, -496, 8320, 1664)  dpi=96  scale=100%
\\.\DISPLAY4            rect=(-1920, 224, 0, 1304)     dpi=96  scale=100%
```

Four monitors. `GetDpiForMonitor(MDT_EFFECTIVE_DPI)` returns **96 on all four — every display
is at 100%**.

**The mixed-DPI risk is not reproducible on this machine.** That is the single most important
line in this document for whoever builds Interact mode: a `WindowFromPoint` bug caused by
system-awareness *cannot* be observed here, and a hand test on this desk will pass whatever
the process's awareness is.

---

## WindowFromPoint, measured

Twelve probe points — the centre and two opposite inset corners of each of the four monitors
— resolved under three different **thread** DPI contexts, in one process, same coordinates:

```
monitor        point            where            UNAWARE        SYSTEM_AWARE   PER_MONITOR_V2  agree  inside
\\.\DISPLAY1   (1280, 720)      centre           Claude         Claude         Claude          yes    yes
\\.\DISPLAY1   (60, 60)         top-left+60      109_truthful   109_truthful   109_truthful    yes    yes
\\.\DISPLAY1   (2500, 1380)     bottom-right-60  Claude         Claude         Claude          yes    yes
\\.\DISPLAY2   (3520, 541)      centre           OpenAI Help    OpenAI Help    OpenAI Help     yes    yes
\\.\DISPLAY2   (2620, 61)       top-left+60      ChatGPT        ChatGPT        ChatGPT         yes    yes
\\.\DISPLAY2   (4420, 1021)     bottom-right-60  OpenAI Help    OpenAI Help    OpenAI Help     yes    yes
\\.\DISPLAY3   (6400, 584)      centre           Program Mana   Program Mana   Program Mana    yes    yes
\\.\DISPLAY3   (4540, -436)     top-left+60      Program Mana   Program Mana   Program Mana    yes    yes
\\.\DISPLAY3   (8260, 1604)     bottom-right-60  Program Mana   Program Mana   Program Mana    yes    yes
\\.\DISPLAY4   (-960, 764)      centre           Samsara-dev    Samsara-dev    Samsara-dev     yes    yes
\\.\DISPLAY4   (-1860, 284)     top-left+60      Samsara-dev    Samsara-dev    Samsara-dev     yes    yes
\\.\DISPLAY4   (-60, 1244)      bottom-right-60  Samsara-dev    Samsara-dev    Samsara-dev     yes    yes

disagreements between awareness contexts: 0 of 12
every returned root window's GetWindowRect contains the probe point: yes
```

- **0 disagreements out of 12.** The three awareness contexts return the identical HWND at
  every point.
- **The returned window is the right one at every point**: `GetWindowRect` of the returned
  root window contains the probe coordinate in all 36 (12 points x 3 contexts) reads.
- `DISPLAY3` returning "Program Manager" is correct — nothing but the desktop is on it.
- This is the expected result *given four monitors at the same scale*, and it is exactly why
  it proves nothing about mixed DPI. It confirms the plumbing is sane; it does not clear the
  risk.

---

## Is a fix needed?

**Not in this prompt, and not on this hardware.** Per the brief: the process is not V2, so
the ordering change is reported, not made — it moves every window in the app.

### What it would take

| change | effect |
|---|---|
| **Preferred: a manifest `<dpiAwareness>PerMonitorV2</dpiAwareness>`** in the PyInstaller spec (`EXE(..., manifest=...)`) plus an equivalent for the dev tree | Windows sets awareness before any Python runs, so import order stops mattering. The dev tree has no manifest, so it would need the API call made before `import pyautogui` instead — meaning the two builds would get there by different routes and must both be checked. |
| **Or: call `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)` in `dictation.py` above line 288**, before `import pyautogui` | One line, no build change, works for both frozen and dev. pyautogui's import-time call then fails harmlessly (confirmed by the `order-qt` control above). Puts a Win32 call above the import block, which the file's structure currently avoids. |
| Qt attribute (`AA_EnableHighDpiScaling` etc.) | Does **not** help. Qt already asks for PMv2 at `QApplication` construction; the problem is that by then it is too late. |

### What it would disturb

Everything that currently compensates for system-awareness by forcing a PMv2 *thread*
context would become redundant, and — more importantly — the physical→logical mapping built
on top of it would need re-checking, because Qt's logical coordinates would become
per-monitor instead of system-wide:

- `samsara/ui/numbers_overlay_qt.py` — `_ensure_dpi_thread_context`, `_with_physical_dpi_context`,
  `_map_physical_to_qt`, `current_monitor_mappings`, `_win32_monitor_rects`
- `plugins/commands/show_numbers.py` — 8 call sites (cursor placement, `GetWindowRect`,
  overlay pill placement)
- `samsara/screen_ocr.py` — every geometry query, the BitBlt capture and the click
- every Qt window's size and position, on any monitor not at 100%

**And the trap:** with all four of this machine's monitors at 100%, *none* of that would
visibly change here. A switch to PMv2 would look completely clean on this desk and could
still be wrong on a user's 150%-plus-100% setup. Verifying it needs a mixed-DPI monitor
configuration — one display temporarily set to 150% would do it.

### What did change

`samsara/boot.py` gained `resolve_dpi_awareness()` and `log_dpi_awareness()`, and
`create_splash()` calls the latter once, after Qt is up. The boot log now states the answer
instead of leaving it to be inferred from an ambiguous error:

```
INFO - [DPI] process DPI awareness: SYSTEM_AWARE (enum SYSTEM_AWARE). NOT Per-Monitor V2:
on a monitor whose scale differs from the system scale, Windows virtualises coordinates for
this process, so WindowFromPoint and window rectangles can name the wrong window. Code that
needs real screen pixels must run inside an explicit PMv2 thread context (see
numbers_overlay_qt._with_physical_dpi_context).
```

Qt's own `[QT] SetProcessDpiAwarenessContext() failed: Access is denied.` was **left
unchanged**. The brief's §3 only called for rewording it if the process turned out to be
already-V2 (making the warning misleading); it is not, so the warning is accurate — Qt really
did fail. It is Qt's own message text, routed verbatim by `install_qt_message_handler`, and
rewriting a third party's message would make the handler lie. It will disappear by itself if
the ordering is ever fixed.

---

## For Interact mode

1. The process resolves `WindowFromPoint` as a **system-aware** caller today. On a mixed-DPI
   desktop that returns the wrong window.
2. It is **not reproducible on this machine** — four monitors, all 100%. Do not treat a
   passing hand test here as evidence.
3. The cheap, already-proven mitigation is the one `screen_ocr.py` and `show_numbers.py`
   already use: run the pointer resolution inside
   `numbers_overlay_qt._with_physical_dpi_context`. It must wrap **both** halves —
   `GetCursorPos` *and* `WindowFromPoint` — in one context, because both interpret
   coordinates in the calling thread's space, and reading the point in one awareness and
   resolving it in another is its own bug. Anything derived from the result
   (`GetWindowRect`, `DwmGetWindowAttribute`) belongs in the same context.
   That makes Interact correct under mixed DPI **without** changing process awareness, and
   therefore without touching every window in the app. It is the recommended path unless the
   owner wants the process-wide change and a mixed-DPI test rig to validate it.
