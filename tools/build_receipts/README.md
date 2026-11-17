# Installed Daily build receipts (1241)

Runner only. The worker writes and unit-tests this harness without launching,
stopping or signalling an application. This is an **instrumented installed-build
smoke**, not an unmodified-build end-to-end voice acceptance test.

The runner copies `F:\Samsara-daily` to the previously nonexistent
`D:\samsara_tmp1241\app`, with `D:\samsara_tmp1241\home` empty or absent, then runs:

```powershell
& .\tools\build_receipts\run_all.ps1 -App D:\samsara_tmp1241\app -Home D:\samsara_tmp1241\home
```

Use an unlocked interactive Windows desktop and the existing Python environment
with pywinauto, pywin32, comtypes and psutil. No dependencies are installed.
The app needs its already-installed CPU speech model; downloads are disabled.
Cockpit's existing loopback API is needed for T10. T80 needs a working microphone
and a real spoken synthetic sentence during its eight-second capture; absent
speech produces FAIL. No spoken text, audio, ticket contents or UI dumps enter
the evidence files. The harness never starts/stops Cockpit or any owner app.

The app/profile paths are restricted to the exact scratch tree. Existing
nonempty profiles and path links/reparse points are refused. Preserve previous
run directories before a rerun; the harness never recursively deletes them.
It creates two disposable native Edit windows and a silent owned audio session.

## How it drives the build

The executable and installed product files are unchanged. A temporary personal
plugin is placed **only in the fresh profile**, using the installed
`load_extra_plugins`/`start_services` extension point. Its fixed-operation
mailbox calls the installed `CommandExecutor.process_text`, Settings and
mic-check UI, plus named installed readers/observers/ducking helpers. This is a
new harness adapter, not an existing public HTTP command endpoint. No product
functions are replaced, and no provider responses or desktop effects are mocked.
The adapter requires both exact scratch paths and an explicit child environment
flag; importing it in a normal app or worker does nothing. QSettings uses a
disposable INI store instead of sharing Windows registry UI state.

Settings' actual visible slider and Apply button are driven through UIA. Other
commands enter the installed executor after speech recognition; this proves no
ASR behavior. T29 performs one normal Exit and relaunch, then reopens the control.
The final shutdown uses the same `quit_app` lifecycle as tray Exit and verifies
exit code zero plus removal of `logs/session.running`. There is no force kill,
restart command or owner-process discovery. If normal shutdown cannot complete,
the isolated copy is left for the runner and lifecycle evidence reports failure.

## Proof boundaries

| Ticket | Scripted proof | Remaining acceptance |
| --- | --- | --- |
| T10 | Installed ticket reader's ID/checkpoint fields; `hey ava resume ticket T10` reaches ticket reply/TTS queue | Spoken recognition and audible correctness; needs live Cockpit API |
| T29 | Set 5 s via visible slider, Apply & Close, normal restart, verify saved config and reopened slider | Real speech pause/endpoint behavior |
| T67 | Seed contrasting prior committed text; select different native-Edit text; offline cleanup, review slot and Apply affect the selection and preserve prior source | Natural Ava requests, model rewrites, ASR, other editor classes |
| T68 | `game mode on/off`; actual runtime CPU state, model object/device restored; installed log markers | Real-game detection, GPU classification, exit grace and decode performance |
| T70 | Ordinary command completes in <1 s without pending confirmation; read-only policy decision requires confirmation for restart | Real security/privacy question UI and all command families; restart never executes |
| T73 | Save two scratch windows, move them, restore their rectangles | Refuses any unrelated movable windows; missing apps, tabs and monitor changes unproved |
| T80 | Open real mic-check window; actual capture/decoder, recognised-speech presence and saved metrics | Needs real speech; does not grade transcript accuracy or every verdict |
| T81 | Synthetic History observer seed; focus second editor; `paste that again` reaches it and leaves first editor intact | Initial voice dictation, focus races and clipboard format matrix |
| T87 | Real installed WASAPI host/engine; only owned audio session: nested reduction/restoration and intentional change retained | Recording lifecycle, Brave, session disappearance and rapid-hold races |

T73 refuses before saving when unrelated movable windows exist. Use a disposable
interactive Windows session containing only the copied app and scratch editors
for that check. The harness never minimizes, closes, moves or reads the contents
of unrelated windows. It retains only scratch entries for restoration.
T87 excludes all other enumerated audio session PIDs and restores the scratch
session's initial volume in `finally`; master volume is never changed.

## Evidence and interpretation

`C:\Users\Morne\Documents\Claude\reports\1241\receipts.json` contains one
row per ticket with `ticket`, `build_ts`, `trigger`, `result`, `evidence`,
observations, limitations, landing identity and `acceptance_eligible`. Evidence
is stored in a unique `artifacts\claude\<run>\` directory. Preflight failures
also emit nine FAIL rows; no build timestamp is invented when unavailable.

Build time comes from copied `BUILD_INFO.txt`, with metadata and executable
SHA-256 hashes; copy mtime is never used. The current Daily format lacks an
offset on `built`, so a same-day `date` offset can be retained as an explicit
inference. It disables automatic acceptance until the runner confirms timezone.
Build time must be newer than each named feature's landing. Timestamp order does
not itself establish commit ancestry; the runner must reconcile that identity.

PASS means **all observations in that scoped check passed**. Every partial
check keeps `acceptance_eligible=false`. Do not map component PASS to `in_daily`
automatically. Only T29 can qualify for its persistence scope, with explicit
timestamp timezone and verified normal shutdown. The runner reads tickets fresh
and records the exact exercised scope and remaining limits through `tickets.py`;
this harness never edits tickets or declares features done.

Exit 0 means all nine scoped checks passed and normal shutdown was verified;
1 means a check/lifecycle failure; 2 means preflight refusal. Console lines print
PASS/FAIL, exact trigger and evidence path. No installed-build result is claimed
until the runner actually executes this command.
