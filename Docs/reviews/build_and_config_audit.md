# Build and Config Audit

Date: 2026-09-10
Branch: `feature/v0.22` (read-only audit; no checkout, no commits, no source edits)
Interpreter used for analysis: `F:\envs\sami\python.exe`
Nothing was built. PyInstaller was never invoked. `C:\Users\Morne\.samsara\config.json` was read only.

## Evidence basis

Three independent artifacts were used, so claims are not spec-reading alone:

| Artifact | What it proves | Note |
|---|---|---|
| `scripts/samsara.spec` | declared intent | static read |
| `dist/Samsara/` (built 2026-07-18) | what a build actually emitted | pre-existing on disk |
| `Samsara-Windows-v0.22.1.zip` (7729 entries, 1408 MB uncompressed) | what actually shipped | pre-existing on disk |

The decisive check is the embedded PYZ archive. Pure-Python packages do **not** appear as
`_internal/<pkg>/` folders; they are compiled into `PYZ.pyz` inside `Samsara.exe`. Absence from
the `_internal/` listing therefore proves nothing on its own. The PYZ was extracted via
`PyInstaller.archive.readers.CArchiveReader` / `ZlibArchiveReader` and enumerated: **4364 modules**.

---

# PART A - Frozen build audit

## A1. Does the CPU build bundle torch, torchaudio, or CUDA DLLs?

**No torch. No torchaudio. One CUDA-adjacent DLL leaks, and it is not gated by `INCLUDE_CUDA`.**

PYZ enumeration of the shipped `Samsara.exe`:

```
PYZ modules: 4364
   torch              0     []
   torchaudio         0     []
   torchvision        0     []
   triton             0     []
   transformers       0     []
```

Release ZIP scan: `torch entries: 0`. Filesystem scan of `dist/Samsara/_internal`: no `torch*`
directory, no torch-sourced DLL.

### Trace: why torch stays out (four independent reasons, not one gate)

The task asks whether the `INCLUDE_CUDA=1` gate is the only thing keeping torch out. It is not,
and in fact the gate has nothing to do with the Python framework at all.

1. **No source import exists.** `grep` for `^\s*import torch` / `^\s*from torch` across
   `samsara/`, `dictation.py`, `plugins/` returns nothing. The only occurrences of the string
   `torch` in shipped code are logger-suppression names:
   `dictation.py:551: "torchaudio._extension", "torch", "urllib3",`
   Strings in a logging list are not imports, so PyInstaller's analysis never sees a torch edge.

2. **No collected package pulls it.** The spec's `collect_all()` calls are `openwakeword`,
   `PySide6`, `shiboken6`, `mediapipe`. Of these only `openwakeword` mentions torch, and only
   under an extra that is not installed:
   ```
   openwakeword -> [... "torch <3,>=1.13.1 ; extra == 'full'",
                        "torchaudio <1,>=0.13.1 ; extra == 'full'", ...]
   ```
   Base install requires only `onnxruntime, tqdm, scipy, scikit-learn, requests`. `collect_all`
   walks the installed package, not unselected extras, so the `full` extra is inert.

3. **The `excludes` list names them explicitly** (`scripts/samsara.spec`):
   ```python
   'torch',
   'torch._C',
   'torch.cuda',
   'torch.nn',
   'torch.utils',
   'torchgen',
   'torchaudio',
   ...
   'torchvision',  # Not needed for audio
   ```

4. **No hook contributes torch.** `hookspath=[]` and `runtime_hooks=[]`; the only runtime hooks in
   the built CArchive are PyInstaller's own (`pyi_rth_inspect`, `pyi_rth_pkgutil`,
   `pyi_rth_pywintypes`, `pyi_rth_pythoncom`, `pyi_rth__tkinter`, `pyi_rth_pyside6`, ...).

This is a belt-and-braces situation: reasons 1 and 2 alone are sufficient. The `excludes` entries
are redundant defence, not the load-bearing mechanism. Torch **is** installed in the dev env
(`torch-2.5.1+cu121`, `torchaudio`, `torchvision`), so the redundancy is not worthless - it
protects against a future accidental import - but it is not what is holding the line today.

### What leaks anyway

`cudnn64_9.dll` (266,288 bytes) ships in **both** `dist/` and the release ZIP:

```
_internal/ctranslate2/cudnn64_9.dll   266288
```

It is bundled by the **unconditional** DLL loop, which runs regardless of `INCLUDE_CUDA`:

```python
# ctranslate2 DLLs
for dll in ['ctranslate2.dll', 'cudnn64_9.dll', 'libiomp5md.dll']:
    dll_path = os.path.join(ctranslate2_path, dll)
    if os.path.exists(dll_path):
        binaries.append((dll_path, 'ctranslate2'))
```

Two things follow:

- Its source is `site-packages/ctranslate2/`, **not** `site-packages/torch/lib/`. So it is not
  "torch leaking"; it is ctranslate2's own CUDA-enabled wheel leaking one file.
- In cuDNN 9 this file is a thin dispatch stub that `LoadLibrary`s the real backends
  (`cudnn_graph64_9.dll`, `cudnn_cnn64_9.dll`, `cudnn_engines_*`). None of those are in the CPU
  build. It is 266 KB of non-functional stub in every CPU ZIP.

