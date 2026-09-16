"""Cold-start profile for Samsara (measurement only; changes no startup code).

Runs in a SEPARATE interpreter with SAMSARA_HOME_DIR pointed at a temp copy of
~/.samsara (config.json only). Never touches the live profile, never imports
dictation.py into a running Samsara.

Experiments (each in its own subprocess unless noted):
  imports     per-module import cost, one interpreter per module, warm disk cache
  silero      faster_whisper.vad import + bundled Silero ONNX load
  oww         openwakeword import + Model(hey_jarvis, onnx) + one predict()
  tts         edge_tts import + samsara.tts import + EdgeTTSEngine()
  contention  the ACE engine.start() stall: main thread times the exact steps
              AudioCaptureEngine._open_stream performs (polyphase filter design +
              sd.InputStream open at the live capture rate) plus a 10 ms "GIL
              ticker", while a worker thread does what load_model_async does
              (cuda_detect, import faster_whisper, WhisperModel(medium, cuda,
              float16)). Pauses in the ticker are attributed to the worker's phase.
  calibrate   samsara.calibration.measure_ambient_rms (the 1.5 s recording)
  config      3x save_config-equivalent write (tmp + backup copy + replace)

Usage:  F:\\envs\\sami\\python.exe tools\\boot_profile.py [--gpu-load]
        --gpu-load actually constructs WhisperModel on CUDA (allocates ~1.5 GB
        VRAM next to the live app); without it the worker stops after imports.
Output: one JSON document on stdout (last line) -- perf_artifacts/boot_profile.md carries the tables.

Full-boot mode (queue 46):
        F:\\envs\\sami\\python.exe tools\\boot_profile.py --full-boot N --label NAME [--no-cal-cache]
            [--set dotted.key=<json> ...]
        Boots the real dictation.py N times, each in a fresh temp SAMSARA_HOME_DIR holding copies
        of config.json, hints_shown.json, app_index.json, .source-config-migrated, the polyphase
        filter cache and (unless --no-cal-cache) mic_calibration.json. Every hotkey / mouse / command-mode button in the
        temp config is rebound to F13-F24 so a test boot can never dictate, paste or swallow a key
        the owner is using. Reads the temp home's samsara.log, stops the process tree 3 s after
        "Startup complete" (or at 180 s), and writes perf_artifacts/boot_fullboot_NAME.{json,md}
        with per-run milestones (seconds since Popen) and median / min-max per milestone.
"""
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
GPU_LOAD = '--gpu-load' in sys.argv

# --- full-boot mode -------------------------------------------------------------

_INERT_KEYS = [f'f{i}' for i in range(13, 25)] + [f'shift+f{i}' for i in range(13, 25)]
_PROFILE_FILES = ['config.json', 'hints_shown.json', 'app_index.json', '.source-config-migrated']

# (key, regex) -- first match wins per run; times are seconds since Popen.
_MILESTONES = [
    ('first_log', r'\[TORCH-GUARD\] enabled'),
    ('main_entry', r'\[BOOT-DIAG\] __main__: entry'),
    ('splash_shown', r'\[BOOT-DIAG\] splash init'),
    ('init_entry', r'\[BOOT-DIAG\] __init__ entry'),
    ('model_kickoff', r'\[BOOT\] model load kicked off'),
    ('config_watcher', r'\[CONFIG\] File watcher started'),
    ('shell_ready', r'\[BOOT\] shell ready'),
    ('tray_created', r'\[BOOT\] tray icon created'),
    ('tts_ready', r'\[TTS\] AudioCoordinator ready'),
    ('whisper_ready', r'\[BOOT\] async: Whisper model load'),
    ('silero_ready', r'\[BOOT\] async: Silero VAD load'),
    ('ready_for_dictation', r'Ready for dictation\.'),
    ('startup_complete', r'\[INIT\] Startup complete'),
]
_TS = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - \w+ - (.*)$')
_STAGE = re.compile(r'\[BOOT\] (.+?): (\d+)ms\s+\(total \d+ms, thread=([^)]+)\)')


