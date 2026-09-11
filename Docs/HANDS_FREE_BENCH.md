# Hands-free offline bench

The bench replays `voice_samples/hf_corpus/manifest.json` WAV rows through the
production resampler, faster-whisper model call, Silero VAD adapter, and the
production `_apply_segment_quality_gates` helper when post-gates are enabled.
It does not call `process_wake_word_buffer` end-to-end because that function
owns the live app/session/UI and would inject benchmark text. The seam is
immediately around its buffer-to-`model.transcribe` boundary; no live audio
device, running app state, or `config.json` is touched.

## Run

By default, configuration is resolved in this order:
DEFAULT_CONFIG fallback values, then the live values in
C:\Users\Morne\.samsara\config.json, then the values in the --config JSON.
An unreadable --config file silently falls back to DEFAULT_CONFIG.
--stock-config skips the live file and uses the stock defaults. The
--no-post-gates flag forces apply_post_gates to false, even if another
configuration layer enables it.

```powershell
F:\envs\sami\python.exe tools\hf_bench.py --corpus C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus --out bench\hf_current
```

The default corpus is `C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus`.
The command writes Markdown and JSON beside the requested `--out` stem.
If the manifest is absent, it reports that the real corpus is not present and
does not load a model.

The supported --config keys are:

- model_size: faster-whisper model size.
- language: transcription language.
- performance_mode: production transcription performance mode.
- initial_prompt: optional transcription prompt.
- device: faster-whisper device.
- compute_type: faster-whisper compute type.
- temperature: transcription temperature setting.
- vad_filter: whether faster-whisper VAD filtering is enabled.
- vad_prob_threshold: Silero voiced-probability threshold.
- vad_min_contig_ms: minimum contiguous voiced duration for the gate.
- rms_threshold: RMS fallback threshold when Silero is unavailable.
- no_speech_threshold: native no-speech quality threshold.
- log_prob_threshold: native average-log-probability quality threshold.
- compression_ratio_threshold: native compression-ratio quality threshold.
- apply_post_gates: whether to call the production segment-quality gates;
  the stock default is true.
- wake_targets: wake target objects; every enabled phrase is accepted for
  wake_over_media, together with wake_word.
- wake_word: transcript wake phrase, always included even when wake_targets exist.

The first line of each run reports the resolved model, performance mode,
compute type, and post-gate state.

The requested live-config runs are:

~~~powershell
# Run A: gates ON
F:\envs\sami\python.exe tools\hf_bench.py --corpus C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus --out C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_live

# Run B: gates OFF
F:\envs\sami\python.exe tools\hf_bench.py --corpus C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus --no-post-gates --out C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_nogates
~~~

Both commands use the live configuration because neither passes
--stock-config. Run A writes
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_live.md and
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_live.json. Run B writes
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_nogates.md and
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_nogates.json.

## Media gain sweep

`--media-gain G` is repeatable and takes a linear gain. When it is given, every
`media_only` row is decoded once per gain with its samples scaled by G through
the int16 grid, then mixed with an unscaled recorded room floor and rounded/clipped
to int16. It is reported under `media_only@G`; pass `--media-gain 1.0` to include
unity media gain with the added floor. `--floor-label` defaults to `silence`;
`nonspeech_room` selects the other room recordings. Floor clips cycle in manifest
order by media row, starting at sample zero and looping to media length. The same
floor is used for all gains of a media clip, and its source and RMS dBFS are
reported per row. Floor and media WAVs must have matching sample rates.

`--no-floor` reproduces the old plain-scale path, which lowers the whole clip
without changing its internal SNR. Owner and wake rows are untouched, and
omitting `--media-gain` leaves every row exactly as before. The floor mix measures
how far the PC media stream must be ducked before room noise masks transcription.
At gain zero only the floor remains. Reports include exact accepted media texts
and the largest tested gain with zero false accepts, compared with the capture
duck (`ducking.hands_free_level`, read from live config; fallback 0.15).

```powershell
F:\envs\sami\python.exe tools\hf_bench.py --out perf_artifacts\hf_bench_media_gain_floor_silence --floor-label silence --media-gain 1.0 --media-gain 0.30 --media-gain 0.15 --media-gain 0.08 --media-gain 0.04 --media-gain 0.02 --media-gain 0.0
```

