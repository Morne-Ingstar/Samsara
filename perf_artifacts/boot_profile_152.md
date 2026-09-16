# Boot profile 152

Source-tree measurement at HEAD 763e509; three subprocess boots per cohort.
Every boot used a fresh temporary SAMSARA_HOME_DIR and inert hotkeys. Disk cache was warm,
the normal app was already running, and these are not post-reboot filesystem-cold numbers.

## 2026-09-11 versus current source

| stage | 2026-09-11 | current source |
|---|---:|---:|
| sounddevice import | 1,500 ms | 171ms (169–177, n=3) |
| config load (includes migration checks) | 531 ms | 156ms (125–172, n=3) |
| mic calibration | 1,766 ms | cached 0ms (0–16, n=3); uncached 1532ms (1531–1562, n=3) |
| TTS engine init | 1,641 ms | deferred scheduler 31ms (31–32, n=3) |
| plugin discovery | 688 ms | 234ms (219–234, n=3) |
| _start_ace_engine total | 13,400 ms | 19ms (17–20, n=3) |
| Silero VAD load | 12,500 ms | async 63ms (62–63, n=3); ONNX ctor 50ms (48–52, n=3) |
| OpenWakeWord | 7,900 ms | models after dictation 1.253s (1.219–1.255, n=3); arming 0.311s (0.3–0.313, n=3) |
| wake listener active | 26.4 s | 6.057s (5.816–6.065, n=3) |

## Import-time profile: source boot path

python -X importtime was used for each cached source boot. Values are inclusive package-root
costs; they must not be summed because import trees overlap.

| package | median ms | min–max | interpretation |
|---|---:|---:|---|
| onnxruntime | 51.083 | 50.952–227.881 | runtime import is not a 12.5 s cost |
| torch | 0.229 | 0.019–0.315 | guarded; this is rejected-import overhead, not PyTorch load |
| numpy | 50.731 | 49.668–60.158 | normal dependency cost |
| sounddevice | 176.046 | 165.154–181.058 | largest named pre-main import here |
| PySide6 | 33.044 | 32.775–33.857 | Qt root import cost |

## Capability readiness timeline

| capability | median seconds since Popen | min–max | definition |
|---|---:|---:|---|
| Ava provider | 1.572 | 1.458–1.576 (n=3) | readiness monitor reported ready in all 3 runs; speech input still requires Whisper |
| hotkey dictation | 4.491 | 4.297–4.499 (n=3) | Whisper + Silero complete |
| wake models | 5.744 | 5.516–5.754 (n=3) | OpenWakeWord models constructed |
| wake listener | 6.057 | 5.816–6.065 (n=3) | wake capture is actually armed |

Ava can have a provider-ready badge at ~1.57 s, but spoken Ava is only usable when
the shared speech path becomes hotkey-ready (~4.49 s).

## Findings and ranked fixes

1. Preserve calibration caching: it already saves 1.433 s median to startup (1,532 ms direct phase). Risk: low; it is already implemented, so do not rework it.
2. If startup needs further reduction, profile and optimize Whisper model construction first: its current critical-path stage is 2,656 ms median. Potential ceiling ~2.66 s; risk high (recognition quality/device support).
3. Consider overlapping OpenWakeWord only after a contention measurement: it is 1.253 s after dictation and can at most advance wake by that amount. Risk medium/high: it imports a heavy stack and can slow hotkey readiness.
4. Plugin discovery is 234–250 ms. Lazy loading could advance model kickoff by at most ~250 ms. Risk medium: command/Ava availability must remain truthful.
5. Do not prioritize migration work. The three migration helpers still execute, but all three runs logged zero MIGRATE actions; config load is 156 ms for the entire envelope. Any saving is bounded by that amount, not the old 400–531 ms claim.
6. Do not pursue the old Silero-import theory. ONNX runtime is 51.083 ms median, ONNX construction is 50 ms, and the entire async Silero phase is 63 ms. Risk of lazy-loading this now outweighs a negligible saving.
7. Sounddevice import is 171 ms. It is a small first-paint candidate only; it cannot improve audio capability readiness and is not a 1.5 s target.

## Frozen build

Not measured. dist/Samsara is timestamped 2026-09-14 19:16 and predates the measured
2026-09-16 source revision. A frozen import profile would therefore be a profile of a stale
import graph, not the beta to be built. Re-run this tool against a fresh frozen build; a
PyInstaller executable also cannot accept python -X importtime directly.

Raw cohort data and all derived numbers are in boot_profile_152.json.
