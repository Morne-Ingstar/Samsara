# Guidance-Surface Audit vs Live Config / Command Table

Date: 2026-09-13
Tree audited: commit `ac7f6d3` (the commit both `feature/v0.22` and `master` point at). Read-only audit: no checkout, no source edits, no pytest, `dictation` never imported. `C:\Users\Morne\.samsara\config.json` was not read.
Interpreter used for the registry dump: `F:\envs\sami\python.exe tools/dump_command_metadata.py --summary` -> **481 commands (287 builtin, 194 plugin)**.

Precedent: `Docs/reviews/build_and_config_audit.md` (2026-09-10) -- same shape: every claim quoted, one verdict, the ground-truth line that decides it.

## Method

Every string literal in the seven surfaces was extracted with `tokenize` (line numbers are the literal's first line) and each one that asserts a fact -- a key, a phrase, a mode name, a number, a "say X to do Y" -- was checked against:

| Ground truth | Where |
|---|---|
| config defaults | `dictation.py:3346-3700` (`default_config`) |
| hotkey handling | `dictation.py:5762` `on_key_press`, `:5961` `on_key_release`, `:6185` `_check_command_mode_key`, `:6024-6120` CapsLock hook, `:6255-6271` Ava key |
| session control words | `samsara/session_modes.py:79-155` (`_WHOLE_UTTERANCE_SWITCHES`, `_PREFIX_SWITCHES`, `SCRATCH_THAT_PHRASE`, `DICTATE_COMMIT_PHRASE`, `_DICTATE_COMMIT_HOMOPHONES`, `SESSION_SLEEP_PHRASES`, `SESSION_STOP_PHRASES`, `GLOBAL_SESSION_EXIT_PHRASES`), `:279` `DEFAULT_AVA_INVOCATIONS`, `:346-366` `detect_stage_reference` |
| settings schema | `samsara/config_schema.py` (`SETTINGS_SCHEMA`) |
| command table | `commands.json` + `plugins/commands/*.py` via `tools/dump_command_metadata.py` |
| packs | `samsara/command_packs.py` (`PACKS`, `get_enabled_packs`) |

Verdicts: **OK** / **STALE** (was true, changed) / **FALSE** (never true or wrong now) / **DEAD** (control or text references a config key nothing reads) / **MISSING** (behaviour exists, no surface mentions it). Docstrings, log lines, stylesheets and config-key literals that only name a key were not counted as claims.

## Known findings from the manual Modes-page review -- verified

| Finding | Verdict | Decided by |
|---|---|---|
| `streaming_hotkey` is DEAD: hook hardcodes 'caps lock', gated on `streaming_mode` which only the tray toggles | **Confirmed** | `dictation.py:6042` (`if not self.config.get('streaming_mode', False): return`), `:6047-6048` (`keyboard.hook_key('caps lock', ...)`); `streaming_hotkey` is read only by `samsara/ui/quick_reference_qt.py:141` and `samsara/ui/settings_qt.py` (display); the only toggle is `samsara/ui/tray_qt.py:360-362` -> `app.set_streaming_mode` (`dictation.py:12380`) |
| `command_hotkey` is not gated by `command_mode.enabled` | **Confirmed** | `dictation.py:5794` reads `command_hotkey`, `:5814` fires on `check_hotkey_state(command_hotkey) and not self.hotkey_pressed and not self.recording` -- no `command_mode` read; `command_mode.enabled` gates only `_check_command_mode_key` (`:6205-6209`) |
| toggle hands-free opens in DICTATE and buffers until "end" / `dictate_commit_hotkey` | **Confirmed** | `dictation.py:6832` `reset(initial_mode=SessionMode.DICTATE)`, `:6596` `buffer_dictate_until_commit=True`, `:5862-5868` commit hotkey gated by `_hands_free_dictation_commit_available()` (toggle + DICTATE lane + buffering); `session_modes.py:117` `DICTATE_COMMIT_PHRASE = "end"`, `:129` "and" is accepted as a homophone |
| memo / correction / capture_correction / continuous_commit hotkeys exist in code with no settings surface | **Confirmed** | `dictation.py:5796` `memo_hotkey` (ctrl+alt+m), `:5832` `correction_hotkey` (ctrl+alt+r), `:5843` `hotkeys.capture_correction` (ctrl+alt+x), `:5889-5891` `continuous_commit_hotkey` (ctrl+space, only with `continuous_commit_trigger == 'key'`); zero references to any of the four in `samsara/ui/settings_qt.py` or `samsara/config_schema.py`; none of the seven guidance surfaces mentions them either (see MISSING rows below) |
| `ava_mode_key` runtime accepts single named keys only | **Confirmed** | `dictation.py:6258-6260` `_get_pynput_command_key(ava_key_name)`; `:951-955` returns `None` for `mouse4`/`mouse5` and unknown names; `samsara/mouse_hook.py` has no Ava reference -> a mouse value silently disables the Ava key |

---

## 1. First-run wizard -- `samsara/ui/first_run_wizard_qt.py`

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 335 | "Tap Right Ctrl once to start a 15-minute hands-free session." | OK | `_USE_CASE_CONFIGS['chronic_pain']` sets `command_mode.enabled/mode=toggle/inactivity_timeout_s=900` (`first_run_wizard_qt.py:306-316`); default `command_mode.button = "rctrl"` (`dictation.py:3653`); toggle latch `dictation.py:6185-6232` |
| 336 | "Say 'command', 'dictate', or 'hey ava' to switch lanes." | FALSE **FIXED** (samsara/ui/first_run_wizard_qt.py:380) | bare "command" is not a switch word: `session_modes.py:79-96` `_WHOLE_UTTERANCE_SWITCHES` = "command mode", "dictate mode", "dictation mode", "dictate", "ava mode"; "dictate" OK (`:79`, prefix form `:104`); "hey ava" OK (`:279` `DEFAULT_AVA_INVOCATIONS`) |
| 337 | "In Dictate, say 'end' by itself to paste your thought and keep dictating." | OK | `session_modes.py:117` `DICTATE_COMMIT_PHRASE = "end"`; `dictation.py:6596` `buffer_dictate_until_commit=True` |
| 338 | "Say 'stop listening' at any time to leave hands-free mode." | OK | `session_modes.py:150-155` `GLOBAL_SESSION_EXIT_PHRASES` |
| 336-338 | (the card lists no sleep or stop word) | MISSING **FIXED** (samsara/ui/first_run_wizard_qt.py:396) | `session_modes.py:141-145` `SESSION_SLEEP_PHRASES` ("go to sleep", "samsara sleep", "sleep now") and `:149` `SESSION_STOP_PHRASES = ("stop",)` -- no surface mentions "stop" |
| 341-342 | "All your data stays on this machine. Voice recognition runs locally via Whisper — nothing is sent to the cloud." | OK | true at the profile's defaults: `privacy` sets `cloud_llm.enabled = False` (`:319`); `smart_corrections.allow_cloud_fallback = False` (`dictation.py:3605`). Opt-in exceptions exist ("ava cloud" command, `cloud_llm.enabled`) -- the sentence does not say "unless you turn on cloud mode" |
| 345 | "CapsLock streaming is enabled. Hold CapsLock for live transcription." | OK | `power_user` sets `streaming_mode = True` (`:324`); hook installed only then (`dictation.py:6042-6048`); press starts, release stops (`:6076-6120`) |
| 349-350 | "Hold Ctrl+Shift, speak, and release." / "Text appears wherever your cursor is." | OK | defaults `hotkey = "ctrl+shift"`, `mode = "hold"` (`dictation.py:3347, 3366`) |
| 371 | `"wake_word": "jarvis"` written to config | STALE **FIXED** (samsara/ui/first_run_wizard_qt.py:452) | legacy flat key; migrated into `wake_word_config.phrase` at `dictation.py:4013-4014` then treated as a migration marker (`:4082`). The live key is `wake_word_config.phrase` |
| 372 | `"wake_word_timeout": 5.0` written to config | DEAD **FIXED** (samsara/ui/first_run_wizard_qt.py:453) | `dictation.py:4016-4023` only copies it into `wake_word_config.modes.dictate.silence_timeout` *if that nesting exists*; the defaults (`:3413-3440`) have no `modes` block, so the value is dropped. The live key is `wake_word_config.audio.wake_command_timeout` (`config_schema.py:88`) |
| 686 | "Set up for hands-free use with health tracking, voice reminders, and spoken feedback." | OK | `chronic_pain` sets `tts.enabled`, `wake_word_enabled`, `command_mode` toggle (`:306-316`); `health` and `alarms` packs are `default_enabled: True` (`command_packs.py`) |
| 688 | "Everything stays on your machine. No cloud, no accounts, no data leaves your computer." | OK | `privacy` -> `cloud_llm.enabled = False` (`:319`); same opt-in caveat as line 341 |
| 690 | "Scriptable voice macros, command packs, and deep customization." | OK | `macros` pack exists but is `default_enabled: False` (`command_packs.py`, `dictation.py:3407`); the power-user profile does not turn it on |
| 692 | "Simple speech-to-text. Press a key, speak, release." | OK | `just_dictation` sets `mode = "hold"` (`:327-330`) |
| 762 | "Larger models are more accurate but use more memory and are slower to start." | OK | model_size enum (`config_schema.py:34-38`); ordering claim is external to the repo |
| 771-773 | "~75 MB — lowest accuracy, instant startup" / "~150 MB — good accuracy, fast startup" / "~500 MB — highest accuracy, slower startup" | OK | approximate CT2 sizes of tiny/base/small; not verifiable in the repo, consistent with the `tiny/base/small` options (`:1033-1035`) |
| 799, 919 | "The model downloads on first use (once)." | OK | faster-whisper download-on-load; not a repo fact |
| 818-819 | "Hold to Record" / "Hold to record, release to transcribe" | OK | `hotkey` + `mode = "hold"` (`dictation.py:3347, 3366`) |
| 820-821 | "Continuous Mode" / "Toggle always-on dictation" | OK | `dictation.py:5880-5884` continuous hotkey toggles |
| 822-823 | "Wake Word Mode" / "Toggle wake word activation" | OK | `dictation.py:5853-5857` flips `wake_word_enabled` |
| 824-825 | "Command Only" / "Hold to speak a command (no text output)" | OK | `dictation.py:5814-5820`; (not gated by `command_mode.enabled` -- nothing says so: MISSING) |
| 857, 1024 | 'Say "Jarvis" or "Hey Jarvis" to activate voice commands' | OK | `wake_word_config.phrase_options` (`dictation.py:3421`) |
| 872 | "Wake word is off for your setup — turn it on anytime in Settings." | OK | `wake_word_enabled` in schema (`config_schema.py:73`) |
| 879 | "More wake word options coming soon." | STALE **FIXED** (samsara/ui/first_run_wizard_qt.py:966) | the options already exist: `phrase_options` = jarvis / hey jarvis / computer / hey computer / samsa / hey samsa (`dictation.py:3421`), plus `wake_profiles` (`:3455-3475`); the wizard row is a fixed label (`:1024`) |
| 927 | "Don't show me hints (you can re-enable this in Settings)" | OK | `hints_enabled` (`config_schema.py:50`) |
| 1043-1045 | f"Record: {hotkey} (hold)" / f"Continuous: {continuous_hotkey}" / f"Wake Word Key: {wake_word_hotkey}" | OK | all three read in `on_key_press` (`dictation.py:5790-5794`) |
| 1046 | f"Wake Phrase: {DEFAULT_WAKE_PHRASE}" | OK | constant, not config (`constants.py:45`); correct at first run, would be wrong if the wizard were re-run after changing the phrase |
| -- | Ava key (`ava_mode_key`, default right_alt), undo (ctrl+alt+z), cancel (escape), memo/correction/capture hotkeys never mentioned | MISSING **FIXED** (samsara/ui/first_run_wizard_qt.py:928) | `dictation.py:6258`, `:5826`, `:5901`, `:5796`, `:5832`, `:5843` |

Counts: OK 22, STALE 2, FALSE 1, DEAD 1, MISSING 3.

---

## 2. Mic setup wizard -- `samsara/ui/mic_setup_wizard_qt.py`

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 327-328 | "Select the microphone you'll be speaking into, then say a few words to confirm it's picking up your voice." | OK | device combo from `DictationApp.get_available_microphones()` (`:661`), own meter stream (`:573`) |
| 359-361 | "Speak at the distance and volume you'll normally use. Aim for the green zone -- it means Samsara will hear you clearly without picking up too much background noise." | OK | meter zones are the wizard's own (`:771-795`) |
| 399, 876 | f'Say "{wake_phrase}" three times at your normal speaking volume. Each circle lights up when Samsara hears it.' | OK | `_OWW_ATTEMPTS = 3` (`:68`); phrase from `wake_word_config.phrase` (`:397`) |
| 446 | "Open Wake Word Debug (advanced)" | OK | `self._app.open_wake_word_debug()` (`:1038`) -> `dictation.py:12707` |
| 821 | f"Calibrated -- threshold set to {threshold:.4f}" | OK | writes `wake_word_config.audio.speech_threshold` (`:815-819`), a live schema key (`config_schema.py` `wake_word_config.audio.speech_threshold`) |
| 846-847 | "Please stay quiet for <b>3 seconds</b> while Samsara measures the background sound around your microphone." | OK | `seconds=3.0` (`:857`) |
| 881 | "Background calibration was unavailable; continuing with the existing setting." | OK | `:868-884` |
| 908-909 | f'No built-in model for "{wake_phrase}" -- Whisper handles detection instead (no live preview here).' | OK | Whisper-transcript fallback `samsara/wake_word_matcher.py:74` `match_wake_phrase`; OWW model lookup `:894-903` |
| 912 | 'Use "Test Wake Word..." in Settings -> Advanced to run a live test.' | FALSE **FIXED** (samsara/ui/mic_setup_wizard_qt.py:72) | no "Test Wake Word" control exists in `samsara/ui/settings_qt.py` (no match for "Test Wake"); the Advanced tab (`settings_qt.py:646`, `_build_advanced_tab` `:4750`) has no wake test. The only live test is this wizard's own step 3 and `open_wake_word_debug` |
| 1004-1005 | "If it keeps missing, lower 'Wake word sensitivity' in Settings -> Advanced (try 0.10)." | STALE **FIXED** (samsara/ui/mic_setup_wizard_qt.py:1106) | the control is labelled "Wake-word threshold" (`settings_qt.py:2539-2540`) and lives on the **Modes** tab (`_build_modes_tab` `:2032`, spin at `:2535`), schema tab `hotkeys` (`config_schema.py:118-125`); 0.10 is inside the schema range (min 0.05) |
| 1020 | f"Speech threshold: calibrated ({self._cal_threshold:.4f})" | OK | `:815-821` |
| 1026 | f"Wake word: detected {hits}/{_OWW_ATTEMPTS} during test" | OK | `:952-985` |
| 758 | "Could not switch microphones. Check the log and try again." | OK | `:730-756` runtime switch |
| 784 / 790 / 795 | "Essentially silent -- check the mic is connected and selected above." / "Very loud -- you may get clipping. Back off slightly or reduce gain." / "Level looks good -- keep talking naturally." | OK | wizard's own RMS bands (`:771-795`) |
| -- | step 3 runs its wake test whether or not `wake_word_enabled` is on; nothing tells the user the wake word is off (default `False`, `dictation.py:3630`) | MISSING **FIXED** (samsara/ui/mic_setup_wizard_qt.py:416) | `:894-912` initialises the detector regardless of `wake_word_enabled` |

Counts: OK 12, STALE 1, FALSE 1, DEAD 0, MISSING 1.

---

## 3. Ava guide -- `samsara/ui/ava_guide_qt.py`

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 129 | "Right Alt (default)" (`right_alt`) | OK | `dictation.py:6258` default `'right_alt'`; `_get_pynput_command_key` `:951-973` maps `right_alt` |
| 130 | "Right Ctrl" (`rctrl`) | OK | mapped at `:962`; note it is also the default `command_mode.button` (`:3653`) and the two are mutually exclusive at `:6269` |
| 131 | "F13" (`f13`) | OK | `:959` f1-f24 |
| 132-133 | "Mouse button 4" (`mouse4`) / "Mouse button 5" (`mouse5`) | DEAD **DEFERRED** (08b owns the Mouse-row removal in ava_guide_qt.py:126-136; not touched here) | `_get_pynput_command_key` returns `None` for mouse4/mouse5 (`dictation.py:954-955`); the Ava key check `:6258-6271` has no mouse path and `samsara/mouse_hook.py` never reads `ava_mode_key` -- choosing either disables the Ava key silently |
| 341-342 | "Ava is Samsara's AI assistant. It runs entirely on your machine — nothing leaves your computer." | STALE **FIXED** (samsara/ui/ava_guide_qt.py:932) | true only while `cloud_llm.enabled` is `False`: the "ava cloud" / "use cloud" command (`plugins/commands/ask_ollama.py` `"ava cloud"`) and `cloud_llm.*` schema keys (`config_schema.py`) route Ava to a cloud provider |
| 345-346 | "The main thing Ava does: you don't have to memorise command phrases. Say what you mean in plain language, and Ava figures out the right action." | OK | ACTION / ACTION2 model routes (`ask_ollama.py:686` `handle_response`) |
| 345-346 | (nothing says a model-chosen write/destructive command now asks for "yes", or that undeclared plugins are not model-callable) | MISSING **FIXED** (samsara/ui/ava_guide_qt.py:944) | `samsara/execution_policy.py` policy table (module docstring `:15-23`), `authorize` `:560-620` |
| 369 | '"scroll down a little"' -> '"scroll just a tiny bit"' | OK | phrase registered (`core`, plugin `scroll.py`); dump confirms |
| 370 | '"pain level 6"' -> '"my pain is about a 6 today"' | OK | phrase `pain level` (`health` pack, default on) |
| 371 | '"took ibuprofen 400mg"' -> '"I just took my ibuprofen"' | OK | phrase `took` (`health` pack) |
| 372 | '"complete alarm"' -> '"I finished my stretch"' | OK | phrase `complete alarm` (`alarms` pack, default on) |
| 373 | '"read alarms"' -> '"what alarms do I have set?"' | OK | phrase `read alarms` (`alarms` pack) |
| 391-392 | "Ava uses a small AI model that runs locally via Ollama. The next steps get that set up." | OK | `ava_command_session.backend` default `ollama` (`config_schema.py`), `ask_ollama.get_host` -> `http://localhost:11434` |
| 405-406 | "Ollama is the runtime that lets AI models run locally. It installs as a background service and Samsara connects to it automatically." | OK | `ask_ollama._start_health_monitor` (`ask_ollama.py:1790-1800`) |
| 444-446 | f"Go to {_OLLAMA_DOWNLOAD} and download Ollama for Windows." / "Run the installer — it starts Ollama automatically." / 'Click "Check again" above once it's installed.' | OK | `:125-126` URLs; external facts |
| 462-463 | "Ollama only uses resources when Ava is actively answering — it idles silently in the background otherwise." | STALE **FIXED** (samsara/ui/ava_guide_qt.py:953) | `ava_command_session.keep_warm` default `True` (`config_schema.py:262`, `ava_command_session.py:98`) pre-loads and holds the model on session entry (`dictation.py:7145`); `smart_corrections.keep_alive = "30m"` (`dictation.py:3607`) keeps the corrections model resident |
| 480-481 | "Ava needs a language model to understand you. Pick one below and click Pull — it downloads once and runs offline forever." | OK | `ollama pull` (`:820-869`) |
| 575-578 | "Activation key" / "Hold this key and speak — Ava listens while you hold it, then responds when you release." | OK | `dictation.py:6269-6271` enter on press, exit on release; gated by `ava_mode_enabled` (`:6256`) |
| 615 | f'"{wake.title()}, hey Ava" also works — no key needed, fully hands-free.' | OK | wake command text -> `process_text` (`dictation.py:9855-9880`) -> "hey ava" command (`ask_ollama.py` `"hey ava"` aliases "ava", "ask ava"...). With no question after it Ava only says "Yes? How can I help?" and does not keep listening (`ask_ollama.py` `handle_ask_ava`, `if not remainder`) |
| 616-617 | f'"{wake.title()}, Ava local" — same as above, but guaranteed to stay on your computer. Nothing sent online.' | FALSE **FIXED** (samsara/ui/ava_guide_qt.py:975) | "ava local" is a mode switch, not a way to ask: `ask_ollama.py` `switch_local` sets `cloud_llm.enabled = False` and speaks "Cloud mode disabled. Using local Ollama." It never forwards a question |
| 618-619 | f'"{wake.title()}, Ava cancel" — if Ava asked a question and is waiting for your answer, this clears it.' | OK | `ask_ollama.py:1660` `handle_ava_cancel` -> `execution_policy.stop_all` |
| 637 | "Enable AI commands pack" | OK | `ai` pack `default_enabled: False` (`command_packs.py:139-144`); button writes `command_packs['ai'] = True` (`:890-914`) |
| 894 / 896 | "Ollama: running" / f"AI pack: {'enabled' if ai_enabled else 'disabled (see button below)'}" | OK | `:735-745`, `:890-896` |
| 914 | "AI pack enabled — restart to activate" | OK | packs are applied once, at `CommandExecutor` construction (`samsara/commands.py:182` `set_enabled_packs(get_enabled_packs(...))`, built at `dictation.py:2379`); `reload_config_from_disk` (`:4433`) does not re-apply them |
| -- | the second Ava key -- `ava_command_session.key` (default `left_alt`, `dictation.py:6209`, schema `ava_command_session.*`) -- and the "ava mode" switch word (`session_modes.py:96`) are mentioned nowhere in the guide | MISSING **FIXED** (samsara/ui/ava_guide_qt.py:980) | `dictation.py:6205-6215`, `session_modes.py:96` |
| -- | "yes" / "ava cancel" / "scratch that" as the answer to Ava's confirmation question is not explained (only "Ava cancel" for a pending question) | MISSING **FIXED** (samsara/ui/ava_guide_qt.py:948) | `ask_ollama.py` `"yes"` command (aliases "confirm it", "do it", "go ahead", ...), `session_modes.py` scratch-that pending-action path |

Counts: OK 19, STALE 2, FALSE 1, DEAD 1, MISSING 3.

---

## 4. Command cheat sheet -- `samsara/ui/command_cheatsheet_qt.py`

The window is data-driven: rows come from the live registry through `commands_cb` (`:513`) and the category dropdown from registered packs (`:336-375`). Static claims are few.

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 211 | "Command Reference" | OK | tray menu item of the same name (`samsara/ui/tray_qt.py:416`) |
| 375 | "All commands" | OK | category picker default (`:336-375`); rows from the registry (`:513`) |
| 458 | "Filter commands..." | OK | `:458-460` |
| 310, 663 | click a row -> f"[CHEATSHEET] Execute '{phrase}'" (rows execute the command) | OK | executes through the app's executor (`:300-310`, `:655-663`) -- which now means the execution policy can stage a confirmation for destructive rows (`samsara/execution_policy.py`); the sheet gives no hint that a click may ask "yes?" |
| -- | disabled packs: the sheet does not say whether a listed command's pack is enabled | MISSING **FIXED** (samsara/ui/command_cheatsheet_qt.py:46) | `command_packs.get_enabled_packs` (`samsara/command_packs.py`) vs `commands_cb` (`:513`) -- not verifiable from the surface strings alone |

Counts: OK 4, STALE 0, FALSE 0, DEAD 0, MISSING 1.

---

## 5. Tutorial -- `samsara/ui/tutorial_qt.py`

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 292 | "Samsara lets you control your computer and type with your voice." | OK | -- |
| 299-300 | "Let's try each thing once — it takes about two minutes." / "Every step is skippable." | OK | `_steps` (`:235-238`), skip handlers `:612-620` |
| 313-314 | "Talk → text appears wherever you're typing" / "Say a command → it happens" | OK | -- |
| 335-336 | f"Hold {hotkey.upper()} and say anything — 'hello world' works fine. Release to transcribe. Text will appear in the box below." | STALE **FIXED** (samsara/ui/tutorial_qt.py:47) | correct for `mode == "hold"` only; the page reads `hotkey` (`:333`) but never `mode`, whose schema options are hold / toggle / continuous (`config_schema.py:67-71`) -- a toggle user is told to hold and release |
| 356-357 | f"💡 Hotkey not working? Make sure Samsara is running and {hotkey.upper()} is your configured record key." | OK | `hotkey` (`dictation.py:5790`) |
| 374 | f" • Hold {cmd_hotkey.upper()} and say \"scroll down\"" | OK | `command_hotkey` hold (`dictation.py:5814-5820`); "scroll down" registered (`scroll.py`, also in `_HANDS_FREE_PRESERVE_COMMANDS` `dictation.py:634`) |
| 376 | f" • Say \"{wake}, scroll down\" (wake word mode)" | OK | shown only when `wake_word_enabled` (`:375`); wake command path `dictation.py:9855-9880` |
| 377, 410 | "• Or any command you know — 'show numbers', 'what can I say', etc." / "💡 Try saying 'scroll down', 'show numbers', or 'what can I say'." | OK | `show numbers` (`accessibility` pack, default on), `what can i say` (`core`) -- both in the dump |
| 403-404 | "Commands do things. There are 150+ of them — scroll, open apps, manage windows, type shortcuts, and more." | STALE **FIXED** (samsara/ui/tutorial_qt.py:61) | 481 today (`tools/dump_command_metadata.py --summary`: 287 builtin + 194 plugin) |
| 461-462 | 'Say "what can I say" anytime for the full command list, or open the Command Reference from the tray menu.' | OK | `what can i say` (`plugins/commands/core_utils.py`); `tray_qt.py:416` "Command Reference" |
| 469-470 | 'Ava (your on-device voice assistant) and "show numbers" (click anything by saying its number) are also here once you set them up.' | OK | `show_numbers.py` `click`; sole spoken number = implicit click (`dictation.py:6335-6345`) |
| 484-492 | "Mic Setup Guide" / "Ava Setup Guide" / "Voice Training" -> `open_mic_setup_guide` / `open_ava_guide` / `open_voice_training` | OK | `dictation.py:12614`, `:12619`, `:12607` |
| 635 | `tutorial_complete` written | OK (not dead) | read at `dictation.py:2739` |
| -- | the tutorial never mentions the hands-free session (toggle tap / "end" / "scratch that"), undo (ctrl+alt+z) or cancel (escape) | MISSING **FIXED** (samsara/ui/tutorial_qt.py:84) | `dictation.py:6185-6232`, `:5826`, `:5901`; `session_modes.py:116-117` |

Counts: OK 11, STALE 2, FALSE 0, DEAD 0, MISSING 1.

---

## 6. Quick reference -- `samsara/ui/quick_reference_qt.py`

The file's own hard rule (`:1-20`): "every value shown is read from live config/registries at window-open time -- never hardcoded". The rows below are where that holds and where it does not.

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 59 | `"streaming_hotkey": "capslock"` (fallback) | OK (value) | matches `dictation.py:3377` -- but see line 170 |
| 60 | `"dictate_commit_hotkey": "ctrl+space"` (fallback) | OK | `DEFAULT_CONTINUOUS_COMMIT_HOTKEY = 'ctrl+space'` (`constants.py:21`) |
| 66-67 | `"end_words": ["over", "done", "end dictation"]` / `"wake_abort_phrase": ["cancel", "cancel dictation", "abort"]` (fallbacks) | OK | `dictation.py:3423-3424` |
| 148 | "Dictate (hold to talk)" | FALSE (conditional) **FIXED** (samsara/ui/quick_reference_qt.py:184) | label is hardcoded (`:147-150`) and `mode` is never read; with `mode = "toggle"` or `"continuous"` (`config_schema.py:67-71`) the row misdescribes the key |
| 153 | "Hands-free Session" / "Command Mode (hold)" | OK | chosen by `command_mode.mode` (`:153`); toggle vs hold in `_check_command_mode_key` (`dictation.py:6216-6232`) |
| 158-160 | "Paste staged thought" = `dictate_commit_hotkey`, enabled when `command_mode.enabled` and mode toggle | OK | `dictation.py:5862-5868` + `_hands_free_dictation_commit_available()` |
| 165-166 | "Ava" -> f'Say "{ava_phrase}" during the voice session' | OK | `resolve_ava_invocations` (`session_modes.py:282`), default "hey ava" / "so ava" / "oracle" |
| 165-166 | (the "ava mode" whole-utterance switch is not listed) | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:154) | `session_modes.py:96` `"ava mode": SessionMode.AVA`; `_lane_switch_phrases` filters to COMMAND/DICTATE (`:104-116`) |
| 170-172 | "Streaming (live partials)" = `config['streaming_hotkey']` | DEAD **FIXED** (samsara/ui/quick_reference_qt.py:76) | the hook hardcodes `'caps lock'` (`dictation.py:6047-6048`); no code path reads `streaming_hotkey`; the row is dimmed unless `streaming_mode`, which only the tray toggles (`tray_qt.py:360-362`) |
| 175 | "Undo last dictation" = `undo_hotkey` | OK | `dictation.py:5826-5829` |
| 210-215, 458-459 | hands-free toggle: "latches a session" / "requires command_mode.enabled and mode=toggle" | OK | `dictation.py:6205-6232` |
| 249 | "Legacy command-only lane retained for compatibility." (COMMAND) | OK | `session_modes.py:60-66` `SessionMode` docstring ("COMMAND remains the legacy command-only lane") |
| 250 | 'HANDS FREE: ordinary speech is buffered dictation; exact navigation commands execute immediately. Say "end" to paste and continue.' | OK | `session_modes.py:1103-1140` hands-free probe path; `:117` "end" |
| 250 | (an isolated "and" also commits) | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:305) | `session_modes.py:129` `_DICTATE_COMMIT_HOMOPHONES = {"end", "and"}` |
| 251 | 'Everything you say goes to the local AI agent as natural language. Say "submit that" / "the text" to attach anything you just dictated.' | OK | `detect_stage_reference` (`session_modes.py:346-366`: "the text" / "what i dictated" / "the dictation", or verb + "that"); "local" has the same cloud caveat as the Ava guide |
| 282-288 | token descriptions "paragraph break" / "line break" / "tab character" / "bullet point" | OK | `samsara/formatting_tokens.py:38-41` `_SIMPLE_TOKENS` ("new paragraph", "new line", "insert tab", "bullet point"/"bullet") |
| 363 | "Values reflect your current settings" | STALE **FIXED** (samsara/ui/quick_reference_qt.py:184) | true for every row except 148 and 170 above |
| 470 | "Wake send word" = `end_words` | OK | read on the wake path (13 references in `dictation.py`) |
| 489, 516 | "Exit session phrase(s)" / "Abort phrase(s)" = `wake_abort_phrase` + `GLOBAL_SESSION_EXIT_PHRASES` | OK | `:204-208`; `GLOBAL_SESSION_EXIT_PHRASES` now includes the three sleep phrases (`session_modes.py:150-155`) so they render -- under a label ("Abort") that does not say sleep keeps the staged draft (`:136-140`) |
| 489, 516 | `command_mode.abort_phrases` (schema key) is not merged into the list | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:252) | `config_schema.py` `command_mode.abort_phrases`; `dictation.py:6549-6555` merges it into the live session's abort list, the quick reference does not (`:204-208`) |
| 489, 516 | "stop" is not listed | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:307) | `session_modes.py:149` `SESSION_STOP_PHRASES = ("stop",)` |
| 482-483, 512-513 | "DICTATE commit" / "Commit DICTATE thought" = "end" | OK | `session_modes.py:117` |
| 485, 518 | "scratch that" | OK | `session_modes.py:116` |
| -- | hotkeys the app reads but the reference never lists: `continuous_hotkey` ctrl+alt+d, `wake_word_hotkey` ctrl+alt+w, `command_hotkey` ctrl+alt+c, `cancel_hotkey` escape, `memo_hotkey` ctrl+alt+m, `correction_hotkey` ctrl+alt+r, `hotkeys.capture_correction` ctrl+alt+x, `continuous_commit_hotkey` ctrl+space (trigger "key") | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:215) | `dictation.py:5790-5796`, `:5832`, `:5843`, `:5889-5891`, `:5901` |
| -- | "dictate <payload>" prefix switch, "literal <command>" escape hatch, "literal on/off" verbatim profile, "retype that", wake `pause_words` / `resume_words` | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:310) | `session_modes.py:104` `_PREFIX_SWITCHES`, `:170-183` `match_literal_payload`; `plugins/commands/verbatim_toggle.py` ("literal on"/"verbatim on", "literal off"/"verbatim off"); `session_mode_commands.py` "retype that"; `dictation.py:9689-9708` |
| -- | `wake_word_config.opens_session` (wake phrase opens the latched session) | MISSING **FIXED** (samsara/ui/quick_reference_qt.py:282) | `config_schema.py:106-116` |