### What `INCLUDE_CUDA=1` would do

The gate harvests from `site-packages/torch/lib`, and every DLL it names is present there:

```
PRESENT torch/lib/cudnn_engines_precompiled64_9.dll  588910632
PRESENT torch/lib/cublasLt64_12.dll                  538818048
PRESENT torch/lib/cudnn_adv64_9.dll                  241576488
PRESENT torch/lib/cudnn_ops64_9.dll                  107721256
PRESENT torch/lib/cublas64_12.dll                     98058240
PRESENT torch/lib/cudnn_heuristic64_9.dll             85741608
PRESENT torch/lib/cudnn_engines_runtime_compiled64_9.dll  8235560
PRESENT torch/lib/cudnn_cnn64_9.dll                    4019752
PRESENT torch/lib/cudnn_graph64_9.dll                  2160680
PRESENT torch/lib/cudart64_12.dll                       527872
```

Total ~1.67 GB of DLLs, on top of the current 1408 MB. Note this harvests **DLLs only** - it
appends to `binaries`, never to `hiddenimports` - so even `INCLUDE_CUDA=1` would not put the torch
*Python framework* in the build. The spec comment is accurate on this point:

```
# INCLUDE_CUDA above may still harvest selected CUDA DLLs from an
# installed torch package without bundling the Python framework.
```

Side observation: these same DLLs also exist in `site-packages/ctranslate2/` (e.g.
`cudnn_engines_precompiled64_9.dll`, 588 MB). The gate reads them from `torch/lib`, which makes a
CUDA build silently dependent on torch being installed even though torch is otherwise irrelevant
to the product.

## A2. Is torch importable in the frozen build, and did torch_guard ever matter there?

**Not importable in the frozen build. The guard only ever constrained the dev environment.**

Evidence: 0 torch modules in the 4364-module PYZ, no `torch` directory in `_internal`, 0 torch
entries in the release ZIP. There is no `torch` for a guard to block once frozen. A
`MetaPathFinder` that blocks an import of something that was never packaged is a no-op; the
frozen app would have raised `ModuleNotFoundError` at that import with or without it.

So the guard's entire practical effect is on `F:\envs\sami`, where torch 2.5.1+cu121 **is**
installed and would otherwise be importable (and where `openwakeword[full]`-style code paths or a
stray dependency could pull a 2.4 GB framework into app startup). That matches the module's own
stated purpose:

```
"""Keep optional CTranslate2 conversion dependencies out of app startup.

Call install() before third-party imports. Set SAMSARA_ALLOW_TORCH=1 before
startup for development tools that need PyTorch in the same interpreter.
"""
```

### Correction to the task premise

The task states the guard is "currently DISABLED (commented import in dictation.py ~383)". That
is not the current state of the tree. The import is live and uncommented:

```
384:import samsara.torch_guard; samsara.torch_guard.install()
```

`git show HEAD:dictation.py | grep torch_guard` returns nothing, and `samsara/torch_guard.py` is
untracked (`?? samsara/torch_guard.py`). So the guard is a **new, currently-active working-tree
change**, not a disabled one. Whatever disabling happened previously has been reverted, or the
re-enable was not recorded. This should be reconciled before the premise is relied on, because the
guard being active is the riskier state for the scipy/OpenWakeWord breakage referenced in the task:
`openwakeword` declares `scipy` and `scikit-learn` as **base** requirements, and both are in the
build (`scipy` 55.3 MB, `sklearn` 12.2 MB), so they are live import paths at runtime today.

The guard is also frozen-build-irrelevant either way, so re-enabling it cannot fix or break a
shipped ZIP - only a dev run.

## A3. Third-party packages the spec collects that nothing under `samsara/` or `dictation.py` imports

Method: AST import extraction over `samsara/`, `dictation.py`, `plugins/` (nested/lazy imports
included via `ast.walk`), intersected against every `hiddenimports` / `collect_all` / `datas`
entry in the spec. Sizes are uncompressed bytes measured inside `Samsara-Windows-v0.22.1.zip`.

### Genuinely dead - zero references anywhere in shipped code

| Package | Spec mechanism | Size in ZIP | Evidence |
|---|---|---|---|
| `customtkinter` | `hiddenimports` + whole-folder `datas` (spec item 3) | 1.5 MB data + 43 PYZ modules | zero matches for `customtkinter` in `samsara/`, `dictation.py`, `plugins/` |
| `tkinter`, `tkinter.ttk`, `tkinter.messagebox` | `hiddenimports` | `_tkinter.pyd` 0.1 MB + `tcl8` 0.3 MB + 9 PYZ modules | zero matches; also drags `pyi_rth__tkinter` runtime hook |
| `pystray`, `pystray._win32` | `hiddenimports` | 13 PYZ modules | only docstring mentions in `samsara/ui/tray_qt.py:3,38,95` ("Drop-in replacement for the pystray.Icon usage"). Superseded by Qt tray. |
| `win32clipboard` | `hiddenimports` | `win32/win32clipboard.pyd` | zero matches |
| `win10toast_click` | `hiddenimports` | **0 - absent from build** | not installed in `F:\envs\sami`; zero matches in code |

