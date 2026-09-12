# Hands-free audio path -- adversarial review

Branch `feature/v0.22`. Read-only review; no source file was modified.

## Summary

The ring/consumer transport is sound, but the policy layered on top of it leaks state across
threads in ways the docstrings assert are safe and are not. One latch (`_await_wake_onset`) can
make hands-free permanently deaf with no log after the first frame. Two consumers can silently
run two poll threads on one `Reader` after a join timeout, splitting every utterance in half.
The wake lane has no dispatch queue and drops whole utterances under CPU load; the toggle lane
solved exactly this with a FIFO and the fix was never carried across. Ducking, the OWW gate, and
the long-dictation hard cap each have a path that is a no-op while reading as if it worked.

## Findings

| Sev | file:line | Trigger | Consequence |
|---|---|---|---|
| critical | `samsara/audio_engine/wake_consumer.py:687` | any wake session ends while `_vad_available` is False | `_await_wake_onset` can never clear; every frame returns early forever -- hands-free deaf until restart |
| high | `dictation.py:8758` | second wake utterance arrives while the first is still in Whisper | whole utterance silently dropped (debug log only); no queue on this lane |
| high | `samsara/audio_engine/wake_consumer.py:156` | `stop()`'s 2 s join times out, then `start()` runs | two poll threads share one `Reader`; frames split between them, utterances garbled |
| high | `samsara/audio_engine/dictation_consumer.py:103` | `drain()`'s 2 s join times out, then `activate()` runs | old drain thread resurrected by the new `_drain_stop`; two threads on one `Reader` |
| high | `samsara/audio_engine/wake_consumer.py:421` | any repeatable exception in `_process_frame` | 10 log lines/sec forever, session latched and deaf, no earcon -- the exact failure the handler 7 lines below fails loudly for |
| high | `dictation.py:9429` | `long_dictation` reaches its hard cap while audio is buffered | flush is a guaranteed no-op (`self.speech_buffer` is dead post-ACE); in-flight sentence is dropped |
| high | `samsara/audio_engine/wake_consumer.py:960` | one enabled `wake_profiles` entry exists | primary OWW gate disabled for every phrase, profile detectors never consulted, fallback warning cannot fire |
| high | `dictation.py:7642` | tray "Wake word" unticked with a buffered utterance in flight | `WakeConsumer.stop()` join + a synchronous Whisper decode run on the Qt GUI thread |
| high | `samsara/audio_engine/continuous_consumer.py:139` | any exception in `_process_frame` | poll thread dies, `_running`/`continuous_active` stay True -- continuous mode deaf, silently |
| medium | `samsara/audio_engine/wake_consumer.py:877` | too-short utterance discarded while a previous dispatch is decoding | `_close_hands_free_capture_duck(None)` clears *all* duck owners; media un-ducks mid-capture |
| medium | `samsara/audio_engine/wake_consumer.py:663` | Silero unavailable, or disabled after 50 errors | `silero_onset` is structurally False, so `_restart_wake_session_timer(speech_onset=True)` never fires; session dies at the first inactivity deadline mid-speech |
| medium | `dictation.py:8996` | "pause" spoken during `long_dictation` | dispatch thread writes `self.silence_start = None`, which the poll thread owns; current silence window restarts |
| medium | `samsara/audio_engine/wake_consumer.py:988` | any passive wake dispatch | `_hands_free_capture_duck_token` is set and never cleared; later closes pass a dead token instead of the live one |
| medium | `samsara/audio_engine/dictation_consumer.py:163` | hotkey pressed within 0.5 s of TTS ending | prebuffer was skipped, so the "ambient" noise floor is measured from live speech; release tail truncates early |
| medium | `samsara/audio_engine/continuous_consumer.py:179` | `commit_now()` from the keyboard thread during a silence run | `_is_speaking`/`_silence_start` read and written outside `_frames_lock`; dead-air frame prepended to the next buffer |
| medium | `dictation.py:6524` | two session-transition hotkey presses in the same instant | unlocked check-then-set on `_session_transition_inflight`; same shape at `dictation.py:10423` |
| medium | `samsara/streaming.py:1199` | preview tick starts just before a silence boundary | partial decode of up to 8 s holds `model_lock`; the authoritative utterance decode queues behind it |
| medium | `dictation.py:8403` | any hands-free utterance | live VAD uses `> 0.5`, the contiguous gate uses `_GATE_VAD_PROB = 0.45` on the same model; RMS is 0.03/0.01 hands-free vs 0.008/0.002 hold |
| medium | `dictation.py:8330` | Silero onset on the poll thread concurrent with a timer callback | `_wake_session_expires_at` / `_wake_session_inactivity_timer` read-modify-write with no lock; orphan timers leak |
| low | `samsara/audio_engine/wake_consumer.py:272` | reading the docstring | claims "never in-place mutation" but lines 746/794/846 call `.append`; the stated safety argument is false even though CPython saves it |
| low | `samsara/streaming.py:1110` | final lands while a preview tick renders | `self._finalized` mutated on the cmd-utt thread while the tick thread iterates it; a transcript line can vanish |
| low | `samsara/audio_engine/wake_consumer.py:138` | wake mode toggled off and on | `start()` does not reset `_silero_was_speech`, `_await_wake_onset`, `_wake_admission` |
| low | `samsara/audio_engine/dictation_consumer.py:155` | `drain_after_release()` or `stop_streaming()` before `activate*()` | `AttributeError` on `_frames_lock` / `_streaming_stop` |
| low | `dictation.py:2438` | n/a | `self.speech_buffer` and `self.buffer_lock` are dead; their only reader is the no-op flush above |
| low | `dictation.py:7879` | duck start loses a generation race | `owner_token` stays in `_hands_free_capture_duck_owners` with no ducker; blocks the next restore until an explicit close |

