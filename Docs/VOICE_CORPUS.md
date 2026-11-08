# Voice corpus

Double-click **Record voice corpus.cmd**, then hold **Space**, read the large
line, and release. The recorder saves the ending and moves to the next line.
Hold the same key and say **again**, **skip**, or **pause session** to control
the recorder. A press resumes a paused session. Closing and reopening resumes
at the first unfinished line; an earlier take is retained when you redo it.
The three buttons provide the same controls. Mouse 5 also works while this
window has focus. Space is recommended when the everyday app uses Mouse 5:
the recorder does not intercept or suppress another app's global hotkey.

## Setup by the runner

The launcher lives at `tools/voice_corpus/Record voice corpus.cmd`. Run
`tools/voice_corpus/Install recorder shortcut.ps1` to create a Desktop CMD
that calls this file (copying the original file changes its relative working
directory). The installer preserves existing launchers. The launcher uses
`SAMSARA_CORPUS_PYTHON` when set (an interpreter path without surrounding quotes),
then pythonw from an active virtualenv or Conda environment, then `pythonw.exe`
on PATH. Set the override to a Python environment with the recorder dependencies
installed when launching from the Desktop without an active environment.
No application is launched by setup or replay. The corpus uses
`D:\SamsaraCorpus` by default; set `SAMSARA_VOICE_ROOT` to use another folder
outside all repositories. No upload, cloud
transcription, source-control operation, or driver installation is performed.

The recorder uses a locally cached faster-whisper base model for the three
control phrases. A missing model produces a visible setup error, without a
download. A runner can place a minimal `recorder-settings.json` in the corpus
folder, or supply `record --settings <offline-snapshot.json>`:

```json
{
  "microphone_name": "Exact microphone name copied from an approved snapshot",
  "model_path": "D:/Models/faster-whisper-base"
}
```

The tool never discovers, reads or changes the owner's live profiles. A
snapshot can also supply the production `microphone` device index; the stable
name is preferred. Missing devices fail rather than silently using another
input. Without a snapshot, the system default input is explicitly labelled
**Daily match unverified**. The recorder never changes system gain or applies
automatic gain/normalization. The owner or runner must verify the device
against an approved offline Daily settings snapshot before treating the
corpus as evidence of Daily's microphone. No software gain exists in the
production capture engine; system input gain remains in effect.

For a capture-path comparison the runner can use:

```powershell
python -m tools.voice_corpus record --capture-path daily --settings <offline-snapshot.json> --corpus D:\SamsaraCorpus\daily_path
```

`--capture-path daily` requires an exact `microphone_name`. It matches that name
and `microphone_hostapi` (default `Windows WASAPI`) to one current input index;
missing or ambiguous matches fail, without opening another input. A stale saved
index cannot select another microphone. The existing default capture path stays
at 16 kHz. Daily capture opens mono float32 at the endpoint's default sample rate
in 100 ms blocks, then calls the production engine callback for its block-local
polyphase resampling and PCM16 clipping/scaling. No second PCM conversion occurs.
An optional snapshot `_capture_rate` follows the engine's recorded-index rule.
This matches the code and offline endpoint identity; it does not inspect or
verify the currently running Daily stream, Windows enhancements or interface gain.

The console shows the actual device index, host API, input
rate, block size and output format. Each saved sidecar retains that receipt in
`capture_status` and saves the device index. The window continues to label the
running Daily match as unverified. Production logging is isolated under
the corpus's `recorder-profile` folder. No gain, AGC, normalization, playback
ducking or reference stream is started. Preduck is an optional production
pre-roll selector, not input gain. PAD is a physical interface control.
For the comparison, record sitting 1 (15 lines), then pause before sitting 2;
use Space while Daily has Mouse 5. Keep interface gain, distance and room fixed.
Separate-session SNR estimates alone cannot attribute a difference to WASAPI.

The 60 lines are grouped into four sittings of 15. Most are short; two are
long holds with pauses. Allow roughly 20–25 minutes including reading,
retries and breaks; actual voiced material is considerably shorter. Source:
spoken scenarios in the agreed 0.42.0 stress packet, with pause, quiet-tail,
repetition, names/numbers, punctuation, controls, short replies and media
conditions. The script marks prose, commands and replies. It does not claim
to reproduce game load, changing focus, wake-word detection or player effects.
Background conditions use already-playing sound or are skipped.

