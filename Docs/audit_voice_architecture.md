# Audit Brief: Voice Command Architecture

> Self-contained review doc. Paste the framing + questions into ARC's Problem
> box and the code appendix into Project Context, or hand the whole thing to a
> single reviewing model (Claude/GPT/Gemini).

---

## 1. What this project is

Samsara is a hands-free Windows voice-control tool built for accessibility
(user has hand pain, wants to stay off the keyboard). The architecturally
interesting part for this audit is the **command routing + wake-word
pipeline**: the path from "user speaks into mic" to "action fires."

Rough flow for a single spoken command:

```
mic audio (sounddevice callback, 16kHz chunks)
  -> RMS-gated silence detector (wake_word_audio_callback)
  -> buffered until silence window satisfied
  -> Whisper transcription (faster_whisper, on worker thread)
  -> corrections dict (voice_training)
  -> [if in dictation state] cancel/end/pause/resume words OR buffer
  -> [if asleep] apply_wake_corrections, match_wake_phrase, strip_wake_echoes
  -> CommandExecutor.find_command  (builtins in commands.json, then plugins)
  -> dispatch:
       type=hotkey|press|key_down|key_up|release_all|mouse|launch|text -> pynput
       type=method                                                      -> getattr(app, name)()
       plugin                                                            -> handler(app, remainder)
  -> [if dictation-bound output] clipboard preserve + paste + restore (~0.5s)
```

## 2. Scope

**In scope:**
- `samsara/command_parser.py` — normalization, filler stripping, echo stripping
- `samsara/plugin_commands.py` — plugin registry + plugin-side find_command
- `samsara/wake_word_matcher.py` — word-boundary wake phrase matcher
- `samsara/commands.py` — the "modular" `CommandExecutor` (used by tests)
- `dictation.py` — the in-app `CommandExecutor` (used at runtime) and the wake
  audio callback + `process_wake_word_buffer` (the bulk of real dispatch)

**Out of scope:**
- Transcription model tuning, UI widgets, notifications, alarms, voice
  training, clipboard module internals, build/release.

## 3. Goals of this audit

1. **Smooth the architecture** — where are the seams that have been cracking
   as features pile on? What would a cleaner factoring look like?
2. **Make it snappier** — where does wall-clock latency actually go from
   end-of-speech to action-fires, and what's reasonable to squeeze?

Not looking for: a ground-up rewrite proposal. Looking for: the two or three
highest-leverage changes.

## 4. Known history and constraints

- Everything runs on Windows, single user. macOS/Linux portability is
  aspirational, not required.
- Transcription is the dominant latency source in practice, but it's
  externally bounded by Whisper — this audit is about everything *else*.
- There are currently **13 plugin commands** across 6 plugin modules and
  **~105 built-in commands** in `commands.json`. Growth expected.
- Plugin priority is deliberately "built-ins win" (original rationale: keep
  user JSON authoritative). This caused three substring collisions with new
  multi-word plugins (`"search"`/`"search for X"`, `"find"`/`"find tab X"`,
  `"tab"`/`"switch to tab X"`). Two collisions resolved by deleting the
  built-in; one left intact (`"find"`/`"find tab X"` — still collides).
- Wake-word state machine is 4-state: `asleep -> command_window ->
  {quick_dictation, long_dictation}`.
- Pause/resume in `long_dictation` was just added as a boolean flag
  (`_dictation_paused`) rather than a proper state.
- A new `type: "method"` command type dispatches to a named DictationApp
  method (currently only used by `undo_last_dictation`, but generic).

## 5. Seams I've noticed while working in this codebase

### 5.1 Two `CommandExecutor` implementations, drifting

`dictation.py:236` defines a full in-app `CommandExecutor`. `samsara/commands.py:77`
defines a near-parallel one that tests import. Every recent feature
(plugin wiring, `type: method`, even just the 3-collision fix logic) had to be
applied in both, and the two have subtly different `find_command` matching
(in-app does startswith/endswith/word-boundary; modular does padded
boundary only). The "correct" runtime behavior lives in dictation.py; tests
exercise `samsara/commands.py`.

### 5.2 `find_command` uses first-match-in-dict-order (no longest-match)

In-app `find_command`:

```python
for cmd_name in self.commands:
    if text_lower.startswith(cmd_name + " ") ... return cmd_name
    if text_lower.endswith(" " + cmd_name)   ... return cmd_name
    if f" {cmd_name} " in f" {text_lower} " ... return cmd_name
return None  # then consults plugin registry
```

`plugin_commands.find_command` DOES sort longest-phrase-first. The
inconsistency is how the `"find"` vs `"find tab"` collision arose — builtin
`"find"` wins because it appears first via `startswith`, even though plugin
`"find tab"` is a more specific match.

### 5.3 `process_wake_word_buffer` is monolithic

One function (`dictation.py:2044–2243`, ~200 lines) does: audio resample,
transcription, corrections, performance logging, cancel-word detection,
end-word detection, pause/resume state machine, pre-pause buffering,
post-pause ignore, text accumulation, silence-timer restart, wake correction
map, wake phrase matching, substring rejection, wake echo stripping, command
text extraction, noise filtering, dispatch to `_process_wake_command`.
Hard to reason about, hard to profile, and the ordering of the checks (cancel
→ end → pause/resume → buffer) encodes behavior that isn't obvious from the
state machine description.

