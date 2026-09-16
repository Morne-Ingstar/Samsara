> **Taint lifted (2026-09-15, queue 44 run 2):** this was marked TAINTED on 2026-09-14 because the interface gain was believed broken at 60%. Queue 67 then measured the calibration floor at 0.00125 against an independent 0.00118 and found the wizard, not the mic, was at fault; the owner's brief states this evidence was NOT taken at a broken gain. It is used for the wake-session-lane verdict in `reports\44\FINAL.md`.

# Queue 44: where the discard boundary lands on the owner's real speech onsets (offline replay)

First word of recent hotkey clips (no isolated end words exist on disk). Speech lost = ms from the waveform word start to the retained-buffer start under policy=discard, at frame phases 0/25/50/75 ms. Discarded ahead of the onset is always the full 1500 ms rewind. First word re-transcribed from the worst-phase retained buffer.

| clip | variant | lost ms by phase | worst starts mid-word | buffer RMS | first word (full) | first word (discard, worst) | same | first word (keep, same window) | same |
|---|---|---|---|---|---|---|---|---|---|
| 20260914T064721 | normal | [-55, -30, -5, 120] | yes | 0.0040 | did | everything | N | did | Y |
| 20260914T064721 | soft -12 dB | [645, None, None, 1020] | yes | 0.0008 | did | said | N | did | Y |
| 20260914T064553 | normal | [95, 120, 145, 270] | yes | 0.0041 | weird | now | N | weird | Y |
| 20260914T064553 | soft -12 dB | [495, 120, 2845, 2870] | yes | 0.0008 | weird | see | N | local | N |
| 20260914T063914 | normal | [-60, -35, -10, 415] | yes | 0.0016 | he | it | N | he | Y |
| 20260914T063914 | soft -12 dB | [340, 365, None, 415] | yes | 0.0004 | he | set | N | he | Y |
| 20260914T063819 | normal | [270, 295, 320, 245] | yes | 0.0041 | forcing | forcing | Y | by | N |
| 20260914T063819 | soft -12 dB | [270, 395, 520, 245] | yes | 0.0008 | forcing | me | N | forcing | Y |
| 20260914T063808 | normal | [310, 335, 360, 285] | yes | 0.0031 | by | letting | N | by | Y |
| 20260914T063808 | soft -12 dB | [610, 435, None, None] | yes | 0.0007 | by | thank | N | by | Y |
| 20260914T063751 | normal | [15, 40, -35, 90] | yes | 0.0039 | you | cheated | N | you | Y |
| 20260914T063751 | soft -12 dB | [215, 240, None, None] | yes | 0.0007 | you | (empty) | N | you | Y |
| 20260914T063748 | normal | [-60, -35, -10, 215] | yes | 0.0044 | that | more | N | that | Y |
| 20260914T063740 | normal | [15, 40, -35, 190] | yes | 0.0061 | he | shakes | N | he | Y |
| 20260914T063740 | soft -12 dB | [115, 40, 65, 1090] | yes | 0.0015 | he | shakes | N | he | Y |
| 20260914T063711 | normal | [-20, 5, 230, 255] | yes | 0.0038 | my | i | N | my | Y |
| 20260914T063654 | normal | [125, 450, 75, 300] | yes | 0.0034 | my | and | N | my | Y |
| 20260914T063654 | soft -12 dB | [125, None, None, None] | yes | 0.0010 | my | my | Y | my | Y |
| 20260914T063638 | normal | [110, 35, 160, 185] | yes | 0.0038 | i | the | N | i | Y |
| 20260914T063623 | normal | [110, 135, 160, 185] | yes | 0.0036 | i | may | N | i | Y |
| 20260914T063623 | soft -12 dB | [110, 835, 860, 3285] | no | 0.0007 | i | from | N | i | Y |
| 20260914T063456 | normal | [10, 35, None, None] | yes | 0.0038 | with | (empty) | N | with | Y |
| 20260914T063440 | normal | [90, 115, 40, 65] | yes | 0.0041 | with | our | N | with | Y |
| 20260914T063440 | soft -12 dB | [90, None, None, None] | no | 0.0010 | with | (empty) | N | with | Y |
| 20260914T063437 | normal | [-25, 0, 25, -50] | yes | 0.0049 | i | i | Y | i | Y |
| 20260914T063437 | soft -12 dB | [-25, 400, 225, 250] | yes | 0.0011 | i | with | N | i | Y |
| 20260914T063220 | normal | [90, 15, 40, -35] | yes | 0.0074 | can | to | N | can | Y |
| 20260914T063220 | soft -12 dB | [90, 315, 340, 365] | yes | 0.0017 | can | me | N | can | Y |
| 20260914T063127 | normal | [-50, -25, 300, 325] | yes | 0.0033 | he | it | N | he | Y |
| 20260914T063127 | soft -12 dB | [None, None, None, 325] | yes | 0.0008 | he | really | N | he | Y |
| 20260914T062841 | normal | [-40, -15, 10, -65] | yes | 0.0061 | first | first | Y | first | Y |
| 20260914T062841 | soft -12 dB | [-40, -15, None, None] | no | 0.0015 | first | first | Y | first | Y |
| 20260914T062457 | normal | [-70, -45, -20, 5] | yes | 0.0027 | today | today | Y | today | Y |
| 20260914T062441 | normal | [135, 160, 85, 210] | yes | 0.0024 | i | compliments | N | i | Y |
| 20260914T062441 | soft -12 dB | [None, 5060, None, None] | no | 0.0006 | i | (empty) | N | deserve | N |

**normal:** n=20; worst-phase speech lost median 188 ms, max 450 ms; boundary after word start in 20/20; retained buffer starts mid-word in 20/20; first word changed under discard in 16/20, under keep (control) in 1/20.

**soft -12 dB:** n=15; worst-phase speech lost median 415 ms, max 5060 ms; boundary after word start in 14/15; retained buffer starts mid-word in 11/15; first word changed under discard in 13/15, under keep (control) in 2/15.
