# Hands-free bench

Resolved config: model=medium, perf=accurate, compute=float16, device=cuda, language=auto, post_gates=True.
Language metadata is retained before post-gates; text below is after gates.
This is the offline hf_bench decode/segment-gate seam, not live capture or injection.

| label | detected language(s) | probability min/mean/max | text produced yes/no |
|---|---|---|---|
| media_only | en | 0.840332 / 0.855957 / 0.886230 | yes (3/3) |
| media_only@0.5 | en | 0.541504 / 0.699382 / 0.800781 | yes (2/3) |
| media_only@1.0 | en | 0.733887 / 0.789388 / 0.896973 | yes (3/3) |
| media_only@w0.8 | en | 0.511230 / 0.820399 / 0.985352 | yes (13/17) |
| media_only@w1.2 | en | 0.774902 / 0.895682 / 0.995117 | yes (14/14) |
| media_only@w2.0 | en | 0.857422 / 0.926382 / 0.979492 | yes (13/13) |
| media_only@w3.0 | en | 0.756836 / 0.892052 / 0.975098 | yes (13/13) |
| nonspeech_body | en | 0.541504 / 0.598145 / 0.824707 | no (0/5) |
| nonspeech_room | en | 0.541504 / 0.541504 / 0.541504 | no (0/4) |
| silence | en | 0.541504 / 0.541504 / 0.541504 | no (0/3) |
| speech_owner | en | 0.984375 / 0.990072 / 0.993164 | yes (6/6) |
| speech_owner@w0.8 | en | 0.869629 / 0.959692 / 0.991211 | yes (25/29) |
| speech_owner@w1.2 | en | 0.932129 / 0.973453 / 0.996094 | yes (17/19) |
| speech_owner@w2.0 | en | 0.947754 / 0.973544 / 0.990234 | yes (11/11) |
| speech_owner@w3.0 | en | 0.962891 / 0.983032 / 0.992676 | yes (8/8) |
| speech_owner_over_media | en | 0.983398 / 0.987630 / 0.994629 | yes (3/3) |
| wake_over_media | en | 0.981934 / 0.983398 / 0.984863 | yes (2/2) |

Owner minimum (9 rows): 0.9833984375
Noise maximum (12 rows): 0.82470703125
Separate: True

## Language probability windows

Full windows start at sample zero with hop equal to window size; incomplete tails are excluded. Silero is scanned independently for each window using complete 512-sample/32 ms frames. Frames above the configured VAD probability threshold are voiced; windows below 50% are dropped. Per-file drops, frame counts, timestamps and tail sample counts are in the JSON.

Single probability floor: 0.9951171875000001; accept iff language_probability >= floor. The floor is the next representable float above the maximum over ALL retained raw media windows. Probability FRR includes every retained owner window, regardless of post-gate text; VAD drops are excluded from its denominator. Gain/floor mixes are a separate full-clip comparison.

| window s | kept owner/media | dropped owner/media | owner min | media max | gap | owner FRR at floor | media accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.8 | 29/17 | 14/94 | 0.869628906 | 0.985351562 | -0.115722656 | 29/29 (100.00%) | 0 |
| 1.2 | 19/14 | 9/61 | 0.932128906 | 0.995117188 | -0.062988281 | 18/19 (94.74%) | 0 |
| 2.0 | 11/13 | 5/32 | 0.947753906 | 0.979492188 | -0.031738281 | 11/11 (100.00%) | 0 |
| 3.0 | 8/13 | 1/17 | 0.962890625 | 0.975097656 | -0.012207031 | 8/8 (100.00%) | 0 |

VERDICT LINE: not usable; floor=0.9951171875000001; owner FRR at 1.2 s=94.74% (limit 5%), at 2.0 s=100.00% (limit 2%).

## Full-clip media gain language probability

| file | gain | silence floor | detected language | language probability |
|---|---:|---|---|---:|
| media_only_01.wav | 1.0 | silence_01.wav | en | 0.73388671875 |
| media_only_01.wav | 0.5 | silence_01.wav | en | 0.54150390625 |
| media_only_01.wav | raw | none | en | 0.84033203125 |
| media_only_02.wav | 1.0 | silence_02.wav | en | 0.89697265625 |
| media_only_02.wav | 0.5 | silence_02.wav | en | 0.755859375 |
| media_only_02.wav | raw | none | en | 0.88623046875 |
| media_only_03.wav | 1.0 | silence_03.wav | en | 0.7373046875 |
| media_only_03.wav | 0.5 | silence_03.wav | en | 0.80078125 |
| media_only_03.wav | raw | none | en | 0.84130859375 |

