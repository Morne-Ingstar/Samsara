# Hands-free bench

Live/resolved config: model=medium, perf=accurate, compute=float16, device=cuda, post_gates=True.
Floor: silence. Floor clips cycle in manifest order, start at sample zero and loop to media length; each media clip uses the same floor at every gain. Mixes are rounded/clipped to int16.

| gain | false-accept |
|---:|---:|
| 1.0 | 3/3 |
| 0.3 | 0/3 |
| 0.15 | 0/3 |
| 0.08 | 0/3 |
| 0.04 | 0/3 |
| 0.02 | 0/3 |
| 0.0 | 0/3 |

Verdict: largest tested gain with media_only false-accept 0/3 is 0.3 with silence floor; above the current capture duck of 0.15.

## Media rows

| file | gain | floor file | floor RMS dBFS | mix RMS dBFS | false-accept |
|---|---:|---|---:|---:|---|
| media_only_01.wav | 1.0 | silence_01.wav | -40.04 | -35.4 | True |
| media_only_01.wav | 0.3 | silence_01.wav | -40.04 | -39.26 | False |
| media_only_01.wav | 0.15 | silence_01.wav | -40.04 | -39.79 | False |
| media_only_01.wav | 0.08 | silence_01.wav | -40.04 | -39.95 | False |
| media_only_01.wav | 0.04 | silence_01.wav | -40.04 | -40.0 | False |
| media_only_01.wav | 0.02 | silence_01.wav | -40.04 | -40.02 | False |
| media_only_01.wav | 0.0 | silence_01.wav | -40.04 | -40.04 | False |
| media_only_02.wav | 1.0 | silence_02.wav | -39.67 | -37.74 | True |
| media_only_02.wav | 0.3 | silence_02.wav | -39.67 | -40.61 | False |
| media_only_02.wav | 0.15 | silence_02.wav | -39.67 | -40.31 | False |
| media_only_02.wav | 0.08 | silence_02.wav | -39.67 | -40.04 | False |
| media_only_02.wav | 0.04 | silence_02.wav | -39.67 | -39.87 | False |
| media_only_02.wav | 0.02 | silence_02.wav | -39.67 | -39.77 | False |
| media_only_02.wav | 0.0 | silence_02.wav | -39.67 | -39.67 | False |
| media_only_03.wav | 1.0 | silence_03.wav | -38.81 | -32.48 | True |
| media_only_03.wav | 0.3 | silence_03.wav | -38.81 | -37.2 | False |
| media_only_03.wav | 0.15 | silence_03.wav | -38.81 | -38.14 | False |
| media_only_03.wav | 0.08 | silence_03.wav | -38.81 | -38.5 | False |
| media_only_03.wav | 0.04 | silence_03.wav | -38.81 | -38.67 | False |
| media_only_03.wav | 0.02 | silence_03.wav | -38.81 | -38.75 | False |
| media_only_03.wav | 0.0 | silence_03.wav | -38.81 | -38.81 | False |

## Exact accepted media texts

### media_only_01.wav @ 1.0

think about this yeah but no well I played with this striker Jemma who would be great great but she quit a couple years ago to study law. I'm Jack. Plus she'd never played for Regiment. Well how about you give her a shout anyway and see what's worth it. It's worth a shot. Fine yeah but we'd better like getting players off the screen.

### media_only_02.wav @ 1.0

Philstone, professor of sports psychology at Loughborough University, and she's here to talk about her new book, Unlocking the Eye in Team. The psychology guy in team, really. Dr. Philstone. Mr. Crane. Sharon. It's good to have you. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I usually invoke that same phrase in my book, The Richmond Way, out now on paperback. I have a major book. Okay. I'll send you home with the signed copy.

### media_only_03.wav @ 1.0

ladies come spit out my mouth thanks that's not worse than the last one yeah man i just mean i used to be able to walk into any part of this building and i don't love me and make it feel like i'm now violating the lady parts what the heck is going on you okay coach i don't know thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took

## All labels

| label | count | metrics |
|---|---:|---|
| media_only@0.0 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.02 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.04 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.08 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.15 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.3 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@1.0 | 3 | false_accept_rate=1.0, phrases=["Philstone, professor of sports psychology at Loughborough University, and she's here to talk about her new book, Unlocking the Eye in Team. The psychology guy in team, really. Dr. Philstone. Mr. Crane. Sharon. It's good to have you. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I usually invoke that same phrase in my book, The Richmond Way, out now on paperback. I have a major book. Okay. I'll send you home with the signed copy.", "ladies come spit out my mouth thanks that's not worse than the last one yeah man i just mean i used to be able to walk into any part of this building and i don't love me and make it feel like i'm now violating the lady parts what the heck is going on you okay coach i don't know thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took", "think about this yeah but no well I played with this striker Jemma who would be great great but she quit a couple years ago to study law. I'm Jack. Plus she'd never played for Regiment. Well how about you give her a shout anyway and see what's worth it. It's worth a shot. Fine yeah but we'd better like getting players off the screen."] |
| nonspeech_body | 5 | false_accept_rate=0.0, phrases=[] |
| nonspeech_room | 4 | false_accept_rate=0.0, phrases=[] |
| silence | 3 | false_accept_rate=0.0, phrases=[] |
| speech_owner | 6 | wer=0.030303030303030304, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=1, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None |
| speech_owner_over_media | 3 | wer=0.0, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=0, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None, contamination_words_per_utterance=0.0, contaminated_rate=0.0, pre_roll_contamination_words_per_utterance=0.0 |
| wake_over_media | 2 | wake_detected=2, command_text=['shift chrome to left monitor', 'open clawed'], wake_phrases=['activate hermes', 'hey claude', 'jarvis'], notes=This matches the wake phrase in the TRANSCRIPT and is not an OpenWakeWord detection. |
