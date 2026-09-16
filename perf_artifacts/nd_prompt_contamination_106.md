# Queue 106: the same tails, raw and sanitised

Paired arms on ONE run: every token goes through the no-prompt floor, each of the five real 200-char tails queue 44 rebuilt from the live log, and the same five tails put through dictation.py `_sanitise_context_tail` (lifted from source, not reimplemented). Clips, cuts, room tone, lane decode parameters and the bucketing are imported unchanged from nd_prompt_contamination.py. pending_chars verified at all 53 staged steps.

## owner (n=1)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| no_prompt | 1 | 0 | 0 | 0 |
| tail_14:09:20/raw | 0 | 1 | 0 | 0 |
| tail_14:09:20/clean | 1 | 0 | 0 | 0 |
| tail_14:11:36/raw | 1 | 0 | 0 | 0 |
| tail_14:11:36/clean | 1 | 0 | 0 | 0 |
| tail_14:11:40/raw | 0 | 0 | 1 | 0 |
| tail_14:11:40/clean | 0 | 0 | 1 | 0 |
| tail_14:11:45/raw | 0 | 1 | 0 | 0 |
| tail_14:11:45/clean | 0 | 1 | 0 | 0 |
| tail_14:11:55/raw | 0 | 1 | 0 | 0 |
| tail_14:11:55/clean | 0 | 0 | 1 | 0 |

## sapi_normal (n=18)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| no_prompt | 10 | 0 | 8 | 0 |
| tail_14:09:20/raw | 5 | 8 | 5 | 0 |
| tail_14:09:20/clean | 13 | 0 | 5 | 0 |
| tail_14:11:36/raw | 18 | 0 | 0 | 0 |
| tail_14:11:36/clean | 18 | 0 | 0 | 0 |
| tail_14:11:40/raw | 14 | 4 | 0 | 0 |
| tail_14:11:40/clean | 15 | 3 | 0 | 0 |
| tail_14:11:45/raw | 2 | 16 | 0 | 0 |
| tail_14:11:45/clean | 4 | 14 | 0 | 0 |
| tail_14:11:55/raw | 0 | 18 | 0 | 0 |
| tail_14:11:55/clean | 15 | 3 | 0 | 0 |

## sapi_soft (n=18)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| no_prompt | 5 | 0 | 13 | 0 |
| tail_14:09:20/raw | 5 | 4 | 9 | 0 |
| tail_14:09:20/clean | 8 | 0 | 10 | 0 |
| tail_14:11:36/raw | 14 | 0 | 4 | 0 |
| tail_14:11:36/clean | 13 | 0 | 5 | 0 |
| tail_14:11:40/raw | 13 | 0 | 5 | 0 |
| tail_14:11:40/clean | 13 | 0 | 5 | 0 |
| tail_14:11:45/raw | 1 | 13 | 4 | 0 |
| tail_14:11:45/clean | 1 | 13 | 4 | 0 |
| tail_14:11:55/raw | 0 | 18 | 0 | 0 |
| tail_14:11:55/clean | 14 | 0 | 4 | 0 |

## ALL (n=37)

| arm | and | nd | other | empty |
|---|---|---|---|---|
| no_prompt | 16 | 0 | 21 | 0 |
| tail_14:09:20/raw | 10 | 13 | 14 | 0 |
| tail_14:09:20/clean | 22 | 0 | 15 | 0 |
| tail_14:11:36/raw | 33 | 0 | 4 | 0 |
| tail_14:11:36/clean | 32 | 0 | 5 | 0 |
| tail_14:11:40/raw | 27 | 4 | 6 | 0 |
| tail_14:11:40/clean | 28 | 3 | 6 | 0 |
| tail_14:11:45/raw | 3 | 30 | 4 | 0 |
| tail_14:11:45/clean | 5 | 28 | 4 | 0 |
| tail_14:11:55/raw | 0 | 37 | 0 | 0 |
| tail_14:11:55/clean | 29 | 3 | 5 | 0 |

## What the sanitiser did to each tail

- **14:09:20** (live decode of a standalone "and": `and..`)
  - raw   `...your main. apps uh.. with. number designations next to each one. so like 1, 2, 3, 4, 5, 6.`
  - clean `...your main. apps uh.. with. number designations next to each one. so like 1, 2, 3, 4, 5, 6.`
- **14:11:36** (live decode of a standalone "and": `and`)
  - raw   `...r it to be because people could start their sentence with one or two etc yeah I don't know`
  - clean `...r it to be because people could start their sentence with one or two etc yeah I don't know`
- **14:11:40** (live decode of a standalone "and": `nd`)
  - raw   `... to be because people could start their sentence with one or two etc yeah I don't know and`
  - clean `... to be because people could start their sentence with one or two etc yeah I don't know and`
- **14:11:45** (live decode of a standalone "and": `nd`)
  - raw   `... be because people could start their sentence with one or two etc yeah I don't know and nd`
  - clean `... be because people could start their sentence with one or two etc yeah I don't know and nd`
- **14:11:55** (live decode of a standalone "and": `nd`)
  - raw   `...cause people could start their sentence with one or two etc yeah I don't know and nd nd nd`
  - clean `... to be because people could start their sentence with one or two etc yeah I don't know and`

## Per token

