# Hands-free bench

Live/resolved config: model=medium, perf=accurate, compute=float16, device=cuda, post_gates=True.
Floor: nonspeech_room. Floor clips cycle in manifest order, start at sample zero and loop to media length; each media clip uses the same floor at every gain. Mixes are rounded/clipped to int16.

| gain | false-accept |
|---:|---:|
| 1.0 | 3/3 |
| 0.3 | 0/3 |
| 0.15 | 0/3 |
| 0.08 | 0/3 |
| 0.04 | 0/3 |
| 0.02 | 0/3 |
| 0.0 | 0/3 |

Verdict: largest tested gain with media_only false-accept 0/3 is 0.3 with nonspeech_room floor; above the current capture duck of 0.15.

## Media rows

| file | gain | floor file | floor RMS dBFS | mix RMS dBFS | false-accept |
|---|---:|---|---:|---:|---|
| media_only_01.wav | 1.0 | nonspeech_room_01.wav | -35.28 | -33.44 | True |
| media_only_01.wav | 0.3 | nonspeech_room_01.wav | -35.28 | -35.14 | False |
| media_only_01.wav | 0.15 | nonspeech_room_01.wav | -35.28 | -35.27 | False |
| media_only_01.wav | 0.08 | nonspeech_room_01.wav | -35.28 | -35.29 | False |
| media_only_01.wav | 0.04 | nonspeech_room_01.wav | -35.28 | -35.29 | False |
| media_only_01.wav | 0.02 | nonspeech_room_01.wav | -35.28 | -35.29 | False |
| media_only_01.wav | 0.0 | nonspeech_room_01.wav | -35.28 | -35.28 | False |
| media_only_02.wav | 1.0 | nonspeech_room_02.wav | -39.53 | -34.8 | True |
| media_only_02.wav | 0.3 | nonspeech_room_02.wav | -39.53 | -38.59 | False |
| media_only_02.wav | 0.15 | nonspeech_room_02.wav | -39.53 | -39.19 | False |
| media_only_02.wav | 0.08 | nonspeech_room_02.wav | -39.53 | -39.38 | False |
| media_only_02.wav | 0.04 | nonspeech_room_02.wav | -39.53 | -39.47 | False |
| media_only_02.wav | 0.02 | nonspeech_room_02.wav | -39.53 | -39.5 | False |
| media_only_02.wav | 0.0 | nonspeech_room_02.wav | -39.53 | -39.53 | False |
| media_only_03.wav | 1.0 | nonspeech_room_03.wav | -35.62 | -32.12 | True |
| media_only_03.wav | 0.3 | nonspeech_room_03.wav | -35.62 | -35.15 | False |
| media_only_03.wav | 0.15 | nonspeech_room_03.wav | -35.62 | -35.49 | False |
| media_only_03.wav | 0.08 | nonspeech_room_03.wav | -35.62 | -35.58 | False |
| media_only_03.wav | 0.04 | nonspeech_room_03.wav | -35.62 | -35.61 | False |
| media_only_03.wav | 0.02 | nonspeech_room_03.wav | -35.62 | -35.62 | False |
| media_only_03.wav | 0.0 | nonspeech_room_03.wav | -35.62 | -35.62 | False |

## Exact accepted media texts

### media_only_01.wav @ 1.0

Okay

### media_only_02.wav @ 1.0

Dr. Fieldstone, professor of sports psychology at the Loughborough University, and she's here to talk about her new book, Unlocking the I in Team, the psychology behind team building. It's good to have you here. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now. I made your book. Yeah.

### media_only_03.wav @ 1.0

instead of after I heard the word ladies come spit out of my mouth today because that's not worse than the last one. Yeah, man. I just mean, I used to be able to walk into any part of this building and I don't love me, it made me feel like I'm now violating the ladies' parts. What the heck is going on with me? You okay, Coach? I don't know. Well, thank God. Sorry, Ruth just wanted to shower right away to wash the blood off her hands and then she took a bit of the air dryer.

## All labels

| label | count | metrics |
|---|---:|---|
| media_only@0.0 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.02 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.04 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.08 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.15 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@0.3 | 3 | false_accept_rate=0.0, phrases=[] |
| media_only@1.0 | 3 | false_accept_rate=1.0, phrases=["Dr. Fieldstone, professor of sports psychology at the Loughborough University, and she's here to talk about her new book, Unlocking the I in Team, the psychology behind team building. It's good to have you here. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now. I made your book. Yeah.", 'Okay', "instead of after I heard the word ladies come spit out of my mouth today because that's not worse than the last one. Yeah, man. I just mean, I used to be able to walk into any part of this building and I don't love me, it made me feel like I'm now violating the ladies' parts. What the heck is going on with me? You okay, Coach? I don't know. Well, thank God. Sorry, Ruth just wanted to shower right away to wash the blood off her hands and then she took a bit of the air dryer."] |
| nonspeech_body | 5 | false_accept_rate=0.0, phrases=[] |
| nonspeech_room | 4 | false_accept_rate=0.0, phrases=[] |
| silence | 3 | false_accept_rate=0.0, phrases=[] |
| speech_owner | 6 | wer=0.030303030303030304, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=1, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None |
| speech_owner_over_media | 3 | wer=0.0, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=0, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None, contamination_words_per_utterance=0.0, contaminated_rate=0.0, pre_roll_contamination_words_per_utterance=0.0 |
| wake_over_media | 2 | wake_detected=2, command_text=['shift chrome to left monitor', 'open clawed'], wake_phrases=['activate hermes', 'hey claude', 'jarvis'], notes=This matches the wake phrase in the TRANSCRIPT and is not an OpenWakeWord detection. |
