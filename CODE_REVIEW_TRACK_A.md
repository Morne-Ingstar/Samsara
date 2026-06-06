# Track A Code Review — Command System, Core Dispatch

**Files:** A1 (launcher + init + constants + languages) · A2 (commands + registry + parser) · A3 (packs + handlers + stats) · A4 (plugin_commands + key_macros + phonetic_wash + cleanup)

---

## HIGH — Fix before next release

---

### A3-H1 · `LaunchHandler` — shell injection via `commands.json` target
**File:** `samsara/handlers.py:226`

```python
subprocess.Popen(f'start "" "{target}"', shell=True)
```

`target` comes directly from `commands.json`'s `"target"` field, which is user-editable. `shell=True` with string interpolation means any path containing `&&`, `;`, or `"` can execute arbitrary shell commands. A malicious plugin that writes to `commands.json`, or a user who pastes a bad example from the docs, would trigger this.

**Fix:**
```python
subprocess.Popen(['cmd', '/c', 'start', '', target], shell=False)
```
This removes the shell intermediary. The macOS and Linux paths (`['open', target]`, `['xdg-open', target]`) are already safe — list form only.

---

### A4-H2 · `plugin_commands._REGISTRY` — module-level global mutated by all `CommandExecutor` instances
**File:** `samsara/plugin_commands.py:39`

`_REGISTRY` is a module-level dict. Every `CommandExecutor.__init__` calls `_plugin_commands.load_plugins(...)`, which appends to it. In production there's only one executor, so entries accumulate correctly. But:
- Tests that create multiple `CommandExecutor` instances cross-contaminate — commands registered by one test's plugins are visible in the next test's matcher.
- If a reload/restart path ever creates a second executor (possible during config reload), commands double-register or appear under both executors' matchers.

The `test_modules.py` suite passing doesn't catch this because tests likely create executors sequentially in isolated processes. A test that creates two executors in the same process would fail with duplicate registrations.

**Fix:** `CommandExecutor.__init__` should call `_plugin_commands._REGISTRY.clear()` before `load_plugins`, or the registry should be per-instance rather than global. `clear_shared_matcher()` already exists for tests — a parallel `clear_registry()` function would close the gap.

---

## MEDIUM — Address soon, not blocking

---

### A2-M1 · `process_text` does config disk write on the audio thread
**File:** `samsara/commands.py:256–258, 266–268`

```python
with effective_app._config_lock:
    effective_app.config['command_mode_enabled'] = True
    effective_app.save_config()   # ← full file I/O, lock held
```

`process_text` is called from the transcription worker. `save_config()` does an atomic file write (tmp + rename) with the config lock held. The lock + rename are fast, but this is still synchronous disk I/O on the hot path. If the filesystem stalls (network drive, spinning disk under load), the audio pipeline blocks.

**Fix:** Replace with `effective_app.persist_config()` which releases the lock before writing, or queue the save as a deferred task. The toggle takes effect immediately on `effective_app.command_mode_enabled = True`; the disk write can be async.

---

### A2-M2 · `process_text` uses `self._app` instead of `effective_app` for smart actions
**File:** `samsara/commands.py:297–301`

```python
if self._app is not None and self._is_routing_verb(text):
    ...
    if self._try_smart_actions_route(text):
```

`_is_routing_verb` and `_try_smart_actions_route` both read `self._app` directly. The rest of the method uses `effective_app` (which falls back to `self._app`). If a per-call `app_instance` is passed that differs from the constructor-time `self._app` — which the cheat sheet does — smart actions routing silently uses the wrong app state.

**Fix:** Thread `effective_app` through to `_is_routing_verb` and `_try_smart_actions_route`, or change them to accept an `app` argument instead of reading `self._app`.

---

### A3-M3 · `save_commands` — non-atomic write, can corrupt `commands.json`
**File:** `samsara/commands.py:158`

```python
with open(self.commands_path, 'w', encoding='utf-8') as f:
    json.dump({'commands': self.commands}, f, indent=2)
```

A crash or power loss between the open-for-write (which truncates the file) and `json.dump` completing leaves a zero-byte or partial JSON file. `commands.json` is the entire voice command database — losing it silently degrades to an empty command set on next launch.

`phonetic_wash._save_user_corrections` already implements the correct pattern (tmp file + `tmp.replace(path)`). Apply the same here.

---

### A3-M4 · `command_stats` — unprotected disk I/O in transcription hot path
**File:** `samsara/command_stats.py:19–29`