### 5.4 Dispatch targets are heterogeneous

- `commands.json` types: `hotkey | press | key_down | key_up | release_all |
  mouse | launch | text | macro | method`
- Plugin callable: `handler(app, remainder)`
- App-level methods via `type: method`: `handler() -> bool|None`

No shared dispatch interface. Adding a new action type requires editing
`execute_command`'s big `if/elif` chain AND probably both CommandExecutors.

### 5.5 Wake phrase extraction is string slicing plus patch-up

```python
command_text = corrected_lower[match_index + len(wake_phrase):].strip()
command_text = normalize_command_text(command_text)
command_text, echo_count = strip_wake_echoes(command_text, wake_phrase)
if echo_count:
    command_text = normalize_command_text(command_text)  # re-run
```

The re-run of `normalize_command_text` after echo stripping is a smell. We
have a tokenization story implicitly (wake_word_matcher uses word
boundaries; strip_wake_echoes uses `\b`; normalize_command_text strips
leading non-word chars) but no explicit token stream.

### 5.6 Config is the source of truth for lots of spoken surface area

Spoken phrases live in: `commands.json`, each plugin file's `@command`
decorator, plus user-editable config dicts (`audio_devices`, `web_shortcuts`
— these resolve at runtime in plugin handlers via `app.config.get(...)`).
There's no startup step that:
- lists all active spoken phrases,
- detects collisions between built-ins and plugins,
- warns on user config entries that shadow built-ins.

### 5.7 Threading model

- Audio callback runs in sounddevice's callback thread.
- When a speech buffer closes, a **new daemon thread** is spawned per
  utterance for `process_wake_word_buffer`, which does transcription AND
  dispatch inline.
- Plugin handlers run on that same daemon thread, synchronously.
- Command dispatch that needs UI updates uses `self._schedule_ui(...)` /
  `self.root.after(0, ...)`.
- The `model_lock` serializes Whisper calls; two back-to-back utterances
  queue up.

No obvious correctness bug, but: if a plugin handler does something slow
(web fetch, big subprocess), the NEXT utterance's dispatch is delayed — not
the transcription, because it's a different thread, but the user's
perception is "Samsara is stuck."

### 5.8 Paste latency is fixed overhead

For any dictation output:
- `CLIPBOARD_PASTE_DELAY = 0.05s` (after copy, before `Ctrl+V`)
- `CLIPBOARD_RESTORE_DELAY = 0.15s` (after paste, before clipboard restore)
- Plus the pyautogui.hotkey call itself.

That's ~250ms of unavoidable lag on top of transcription. Any lower and
clipboard restore can clobber the paste before the target app processes it.
Is there a better mechanism (SendInput directly, or target-app-specific
pastes)?

### 5.9 Pre-existing failing tests concentrated in command routing

33 out of 280 tests fail on master, all in `test_dictation_app.py` and
`test_integration.py`. They were already failing before recent work, and
they're in the exact modules this audit cares about.

## 6. Questions for the reviewer

### 6.1 Consolidation

The two `CommandExecutor` implementations: are they actually supposed to be
one thing? If so, which one is canonical, and how would you untangle the
in-app version's coupling to `DictationApp` state so the modular one could
be the runtime? If not, what's the invariant that should keep them in sync?

### 6.2 Matching semantics

Given the three collisions seen this session (`"search"` vs `"search for X"`,
`"tab"` vs `"switch to tab X"`, `"find"` vs `"find tab X"`), what's the
*right* matching rule? Candidates:

- A. Keep first-match-wins; tell users to avoid overlapping phrases.
- B. Longest-match-wins across built-in + plugin combined; exact match still
  prefers built-in.
- C. Trie / suffix-index for O(k) longest-match, with startup collision report.
- D. Something else.

What's the tradeoff analysis?

### 6.3 Pipeline shape

`process_wake_word_buffer` as a sequence of named stages (e.g. `[transcribe,
apply_corrections, detect_cancel, detect_end, handle_pause_state,
extract_wake_command, dispatch]`) — is that a worthwhile refactor? What
would the stage interface look like? Any pitfalls (stages need access to
config/state/ui scheduler)?

### 6.4 Dispatch unification

Is it worth formalizing a `CommandHandler` protocol
(`execute(context) -> bool`) so that built-in hotkey/text/launch,
plugin callables, and `type: method` all satisfy the same interface? Or is
the current `if cmd_type == ...` chain fine for 10 types and we're
overthinking it?

### 6.5 Latency budget

End-to-end latency from end-of-speech to action-fires is roughly:
`silence_window + transcribe_time + dispatch + (paste_latency_if_output)`.
Given `silence_window` is user-configured (0.8s default for wake
detection, 1.0s for quick_dictation) and `transcribe_time` is externally
bounded: where's the first 100ms to cut from the rest? Is the `0.05+0.15s`
clipboard overhead actually necessary on modern Windows?

### 6.6 Plugin handler contract

Current: `handler(app, remainder) -> bool|None`. I've been adding
`**kwargs` in new plugins to future-proof. Should the contract be formalized
(e.g., `handler(context) -> Result` where context has `app`, `remainder`,
`raw_text`, `source='voice'|'hotkey'`, `emit_feedback(...)`)? What's lost in
going from positional to context-object, and what's gained?