These are the whole of the true dead weight, and it is small: roughly **2 MB**. The
`customtkinter` entry is the worst of them because spec item 3 copies the entire site-packages
folder as `datas` *in addition to* the PYZ modules.

`win10toast_click` deserves a separate note. It is listed as a `hiddenimport`, is not installed,
and PyInstaller shipped anyway - it emitted a warning rather than failing. That contradicts the
spec's own comment, which is used to justify the `collect_submodules` approach:

```
# named non-suffixed/renamed/removed names (samsara.ui.main_window, a phantom
# samsara.ui.tabs.* subpackage, samsara.tts.edge_engine) that don't exist --
# PyInstaller hard-errors on an unresolvable hiddenimport and never produces
# Samsara.exe.
```

An unresolvable `hiddenimport` does not hard-error. The `collect_submodules` change is still the
right call, but the stated reason is wrong, and relying on "the build will fail loudly" as a
safety net is not sound.

### Collected without a direct import, but transitively required - do not remove

| Package | Size in ZIP | Required by |
|---|---|---|
| `onnxruntime` | 32.6 MB | base requirement of `openwakeword`; also faster-whisper VAD |
| `huggingface_hub` | 138 PYZ modules | `faster-whisper` model download (`huggingface-hub>=0.21`) |
| `urllib3` | - | transitive dep of `requests` |
| `shiboken6` | 4 MB | PySide6 binding runtime |

`huggingface_hub` and `urllib3` appear in code only as logger-name strings
(`dictation.py:552`, `dictation.py:551`, `dictation.py:1699`), which is why a naive import scan
flags them. They are load-bearing.

### Largest unimported payload - transitive, not spec-listed

Not answers to the literal question (the spec does not name them) but they dominate ZIP size and
arrive through the spec's `collect_all` calls:

| Package | Size in ZIP | Arrives via |
|---|---|---|
| `PySide6` | 664.5 MB | `collect_all('PySide6')` - blanket collection of all Qt modules, including WebEngine/3D/Charts that the app never imports |
| `cv2` | 103.4 MB | direct import (gesture lane) - legitimate |
| `mediapipe` | 98.9 MB | `collect_all('mediapipe')` - legitimate |
| `pyarrow` | 80.5 MB | `fsspec`/`datasets` chain reached through `huggingface_hub`. Nothing imports it. |
| `av.libs` | 67.4 MB | hard dep of `faster-whisper` (`av>=11`) |
| `scipy` | 55.3 MB + 20.3 MB libs | base dep of `openwakeword`; also used at `samsara/audio_engine/engine.py:110` |
| `sklearn` | 12.2 MB | base dep of `openwakeword`. Nothing imports it. |

`pyarrow` at 80.5 MB is the single largest removable item and is a strong `excludes` candidate.
`PySide6` at 664.5 MB is 47 percent of the ZIP; the blanket `collect_all` is the cause, and the
spec documents that choice deliberately (Qt plugins load by path, invisible to static analysis),
so narrowing it is a real change with real risk, not a cleanup.

## A4. Modules imported in code but NOT covered by the spec's analysis

Method: all non-stdlib top-level imports across `samsara/`, `dictation.py`, `plugins/`
(32 distinct), minus everything the spec names, cross-checked against the shipped PYZ.

| Module | First site | Import style | In shipped build? |
|---|---|---|---|
| `mss` | `plugins/commands/screen_gif.py:49` | lazy (in-function) | **NO - absent from PYZ and ZIP** |
| `pydub` | `dictation.py:10107` | lazy | yes (7 PYZ modules) |
| `uiautomation` | `plugins/commands/show_numbers.py:330` | lazy | yes (3 PYZ modules) |
| `pythoncom` | `plugins/commands/workflow_capture.py:46` | lazy | yes (+ `pywin32_system32/pythoncom311.dll`) |
| `win32com` | `plugins/commands/workflow_capture.py:47` | lazy | yes (17 PYZ modules) |
| `stremio_control` | `plugins/commands/stremio.py:36` | `sys.path` + lazy | yes, as **data** (`_internal/tools/stremio_control.py`) |
| `scipy` | `samsara/audio_engine/engine.py:110` | lazy | yes |
| `winsdk` | `plugins/commands/media_keys.py:56` | lazy | yes (38.4 MB) |
| `numpy` | `dictation.py:385` | top-level | yes |

### The one real gap: `mss`

`mss` is declared in `requirements.txt` (`mss>=9.0`, "Screen recording (GIF plugin)"), is **not**
in the spec's `hiddenimports`, is **not installed** in `F:\envs\sami`, and is **not in the
shipped build** (0 PYZ modules, 0 ZIP entries). `plugins/commands/screen_gif.py` will raise
`ImportError` on invocation in the frozen app. It survived only because the import is lazy and the
plugin loader is exception-tolerant - the failure is deferred to whenever a user actually asks for
a screen GIF, and never appears at build or smoke time.

### Structural risk: the plugin loader is invisible to PyInstaller

Every module above except `scipy` and `numpy` is reached from `plugins/commands/*.py`, which are
not imported statically. They are read off disk and exec'd:

```
samsara/plugin_commands.py:209:            spec = importlib.util.spec_from_file_location(
samsara/plugin_commands.py:212:            module = importlib.util.module_from_spec(spec)
```

PyInstaller's analysis cannot follow this. The spec acknowledges the pattern for two Samsara
modules (`samsara.audio_switch`, `samsara.browser_bridge`) but never generalises it to the
**third-party** imports those plugins make. `mss` is the case where that gap already bites.
`pydub`, `uiautomation`, `pythoncom`, `win32com` are currently present only because other
collected packages happen to drag them in - none is a declared `hiddenimport`, so any future
dependency change can silently drop them the same way.

Other dynamic-import sites (all resolvable, listed for completeness):

```
samsara/ui/dictionary_panel_qt.py:433:  __import__('PySide6.QtGui', fromlist=['QColor'])
samsara/ui/workflow_capture_qt.py:23:   __import__('logging')
dictation.py:2613:                      __import__('PySide6.QtWidgets', fromlist=['QApplication'])
dictation.py:11903:                     __import__('PySide6.QtWidgets', fromlist=['QApplication'])
```

---

# PART B - Config audit

Method: AST scan of 244 Python files (excluding `tests/`, `dist/`, `build/`), resolving
section-variable aliases so that `cfg = self.config.get('command_mode', {})` followed by
`cfg.get('mode', 'hold')` is recorded as `command_mode.mode`, with per-function scoping.
Result: **270 distinct keys across 780 read sites**, 110 of them nested.

Live config `C:\Users\Morne\.samsara\config.json`: 88 top-level keys, **267 keys when flattened**.

Two scanner limitations are stated up front rather than hidden, because they bound the tables
below:

- Sections obtained through a helper function (`cloud_llm.py:_get_config(app)` returning
  `config['cloud_llm']`) or through a constructor parameter (`tts/coordinator.py` receiving
  `config`) cannot be attributed statically. Those reads are separated into an "unattributed"
  bucket rather than being falsely reported as missing.
- `<expr>` marks a non-literal default (a variable or named constant). Those are excluded from the
  conflict counts rather than guessed at.

## B1 / B3a. Keys read in code but ABSENT from config.json (silent defaults)

59 keys. These take their hardcoded default on this machine today; the value in the table is what
the code silently uses.

| Key | First read | Default used |
|---|---|---|
| `ace_debug_capture` | `dictation.py:2893` | `False` |
| `active_command_profile` | `samsara/profiles.py:427` | none - `.get()` with no default |
| `active_dictionary_profile` | `samsara/profiles.py:426` | none |
| `anthropic_model` | `samsara/cloud_llm.py:62` | none |
| `ava_command_session.inactivity_timeout_s` | `dictation.py:6710` | `60` |
| `ava_invocations` | `samsara/session_modes.py:263` | non-literal |
| `ava_mode_enabled` | `dictation.py:5950` | `True` |
| `clipboard_delay` | `dictation.py:9613` | non-literal |
| `cloud_llm.model` | `samsara/ui/settings_qt.py:5325` | `''` |
| `command_mode.dictate_utterance_silence_s` | `samsara/audio_engine/wake_consumer.py:650` | `0.65` |
| `command_mode.utterance_silence_s` | `samsara/audio_engine/wake_consumer.py:652` | `1.0` |
| `continuous_speech_threshold` | `samsara/audio_engine/continuous_consumer.py:170` | non-literal |
| `debug_dump_wake_audio` | `dictation.py:8727` | `False` |
| `enable_case_formatters` | `dictation.py:4877` | `False` |
| `initial_prompt` | `samsara/profiles.py:109` | `''` |
| `paste_min_chars` | `dictation.py:9623` | `300` |
| `prebuffer_policy` | `samsara/audio_engine/wake_consumer.py:76` | `'discard'` |
| `ready_cue_dir` | `samsara/ava_command_session.py:565` | non-literal |
| `ready_cue_enabled` | `samsara/ava_command_session.py:562` | non-literal |
| `recording_tail_ms` | `dictation.py:10612` | `250` |
| `recording_tail_max_ms` | `dictation.py:10623` | `1200` |
| `recording_tail_silence_ms` | `dictation.py:10622` | `300` |
| `recording_tail_speech_threshold` | `dictation.py:10624` | `0.008` |
| `shortlist_size` | `samsara/ava_command_session.py:238` | non-literal |
| `tutorial_complete` | `dictation.py:2653` | `False` |
| `typed_injection_processes` | `dictation.py:9574` | none |
| `updates.last_check_epoch` | `samsara/ui/update_qt.py:334` | `0.0` |
| `wake_word.session` | `samsara/audio_engine/wake_consumer.py:75` | `{}` - guarded, see note |
| `wake_word_config.audio.adaptive_gate` | `dictation.py:8664` | `True` |
| `wake_word_config.end_word` | `samsara/ui/wake_word_debug_qt.py:277` | `{}` |
| `wake_word_config.end_word.enabled` | `samsara/ui/wake_word_debug_qt.py:278` | none |
| `wake_word_config.end_word.phrase` | `samsara/ui/wake_word_debug_qt.py:278` | `'over'` |
| `wake_word_config.long_chunk_silence` | `samsara/audio_engine/wake_consumer.py:654` | `1.0` |
| `wake_word_config.long_failsafe_duration` | `dictation.py:9248` | `60.0` |
| `wake_word_config.long_max_duration` | `dictation.py:9247` | `15.0` |
| `wake_word_config.modes` | `dictation.py:3889` | none |
| `wake_word_config.modes.dictate` | `dictation.py:3890` | none |
| `wake_word_config.send_words` | `dictation.py:3919` | none |
| `wake_word_config.session` | `samsara/audio_engine/wake_consumer.py:72` | `{}` |
| `_capture_rate` | `dictation.py:5117` | subscript - raises `KeyError` if absent |
| `_use_case` | `samsara/ui/first_run_wizard_qt.py:999` | subscript - raises `KeyError` if absent |
| `flashforge_ip` | `plugins/commands/flashforge_printer.py:42` | `''` |
| `flashforge_model_file` | `plugins/commands/flashforge_printer.py:114` | non-literal |
| `flashforge_print_file` | `plugins/commands/flashforge_printer.py:150` | `''` |
| `hyperion_host` | `plugins/commands/demo_commands.py:68` | `'192.168.50.247'` |
| `hyperion_port` | `plugins/commands/demo_commands.py:69` | `19444` |
| `music_library` | `plugins/commands/music.py:362` | `{}` |
| `music_volume` | `plugins/commands/music.py:341` | `30` |
| `orca_path` | `plugins/commands/flashforge_printer.py:116` | non-literal |
| `scroll` | `plugins/commands/scroll.py:105` | `{}` |
| `window_manager` | `plugins/commands/windows.py:147` | `{}` |
| `ollama` | `samsara/ui/ava_guide_qt.py:873` | `{}` |
| `max_tokens` | `samsara/cloud_llm.py:83` | `300` |
| `apply_post_gates` | `tools/hf_bench.py:224` | `False` (tooling) |
| `log_prob_threshold` | `tools/hf_bench.py:186` | `-1.0` (tooling) |
| `no_speech_threshold` | `tools/hf_bench.py:185` | `0.6` (tooling) |
| `rms_threshold` | `tools/hf_bench.py:192` | `0.01` (tooling) |
| `vad_filter` | `tools/hf_bench.py:184` | `True` (tooling) |
| `vad_prob_threshold` | `tools/hf_bench.py:198` | `0.5` (tooling) |