## Critical

**`_await_wake_onset` is a one-way latch when Silero is down** -- `samsara/audio_engine/wake_consumer.py:687`.

```python
if self._await_wake_onset:
    if not silero_onset:
        return
    self._await_wake_onset = False
```

`silero_onset` is computed only inside `if app._vad_available:` (line 663); when VAD is unavailable it
is unconditionally `False`. `_await_wake_onset` is set to `True` at line 469 whenever
`app._wake_rearm_needs_onset` is seen, and `dictation.py:9316` sets that flag on *every* wake-session
close. It is cleared in exactly two places: the assignment above, and `__init__` (line 131) -- notably
**not** in `start()`. So: run with the bundled Silero ONNX missing (`dictation.py:8077-8081` swallows the
load failure into `_vad_available = False`), or accumulate 50 inference errors (line 680-682 flips the
same flag permanently -- `_load_vad_model` is only called once, at `dictation.py:5208`), then end one
wake session. From the next frame on, `_process_frame` returns at line 689 before OWW and before speech
accumulation, forever, with no log line and no earcon. Toggling wake mode off and on does not clear it.
Fix: gate the latch on VAD availability -- when `not app._vad_available`, treat the first RMS-positive
frame after a quiet run as the required onset edge -- and reset `_await_wake_onset`/`_silero_was_speech`
in `start()` so a listener restart is a clean slate.

## High

**No dispatch queue on the wake lane; concurrent utterances are dropped** -- `dictation.py:8758`.

```python
if self._wake_transcription_in_progress:
    logging.debug("[WAKE] Transcription already in progress, skipping")
    return
self._wake_transcription_in_progress = True
_set_in_progress = True
```

Each silence-bounded wake utterance is dispatched on its own daemon thread (`wake_consumer.py:1013`).
With the default `base` model on CPU a decode routinely exceeds the 1.0 s wake-session chunk gap
(`_WAKE_SESSION_CHUNK_GAP_S`, `dictation.py:700`), so utterance N+1 hits this guard and is discarded --
a whole spoken sentence, on a lane whose documented contract (`dictation.py:8258`) is "each transcribed
utterance is delivered immediately". The toggle lane hit exactly this and fixed it with a FIFO drain
(`wake_consumer.py:123-127`: "prevents a fast next utterance from being silently dropped"); the fix was
never carried across. The flag is also unlocked check-then-set, and `_handle_command_mode_utterance`
writes the same attribute unconditionally (`dictation.py:7157`) and clears it in its `finally`
(`dictation.py:7331`) with no ownership tracking, so the toggle worker can clear a flag the wake lane
owns. Fix: give the wake lane the same bounded FIFO the toggle lane has, and make the in-progress flag
a per-lane owned token -- or drop it entirely and rely on `model_lock` -- rather than a shared bool.