- [owner] `hotkey_20260911T185540_816130.wav` word 160 ms, rms 0.06047: no_prompt `and`; tail_14:09:20/raw `nd..`; tail_14:09:20/clean `and..`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `then`; tail_14:11:40/clean `then`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `then`
- [sapi_normal] `MicrosoftDavidDesktop_r-2.wav` word 410 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `and`; tail_14:11:45/clean `and`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftDavidDesktop_r-2.wav` word 410 ms, rms 0.00754: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `you`; tail_14:11:40/clean `you`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `you`
- [sapi_normal] `MicrosoftDavidDesktop_r0.wav` word 340 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `and`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftDavidDesktop_r0.wav` word 340 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftDavidDesktop_r2.wav` word 265 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `and..`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftDavidDesktop_r2.wav` word 265 ms, rms 0.00754: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftGeorge_r-2.wav` word 400 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftGeorge_r-2.wav` word 400 ms, rms 0.00754: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftGeorge_r0.wav` word 325 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `nd`; tail_14:11:40/clean `nd`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `nd`
- [sapi_soft] `MicrosoftGeorge_r0.wav` word 325 ms, rms 0.00754: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftGeorge_r2.wav` word 265 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftGeorge_r2.wav` word 265 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftHazelDesktop_r-2.wav` word 495 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftHazelDesktop_r-2.wav` word 495 ms, rms 0.00754: no_prompt `out`; tail_14:09:20/raw `out.`; tail_14:09:20/clean `out`; tail_14:11:36/raw `oud`; tail_14:11:36/clean `out`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `oud`; tail_14:11:45/clean `oud`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftHazelDesktop_r0.wav` word 400 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftHazelDesktop_r0.wav` word 400 ms, rms 0.00754: no_prompt `out`; tail_14:09:20/raw `ouch`; tail_14:09:20/clean `out`; tail_14:11:36/raw `oud`; tail_14:11:36/clean `oud`; tail_14:11:40/raw `oud`; tail_14:11:40/clean `oud`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftHazelDesktop_r2.wav` word 325 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `nd`; tail_14:11:40/clean `nd`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `nd`
- [sapi_soft] `MicrosoftHazelDesktop_r2.wav` word 325 ms, rms 0.00754: no_prompt `Out.`; tail_14:09:20/raw `ouch.`; tail_14:09:20/clean `out.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `oud`; tail_14:11:40/clean `oud`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `oud`
- [sapi_normal] `MicrosoftMark_r-2.wav` word 440 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftMark_r-2.wav` word 440 ms, rms 0.00754: no_prompt `and`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftMark_r0.wav` word 360 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `end`; tail_14:09:20/clean `end`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftMark_r0.wav` word 360 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `end.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftMark_r2.wav` word 290 ms, rms 0.03: no_prompt `end`; tail_14:09:20/raw `end.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `and`; tail_14:11:45/clean `and`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftMark_r2.wav` word 290 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `end.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftSusan_r-2.wav` word 480 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftSusan_r-2.wav` word 480 ms, rms 0.00754: no_prompt `Out.`; tail_14:09:20/raw `out`; tail_14:09:20/clean `out`; tail_14:11:36/raw `oud`; tail_14:11:36/clean `oud`; tail_14:11:40/raw `oud`; tail_14:11:40/clean `oud`; tail_14:11:45/raw `oud`; tail_14:11:45/clean `oud`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `oud`
- [sapi_normal] `MicrosoftSusan_r0.wav` word 390 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `nd`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftSusan_r0.wav` word 390 ms, rms 0.00754: no_prompt `out`; tail_14:09:20/raw `ouch`; tail_14:09:20/clean `out`; tail_14:11:36/raw `and`; tail_14:11:36/clean `oud`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftSusan_r2.wav` word 330 ms, rms 0.03: no_prompt `and`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `nd`; tail_14:11:40/clean `nd`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `nd`
- [sapi_soft] `MicrosoftSusan_r2.wav` word 330 ms, rms 0.00754: no_prompt `out`; tail_14:09:20/raw `ouch.`; tail_14:09:20/clean `ouch.`; tail_14:11:36/raw `I`; tail_14:11:36/clean `I`; tail_14:11:40/raw `I`; tail_14:11:40/clean `I`; tail_14:11:45/raw `I`; tail_14:11:45/clean `I`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `I`
- [sapi_normal] `MicrosoftZiraDesktop_r-2.wav` word 475 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `end`; tail_14:09:20/clean `end`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftZiraDesktop_r-2.wav` word 475 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `and.`; tail_14:09:20/clean `and.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `and`; tail_14:11:45/clean `and`; tail_14:11:55/raw `nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftZiraDesktop_r0.wav` word 400 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `end`; tail_14:09:20/clean `end`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `and`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftZiraDesktop_r0.wav` word 400 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `nd.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `you`; tail_14:11:45/clean `you`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`
- [sapi_normal] `MicrosoftZiraDesktop_r2.wav` word 320 ms, rms 0.03: no_prompt `End.`; tail_14:09:20/raw `end.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd nd`; tail_14:11:55/clean `and`
- [sapi_soft] `MicrosoftZiraDesktop_r2.wav` word 320 ms, rms 0.00754: no_prompt `End.`; tail_14:09:20/raw `end.`; tail_14:09:20/clean `end.`; tail_14:11:36/raw `and`; tail_14:11:36/clean `and`; tail_14:11:40/raw `and`; tail_14:11:40/clean `and`; tail_14:11:45/raw `nd`; tail_14:11:45/clean `nd`; tail_14:11:55/raw `nd nd`; tail_14:11:55/clean `and`

Skipped owner clips: {'owner:no_quiet_room_or_onset': 27, 'owner:cut_not_heard_as_and_without_prompt': 7, 'owner:first_word_not_and': 9}
