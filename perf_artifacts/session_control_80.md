# Queue 80: session-control latency and reliability (2026-09-15)

Re-run everything with:

    python perf_artifacts/session_control_latency.py --out-prefix perf_artifacts/session_control_latency_baseline
    python perf_artifacts/session_control_misses.py --out-prefix perf_artifacts/session_control_misses
    python perf_artifacts/model_size_bench.py            # loads medium + small on CUDA; ~2 min

The first two read only `~/.samsara/logs/samsara.log(.1)`. Nothing in the app was changed to measure.

## 1. Stage budget: "End" said -> text on screen (21 real one-word End commits, toggle DICTATE lane, medium, CUDA fp16)

| Stage | median ms | p95 ms |
|---|---|---|
| **VAD/endpoint wait** (silence after the word; `command_mode.dictate_utterance_silence_s` 0.65 s, 100 ms frames) | **670** | **768** |
| buffer assembly + wait for the Whisper lock (a streaming preview decode in flight) | 1 | 292 |
| Whisper decode of "End." | 144 | 339 |
| speech gate | 2 | 3 |
| commit re-decode of the whole staged audio (`_dictate_commit_redecode`) | 364 | 964 (max 2328, 89.6 s of audio) |
| formatting + clipboard save/copy + 50 ms settle + Ctrl+V + 100 ms pyautogui pause + 139 ms restore delay | 318 | 336 |
| **speech offset -> text on screen** (Ctrl+V; [PASTE] is logged 239 ms later) | **1302** | **1938** |

Dominant stage: the endpoint wait. The commit re-decode is second. Decoding "End" itself is ~11%.

Endpoint wait over 313 short staged utterances: median 702, p95 837.
Decode by audio length (678 DICTATE utterances): <=2 s 157/305, 2-5 s 196/355, 5-10 s 271/558, >10 s 487/1175.
Worst short decode seen: 6.0 s for "Submit" (14:47:54), after Whisper's temperature-fallback ladder ran (`prompt_reset_on_temperature`, 7 times in these logs).

## 2. medium vs small on the owner's recordings (`model_size_bench.json`)

Lane params: en, beam 5, no VAD filter, condition_on_previous_text, no prompt. Interleaved per clip, median of 3.

| | medium | small |
|---|---|---|
| decode, clips <= 3 s (n=19) | 150 / p95 191 ms | 78 / p95 100 ms |
| decode, 3-10 s (n=40) | 185 / 276 ms | 100 / 155 ms |
| decode, > 10 s (n=14) | 385 / 731 ms | 216 / 305 ms |
| control words, scripted (over/cancel/done, n=17) | 17/17 | 17/17 |
| scripted phrases exact (n=43, 26 are "Jarvis dictate") | 35/43 | 39/43 |
| free speech (30 hold-lane clips, no ground truth) | reference | 7.7% of words differ |

Reading the disagreements: medium is better on the owner's vocabulary ("Codex" vs "codecs", "queue" vs "Q", "trazodon" vs "trisodone"); small is better on a few function words and hallucinated "You" on a silent clip.

## 3. Misses (`session_control_misses.json`, checked by hand)

- end: 21 recognised. 2 incidents, 6 missed attempts: 22:37:12 trailing ". End." staged (the build running then predated the trailing-commit rule; the current tree commits it); 14:47:30-46 five "end" -> "nd", never recognised, owner gave up and said "Submit" (the "nd" went into the pasted text).
- scratch that: 7 recognised, 0 misses.
- cancel/abort: 0 misses; 1 FALSE fire: 15:12:29 "Have it stop listening to you, or something like that." aborted and discarded a 477-char draft, re-dictated at 15:12:51.
- Related: "end quotes" heard as "In quotes." 9 times.