Counts: OK 17, STALE 1, FALSE 1, DEAD 1, MISSING 7.

---

## 7. Stress wizard -- `samsara/ui/stress_wizard_qt.py`

| line | claim (quoted) | verdict | decided by |
|---|---|---|---|
| 198-200 | "This wizard listens for your NEXT real dictation -- perform the step above with your actual hotkey. Nothing needs focus; the box below fills in on its own once captured." | OK | `samsara/diagnostics.py:50-60` `add_one_shot_hook`, armed per step (`:388-424`) |
| 208, 364 | "Waiting for your dictation…" / "Waiting for you to dictate…" | OK | hook armed state |
| 347 | f"Step {idx + 1} of {len(self._battery)} — {step.category}" | OK | battery from `samsara.stress_tests` |
| 359 | f'"{step.expected_text}"' | OK | battery-defined expected text |
| 419 | "accidental_tap/silent_hold: nothing captured within the short window -- a clean pass" | OK | `_pc_no_output` |
| 432-433 | f"No dictation detected — did you hold {hotkey_display}? Try again, or Skip this step." | OK | `hotkey` from config; "hold" carries the same hold-mode assumption as the tutorial (`mode` not read) |
| 453 | "Result ready — Retry, Skip, or Next." | OK | `:445-453` |
| 463 | "Diagnostics:" / "No diagnostics record captured." | OK | `DiagRecord` capture |
| 496-497 | f"{passed} passed, {failed} failed, {skipped} skipped (of {len(self._results)})" | OK | `:490-497` |
| -- | the wizard captures any `diagnostics.record()` (wake, hands-free and streaming completions too, not only the hotkey) but says "your actual hotkey" | MISSING **FIXED** (samsara/ui/stress_wizard_qt.py:200) | `samsara/diagnostics.py:13-16` call sites (hotkey/wake/streaming) |