## Retained window metadata

| file | label | start-end s | voiced/total frames | language | probability | text after gates |
|---|---|---|---:|---|---:|---|
| speech_owner_01.wav | speech_owner@w0.8 | 1.600-2.400 | 13/25 | en | 0.90625 | yes |
| speech_owner_01.wav | speech_owner@w0.8 | 2.400-3.200 | 24/25 | en | 0.9912109375 | yes |
| speech_owner_01.wav | speech_owner@w0.8 | 3.200-4.000 | 18/25 | en | 0.95458984375 | yes |
| speech_owner_01.wav | speech_owner@w0.8 | 4.000-4.800 | 22/25 | en | 0.97705078125 | yes |
| speech_owner_01.wav | speech_owner@w0.8 | 4.800-5.600 | 21/25 | en | 0.98828125 | yes |
| speech_owner_01.wav | speech_owner@w1.2 | 2.400-3.600 | 30/37 | en | 0.98388671875 | yes |
| speech_owner_01.wav | speech_owner@w1.2 | 3.600-4.800 | 36/37 | en | 0.982421875 | yes |
| speech_owner_01.wav | speech_owner@w1.2 | 4.800-6.000 | 28/37 | en | 0.99267578125 | yes |
| speech_owner_01.wav | speech_owner@w2.0 | 2.000-4.000 | 53/62 | en | 0.97509765625 | yes |
| speech_owner_01.wav | speech_owner@w2.0 | 4.000-6.000 | 54/62 | en | 0.9873046875 | yes |
| speech_owner_01.wav | speech_owner@w3.0 | 3.000-6.000 | 77/93 | en | 0.98486328125 | yes |
| speech_owner_02.wav | speech_owner@w0.8 | 1.600-2.400 | 23/25 | en | 0.9501953125 | yes |
| speech_owner_02.wav | speech_owner@w0.8 | 2.400-3.200 | 25/25 | en | 0.966796875 | yes |
| speech_owner_02.wav | speech_owner@w0.8 | 3.200-4.000 | 25/25 | en | 0.98046875 | yes |
| speech_owner_02.wav | speech_owner@w0.8 | 4.000-4.800 | 16/25 | en | 0.96484375 | yes |
| speech_owner_02.wav | speech_owner@w1.2 | 1.200-2.400 | 32/37 | en | 0.96337890625 | yes |
| speech_owner_02.wav | speech_owner@w1.2 | 2.400-3.600 | 37/37 | en | 0.9677734375 | yes |
| speech_owner_02.wav | speech_owner@w1.2 | 3.600-4.800 | 32/37 | en | 0.97265625 | yes |
| speech_owner_02.wav | speech_owner@w2.0 | 2.000-4.000 | 62/62 | en | 0.982421875 | yes |
| speech_owner_02.wav | speech_owner@w3.0 | 0.000-3.000 | 51/93 | en | 0.962890625 | yes |
| speech_owner_02.wav | speech_owner@w3.0 | 3.000-6.000 | 64/93 | en | 0.98876953125 | yes |
| speech_owner_03.wav | speech_owner@w0.8 | 0.800-1.600 | 24/25 | en | 0.9853515625 | yes |
| speech_owner_03.wav | speech_owner@w0.8 | 1.600-2.400 | 24/25 | en | 0.95458984375 | yes |
| speech_owner_03.wav | speech_owner@w0.8 | 2.400-3.200 | 18/25 | en | 0.87060546875 | no |
| speech_owner_03.wav | speech_owner@w0.8 | 3.200-4.000 | 21/25 | en | 0.9599609375 | no |
| speech_owner_03.wav | speech_owner@w0.8 | 4.000-4.800 | 18/25 | en | 0.9716796875 | yes |
| speech_owner_03.wav | speech_owner@w0.8 | 4.800-5.600 | 23/25 | en | 0.86962890625 | no |
| speech_owner_03.wav | speech_owner@w0.8 | 5.600-6.400 | 23/25 | en | 0.9892578125 | yes |
| speech_owner_03.wav | speech_owner@w1.2 | 1.200-2.400 | 37/37 | en | 0.97412109375 | yes |
| speech_owner_03.wav | speech_owner@w1.2 | 2.400-3.600 | 30/37 | en | 0.974609375 | no |
| speech_owner_03.wav | speech_owner@w1.2 | 3.600-4.800 | 32/37 | en | 0.9775390625 | yes |
| speech_owner_03.wav | speech_owner@w1.2 | 4.800-6.000 | 35/37 | en | 0.93212890625 | no |
| speech_owner_03.wav | speech_owner@w1.2 | 6.000-7.200 | 20/37 | en | 0.9892578125 | yes |
| speech_owner_03.wav | speech_owner@w2.0 | 0.000-2.000 | 36/62 | en | 0.96533203125 | yes |
| speech_owner_03.wav | speech_owner@w2.0 | 2.000-4.000 | 52/62 | en | 0.9833984375 | yes |
| speech_owner_03.wav | speech_owner@w2.0 | 4.000-6.000 | 55/62 | en | 0.94775390625 | yes |
| speech_owner_03.wav | speech_owner@w3.0 | 0.000-3.000 | 66/93 | en | 0.98974609375 | yes |
| speech_owner_03.wav | speech_owner@w3.0 | 3.000-6.000 | 86/93 | en | 0.97802734375 | yes |
| speech_owner_04.wav | speech_owner@w0.8 | 0.800-1.600 | 16/25 | en | 0.97412109375 | yes |
| speech_owner_04.wav | speech_owner@w0.8 | 1.600-2.400 | 25/25 | en | 0.92919921875 | yes |
| speech_owner_04.wav | speech_owner@w0.8 | 2.400-3.200 | 23/25 | en | 0.91943359375 | no |
| speech_owner_04.wav | speech_owner@w0.8 | 3.200-4.000 | 20/25 | en | 0.94970703125 | yes |
| speech_owner_04.wav | speech_owner@w0.8 | 4.000-4.800 | 13/25 | en | 0.9873046875 | yes |
| speech_owner_04.wav | speech_owner@w1.2 | 1.200-2.400 | 35/37 | en | 0.939453125 | yes |
| speech_owner_04.wav | speech_owner@w1.2 | 2.400-3.600 | 35/37 | en | 0.97021484375 | yes |
| speech_owner_04.wav | speech_owner@w1.2 | 3.600-4.800 | 34/37 | en | 0.9765625 | yes |
| speech_owner_04.wav | speech_owner@w2.0 | 2.000-4.000 | 60/62 | en | 0.9658203125 | yes |
| speech_owner_04.wav | speech_owner@w3.0 | 0.000-3.000 | 59/93 | en | 0.9873046875 | yes |
| speech_owner_05.wav | speech_owner@w0.8 | 0.800-1.600 | 25/25 | en | 0.9765625 | yes |
| speech_owner_05.wav | speech_owner@w0.8 | 1.600-2.400 | 24/25 | en | 0.9609375 | yes |
| speech_owner_05.wav | speech_owner@w0.8 | 2.400-3.200 | 24/25 | en | 0.94677734375 | yes |
| speech_owner_05.wav | speech_owner@w1.2 | 1.200-2.400 | 37/37 | en | 0.96044921875 | yes |
| speech_owner_05.wav | speech_owner@w1.2 | 2.400-3.600 | 35/37 | en | 0.97900390625 | yes |
| speech_owner_05.wav | speech_owner@w2.0 | 0.000-2.000 | 37/62 | en | 0.98486328125 | yes |
| speech_owner_05.wav | speech_owner@w2.0 | 2.000-4.000 | 56/62 | en | 0.990234375 | yes |
| speech_owner_05.wav | speech_owner@w3.0 | 0.000-3.000 | 68/93 | en | 0.97998046875 | yes |
| speech_owner_06.wav | speech_owner@w0.8 | 0.800-1.600 | 22/25 | en | 0.97412109375 | yes |
| speech_owner_06.wav | speech_owner@w0.8 | 1.600-2.400 | 21/25 | en | 0.990234375 | yes |
| speech_owner_06.wav | speech_owner@w0.8 | 2.400-3.200 | 21/25 | en | 0.98193359375 | yes |
| speech_owner_06.wav | speech_owner@w0.8 | 3.200-4.000 | 21/25 | en | 0.97265625 | yes |
| speech_owner_06.wav | speech_owner@w0.8 | 4.000-4.800 | 20/25 | en | 0.9873046875 | yes |
| speech_owner_06.wav | speech_owner@w1.2 | 1.200-2.400 | 31/37 | en | 0.98046875 | yes |
| speech_owner_06.wav | speech_owner@w1.2 | 2.400-3.600 | 29/37 | en | 0.98291015625 | yes |
| speech_owner_06.wav | speech_owner@w1.2 | 3.600-4.800 | 33/37 | en | 0.99609375 | yes |
| speech_owner_06.wav | speech_owner@w2.0 | 0.000-2.000 | 34/62 | en | 0.95556640625 | yes |
| speech_owner_06.wav | speech_owner@w2.0 | 2.000-4.000 | 52/62 | en | 0.97119140625 | yes |
| speech_owner_06.wav | speech_owner@w3.0 | 0.000-3.000 | 61/93 | en | 0.99267578125 | yes |
| media_only_01.wav | media_only@w0.8 | 4.000-4.800 | 14/25 | en | 0.92724609375 | yes |
| media_only_01.wav | media_only@w0.8 | 11.200-12.000 | 14/25 | en | 0.91455078125 | yes |
| media_only_01.wav | media_only@w0.8 | 12.800-13.600 | 13/25 | en | 0.51123046875 | no |
| media_only_01.wav | media_only@w0.8 | 13.600-14.400 | 13/25 | en | 0.7080078125 | yes |
| media_only_01.wav | media_only@w1.2 | 6.000-7.200 | 23/37 | en | 0.93798828125 | yes |
| media_only_01.wav | media_only@w1.2 | 12.000-13.200 | 19/37 | en | 0.77490234375 | yes |
| media_only_01.wav | media_only@w2.0 | 6.000-8.000 | 32/62 | en | 0.9794921875 | yes |
| media_only_01.wav | media_only@w2.0 | 8.000-10.000 | 35/62 | en | 0.95361328125 | yes |
| media_only_01.wav | media_only@w3.0 | 6.000-9.000 | 47/93 | en | 0.9453125 | yes |
| media_only_01.wav | media_only@w3.0 | 12.000-15.000 | 57/93 | en | 0.9462890625 | yes |
| media_only_01.wav | media_only@w3.0 | 15.000-18.000 | 72/93 | en | 0.7568359375 | yes |
| media_only_02.wav | media_only@w0.8 | 0.000-0.800 | 15/25 | en | 0.9423828125 | no |
| media_only_02.wav | media_only@w0.8 | 4.000-4.800 | 23/25 | en | 0.81689453125 | yes |
| media_only_02.wav | media_only@w0.8 | 15.200-16.000 | 17/25 | en | 0.939453125 | yes |
| media_only_02.wav | media_only@w0.8 | 16.800-17.600 | 15/25 | en | 0.91357421875 | yes |
| media_only_02.wav | media_only@w0.8 | 18.400-19.200 | 13/25 | en | 0.78857421875 | yes |
| media_only_02.wav | media_only@w1.2 | 0.000-1.200 | 27/37 | en | 0.904296875 | yes |
| media_only_02.wav | media_only@w1.2 | 1.200-2.400 | 28/37 | en | 0.81396484375 | yes |
| media_only_02.wav | media_only@w1.2 | 3.600-4.800 | 30/37 | en | 0.8974609375 | yes |
| media_only_02.wav | media_only@w1.2 | 14.400-15.600 | 19/37 | en | 0.8974609375 | yes |
| media_only_02.wav | media_only@w1.2 | 16.800-18.000 | 27/37 | en | 0.81103515625 | yes |
| media_only_02.wav | media_only@w2.0 | 0.000-2.000 | 52/62 | en | 0.857421875 | yes |
| media_only_02.wav | media_only@w2.0 | 4.000-6.000 | 50/62 | en | 0.89599609375 | yes |
| media_only_02.wav | media_only@w2.0 | 8.000-10.000 | 42/62 | en | 0.9521484375 | yes |
| media_only_02.wav | media_only@w2.0 | 14.000-16.000 | 32/62 | en | 0.93212890625 | yes |
| media_only_02.wav | media_only@w3.0 | 0.000-3.000 | 83/93 | en | 0.83740234375 | yes |
| media_only_02.wav | media_only@w3.0 | 3.000-6.000 | 72/93 | en | 0.91455078125 | yes |
| media_only_02.wav | media_only@w3.0 | 15.000-18.000 | 85/93 | en | 0.8046875 | yes |
| media_only_02.wav | media_only@w3.0 | 18.000-21.000 | 56/93 | en | 0.93701171875 | yes |
| media_only_02.wav | media_only@w3.0 | 21.000-24.000 | 52/93 | en | 0.82275390625 | yes |
| media_only_03.wav | media_only@w0.8 | 1.600-2.400 | 18/25 | en | 0.873046875 | yes |
| media_only_03.wav | media_only@w0.8 | 3.200-4.000 | 13/25 | en | 0.98095703125 | yes |
| media_only_03.wav | media_only@w0.8 | 8.000-8.800 | 19/25 | en | 0.833984375 | yes |
| media_only_03.wav | media_only@w0.8 | 12.800-13.600 | 24/25 | en | 0.5400390625 | no |
| media_only_03.wav | media_only@w0.8 | 15.200-16.000 | 13/25 | en | 0.9853515625 | yes |
| media_only_03.wav | media_only@w0.8 | 16.000-16.800 | 17/25 | en | 0.84912109375 | no |
| media_only_03.wav | media_only@w0.8 | 16.800-17.600 | 22/25 | en | 0.85400390625 | yes |
| media_only_03.wav | media_only@w0.8 | 17.600-18.400 | 18/25 | en | 0.568359375 | yes |
| media_only_03.wav | media_only@w1.2 | 3.600-4.800 | 22/37 | en | 0.9951171875 | yes |
| media_only_03.wav | media_only@w1.2 | 6.000-7.200 | 33/37 | en | 0.9111328125 | yes |
| media_only_03.wav | media_only@w1.2 | 9.600-10.800 | 20/37 | en | 0.9619140625 | yes |
| media_only_03.wav | media_only@w1.2 | 10.800-12.000 | 19/37 | en | 0.955078125 | yes |
| media_only_03.wav | media_only@w1.2 | 12.000-13.200 | 21/37 | en | 0.92138671875 | yes |
| media_only_03.wav | media_only@w1.2 | 16.800-18.000 | 34/37 | en | 0.814453125 | yes |
| media_only_03.wav | media_only@w1.2 | 18.000-19.200 | 26/37 | en | 0.943359375 | yes |
| media_only_03.wav | media_only@w2.0 | 2.000-4.000 | 60/62 | en | 0.97021484375 | yes |
| media_only_03.wav | media_only@w2.0 | 4.000-6.000 | 43/62 | en | 0.93408203125 | yes |
| media_only_03.wav | media_only@w2.0 | 6.000-8.000 | 46/62 | en | 0.9453125 | yes |
| media_only_03.wav | media_only@w2.0 | 8.000-10.000 | 40/62 | en | 0.92529296875 | yes |
| media_only_03.wav | media_only@w2.0 | 12.000-14.000 | 46/62 | en | 0.96875 | yes |
| media_only_03.wav | media_only@w2.0 | 16.000-18.000 | 54/62 | en | 0.8623046875 | yes |
| media_only_03.wav | media_only@w2.0 | 18.000-20.000 | 47/62 | en | 0.8662109375 | yes |
| media_only_03.wav | media_only@w3.0 | 3.000-6.000 | 73/93 | en | 0.96044921875 | yes |
| media_only_03.wav | media_only@w3.0 | 6.000-9.000 | 77/93 | en | 0.94482421875 | yes |
| media_only_03.wav | media_only@w3.0 | 12.000-15.000 | 63/93 | en | 0.97509765625 | yes |
| media_only_03.wav | media_only@w3.0 | 15.000-18.000 | 71/93 | en | 0.93798828125 | yes |
| media_only_03.wav | media_only@w3.0 | 18.000-21.000 | 78/93 | en | 0.8134765625 | yes |

