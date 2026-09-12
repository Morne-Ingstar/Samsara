# Hands-free gates findings (current `feature/v0.22`)

## Audio path and current gates

The ring consumer reads 16 kHz frames, applies live VAD/RMS onset and silence-boundary logic, rewinds a prebuffer, then dispatches the completed buffer to `process_wake_word_buffer`; that function resamples, applies an RMS gate, decodes with faster-whisper, and routes the text to wake/session handling. The wake consumer dispatch is at `samsara/audio_engine/wake_consumer.py:811-910`; decode begins at `dictation.py:8628-8710`; delivered wake-session text reaches `_output_dictation` at `dictation.py:8858-8860`.

1. VAD. The live engine is bundled faster-whisper Silero ONNX (`dictation.py:8012-8052`), one 512-sample/32 ms frame at a time (`dictation.py:8294-8307`). A live frame is speech when any Silero probability is `> 0.5` (`dictation.py:8309-8326`). If Silero is unavailable or errors, the path falls back to RMS against `wake_word_config.audio.speech_threshold`, capped at `0.01` when VAD is unavailable (`wake_consumer.py:563-615`). The configured `min_speech_duration` is enforced at flush; default silence is `wake_detection_silence`, with toggle DICTATE `0.65 s`, toggle command `1.0 s`, and quick/wake-session configured timeout (`wake_consumer.py:569-591`, `wake_consumer.py:752-780`). The hands-free decode uses faster-whisper `vad_filter=True` in balanced/accurate mode, with `min_silence_duration_ms=500` and `speech_pad_ms=200/300`; if Silero is unavailable, `process_wake_word_buffer` forces `vad_filter=False` (`dictation.py:4663-4719`, `dictation.py:8693-8699`).

2. Whisper decode. The model is `self.config['model_size']`, default `base`, loaded through `WhisperModel` (`dictation.py:3240`, `dictation.py:5170-5176`). Hands-free calls `get_transcription_params(include_vocabulary=False)` (`dictation.py:8683-8699`). In balanced mode this is beam `3`, temperature not explicitly passed, `no_speech_threshold=0.6`, `log_prob_threshold=-1.0`, `initial_prompt` limited to the explicit user prompt, `condition_on_previous_text=False`, and no explicit `suppress_tokens`; native `compression_ratio_threshold` is not passed and remains faster-whisper’s internal default `2.4` (`dictation.py:698-717`, `dictation.py:4641-4719`). Fast mode is beam `1`, explicit temperature `0.0`, VAD silence `300 ms`/pad `100 ms`; accurate mode is beam `5`, VAD silence `500 ms`/pad `300 ms`, and `condition_on_previous_text=True` (`dictation.py:4679-4706`). Hold-to-dictate starts from the same mode defaults but forces `vad_filter=False`, `condition_on_previous_text=False`, and keeps only the explicit prompt for ordinary hold; command-only hold may include vocabulary (`dictation.py:4722-4813`). Thus hands-free differs from ordinary hold mainly by leaving mode-selected Whisper VAD enabled when Silero is available; the hold path also has a separate contiguous-speech gate and segment-quality pipeline.

3. Post-decode gates. The hold path applies `_apply_segment_quality_gates`, including the fixed hallucination strings, repetition signatures such as `click click`, Signature D corroboration using `no_speech_prob > 0.5`, per-segment quality exhaustion, the long-chunk fallback, and its contiguous Silero/ZCR pre-decode gate (`dictation.py:1164-1253`, `dictation.py:1310-1455`, `dictation.py:8337-8445`). `process_wake_word_buffer` does not call `_apply_segment_quality_gates`, `_is_hallucinated_segments`, `_is_quality_exhausted`, the hold `[LONG]` path, or the hold ZCR floor; it only accumulates `no_speech_prob`, `avg_logprob`, compression ratio, and temperature for diagnostics (`dictation.py:8716-8754`). Therefore those per-segment values are read in the hands-free path for telemetry, not used to reject text. There is a phrase blocklist in the hold helper (`_HALLUCINATION_STRING_BLACKLIST`, `dictation.py:1151-1161`), but it is not called by `process_wake_word_buffer`. `(no speech detected)` is produced by the empty-text history branch of the wake path (`dictation.py:8768-8781`); streaming has a separate UI/history empty branch (`samsara/streaming.py:764-788`).