**`WakeConsumer.stop()` can leave its poll thread alive, and `start()` then spawns a second** --
`samsara/audio_engine/wake_consumer.py:152`.

```python
def stop(self) -> list:
    app = self._app
    self._running = False
    if self._thread is not None:
        self._thread.join(timeout=2.0)
        self._thread = None
```

The join result is never checked. If the poll thread is blocked past 2 s -- `app._vad_is_speech` waiting
on `_vad_lock` held by a long `_buffer_has_contiguous_speech` scan (`dictation.py:8460`), or
`_open_hands_free_duck_safe` inside `SessionDucker.start()`, which since the P2 change talks to a child
process -- `stop()` returns with the thread still in `_process_frame`. `start()` then sees
`self._running == False`, sets it back to `True`, calls `snap_to_head()`, and spawns a second
`wake-consumer` thread. The old thread's `while self._running` is now true again, so both run
permanently against the single `self._reader`; `Reader.read_next()` advances one shared cursor, so each
thread gets roughly every other 100 ms frame and every utterance is assembled from half its audio.
`_release_wake_consumer` (`dictation.py:7640`) holds `_wake_consumer_lock` across this join, so the
window is easy to hit on a rapid toggle-off/toggle-on. Fix: have `stop()` return the join result, keep
a per-generation thread identity, and make `start()` refuse -- or wait -- while a previous thread is
still alive rather than assuming the join succeeded.

**`DictationSessionConsumer.activate()` resurrects a timed-out drain thread** --
`samsara/audio_engine/dictation_consumer.py:103`.

```python
self._frames.clear()
self._active         = True
self._epoch_at_start = None
self._drain_stop     = threading.Event()
self._frames_lock    = threading.Lock()
```

`_hold_drain_loop` re-reads `self._drain_stop` on every iteration (line 116). `drain()` and `cancel()`
both `join(timeout=2.0)` and ignore the result (lines 216, 244). If a hold's drain thread is starved
past 2 s -- entirely plausible while Whisper saturates the CPU -- `drain()` returns audio while that
thread is still looping. The next `activate()` then installs a *fresh, unset* `_drain_stop`, which the
surviving thread reads on its next iteration and happily continues on. Now two threads call
`self._reader.read_next()` and append to the same `self._frames`, and `activate()`'s `snap_to_head()`
plus `rewind()` moved the cursor under the old one. The result is a hold recording whose frames are
split between two loops. Fix: keep the `Event` bound to the thread that owns it (pass it into
`_hold_drain_loop` as an argument instead of reading `self.`), check `is_alive()` after the join, and
refuse to activate while a prior drain thread has not exited.

**The per-frame `except Exception` turns a hard failure into permanent silent deafness** --
`samsara/audio_engine/wake_consumer.py:421`.

```python
try:
    self._process_frame(frame)
except Exception as exc:
    logger.exception(f"[ERROR] Wake consumer frame error: {exc}")
    app = self._app
    self._close_hands_free_duck_safe(app, self._hands_free_capture_duck_token)
    self._hands_free_capture_duck_token = None
```

The loop-level handler immediately below (line 428) is explicitly designed to fail loud -- error earcon,
force-exit any latched session -- precisely so a dead consumer cannot leave a "zombie session nobody can
hear". This inner handler defeats that for the far more likely case: a *repeatable* per-frame error
(`app.echo_canceller` unset at line 612, a config value of the wrong shape at 631-658, an OWW detector
raising at 711) never escapes to the outer handler. Instead it logs a full traceback at 10 Hz while the
session stays latched and hears nothing, and it closes the capture duck on every frame, so a transient
error mid-utterance un-ducks while the buffer keeps accumulating. Fix: count consecutive frame errors
and, past a small threshold, re-raise into the loud loop-level handler; and do not touch the duck token
on a transient frame error unless the error actually aborted the utterance.

