# Queue 44 run 2: "and" -> "nd" -- prompt tail vs onset clipping

Each token goes through every arm unchanged, so arms compare like with like. Lane buffer shape: 500 ms lead-in + word + 700 ms tail (toggle-DICTATE). Decode = owner lane params (performance_mode=accurate, language=en, vad_filter=False, beam 5). Prompt tails rebuilt from the 14:08:37-14:12:30 live log; pending_chars verified at all 53 staged steps.

- **owner**: real word-initial "and" from ~/.samsara/debug hotkey dumps, kept only if the cut decodes to exactly "and" with no prompt.
- **sapi_normal / sapi_soft**: Windows SAPI "and" (6 voices x 3 rates), word RMS 0.03 / -12 dB, white room noise at the owner calibrated floor 0.00125. Synthetic voice: tests the decoder mechanism, not the owner accent.
- **clipN**: buffer starts N ms into the word (the brief's hypothesis), no lead-in.

## owner (n=1)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| intact/no_prompt | 1 | 0 | 0 | 0 |
| intact/tail_14:09:20 | 0 | 1 | 0 | 0 |
| intact/tail_14:11:36 | 1 | 0 | 0 | 0 |
| intact/tail_14:11:40 | 0 | 0 | 1 | 0 |
| intact/tail_14:11:45 | 0 | 1 | 0 | 0 |
| intact/tail_14:11:55 | 0 | 1 | 0 | 0 |
| clip40/no_prompt | 0 | 0 | 1 | 0 |
| clip40/tail_14:11:36 | 0 | 0 | 1 | 0 |
| clip80/no_prompt | 0 | 0 | 1 | 0 |
| clip80/tail_14:11:36 | 0 | 0 | 1 | 0 |
| clip120/no_prompt | 0 | 0 | 1 | 0 |
| clip120/tail_14:11:36 | 0 | 0 | 1 | 0 |

## sapi_normal (n=18)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| intact/no_prompt | 10 | 0 | 8 | 0 |
| intact/tail_14:09:20 | 5 | 8 | 5 | 0 |
| intact/tail_14:11:36 | 18 | 0 | 0 | 0 |
| intact/tail_14:11:40 | 14 | 4 | 0 | 0 |
| intact/tail_14:11:45 | 2 | 16 | 0 | 0 |
| intact/tail_14:11:55 | 0 | 18 | 0 | 0 |
| clip40/no_prompt | 5 | 0 | 12 | 1 |
| clip40/tail_14:11:36 | 7 | 11 | 0 | 0 |
| clip80/no_prompt | 1 | 0 | 16 | 1 |
| clip80/tail_14:11:36 | 3 | 15 | 0 | 0 |
| clip120/no_prompt | 0 | 0 | 18 | 0 |
| clip120/tail_14:11:36 | 2 | 14 | 2 | 0 |

## sapi_soft (n=18)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| intact/no_prompt | 5 | 0 | 13 | 0 |
| intact/tail_14:09:20 | 5 | 4 | 9 | 0 |
| intact/tail_14:11:36 | 14 | 0 | 4 | 0 |
| intact/tail_14:11:40 | 13 | 0 | 5 | 0 |
| intact/tail_14:11:45 | 1 | 13 | 4 | 0 |
| intact/tail_14:11:55 | 0 | 18 | 0 | 0 |
| clip40/no_prompt | 1 | 0 | 14 | 3 |
| clip40/tail_14:11:36 | 7 | 8 | 3 | 0 |
| clip80/no_prompt | 0 | 0 | 12 | 6 |
| clip80/tail_14:11:36 | 5 | 11 | 2 | 0 |
| clip120/no_prompt | 0 | 0 | 16 | 2 |
| clip120/tail_14:11:36 | 2 | 14 | 2 | 0 |

## ALL (n=37)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| intact/no_prompt | 16 | 0 | 21 | 0 |
| intact/tail_14:09:20 | 10 | 13 | 14 | 0 |
| intact/tail_14:11:36 | 33 | 0 | 4 | 0 |
| intact/tail_14:11:40 | 27 | 4 | 6 | 0 |
| intact/tail_14:11:45 | 3 | 30 | 4 | 0 |
| intact/tail_14:11:55 | 0 | 37 | 0 | 0 |
| clip40/no_prompt | 6 | 0 | 27 | 4 |
| clip40/tail_14:11:36 | 14 | 19 | 4 | 0 |
| clip80/no_prompt | 1 | 0 | 29 | 7 |
| clip80/tail_14:11:36 | 8 | 26 | 3 | 0 |
| clip120/no_prompt | 0 | 0 | 35 | 2 |
| clip120/tail_14:11:36 | 4 | 28 | 5 | 0 |

## Prompt tails (last 200 chars of the pending buffer before the utterance)

- **14:09:20** (live decode of a standalone "and": `and..`): `...your main. apps uh.. with. number designations next to each one. so like 1, 2, 3, 4, 5, 6.`
- **14:11:36** (live decode of a standalone "and": `and`): `...r it to be because people could start their sentence with one or two etc yeah I don't know`
- **14:11:40** (live decode of a standalone "and": `nd`): `... to be because people could start their sentence with one or two etc yeah I don't know and`
- **14:11:45** (live decode of a standalone "and": `nd`): `... be because people could start their sentence with one or two etc yeah I don't know and nd`
- **14:11:55** (live decode of a standalone "and": `nd`): `...cause people could start their sentence with one or two etc yeah I don't know and nd nd nd`

## Per token

- [owner] `hotkey_20260911T185540_816130.wav` word 160 ms, rms 0.06047: intact/no_prompt `and`; intact/tail_14:09:20 `nd..`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `then`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `and I'll see you in the next one. Bye.`; clip40/tail_14:11:36 `I don't know`; clip80/no_prompt `and I'll see you in the next one. Bye.`; clip80/tail_14:11:36 `you`; clip120/no_prompt `Thanks for watching!`; clip120/tail_14:11:36 `you`
- [sapi_normal] `MicrosoftDavidDesktop_r-2.wav` word 410 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `and`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftDavidDesktop_r-2.wav` word 410 ms, rms 0.00754: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `you`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `and`
- [sapi_normal] `MicrosoftDavidDesktop_r0.wav` word 340 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `and`
- [sapi_soft] `MicrosoftDavidDesktop_r0.wav` word 340 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftDavidDesktop_r2.wav` word 265 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `and..`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `Thank you for joining us. Have a great weekend.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftDavidDesktop_r2.wav` word 265 ms, rms 0.00754: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `in.`; clip120/tail_14:11:36 `it couldn't be in`
- [sapi_normal] `MicrosoftGeorge_r-2.wav` word 400 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftGeorge_r-2.wav` word 400 ms, rms 0.00754: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftGeorge_r0.wav` word 325 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `nd`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftGeorge_r0.wav` word 325 ms, rms 0.00754: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftGeorge_r2.wav` word 265 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `Thank you for your attention.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftGeorge_r2.wav` word 265 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `Thank you very much. Have a great weekend.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftHazelDesktop_r-2.wav` word 495 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `And.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftHazelDesktop_r-2.wav` word 495 ms, rms 0.00754: intact/no_prompt `out`; intact/tail_14:09:20 `out.`; intact/tail_14:11:36 `oud`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `oud`; intact/tail_14:11:55 `nd`; clip40/no_prompt `out.`; clip40/tail_14:11:36 `oud`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftHazelDesktop_r0.wav` word 400 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `and.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftHazelDesktop_r0.wav` word 400 ms, rms 0.00754: intact/no_prompt `out`; intact/tail_14:09:20 `ouch`; intact/tail_14:11:36 `oud`; intact/tail_14:11:40 `oud`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `out.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt ``; clip80/tail_14:11:36 `I don't know`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftHazelDesktop_r2.wav` word 325 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `nd`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `and .`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftHazelDesktop_r2.wav` word 325 ms, rms 0.00754: intact/no_prompt `Out.`; intact/tail_14:09:20 `ouch.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `oud`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt ``; clip40/tail_14:11:36 `nd`; clip80/no_prompt `Thank you for your time.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt ``; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftMark_r-2.wav` word 440 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `and`
- [sapi_soft] `MicrosoftMark_r-2.wav` word 440 ms, rms 0.00754: intact/no_prompt `and`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `and`
- [sapi_normal] `MicrosoftMark_r0.wav` word 360 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `end`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftMark_r0.wav` word 360 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `end.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftMark_r2.wav` word 290 ms, rms 0.03: intact/no_prompt `end`; intact/tail_14:09:20 `end.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `and`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `and`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftMark_r2.wav` word 290 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `end.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `and`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `you`; clip120/no_prompt `Thank you for your attention.`; clip120/tail_14:11:36 `it couldn't be in`
- [sapi_normal] `MicrosoftSusan_r-2.wav` word 480 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `and.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `and`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftSusan_r-2.wav` word 480 ms, rms 0.00754: intact/no_prompt `Out.`; intact/tail_14:09:20 `out`; intact/tail_14:11:36 `oud`; intact/tail_14:11:40 `oud`; intact/tail_14:11:45 `oud`; intact/tail_14:11:55 `nd`; clip40/no_prompt `And.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftSusan_r0.wav` word 390 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `nd`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `and.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftSusan_r0.wav` word 390 ms, rms 0.00754: intact/no_prompt `out`; intact/tail_14:09:20 `ouch`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt ``; clip40/tail_14:11:36 `yeah I don't know`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftSusan_r2.wav` word 330 ms, rms 0.03: intact/no_prompt `and`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `nd`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt ``; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `yeah I don't know`
- [sapi_soft] `MicrosoftSusan_r2.wav` word 330 ms, rms 0.00754: intact/no_prompt `out`; intact/tail_14:09:20 `ouch.`; intact/tail_14:11:36 `I`; intact/tail_14:11:40 `I`; intact/tail_14:11:45 `I`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt ``; clip40/tail_14:11:36 `I don't know`; clip80/no_prompt ``; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftZiraDesktop_r-2.wav` word 475 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `end`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `End.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftZiraDesktop_r-2.wav` word 475 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `and.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `and`; intact/tail_14:11:55 `nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftZiraDesktop_r0.wav` word 400 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `end`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `end`
- [sapi_soft] `MicrosoftZiraDesktop_r0.wav` word 400 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `nd.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `you`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `end.`; clip120/tail_14:11:36 `nd`
- [sapi_normal] `MicrosoftZiraDesktop_r2.wav` word 320 ms, rms 0.03: intact/no_prompt `End.`; intact/tail_14:09:20 `end.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd nd`; clip40/no_prompt `end.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt `Thank you for your time. Have a great weekend.`; clip120/tail_14:11:36 `nd`
- [sapi_soft] `MicrosoftZiraDesktop_r2.wav` word 320 ms, rms 0.00754: intact/no_prompt `End.`; intact/tail_14:09:20 `end.`; intact/tail_14:11:36 `and`; intact/tail_14:11:40 `and`; intact/tail_14:11:45 `nd`; intact/tail_14:11:55 `nd nd`; clip40/no_prompt `End.`; clip40/tail_14:11:36 `nd`; clip80/no_prompt `end.`; clip80/tail_14:11:36 `nd`; clip120/no_prompt ``; clip120/tail_14:11:36 `nd`

Skipped owner clips: {'owner:no_quiet_room_or_onset': 27, 'owner:cut_not_heard_as_and_without_prompt': 7, 'owner:first_word_not_and': 9}