### 6.7 Discoverability & collision detection

Worth adding a startup phase that walks: `commands.json` keys ∪ plugin
canonical phrases ∪ plugin aliases ∪ config dicts referenced by plugins
(`audio_devices`, `web_shortcuts`) → and emits a warning log + a listable
registry? Or is it YAGNI until we hit more collisions?

### 6.8 Pause/resume as state

Right now: `_dictation_paused: bool` flag gating a branch inside `if
self.app_state == 'long_dictation':`. Should the 4-state machine become 5
(adding `long_dictation_paused`) so the pause condition is encoded in state
rather than a side flag? Or is a flag fine because it's truly one boolean
axis orthogonal to the main mode?

## 7. How to use this brief

- **Fast pass:** paste sections 1–6 into a single Claude/GPT session with one
  or two targeted questions you care about.
- **ARC cycle:** paste sections 1–6 into the Problem box, paste the appendix
  code into Project Context, pick Full mode. After the audit, use the Execute
  pipeline on whichever recommendation wins.
- **Pick two:** if the reviewer wants to scope to the two highest-leverage
  items, 6.1 (consolidation) and 6.2 (matching semantics) are the ones that
  will pay the most architectural rent over the next 6 months of adding
  plugins.

---

## Appendix A — `samsara/command_parser.py`

```python
"""
Wake word command parser.

Turns raw text (after wake word extraction) into structured intent dicts.
Pure functions, no side effects, no dependencies beyond stdlib + re.
"""

import re

DEFAULT_FILLERS = frozenset({'please', 'uh', 'um', 'like'})

DICTATION_COMMANDS = {
    "long dictate": "long_dictation",
    "long dictation": "long_dictation",
    "short dictate": "quick_dictation",
    "short dictation": "quick_dictation",
    "quick dictate": "quick_dictation",
    "dictate": "long_dictation",
    "dictation": "long_dictation",
    "type": "quick_dictation",
}

_SEP_PATTERN = r'[\s:,\-]+'


def normalize_command_text(text):
    """Lowercase, strip, collapse whitespace, strip leading non-word chars."""
    text = text.lower().strip()
    text = re.sub(r'^[^\w]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def strip_wake_echoes(text, wake_phrase):
    """Remove all occurrences of wake_phrase from text (word-boundary aware).

    Returns (cleaned_text, count) where count is the number of *extra* echoes
    removed beyond the canonical leading wake word (i.e. total matches - 1,
    floored at 0).
    """
    if not wake_phrase or not text:
        return text, 0
    pattern = r'\b' + re.escape(wake_phrase) + r'\b'
    cleaned, total = re.subn(pattern, '', text, flags=re.IGNORECASE)
    return cleaned, max(0, total - 1)


def strip_fillers(text, fillers=None):
    """Remove leading/trailing filler words from text."""
    if fillers is None:
        fillers = DEFAULT_FILLERS
    words = text.split()
    while words and words[0].lower() in fillers:
        words.pop(0)
    while words and words[-1].lower() in fillers:
        words.pop()
    return ' '.join(words)


def parse_wake_command(raw_text):
    """Parse a wake word command into a structured intent dict."""
    raw = raw_text
    normalized = normalize_command_text(raw_text)
    stripped = strip_fillers(normalized)

    word_content = re.sub(r'[^\w\s]', '', stripped).strip()
    if len(word_content) < 2:
        return {"type": "unknown", "name": None, "content": None, "raw": raw}

    for phrase, mode_name in DICTATION_COMMANDS.items():
        if stripped == phrase:
            return {"type": "dictation", "name": mode_name, "content": None, "raw": raw}
        pattern = rf'^{re.escape(phrase)}{_SEP_PATTERN}(.+)$'
        m = re.match(pattern, stripped)
        if m:
            content = strip_fillers(m.group(1).strip())
            return {"type": "dictation", "name": mode_name,
                    "content": content if content else None, "raw": raw}
        if stripped.startswith(phrase) and len(stripped) > len(phrase):
            remainder = stripped[len(phrase):]
            if remainder and remainder[0].isalpha():
                content = strip_fillers(remainder.strip())
                return {"type": "dictation", "name": mode_name,
                        "content": content if content else None, "raw": raw}

    return {"type": "command_text", "name": None, "content": normalized, "raw": raw}
```

## Appendix B — `samsara/plugin_commands.py`