**The long-dictation hard-cap flush is a dead no-op** -- `dictation.py:9429`.

```python
# Force any in-buffer audio to dispatch NOW so the pending counter
# captures it. Without this, audio currently being captured but not
# yet flushed by VAD silence would be lost.
self._flush_speech_buffer_to_transcription()
```

`_flush_speech_buffer_to_transcription` reads `self.speech_buffer` (`dictation.py:9444-9448`). Since the
ACE migration nothing appends to `self.speech_buffer` anywhere in the tree -- the wake path accumulates
into `WakeConsumer._utterance_frames` instead -- so it always returns `False` on the empty check. The
comment states the exact consequence of it not running, and that is now the behaviour: the in-progress
buffer is untouched, `_maybe_finalize_dictation` proceeds to `_reset_wake_dictation`, which sets
`_wake_rearm_needs_onset` (`dictation.py:9316`), which makes the poll thread call `abort_utterance()`
(`wake_consumer.py:468`) and discard it. The user's last sentence before the cap is silently lost.
Fix: route the hard cap through the consumer -- add a `flush_utterance()` on `WakeConsumer` that pulls
`_utterance_frames` and dispatches via `_flush` -- and delete the dead `speech_buffer`/`buffer_lock`
pair so no future caller believes it is live.

**One enabled wake profile disables OWW pre-filtering for everything, silently** --
`samsara/audio_engine/wake_consumer.py:960`.

```python
if _primary_oww_eligible and not _primary_oww_hit and not _has_wake_profiles:
    if app._wake_detector is not None:
        app._wake_detector.reset()
    flight_recorder.record('session.dispatch', op='dropped', kind='oww_gate_no_hit')
    self._close_hands_free_duck_safe(app, owner_token)
```

Answering the scoped question directly: yes, the Whisper wake fallback activates for a phrase that has
a working OWW model. `_has_wake_profiles` is true as soon as any `wake_profiles` entry is enabled, and
that alone makes every non-hit buffer fall through to Whisper -- including buffers containing the
*primary* phrase, which `dictation.py:9040` then matches from the transcript with `oww_confirmed=False`.
Worse, the per-profile detectors this branch exists to accommodate are loaded into
`self._wake_profile_detectors` (`dictation.py:8144`) and **never read anywhere** -- grep returns only the
two write sites and the `__init__`. So a user who trains and ships a profile `.onnx` gets no pre-filter
for that phrase *and* loses the pre-filter for the primary phrase, with every buffer of room audio going
to Whisper instead. `_warn_wake_fallback_once` (`dictation.py:8104`) cannot surface this: it returns
early when the primary detector *is* available, which is exactly this case. Fix: consult
`_wake_profile_detectors` in the gate so a profile with a loaded model participates as a real
pre-filter, restrict the fallback to profiles whose detector entry is `None`, and emit the fallback
warning whenever any enabled profile is running model-less.

**Tray toggle runs a Whisper decode on the Qt GUI thread** -- `dictation.py:7642`.

```python
with self._wake_consumer_lock:
    self._wake_consumer_reasons.discard(reason)
    ...
    remaining = self._wake_consumer.stop()
if remaining and self.wake_word_triggered:
    self.process_wake_word_buffer(remaining, src_rate=16000)
```

`samsara/ui/tray_qt.py:346` wires the wake-word checkbox straight to `app.set_wake_word_enabled(checked)`
with no `post()` hop, so the whole chain -- `save_config()`, `stop_wake_word_mode()`,
`_release_wake_consumer('wake_word')` -- runs inline on the Qt main thread. That is up to a 2 s `join`
plus, when an utterance was in flight, a full synchronous `process_wake_word_buffer` holding
`model_lock`: seconds of frozen UI. This is the same shape as the hook-thread freezes the codebase
already documents (`dictation.py:6513`, and the `_dispatch_session_transition` worker introduced to fix
them). Fix: dispatch `set_wake_word_enabled`'s side effects to a worker the way
`_dispatch_session_transition` already does (`dictation.py:5551` already spawns it for one caller), and
make the tail-buffer decode asynchronous.

