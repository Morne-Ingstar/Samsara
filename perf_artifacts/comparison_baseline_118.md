# Samsara measured baseline (queue 118)

Measured 2026-09-16 07:58 UTC on Windows (build 10.0.26200), NVIDIA GeForce RTX 3080, Python 3.11.15. Samsara was running throughout and shared the GPU.

Every number below was produced by one of the commands named in its section. Nothing here is derived from another number. Where a figure could not be produced honestly the cell says so, and the whole page regenerates with:

```
python tools/wer_bench_118.py --all --n 120
python tools/e2e_latency_bench_118.py --lane hotkey    --trials 32
python tools/e2e_latency_bench_118.py --lane handsfree --trials 32
python tools/comparison_report_118.py
```

## The three headline numbers

| | Measured | One-line caveat |
|---|---|---|
| Latency, hold-to-dictate (last word -> text on screen) | p50 651 ms, p95 1923 ms | over half of it is the owner's own key-release delay; the app owns 351 ms of it |
| Latency, hands-free | p50 956 ms, p95 1095 ms | 650 ms of it is the silence gap the endpoint waits for; complete utterances only |
| WER of the first-run default (`base`) | 3.61% normalised | clean read speech, not this microphone in this room; formatting-sensitive WER is not measurable on this corpus |

## What the defaults actually are

| Setting | First run | This machine today |
|---|---|---|
| model_size | `base` (config_schema.py) | `medium` |
| device | `auto` -> CUDA when present (dictation.py:3512, :5587); config_schema.py still says `cpu` | `cuda` |
| compute_type | `float16` on CUDA, `int8` on CPU (dictation.py:5601) | `float16` |
| performance_mode | `balanced` (beam 3, no conditioning) | `accurate` |
| language | `en` | `auto` |

The schema's `device: cpu` and the app's own `device: auto` disagree; the app's default wins at runtime, so a first run on this machine gets `base` on the GPU.

## 1. End-to-end latency: last word spoken -> text in the target window

```
python tools/e2e_latency_bench_118.py --lane hotkey    --trials 32 --profile default
python tools/e2e_latency_bench_118.py --lane handsfree --trials 32 --profile default
```

| Lane | n | p50 | p95 | max | Caveat |
|---|---|---|---|---|---|
| hotkey | 29 | 651 ms | 1923 ms | 2658 ms | includes the owner's own key-release delay (below); the app cannot start before the key is released |
| handsfree | 23 | 956 ms | 1095 ms | 1121 ms | complete utterances only; the endpoint gap is the largest single component, and the split captures below are excluded |

Stage breakdown (same runs, median):

| Lane | end of speech -> end of capture | decode | paste -> visible | app-owned total (capture end -> visible) |
|---|---|---|---|---|
| hotkey | 300 ms | 239 ms | 94 ms | 351 ms |
| handsfree | 651 ms | 226 ms | 78 ms | 306 ms |

**6 of 29 hands-free trials never got the whole utterance.** The endpoint fired during a pause, so the capture was cut mid-sentence and only the first fragment was decoded (one 7.1 s clip came back as "That's..."). That is the `command_mode.dictate_utterance_silence_s` gap of 0.65 s, read from ~/.samsara/config.json command_mode.dictate_utterance_silence_s. Those trials are excluded from the latency figures above: a fast fragment is not a fast dictation.

The hold-to-dictate gap is the human, not the software: across 29 of the owner's own captures the delay between his last word and his releasing the key was p50 300 ms, p95 1540 ms, max 2200 ms. That is why the hotkey p95 is high while the app-owned part is small.

## 2. WER of the shipping default, and WER with formatting taken out

```
python tools/wer_bench_118.py --all --n 120
```

Corpus: LibriSpeech test-clean (openslr.org/resources/12, CC BY 4.0), 120 utterances, 854.2 s of audio, 2308 reference words, clip length 1.49-23.06 s (median 5.73 s).