## Storage and crash recovery

`takes/<opaque-id>.wav` is 16 kHz mono PCM16, matching Kept dictations' audio
layout. Its JSON contains the fixed spoken and expected rendered references,
tags, script hash, device name/index, UTC time, duration, release offset,
peak/RMS and audio hash. The 400 ms rolling lead-in and 1.2 s maximum tail are
retained, including quick taps. Replay makes the production tail decision.
There is no decoder-generated truth and no recorded control transcript.

A WAV is replaced atomically before its sidecar. A completed sidecar recovers
the selected take if a crash precedes the session update. Old redos remain
available rather than being deleted. An OS lock prevents two recording
windows from writing the same session. A changed script requires a new folder.
Keep the corpus out of sync/upload folders; the real microphone entry point
rejects paths inside any repository. Live-profile paths and this source
checkout are rejected for all data, with additional `.gitignore` exclusions.

## Replay and scoring

Run in the checkout for the build being assessed. Supply that build's commit
identity and explicit local model; neither the live app nor its configuration
is consulted. Use a new output filename each time:

```powershell
python -m tools.voice_corpus replay --model D:\Models\faster-whisper-base --build-id <full-commit> --output D:\SamsaraCorpus\runs\run.json
python -m tools.voice_corpus score D:\SamsaraCorpus\runs\run.json
python -m tools.voice_corpus compare <older-baseline.json> <newer-baseline.json>
```

Optional `replay --settings <offline-settings.json>` accepts only model_size, language,
performance_mode, cleanup_mode, add_trailing_space, format_numbers,
auto_capitalize, enable_case_formatters, formatting_tokens, dictation, and
initial_prompt. Omitted keys use the chosen checkout's defaults. The model
directory is loaded offline; model/tokenizer hashes, settings identity and
source fingerprint accompany each run. Streaming smart corrections and
personal dictionaries are deliberately excluded; choose that same scope for
both builds. This harness exercises the production pure number/case
transforms through a small facade, then production cleanup and formatting.

Mode A uses the real FrameBus, DictationSessionConsumer, streaming previews,
release-tail waiter, StreamingWorker final decoder/guards, cleanup/formatting,
and StreamingSession final delivery to a fake paste receipt. Audio time is
accelerated until release; tail time is real. PCM preservation and frame loss
are checked. Reported latency is release through fake text delivery, including
the real tail wait, and is not a desktop/OS performance promise. Silent or
failed takes have a completion latency, not successful insertion latency.

Command/reply lines also get a separate production **text routing probe**:
session controls use a seeded harmless draft, yes/no/cancel use the real
reply classifier, and public media/edit commands use the real CommandMatcher
with repository catalogue metadata and inert handlers. No command effects
run. These probes do not establish hands-free audio segmentation, pending
question lifetimes, media argument execution or wake-word integration. Held
`keep that` additionally exercises the actual held control branch. The
catalogue must be regenerated when command definitions change.

Results persist numbers, opaque take IDs and route IDs, never recognition
text or audio. `score` writes a dated Markdown summary (plain language first),
per-condition metrics and a JSON baseline named for the supplied commit.
The scorer's `measure(reference, hypothesis, tags=())` returns normalised
Levenshtein WER as `wer` and the historical case/punctuation-tolerant WER in
`strict`. Normalisation folds case, punctuation, whitespace and hyphens, splits
joined letters/digits (`test4`), expands English integers/decimals and maps
ordinals to cardinals (`4`, `four`, `fourth`). Common spoken punctuation is
removed on both sides. Lines tagged `punctuation` retain historical strict
scoring, so punctuation rendering regressions remain measurable. Exact rendered
text equality remains separate. Empty-reference WER is the insertion count.

`off_script` means multiset token overlap covers less than 50% of the
normalised reference (exactly 50% remains included). This is a heuristic flag,
not proof of a recording mistake: severe recognition failures and empty
hypotheses can also trigger it. Empty references are never flagged. Flagged
takes are listed separately and excluded from both WER columns, overall and
per tag. Routing/decode failure counts and timing still include every take.
`scored_takes` and `off_script_takes` expose the denominator change; strict
and normalised WER each use their own reference-token denominator.

