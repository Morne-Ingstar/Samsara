# Track G1-G4 Code Review — Core Test Suite

**Files:** G1 (conftest + test_modules) · G2 (audio_engine tests) · G3 (wake word tests) · G4 (handler/executor/parser/registry/packs tests)

---

## HIGH — Tests that can't catch real bugs

---

### G1-H1 · `test_modules.py` — entire file tests `_stale/` dead code, not active modules
**File:** `tests/test_modules.py`

Every test class in this file imports from `samsara._stale.*`:
```python
from samsara._stale.config import Config
from samsara._stale.audio import AudioCapture, AudioPlayer
from samsara._stale.speech import SpeechRecognizer, TextProcessor
```

The `_stale/` directory exists explicitly for superseded code. This file provides 127 lines of test coverage for modules that are no longer used. When the active modules have bugs, these tests cannot catch them. When `_stale/` is eventually deleted, these tests disappear too.

`TestModuleImports.test_import_submodules` (line 372) imports all `_stale` classes and asserts they are `not None` — the weakest possible assertion.

**Fix:** Redirect these tests to the live equivalents (`samsara.commands`, `samsara.cleanup`, `samsara.phonetic_wash`, `samsara.constants`), or delete them if they duplicate coverage in other test files.

---

### G3-H2 · `test_wake_word.py` — tests Python built-ins, not any samsara module
**File:** `tests/test_wake_word.py`

The entire file tests `wake_word.lower() in text.lower()` — Python's built-in string containment. No actual samsara module is imported or called:

```python
# Lines 15–68: entire TestWakeWordDetection
def test_exact_match(self):
    wake_word = "samsara"
    text = "samsara open browser"
    assert wake_word.lower() in text.lower()   # tests str.find(), not samsara
```

`TestWakeWordModeFlow` creates `Mock()` objects and sets attributes, never calling real code. `TestWakeWordIntegration` imports `CommandExecutor` (line 147) but never uses it to match a wake word.

All 179 lines of tests pass trivially. A completely broken wake word detector would not be caught.

**Fix:** Import and test the real implementation — `samsara.wake_word_matcher.match_wake_phrase`, `samsara.wake_detector.WakeWordDetector`, or whichever module owns this logic. `test_wake_word_matcher.py` already does this correctly; `test_wake_word.py` should either be deleted or rewritten to test the same real functions.

---

### G2-H3 · `test_interfaces.py` line 55 — broken assertion always passes
**File:** `tests/audio_engine/test_interfaces.py:55`

```python
assert engine._stream is engine_stream_ref if False else True  # noqa
```

Due to Python operator precedence this evaluates as:
```python
assert (engine._stream is engine_stream_ref if False else True)
# → assert True
```

