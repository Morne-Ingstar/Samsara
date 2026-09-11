# Ducked-media transcription bench (media gain sweep)

Question: with the prebuffer discarded on wake (2026-09-10 session policy), the TV audio
that still reaches Whisper during a hands-free capture is the DUCKED media. At what gain
does Whisper stop producing text from it? Speaker verification is not an option
(perf_artifacts/sv_feasibility.md).

## Verdict

**Largest tested gain with media_only false-accept 0/3 is 0 (full mute); that is below the current capture duck of 0.15 -- ducking to 0.15 does not stop transcription of the TV, and neither does 0.04 (-28 dB).**

## Run

```
F:\envs\sami\python.exe tools\hf_bench.py --out perf_artifacts\hf_bench_media_gain \
  --media-gain 1.0 --media-gain 0.30 --media-gain 0.15 --media-gain 0.08 \
  --media-gain 0.04 --media-gain 0.0
F:\envs\sami\python.exe tools\probes\hf_owner_gain_probe.py --gain 0.15 \
  --out perf_artifacts\hf_owner_gain_015
```

Live config, post-gates ON: model=medium perf=accurate compute=float16 device=cuda. Corpus: voice_samples/hf_corpus (3 media_only rows, 30 s each).
Machine artifacts: `hf_bench_media_gain.json`, `hf_owner_gain_015.json`;
raw decoder logs: `_run_media_gain.log`, `_owner_probe.log`. Note that re-running
hf_bench.py with `--out perf_artifacts/hf_bench_media_gain` overwrites this file with
the tool's own short summary table.

## False accepts per gain (media_only, n=3)

| gain | approx dB | false-accept rate | rows with text |
|---|---:|---:|---:|
| 1 | 0.0 | 1.00 (3/3) | 3 |
| 0.3 | -10.5 | 1.00 (3/3) | 3 |
| 0.15 | -16.5 | 0.67 (2/3) | 2 |
| 0.08 | -21.9 | 0.67 (2/3) | 2 |
| 0.04 | -28.0 | 0.33 (1/3) | 1 |
| 0 | -inf | 0.00 (0/3) | 0 |

Per-row voiced_ms (Silero) and post-scale RMS:

| gain | file | rms dBFS | voiced_ms | text? |
|---|---|---:|---:|---|
| 1 | media_only_01.wav | -37.51 | 13824 | yes |
| 1 | media_only_02.wav | -37.31 | 23392 | yes |
| 1 | media_only_03.wav | -34.73 | 16960 | yes |
| 0.3 | media_only_01.wav | -47.96 | 4800 | yes |
| 0.3 | media_only_02.wav | -47.77 | 19296 | yes |
| 0.3 | media_only_03.wav | -45.19 | 13984 | yes |
| 0.15 | media_only_01.wav | -53.98 | 0 | no |
| 0.15 | media_only_02.wav | -53.79 | 17760 | yes |
| 0.15 | media_only_03.wav | -51.21 | 13760 | yes |
| 0.08 | media_only_01.wav | -59.44 | 0 | no |
| 0.08 | media_only_02.wav | -59.25 | 13728 | yes |
| 0.08 | media_only_03.wav | -56.67 | 11232 | yes |
| 0.04 | media_only_01.wav | -65.46 | 0 | no |
| 0.04 | media_only_02.wav | -65.27 | 0 | no |
| 0.04 | media_only_03.wav | -62.69 | 7840 | yes |
| 0 | media_only_01.wav | -inf | 0 | no |
| 0 | media_only_02.wav | -inf | 0 | no |
| 0 | media_only_03.wav | -inf | 0 | no |

## Exact text per gain

### media_only@1

- `media_only_01.wav`: Okay, do you know anybody? Yeah, but no. I'm intrigued. Oh, well, I played with this Stryker jammer who would be great great, but she quit a couple years ago to study law I object she'd never played for regiment. How about you give her a shout anyway? See what's what you know? It's worth a shot fine Yeah, but would have better luck getting plays off the street
- `media_only_02.wav`: Philstone professor of sports psychology at the Loughborough University and she's here to talk about her new book, Unlocking the Eye in Team. The psychology behind team building, Dr. Philstone. Mr. Crimm. Sharon, it's good to have you here. I'd like to start with the phrase that you returned to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now in the paperback. I have a major book. Okay, I'll send you over the signed copy.
- `media_only_03.wav`: after i heard the word ladies come spitting out of my mouth i guess that's not worse than the last one yeah man yeah i just mean i used to be able to walk into any part of this building and i don't love me and make you feel like i'm now violating the lady parts what the heck is going on with me you okay coach i don't know well thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took forever to air dry