```python
"""
Plugin-based voice command system for Samsara.
Commands register themselves via @command decorator. The registry is scanned
by find_command() using the same matching rules as the JSON command system
(exact, start, end, word-bounded middle).
Plugins live in plugins/commands/*.py. Drop a file in, it's loaded at startup.
"""

import importlib.util
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Global registry: phrase -> {func, aliases, source}
# Aliases are also keys in the registry for O(1) lookup.
_REGISTRY = {}


def command(phrase, aliases=None):
    """Decorator: register a function as a voice command."""
    def decorator(func):
        entry = {
            'func': func,
            'phrase': phrase.lower().strip(),
            'aliases': [a.lower().strip() for a in (aliases or [])],
            'source': getattr(func, '__module__', 'unknown'),
        }
        _REGISTRY[entry['phrase']] = entry
        for alias in entry['aliases']:
            _REGISTRY[alias] = entry
        return func
    return decorator


def find_command(text):
    """Return (entry, remainder) if text matches a registered command, else (None, '')."""
    if not text:
        return None, ''
    text_lower = text.lower().strip()

    if text_lower in _REGISTRY:
        return _REGISTRY[text_lower], ''

    # Longest-phrase-first prevents "open" matching before "open browser"
    for phrase in sorted(_REGISTRY, key=len, reverse=True):
        entry = _REGISTRY[phrase]
        if text_lower.startswith(phrase + ' '):
            return entry, text[len(phrase):].strip()
        if text_lower.startswith(phrase):
            return entry, text[len(phrase):].strip()
        if text_lower.endswith(' ' + phrase):
            return entry, text[:-len(phrase)].strip()
        if f' {phrase} ' in f' {text_lower} ':
            idx = text_lower.find(phrase)
            remainder = (text[:idx] + ' ' + text[idx + len(phrase):]).strip()
            return entry, remainder

    return None, ''


def execute_command(text, app=None):
    """Find and execute a command matching text. Returns (phrase, success) or (None, False)."""
    entry, remainder = find_command(text)
    if entry is None:
        return None, False
    try:
        result = entry['func'](app, remainder)
        return entry['phrase'], bool(result)
    except Exception as e:
        logger.exception(f"Plugin command '{entry['phrase']}' failed: {e}")
        return entry['phrase'], False


def load_plugins(plugins_dir):
    """Auto-load every .py file in plugins_dir. Imports trigger @command decorators."""
    plugins_path = Path(plugins_dir)
    if not plugins_path.exists():
        return 0
    loaded = 0
    for py_file in plugins_path.glob("*.py"):
        if py_file.name.startswith('_'):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"samsara_plugin_{py_file.stem}", py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            loaded += 1
        except Exception as e:
            logger.exception(f"Failed to load plugin {py_file.name}: {e}")
    return loaded
```

## Appendix C — `samsara/wake_word_matcher.py`

```python
"""
Shared token-aware wake-word matching.
Match types, ordered by preference:
    "exact"     : text.strip().lower() == phrase.lower()
    "prefix"    : phrase at start of text, followed by word boundary
    "suffix"    : phrase at end of text,   preceded by word boundary
    "token"     : phrase in middle, surrounded by word boundaries
    "substring" : phrase appears but is NOT token-bounded (REPORTED, but matched=False)
    "none"      : phrase not present at all
"""

import re

_WORD_CHAR = re.compile(r"\w")
# Hyphens and apostrophes count as word-internal: "samsara-like", "samsara's".
_INTERNAL_PUNCT = frozenset("-'")


def _is_boundary(ch):
    if ch is None:
        return True
    if ch in _INTERNAL_PUNCT:
        return False
    return not _WORD_CHAR.match(ch)


def match_wake_phrase(text, phrase):
    """Returns (matched, match_type, match_index)."""
    if not text or not phrase:
        return (False, "none", -1)
    text_lower = text.lower()
    phrase_lower = phrase.lower().strip()
    if not phrase_lower:
        return (False, "none", -1)
    stripped = text_lower.strip()

    if stripped == phrase_lower:
        return (True, "exact", 0)

    idx = text_lower.find(phrase_lower)
    if idx == -1:
        return (False, "none", -1)

    before = text_lower[idx - 1] if idx > 0 else None
    end = idx + len(phrase_lower)
    after = text_lower[end] if end < len(text_lower) else None

    left_ok = _is_boundary(before)
    right_ok = _is_boundary(after)

    if not (left_ok and right_ok):
        return (False, "substring", idx)

    at_start = (before is None)
    at_end = (after is None)
    if at_start and at_end:
        return (True, "exact", idx)
    if at_start:
        return (True, "prefix", idx)
    if at_end:
        return (True, "suffix", idx)
    return (True, "token", idx)
```

## Appendix D — `dictation.py` in-app `CommandExecutor` (the runtime one)

Located at `dictation.py:236-496`. This class is instantiated once in
`DictationApp.__init__` and owns all hotkey/launch/macro execution.