```python
def _save():
    with open(_STATS_PATH, 'w', encoding='utf-8') as f:   # no try/except
        json.dump(_stats, f, indent=2)

def increment_command_count(name: str):
    with _lock:
        _stats[name] = _stats.get(name, 0) + 1
        if _stats[name] % 5 == 0:
            _save()   # ← disk write on transcription thread, every 5 commands
```

Two issues:
1. `_save()` has no try/except — any I/O error propagates up and would crash `increment_command_count` on the audio thread.
2. The every-5-commands heuristic means frequent commands (scroll, undo) trigger disk writes multiple times per session on the hot path.

**Fix:** Wrap `_save()` in try/except. Consider a background flush thread (e.g., `threading.Timer`) that saves on a delay rather than inline.

---

### A4-M5 · `phonetic_wash` — global state race with Voice Training UI
**File:** `samsara/phonetic_wash.py:186–196`

```python
def reload_corrections():
    global _PHRASE_CORRECTIONS, _WORD_CORRECTIONS
    ...
    _PHRASE_CORRECTIONS = phrase    # replaces the reference
    _WORD_CORRECTIONS = word
```

`apply_phonetic_wash` iterates `_PHRASE_CORRECTIONS` on the transcription thread:
```python
for bad in sorted(_PHRASE_CORRECTIONS, key=len, reverse=True):
```

`reload_corrections()` is called from the Voice Training UI (Qt thread). The `global` assignment replaces the reference atomically in CPython (GIL), but `sorted(_PHRASE_CORRECTIONS, ...)` reads the old reference stored in the iteration state. In CPython this is safe due to the GIL. Under PyPy or a future no-GIL Python this is a genuine data race.

**Fix:** Take a local snapshot at the start of `apply_phonetic_wash`:
```python
phrase_corrections = _PHRASE_CORRECTIONS   # snapshot reference
word_corrections = _WORD_CORRECTIONS
```
One-line fix; makes the intent explicit regardless of GIL assumptions.

---

### A4-M6 · `key_macros` — no lock on shared state accessed from listener thread
**File:** `samsara/key_macros.py:88–89`

```python
self.tap_history = {}
self.held_keys = set()
```

`_on_press` and `_on_release` (pynput listener thread) mutate `tap_history`, `held_keys`, and `current_modifiers`. External callers — `release_all_held()`, `stop()`, `get_held_keys()` — read or modify these from the calling thread. There is no lock anywhere in `KeyMacroManager`.

`release_all_held()` iterates `self.held_keys` in a `for` loop while `_on_press` could be modifying it simultaneously — concrete `RuntimeError: Set changed size during iteration` risk.

**Fix:** Add `self._lock = threading.Lock()` in `__init__` and wrap all state access.

---

## LOW / Code quality

---

### A1-L1 · `samsara_launcher.py` — `check_dependencies` defined but never called
**File:** `samsara_launcher.py:134`

The function exists, is complete, and would catch a missing `faster_whisper`/`pystray`/`sounddevice` installation before a cryptic traceback. `main()` never calls it. Either delete it or wire it in before `launch_app`.

---

### A1-L2 · macOS debug path — unescaped path injection in osascript string
**File:** `samsara_launcher.py:186`

```python
f'tell app "Terminal" to do script "cd {app_dir} && {python_exe} {script}"'
```

If `app_dir` contains spaces or double quotes, this osascript command fails or produces unexpected behaviour. Debug-only, but worth fixing with `shlex.quote`.

---

### A1-L3 · `samsara/__init__.py` — bare Exception swallow loses context
**File:** `samsara/__init__.py:11–17`

```python
try:
    from .commands import CommandExecutor
except Exception:
    CommandExecutor = None
```

A syntax error or import error in `commands.py` silently sets `CommandExecutor = None`. The app then fails later with `AttributeError: 'NoneType'...` — confusing and far from the root cause. At minimum, log the exception so startup output contains the real error.

---

### A2-L4 · `DICTATION_COMMANDS` dict ordering is an implicit invariant
**File:** `samsara/command_parser.py:26`

The comment says "ordered longest-prefix-first" — this works because Python 3.7+ preserves insertion order, but the ordering requirement is not enforced. Adding a new entry in the wrong position silently breaks matching priority. Use a `list of (phrase, name) tuples` or add a startup assertion that verifies descending length order.

---

### A3-L5 · `PressHandler` — bare `cmd['key']` raises `KeyError` on malformed entry
**File:** `samsara/handlers.py:157`

