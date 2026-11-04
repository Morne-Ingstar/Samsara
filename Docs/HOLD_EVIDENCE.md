# Read one held-dictation incident

Join the original `[TAIL_CAPTURE]`, `[HOLD_FINAL]`, `[DELIVERY]` records and
the incident JSON sidecar by `hold_seq_id`. The ID is a process-local increasing
sequence plus a short random process suffix. It carries no words or owner ID.
The instrumented incident lane is StreamingSession (ordinary Full-display holds
use it too). Legacy batch final logs join the capture identity when available;
they do not gain a second incident recorder. Missing observations stay unknown.

Incident recording remains local, opt-in through
`diagnostics.keep_incident_holds` (default off), with at most five WAV/JSON pairs.
It is independent of benchmark recording and its 200-sample cap. Consent off
creates no incident store or incident files. Clear invalidates old holds and
queued receipt updates; neither late completion nor late paste restores a slot.
Sidecars contain counts, timings, fixed reason enums and decoder parameters,
never dictated text. The existing benchmark feature has its separate consent
and text policy; this change does not enable it.

| Fields | Meaning |
| --- | --- |
| `captured_audio_ms`, `release_to_stop_ms`, `cutoff_set` | Final admitted PCM duration, elapsed release to final drain, and whether capture cutoff was set. Tail snapshots are retained even with DEBUG logging off; `[TAIL_CAPTURE]` itself remains DEBUG. |
| `last_speech_end_ms`, `last_speech_source` | Last VAD frame above the existing p>.1 floor (`vad_p01`), otherwise the existing tail RMS floor (`energy_tail_floor`). VAD ends are 32 ms bounds; energy ends are 100 ms bounds. Energy is activity, not a speech verdict. `unknown`/`-1` means no observation. |
| `speech_present` | Existing whole-buffer presence decision: 1 above the existing speech floor, 0 confirmed below, -1 unknown. It does not change admission. |
| `cue_or_duck_overlap_ms`, `cue_events`, `duck_events` | Union of queued cue-duration intervals and synchronous duck engage/restore operation intervals that began while `recording=True`, clipped at capture stop. A duck's sustained attenuation is not an operation interval. Cue duration estimates queued playback; it does not prove the speaker emitted sound or the microphone heard it. |
| `hold_final.segments_decoded`, `segments_dropped`, `reasons` | Original decoder decisions, including stripped/rejected segment reasons. Counts include attempted retries. |
| `no_speech_retries`, `decoded_chars`, `decoded_words` | The 1118 retry count and original final decoder output before correction/cleanup/formatting. A retry is an incident whether it succeeds or fails. |
| `request_chars`, `request_words` | Final insertion request after corrections, cleanup and formatting. Words are whitespace-separated tokens at every stage. |
| `cleanup_word_delta`, `formatting_word_delta`, `corrections_word_delta` | Signed output-minus-input count per stage, accumulating all passes (dictionary, transcription processing and smart correction are corrections). Check each independently: legitimate additions/removals can cancel in the total. |
| `delivered_words`, `delivery_confirmed`, `delivery_abort_reason` | Target-acknowledged word count, receipt certainty and enum. Sent without acknowledgement is -1/0/unverified. Rejected dispatch is 0/0 with a reason. Pending means the original delivery has not completed. |
| `delivery_stage`, `paste_abort_word_delta` | Stage enum is `none`, `cleanup`, `formatting`, `corrections`, `paste_abort`. All stage deltas persist; delivery_stage identifies the latest stage that changed a count or an abort. No truncation cap exists in this pipeline, so there is no `truncated_cap`. |
| `sample_count`, `wav_source`, `decode_attempts` | The saved WAV's own length/source and numeric native attempts. `native_first_attempt` preserves the existing native model input (possibly trimmed or just one chunk), not necessarily the whole capture. Compare sample_count with captured_audio_ms before drawing a timeline conclusion. |

`delivery_abort_reason` includes `pending`, `none`, `unverified`, `empty_final`,
`paste_failed`, `paste_exception`, `elevated_target`, `target_changed`,
`privacy_refused`, `typing_failed`, `decode_failed`, `cancelled`, and
`not_requested` (for example parked or auto-paste off). Legacy
`hold_final.chars_delivered` / `[HOLD_FINAL] chars_delivered` is the prepared
string length and is **not a paste receipt**; use the joined delivery fields.
Late paste receipts refresh the same sidecar, without adding a ring slot.
The acknowledged count describes the request covered by the existing receipt;
a generic target acknowledgement is not independent proof of every word in an
editor. An available exact-text readback is stronger evidence. Unknown targets
remain unknown.

An `empty_spoken_chunk` triggers when speech presence is above the existing
floor. A confirmed silent empty does not trigger by itself; an empty/failed
hold followed by another hold within five seconds does (`immediate_rehold`).
Independent triggers such as first hold after startup, an actual segment drop,
retry or quality warning still apply. The older 30-second followed-by-hold
observation remains for other nonempty holds. Unknown presence stays unknown;
an independently failed decode can still be retained.

EOF/silence boundaries such as **<100 ms** or **>=200 ms** are heuristics for
triage, not proof of truncation or decoder error. Cue/duck overlap is correlation,
not proof of causation. Offline replay is a different execution and cannot prove
historical delivery loss. Compare the original decoder, stage deltas, request
and acknowledged delivery under the same ID. Unknown stays unknown. Synthetic
tests demonstrate these observations and persistence rules; they do not prove
the owner's fault reproduced, real microphone correctness or frozen-build behavior.
