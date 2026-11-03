# Held dictation bench fixtures (1052c)

These are real human recordings, never TTS or repeated sentences. The room recording is longer than every fixture; no noise looping is needed. All source assets are below 20 MB, and the eight materialized WAVs are
also below 20 MB. WAVs are deterministically materialized under `--output/fixtures`;
the repository keeps losslessly encoded source samples as ASCII base64 so the canonical
text-patch handover can reconstruct and verify every byte. `sources.json`
contains decoded source SHA-256 values and independent corpus transcripts.
The benchmark never downloads assets or models.

## Source and licence

- Speech: LibriSpeech, speaker 1272 (human LibriVox reader), chapter 128104,
  recordings 0000..0007 and 0014. Original corpus: <https://www.openslr.org/12/>,
  CC BY 4.0, compiled by Vassil Panayotov with Daniel Povey; Panayotov, Chen,
  Povey and Khudanpur, *LibriSpeech: an ASR corpus based on public domain audio
  books*, ICASSP 2015. Redistribution/attribution licence:
  <https://creativecommons.org/licenses/by/4.0/>.
  Exact fetched subset (9,192,059-byte parquet, 73 original utterances):
  <https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy/resolve/main/clean/validation-00000-of-00001.parquet>.
  Only the nine listed utterances are included here, in their original
  lossless encoding. The reference text is the corpus text, never a bench
  transcription. All processing is described below; no author endorsement.
- Room noise: Soapuel, *Room Tone.wav* (2019-10-22), Freesound sample
  489443, explicitly CC0 1.0 on its source page:
  <https://freesound.org/people/Soapuel/sounds/489443/>.
  Exact fetched public audio preview:
  <https://cdn.freesound.org/previews/489/489443_10448599-lq.ogg>.
  Licence: <https://creativecommons.org/publicdomain/zero/1.0/>.
  The 130.671-second actual room recording is decoded from the public Ogg
  preview, mixed to mono and resampled to 16 kHz, then stored as PCM16 FLAC.
  This is recorded indoor room tone, not a measured impulse response.
  The source page and original download are preserved in baseline evidence.
- Wake-ready cue: this repository's `sounds/start.wav`, loaded by
  `samsara/cue_assets.py`, with the repository licence. This is the actual
  legacy wake-start cue in master, not a newly synthesized approximation.
  Its original-byte checksum is pinned in `sources.json`.

## Recipes and calibration

All output is mono 16 kHz PCM16. Resampling uses scipy, with samples and all
parameters pinned in the script. Peak levels describe float construction;
PCM16 quantization changes them by at most one step. `peak_minus12` means
peak -12 dBFS, not RMS -12 dBFS. JSON records actual peak, RMS, duration and
each WAV checksum. Noise SNR is 26 dB against the complete source speech RMS;
local SNR falls during hesitant low-level speech. Source recordings retain
their human breathing, cadence and within-utterance pauses.

- 1026: consecutive utterances 0,1 rate-adjusted from 10.670 to 12 seconds;
  800 ms thinking pause; human utterance 14, "BY HARRY QUILTER M A", at
  -26 dBFS RMS; 300 ms release tail. The name Quilter is a rare proper noun.
  Room tone is -48 dBFS under the pause/tail. Overall peak .995. A PASS
  requires the contiguous trailing words "Harry Quilter", not the earlier
  occurrence of Quilter in the introductory passage.
- 1043: consecutive, unrepeated utterances 0..4 rate-adjusted together from
  62.455 to 54.2 seconds; attenuate 27.5..33 seconds to 16% amplitude;
  insert 800 ms hesitation at 29.2 seconds inside a clause. Then append
  consecutive utterances 5..7 rate-adjusted to 20 seconds. Total 75 seconds,
  overall peak .995. The scored reference words (utterance 4, pictures..man) fall wholly
  inside 30..55 seconds. The boundary word Linnell's spans 30 seconds and is
  excluded. Source timing audit (base, word timestamps, original human clip)
  locates it at 0..1.14 s before recipe projection; its original spelling
  still comes from the independent corpus transcript. Exact score word indices
  and method are in JSON. PASS requires >=90% normalized word recall in that
  range. A separate >=90% recall check on the final 20 seconds of clear
  control speech prevents a middle-range PASS from concealing later word loss.