4. Streaming window. Toggle DICTATE partials start after `FIRST_CHUNK_S` and repeat every `CHUNK_INTERVAL_S` (`samsara/streaming.py:1174-1192`). They decode the current tail with beam `1`, `vad_filter=False`, thresholds `0.6/-1.0`, no previous-text conditioning, and temperature `0.0` (`samsara/streaming.py:1194-1253`). Before display, the only gate is the control-phrase matcher: recognized scratch/commit/switch/Ava/abort phrases are withheld; segment-level hallucination and commit gates are explicitly not run (`samsara/streaming.py:1128-1153`, `1174-1192`). Ordinary partial text is sent directly to the overlay. It is not injected by this preview class, but it is visible before final gating; the final dispatch/injection path is separate (`samsara/streaming.py:1135-1153`, `dictation.py:8858-8860`).

5. Ducking. Hands-free idle duck defaults to `0.8` and is held for the whole wake-word toggle (`dictation.py:3393-3402`, `7721-7752`). Capture duck defaults to `0.15`, opens at toggle/AI-command speech onset, and restores after a `0.5 s` debounce/settle delay (`dictation.py:677-684`, `wake_consumer.py:643-650`, `dictation.py:7785-7888`). Passive wake-word listening keeps the wake phrase at idle duck and delays deep ducking until wake confirmation (`wake_consumer.py:643-650`, `872-879`). The capture path opens a `SessionDucker` over other audio sessions, not the microphone input; the live mic still reads the ACE ring’s raw, non-AEC audio (`wake_consumer.py:532-543`). TV/media therefore remains present in the captured microphone signal even while playback sessions are ducked.