```python
class CommandExecutor:
    """Executes voice commands - hotkeys, launches, key holds, etc."""

    def __init__(self, commands_path):
        self.commands_path = commands_path
        self.commands = {}
        self.held_keys = {}
        self.keyboard_controller = KeyboardController()
        self.mouse_controller = MouseController()
        self.load_commands()

        # Plugin commands: auto-load *.py files from plugins/commands/.
        plugins_dir = Path(__file__).parent / "plugins" / "commands"
        try:
            _plugin_commands.load_plugins(plugins_dir)
        except Exception as e:
            print(f"[PLUGINS] Failed to load plugins: {e}")
        unique = len({id(entry) for entry in _plugin_commands._REGISTRY.values()})
        print(f"[PLUGINS] Loaded {unique} plugin commands")

        self.key_map = { 'ctrl': Key.ctrl, 'shift': Key.shift, ... }

    def load_commands(self):
        try:
            with open(self.commands_path, 'r') as f:
                data = json.load(f)
                self.commands = data.get('commands', {})
        except Exception as e:
            self.commands = {}

    def execute_command(self, command_name):
        """Execute a voice command by name."""
        if command_name not in self.commands:
            return False
        cmd = self.commands[command_name]
        cmd_type = cmd.get('type')
        try:
            if cmd_type == 'hotkey':
                keys = [self.get_key(k) for k in cmd['keys']]
                for key in keys[:-1]: self.keyboard_controller.press(key)
                self.keyboard_controller.press(keys[-1])
                self.keyboard_controller.release(keys[-1])
                for key in reversed(keys[:-1]): self.keyboard_controller.release(key)
                return True
            elif cmd_type == 'press':          ...
            elif cmd_type == 'key_down':       ...
            elif cmd_type == 'key_up':         ...
            elif cmd_type == 'release_all':    ...
            elif cmd_type == 'mouse':          ...
            elif cmd_type == 'launch':         ...
            elif cmd_type == 'text':           ...
            else:
                return False
        except Exception as e:
            return False

    def find_command(self, text):
        """Check if transcribed text matches a command."""
        text_lower = text.lower().strip()
        if text_lower in self.commands:
            return text_lower
        for cmd_name in self.commands:
            if text_lower.startswith(cmd_name + " ") or text_lower.startswith(cmd_name):
                return cmd_name
            if text_lower.endswith(" " + cmd_name) or text_lower.endswith(cmd_name):
                return cmd_name
            if f" {cmd_name} " in f" {text_lower} ":
                return cmd_name
        # Plugin fallback (lower priority).
        plugin_entry, _remainder = _plugin_commands.find_command(text)
        if plugin_entry is not None:
            return plugin_entry['phrase']
        return None

    def process_text(self, text, app_instance=None, force_commands=False):
        if not text: return None, False
        text_lower = text.lower().strip()
        # ALWAYS-ON: command-mode toggle phrases, reminder phrases.
        # ... [omitted for brevity] ...

        # If command mode disabled, return text for dictation.
        if not force_commands:
            if app_instance and not app_instance.command_mode_enabled:
                return text, False

        # Find + execute.
        command = self.find_command(text)
        if command:
            if command in self.commands:
                cmd = self.commands[command]
                if cmd.get('type') == 'method':
                    method_name = cmd.get('method')
                    if app_instance and method_name and hasattr(app_instance, method_name):
                        try:
                            getattr(app_instance, method_name)()
                            return command, True
                        except Exception as e:
                            return command, False
                    return command, False
                success = self.execute_command(command)
            else:
                print(f"[PLUGIN] Executing: {command}")
                _phrase, success = _plugin_commands.execute_command(text, app=app_instance)
            return command, success

        return text, False
```

## Appendix E — `samsara/commands.py` modular `CommandExecutor`

Full file at path, ~480 lines. Key shape:

```python
class CommandExecutor:
    def __init__(self, commands_path=None, app=None, plugins_dir=None):
        # Same fields as in-app, plus self._app for plugin dispatch.
        self.load_commands()
        # Loads plugins from project-root plugins/commands/ by default.

    def find_command(self, text):
        text_lower = text.lower().strip()
        if text_lower in self.commands:
            return text_lower
        # NOTE: only padded word-boundary match here, no startswith/endswith
        padded = f" {text_lower} "
        for cmd_name in self.commands:
            if f" {cmd_name} " in padded:
                return cmd_name
        plugin_entry, _ = _plugin_commands.find_command(text)
        if plugin_entry is not None:
            return plugin_entry['phrase']
        return None

    def execute_command(self, command_name):
        # Same hotkey/press/.../text dispatch as in-app. No 'method' branch.
        ...

    def process_text(self, text, command_mode_enabled=True, on_mode_change=None):
        # Similar shape to in-app process_text, with 'method' dispatch added
        # for test coverage, but no reminder/cancel-word logic.
        ...
```

The `find_command` divergence in 5.1 is visible here: modular only checks
word-bounded interior, not prefix/suffix startswith. Tests against this
implementation have not caught the collision-on-prefix behavior because the
modular version's matching rule is stricter.

## Appendix F — `dictation.py` wake pipeline

`wake_word_audio_callback` (mic -> buffered speech -> dispatched to worker):

```python
def wake_word_audio_callback(self, indata, frames, time_info, status):
    """Callback for wake word listening"""
    if not self.wake_word_active: return
    audio_chunk = indata.copy()
    if self.echo_canceller.is_active:
        audio_chunk = self.echo_canceller.process(audio_chunk)
    audio_chunk = audio_chunk.flatten()

    if self._hotkey_recording: return
    self._prebuffer.append(audio_chunk.copy())

    rms = np.sqrt(np.mean(audio_chunk**2))
    # Per-state silence thresholds
    if self.app_state == 'long_dictation':
        silence_threshold = 999999.0       # mic stays hot
    elif self.app_state == 'quick_dictation' and self._dictation_silence_timeout:
        silence_threshold = self._dictation_silence_timeout
    else:
        silence_threshold = audio_config.get('wake_detection_silence', 0.8)

    if rms > speech_threshold:
        self.is_speaking = True
        self.silence_start = None
        with self.buffer_lock:
            self.speech_buffer.append(audio_chunk)
            if len(self.speech_buffer) >= 50:  # 5s cap
                buffer_copy = self.speech_buffer.copy()
                self.speech_buffer = []
                self.is_speaking = False
                threading.Thread(target=self.process_wake_word_buffer,
                                 args=(buffer_copy,), daemon=True).start()
    else:
        # silence: accumulate then flush when threshold exceeded
        ...
```