Live/resolved config: model=medium, perf=accurate, compute=float16, device=cuda, post_gates=True.
Floor: silence. Floor clips cycle in manifest order, start at sample zero and loop to media length; each media clip uses the same floor at every gain. Mixes are rounded/clipped to int16.

| gain | false-accept |
|---:|---:|
| 1.0 | 3/3 |
| 0.5 | 2/3 |

Verdict: no tested gain has zero media_only false accepts with silence floor; no threshold to compare with the current capture duck of 0.15.

## Media rows

| file | gain | floor file | floor RMS dBFS | mix RMS dBFS | false-accept |
|---|---:|---|---:|---:|---|
| media_only_01.wav | 1.0 | silence_01.wav | -40.04 | -35.4 | True |
| media_only_01.wav | 0.5 | silence_01.wav | -40.04 | -38.25 | False |
| media_only_02.wav | 1.0 | silence_02.wav | -39.67 | -37.74 | True |
| media_only_02.wav | 0.5 | silence_02.wav | -39.67 | -40.38 | True |
| media_only_03.wav | 1.0 | silence_03.wav | -38.81 | -32.48 | True |
| media_only_03.wav | 0.5 | silence_03.wav | -38.81 | -35.79 | True |

## Exact accepted media texts

### media_only_01.wav @ 1.0

