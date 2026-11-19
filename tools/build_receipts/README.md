# Unattended installed Daily receipts (1262)

From the repository, pass the dedicated scratch root with the copied Daily
build and `BUILD_INFO.txt` already in its `app` subdirectory. Supply the
Python executable and corpus root from the runner's private configuration.

```powershell
& .\tools\build_receipts\run_all.ps1 -ScratchRoot 'C:\Example\Receipts' -Python 'C:\Example\venv\Scripts\python.exe' -Corpus 'C:\Example\Corpus'
```

The worker must never execute that command. Only the runner launches and exits
its isolated child. No schedule is installed: RUNNER_RULES 37 invokes this once
at the first build window after 06:00 PT. A completed same-day/build result is
returned without another launch. `-Force` explicitly reruns while retaining all
previous artifact directories. A changed same-day build refuses with exit 1
until the runner explicitly chooses a rerun. A crashed run leaves RUNNING.lock
for inspection; the harness never takes over another run's lock.

The command returns **0 or 1**, and writes
`reports/receipts/<local-date>/RECEIPTS.md`, `receipts.json`, `completion.json`
and unique `artifacts/claude/<run>/` evidence. The Markdown has one line per
feature, build commit from the copied BUILD_INFO, timestamp and elapsed time.
Exit 0 means every attempted scoped check passed and normal shutdown verified;
NOT COVERED rows do not qualify for acceptance. Exit 1 includes preflight,
feature, placement, budget and shutdown failures. No ticket is updated.

There is a shared **480-second observation budget**, followed by at most a
10-second quit-request wait and 45-second normal-exit wait. This is a budget,
not a measured installed-build duration. The worker verifies only fakes; the
runner must measure the first real run. A failed child shutdown leaves the
isolated child for the runner; there is no force kill. Model/device init and
filesystem operations are not forcibly interrupted.

## Prerequisites and isolation

- An unlocked interactive desktop containing exactly `\\.\DISPLAY3`; absence
  refuses before launch. Owner work can continue; no keyboard/mouse injection,
  UIA focus, clipboard, layouts or owner audio sessions are changed.
- The Python executable passed to `-Python` has numpy, psutil and pywin32.
  No dependencies, model or virtual audio device are installed.
- The directory passed to `-Corpus` contains WAVs under `takes` and offline
  `recorder-settings.json` pointing to an existing supported faster-whisper HF
  cache snapshot. The harness reads the WAVs and that model pointer. The
  Python entry point also accepts `--model` for an explicit cache snapshot.
- The copied executable must support personal plugin services and the named
  installed APIs. An older build fails visibly; no source product modules are
  substituted. Build metadata identifies what actually ran; timestamp freshness
  is checked, but commit ancestry is not claimed or automatically accepted.
- Every run creates `runs\<unique-run>\home` under the prepared scratch root.
  The temporary plugin checks its staged location, the run home and the copied
  executable before starting.
  Links/reparse points and reused homes are refused. HOME, USERPROFILE,
  SAMSARA_HOME_DIR, APPDATA, LOCALAPPDATA, XDG and temp paths point at that home;
  QSettings uses an isolated INI store. The installed single-instance identity
  follows SAMSARA_HOME_DIR, independently of the owner's profile.

The executable launches initially hidden with monitor-3 startup coordinates.
This protects the splash, which is created before plugin services and otherwise
chooses the primary monitor. The plugin sets show-without-activation and
WindowDoesNotAcceptFocus before showing/moving windows on monitor 3, and rejects
in-child raise/activate/focus requests. It repositions windows that move outside
that screen. Desktop evidence verifies the copied process's visible geometry
and nonactivation flags; no owner window titles or text are read. This launch
behaviour still requires the runner's real desktop verification.

## Recorded input and proof boundaries

The guarded plugin replaces **sounddevice.InputStream**, before the installed
ACE engine starts, with real-time WAV frames; RawInputStream fallback refuses.
All installed callbacks, native-rate conversion, ring writes, mic-check source,
capture timing, VAD, decoding and heard-label delivery remain real. The input is
one whole mono PCM16/16kHz corpus take between 0.6 and 7 seconds, after a quiet
lead-in. Missing/empty/invalid corpus input fails; it never falls back to a mic.
Only a WAV hash is saved in evidence. No PCM or recognised/dictated words are
written to reports. The mic result is nonempty-recognition proof, not accuracy.

Covered recorded stages: stream callback -> ACE resampling/ring -> mic-check
capture -> installed admission/hold decoder -> actual UI heard label/metrics.
OS text paste, ordinary hold delivery and hands-free ASR are **not** proved by
that mic check. Physical microphone/acoustics/interface/driver/PortAudio device
callbacks require live hardware. No owner test ask is filed by this harness.

| Feature | Scoped unattended observation | Remaining boundary |
| --- | --- | --- |
| T10 | Installed ticket lookup + command reply queue | Existing Cockpit API needed; ASR and audible correctness unproved |
| T29 | Actual Qt pace slider/signals/Apply, normal restart, reopened 5 s | Speech endpoint timing unproved; no UIA/keyboard interaction |
| T67 | NOT COVERED | Foreground selection/edit requires disposable desktop |
| T68 | Manual game mode on/off, real CPU/model/runtime restoration | Real-game detection, GPU behaviour and auto-exit unproved |
| T70 | Ordinary command timing + read-only restart authorization | Restart never executed; security/privacy question UI unproved |
| T73 | NOT COVERED | Global layout save may enumerate owner windows |
| T80 | Recorded input through installed capture/decoder/UI label | No physical mic/driver or transcript accuracy proof |
| T81 | NOT COVERED | OS paste/clipboard/foreground targeting requires disposable desktop |
| T87 | NOT COVERED | Recording duck lifecycle and owner audio sessions not exercised |
| 1252 | Zero PCM during continuous listening -> actual visible notice | No unplug/mute diagnosis |
| 1251 | Synthetic staged hands-free draft -> actual visible Re-read | Full display only; no Re-read decode or hands-free ASR proof |
| 1135 | Help question -> exact answer forwarded to real speech coordinator | Text admission; no ASR or audible quality judgement |
| 1254/1255 | Owned main-mouse callback down/up -> capture starts/stops | No physical/global hook/helper IPC test; no events sent to owner hooks |

These are instrumented build smoke receipts. Product audio stream input is
replaced explicitly, and text fixtures enter existing command/draft routes.
They must never be called full end-to-end voice acceptance or the 692-take
corpus gate. The older interactive `runner.py` is retained for reference; the
default PowerShell command no longer runs its focus-taking checks or live mic.