**`ContinuousConsumer` has no exception guard at any level** --
`samsara/audio_engine/continuous_consumer.py:139`.

```python
while self._running:
    if not app.continuous_active:
        time.sleep(0.005)
        continue
    frame = self._reader.read_next()
    if frame is EMPTY:
        time.sleep(0.005)
        continue
    self._process_frame(frame)
```

`_process_frame` calls into `app.echo_canceller.process` (line 162) and reads several config keys with
no protection, and neither the call site nor `_poll_loop` has a `try`. Any exception kills the poll
thread outright. `self._running` stays `True` and `app.continuous_active` stays `True`, so nothing
detects it: `stop()` joins an already-dead thread and returns cleanly, `start()` refuses to restart
because `self._running` is truthy, and the user sees continuous dictation stop responding with no earcon
and no log beyond the thread's death. `WakeConsumer` has both a per-frame and a loud loop-level handler
for this exact reason (`wake_consumer.py:428-460`); this consumer got neither. Fix: mirror
`WakeConsumer`'s loop-level handler -- log, error earcon, clear `continuous_active`, and stop the mode
so the failure is visible rather than a silent hang.

## Exception handlers in scope, and the state they leave behind

Every `except Exception` / bare `except` in the reviewed code:

- `wake_consumer.py:84` -- `wake_session_policy` falls back to the default for a malformed value. Benign.
- `wake_consumer.py:213` (`_open_hands_free_duck_safe`), `:230` (`_close_hands_free_duck_safe`), `:174`
  (`deactivate`) -- debug-logged. `_open` returning `None` on failure means the caller stores a `None`
  token, which later becomes the clear-all-owners close at `dictation.py:7916`. See the medium finding
  at `wake_consumer.py:877`.
- `wake_consumer.py:194`, `:483`, `:678`, `:818`, `:837` -- all wrap `app._vad_reset()`, which is a
  documented no-op (`dictation.py:8405`). Dead handlers.
- `wake_consumer.py:396` -- FIFO worker error: one utterance lost, session stays alive. Intended.
- `wake_consumer.py:421` -- see the high finding above; the worst of the set.
- `wake_consumer.py:428` -- loop-level, fails loud. Correct.
- `wake_consumer.py:449`, `:454`, `:459` -- bare `except: pass` around the loud-fail earcon and the two
  session force-exits. If `exit_command_mode()` raises here the session stays latched *and* the consumer
  is dead: the zombie state the handler exists to prevent, reintroduced by its own swallow.
- `wake_consumer.py:669` -- VAD inference error. Logs at most every 30 s; after 50 consecutive errors
  `_vad_available` is set `False` for the whole process with a single WARNING and no recovery path. This
  is the entry point to the critical finding and to the inactivity-extension finding.
- `wake_consumer.py:904` -- re-raises after recording; the `finally` still closes the duck. Correct.
- `continuous_consumer.py:109` -- `unregister_consumer` at shutdown. Benign.
- `dictation_consumer.py:434` -- same. Benign.
- `dictation.py:7733` (`_wake_gate_frozen`) -- fails toward "not frozen", so the adaptive gate resumes
  chasing a duck step. Documented and deliberate.
- `dictation.py:7779`, `:7810`, `:7883`, `:7952` -- duck engage/release failures are warned and
  swallowed. After `:7883` the owner token remains in `_hands_free_capture_duck_owners`, so the next
  restore is blocked until something calls close with `None`.
- `dictation.py:8077` -- VAD load failure. `_vad_available = False` for the whole process; feeds the
  critical finding.
- `dictation.py:9113` -- `process_wake_word_buffer`'s catch-all. Logs, writes a failed history entry,
  plays a system sound. Session state (`app_state`, `wake_word_triggered`) is left exactly as it was, so
  a failure mid-`wake_session` leaves the session open with the utterance dropped.
