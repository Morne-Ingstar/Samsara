# Track C Code Review — TTS / Ava / Smart Actions

**Files:** C1 (tts exceptions/utils/base/__init__) · C2 (coordinator + edge engine) · C3-C4 (winrt_engine) · C5 (ava_memory / corrections / profile) · C6 (smart_actions_tools / bridge / session) · C7 (cloud_llm / learning / premium / cuda_detect / vision / hints)

---

## HIGH — Fix before next release

---

### C2-H1 · `AudioCoordinator.speak()` — state machine race if synthesis is instantaneous
**File:** `samsara/tts/coordinator.py:163–175`

```python
handle = self.engine.speak(text, ..., on_done=_on_tts_done)  # starts worker thread
self._active_handle = handle
self._active_interruptible = interruptible
self.transition_to(SPEAKING, ...)    # called AFTER engine is already running
```

`on_done` fires `transition_to(IDLE)`. If the utterance completes before `transition_to(SPEAKING)` executes (possible for very short text on a fast machine, or with a mock engine in tests), the sequence is:

1. `on_done` → `transition_to(IDLE)` — rejected, current state is already IDLE
2. `transition_to(SPEAKING)` succeeds — coordinator stuck in SPEAKING with no active utterance

**Fix:** Call `transition_to(SPEAKING)` **before** `engine.speak()` so the state is set before the worker can fire `on_done`.

---

### C4-H2 · `WinRTEngine._tts_callback` — threading.Lock inside PortAudio callback causes audio glitches
**File:** `samsara/tts/winrt_engine.py:192–208`

```python
def _tts_callback(self, outdata, frames, time_info, status):
    with self._tts_buffer_lock:   # ← acquires Python threading.Lock
        ...
```

PortAudio callbacks run in a high-priority real-time thread. Acquiring a `threading.Lock` inside a PortAudio callback blocks if any Python thread is currently holding `_tts_buffer_lock` (e.g., `cancel()` rebuilding the deque, or `_push_chunks()` appending). Any contention causes a buffer underrun and audible glitch.

**Fix:** Use lock-free deque operations instead. `collections.deque.append()` and `.popleft()` are GIL-atomic in CPython. Replace the `with self._tts_buffer_lock:` in the callback with bare deque operations. Keep the lock only in `cancel()` and `_push_chunks()` where you rebuild/clear the deque (these are not real-time paths).

---

### C4-H3 · `_build_voice_list_from_registry` — `base_key` handle leaked
**File:** `samsara/tts/winrt_engine.py:381`

```python
base_key = winreg.OpenKey(hive, base_path)
# ... while True loop with no CloseKey for base_key
```

`base_key` is opened but never closed. The `while True` loop exits via `OSError` from `EnumKey` (expected end-of-keys) without a `finally` block. With two registry paths and potentially multiple calls, handle counts accumulate.

**Fix:** Use `with winreg.OpenKey(hive, base_path) as base_key:`.

---

## MEDIUM — Address soon, not blocking

---

### C2-M1 · `EdgeTTSEngine.set_volume` is a silent no-op — ducking doesn't work with Edge engine
**File:** `samsara/tts/edge_tts_engine.py:280`

```python
def set_volume(self, handle: SpeechHandle, volume: float, fade_ms: int = 5) -> None:
    pass  # Phase 1: no per-utterance volume adjustment
```

`AudioCoordinator.on_earcon_starting()` calls `engine.set_volume(handle, duck_factor)` when an earcon fires during TTS. With `EdgeTTSEngine`, this silently does nothing — earcon ducking is completely disabled without any log or warning. Users who configure Edge TTS won't get earcon ducking and won't know why.

**Fix (minimal):** Log a single `logger.debug` so it's visible during testing. The full fix requires buffering the PCM and applying volume — document as deferred and add a test that verifies the no-op doesn't raise.

---

### C2-M2 · `_apply_speaking_thresholds` mutates `app.config` without the config lock
**File:** `samsara/tts/coordinator.py:438–445`

```python
ww_audio = self.app.config.get('wake_word_config', {}).get('audio', {})
ww_audio['speech_threshold'] = raised    # ← unprotected mutation
```

`app.config` is protected by `app._config_lock` everywhere else. This method mutates a nested dict without it. If a config reload or save runs concurrently (e.g., user changes settings while TTS is playing), this write is unprotected.

**Fix:** Wrap the config mutation in `with self.app._config_lock:` or use `app.update_config_and_save()` if available.

---

### C2-M3 · `_duck_timers` list never shrinks during sustained use
**File:** `samsara/tts/coordinator.py:210–213`

Every `on_earcon_starting()` appends a new `threading.Timer` to `_duck_timers`. Fired timers are not removed from the list; only `_cancel_duck_timers()` on speech exit clears it. During a long TTS segment with many earcons (e.g., 20+ command confirmations), the list grows indefinitely within that SPEAKING window, holding references to already-fired timers.