think about this yeah but no well I played with this striker Jemma who would be great great but she quit a couple years ago to study law. I'm Jack. Plus she'd never played for Regiment. Well how about you give her a shout anyway and see what's worth it. It's worth a shot. Fine yeah but we'd better like getting players off the screen.

### media_only_02.wav @ 1.0

Philstone, professor of sports psychology at Loughborough University, and she's here to talk about her new book, Unlocking the Eye in Team. The psychology guy in team, really. Dr. Philstone. Mr. Crane. Sharon. It's good to have you. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I usually invoke that same phrase in my book, The Richmond Way, out now on paperback. I have a major book. Okay. I'll send you home with the signed copy.

### media_only_02.wav @ 0.5

to start with the phrase at all times in your work as the dressing room.

### media_only_03.wav @ 1.0

ladies come spit out my mouth thanks that's not worse than the last one yeah man i just mean i used to be able to walk into any part of this building and i don't love me and make it feel like i'm now violating the lady parts what the heck is going on you okay coach i don't know thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took

### media_only_03.wav @ 0.5

I'm going to go and shower. Goats wanted to shower right away to wash the blood off their hands.

## All labels

| label | count | metrics |
|---|---:|---|
| media_only | 3 | false_accept_rate=1.0, phrases=["Okay, do you know anybody? Yeah, but no. I'm intrigued. Oh, well, I played with this Stryker jammer who would be great great, but she quit a couple years ago to study law I object she'd never played for regiment. How about you give her a shout anyway? See what's what you know? It's worth a shot fine Yeah, but would have better luck getting plays off the street", "Philstone professor of sports psychology at the Loughborough University and she's here to talk about her new book, Unlocking the Eye in Team. The psychology behind team building, Dr. Philstone. Mr. Crimm. Sharon, it's good to have you here. I'd like to start with the phrase that you returned to several times in your book, lose the dressing room, lose the team. I actually invoke that same phrase in my book, The Richmond Way, out now in the paperback. I have a major book. Okay, I'll send you over the signed copy.", "after i heard the word ladies come spitting out of my mouth i guess that's not worse than the last one yeah man yeah i just mean i used to be able to walk into any part of this building and i don't love me and make you feel like i'm now violating the lady parts what the heck is going on with me you okay coach i don't know well thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took forever to air dry"] |
| media_only@0.5 | 3 | false_accept_rate=0.6666666666666666, phrases=["I'm going to go and shower. Goats wanted to shower right away to wash the blood off their hands.", 'to start with the phrase at all times in your work as the dressing room.'] |
| media_only@1.0 | 3 | false_accept_rate=1.0, phrases=["Philstone, professor of sports psychology at Loughborough University, and she's here to talk about her new book, Unlocking the Eye in Team. The psychology guy in team, really. Dr. Philstone. Mr. Crane. Sharon. It's good to have you. I'd like to start with the phrase that you return to several times in your book, lose the dressing room, lose the team. I usually invoke that same phrase in my book, The Richmond Way, out now on paperback. I have a major book. Okay. I'll send you home with the signed copy.", "ladies come spit out my mouth thanks that's not worse than the last one yeah man i just mean i used to be able to walk into any part of this building and i don't love me and make it feel like i'm now violating the lady parts what the heck is going on you okay coach i don't know thank god sorry uh boots wanted to shower right away to wash the blood off her hands and then she took", "think about this yeah but no well I played with this striker Jemma who would be great great but she quit a couple years ago to study law. I'm Jack. Plus she'd never played for Regiment. Well how about you give her a shout anyway and see what's worth it. It's worth a shot. Fine yeah but we'd better like getting players off the screen."] |
| media_only@w0.8 | 17 | false_accept_rate=0.7647058823529411, phrases=["He's here to talk about it.", "I'll love you, man.", "I'm going to shower right now.", "Let's start with the phrase.", "Let's think about this.", 'Loose the dress.', 'Sorry', "That's not personal", "Wait, what's the blood?", 'and it would be cool.', 'played with this.', 'several times.', 'when ladies come spin up.'] |
| media_only@w1.2 | 14 | false_accept_rate=1.0, phrases=['Do you know anybody?', 'I used to be able to walk into any part of the...', "I'd like to stop.", 'Phil Stone, professor.', 'Stryker Gemini', 'What the heck is going on, man?', 'You okay, Coach?', "and she's here to talk about...", 'and the lady parts, what the?', 'of sports psychology.', 'several times in your book.', 'to shower right away.', 'wash the blood off her hands.', 'worse than the last one.'] |
| media_only@w2.0 | 13 | false_accept_rate=1.0, phrases=['Boots wanted to shower right away.', "But no, I'm a tree.", 'Do you know anybody? Yeah.', 'I used to be able to walk into any part of this building and I', "I'd like to start with a phrase.", "If you don't love me, it makes me feel like I'm not violating the legi-", 'Phil Stone, professor of sports psychology.', "She's here to talk about her new book.", 'Yeah, man. I just mean...', "You okay, Coach? I don't know.", "spit out my mouth because that's not personal.", 'the blood off her hands and then she', 'the psychology behind team building.'] |
| media_only@w3.0 | 13 | false_accept_rate=1.0, phrases=['Do you know anybody? Yeah. But no.', 'I actually invoke that same phrase in my book, The Richmond Way.', "I guess that's not worse than the last one. Yeah, man. I just mean...", "I used to be able to walk into any part of this building, and I don't love being made to feel like.", "I'd like to start with the phrase that you return to several times in your book.", 'Lose the dressing room. Lose the team.', 'Professor of Sports Psychology at the Loughborough University.', 'Sorry, Boots wanted to shower right away.', "You okay, Coach? I don't know. Well, thank God.", "and she's here to talk about her new book.", 'but she quit a couple of years ago to study law. I object!', 'strike her jammer, and it would be great. Great!', 'washed the blood off her hands and then she took forever to air dry her hands.'] |
| nonspeech_body | 5 | false_accept_rate=0.0, phrases=[] |
| nonspeech_room | 4 | false_accept_rate=0.0, phrases=[] |
| silence | 3 | false_accept_rate=0.0, phrases=[] |
| speech_owner | 6 | wer=0.030303030303030304, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=1, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None |
| speech_owner@w0.8 | 29 | false_reject_rate=0.13793103448275862, notes=post-gate empty text; probability-floor FRR is reported separately |
| speech_owner@w1.2 | 19 | false_reject_rate=0.10526315789473684, notes=post-gate empty text; probability-floor FRR is reported separately |
| speech_owner@w2.0 | 11 | false_reject_rate=0.0, notes=post-gate empty text; probability-floor FRR is reported separately |
| speech_owner@w3.0 | 8 | false_reject_rate=0.0, notes=post-gate empty text; probability-floor FRR is reported separately |
| speech_owner_over_media | 3 | wer=0.0, false_reject_rate=0.0, leading_insertions=0, trailing_insertions=0, pre_buffer_comparison_count=0, pre_buffer_leading_insertions_delta_per_utterance=None, contamination_words_per_utterance=0.0, contaminated_rate=0.0, pre_roll_contamination_words_per_utterance=0.0 |
| wake_over_media | 2 | wake_detected=2, command_text=['shift chrome to left monitor', 'open clawed'], wake_phrases=['activate hermes', 'hey claude', 'jarvis'], notes=This matches the wake phrase in the TRANSCRIPT and is not an OpenWakeWord detection. |