6. Voice profiles. `voice_training_qt.py` loads/saves `training_data.json` containing vocabulary and correction mappings (`samsara/ui/voice_training_qt.py:192-240`, `301-327`). Vocabulary is optionally placed in Whisper `initial_prompt`; corrections are text substitutions after decode (`samsara/ui/voice_training_qt.py:329-400`). Its phrase self-test records five seconds from the configured app microphone and transcribes it; it is not acoustic adaptation and does not train a wake-word model (`samsara/ui/voice_training_qt.py:649-763`). The “Whisper profiles” are therefore decoder prompt/correction data, not speaker embeddings, acoustic adaptation, or wake-word training. Read-only inventory of `C:\Users\Morne\Documents\Claude\voice_samples\`: 16 files total — 15 MP3 files and `_status.txt`; the MP3s total 165.144 seconds (2.752 minutes). No WAV files were present, so the requested recorder’s new corpus is not yet recorded.

| gate | hold path | hands-free path | file:line |
|---|---|---|---|
| Live VAD/onset | Silero/RMS plus hold contiguous gate | Silero/RMS onset and silence flush | `wake_consumer.py:563-615`; `dictation.py:8337-8445` |
| Whisper VAD | forced `vad_filter=False` | mode default, usually `True`; forced off only if Silero unavailable | `dictation.py:4694-4719`, `4722-4813`, `8683-8699` |
| Native metadata thresholds | used by hold segment-quality gates | passed to Whisper and recorded only as wake diagnostics | `dictation.py:1310-1455`, `8716-8754` |
| Hallucination strings/repetition | fixed strings, click/repetition, Signature D | not applied | `dictation.py:1151-1253`; `dictation.py:8628-8781` |
| `[LONG]` / quality exhaustion | applied through hold decode helper | not used | `dictation.py:4815-4859`, `1310-1455` |
| ZCR floor | contiguous fallback for short hold buffers | not used as a hands-free post-decode gate | `dictation.py:8412-8445` |
| Partial display | streaming hold preview has its own display path | toggle DICTATE partials shown unless control phrase | `samsara/streaming.py:1174-1192` |
| Injection | final hold delivery/injection | wake-session `_output_dictation` delivery | `dictation.py:8858-8860`, `10708-10880` |
| Audio duck | normal hold duck path | idle `0.8`, capture `0.15` | `dictation.py:3393-3402`, `7721-7888` |

## Session policy (2026-09-10)

1. **Post-wake prebuffer admission.** A confirmed wake publishes its capture boundary at `dictation.py:8247` (profile-session entry at `dictation.py:8270`; legacy phrase confirmation at `dictation.py:9051`). The default `discard` policy clears audio accumulated while confirmation was pending and skips the onset rewind at `samsara/audio_engine/wake_consumer.py:745`. Timestamp-based trimming at `samsara/audio_engine/wake_consumer.py:357` admits only samples at or after confirmation plus the guard band. The ACE timestamp marks the end of its captured block; the consumer removes whole older blocks and trims a block crossing the boundary. `keep` retains the existing rewind and bypasses the guard. Hold and toggle-DICTATE prebuffer behavior is unchanged. Each post-wake capture emits one DEBUG line and a `wake.capture_policy` flight event at `samsara/audio_engine/wake_consumer.py:368`, with matching `policy`, `discarded_ms`, and `post_wake_guard_ms` fields. The discarded duration includes the omitted rewind window and the samples trimmed at the confirmation boundary. Wake-matching audio still follows the existing detection path.

2. **Bounded wake-session lifetime and onset-only extensions.** The absolute deadline is fixed on session entry at `dictation.py:8274`. The wake inactivity reset at `dictation.py:8320` uses the earlier of that deadline and the configured inactivity deadline. Both a timer and live-frame checks call `dictation.py:8340`, so continuous speech cannot defeat the cap. Closure discards the in-progress capture; queued or already-decoding work from the expired session cannot deliver it. A fresh Silero non-speech-to-speech edge is tracked separately from capture state at `samsara/audio_engine/wake_consumer.py:666`; RMS fallback, sustained speech, queueing, decode completion, and delivery do not extend inactivity. After closure, another Silero onset and a new wake match are required. The toggle-command activity entry is `dictation.py:7052`, its reset call is `dictation.py:7069`, and its existing timer implementation is `dictation.py:7027`. Toggle `command_mode.inactivity_timeout_s` remains unchanged (the current source default is 300 seconds; older 30-second comments are stale). Device-recovery resume still establishes an initial idle deadline.

3. **Audible-risk warning for Whisper fallback.** `dictation.py:8104` emits one WARNING per app instance when hands-free starts without an available OWW model, naming the phrase and stating that the Whisper wake fallback decodes all room audio. Model-load failure while hands-free is already active reaches the same warning. `dictation.py:8115` records `wake.fallback_active` with the phrase and `detector="whisper"`. Repeated model-load attempts or listener toggles do not repeat the warning. The fallback remains available.

Policy configuration is read without modifying the config at `samsara/audio_engine/wake_consumer.py:69`:

| Key | Default | Meaning |
|---|---|---|
| `wake_word.session.prebuffer_policy` | `"discard"` | `"discard"` or `"keep"` for captures opened by a confirmed wake |
| `wake_word.session.post_wake_guard_ms` | `150` | Additional samples excluded after confirmation under `discard` |
| `wake_word.session.max_session_s` | `20` | Absolute limit for a post-wake session, even during continuous speech |
| `wake_word.session.inactivity_timeout_s` | `10` | Inactivity bound, preserving the previous wake-session timeout; the shorter bound wins |

Existing configs store `wake_word` as a phrase string. For that format, the same settings are accepted under `wake_word_config.session`; an object-form `wake_word.session` overrides corresponding legacy settings. Missing keys use the defaults above without a migration or config write. Invalid policies fall back to `discard`; invalid/nonfinite durations fall back to their defaults. A zero guard band is allowed.

Focused verification: `F:\envs\sami\python.exe -m pytest tests/test_wake_session_policy.py -q -p no:cacheprovider` — 21 passed with mocked audio, VAD, model, timers, and clock. No microphone or inference model is opened. Whisper decode parameters, the gate stack, `streaming.py`, P2 ducking, and memo-hotkey code are unchanged.

## Hallucination on silent holds (2026-09-10)

Measured the complete 26-row corpus before adding either gate. Read the live config without writing it: `medium`, `accurate`, `cuda`, `float16`, `language="auto"`. The benchmark now resolves that language through the production resolver (`None` for auto), retains `info.language` and `info.language_probability` even when segment gates suppress all text, and accepts `--force-language LANG`. It binds selected production definitions from source instead of importing the desktop app or its package bootstrap. The model is loaded from the existing local cache; no new dependencies were installed.

Reproduce the calibration:

```powershell
& 'F:\envs\sami\python.exe' -u tools/hf_bench.py --corpus 'C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus' --out perf_artifacts/hf_lang_confidence
```

Artifacts: [Markdown](../perf_artifacts/hf_lang_confidence.md) and [JSON with every row](../perf_artifacts/hf_lang_confidence.json). Gates were ON in the existing offline benchmark seam: native Whisper VAD/thresholds and production segment-quality gates. This seam does not simulate capture, the hold prefix gate, session dispatch, or injection; its VAD setting remains the benchmark's existing `True`, whereas hold decoding uses `False`. These are baseline calibration results, not a replay proving the live foreign-script incident is fixed.

| label | detected language(s) | probability min/mean/max | text produced yes/no |
|---|---|---|---|
| speech_owner | en | 0.984375 / 0.990072 / 0.993164 | yes (6/6) |
| speech_owner_over_media | en | 0.983398 / 0.987630 / 0.994629 | yes (3/3) |
| silence | en | 0.541504 / 0.541504 / 0.541504 | no (0/3) |
| nonspeech_room | en | 0.541504 / 0.541504 / 0.541504 | no (0/4) |
| nonspeech_body | en | 0.541504 / 0.598145 / 0.824707 | no (0/5) |
| media_only | en | 0.840332 / 0.855957 / 0.886230 | yes (3/3) |
| wake_over_media | en | 0.981934 / 0.983398 / 0.984863 | yes (2/2) |

The minimum across the 6 owner + 3 owner-over-media rows is **0.9833984375** (`speech_owner_over_media_03.wav`). The maximum across the 3 silence + 4 room + 5 body rows is **0.82470703125** (`nonspeech_body_04.wav`). They separate, with a gap of **0.15869140625**. The midpoint is **0.904052734375**; rounding down to two decimal places gives **0.90**, the default for `language_confidence_floor` (`samsara/languages.py:19`). Invalid config floors fall back to this value; no config migration or write occurs.

The language gate enters at `dictation.py:4841`; its rolling state and decision are in `samsara/languages.py:87`. It rejects unexpected languages below the floor, or transcripts with more than 30% of letters outside the expected scripts. A forced configured language supplies the expected set; auto uses English plus the last 20 accepted decode languages. The state lives only in the app instance. The script-range table is in `samsara/languages.py`; accents, punctuation, numbers and emoji do not create a script mismatch. Unicode normalization and letter-only counting prevent spaces or emoji from diluting foreign-script text. The table references the [Unicode script data](https://www.unicode.org/Public/17.0.0/ucd/Scripts.txt).

The same check covers hold dictation (including long split decodes and retry handling), toggle DICTATE/AVA, continuous transcription, wake-buffer transcription, and DICTATE commit re-decodes. Rejections become empty results before dispatch/paste, log one INFO line with language, probability and the first 40 characters, and emit `decode.language_rejected`. The source has no dedicated no-speech earcon, so final rejections reuse its existing soft refusal sound, `scratch_refuse`. DICTATE preview partials are also checked before display (`samsara/streaming.py:1210`), without learning from provisional text or repeatedly playing feedback. Command-only decodes keep their prior behavior. An intentionally rejected hold decode cannot trigger the missing-text retry; a rejected retry cannot replace an accepted original.

The long-buffer gate is `dictation.py:8477`, called at `dictation.py:10805`. It reuses **`_SANITY_RMS_FLOOR_DB = -40.0`** (`dictation.py:829`, linear RMS 0.01). For buffers longer than eight seconds, an overall RMS at or above that threshold bypasses VAD. Below it, only the first eight seconds are checked for contiguous speech. The decode is skipped only when both conditions indicate silence. Short buffers retain the existing whole-buffer contiguous check and head grace. This preserves later-onset speech when overall RMS clears the existing floor.

Measured added work relative to the previous long-buffer bypass, using 30-second float32/16 kHz buffers, the bundled Silero ONNX VAD, and 30 repetitions after the first call:

| 30-second buffer | result | first call | mean | median | p95 |
|---|---|---:|---:|---:|---:|
| Silence | skipped; first 8 s scanned | 11.59 ms | 10.93 ms | 10.48 ms | 12.23 ms |
| Speech only after 10 s, RMS above floor | retained; no VAD scan | 0.76 ms | 0.65 ms | 0.60 ms | 0.84 ms |

VAD model initialization and live lock contention are excluded. Reproduce with `F:\envs\sami\python.exe perf_artifacts/hf_long_buffer_latency.py`; [raw timings](../perf_artifacts/hf_long_buffer_latency.json) retain every sample.

Focused verification: `F:\envs\sami\python.exe -m pytest tests/test_language_confidence_gate.py tests/test_hf_bench.py tests/test_wake_session_policy.py -q -p no:cacheprovider` — **102 passed** (37 gate, 44 benchmark, 21 session-policy). Tests use mocked decode info, no model or audio devices, and no `dictation` import. Checks cover unexpected/expected languages at identical confidence, Telugu/Hiragana mismatch, accented English/emoji, the strict 30% boundary, rolling eviction, forced languages, config overrides, every affected final-decode lane, preview suppression, retries, and the 30-second silence/later-speech cases. Source comparison found all 15 existing duck/memo/wake-policy methods identical to HEAD; session guards within wake decode and memo handling within hold finalization are untouched.

Limitations: all baseline corpus languages were English, so the unexpected-language clause deliberately would not reject those English outputs; the corpus's 12 noise rows already produced no text under existing gates. Media-only rows still produce English text, and this gate is not a speaker-identity filter. Long quiet buffers whose speech starts after the first eight seconds and whose overall RMS stays below the existing floor meet the specified rejection rule. Samsara was left running; the modified code has not been loaded into that live process or validated with a live silent hold. No commit was made.

## language_probability as source gate -- measurement (2026-09-10)

**VERDICT LINE: not usable.** The lowest single floor that rejects all 57 retained media windows is **0.9951171875000001** (accept when probability >= floor). It rejects **18/19 owner windows at 1.2 s (94.74%)**, above the 5% limit, and **11/11 at 2.0 s (100%)**, above the 2% limit. A convenient decimal floor of 0.9952 gives identical decisions on these measurements. Any higher floor can only increase owner rejection.

Artifacts: [Markdown](../perf_artifacts/hf_lang_prob_windows.md) and [JSON with every retained window, VAD drop and gain decode](../perf_artifacts/hf_lang_prob_windows.json).

Read the live config: `medium`, `accurate`, `cuda`, `float16`, language `auto` (resolved to `None`), beam 5. Gates ON uses the existing offline seam: Whisper VAD, native thresholds 0.6/-1.0 and production segment-quality gates. Language metadata is retained before text gating. This does not simulate live capture, routing, injection, or the shipped language/script gate. The serial benchmark uses one model worker and four CPU threads; model/decode/gate settings were not changed. No dependencies were installed.

For each of six `speech_owner` and three `media_only` clips, full 0.8/1.2/2.0/3.0 s windows start at sample zero with hop equal to window length. Each window gets an independent bundled Silero scan over complete 512-sample (32 ms) frames; frame probability > 0.5 is voiced. Windows with fewer than 50% voiced frames are dropped; exactly 50% is retained. Incomplete final windows are excluded rather than padded. Of 357 full candidate windows, 124 were decoded and 233 dropped (29 owner, 204 media). The table reports probability-only FRR among retained owner windows, regardless of post-gate text; VAD drops are not included in that denominator.

| Window s | Kept owner/media | Dropped owner/media | Owner min | Media max | Gap (owner min - media max) | Owner FRR at single floor | Media accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.8 | 29/17 | 14/94 | 0.869628906 | 0.985351562 | -0.115722656 | 29/29 (100.00%) | 0/17 |
| 1.2 | 19/14 | 9/61 | 0.932128906 | 0.995117188 | -0.062988281 | 18/19 (94.74%) | 0/14 |
| 2.0 | 11/13 | 5/32 | 0.947753906 | 0.979492188 | -0.031738281 | 11/11 (100.00%) | 0/13 |
| 3.0 | 8/13 | 1/17 | 0.962890625 | 0.975097656 | -0.012207031 | 8/8 (100.00%) | 0/13 |

The full-clip confidence gap disappears at every tested window size. The strongest media window is `media_only_03.wav`, 3.6-4.8 s: English, probability 0.9951171875, with 22/37 voiced frames; its text also survives the existing gates. All retained windows were detected as English. This measurement supports rejecting language probability as an owner-versus-TV gate.

Each full media clip was also decoded at gains 1.0 and 0.5 using the existing int16 gain/floor mixing path. Pairings are media 01/02/03 with silence 01/02/03, respectively; the same unscaled floor is used at both gains.

| Media file | Probability, gain 1.0 + silence | Probability, gain 0.5 + silence | Change when quieter |
|---|---:|---:|---:|
| media_only_01.wav | 0.733886719 | 0.541503906 | -0.192382812 |
| media_only_02.wav | 0.896972656 | 0.755859375 | -0.141113281 |
| media_only_03.wav | 0.737304688 | 0.800781250 | +0.063476562 |

| Full-clip condition | Media probability mean | Media probability max | Full-owner min minus media max | Text after existing gates |
|---|---:|---:|---:|---:|
| media_only | 0.855957031 | 0.886230469 | 0.098144531 | 3/3 |
| media_only@1.0 | 0.789388021 | 0.896972656 | 0.087402344 | 3/3 |
| media_only@0.5 | 0.699381510 | 0.800781250 | 0.183593750 | 2/3 |

Quieter media widens the aggregate full-clip gap from **0.087402344 to 0.183593750** relative to the six full owner clips (minimum 0.984375); media mean drops by 0.090006510. The effect is not monotonic per clip: media 03 rises by 0.063476562 when halved. Media 01 produces no text at gain 0.5; media 02 and 03 still do. This full-clip gain comparison does not rescue the failed short-window separator.

Coverage: 36.790 s of owner audio and 90.038 s of media, from six and three existing clips respectively. No additional media sources were collected. Cropped windows within a recording and across window sizes are correlated, and these are not newly recorded short utterances. Zero media accepts describes this corpus only. The measured owner rejection is already far beyond both required limits.

Reproduce:

```powershell
& 'F:\envs\sami\python.exe' -B -u tools/hf_bench.py --corpus 'C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus' --lang-prob-windows --media-gain 1.0 --media-gain 0.5 --floor-label silence --out perf_artifacts/hf_lang_prob_windows
```

Focused verification: `F:\envs\sami\python.exe -B -m pytest tests/test_hf_bench.py -q -p no:cacheprovider` — **44 passed**. Six additional inline checks passed for exact 50% admission, drops/tails/slices and metadata retention, one global floor with strict equality, missing/invalid probabilities and probability 1, Silero input preservation/no RMS fallback, combined raw/gain/window routing and paired floors, and forced-language refusal. Source and artifact consistency checks confirm unchanged config/corpus/other pre-existing edits. Samsara remained running. No shipped gate change and no commit.

## Hold capture duck and prebuffer admission (2026-09-10)

The [silence-floor measurement](../perf_artifacts/hf_bench_media_gain_floor_silence.md) and [room-floor measurement](../perf_artifacts/hf_bench_media_gain_floor_room.md) both accepted TV/media speech in **3/3 clips at gain 1.0**, and **0/3 at each tested gain 0.3, 0.15, 0.08, 0.04 and 0.02**. This supports applying the existing 0.15 capture attenuation to ordinary hold recordings. These are three-clip offline measurements, not a guarantee against every media source.

The existing source did have an optional legacy hold duck (`ducking.enabled` / `ducking.level`), contrary to the handoff's description of no duck at all. However, capture activation rewound 1.5 seconds of microphone history that could precede that duck. Ordinary batch and streaming holds now use the shared capture helpers instead of the legacy singleton. Command, Ava and memo recording calls retain their existing legacy policy. No second duck implementation or level setting was added.

| Key | Default | Hold behavior |
|---|---|---|
| `capture_duck_hold_enabled` | `true` | Top-level switch. `false` disables hold ducking entirely and preserves the existing prebuffer admission. |
| `ducking.hands_free_level` | `0.15` | **Exact existing capture factor key reused.** There is no `capture_duck_factor` key in this checkout. Values at or above 1 disable capture ducking as before. |
| `ducking.hands_free_enabled` | `true` | Existing shared-helper enable flag remains respected. |

The live config was read, never written. Its legacy `ducking` object contained `enabled: true` and `level: 0.1`; the capture settings above were absent and therefore use defaults. No config migration is needed.

Hold startup calls `_open_hold_capture_duck` at `dictation.py:10631`, which opens the existing shared helper with a fresh negative owner token at `dictation.py:10550`. Hands-free tokens remain positive and their helpers are unchanged. Stop releases in `finally` at `dictation.py:10757`; Escape/cancel does so at `dictation.py:11341`; failed startup releases at `dictation.py:10532`. The shared close call is `dictation.py:10589`. Shutdown flushes an unowned restore synchronously at `dictation.py:12297`, so it does not depend on the 0.5-second timer surviving `os._exit`. Normal closes retain the existing debounce. A lifecycle lock serializes startup, release, cancellation and shutdown; tests verify that a release racing a slow engagement cannot orphan its owner. Closing either of a hands-free/hold pair leaves media ducked until the other owner also closes.

**Ordering:** the permanent ACE ring is already capturing before the hotkey, so ducking cannot precede its first hardware frame. Instead, hold activation waits for the shared ducker to finish its synchronous session-volume acknowledgements, including when another owner is still starting. Failure to confirm aborts startup and releases the hold token. Confirmation records a `perf_counter` boundary before either consumer activation. The consumer snapshots that boundary at `samsara/audio_engine/dictation_consumer.py:123` (streaming: `:446`) and filters **before accumulation**, at `:182`, `:321` and `:470`, including the final synchronous batch drain. The predicate at `:156` rejects older frames and whole 100 ms frames straddling confirmation. Rewinding remains intact; pre-confirmation audio is discarded rather than passed to Whisper or streaming previews.

This guarantee is against ACE's callback timestamps: a retained block's timestamp minus its duration must be at or after confirmation. Hardware input buffering, callback scheduling delay and room reverberation are not measured by that clock rule. Streaming release also sets an end boundary through `finish_capture` (`samsara/audio_engine/dictation_consumer.py:147`), excluding frames arriving after the owner releases while final decoding completes asynchronously. Once pre-confirmation history has been excluded, the release-tail gate keeps its configured RMS floor instead of estimating ambient noise from the user's opening speech.

**Duck-engage timing:** [raw live measurements](../perf_artifacts/hold_capture_duck_latency_live.json) from the bound production helpers and real WASAPI child show **162.733 ms** for the first engagement, including child startup. Four sessions were enumerated, three ducked, zero failed; the running Python/Pythonw processes were excluded. Twenty immediate reuses of the same debounced duck had **0.0078 ms median**, **0.0097 ms maximum**. These measure volume acknowledgement/owner acquisition, not acoustic settling or end-to-end microphone latency. The measurement restored in `finally`, finished with zero owners, and opened no microphone. Each production hold now logs and emits `hold_capture_duck.engage` with `owner_token`, `confirmed` and `elapsed_ms`.

Reproduce without audio devices using `F:\envs\sami\python.exe -B perf_artifacts/hold_capture_duck_latency.py` (the existing fake IPC child). The script's `--live` mode uses actual session volumes; its repeatable `--exclude-pid` arguments are recorded in the live JSON. It never imports `dictation`, starts the desktop app or writes its config.

Focused verification: `F:\envs\sami\python.exe -B -m pytest tests/test_hold_capture_duck.py tests/audio_engine/test_dictation_consumer.py -q -p no:cacheprovider` — **35 passed** (28 hold-duck tests, 7 existing consumer tests), with mocked apps/duckers and synthetic audio. Coverage includes both nesting close orders, twenty rapid cycles, stale closes, disabled config, start/stop/cancel exceptions, refused/aborted activation, a concurrent hands-free start, shutdown racing startup, immediate shutdown restoration, batch/streaming prebuffer boundaries, asynchronous streaming release, and the release-tail noise estimate. The desktop module is never imported by the new tests; production methods are bound to fake apps from AST definitions.

All 252 other existing `DictationApp` methods remain identical to the pre-task snapshot, including P2-ducking, memo, session-policy, dispatch-lane and language-gate methods. Existing `samsara/languages.py`, `samsara/streaming.py`, benchmark edits and the earlier findings text are preserved. Branch remains `feature/v0.22`; no checkout/switch/merge/rebase/stash or commit. Samsara was left running; the modified recording code has not been loaded into that live process or validated with a live microphone hold.

## Post-fix baseline (2026-09-11)

Re-ran the full 26-clip `hf_corpus` (`speech_owner` x6, `nonspeech_body` x5, `nonspeech_room` x4, `silence` x3, `media_only` x3, `speech_owner_over_media` x3, `wake_over_media` x2 -- the corpus currently has no rows labeled `speech_owner_over_media_leadin`, so "including the lead-in rows" is satisfied trivially by running the plain manifest with no row-filtering flags) against the live config, gates on, language resolved from the live config's `auto`:

```
RESOLVED: model=medium perf=accurate compute=float16 post_gates=True language=auto
```

Reproduce: `F:\envs\sami\python.exe tools/hf_bench.py --corpus "C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus" --out perf_artifacts/hf_bench_post_fix`. Outputs: [perf_artifacts/hf_bench_post_fix.md](../perf_artifacts/hf_bench_post_fix.md), [perf_artifacts/hf_bench_post_fix.json](../perf_artifacts/hf_bench_post_fix.json).

**Baseline location note:** the task asked to diff against `perf_artifacts/hf_bench_live.json`. No file of that name exists anywhere under `perf_artifacts/` or in this repo's git history. The actual 2026-09-10 pre-fix run was found at `C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_live.json` (mtime 2026-09-09) -- `tools/hf_bench.py --out` defaults to a bare `hf_bench` path, and that run predates this repo convention of writing under `perf_artifacts/`. Its embedded config (`model=medium, perf=accurate, compute=float16, device=cuda, language=en, wake_targets=[claude, hermes]`) matches everything about today's live config except `language`, which was `en` there and is `auto` now -- consistent with it being the intended pre-fix baseline, so it was used for this diff rather than treating the file as missing.

| Label | Metric | Before (2026-09-10) | After (2026-09-11) |
|---|---|---:|---:|
| media_only | false_accept_rate | 1.0 (3/3) | 1.0 (3/3) |
| nonspeech_body | false_accept_rate | 0.0 (0/5) | 0.0 (0/5) |
| nonspeech_room | false_accept_rate | 0.0 (0/4) | 0.0 (0/4) |
| silence | false_accept_rate | 0.0 (0/3) | 0.0 (0/3) |
| speech_owner | WER | 0.030303 | 0.030303 |
| speech_owner | false_reject_rate | 0.0 (0/6) | 0.0 (0/6) |
| speech_owner_over_media | WER | 0.0 | 0.0 |
| speech_owner_over_media | false_reject_rate | 0.0 (0/3) | 0.0 (0/3) |
| speech_owner_over_media | pre_roll_contamination_words_per_utterance | 0.0 | 0.0 |
| wake_over_media | wake_detected | 0/2 | **2/2** |
| wake_over_media | command_text | `['', '']` | `['shift chrome to left monitor', 'open clawed']` |

Detected-language probability: the pre-fix baseline's rows carry no `detected_language`/`language_probability` fields at all (`language_confidence` is new instrumentation), so there is no "before" figure for this metric -- after-only, from the post-fix run:

| Label | probability min | probability mean | probability max |
|---|---:|---:|---:|
| media_only | 0.840332 | 0.855957 | 0.886230 |
| nonspeech_body | 0.541504 | 0.598145 | 0.824707 |
| nonspeech_room | 0.541504 | 0.541504 | 0.541504 |
| silence | 0.541504 | 0.541504 | 0.541504 |
| speech_owner | 0.984375 | 0.990072 | 0.993164 |
| speech_owner_over_media | 0.983398 | 0.987630 | 0.994629 |
| wake_over_media | 0.981934 | 0.983398 | 0.984863 |

**Anything worse: none.** Every false-accept rate, WER, false-reject rate and pre-roll contamination figure is bit-identical before/after (the same model/perf/compute/device and the same detected language, `en`, on every clip -- switching the configured language from fixed `en` to `auto` changed nothing here because auto-detection landed on `en` every time). The one change is `wake_over_media` improving from `0/2` to `2/2` wake detections, with both clips now producing real dispatched command text instead of empty strings.