Notes on the sharpest entries:

- **`wake_word_config.end_word*`, `wake_word_config.modes*`, `wake_word_config.send_words`** -
  these are schema drift, not oversights. The live config has the **plural** forms
  (`end_words`, `cancel_words`, `pause_words`, `resume_words`) while `wake_word_debug_qt.py` and
  parts of `dictation.py` read **singular** `end_word` / `cancel_word` / `pause_word` dict-shaped
  keys. Two generations of the wake schema are being read simultaneously. Several of these use
  `.get()` with **no default**, returning `None`, so the failure mode is a downstream
  `AttributeError`/`TypeError` rather than a clean fallback.
- **`_capture_rate`** (`dictation.py:5117`) and **`_use_case`**
  (`samsara/ui/first_run_wizard_qt.py:999`) are subscript reads, not `.get()`. If the key is
  absent these raise `KeyError` outright. Both are absent from the live config.
- **`wake_word.session`** is correctly guarded - `wake_consumer.py:77` checks
  `isinstance(wake_word, dict)` before descending, and the live value is the legacy string
  `'jarvis'`. Not a defect; listed because the key genuinely is not present in dict form.

### Unattributed (32) - leaf exists nested in config, parent unresolvable statically

Listed so they are not mistaken for silent defaults. Each was manually confirmed to resolve to a
key that **is** present.

`allow_cloud_fallback`, `allowed_directories`, `allowed_domains`, `api_key`, `auth_header`,
`backend`, `collect_samples`, `duck_default_duration_ms`, `duck_factor`, `duck_fade_ms`,
`enabled`, `endpoint_url`, `hold_ms`, `interrupt_grace_period_ms`, `max_samples`,
`min_detection_confidence`, `min_tracking_confidence`, `miss_limit`, `model`, `ollama_model`,
`poses`, `provider`, `queue_depth_cap`, `refractory_neutral_frames`, `repair_disfluencies`,
`speaking_vad_threshold_multiplier`, `speaking_wake_threshold_multiplier`,
`thinking_pulse_enabled`, `thinking_pulse_interval_ms`, `tier2_approvals`, `timeout_s`,
`timeout_seconds`

Reached via `cloud_llm.py:_get_config()`, `tts/coordinator.py` constructor param,
`smart_actions_bridge.py`, `vision/gesture_loop.py`, `benchmark_store.py`.

## B2 / B3b. Keys PRESENT in config.json but never read (dead config)

**19 keys.**