def _inert_config(cfg: dict) -> dict:
    keys = iter(_INERT_KEYS)
    for k in sorted(cfg):
        if (k.endswith('hotkey') or k.endswith('_key')) and isinstance(cfg[k], str):
            cfg[k] = next(keys)
    if isinstance(cfg.get('hotkeys'), dict):
        for k in cfg['hotkeys']:
            cfg['hotkeys'][k] = next(keys)
    if isinstance(cfg.get('command_mode'), dict):
        cfg['command_mode']['button'] = next(keys)
    return cfg


def _apply_overrides(cfg: dict, overrides) -> dict:
    """--set dotted.key=<json> pairs, e.g. tts.enabled=false."""
    for item in overrides:
        dotted, raw = item.split('=', 1)
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        node = cfg
        parts = dotted.split('.')
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return cfg


def _run_one_boot(cal_cache: bool, overrides=(), timeout_s: float = 180.0) -> dict:
    live = Path(os.path.expanduser('~')) / '.samsara'
    home = Path(tempfile.mkdtemp(prefix='samsara_fullboot_'))
    files = _PROFILE_FILES + (['mic_calibration.json'] if cal_cache else [])
    for name in files:
        if (live / name).exists():
            shutil.copy2(live / name, home / name)
    # The live profile's polyphase filter disk cache (audio_engine.engine._filter_cache_path);
    # without it every temp boot would re-design the filter (~0.9 s) and not match a real boot.
    for npz in (live / 'cache').glob('polyphase_filter_*.npz'):
        (home / 'cache').mkdir(exist_ok=True)
        shutil.copy2(npz, home / 'cache' / npz.name)
    cfg = json.loads((home / 'config.json').read_text(encoding='utf-8'))
    cfg = _apply_overrides(_inert_config(cfg), overrides)
    (home / 'config.json').write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    env = dict(os.environ, SAMSARA_HOME_DIR=str(home), PYTHONIOENCODING='utf-8')
    log_path = home / 'logs' / 'samsara.log'
    t0 = time.time()
    proc = subprocess.Popen([PY, 'dictation.py'], cwd=str(REPO), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
    done_at = None
    while time.time() - t0 < timeout_s:
        time.sleep(0.25)
        if proc.poll() is not None:
            break
        if done_at is None and log_path.exists():
            if 'Startup complete' in log_path.read_text(encoding='utf-8', errors='replace'):
                done_at = time.time()
        if done_at is not None and time.time() - done_at >= 3.0:
            break
    exit_code = proc.poll()
    subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)], capture_output=True)
    text = log_path.read_text(encoding='utf-8', errors='replace') if log_path.exists() else ''
    run = {'milestones': {}, 'stages': [], 'migrate_lines': 0, 'calibration': None,
           'exit_code_before_kill': exit_code, 'timed_out': done_at is None}
    for line in text.splitlines():
        m = _TS.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S,%f').timestamp() - t0
        msg = m.group(2)
        for key, pat in _MILESTONES:
            if key not in run['milestones'] and re.search(pat, msg):
                run['milestones'][key] = round(ts, 3)
        s = _STAGE.search(msg)
        if s:
            run['stages'].append({'label': s.group(1), 'ms': int(s.group(2)), 'thread': s.group(3)})
        if '[MIGRATE]' in msg:
            run['migrate_lines'] += 1
        c = re.search(r'\[BOOT-DIAG\] mic calibration \((\w+)\): (\d+)ms', msg)
        if c:
            run['calibration'] = {'result': c.group(1), 'ms': int(c.group(2))}
        if re.search(r'\[BOOT-DIAG\] (sounddevice import|__main__: entry|audio_engine import \(total\))', msg):
            run.setdefault('diag', []).append(msg[:160])
    shutil.rmtree(home, ignore_errors=True)
    return run