**Fix:** In `_on_duck_restore`, after decrementing `_duck_depth`, remove the corresponding timer from `_duck_timers` under the lock.

---

### C5-M4 · `ava_corrections._save()` and `ava_profile._save_locked()` — non-atomic writes
**Files:** `samsara/ava_corrections.py:60–62`, `samsara/ava_profile.py:100–104`

Both use the pattern:
```python
with open(path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=2)
```

No temp-file-and-rename. A crash between the truncate and the write completion corrupts the file. `ava_memory.save()` in the same codebase already implements atomic writes correctly (tmp + `os.replace`). Apply the same pattern here.

---

### C6-M5 · `_tool_paste_text` overwrites clipboard without save/restore
**File:** `samsara/smart_actions_tools.py:446–454`

```python
pyperclip.copy(text)
pyautogui.hotkey('ctrl', 'v')
```

The existing clipboard contents are silently overwritten. `handlers.TextHandler` in Track A saves and restores the clipboard with `clipboard_lock`. This tool should do the same — the user may have copied something important moments before the agent pastes.

---

### C6-M6 · `_check_scope` webhook URL check — subdomain bypass possible
**File:** `samsara/smart_actions_tools.py:278–282`

```python
for domain in self.allowed_domains:
    if url.startswith(domain):
        return True, "OK"
```

If `allowed_domains` contains `"https://example.com"`, this also matches `"https://example.com.evil.com/steal"`. String prefix is insufficient.

**Fix:**
```python
from urllib.parse import urlparse
netloc = urlparse(url).netloc.split(':')[0].lower()
for domain in self.allowed_domains:
    allowed = urlparse(domain).netloc.split(':')[0].lower() or domain.lower()
    if netloc == allowed or netloc.endswith('.' + allowed):
        return True, "OK"
```

---

### C6-M7 · `_qt_confirm_dialog` blocks for up to 120 seconds; never unblocks on app shutdown
**File:** `samsara/smart_actions_tools.py:407`

```python
QTimer.singleShot(0, qt_app, _make)
done.wait(timeout=120)
```

If the Qt event loop is not running (app shutting down, or during early init), the dialog never appears and the worker thread blocks for 2 minutes before timing out and returning `(False, False)`. No log message indicates what happened.

**Fix:** Reduce timeout to 30s. Add a log warning when the timeout expires without a user response.

---

### C7-M8 · `cloud_llm` — unused lock, hardcoded limits, stale model ID
**File:** `samsara/cloud_llm.py`

Three separate issues in one file:

1. `_config_lock` (line 12) is defined but never acquired — dead code.
2. `max_tokens=300` (line 101) is hardcoded — too tight for substantive Ava responses. Should be configurable via `config.cloud_llm.max_tokens`.
3. Anthropic model ID `"claude-sonnet-4-20250514"` (line 39) is hardcoded. If this ID is wrong or is retired, all Anthropic calls fail silently with an API error. Should come from config with this as a default.

---

### C7-M9 · `HintManager.increment()` — disk write on every command (hot path)
**File:** `samsara/hints.py:77–79`

```python
def increment(self, counter_id: str) -> int:
    self._counters[counter_id] = self._counters.get(counter_id, 0) + 1
    self._save()   # ← full JSON write every time
    return self._counters[counter_id]
```

`increment('command_count')` is called from the transcription path after every successful command — same hot-path disk write pattern flagged in Track A (`command_stats`). The counter precision doesn't need sub-second durability.

**Fix:** Batch saves: only write when `count % 10 == 0`, or use a debounced background flush (e.g., `threading.Timer(5.0, self._save)`).

---

## LOW / Code quality

---

### C2-L1 · `AudioCoordinator.is_speaking` reads `_state` without lock
**File:** `samsara/tts/coordinator.py:119`

```python
@property
def is_speaking(self) -> bool:
    return self._state == SPEAKING
```

`get_state()` (line 241) acquires `_state_lock` for consistency. `is_speaking` does not. CPython GIL makes the read atomic in practice, but the inconsistency is a code smell and a trap for future refactors. Either document it as intentionally lock-free (same reasoning as `SmartActionsSession.is_expired()`) or use `get_state()`.

---

### C4-L2 · `_synthesize_mp3` minimum byte estimate is fragile for non-English
**File:** `samsara/tts/edge_tts_engine.py:88`

```python
min_expected_bytes = max(2048, int(len(text) / 12.0 * 3500))
```

Assumes English speech rate (~12 chars/sec). For CJK text (Chinese/Japanese/Korean), character density is much higher and chars-per-second can be 4-6× lower. This means the `min_expected_bytes` is set far too high for CJK input, causing the retry loop to always think the stream was truncated.

---

### C5-L3 · Module-level `_load()` at import — file I/O during module load
**Files:** `samsara/ava_corrections.py:192`, `samsara/ava_profile.py:239`

Both modules call `_load()` at the bottom of the file, triggering file I/O at import time. Consistent with `phonetic_wash.py` (flagged Track A-L1), but worth noting as an import-time side effect that complicates mocking in tests.