Counts: OK 9, STALE 0, FALSE 0, DEAD 0, MISSING 1.

---

## Fix status (08c, 2026-09-13)

Every non-OK row above carries **FIXED** (with the file:line of the fix at commit time) or **DEFERRED** (with the reason). Rows marked OK were left alone. See tests/test_first_run_wizard_guidance.py, tests/test_ava_guide_text.py, tests/test_tutorial_guidance.py, tests/test_command_cheatsheet_packs.py and the audit-fix classes in tests/test_quick_reference_qt.py for the pins.

## Totals

| Surface | OK | STALE | FALSE | DEAD | MISSING |
|---|---|---|---|---|---|
| first_run_wizard_qt.py | 22 | 2 | 1 | 1 | 3 |
| mic_setup_wizard_qt.py | 12 | 1 | 1 | 0 | 1 |
| ava_guide_qt.py | 19 | 2 | 1 | 1 | 3 |
| command_cheatsheet_qt.py | 4 | 0 | 0 | 0 | 1 |
| tutorial_qt.py | 11 | 2 | 0 | 0 | 1 |
| quick_reference_qt.py | 17 | 1 | 1 | 1 | 7 |
| stress_wizard_qt.py | 9 | 0 | 0 | 0 | 1 |
| **all** | **94** | **8** | **4** | **3** | **17** |