def _spread(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {'median': round(statistics.median(values), 3), 'min': round(min(values), 3),
            'max': round(max(values), 3), 'n': len(values)}


def full_boot_main(argv) -> None:
    n = int(argv[argv.index('--full-boot') + 1])
    label = argv[argv.index('--label') + 1] if '--label' in argv else 'run'
    cal_cache = '--no-cal-cache' not in argv
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == '--set']
    runs = []
    for i in range(n):
        r = _run_one_boot(cal_cache, overrides)
        runs.append(r)
        ms = r['milestones']
        print(f"[{label} {i + 1}/{n}] startup_complete={ms.get('startup_complete')}s "
              f"whisper={ms.get('whisper_ready')}s shell={ms.get('shell_ready')}s "
              f"timed_out={r['timed_out']}", flush=True)
        time.sleep(5)  # let GPU memory and the audio device settle between boots
    summary = {k: _spread([r['milestones'].get(k) for r in runs]) for k, _ in _MILESTONES}
    stage_labels = []
    for r in runs:
        for s in r['stages']:
            if s['label'] not in stage_labels:
                stage_labels.append(s['label'])
    stages = {lab: _spread([next((s['ms'] for s in r['stages'] if s['label'] == lab), None) for r in runs])
              for lab in stage_labels}
    out = {'label': label, 'cal_cache': cal_cache, 'overrides': overrides, 'n': n, 'summary_s': summary, 'stages_ms': stages, 'runs': runs,
           'git_head': subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=str(REPO),
                                      capture_output=True, text=True).stdout.strip()}
    perf = REPO / 'perf_artifacts'
    (perf / f'boot_fullboot_{label}.json').write_text(json.dumps(out, indent=2), encoding='utf-8')
    lines = [f'# Full boot: {label} ({n} runs, calibration cache {"on" if cal_cache else "off"}, '
             f'overrides {overrides or "none"}, HEAD {out["git_head"]})', '',
             'Seconds since Popen. Warm disk cache (a true cold boot needs a reboot).', '',
             '| milestone | median s | min | max |', '|---|---:|---:|---:|']
    for k, _ in _MILESTONES:
        s = summary[k]
        if s:
            lines.append(f"| {k} | {s['median']} | {s['min']} | {s['max']} |")
    lines += ['', '| [BOOT] stage | median ms | min | max |', '|---|---:|---:|---:|']
    for lab, s in stages.items():
        if s:
            lines.append(f"| {lab} | {s['median']:.0f} | {s['min']:.0f} | {s['max']:.0f} |")
    (perf / f'boot_fullboot_{label}.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))


if '--full-boot' in sys.argv:
    full_boot_main(sys.argv)
    sys.exit(0)

# --- temp home: copy config.json only ---------------------------------------
LIVE_HOME = Path(os.path.expanduser('~')) / '.samsara'
TMP_HOME = Path(tempfile.mkdtemp(prefix='samsara_bootprof_'))
shutil.copy2(LIVE_HOME / 'config.json', TMP_HOME / 'config.json')
ENV = dict(os.environ, SAMSARA_HOME_DIR=str(TMP_HOME), PYTHONIOENCODING='utf-8')
CFG = json.load(open(TMP_HOME / 'config.json', encoding='utf-8'))
MIC = CFG.get('microphone')
RESULTS = {'home': str(TMP_HOME), 'mic': MIC, 'model_size': CFG.get('model_size'), 'gpu_load': GPU_LOAD}


def sub(code, timeout=600):
    """Run `code` in a fresh interpreter (cwd=repo) and return parsed JSON from its last line."""
    p = subprocess.run([PY, '-c', code], cwd=str(REPO), env=ENV, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=timeout)
    last = [l for l in p.stdout.splitlines() if l.startswith('{')]
    if not last:
        return {'error': (p.stderr or p.stdout)[-800:]}
    return json.loads(last[-1])


PRELUDE = """
import sys, time, json, os
sys.path.insert(0, os.getcwd())
import samsara.torch_guard; samsara.torch_guard.install()   # same as dictation.py
T = {}
def mark(k, t0): T[k] = round((time.perf_counter() - t0) * 1000)
"""

# --- 1. per-module import cost, isolated -------------------------------------
IMPORTS = ['numpy', 'sounddevice', 'scipy.signal', 'onnxruntime', 'ctranslate2', 'faster_whisper',
           'faster_whisper.vad', 'openwakeword', 'openwakeword.model', 'sklearn', 'edge_tts',
           'samsara.tts', 'PySide6.QtWidgets', 'samsara.audio_engine', 'pynput.keyboard']
print('== imports (isolated interpreters, warm cache) ==')
imp = {}
for m in IMPORTS:
    r = sub(PRELUDE + f"""
t0 = time.perf_counter()
try:
    import {m}
    ok = True
except Exception as e:
    ok = False; err = str(e)[:120]
mark('ms', t0)
print(json.dumps({{'module': '{m}', 'ms': T['ms'], 'ok': ok, 'err': locals().get('err')}}))
""")
    imp[m] = r
    print(f"  {m:28s} {r.get('ms', '?'):>7} ms  {'' if r.get('ok') else r.get('err') or r.get('error')}")
RESULTS['imports'] = imp

# --- 2. Silero ----------------------------------------------------------------
print('== silero ==')
r = sub(PRELUDE + """
from pathlib import Path
t0 = time.perf_counter()
from faster_whisper.utils import get_assets_path
from faster_whisper.vad import SileroVADModel
mark('import_faster_whisper_vad', t0)
p = Path(get_assets_path()) / 'silero_vad_v6.onnx'
t0 = time.perf_counter(); m = SileroVADModel(str(p)); mark('SileroVADModel_load', t0)
import numpy as np
t0 = time.perf_counter()
state = m.get_initial_states(1) if hasattr(m, 'get_initial_states') else None
mark('get_initial_states', t0)
print(json.dumps(T))
""")
print('  ', r); RESULTS['silero'] = r

# --- 3. OpenWakeWord ----------------------------------------------------------
print('== openwakeword ==')
r = sub(PRELUDE + """
t0 = time.perf_counter(); import openwakeword; mark('import_openwakeword', t0)
t0 = time.perf_counter(); from openwakeword.model import Model; mark('import_openwakeword_model', t0)
t0 = time.perf_counter(); m = Model(wakeword_models=['hey_jarvis'], inference_framework='onnx'); mark('Model_hey_jarvis_onnx', t0)
import numpy as np
chunk = np.zeros(1280, dtype=np.int16)
t0 = time.perf_counter(); m.predict(chunk); mark('first_predict', t0)
t0 = time.perf_counter(); m.predict(chunk); mark('second_predict', t0)
print(json.dumps(T))
""")
print('  ', r); RESULTS['oww'] = r

# --- 4. TTS -------------------------------------------------------------------
print('== tts ==')
r = sub(PRELUDE + """
t0 = time.perf_counter(); import edge_tts; mark('import_edge_tts', t0)
t0 = time.perf_counter(); from samsara.tts import WinRTEngine, EdgeTTSEngine, AudioCoordinator; mark('import_samsara_tts', t0)
t0 = time.perf_counter(); e = EdgeTTSEngine(output_device=None); mark('EdgeTTSEngine_ctor', t0)
print(json.dumps(T))
""")
print('  ', r); RESULTS['tts'] = r

# --- 5. contention: ACE start() steps vs model thread --------------------------
print('== contention (ACE start steps on main thread while worker imports/loads Whisper) ==')
r = sub(PRELUDE + f"""
import threading, numpy as np
import sounddevice as sd
from samsara.audio_engine.engine import _design_polyphase_filter, _gcd, SAMPLE_RATE
from samsara.audio_engine.frame import FRAME_MS
MIC = {MIC!r}
GPU = {GPU_LOAD!r}
try:
    native = int(sd.query_devices(MIC, kind='input')['default_samplerate'])
except Exception:
    native = 44100
phases = []          # (t, label) from the worker
pauses = []          # (t, gap_ms) from the ticker
main_steps = {{}}
t_start = time.perf_counter()
def wlog(label): phases.append((round(time.perf_counter() - t_start, 3), label))

def worker():
    wlog('worker: start')
    from samsara.cuda_detect import is_cuda_available, resolve_device
    wlog('worker: cuda_detect imported')
    ok = is_cuda_available(); wlog(f'worker: is_cuda_available -> {{ok}}')
    from faster_whisper import WhisperModel
    wlog('worker: faster_whisper imported')
    if GPU:
        m = WhisperModel({CFG.get('model_size')!r}, device='cuda', compute_type='float16', cpu_threads=4, num_workers=2)
        wlog('worker: WhisperModel loaded')
        del m
    wlog('worker: done')

def ticker(stop):
    last = time.perf_counter()
    while not stop.is_set():
        time.sleep(0.01)
        now = time.perf_counter(); gap = (now - last) * 1000
        if gap > 60: pauses.append((round(now - t_start, 3), round(gap)))
        last = now

stop = threading.Event()
th = threading.Thread(target=worker, daemon=True); tk = threading.Thread(target=ticker, args=(stop,), daemon=True)
tk.start(); th.start()
time.sleep(0.25)   # dictation.py calls _start_ace_engine ~5 ms after load_model_async
t0 = time.perf_counter()
g = _gcd(native, SAMPLE_RATE); up, down = SAMPLE_RATE // g, native // g
blocksize = int(native * FRAME_MS // 1000)
main_steps['t_open_stream_begin'] = round(t0 - t_start, 3)
h = _design_polyphase_filter(up, down); main_steps['design_polyphase_filter_ms'] = round((time.perf_counter() - t0) * 1000)
t1 = time.perf_counter()
s = sd.InputStream(samplerate=native, channels=1, dtype=np.float32, blocksize=blocksize, device=MIC, callback=lambda *a: None)
s.start(); main_steps['InputStream_open_start_ms'] = round((time.perf_counter() - t1) * 1000)
main_steps['ace_start_equivalent_total_ms'] = round((time.perf_counter() - t0) * 1000)
main_steps['t_open_stream_end'] = round(time.perf_counter() - t_start, 3)
s.stop(); s.close()
th.join(600); stop.set(); tk.join(2)
print(json.dumps({{'native_rate': native, 'main_steps': main_steps, 'worker_phases': phases, 'ticker_pauses_ms': pauses,
                   'worker_total_s': phases[-1][0] if phases else None}}))
""", timeout=900)
print(json.dumps(r, indent=1)[:3000]); RESULTS['contention'] = r

# --- 5b. baseline: same main-thread steps with NO worker ----------------------
r = sub(PRELUDE + f"""
import numpy as np, sounddevice as sd
from samsara.audio_engine.engine import _design_polyphase_filter, _gcd, SAMPLE_RATE
from samsara.audio_engine.frame import FRAME_MS
MIC = {MIC!r}
native = int(sd.query_devices(MIC, kind='input')['default_samplerate'])
g = _gcd(native, SAMPLE_RATE); up, down = SAMPLE_RATE // g, native // g
t0 = time.perf_counter(); h = _design_polyphase_filter(up, down); mark('design_polyphase_filter_ms', t0)
t0 = time.perf_counter(); s = sd.InputStream(samplerate=native, channels=1, dtype=np.float32, blocksize=int(native*FRAME_MS//1000), device=MIC, callback=lambda *a: None); s.start(); mark('InputStream_open_start_ms', t0)
s.stop(); s.close()
t0 = time.perf_counter(); sd.query_devices(); mark('query_devices_all_ms', t0)
print(json.dumps(T))
""")
print('  baseline (no worker):', r); RESULTS['contention_baseline'] = r

# --- 6. calibration recording ---------------------------------------------------
print('== calibration ==')
r = sub(PRELUDE + f"""
import sounddevice as sd
from samsara.calibration import measure_ambient_rms, CALIBRATION_DURATION
native = int(sd.query_devices({MIC!r}, kind='input')['default_samplerate'])
t0 = time.perf_counter(); rms = measure_ambient_rms({MIC!r}, native); mark('measure_ambient_rms_ms', t0)
T['CALIBRATION_DURATION_s'] = CALIBRATION_DURATION; T['chunks'] = len(rms)
print(json.dumps(T))
""")
print('  ', r); RESULTS['calibration'] = r

# --- 7. config: what one migration+save costs (tmp write + backup copy + replace) --
print('== config save cost (temp home) ==')
r = sub(PRELUDE + """
import shutil
from pathlib import Path
from samsara.paths import samsara_config_path
p = samsara_config_path(); cfg = json.load(open(p, encoding='utf-8'))
t0 = time.perf_counter(); json.load(open(p, encoding='utf-8')); mark('json_load_ms', t0)
t0 = time.perf_counter()
for i in range(3):
    tmp = p.with_suffix('.json.tmp'); bak = p.with_suffix('.json.bak')
    (p.parent / 'config_backups').mkdir(exist_ok=True)
    shutil.copy2(p, p.parent / 'config_backups' / f'config_{i}.json')
    tmp.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
    shutil.copy2(p, bak); os.replace(tmp, p)
mark('three_saves_ms', t0)
T['legacy_keys_present'] = {k: (k in cfg) for k in ('wake_targets', 'command_mode_enabled', 'ai_command_mode', 'config_version')}
print(json.dumps(T))
""")
print('  ', r); RESULTS['config'] = r

shutil.rmtree(TMP_HOME, ignore_errors=True)
print('RESULTS_JSON ' + json.dumps(RESULTS))