`tools\probes\hf_owner_gain_probe.py --gain 0.15` is the companion sanity
bound: it decodes the `speech_owner_over_media` rows with the WHOLE WAV scaled,
owner included, which is not the live mix (ducking attenuates only the media
stream). Results of the 2026-09-10 sweep: `perf_artifacts/hf_bench_media_gain.md`.

For A/B comparison:

```powershell
F:\envs\sami\python.exe tools\hf_bench.py --corpus C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus --ab baseline.json candidate.json
```

For the two requested runs, use:
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_live.json and
C:\Users\Morne\Documents\Claude\hf_bench\hf_bench_nogates.json.
The --ab arguments are the two JSON artifacts above. The printed delta is
candidate minus baseline; negative false-accept and WER deltas are
improvements.

## Metrics and acceptance rule

- `false_accept_rate`: any non-empty output on `silence`, `nonspeech_*`, or `media_only`.
  The exact non-empty phrases are listed as the hallucination list.
- `false_reject_rate`: empty output on `speech_owner`, `speech_owner_over_media`,
  or `speech_owner_over_media_leadin`.
- `WER`: word-level Levenshtein distance divided by expected word count.
- `leading_insertions` and `trailing_insertions`: emitted words before the first
  expected word and after the last expected word.
- `contamination_words_per_utterance`: mean leading insertions for each
  owner-over-media label. `pre_roll_contamination_words_per_utterance` remains
  a legacy alias; it alone does not establish that pre-buffer audio was tested.
- `contaminated_rate`: fraction of that label's rows with at least one leading
  insertion, including zero-output rows in the denominator.
- wake_detected and command_text: wake phrase match and remaining command on
  wake_over_media rows. The accepted phrases are the union of the manifest
  row's optional wake_phrase, live wake_word, and every enabled phrase in live
  wake_targets. Phrases are normalized, deduplicated and matched as whole words.
  The match is against the decoded TRANSCRIPT and is not an
  OpenWakeWord detection. The phrase list actually used is stored in each wake
  row and in the wake_over_media summary.
- `voiced_ms`: Silero-probability voiced frames at the configured threshold,
  or the production RMS fallback when Silero cannot load.
- Segment telemetry records `no_speech_prob`, `avg_logprob`, and
  `compression_ratio` when the backend supplies them.

A gate change is accepted only when false-accept falls, `speech_owner`
false-reject does not rise by more than one item, and `speech_owner` WER does
not rise.

## Record and measure pre-roll contamination

From `C:\Users\Morne\Projects\Samsara-dev`, record just the four new items:

```powershell
F:\envs\sami\python.exe tools\probes\hf_corpus_record.py --only speech_owner_over_media_leadin --lead-in 3
```

The recorder keeps the mic stream open and retains the last three seconds
while you read prompts and make keep/redo decisions. With the TV at normal
volume, wait for a line of dialogue, then press Enter and read the displayed
sentence. Press Enter again to stop, then choose keep, redo or skip. Lead-in
takes have no countdown or beep to contaminate the buffer. Existing manifest
rows are retained; kept items are appended, and already-kept filenames are
skipped on subsequent runs. The original 26 items are unchanged; the four
new items reuse the first four `speech_owner` sentences.

Each kept WAV contains the buffered audio followed by the pressed capture.
`lead_in_s` is the requested buffer capacity; `press_offset_s` is its actual
length in the WAV, which can be shorter if you press before the buffer fills.
With the default `--lead-in 0`, new rows store both fields as `0.0`. Older
manifest rows without these fields remain valid and are treated as zero.

Run the bench normally after recording. The
`speech_owner_over_media_leadin` summary reports WER, false rejects and edge
insertions, plus `contamination_words_per_utterance` and `contaminated_rate`.
For example, leading counts of 0, 2, 0, 4 give 1.5 words per utterance and a
contaminated rate of 0.5. These counts describe transcript insertions relative
to the expected read sentence; they do not identify the speaker of each word.

For every row with `press_offset_s > 0`, the bench also decodes only the audio
from that offset onward, with identical model and gate settings. Speech rows
report `post_press_text`, `post_press_leading_insertions`, and
`pre_buffer_leading_insertions_delta` (full-WAV leading insertions minus
press-only leading insertions). Positive deltas measure the added insertion
cost of including the buffer; negative deltas are retained too. Each speech
label reports `pre_buffer_comparison_count` and
`pre_buffer_leading_insertions_delta_per_utterance`, averaged only over its
paired rows. A comparison count of zero and a null delta mean **not tested**;
a 0.0 contamination average on old press-only clips is not evidence of a
clean pre-buffer.