## language_probability as source gate -- measurement (2026-09-10)

**VERDICT LINE: not usable.** The lowest single floor that rejects all 57 retained media windows is **0.9951171875000001** (accept when probability >= floor). It rejects **18/19 owner windows at 1.2 s (94.74%)**, above the 5% limit, and **11/11 at 2.0 s (100%)**, above the 2% limit. A convenient decimal floor of 0.9952 gives identical decisions on these measurements. Any higher floor can only increase owner rejection.

Artifacts: [Markdown](hf_lang_prob_windows.md) and [JSON with every retained window, VAD drop and gain decode](hf_lang_prob_windows.json).

Read the live config: `medium`, `accurate`, `cuda`, `float16`, language `auto` (resolved to `None`), beam 5. Gates ON uses the existing offline seam: Whisper VAD, native thresholds 0.6/-1.0 and production segment-quality gates. Language metadata is retained before text gating. This does not simulate live capture, routing, injection, or the shipped language/script gate. The serial benchmark uses one model worker and four CPU threads; model/decode/gate settings were not changed. No dependencies were installed.

For each of six `speech_owner` and three `media_only` clips, full 0.8/1.2/2.0/3.0 s windows start at sample zero with hop equal to window length. Each window gets an independent bundled Silero scan over complete 512-sample (32 ms) frames; frame probability > 0.5 is voiced. Windows with fewer than 50% voiced frames are dropped; exactly 50% is retained. Incomplete final windows are excluded rather than padded. Of 357 full candidate windows, 124 were decoded and 233 dropped (29 owner, 204 media). The table reports probability-only FRR among retained owner windows, regardless of post-gate text; VAD drops are not included in that denominator.