## The ten worst

1. `quick_reference_qt.py:170` "Streaming (live partials)" shows `streaming_hotkey` -- **DEAD**: the hook hardcodes 'caps lock' (`dictation.py:6047`) and nothing reads the key; the Settings field for it is equally dead.
2. `first_run_wizard_qt.py:336` "Say 'command', 'dictate', or 'hey ava' to switch lanes." -- **FALSE**: bare "command" is not a switch word (`session_modes.py:79-96`); a chronic-pain user following the card cannot leave the dictate lane by voice.
3. `mic_setup_wizard_qt.py:912` 'Use "Test Wake Word..." in Settings -> Advanced' -- **FALSE**: the control does not exist.
4. `ava_guide_qt.py:616` '"{wake}, Ava local" — same as above, but guaranteed to stay on your computer.' -- **FALSE**: "ava local" only flips `cloud_llm.enabled` off; it never asks anything.
5. `ava_guide_qt.py:132-133` "Mouse button 4/5" as Ava activation key -- **DEAD**: `_get_pynput_command_key` returns `None` for mouse names; selecting one silently disables the key.
6. `quick_reference_qt.py:148` "Dictate (hold to talk)" -- **FALSE** for `mode = toggle/continuous`; the window promises live values and hardcodes this one.
7. `first_run_wizard_qt.py:372` writes `wake_word_timeout` -- **DEAD**: dropped by the migration (`dictation.py:4016-4023`); the live key is `wake_word_config.audio.wake_command_timeout`.
8. `mic_setup_wizard_qt.py:1004` "'Wake word sensitivity' in Settings -> Advanced" -- **STALE**: it is "Wake-word threshold" on the Modes tab (`settings_qt.py:2535-2540`).
9. `ava_guide_qt.py:462` "Ollama only uses resources when Ava is actively answering — it idles silently" -- **STALE**: `ava_command_session.keep_warm` (default on) holds the model resident.
10. **MISSING everywhere**: `memo_hotkey` ctrl+alt+m, `correction_hotkey` ctrl+alt+r, `hotkeys.capture_correction` ctrl+alt+x, `continuous_commit_hotkey` ctrl+space, the "stop" phrase (`session_modes.py:149`) and the fact that `command_hotkey` fires with `command_mode.enabled` off -- no wizard, guide, reference or settings page mentions any of them (`tutorial_qt.py:403` "150+ commands" vs 481 is the same class: the surfaces have not kept up with the table).

## Not fixed here

Nothing was changed. Every row names the line that decides it so each fix is a one-line edit or a one-row addition to the surface in question; the DEAD items (streaming hotkey field, mouse options for the Ava key, `wake_word_timeout`) need a decision -- wire them or remove the controls -- before any wording change.
