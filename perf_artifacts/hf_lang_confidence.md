# Hands-free bench

Resolved config: model=medium, perf=accurate, compute=float16, device=cuda, language=auto, post_gates=True.
Language metadata is retained before post-gates; text below is after gates.
This is the offline hf_bench decode/segment-gate seam, not live capture or injection.

| label | detected language(s) | probability min/mean/max | text produced yes/no |
|---|---|---|---|
| media_only | en | 0.840332 / 0.855957 / 0.886230 | yes (3/3) |
| nonspeech_body | en | 0.541504 / 0.598145 / 0.824707 | no (0/5) |
| nonspeech_room | en | 0.541504 / 0.541504 / 0.541504 | no (0/4) |
| silence | en | 0.541504 / 0.541504 / 0.541504 | no (0/3) |
| speech_owner | en | 0.984375 / 0.990072 / 0.993164 | yes (6/6) |
| speech_owner_over_media | en | 0.983398 / 0.987630 / 0.994629 | yes (3/3) |
| wake_over_media | en | 0.981934 / 0.983398 / 0.984863 | yes (2/2) |

Owner minimum (9 rows): 0.9833984375
Noise maximum (12 rows): 0.82470703125
Separate: True

## All labels

| label | count | metrics |
|---|---:|---|
| media_only | 3 | false_accept_rate=1.0, phrases=["Okay, do you know anybody? Yeah, but no. I'm intrigued. Oh, well, I played with this Stryker jammer who would be great great, but she quit a couple years ago to study law I object she'd never played for regiment. How about you give her a shout anyway? See what's what you know? It's worth a shot fine Yeah, but would have better luck getting plays off the street", "Philstone professor of sports psychology at the Loughborough University and she's here to talk about her new book, Unlocking the Eye in Team. The psychology behind team building, Dr. Philstone. Mr. Crimm. Sharon, it's good to have you here. I'd like to start with the phrase that you returned to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now in the paperback. I have a major book. Okay, I'll send you over the signed copy.", "after i heard the word ladies come spitting out of my mouth i guess that's not worse than the last one yeah man yeah i just mean i used to be able to walk into any part of this building and i don't love me and make you feel like i'm now violating the lady parts what the heck is going on with me you okay coach i don't know well thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took forever to air dry"] |
| nonspeech_body | 5 | false_accept_rate=0.0, phrases=[] |
| nonspeech_room | 4 | false_accept_rate=0.0, phrases=[] |
| silence | 3 | false_accept_rate=0.0, phrases=[] |
| speech_owner | 6 | wer=0.030303030303030304, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=1, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None |
| speech_owner_over_media | 3 | wer=0.0, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=0, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None, contamination_words_per_utterance=0.0, contaminated_rate=0.0, pre_roll_contamination_words_per_utterance=0.0 |
| wake_over_media | 2 | wake_detected=2, command_text=['shift chrome to left monitor', 'open clawed'], wake_phrases=['activate hermes', 'hey claude', 'jarvis'], notes=This matches the wake phrase in the TRANSCRIPT and is not an OpenWakeWord detection. |