| Model | Device | WER (normalised) | WER (raw) | Decode p50 | Real-time factor |
|---|---|---|---|---|---|
| base (first-run default) | cuda (float16) | 3.61% | see caveat | 66 ms | 0.0116 |
| small | cuda (float16) | 3.05% | see caveat | 124 ms | 0.0214 |
| medium | cuda (float16) | 2.19% | see caveat | 246 ms | 0.0439 |
| base | cpu (int8) | 3.78% | see caveat | 554 ms | 0.0927 |

**Raw WER is not reportable on this corpus.** LibriSpeech references are upper case with no punctuation, so comparing them to Whisper's cased, punctuated output scores ~98% whatever the model does. That number measures the corpus's transcription convention, not the product, and must not be published. The raw column is in the JSON for completeness. A raw figure needs a reference that carries the formatting a user expects on screen, and no such corpus exists here yet -- see "What is still missing".

Normalisation is Whisper's own `EnglishTextNormalizer` (lower case, punctuation, "twenty one"/"21", "Mr."/"mister", colour/color) -- the normaliser published WERs use. A plainer lower-case-and-strip-punctuation pass is also in the JSON as `wer_light`; it is about 0.5 points worse (base: 4.12% against 3.61%), and the whole of that difference is number and title formatting.

### The 18.0% `medium` result from queue 80

Corpus: the same 43 wake-lane clips queue 80 used (115.8 s total, 1.2-4.1 s each, 91 reference words), decoded with queue 80's own parameters.

| Model | WER (normalised, corpus) | WER (mean of clips, what queue 80 published) |
|---|---|---|
| base | 7.69% | 8.14% |
| small | 8.79% | 9.30% |
| medium | 16.48% | 17.44% |

**The brief's hypothesis is wrong, and the finding is worse than formatting noise.** Queue 80's WER was already case- and punctuation-insensitive, so normalising changes almost nothing. Two real causes:

1. These clips are 1-4 s of scripted speech averaging about two words, so one wrong word is a 50-250% clip WER and the mean of clips explodes. The corpus-level figure (total errors over total words) is the honest one.
2. `medium` really does mis-hear this phrase: "jarvis dictate" comes back as "Jarvis Stick Tape" in five of the 43 clips, where `base` and `small` get it right.