Replay passes each take's tags into scoring. The dated Markdown baseline shows
scored/excluded take counts, WER and strict WER for each condition, followed by
an off-script exclusion list with take ID, line ID and reference-token overlap.
Its summary identifies legacy/mixed runs as not fully normalised.
Numeric-only legacy runs
cannot recover formatting or token overlap without their original text; they
must not be presented as newly normalised results.
Missing starts/ends count reference words before/after the first/last exact
match, including substitutions. Doubles count excess adjacent repeated token
or phrase runs (up to eight words), capped by insertion count; intentional
reference repeats are subtracted. Exact rendered-text equality is separate.
Per-tag WER is a micro-average, not a mean of clip percentages.

`compare` rejects changed audio, references, selections or model/settings;
it lists increased WER, insertions, missing starts/ends, doubles, routing or
decode failures and latency increases above 100 ms (adjust with
`--latency-ms`). Exit codes: 0 completed/no regression; 1 regressions; 2
setup/harness error; 3 missing VB-CABLE. Recognition errors are measurements,
so a successful replay does not by itself mean the build passes acceptance.

## Optional virtual microphone

`python -m tools.voice_corpus devices` lists VB-CABLE endpoints without
opening a stream. If absent: **needs VB-CABLE (owner one-time install)**.
Mode A works independently. Nothing installs a driver automatically.

For mode B, the runner separately prepares a TEST-profile application,
selects **CABLE Output** as its microphone and arms its test recording.
`python -m tools.voice_corpus virtual-play <take.wav> --device <CABLE-Input-id>`
plays only to an explicitly selected **CABLE Input** playback endpoint. It
never uses the default speaker, changes a device, launches an application,
presses a key or collects target text. This is a transport for a separately
observed end-to-end test, not automated end-to-end scoring. Do not connect
it to the everyday profile. Driver and actual TEST-profile observation are
runner/owner tasks.

## Focused tests and synthetic validation

`pytest tests/test_voice_corpus_1181.py` covers fake audio/session persistence,
schema, scoring, routing and real hold capture/decode/delivery with a
deterministic fake decoder. Normal tests never open a microphone or read a
real corpus. The optional `voice_corpus` check is skipped unless a corpus and
local model exist **and** `SAMSARA_VOICE_CORPUS=1` is explicitly set; it always
skips under CI. Set `SAMSARA_VOICE_MODEL` to the local model and optionally
`SAMSARA_VOICE_ROOT` to a different private corpus folder.

For a native decoder smoke check without the owner's voice:

```powershell
python -m tools.voice_corpus.synthetic --output D:\SyntheticVoiceCorpus --tts-exe "C:\Program Files\eSpeak NG\espeak-ng.exe"
python -m tools.voice_corpus replay --corpus D:\SyntheticVoiceCorpus --model D:\Models\faster-whisper-base --build-id synthetic-smoke --output D:\SyntheticVoiceCorpus\runs\smoke.json
python -m tools.voice_corpus score D:\SyntheticVoiceCorpus\runs\smoke.json
```

Synthetic speech checks wiring and repeatability. It does not establish
recognition accuracy for the owner, quiet endings, room noise or hardware.

## Prior art

The existing `tools/probes/hf_corpus_record.py` supplies the rolling buffer.
Kept dictations supplies the opaque WAV/JSON layout; its profile-coupled
transcript store is intentionally not used for references. The production
hold benchmark and the native capture evidence from 1168 supply the replay
approach. [Whisper tests](https://github.com/openai/whisper/tree/main/tests)
use fixed audio and expected text; [JiWER](https://jitsi.github.io/jiwer/usage/)
documents edit alignments. The small equivalent scorer avoids a new runtime
dependency. [sounddevice](https://python-sounddevice.readthedocs.io/en/0.5.3/api/streams.html)
documents explicit device streams; [VB-Audio](https://vb-audio.com/Cable/)
documents the Input playback to Output capture direction.