| Key | Value in config.json |
|---|---|
| `ai_command_mode` | `<dict>` - entire section |
| `ai_command_mode.backend` | `ollama` |
| `ai_command_mode.enabled` | `True` |
| `ai_command_mode.keep_warm` | `True` |
| `ai_command_mode.key` | `left_alt` |
| `ai_command_mode.model` | `llama3.2:3b` |
| `ai_command_mode.queue_depth_cap` | `3` |
| `ai_command_mode.show_plan_hud` | `True` |
| `ai_command_mode.step_settle_seconds` | `0.4` |
| `ai_command_mode.wake_phrase` | `command mode` |
| `command_mode_enabled` | `True` |
| `gesture.poses.fist` | `stop_cancel` |
| `gesture.poses.open_palm` | `dictation_toggle` |
| `gesture.poses.peace` | `ava_mode` |
| `gesture.poses.shaka` | `window_chooser` |
| `tasks` | `<dict>` - entire section |
| `tasks.sync_to_arcana` | `False` |
| `wake_word_config.feedback.play_sound_on_end` | `True` |
| `wake_word_config.feedback.play_sound_on_wake` | `True` |

Observations:

- **`ai_command_mode` (10 keys) is a fully dead section.** It is a near-duplicate of
  `ava_command_session`, which *is* read (`backend`, `model`, `keep_warm`, `queue_depth_cap`,
  `miss_limit`, `key`, `enabled`). `ai_command_mode.enabled` is `True` while
  `ava_command_session.enabled` is `False` - a user editing the section that looks authoritative
  changes nothing, and the section that is live says disabled. This is the most misleading entry
  in the file.
- **`command_mode_enabled`** (top-level, `True`) is dead; the live key is `command_mode.enabled`.
- **`gesture.poses.*` children** are individually unread only because
  `samsara/vision/gesture_loop.py:98` reads the `poses` dict wholesale with a hardcoded default
  that duplicates all four mappings. Functionally live; listed for completeness.
- **`wake_word_config.feedback.*`** are written by `dictation.py:3322-3323` when scaffolding a
  default config but never read back. Write-only config.

## B3c. Keys read with DIFFERENT defaults in different places

**12 keys.** `<expr>` (non-literal) defaults excluded; `tools/` sites excluded from the count.