On an independent corpus the ranking is the opposite and the usual way round: `medium` is the most accurate model (2.19% against base's 3.61%). So "medium is worse than small" is true only of this 43-clip wake-phrase set and must not be published as an accuracy claim.

## 3. Long-form WER

```
python tools/wer_bench_118.py --corpus libri-long --models base small --long-minutes 5
```

Corpus: 2 chapters of LibriSpeech test-clean read speech, consecutive utterances concatenated into continuous audio of 301.9-308.9 s (610.8 s total, 1656 reference words).

| Model | Params | WER (normalised) | Words returned vs reference | Decode p50 |
|---|---|---|---|---|
| base | balanced | 7.95% | 94% | 2372 ms |
| base | accurate | 2.41% | 99% | 4480 ms |
| small | balanced | 34.88% | 66% | 3224 ms |
| small | accurate | 1.63% | 99% | 6999 ms |
| medium | balanced | 26.81% | 74% | 6871 ms |
| medium | accurate | 1.14% | 99% | 11954 ms |

Caveat: continuous READ speech, not natural dictation with its restarts, fillers and pauses, and not this microphone in this room. It is a floor for long-form accuracy, not a promise.

### The shipped decode parameters lose long-form speech

This is the finding that matters most on this page, and it is not about model size. On five-minute audio the default `balanced` parameters return only part of what was said -- the "words returned" column above is the count of words the decoder emitted against the reference. `accurate` returns effectively all of it and the error rate collapses:

* `small`: 34.88% with `balanced`, 1.63% with `accurate` -- the same model and the same audio.
* `balanced` sets `condition_on_previous_text=False` and `without_timestamps=True`; with `vad_filter` and the no-speech and log-probability thresholds, whole windows are dropped with no context to recover them. `accurate` keeps the context and keeps the words.
* The cost of `accurate` is decode time: roughly double.

Short utterances hide this completely -- on the 120 short clips `balanced` and `accurate` score the same. Anyone dictating continuously on the shipped default is losing text silently. This machine is set to `accurate`, which is why it has not been noticed here.

## 4. The cost of `language: auto`

```
python tools/wer_bench_118.py --corpus libri-short --models base medium --languages en auto
python tools/wer_bench_118.py --corpus owner43 --models base medium --languages en auto --params queue80
```

| Corpus | Model | WER en | WER auto | Decode p50 en | Decode p50 auto | Added per utterance |
|---|---|---|---|---|---|---|
| libri-short | base | 3.61% | 3.61% | 66 ms | 71 ms | +5 ms |
| libri-short | medium | 2.19% | 2.19% | 246 ms | 315 ms | +69 ms |
| owner43 | base | 7.69% | 8.79% | 52 ms | 61 ms | +9 ms |
| owner43 | medium | 16.48% | 16.48% | 157 ms | 234 ms | +77 ms |

On clean English the choice costs time and changes nothing else. The risk is what it does to SHORT utterances, where there is little audio to judge from. Replaying the app's own `LanguageConfidenceGate` over these decodes (floor 0.90, samsara/languages.py):

* With `language: en` the language is never in doubt -- Whisper is told what to decode, so `info.language` comes back `en` and the gate's language branch cannot fire. 0 of 43 and 0 of 120 rejected.
* With `language: auto` and `base`, one of the 43 short clips ("cancel") was detected as Chinese and decoded into CJK characters. The gate threw the text away and played the refusal sound: the user says a word and gets nothing.
* That detection came back with probability **0.914 -- above the 0.90 floor**, so the confidence test passed it. Only the script test caught it. A confident misdetection into another LATIN-script language would pass both tests and be typed out as gibberish.

This machine is set to `language: auto` today, so it is paying the per-utterance cost above and carrying that rejection risk. Reported, not changed.

## What the latency harness cannot see

* It is the PIPELINE, not the running process. The real-time feed, the app's VAD call, its endpoint rule, its clipboard paste path and a polled target window are all real, but Samsara's own threading, queueing and Qt work between capture and paste are not. The shipping app is this plus its orchestration, so treat these as a floor.
* The hotkey hook itself (key down -> capture starts) is not included.
* Microphone and driver input latency are not included: the audio comes from the owner's own recorded captures, not the sound card.
* The target is a plain Win32 EDIT control, which renders faster than a real editor. A real application would add its own render time.
* The GPU was shared with the running app throughout; decode times include whatever contention existed.
* VAD end-of-speech detection IS included for hands-free (it is the endpoint gap, the largest component) and does not apply to hotkey, where the key release ends the capture.

## What is still missing

| Number | Status |
|---|---|
| Raw (formatting-sensitive) WER | **not measured** -- needs a reference with real casing and punctuation. LibriSpeech has neither, and the owner's 43 clips carry a lower-case truth. |
| WER on the owner's own voice, ranked | **not measured** -- the only corpus of his speech has a truth derived from the app's own live decode, so it cannot rank models. It needs perhaps 20 minutes of his speech with a transcript he has confirmed. |
| Long-form WER on natural dictation | **not measured** -- the long-form figures are read speech. Natural dictation has restarts and fillers. |
| End-to-end latency inside the running app | **not measured** -- see the blind spots above; it needs a timestamp the app does not log today. |
| Wispr Flow's numbers on this machine | **not measured** -- not installed, and their published figures are on their own corpus. |

`~/.samsara/benchmark/samples.jsonl` holds 200 recordings and zero confirmed transcripts, so `tools/benchmark_eval.py` has nothing to score. Confirming a couple of hundred of those in Benchmark Review would close the second and third gaps at once.