### media_only@0.3

- `media_only_01.wav`: Well, I played with this striker, Gemma, who would be great, but she quit a couple years ago to study law. I object! Plus, she'd never... You're a shout anyways.
- `media_only_02.wav`: Dr. Philstone, professor of sports psychology at the Loughborough University, and she's here to talk about her new book, Unlocking the Eye in Team, the psychology behind team building. Dr. Philstone. Mr. Crewe. Sharon, it's good to have you here. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now with paperback. I have a major book. Okay. I'll send you home with a signed copy. out now in paperback. I have the magic book. Okay, I'll send you over the signed copy.
- `media_only_03.wav`: Yeah, man. I mean, I used to be able to walk into any part of this building, and I don't love me and make me feel like I'm now violating the lady parts. What the heck is going on with me? You okay, Coach? I don't know. Well, thank God. Sorry, Boots wanted to shower right away to wash the blood off her hands, and then she took forever to air dry. Hmm. You okay, Coach? I don't know. Oh, thank God. Sorry. Boots wanted to shower right away to wash the blood off her hands, and then she took forever to air dry.

### media_only@0.15

- `media_only_01.wav`: (empty)
- `media_only_02.wav`: University, and she's here to talk about her new book, Unlocking the Eye in Team. The psychology behind team building, Dr. Filster. Sharon, it's good to have you here. I'd like to start with a phrase that you return to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now in paperback. I have a major book. Okay, I'll send you home with a signed copy.
- `media_only_03.wav`: the last one yeah man i just mean i used to be able to walk into any part of this building and i don't love me it made me feel like i'm not violating the lady parts what the heck is going on with me you okay coach i don't know thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took forever to air dry

### media_only@0.08

- `media_only_01.wav`: (empty)
- `media_only_02.wav`: ...talk about her new book, Unlocking the Eye in Team, the psychology behind team building, Dr. Filster, Mr. Crimm, Sharon, it's good to have you here. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now in paperback. I have a magic book. Okay, I'll send you home with a signed copy.
- `media_only_03.wav`: And I don't love me, it made me feel like I'm now violating the lady parts. What the heck is going on with me? You okay, Coach? I don't know. Thank God. Sorry, Boots wanted to shower right away to wash the blood off her hands, and then she took forever to air dry.

### media_only@0.04

- `media_only_01.wav`: (empty)
- `media_only_02.wav`: (empty)
- `media_only_03.wav`: me and make me feel like I'm out. What the fuck is going on with me? You okay, Coach? I don't know. Well, thank God. Sorry. Boots wanted to shower right away to wash the blood off her hands, and then she took a bath to air dry. Hey! Congrats on the-

### media_only@0

- `media_only_01.wav`: (empty)
- `media_only_02.wav`: (empty)
- `media_only_03.wav`: (empty)

## Owner over media, whole WAV scaled by 0.15

Sanity bound, not the live mix: this attenuates the owner and the TV together, whereas
ducking attenuates only the PC media stream. It answers whether the owner's voice still
transcribes when it arrives at ducked level.

| file | rms dBFS | WER | text |
|---|---:|---:|---|
| speech_owner_over_media_01.wav | -46.27 | 0.0000 | The television is background noise while I dictate this sentence. |
| speech_owner_over_media_02.wav | -44.38 | 0.0000 | The television is background noise while I dictate this sentence. |
| speech_owner_over_media_03.wav | -45.44 | 0.0000 | The television is background noise while I dictate this sentence. |

Mean WER 0.0000, false-reject rate 0.00 over 3 rows.

## Reading

- At the current capture duck (0.15, -16.5 dB) two of three TV clips still decode into
  full, fluent sentences; the ducked-down audio sits near -52 to -54 dBFS and Silero still
  reports 13-18 s of voiced frames in a 30 s clip.
- At 0.04 (-28 dB) the loudest clip (media_only_03, -34.7 dBFS unducked) still produces a
  paragraph of dialogue. Whisper's decode survives far more attenuation than the loudness
  drop suggests, because scaling changes level without changing SNR within the clip.
- Only 0.0 (mute) is clean, so 'duck harder' is not a usable lever on its own: the gain
  that silences the TV is the gain that silences the media entirely.
- The owner remains perfectly transcribable at 0.15 (WER 0.0), so an aggressive duck does
  not itself endanger the owner's speech -- but since the ducked TV is still transcribed at
  every non-zero gain tested, the discriminator has to be something other than level.