`HotkeyHandler` uses `cmd.get('keys', [])` defensively. `PressHandler` uses `cmd['key']`. A `press` command missing the `"key"` field raises `KeyError` which is caught by `execute_command`'s broad except — not a crash, but inconsistent. Use `.get('key', '')` to match the rest of the handlers.

---

### A3-L6 · `MacroHandler` `type` step silently drops non-ASCII characters
**File:** `samsara/handlers.py:306`

```python
pyautogui.typewrite(step.get('text', ''), interval=0.02)
```

`pyautogui.typewrite` is ASCII-only. Characters above U+007F are silently dropped. A macro that types `"café"` loses the `é` with no warning. Consider clipboard-based paste or `pynput`'s `keyboard.type()` which supports Unicode.

---

### A3-L7 · `TextHandler` hardcoded 0.4s sleep — should use constant
**File:** `samsara/handlers.py:261`

`constants.py` defines `CLIPBOARD_RESTORE_DELAY = 0.139`. `TextHandler` uses its own hardcoded 400ms — nearly 3× longer. Either use the existing constant or add a named one. The sleep blocks the command execution thread.

---

### A3-L8 · `command_stats` — hardcoded `~/.samsara` path, not from constants
**File:** `samsara/command_stats.py:5`

```python
_STATS_PATH = os.path.join(os.path.expanduser('~'), '.samsara', 'command_stats.json')
```

The `~/.samsara/` directory base appears in multiple places across the codebase. A central `SAMSARA_DATA_DIR` constant would prevent drift if the data directory ever changes.

---

### A4-L9 · `plugin_commands.find_command` legacy fallback — end/middle matching inconsistent with registry
**File:** `samsara/plugin_commands.py:135–140`

When no shared matcher is installed, `find_command` matches commands that appear at the END or ANYWHERE in the input. With the registry matcher installed, only **prefix** matching fires. The same utterance can match differently depending on whether a `CommandExecutor` was created. The legacy paths are reachable from `execute_command(text, app=None)` which plugins may call directly.

---

### A4-L10 · `key_macros.py` — no test coverage for thread-sensitive module
**File:** `samsara/key_macros.py`

`KeyMacroManager` is thread-sensitive (pynput listener, mutable shared state) with no visible test file. Given M6 above, tests covering concurrent `stop()` + listener activity would be especially valuable.

---

## Summary table

| ID | Severity | File | Issue |
|---|---|---|---|
| A3-H1 | HIGH | handlers.py:226 | `shell=True` + unsanitized target → command injection |
| A4-H2 | HIGH | plugin_commands.py:39 | Global `_REGISTRY` mutated by all executors → test pollution / double-register |
| A2-M1 | MED | commands.py:256 | `save_config()` disk write on audio thread hot path |
| A2-M2 | MED | commands.py:297 | Smart actions uses `self._app` not `effective_app` |
| A3-M3 | MED | commands.py:158 | `save_commands` non-atomic write → JSON corruption on crash |
| A3-M4 | MED | command_stats.py:19 | `_save()` unprotected + disk I/O every 5 commands on hot path |
| A4-M5 | MED | phonetic_wash.py:186 | Global state replaced while transcription thread iterates it |
| A4-M6 | MED | key_macros.py:88 | No lock on shared state accessed from pynput listener thread |
| A1-L1 | LOW | samsara_launcher.py:134 | `check_dependencies` defined but never called — dead code |
| A1-L2 | LOW | samsara_launcher.py:186 | macOS debug: unescaped paths in osascript string |
| A1-L3 | LOW | samsara/__init__.py:11 | Bare Exception swallow hides startup errors |
| A2-L4 | LOW | command_parser.py:26 | `DICTATION_COMMANDS` ordering is unenforced implicit invariant |
| A3-L5 | LOW | handlers.py:157 | `PressHandler` bare `cmd['key']` — should use `.get()` |
| A3-L6 | LOW | handlers.py:306 | `typewrite` drops non-ASCII silently |
| A3-L7 | LOW | handlers.py:261 | Hardcoded 0.4s sleep — should use constant |
| A3-L8 | LOW | command_stats.py:5 | Hardcoded `~/.samsara` path — no central constant |
| A4-L9 | LOW | plugin_commands.py:135 | Legacy `find_command` end/middle match inconsistent with registry |
| A4-L10 | LOW | key_macros.py | No test coverage for thread-sensitive module |