- 1042: 1.2 seconds of the recorded room noise, peak .07; queue a real
  MouseHook dispatcher release, stall its dispatch for 20 ms, deliver through
  HotkeyCluster and StreamingSession.finalize. PASS requires a nonblank real
  History row, retained original PCM, and delivery of the stalled release.
  No native Windows hook is installed. This is a dispatcher/capture regression,
  not proof about Windows hook scheduling under load.
- 1051: human utterance 2 plus room tone; actual wake-ready cue and a
  single-sample .995 click at 1.6 seconds. PASS requires speech, no repeated
  1..8-word phrase four times consecutively, and no sound request during a
  separate isolated invocation of the actual wake-start method while the
  fixture app is recording. The contaminated WAV remains contaminated for
  decoder robustness even if cue suppression is fixed. This separates the
  captured-audio robustness measurement from the actual cue emission check.

Every case also has the peak -12 dBFS variant (the .07 noise case is attenuated
by the same gain ratio, preserving its required nominal calibration).

## Real path and limits

WAV -> real FrameBus (100 ms blocks) -> real DictationSessionConsumer drain
thread -> real StreamingWorker partial/final entry -> native local
faster-whisper -> production speech presence, segment guards, window recovery,
cleanup, preview-tail preservation -> real StreamingSession delivery -> real
isolated in-memory SQLite History. Empty vocabulary, English, accurate profile and
standard thresholds come from the application parameter builder. No test
segments or confidence values are invented. The model tracing wrapper only
records lazy results. CPU final decode applies the application's real CPU
budget and beam bounds. `--partials` additionally exercises production partial
decodes at 13,30,55 seconds and near release, using audio time; this is a
deterministic replay, not the wall-clock GPU partial schedule. Without that
flag CPU behavior is final-only, as in the app.

The harness supplies an inert UI, no paste target, public-fixture History
destination and empty user dictionary. It does not measure microphone/device
latency, native hook installation, OS focus privacy or physical speaker echo.
The last 300 ms of each speech WAV are delivered at real time after
starting the production wait_for_streaming_release_tail policy on a separate
fixture thread. Its actual quiet threshold, speech level, elapsed time and
reason feed the final decoder guard as in the application. A tail still
containing speech can reach the real 1200 ms deadline; no extra speech is
invented or discarded. The noise-only early-release case uses the queued
release directly, with no late microphone data.

The wake probe compiles only `DictationApp.start_wake_word_mode` from AST into
an isolated namespace and calls it against inert service facades. It never
imports `dictation.py` or executes app bootstrap. Future method dependencies
that are unsupported by this facade produce ERROR, never a guessed PASS.

## Run and compare

```powershell
python tools/hold_e2e_bench.py --model LOCAL_MODEL_DIRECTORY --output baseline --partials
python tools/hold_e2e_bench.py --model LOCAL_MODEL_DIRECTORY --output candidate --partials
```

Keep model/device/compute type/fixture checksums identical. Output includes
an incremental `results.json`, per-case `result.json`, WAVs, native metrics,
parameters, timings and a readable stdout table. Exit 0 = every gate passed,
1 = measured failure, 2 = harness error. Expected production failures must
remain visible; do not change fixtures to make them pass.

`--debug-holds EXPORTED_DIRECTORY` adds each explicitly exported WAV as a
regression-only case, with no reference transcript. Optional adjacent
`name.before.txt` supplies its previous delivered text. Never point it at a
live `.samsara` directory. Public fixtures can be logged; private exported
recordings and their delivered text must remain in an appropriately approved
private output destination, not task work logs or the handover.

The slow smoke uses one short human case and the real local model when
`SAMSARA_HOLD_BENCH_MODEL` is set. With no explicit model it reports SKIP;
it never substitutes a mock or downloads anything.