- `dictation.py:7319` -- `_handle_command_mode_utterance`'s catch-all. Earcon, session stays alive.
  Intended and documented.
- `dictation.py:7380`, `streaming.py:1159`, `:1170`, `:1208`, `:1239` -- gate/preview helpers, all
  display-only or fail-closed. Benign.

## Not reviewed

- `samsara/session_modes.py` -- `SessionModeManager.dispatch_utterance`, the abort/switch/scratch
  matchers, and `reset()`. Reached from `_handle_command_mode_utterance:7297` but outside the stated scope.
- `_handle_ava_command_utterance` and the Ava waterfall queue (`wake_consumer.py:922` dispatches into it).
- `_output_dictation` and the injection path (`dictation.py:8925/8941`), including the `pyautogui.press`
  at 8929.
- `samsara/audio_ducking.py` -- `SessionDucker.start`/`stop` internals and the P2 COM child process. I
  reasoned about call ordering and lock scope only, not about what `start()` can block on.
- `WakeWordDetector` (`detected`/`reset`/`is_available`) and the OWW threshold behaviour.
- `AudioCaptureEngine` -- device recovery, `bump_device_epoch` call sites, and who calls
  `_pause_session_inactivity_for_device_recovery`.
- `samsara/streaming.py` outside 1128-1253, including `DictatePreviewSession` construction/teardown and
  `_release_streaming_preview`.
- `_start_dictation_mode` / `_restart_dictation_timer` / `_maybe_finalize_dictation` beyond the hard-cap
  path traced for the finding above.
- `_apply_segment_quality_gates` and the hold-path hallucination stack, referenced by
  `HANDS_FREE_GATES_FINDINGS.md` but outside the listed line ranges.
- No tests were run (instructed not to; Samsara is running).

## Fixed 2026-09-10

The three requested consumer-lifecycle fixes close these findings (review
coordinates refer to the original traces above):

- **Critical — `samsara/audio_engine/wake_consumer.py:687`:** a listener with
  unavailable Silero now rearms on the first RMS-positive frame after observed
  quiet. Continuous positive RMS cannot rearm it. The first fallback rearm
  produces a WARNING. See the RMS onset block near `wake_consumer.py:809`.
- **Low — `samsara/audio_engine/wake_consumer.py:138`:** `start()` resets
  `_await_wake_onset`, `_silero_was_speech`, `_wake_admission`, RMS history and
  the pending app rearm flag. See `wake_consumer.py:156`.
- **High — `samsara/audio_engine/wake_consumer.py:152` / `:156`:** lifecycle
  operations are serialized; a timed-out join logs ERROR with the thread name
  and retains the thread reference. `start()` refuses a still-live poller
  before changing its running flag, stop event or Reader cursor. Each poller
  also retains its own stop event. `stop()` returns a list-compatible
  `WakeStopResult`: `.stopped` explicitly reports join success, while the list
  still supplies buffered audio to the existing app flush caller. A timeout
  returns no partial audio. See `wake_consumer.py:182`.
- **High — `samsara/audio_engine/dictation_consumer.py:103`:** `activate()`
  refuses a surviving drain thread, and `_hold_drain_loop` receives its stop
  event as an argument. `drain()` returns no audio on timeout and does not race
  the old thread with a synchronous Reader drain. Cancel and streaming use the
  same ownership checks, including exclusion between hold and streaming on the
  shared Reader. See `dictation_consumer.py:75`, `:82`, `:95`, and `:143`.
- **High — `samsara/audio_engine/wake_consumer.py:421` and
  `samsara/audio_engine/continuous_consumer.py:139`:** a frame-processing
  exception now exits the poll loop, logs one ERROR with traceback, clears
  running/capture/session state, releases the consumer's duck token and calls
  `on_fatal(exc)` once. Without an explicit callback, the app's error sound is
  used. Wake cleanup also clears queued captures and cancels session timers;
  failing app cleanup hooks cannot preserve the session latches. Remaining
  recurring frame diagnostics are limited to one per condition per five
  seconds (the existing VAD traceback limit remains 30 seconds). See
  `wake_consumer.py:464` and `continuous_consumer.py:169`.