**FIXED (see this task's report below):** all 12 keys now route through
`samsara/config_defaults.py`'s `DEFAULTS` table, and `tools/config_defaults_check.py`
(wired into `tests/test_config_defaults.py`) fails the gate if a literal default
for any of them (or any future key) ever diverges again.

| Key | Default A | Default B | Further |
|---|---|---|---|
| `wake_word_config.phrase` [FIXED -> `'jarvis'`] | `'jarvis'` - `dictation.py:8094` | `'samsara'` - `dictation.py:8871`, `samsara/ui/wake_word_debug_qt.py:276`, `:1007` | - |
| `wake_word_config.wake_abort_phrase` [FIXED -> `['cancel', 'cancel dictation', 'abort']`] | `['cancel', 'cancel dictation', 'abort']` - `dictation.py:6234` | `['cancel']` - `dictation.py:8878` | - |
| `wake_word_enabled` [FIXED -> `False`] | `False` - `dictation.py:5237`, `:5550`, `:11370`, `:11678`, `samsara/support_feedback.py:69`, `samsara/ui/main_window_qt.py:339`, `quick_reference_qt.py:192`, `settings_qt.py:2147`, `tray_qt.py:344`, `tutorial_qt.py:375` | `True` - `samsara/ui/first_run_wizard_qt.py:1020` | - |
| `command_mode.inactivity_timeout_s` [FIXED -> `300`] | `300` - `dictation.py:6447`, `:7068`, `:7091`, `:10876` | `30` - `samsara/ui/settings_qt.py:2454` | - |
| `model_size` [FIXED -> `'base'`] | `'base'` - `dictation.py:2834`, `first_run_wizard_qt.py:1037`, `:1038`, `settings_qt.py:1329`, `voice_training_qt.py:1005` | `''` - `dictation.py:9894`, `:10724`, `:10832`, `:11002`, `:11031`, `:11094`, `samsara/streaming.py:556` | `'default'` - `support_feedback.py:62`; `'?'` - `diagnostics_qt.py:374`, `tray_qt.py:465` |
| `compute_type` [FIXED -> `'float16'`] | `''` - `dictation.py:9896`, `:10726`, `:10834`, `:11004`, `:11096`, `samsara/streaming.py:558` | `'float16'` - `samsara/ui/settings_qt.py:4695` | `'default'` - `support_feedback.py:65`; `'?'` - `diagnostics_qt.py:376` |
| `device` [FIXED -> `'cpu'`] | `'auto'` - `samsara/support_feedback.py:64` | `'cpu'` - `samsara/ui/settings_qt.py:4677` | `'?'` - `diagnostics_qt.py:375` |
| `language` [FIXED -> `'en'`] | `'en'` - `dictation.py:2840`, `:9912`, `:10729`, `:11016`, `:11103`, `samsara/streaming.py:566`, `settings_qt.py:1354`, `voice_training_qt.py:1031`, `:1112` | `'default'` - `samsara/support_feedback.py:63` | - |
| `mode` [FIXED -> `'hold'`] | `'hold'` - `dictation.py:2905` + 13 more, `main_window_qt.py:336`, `settings_qt.py:2213`, `tray_qt.py:322` | `'default'` - `samsara/support_feedback.py:67` | - |
| `performance_mode` [FIXED -> `'balanced'`] | `'balanced'` - `dictation.py:4670`, `:7481`, `:8781`, `:10680`, `settings_qt.py:4713` | `'default'` - `samsara/support_feedback.py:66` | - |
| `hotkey` [FIXED -> `'ctrl+shift'`] | `'ctrl+shift'` - `first_run_wizard_qt.py:1042`, `settings_qt.py:2224`, `tutorial_qt.py:333` | `'?'` - `samsara/ui/tray_qt.py:463` | - |
| `hyperion_host` [FIXED -> `''`] | `'192.168.50.247'` - `plugins/commands/demo_commands.py:68` | `'discoball.local'` - `plugins/commands/demo_commands.py:291` | `''` - `plugins/commands/hyperion_lights.py:20` |

### Which of these actually matter

Four are behavioural and can change what the app does on a machine where the key is absent:

1. **`wake_word_config.phrase` - `'jarvis'` vs `'samsara'`.** Two different wake words depending
   on which code path reads first. Verified in source:
   ```
   dictation.py:8094:        wake_phrase = ww_cfg.get('phrase', 'jarvis')
   dictation.py:8871:            wake_phrase = ww_config.get('phrase', 'samsara').lower()
   ```
   On a fresh install with no `phrase` set, detection and the debug UI disagree about what the
   wake word is. This is the single most dangerous entry in this audit.

2. **`wake_word_config.wake_abort_phrase`** - one path honours three abort phrases, the other
   honours one. "abort" and "cancel dictation" work or silently do not, depending on path:
   ```
   dictation.py:6234:        configured_abort = ww_cfg.get(
                                 'wake_abort_phrase', ['cancel', 'cancel dictation', 'abort'],
   dictation.py:8878:                abort_words = ww_config.get('wake_abort_phrase', ['cancel'])
   ```

3. **`wake_word_enabled` - `False` in 10 places, `True` in the first-run wizard.** The wizard shows
   the feature as on before the key exists; the engine treats it as off. First-run UX disagrees
   with first-run behaviour.

4. **`command_mode.inactivity_timeout_s` - `300` vs `30`.** A 10x divergence between engine and
   settings UI:
   ```
   dictation.py:6447:            timeout_s = cfg.get('inactivity_timeout_s', 300)
   settings_qt.py:2454:        cmd_timeout_spin.setValue(int(cmd_cfg.get('inactivity_timeout_s', 30)))
   ```
   Masked on this machine (config has `1800`), but on a fresh install the settings dialog displays
   30s while command mode actually times out at 300s. Worse, opening and saving Settings would
   write the displayed 30 back, silently cutting the real timeout by 10x.

`model_size` / `compute_type` / `device` / `language` / `mode` / `performance_mode` / `hotkey`
diverge only between real consumers and **display/telemetry** call sites - `'?'` in
`diagnostics_qt.py` and `tray_qt.py`, `'default'` in `support_feedback.py` are deliberate
"unknown" placeholders for reporting, not competing behavioural defaults. They are listed because
the audit asked for every differing default, but they are not defects.

`hyperion_host` is a plugin-local demo default appearing three ways, one of which
(`'192.168.50.247'`) is a hardcoded LAN IP from a developer's network shipped in
`plugins/commands/demo_commands.py`.

## B4. Keys documented in docs/ or README that do not exist in code

Scan of `docs/*.md` + `README.md` for JSON-key-shaped tokens, cross-referenced against the 270
code-read keys and the 267 live config keys.

Most initially-flagged tokens turned out to be real, just read from call sites the scanner could
not attribute (`cancel_word`, `pause_word`, `require_end_word`, `silence_timeout`,
`short_dictate`, `long_dictate` are all read in `samsara/ui/wake_word_debug_qt.py`;
`delay_after` is a `commands.json` macro-step key read at `samsara/handlers.py:329`, not a
config key at all). Those are **not** reported as mismatches.

Genuine documentation-vs-reality mismatches:

1. **`docs/wake-word-implementation-handoff.md:36-90` documents a `wake_word_config` schema that
   the shipped config does not use.** The doc specifies singular, dict-shaped word entries:
   ```json
   "cancel_word": { "enabled": false, "phrase": "cancel",
                    "phrase_options": ["cancel", "abort", "never mind", "scratch that"] },
   "pause_word":  { "enabled": false, "phrase": "pause", ... }
   ```
   The live config uses **plural flat lists**:
   ```
   cancel_words = ["cancel", "cancel dictation", "abort"]
   pause_words  = ["pause", "hold on", "wait"]
   resume_words = ["resume", "continue", "go on"]
   end_words    = ["over", "done", "end dictation"]
   ```
   Both shapes are read somewhere in code (the singular form only by the debug UI), so this is
   documented-but-superseded rather than documented-but-nonexistent. Following the doc produces
   keys the main engine ignores.

2. **`wake_word_config.feedback.play_sound_on_wake` / `play_sound_on_end`**
   (`docs/wake-word-implementation-handoff.md:86-88`) are documented as functional, exist in the
   live config, and are **never read** - only written at `dictation.py:3322-3323`. Documented
   settings that do nothing. These are the overlap with the B2 dead-config table.

3. **`wake_word_config.modes.{dictate,short_dictate,long_dictate}.silence_timeout` /
   `require_end_word`** (`docs/wake-word-implementation-handoff.md:64-78`) are documented as the
   per-mode timing controls but are absent from the live config and read **only** by
   `samsara/ui/wake_word_debug_qt.py:353-359,773`. The actual runtime timing comes from
   `wake_word_config.long_max_duration`, `long_failsafe_duration`, `long_chunk_silence`, and
   `command_mode.utterance_silence_s` - none of which appear in any doc.

4. **`docs/wake-word-implementation-handoff.md:88` documents `audio.speech_threshold`** as
   `0.01`. Code reads it at `samsara/audio_engine/wake_consumer.py:633` via a module constant and
   at `samsara/tts/coordinator.py:445` with a default of `0.03` - three times the documented
   value.

5. **`README.md:258` `wake_profiles[].send_word` / `target_process`** are documented as config
   fields. `wake_profiles` is read (`samsara/audio_engine/wake_consumer.py:946`,
   `samsara/wake_profiles.py:48`), and `target_process` is used in `samsara/session_modes.py`,
   but `send_word` appears in shipped code only as a display-state dict key in
   `samsara/ui/quick_reference_qt.py:221,468`. The README documents a per-profile field with no
   confirmed consumer in the profile-handling path.

`docs/CUDA.md` was checked against the `INCLUDE_CUDA` mechanism and describes an out-of-band CUDA
pack, consistent with the spec's CPU-only default. No mismatch.

---

# REPORT

- [x] **branch before/after** - `feature/v0.22` before, `feature/v0.22` after. No checkout,
  switch, merge, rebase, or stash was run; no commits were made. The only file written is this
  report, `docs/reviews/build_and_config_audit.md`. Analysis scripts were written to
  `%TEMP%\aud\`, outside the repo, so no source file was touched.

- [x] **torch in frozen cpu build yes/no with evidence** - **NO.** The shipped
  `Samsara-Windows-v0.22.1.zip` contains `torch entries: 0`; the embedded PYZ extracted from
  `Samsara.exe` enumerates 4364 modules with `torch 0`, `torchaudio 0`, `torchvision 0`,
  `transformers 0`; `dist/Samsara/_internal` has no `torch*` directory. Torch is excluded by four
  independent mechanisms (no source import, `openwakeword` needing it only under an uninstalled
  `full` extra, explicit `excludes`, no hooks) - the `INCLUDE_CUDA` gate is **not** what keeps it
  out, and that gate only ever moves DLLs, never the Python framework. One CUDA-adjacent artifact
  leaks regardless of the gate: `_internal/ctranslate2/cudnn64_9.dll`, 266,288 bytes, bundled by
  the unconditional `for dll in ['ctranslate2.dll', 'cudnn64_9.dll', 'libiomp5md.dll']` loop and
  sourced from ctranslate2's own wheel, not torch. Its cuDNN 9 backend libraries are absent, so it
  is a non-functional stub in every CPU ZIP.

- [x] **conflicting defaults count** - **12** keys read with differing literal defaults in shipped
  code. **4 are behavioural**: `wake_word_config.phrase` (`'jarvis'` vs `'samsara'`),
  `wake_word_config.wake_abort_phrase` (3 phrases vs 1), `wake_word_enabled` (`False` x10 vs
  `True` in the first-run wizard), `command_mode.inactivity_timeout_s` (`300` engine vs `30`
  settings UI - and saving Settings writes the 30 back). The remaining 8 diverge only at
  display/telemetry call sites (`'?'`, `'default'` placeholders) and are not defects.

- [x] **dead config keys count** - **19** keys present in `C:\Users\Morne\.samsara\config.json`
  and never read. The dominant item is the entire 10-key `ai_command_mode` section, a
  near-duplicate of the live `ava_command_session` whose `enabled: True` contradicts the live
  section's `enabled: False`. Also dead: top-level `command_mode_enabled` (live key is
  `command_mode.enabled`), the `tasks` section, and the two documented-but-write-only
  `wake_word_config.feedback.play_sound_on_*` flags.

- [x] **not done** - Nothing in the requested scope was skipped. Both parts and all ten
  sub-questions were completed. Three limits on the results, stated rather than papered over:
  (1) Static analysis cannot attribute config sections passed through helper functions or
  constructor parameters, so 32 reads are reported in a separate "unattributed" bucket instead of
  being falsely counted as missing keys; each was manually confirmed present. (2) Non-literal
  defaults (`<expr>`) are excluded from the conflict count rather than guessed, so 12 is a floor,
  not a ceiling. (3) The build evidence comes from the pre-existing `dist/` (2026-07-18) and
  `Samsara-Windows-v0.22.1.zip` on disk, since building was prohibited; both agree on every torch
  and CUDA claim, but neither reflects uncommitted working-tree changes to `dictation.py`.
  One task premise is contradicted by the tree and is corrected in A2: `samsara/torch_guard.py`
  is **not** disabled - the import is live and uncommented at `dictation.py:384`, and both the
  module and the import are untracked additions absent from `HEAD`.