---

### C5-L4 · `ava_corrections._dirty_count` — bool named like a counter
**File:** `samsara/ava_corrections.py:17`

`_dirty_count = False` is a bool flag. Rename to `_dirty` to match its actual usage.

---

### C6-L5 · `run_shell_command` included in `_default_available_tools()` but is a placeholder
**File:** `samsara/smart_actions_tools.py:46, smart_actions_bridge.py:25–29`

`run_shell_command` is in `TOOL_TIERS` as `TIER_ALWAYS_CONFIRM`, in `_default_available_tools()`, and announced to the remote agent as an available tool — but its implementation returns `{'success': False, 'result': 'not yet implemented — Phase 3'}`. The agent will attempt to call it, the confirmation dialog will appear, the user approves, and then receives a failure. Remove from `_default_available_tools()` until implemented.

---

### C7-L6 · `premium._hash_key` — defined but never called
**File:** `samsara/premium.py:19–21`

Dead code, presumably planned for Phase 2 server-side validation. Remove or add a `# TODO Phase 2` comment.

---

### C7-L7 · `cloud_llm.check_available` for Anthropic — always returns wrong result
**File:** `samsara/cloud_llm.py:150–151`

```python
if provider == "anthropic":
    r = requests.get(base_url.rstrip('/'), timeout=3)
```

`base_url` for Anthropic is `https://api.anthropic.com/v1`. GETting that root path returns a 404 or redirect, not a health signal. `check_available` will always return `True` (if a response is received, even 404) or `False` (connection error) regardless of whether the API key is valid or the service is up.

---

### C7-L8 · `AdaptiveLearner` — no lock, called from multiple contexts
**File:** `samsara/learning.py`

`record_transcription()` and `record_correction()` mutate `recent_transcriptions` and `candidates` without a lock. If the voice training UI reads `recent_transcriptions` concurrently with a dictation completing, a partial list could be returned. Low risk in practice but inconsistent with the rest of the codebase.

---

### C7-L9 · `vision.py` — privacy doc comment may be misleading
**File:** `samsara/vision.py:4–6`

The module docstring says "screenshots never leave this machine." `_host()` reads from `config.ollama.host`, which defaults to `localhost` but can be configured to any URL. If the user points Ollama at a remote host, images do leave the machine. The privacy guarantee is conditional on `host` staying as localhost. Amend the docstring: "by default, screenshots never leave this machine; ensure `ollama.host` points to a local instance."

---

## Summary table

| ID | Severity | File | Issue |
|---|---|---|---|
| C2-H1 | HIGH | coordinator.py:163 | Race: engine starts before SPEAKING transition — state can get stuck |
| C4-H2 | HIGH | winrt_engine.py:192 | threading.Lock inside PortAudio callback → audio glitches |
| C4-H3 | HIGH | winrt_engine.py:381 | Registry base_key handle leaked in voice enumeration |
| C2-M1 | MED | edge_tts_engine.py:280 | set_volume is no-op — ducking silently disabled for Edge engine |
| C2-M2 | MED | coordinator.py:438 | config mutation without config lock during threshold adjustment |
| C2-M3 | MED | coordinator.py:210 | _duck_timers list never shrinks during sustained speech |
| C5-M4 | MED | ava_corrections.py:60, ava_profile.py:100 | Non-atomic writes — JSON corruption on crash |
| C6-M5 | MED | smart_actions_tools.py:446 | paste_text overwrites clipboard without save/restore |
| C6-M6 | MED | smart_actions_tools.py:278 | Webhook URL prefix check — subdomain bypass possible |
| C6-M7 | MED | smart_actions_tools.py:407 | Confirm dialog: 120s timeout, silent if Qt loop not running |
| C7-M8 | MED | cloud_llm.py:12,39,101 | Unused lock + hardcoded max_tokens=300 + stale Anthropic model ID |
| C7-M9 | MED | hints.py:77 | Disk write on every command via increment() — hot path I/O |
| C2-L1 | LOW | coordinator.py:119 | is_speaking reads _state without lock (inconsistent with get_state) |
| C4-L2 | LOW | edge_tts_engine.py:88 | Byte estimate for short-stream detection breaks on CJK text |
| C5-L3 | LOW | ava_corrections.py:192, ava_profile.py:239 | File I/O at module import time |
| C5-L4 | LOW | ava_corrections.py:17 | _dirty_count is a bool, name implies counter |
| C6-L5 | LOW | smart_actions_tools.py + bridge.py | run_shell_command advertised to agents but is placeholder |
| C7-L6 | LOW | premium.py:19 | _hash_key defined but never called — dead code |
| C7-L7 | LOW | cloud_llm.py:150 | check_available for Anthropic GETs root URL — always wrong |
| C7-L8 | LOW | learning.py | AdaptiveLearner has no lock — concurrent reads/writes possible |
| C7-L9 | LOW | vision.py:4 | Privacy doc misleading when host is non-local |