Validation: 43 lifecycle tests and 21 policy tests passed (64 total), with fake
apps, an in-memory ring and Event-controlled threads; no audio device/model or
live `dictation` module was loaded. The policy tests were run from a temporary
copy using AST-extracted app methods because the repository's
`tests/test_wake_session_policy.py` still imports `dictation` and is outside
this task's allowed edit list. The exact requested gate remains pending that
test-import change. The original lifecycle tests now use the same extraction
method instead of importing the app.

Other review findings remain open, including wake-profile OWW gating, the
wake dispatch queue (addressed below) and Silero-only inactivity renewal. No running app was
stopped, no configuration was changed, and no commit was created.

The subsequent dictation-side implementation closes these additional findings:

- **High — wake dispatch / shared transcription flag:**
  `DictationApp.process_wake_word_buffer` now transfers every wake utterance to
  `samsara/audio_engine/wake_dispatch.py`'s single FIFO drain worker. The queue
  holds four waiting utterances by default, separately from the current decode;
  `wake_word.session.dispatch_queue_depth` overrides that depth. Overflow drops
  the oldest waiting utterance, emits a WARNING and `wake.dispatch_dropped`, and
  releases its duck owner and any pending-finalization count exactly once.
  Normal, tracked, and stop-tail dispatches use the same queue. Wake, toggle,
  and Ava now own separate identity tokens, so one lane's completion cannot
  clear another lane's state. The model lock also covers consumption of
  Whisper's lazy segment iterator in these three lanes. Decode failures do
  not strand the queue; disabled-listener and expired-session jobs are logged
  and discarded with their cleanup intact.
- **High — tray untick:** `set_wake_word_enabled` uses
  `thread_registry.spawn` for serialized settings work, including config
  persistence, listener teardown and `WakeConsumer.stop()`'s join. Rapid
  requests retain the newest requested setting. Explicit disable discards
  the remaining capture with a log and flight event. Other triggered releases
  submit their final buffer to the wake FIFO; no final decode runs inline.
- **Medium — session timer race:** session start, onset renewal, expiry and
  reset share `_wake_session_lock` (an RLock). Replacement cancels the old
  timer and publishes the new expiry/timer under that lock. Each callback
  carries a timer identity as well as the session identity, so a cancelled
  callback cannot expire its replacement. Fatal-consumer timer cleanup uses
  the same app lock.
- **Medium/low — duck ownership:** the consumer's safe close ignores `None`
  with a diagnostic, and app close treats every unknown/stale owner as a
  logged no-op, even after ducking is disabled. Dispatch takes the capture
  token out of the consumer, and passive wake dispatch never stores its
  token as a subsequent capture owner. A ducker that loses the start-generation
  race removes its own owner and returns `None`, preserving a newer start's
  state and owners. Closing a live token releases only that owner.

Validation: `F:\envs\sami\python.exe -m pytest tests/test_wake_dispatch_lane.py
tests/test_wake_consumer_lifecycle.py -q -o addopts= --tb=short` passed **70 tests**
(27 dispatch tests plus 43 lifecycle tests). The dispatch tests compile named
production methods onto mocked apps without importing the `dictation` module;
they use controlled worker threads, fake timers and mocked decoders/duckers.
They cover ordered utterances 50 ms apart, fifth-waiting-item overflow,
cross-lane token isolation, GUI-thread exclusion and explicit tail discard,
concurrent onset/expiry, stale duck close, lost-generation cleanup, lazy decode
locking, worker failure recovery and queued-session cleanup. No models or
audio devices were loaded, and the full pytest suite was not run.

The thread-discipline scan reports one pre-existing, unrelated raw-thread site
at `tools/probes/hf_corpus_record.py:168`; it was left unchanged. No new raw
thread constructors were introduced. The running app, `config.json`, the
existing P2/memo edits and branch `feature/v0.22` were preserved; no commit
was created. The other findings above remain outside this implementation.

## Fixed 2026-09-10 (gate-unification follow-up)

