# Hands-free corpus: capture and admission

The 1259 inventory found **zero continuous real hands-free sessions paired
with delivered draft events**. Existing holds and the guided recorder are
useful source audio; neither establishes streaming replay fidelity.
The four-build scoreboard, production replay driver and no-rise gate remain
blocked at the brief's fidelity requirement. No accuracy result or PASS is
available from these preparation tools.

## Collecting the missing evidence

The runner can enable `diagnostics.handsfree_session_capture: true` inside
the private profile's existing `diagnostics` object. The process must already
have an explicit `SAMSARA_HOME_DIR` whose resolved directory name is
`.samsara-private`. The switch defaults to off by absence and is not a Settings
control. It was not enabled against an owner profile during 1259.

During a toggle hands-free session, the existing WakeConsumer observes its
frames, including room gaps. A bounded asynchronous writer saves UUID-named
16 kHz mono PCM16 WAV, JSONL events and a JSON completion manifest under
`<private-profile>/handsfree_sessions/`. The journal contains capture times,
frame sequence/epoch, sample offsets, queued chunk sizes/endpoints, sampled
draft revisions and dispatch/delivery text. Dispatch events observe actual
results. Asynchronous draft changes are sampled on the following observation
tick; their timestamps must not be treated as exact UI change times.

Limits are 30 minutes per session, 16 MiB of events per session, 256 MiB total,
and seven days of age. Retention runs when a recording starts or finishes.
It touches only this recorder's UUID-named WAV/JSONL/JSON files, including
orphaned partials. Queue overflow, frame discontinuity, duration/event limits
and an end with a pending decode invalidate a session. Disk failure leaves
no completed manifest. Diagnostics return no decision to capture or dispatch.

Completion is **not** fidelity admission. The source hashes and whitelisted
settings aid diagnosis; they are not a complete imported-code/configuration
snapshot. First-onset ring pre-roll can precede the first recorded frame.
Capture-time dictionary, model, settings, pre-roll coverage and event coverage
must be established before replay can claim historical reproduction. Every
manifest therefore explicitly has `fidelity_ready: false`.

## Preparation CLI

Run from an explicit source checkout. Output must be outside that checkout
and live profiles. No command opens a microphone, starts an app or downloads
a model.

```powershell
python -m tools.voice_corpus.handsfree_replay inventory --root C:\Example\Corpus --root C:\Example\.samsara-private --output <report-folder>\inventory.json
python -m tools.voice_corpus.handsfree_replay preflight --root C:\Example\Corpus --root C:\Example\.samsara-private --output <report-folder>\fidelity-preflight.json
python -m tools.voice_corpus.handsfree_replay derive --takes C:\Example\Corpus\takes --output <report-folder>\DERIVED_recipes.json
```

`preflight` always exits **2 / BLOCKED**. It never turns a file count into
fidelity, including when ten paired captures exist. Pair admission requires
matching WAV/manifest/frame sample counts and a nonempty delivery event.
The next work is a genuine production-path replay and its measured fidelity
check, not bypassing this stop. `handsfree_gate` is intentionally not supplied
after the required stop before scoring.

## DERIVED challenges

The builder takes consecutive *eligible* real guided prose takes in timestamp
order. Eligibility uses the recorder's `synthetic: false` provenance and
single-sentence script reference. This does not prove verbatim delivery.
It uses Silero VAD to locate an internal gap in at least one take of each
pair, then replaces that gap with 0.4 / 0.8 / 1.5 / 3 seconds of measured,
nonzero, VAD-negative room tone. An additional 0.8-second gap separates the
two takes and is labelled separately from the internal pauses.

Sources without a suitable internal gap are never cut arbitrarily through
speech. No generated silence substitutes for missing room tone. VAD is an
acoustic proxy: neither word boundaries nor semantic mid-sentence placement
have been human verified. Gap variants receive only short/long pause tags;
fast-speech and quiet-tail tags are not guessed.

The dataset is a virtual manifest with original WAV hashes, sample ranges,
room-tone source/range and exact gap lengths. `render_derived(row)` verifies
those hashes and renders PCM in memory, preserving speech samples. No owner
audio or transcription is copied into the repository or report artifacts.
All recipes and their measurements say **DERIVED**. Their numbers must never
be combined with real sessions or used for the ten-session fidelity check.

## Resume condition

The requested driver must invoke the selected build's actual frame/pause
path, `_handle_command_mode_utterance`, seam joining, open-span scheduling,
re-decode and commit. Replaying already decoded text through SessionModeManager
alone, or using the existing hold StreamingWorker replay, cannot prove this.
Simulated time must preserve frame, inference/FIFO and timer ordering.
Validate at least ten real sessions on their producing builds; report exact
delivered-text matches. Below 80%, stop before scoring. Only after that check
may large-v3 proxy scores, tag-specific seam annotations, the four-build
comparison and a measured no-rise gate be produced.
