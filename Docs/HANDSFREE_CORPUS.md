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

During a toggle hands-free session, a bounded asynchronous writer saves the
listener's input, including room gaps, under
`<private-profile>/handsfree_sessions/`. Schema 2 has a session UUID, a
`<uuid>.json` manifest and numbered `<uuid>-p000001` parts. Each part contains
a 16 kHz mono PCM16 WAV and a segment of the session's shared journal
(`.frames` for packed frame rows, `.jsonl` for events). The writer closes
each part at 30 minutes of PCM or about 2 MiB of events, then continues in
the next part. Part boundaries are half-open session sample offsets.

The packed little-endian format `<QqQQIIffffBB` uses 58 bytes per 100 ms
frame, or 2,088,000 bytes per hour. Fields are ring sequence, capture clock
nanoseconds, observation clock nanoseconds, session sample offset, sample
count, device epoch, RMS, peak probability from the actual live VAD call,
selected speech threshold, silence threshold in seconds, state, and a flag
for live VAD use. State codes 0–5 mean idle, onset, in utterance, suppressed
by hold, suppressed by TTS, and skipped by another early guard. NaN marks
values the listener did not compute. The 1286 silence-close rescore is
recorded separately.

Events include entering and leaving hands-free, lane changes, sampled draft
revisions and character counts, parked draft length, hold and duck edges
visible to the listener, mic epoch changes, and discontinuities. The manifest
pairs the monotonic clock with wall-clock time. A chunk ID joins queued
audio to the speech-presence verdict, decode drop reason or dispatch result.
Each ordered, half-open ring span is `[device_epoch, start_sample, end_sample]`, with
`start_sample = seq * 1600 + offset_within_frame`. This includes the actual
pre-roll, trimmed slices, and silence tail. A long fragmented clip may append
`chunk_spans` events with the same chunk ID and a zero-based `first_span`.
First-onset pre-roll can reference frames older than the session WAV. A
nonzero `unmapped_samples` means the
observer attached after capture began or some provenance is unavailable.
Dispatch rows save word and character counts only, including counts for
text staged to a draft. The journal contains no transcript or draft text.

The recorder-owned files have a 1 GiB budget, about 9 hours of PCM plus frame
rows before sparse events. This is under 7% of a 15 GiB free drive. Ordinary
parts expire after 48 hours. Parts from sessions with a parked draft or a
short reject expire after 7 days. Short rejects include a presence rejection,
empty decode or sign-off drop at up to 1.5 seconds. Size pressure removes oldest ordinary parts first, then
oldest flagged parts. An open part is reserved and excluded from deletion.
A session can run beyond the retained window as older parts are removed.
Queue overflow, frame discontinuities, pruned history and unresolved queue
results mark the recording incomplete. Results already in the decode FIFO
can arrive for up to 120 seconds after session exit. Disk errors terminate
the writer and emit one warning without affecting the listener.

Completion is **not** fidelity admission. The source hashes and whitelisted
settings aid diagnosis; they are not a complete imported-code/configuration
snapshot. Capture-time dictionary, model, settings, pre-roll coverage and
event coverage must be established before replay can claim historical
reproduction. Every manifest has `fidelity_ready: false`. The original
inventory's exact-text admission applies to legacy recordings. Schema 2
provides numeric evidence for diagnosis.

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
