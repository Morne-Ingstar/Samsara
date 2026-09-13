# The outcome chip — what just happened

The listening indicator shows a small coloured chip after everything you say or
do. It exists because a missed command, a mode switch, a refused commit and a
failed command used to sound like the same beep — and if you couldn't hear it,
you got nothing at all.

**The colour is the meaning:**

| Colour | Means | Examples |
|---|---|---|
| **Green** | It worked | `✓ switch window`, `typed`, `undone`, `Ava ✓` |
| **Red** | It failed, or it is recording right now | `MISS`, `✗ no audio`, `REC` |
| **Amber** | It was deliberately refused | `refused: focus lock`, `Ava: nothing to do` |
| **Teal** | A mode change, or work still in progress | `→ AVA`, `staged`, `Ava…`, `…` |

Most chips clear themselves after about two seconds (`typed` is quicker). A
**teal "in progress" chip stays** until the real result replaces it — so after
you release the hold key you see `…` until the result lands, and a hold that
captured nothing ends as `✗ no audio` instead of an unexplained beep.

## It shows even when the indicator is off

If you turned the listening indicator off in Settings, the pill stays hidden —
but the chip still appears, on its own, for as long as it would normally show,
then disappears again. Every result stays visible.

## Microphone lost

If your microphone disconnects and doesn't come back, a red **`mic lost`** chip
stays up (and wins over every other chip) until it reconnects. That's the
reason every recording is about to fail, so it isn't allowed to disappear.

## Every chip

The single source of truth is `samsara/session_modes.py` → `outcome_chip()`.
Its test enumerates every outcome kind straight from that file's source, so a
new kind added without a chip fails the test suite. This table is generated
from that mapping:

| Outcome | Chip | Kind | On screen |
|---|---|---|---|
| `command_miss` | MISS | error (red) | 1800 ms |
| `command_executed` | ✓ switch window *(first two words of the command)* | success (green) | 1800 ms |
| `hands_free_command_executed` | ✓ press tab | success (green) | 1800 ms |
| `mode_switch` | → COMMAND / → DICTATE / → AVA | accent (teal) | 1800 ms |
| `ava_entry_failed` | ✗ *reason* | error (red) | 1800 ms |
| `dictate_commit_failed` | ✗ *reason* | error (red) | 1800 ms |
| `prefix_switch_failed` | ✗ *reason* | error (red) | 1800 ms |
| `hands_free_command_failed` | ✗ *reason* | error (red) | 1800 ms |
| `dictate_commit_blocked_focus_lock` | refused: focus lock | warning (amber) | 1800 ms |
| `dictate_suppressed_focus_lock` | refused: focus lock | warning (amber) | 1800 ms |
| `hands_free_command_blocked` | refused: focus lock *(or `refused: blocked`)* | warning (amber) | 1800 ms |
| `dictate_commit_refused` | refused: unclear | warning (amber) | 1800 ms |
| `hands_free_command_refused` | refused: unclear | warning (amber) | 1800 ms |
| `dictate_commit_unavailable` | refused: nothing staged | warning (amber) | 1800 ms |
| `scratch_refuse` | refused: undo | warning (amber) | 1800 ms |
| `dictate_committed` | typed | success (green) | 900 ms |
| `dictate_injected` | typed | success (green) | 900 ms |
| `scratch_success` | undone | success (green) | 1800 ms |
| `dictate_staged` | staged | pending (teal) | until replaced |
| `ava_dispatched` | Ava… | pending (teal) | until replaced |
| `ava_rejected_not_substantive` | Ava: nothing to do | warning (amber) | 1800 ms |
| `empty`, `abort` | *(no chip)* | | |

*Reasons* come from the outcome's `reason` or `error` detail, cut to 24
characters; with neither, the outcome's own name is shown instead.

Chips outside the dispatch table: `REC` (red, whole hold), `…` (teal, from
releasing the hold until the result), `✗ no audio` / `✗ no speech` /
`✗ transcribe failed` / `✗ no mic` on the hold path, and `Ava ✓` when an Ava
reply lands.

An outcome that somehow isn't in the table still shows — as `? <kind>` in amber,
with a WARNING in the log naming it — so it gets noticed and mapped rather than
silently doing nothing.