| Window s | Kept owner/media | Dropped owner/media | Owner min | Media max | Gap (owner min - media max) | Owner FRR at single floor | Media accepted |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.8 | 29/17 | 14/94 | 0.869628906 | 0.985351562 | -0.115722656 | 29/29 (100.00%) | 0/17 |
| 1.2 | 19/14 | 9/61 | 0.932128906 | 0.995117188 | -0.062988281 | 18/19 (94.74%) | 0/14 |
| 2.0 | 11/13 | 5/32 | 0.947753906 | 0.979492188 | -0.031738281 | 11/11 (100.00%) | 0/13 |
| 3.0 | 8/13 | 1/17 | 0.962890625 | 0.975097656 | -0.012207031 | 8/8 (100.00%) | 0/13 |

The full-clip confidence gap disappears at every tested window size. The strongest media window is `media_only_03.wav`, 3.6-4.8 s: English, probability 0.9951171875, with 22/37 voiced frames; its text also survives the existing gates. All retained windows were detected as English. This measurement supports rejecting language probability as an owner-versus-TV gate.

Each full media clip was also decoded at gains 1.0 and 0.5 using the existing int16 gain/floor mixing path. Pairings are media 01/02/03 with silence 01/02/03, respectively; the same unscaled floor is used at both gains.