`process_wake_word_buffer` (the monolith — 200 lines):

```python
def process_wake_word_buffer(self, buffer):
    """Process audio - check for wake word, commands, or dictation content"""
    try:
        audio = np.concatenate(buffer)
        audio = resample_audio(audio, self.capture_rate, self.model_rate)

        # Transcribe (blocking, Whisper-bounded)
        transcribe_start = time.time()
        with self.model_lock:
            segments, info = self.model.transcribe(audio, **transcribe_params)
        text = "".join([s.text for s in segments]).strip()
        transcribe_time = time.time() - transcribe_start

        # Corrections
        text = self.voice_training_window.apply_corrections(text)
        text_lower = text.lower()
        if not text: return

        wake_phrase = ww_config.get('phrase', 'samsara').lower()

        # === IN DICTATION STATE: cancel/end/pause/resume/buffer ===
        if self.app_state in ('quick_dictation', 'long_dictation'):
            # Cancel words (always)
            for cw in cancel_words:
                if cw.lower() in text_lower:
                    self._reset_wake_dictation(); return

            # End words (always -- even while paused)
            for ew in end_words:
                if ew.lower() in text_lower:
                    end_index = text_lower.rfind(ew.lower())
                    final_text = text[:end_index].strip()
                    if self.wake_dictation_buffer:
                        final_text = ' '.join(self.wake_dictation_buffer) + ' ' + final_text
                    if final_text.strip():
                        self._output_dictation(final_text.strip())
                    self._reset_wake_dictation(); return

            # Pause/resume (long_dictation only)
            if self.app_state == 'long_dictation':
                if self._dictation_paused:
                    for rw in resume_words:
                        if rw.lower() in text_lower:
                            self._dictation_paused = False
                            # ... indicator, sound, trace ...
                            return
                    # Paused + not a resume word -> discard
                    print(f"[PAUSED] Ignoring: '{text}'"); return

                for pw in pause_words:
                    if pw.lower() in text_lower:
                        # Preserve pre-pause content
                        pause_idx = text_lower.find(pw.lower())
                        cleaned = (text[:pause_idx] + text[pause_idx + len(pw):]).strip()
                        if cleaned:
                            self.wake_dictation_buffer.append(cleaned)
                        self._dictation_paused = True
                        # ... indicator, sound, trace ...
                        return

            # Accumulate
            self.wake_dictation_buffer.append(text)
            if not self._dictation_require_end:
                self._restart_dictation_timer()
            return

        # === ASLEEP / COMMAND_WINDOW: wake detection + command dispatch ===
        corrected_lower = apply_wake_corrections(text_lower)
        matched, match_type, match_index = match_wake_phrase(corrected_lower, wake_phrase)

        if matched:
            self.wake_word_triggered = True
            command_text = corrected_lower[match_index + len(wake_phrase):].strip()
            command_text = normalize_command_text(command_text)
            command_text, echo_count = strip_wake_echoes(command_text, wake_phrase)
            if echo_count:
                command_text = normalize_command_text(command_text)   # re-run

            cleaned_cmd = re.sub(r'[^\w\s]', '', command_text).strip()
            if len(cleaned_cmd) >= 2:
                self._process_wake_command(command_text)
            else:
                self._start_wake_timeout()   # wait for follow-up command

        elif match_type == "substring":
            # Rejected: "samsara-like" / "samsara's" etc.
            pass

        elif self.wake_word_triggered:
            # Follow-up command after bare wake word
            self._process_wake_command(text)
```

## Appendix G — An example plugin (for the handler contract)

`plugins/commands/macros.py` (abridged):

```python
from samsara.plugin_commands import command

@command("going dark", aliases=["end of day", "shut it down", "goodnight"])
def going_dark(app, remainder):
    """Mute, minimize everything, lock screen."""
    _nircmd(["mutesysvolume", "1"])
    time.sleep(0.1)
    pyautogui.hotkey('win', 'd')
    time.sleep(0.3)
    pyautogui.hotkey('win', 'l')
    return True
```

Signature: `handler(app, remainder) -> bool`. Plugins typically access
state via `app.config`, `app.play_sound`, `app.listening_indicator`.

## Appendix H — Relevant constants

From `samsara/constants.py`:

```
MODEL_SAMPLE_RATE = 16000            # Whisper input
DEFAULT_SPEECH_THRESHOLD = 0.03      # RMS for speech
DEFAULT_MIN_SPEECH_DURATION = 0.3    # seconds
DEFAULT_SILENCE_TIMEOUT = 2.0        # end-of-recording silence
WAKE_DETECTION_SILENCE = 0.8         # wake-listening silence window
WAKE_COMMAND_TIMEOUT = 5.0           # post-wake command wait
CLIPBOARD_PASTE_DELAY = 0.05
CLIPBOARD_RESTORE_DELAY = 0.15
```


GPT's review:

You’ve already identified most of the symptoms correctly. I’m going to focus on root causes, invariants, and the 2–3 changes that actually buy you long-term leverage, not incremental tweaks.

Executive take

You don’t have “a few messy spots.”
You have three structural inconsistencies that are now compounding:

No single source of truth for command resolution
No explicit intermediate representation (tokens / intent)
No unified execution model

Everything else in your doc is downstream of those.

Fix those, and:

collisions disappear (or become deterministic)
pipeline becomes composable
plugins stop being “special”
latency work becomes measurable
1. CommandExecutor duplication is not the problem
Your question (6.1):

Are they supposed to be one thing?

Yes. But more importantly:

They currently aren’t one thing conceptually.

You don’t just have duplication—you have two different matching models:

in-app: prefix/suffix/substring (greedy, order-dependent)
modular: word-boundary only (stricter, more predictable)

That’s why tests don’t catch runtime bugs.

Root issue:

You’ve coupled:

matching
dispatch
app state

into a single class.

Fix (high leverage, low risk):

Split into 3 layers:

CommandRegistry   ← owns ALL commands (built-in + plugin)
CommandMatcher    ← pure function: text → (command_id, remainder)
CommandExecutor   ← executes resolved command

Then:

Both “executors” go away
Tests target CommandMatcher directly (this is what’s actually broken)
Runtime uses same matcher → no drift possible

👉 Don’t try to “merge the two executors”
👉 Extract the matcher out of both and make it canonical

2. Your matching problem is not about order—it’s about representation
Your question (6.2):

You’re choosing between:

first match
longest match
trie
etc.

That’s the wrong level.

The real problem:

You’re matching against raw strings, not structured input.

You already have partial token logic:

\b boundaries
wake matcher boundary checks
echo stripping
normalization passes (twice!)

That’s an implicit tokenizer trying to exist.

Correct model

Introduce a token stream once, early:

text → normalize → tokenize → [tokens]

Then match against:

["find", "tab", "chrome"]

instead of:

"find tab chrome"
Matching rule becomes trivial:

Longest token-prefix match across ALL commands (built-in + plugin unified)

Example:

Input tokens	Commands	Result
find tab chrome	find / find tab	find tab
search for cats	search / search for	search for

No substring hacks. No ordering bugs.

Implementation options

You don’t need a trie yet.

Start with:

commands = [
    ["find"],
    ["find", "tab"],
    ["search"],
    ["search", "for"]
]

# sort by len desc once at startup

Then match:

for cmd_tokens in commands_sorted:
    if input_tokens[:len(cmd_tokens)] == cmd_tokens:
        return cmd_tokens

That alone fixes:

your 3 collisions
future plugin overlap issues
inconsistent semantics
Key invariant (this matters for your ARC process)

Matching must be:

deterministic
order-independent
identical across runtime + tests

Right now it is none of those.

3. process_wake_word_buffer is doing 3 pipelines at once

You described it as “monolithic,” but that’s not the real issue.

It’s actually mixing 3 different concerns:
Audio → text (I/O + model)
Text → intent (parsing + state machine)
Intent → action (dispatch)

That’s why it’s hard to reason about.

Correct shape

You don’t need over-engineered “stages.” Just enforce boundaries:

[Audio Thread]
    ↓
transcribe(audio) → text

[Processing Thread]
    ↓
intent = parse(text, state)

[Execution Layer]
    ↓
execute(intent)
Minimal refactor (don’t overdo it)

Extract just this:

def parse_text(text, state) -> Intent

Where Intent is something like:

{
  "type": "command" | "dictation" | "control",
  "command": "find tab",
  "args": "...",
  "raw": "...",
}

Now:

wake logic lives here
pause/resume logic lives here
command extraction lives here

👉 process_wake_word_buffer becomes orchestration, not logic

4. Dispatch system: you’re already past the tipping point
Your question (6.4):

Is a CommandHandler protocol worth it?

Yes—not for elegance, but because you already have:

JSON commands
plugin callables
method dispatch
macros
future expansion (guaranteed)

Right now adding a command type = editing a giant if/elif in multiple places.

That will break repeatedly.

Minimal viable unification

Don’t overdesign. Just normalize to:

class CommandHandler:
    def execute(self, context) -> bool:
        ...

Wrap everything:

JSON hotkey → HotkeyHandler
plugin → PluginHandler
method → MethodHandler

Then:

handler.execute(context)

No branching.

Bonus benefit

This also solves your plugin contract question (6.6):

Instead of:

handler(app, remainder)

You get:

context = {
    "app": app,
    "args": remainder,
    "raw": text,
    "source": "voice"
}

Much easier to evolve without breaking plugins.

5. Latency: your intuition is slightly off
Your question (6.5):

Where’s the first 100ms?

Not where you think.

Clipboard (250ms) is real—but:

It only applies to dictation output, not commands.

Your biggest perceived latency risk is actually:

👉 synchronous plugin execution

You even called it out:

plugin handler runs in same thread

So if a plugin:

sleeps
launches something slow
hits disk/network

The system “feels stuck”

Fix (very high leverage, very low effort)

Split execution:

fast-path:
    hotkey / text / small actions → sync

slow-path:
    plugins / launch / macros → async worker queue

User perception improves immediately.

Clipboard note

You’re probably near the floor already.

If you want gains:

SendInput (lower level than pyautogui)
or app-specific insertion (hard)

But that’s diminishing returns vs fixing dispatch blocking.

6. Plugin system: you’re missing one critical piece
Your question (6.7):

Worth adding startup collision detection?

Yes—but not just warnings.

You need a:

Compiled command registry

At startup:

all_commands =
    builtins
    + plugins
    + aliases

Then:

normalize
tokenize
sort (longest first)
detect collisions

And freeze it.

Why this matters

Right now:

matching is dynamic
registry is fragmented
behavior depends on load order + structure

That’s fragile.

7. Pause/resume: don’t overthink it
Your question (6.8)

Keep the boolean.

This is genuinely orthogonal state.

Turning it into a full state adds complexity without payoff unless:

you add more pause-like substates

Right now:
✔ flag is correct

The 3 changes I would actually do

If you do nothing else, do these:

1. Extract and unify CommandMatcher (highest priority)
token-based
longest-match
shared by runtime + tests
unified registry

This fixes:

collisions
executor drift
test irrelevance
2. Introduce Intent layer (parse_text)
separates pipeline cleanly
makes wake logic understandable
unlocks future features cleanly
3. Wrap dispatch into handlers + async slow path
removes branching explosion
prevents blocking behavior
stabilizes plugin system
What I would NOT do yet
trie optimization (premature)
full pipeline framework
over-abstracted state machine
replacing clipboard system immediately
Final blunt assessment

You’re at the transition point from:

“working system with growing complexity”

to:

“system that needs explicit architecture to keep scaling”

You’ve already identified the right pain points. The only risk now is:

fixing symptoms locally instead of introducing the missing abstractions.

If you want, I can go one level deeper and:

sketch the exact CommandMatcher implementation (drop-in)
or redesign your command grammar so plugins + builtins become identical objects

That’s where your next 6 months of stability will come from

gemini audit:

This is a fantastic architectural document, and GPT gave you genuinely senior-level engineering advice. It correctly diagnosed that your app has outgrown its script-like origins and needs formal boundaries.

The core of GPT's advice—shifting from raw string manipulation to tokenized intents and separating the matcher from the executor—is the exact right path forward.

However, while GPT’s structural diagnosis is flawless, its advice on threading (Point 5) contains a dangerous trap for a voice-control application.

Here is a candid audit of the review, highlighting what to build immediately and what to approach with caution.
🟢 Where GPT is 100% Right (The Blueprint)

1. The CommandMatcher Separation
Your current setup (two drifting CommandExecutors) is a classic symptom of mixing state, matching, and execution. Extracting a pure, stateless CommandMatcher that simply takes (text) and returns (command_id, remainder) is the single best thing you can do for your test suite and runtime stability.

2. Tokenized Longest-Match
This is the silver bullet for your collision issues. Stop using .startswith() and .endswith() on raw strings. By tokenizing the input (e.g., ["search", "for", "cat", "toys"]) and matching against tokenized command lists, you completely eliminate substring errors. Sorting the registry by token length descending guarantees that "find tab" will always evaluate before "find".

3. The CommandHandler Protocol
Replacing your giant if/elif cmd_type == ... block with a unified handler interface (execute(context)) is exactly how enterprise software handles extensibility. It treats built-in hotkeys and custom Python plugins as first-class citizens using the exact same pipeline.
🔴 The Trap: The "Async Slow-Path" (Point 5)

GPT suggested fixing latency by putting slow plugins on an async worker queue while keeping hotkeys on a fast-path sync thread. Do not do this. Voice commands are inherently sequential. If you say:
"Jarvis, find the Chrome tab" (Slow Plugin)
"Jarvis, press Enter" (Fast Hotkey)

If the plugin is shoved onto an async background thread, the fast-path hotkey will execute first. Samsara will fire the "Enter" key into whatever application you currently have open, and then the background thread will focus the Chrome tab.

The Fix: A Strict FIFO Action Queue
You do not want async fire-and-forget. You want a strict First-In-First-Out (FIFO) queue.

    The transcription thread parses the text, creates the Intent context, and pushes it to a single ExecutionQueue.

    A single, dedicated Execution Thread pulls from that queue one by one.
    If a plugin takes 2 seconds to load a webpage, the queue waits. This guarantees that your commands fire in the exact order you spoke them, preserving causality.

🟡 The Execution Plan (How to build this safely)

Refactoring a core engine while it's in active use is like changing the tires on a moving car. Do not try to rewrite process_wake_word_buffer and the CommandExecutor at the same time.

Do this in three distinct phases to ensure you don't break your daily driver:

Phase 1: The Registry & Matcher (Safe)

    Create a new file: command_registry.py.

    Build the startup logic that pulls from commands.json and the plugins folder, tokenizes all the trigger phrases, and sorts them by length.

    Build the pure CommandMatcher.match(tokens) function.

    Test: Point your existing Pytest suite at this new matcher to ensure it handles the "find" vs "find tab" collision correctly.

Phase 2: The Handlers (Safe)

    Create handlers.py.

    Define the base class/protocol: class BaseHandler: def execute(self, context: dict) -> bool:

    Create implementations for HotkeyHandler, LaunchHandler, and PluginHandler.

    Test: Manually instantiate these in a test file and pass them dummy contexts.

Phase 3: The Swap (High Risk)

    Go into your monolithic process_wake_word_buffer.

    Rip out the string-slicing logic and replace it with your new CommandMatcher.

    Rip out the execute_command if/elif chain and route the intent to your new BaseHandler.execute().

Final Verdict

You have officially hit the architectural maturity phase of this project. Implementing a token-based registry and a unified handler protocol will make adding your next 100 commands trivial. Stick to a strict sequential execution model rather than splitting into async fast/slow paths, and this refactor will make Samsara bulletproof.