- **Medium — `dictation.py:8403` (duplicated hold/hands-free gate
  constants):** the five VAD/RMS thresholds that were separately declared on
  the hold path and the hands-free path now have exactly one declaration
  each, in `samsara/constants.py:31-35`
  (`LIVE_VAD_PROB_THRESHOLD`, `CONTIGUOUS_VAD_PROB_THRESHOLD`,
  `ADAPTIVE_SPEECH_FLOOR_RATIO`, `HOLD_RELEASE_TAIL_SPEECH_THRESHOLD`,
  `WAKE_SPEECH_THRESHOLD_CAP`). `dictation.py:602-603`,
  `samsara/audio_engine/dictation_consumer.py:47-49` and
  `samsara/audio_engine/wake_consumer.py:47` import from there instead of
  redeclaring their own literal. Values are unchanged -- this closes the
  silent-drift risk, not the (intentional) hold/hands-free gap itself. Test:
  `tests/test_hf_review_mediums.py::test_gate_constants_share_one_declaration`.
- **Medium — `samsara/streaming.py:1199` (now `:1205`):**
  `DictatePreviewSession._transcribe_partial` used a blocking `model_lock`
  acquire, so an up-to-8s partial decode could queue ahead of the
  authoritative utterance decode. It now does `lock.acquire(blocking=False)`
  and skips the tick entirely when the lock is held. Test:
  `tests/test_hf_review_mediums.py::test_preview_partial_skips_tick_instead_of_blocking_model_lock`.
- **Low — `samsara/audio_engine/wake_consumer.py:272` (now `:352`):** the
  `_utterance_frames` docstring claimed "never in-place mutation", which
  lines 746/794/846 (as numbered in the original trace) contradicted. The
  docstring now states plainly that `_process_frame` mutates the list via
  `.append()` and that safety comes from that append plus the `list(...)`
  snapshot each being atomic under the GIL, not from avoiding mutation.
  Documentation-only; no behavior changed, so no new test.
- **Low — `samsara/streaming.py:1110`:** `on_utterance_final` and the
  partial-tick path now hand the overlay `list(self._finalized)` instead of
  the live list, so a render mid-mutation cannot observe a torn list. Test:
  `tests/test_hf_review_mediums.py::test_finalized_transcript_snapshot_is_independent_copy`.
- **Low — `samsara/audio_engine/dictation_consumer.py:155`:**
  `drain_after_release()` / `stop_streaming()` called before any
  `activate*()` used to raise `AttributeError` on `_frames_lock` /
  `_streaming_stop`. Both are now assigned in `__init__`
  (`dictation_consumer.py:69`, `:73`), so a pre-activate call is a no-op
  instead of a crash. Test:
  `tests/test_hf_review_mediums.py::test_release_and_stop_streaming_survive_before_any_activate`.

All other Medium findings from the table above (`wake_consumer.py:663`,
`:960`, `:877`, `:988`; `dictation.py:9429`, `:8996`, `:6524`;
`dictation_consumer.py:163`; `continuous_consumer.py:179`) were already
closed by the two "Fixed 2026-09-10" passes above this section, each with
its own regression test in `tests/test_hf_review_mediums.py`.

### Deferred

- **Low — `dictation.py:7879` (duck-open generation race leaving a stale
  owner token):** covered narratively by the "Medium/low -- duck ownership"
  bullet in the first Fixed pass above (a ducker that loses the
  start-generation race now removes its own owner and returns `None`), but
  has no dedicated regression test of its own in
  `tests/test_hf_review_mediums.py`. Deferred rather than added here because
  reproducing the generation-race window needs the existing
  `tests/test_hands_free_capture_ducking.py` harness, not the lightweight
  fakes this file uses, and that harness's fixtures currently pre-date
  unrelated app attributes added since (see "not done" in the session
  report) -- fixing it properly means updating that harness, which is
  outside this pass's scope.
- **Everything under "Not reviewed" above:** unchanged; still out of scope
  for this pass (no new code in `session_modes.py`, the Ava waterfall,
  `_output_dictation`, `audio_ducking.py` internals, `WakeWordDetector`, or
  `AudioCaptureEngine` was reviewed here).