| Media file | Probability, gain 1.0 + silence | Probability, gain 0.5 + silence | Change when quieter |
|---|---:|---:|---:|
| media_only_01.wav | 0.733886719 | 0.541503906 | -0.192382812 |
| media_only_02.wav | 0.896972656 | 0.755859375 | -0.141113281 |
| media_only_03.wav | 0.737304688 | 0.800781250 | +0.063476562 |

| Full-clip condition | Media probability mean | Media probability max | Full-owner min minus media max | Text after existing gates |
|---|---:|---:|---:|---:|
| media_only | 0.855957031 | 0.886230469 | 0.098144531 | 3/3 |
| media_only@1.0 | 0.789388021 | 0.896972656 | 0.087402344 | 3/3 |
| media_only@0.5 | 0.699381510 | 0.800781250 | 0.183593750 | 2/3 |

Quieter media widens the aggregate full-clip gap from **0.087402344 to 0.183593750** relative to the six full owner clips (minimum 0.984375); media mean drops by 0.090006510. The effect is not monotonic per clip: media 03 rises by 0.063476562 when halved. Media 01 produces no text at gain 0.5; media 02 and 03 still do. This full-clip gain comparison does not rescue the failed short-window separator.

Coverage: 36.790 s of owner audio and 90.038 s of media, from six and three existing clips respectively. No additional media sources were collected. Cropped windows within a recording and across window sizes are correlated, and these are not newly recorded short utterances. Zero media accepts describes this corpus only. The measured owner rejection is already far beyond both required limits.

Reproduce:

```powershell
& 'F:\envs\sami\python.exe' -B -u tools/hf_bench.py --corpus 'C:\Users\Morne\Documents\Claude\voice_samples\hf_corpus' --lang-prob-windows --media-gain 1.0 --media-gain 0.5 --floor-label silence --out perf_artifacts/hf_lang_prob_windows
```

Focused verification: `F:\envs\sami\python.exe -B -m pytest tests/test_hf_bench.py -q -p no:cacheprovider` — **44 passed**. Six additional inline checks passed for exact 50% admission, drops/tails/slices and metadata retention, one global floor with strict equality, missing/invalid probabilities and probability 1, Silero input preservation/no RMS fallback, combined raw/gain/window routing and paired floors, and forced-language refusal. Source and artifact consistency checks confirm unchanged config/corpus/other pre-existing edits. Samsara remained running. No shipped gate change and no commit.