The condition `if False` is never evaluated. The assert is `assert True` — it never fails. The intended test (that `start()` is idempotent and doesn't open a new stream) is completely untested.

**Fix:**
```python
assert engine._stream is stream_ref, "start() opened a new stream on second call"
```

---

## MEDIUM — Gaps that reduce signal from the test suite

---

### G1-M1 · `conftest._isolate_plugin_registry` fixture — directly addresses Track A-H2
**File:** `tests/conftest.py:14`

The autouse fixture that clears and restores `_plugin_commands._REGISTRY` was the right response to the global-state contamination flagged as Track A-H2. It works correctly. However:

- It saves/restores `_REGISTRY` but NOT the list of loaded plugin modules in `sys.modules`. Plugins imported during a test stay imported and their `@command` decorators won't re-fire even after `_REGISTRY.clear()`. If a test relies on fresh plugin registration, it may see stale state from a previously imported plugin module.
- The fixture prevents the *production* plugins from loading (`if plugins_dir.resolve() == project_plugins.resolve(): return 0`) but allows test-local plugins. This is correct, but the comment says "Tests that want plugin behavior can register via the @command decorator" — there's no documented test showing this pattern. A test that actually needs a plugin fixture would need to discover this by reading conftest.

---

### G2-M2 · `test_ring.py` — no test for concurrent access (single-threaded only)
**File:** `tests/audio_engine/test_ring.py`

`test_ring.py` is entirely single-threaded. Concurrent stress is in `test_ring_concurrent.py`. This is fine architecturally, but the two files should be run together to get meaningful coverage of the ring buffer. If only `test_ring.py` is run (e.g., `pytest tests/audio_engine/test_ring.py`), thread safety is completely untested.

There is also no test for the prebuffer underflow edge case when `write_cursor` is at 0 or 1 (rewind would go negative; implementation clamps). Only 3-frame clamping is tested (line 161).

---

### G2-M3 · `test_equivalence.py` — hardware-dependent, flaky on the existing machine
**File:** `tests/audio_engine/test_equivalence.py`

The `test_rms_within_6dB` test is currently failing in the CI-equivalent run with a 17.34 dB RMS difference (you saw this when running pytest). The 6 dB threshold is sound in principle but the test is fragile:
- It depends on ambient noise levels at capture time
- It depends on hardware (microphone gain, ADC characteristics)
- It fails silently on mute/no-input because silent audio is skipped (`if rms < 1e-6: pytest.skip()`) — but that line may not be reached if the capture itself returns zeros

This test should be in a dedicated `@pytest.mark.hardware` marker and excluded from default runs (`-m "not hardware"` in `pytest.ini`).

---

### G3-M4 · `test_wake_word_pipeline.py` — reimplements logic instead of importing it
**File:** `tests/test_wake_word_pipeline.py:145–151`

```python
@staticmethod
def _strip(text):
    FILLERS = frozenset({'please', 'uh', 'um', 'like'})
    words = text.split()
    while words and words[0] in FILLERS:
        words.pop(0)
    ...
```

The filler stripping logic is re-implemented inline in the test instead of importing `strip_fillers` from `samsara.command_parser`. If the real implementation changes (e.g., adds a new filler word), this test won't catch the divergence. Same pattern exists for the dictation command parsing tests that check `text.lower().startswith(cmd + ' ')` (line 105) — testing Python's `str.startswith`, not the parser.

---

### G4-M5 · `test_command_mode.py` — `MockApp` duplicates real DictationApp logic
**File:** `tests/test_command_mode.py:263–330`

`_MockApp` reimplements `enter_command_mode()`, `exit_command_mode()`, and `_on_command_mode_inactivity()` — real methods of `DictationApp`. If the real implementation changes (timer constants, state flags, lock names), the mock diverges silently and tests continue to pass against behaviour that no longer exists.

**Better pattern:** Use a real `DictationApp` instance in a `@pytest.fixture` that stubs only the hardware (audio device, model loading) rather than reimplementing the business logic. The existing `test_dictation_app.py` probably already does this — verify there's no duplication.

---

### G4-M6 · `test_handlers.py` — hotkey execution order not validated
**File:** `tests/test_handlers.py:68–76`

```python
calls = mock_kb.mock_calls
assert calls[0] == call.press(ctrl)
# ... but order not verified as a sequence assertion
```

The test checks individual call indices but not that they arrive in the legally correct order: `press(ctrl)` → `press(c)` → `release(c)` → `release(ctrl)`. Out-of-order key events (e.g., `release(ctrl)` before `release(c)`) would cause incorrect keyboard output in practice.

**Fix:**
```python
assert mock_kb.mock_calls == [
    call.press(ctrl), call.press(c), call.release(c), call.release(ctrl)
]
```

---

## LOW / Code quality

---

### G1-L1 · `conftest.py` — `qapp` fixture is session-scoped but never used
**File:** `tests/conftest.py:293`

```python
@pytest.fixture(scope="session")
def qapp():
    ...
```

This fixture creates a session-scoped `QApplication` for Qt widget tests. None of the G1-G4 test files reference it. Tests that do use Qt (e.g., `test_settings.py`) appear to create their own `QApplication` or use PySide6's own test utilities. The fixture is dead code in conftest.py. Either document which tests use it or remove it.

---

### G2-L2 · `test_ring_concurrent.py` — timing-dependent assertion could flake on slow CI
**File:** `tests/audio_engine/test_ring_concurrent.py:282`

```python
elapsed = time.monotonic() - t_start
assert elapsed < 5.0, f"Writer stalled: {elapsed:.2f}s"
```

The writer runs for `RUN_SECONDS = 6` (line 36) and is expected to finish within 5 seconds of the test timeout. On a heavily loaded CI machine or under GC pressure, 5 seconds is tight. The zombie-reader test (verifies non-blocking) validates the more important property; the wall-clock assertion adds fragility without adding correctness signal.

---

### G3-L3 · `test_command_mode.py` — line 504 imports from `dictation` — undefined import path
**File:** `tests/test_command_mode.py:504`

```python
from dictation import DictationApp
```

`dictation` is not a package-qualified import (`from samsara.dictation` or similar). It relies on `sys.path` having the project root, which is set in `conftest.py` (line 11). If the test is run from a different working directory or without the conftest injection, this import fails with `ModuleNotFoundError`. Should be `from dictation import DictationApp` only if `dictation.py` is at the repo root — which it is — but the import is fragile. Consider `from pathlib import Path; sys.path.insert(0, str(Path(...).parent))` or a proper package structure.

---

### G4-L4 · `test_command_packs.py` — no test that disabling 'core' pack is a no-op
**File:** `tests/test_command_packs.py:128`

`PACKS['core']['always_on'] = True` is verified to cause 'core' to be always enabled. But there's no test that passing `{'core': False}` in user config has no effect:

```python
# Missing test:
config = {'command_packs': {'core': False}}
enabled = get_enabled_packs(config)
assert 'core' in enabled  # always_on should override user preference
```

`get_enabled_packs` correctly handles this (the `always_on` check runs before the `user_packs.get()` branch), but it's untested.

---

### G4-L5 · `test_command_parser.py` — filler set assumed, not validated
**File:** `tests/test_command_parser.py:38`

Tests call `strip_fillers(text)` with no fillers argument and expect 'please', 'uh', 'um', 'like' to be stripped. The default filler set in the actual implementation (`command_parser.DEFAULT_FILLERS`) is `frozenset({'please', 'uh', 'um', 'like'})`. If a new filler is added or one is removed, these tests would silently diverge from the documented API. One test should assert `strip_fillers("um uh please like test")` == `"test"` to pin the full default set.

---

### G4-L6 · `test_command_executor.py` — no test of `process_text` remainder
**File:** `tests/test_command_executor.py`

`CommandExecutor.process_text("open chrome browser", app)` should match "open chrome" (2-token command) with remainder "browser". No test exercises the remainder path from `process_text`. Remainder text is the primary mechanism for plugin parameter passing — its correctness under `process_text` is untested.

---

## Coverage gaps relative to complexity

| Module | Complexity | Coverage | Missing |
|---|---|---|---|
| `command_registry.py` (CommandMatcher) | HIGH | Good | No concurrent freeze/load race test |
| `coordinator.py` (AudioCoordinator) | HIGH | None in G1-G4 | Entirely untested in this track (Track C) |
| `winrt_engine.py` | HIGH | None in G1-G4 | PortAudio callback, voice enumeration |
| `phonetic_wash.py` | MED | Via test_phonetic_wash.py (G7) | Reload_corrections thread safety |
| `key_macros.py` | MED | **Zero coverage** | No test file anywhere |
| `edge_tts_engine.py` | MED | None in G1-G4 | Retry logic, truncation detection |
| `smart_actions_tools.py` | MED | None in G1-G4 | Scope check, consent dialog, tool dispatch |
| `ava_corrections.py` | LOW | None in G1-G4 | Teaching/forget/query parsing |

---

## Summary table

| ID | Severity | File | Issue |
|---|---|---|---|
| G1-H1 | HIGH | test_modules.py | All tests target `_stale/` dead code — no active module coverage |
| G3-H2 | HIGH | test_wake_word.py | No module imported — tests Python built-ins, all trivially pass |
| G2-H3 | HIGH | test_interfaces.py:55 | `if False else True` broken assertion — idempotency untested |
| G1-M1 | MED | conftest.py:14 | Plugin isolation fixture doesn't clear sys.modules for imported plugins |
| G2-M2 | MED | test_ring.py | Single-threaded only; no hint to run with test_ring_concurrent.py |
| G2-M3 | MED | test_equivalence.py | Hardware test should be marked; currently failing (17 dB diff) |
| G3-M4 | MED | test_wake_word_pipeline.py:145 | Filler stripping reimplemented in test, not imported |
| G4-M5 | MED | test_command_mode.py:263 | MockApp duplicates real app logic — divergence risk |
| G4-M6 | MED | test_handlers.py:68 | Hotkey execution order not validated as a sequence |
| G1-L1 | LOW | conftest.py:293 | `qapp` fixture defined but never used in G1-G4 |
| G2-L2 | LOW | test_ring_concurrent.py:282 | Wall-clock assertion could flake on slow CI |
| G3-L3 | LOW | test_command_mode.py:504 | `from dictation import ...` fragile without explicit sys.path |
| G4-L4 | LOW | test_command_packs.py | No test that user config `{'core': False}` is ignored |
| G4-L5 | LOW | test_command_parser.py | Default filler set not explicitly validated |
| G4-L6 | LOW | test_command_executor.py | `process_text` remainder path untested |
