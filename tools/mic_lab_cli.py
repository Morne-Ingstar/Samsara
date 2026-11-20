"""Headless Mic Lab using the production capture ring and held decoder.

Run from the repo: python tools/mic_lab_cli.py --device "exact name" --label "mic"
Saved run: python tools/mic_lab_cli.py --from-folder PATH
No DictationApp, tray, clipboard, hotkey hook or running Samsara is used.
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
from pathlib import Path
from queue import Queue, Empty
import sys
import threading
import time
from types import SimpleNamespace as NS

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from samsara import diagnostics
from samsara.mic_lab_battery import BatteryRunner, rejudge_run
from samsara.mic_lab_endpoint import (capture_configuration, endpoint_volume,
    inspect_endpoint, native_preflight)
from samsara.mic_lab_store import MicLabStore, json_value, load_run, storage_root


def parser():
    # PLACEHOLDER COPY (1267)
    p = argparse.ArgumentParser(description='Run a local microphone battery; all audio stays on this PC.')
    p.add_argument('--device', help='Exact input name or PortAudio input index')
    p.add_argument('--label', help='Your microphone label')
    action = p.add_mutually_exclusive_group()
    action.add_argument('--from-folder', type=Path, help='Re-judge saved audio and reference results without a microphone')
    # PLACEHOLDER COPY (1299)
    action.add_argument('--devices', action='store_true', help='Show the tested microphones table')
    action.add_argument('--rebuild-devices', action='store_true', help='Re-grade saved runs and rebuild the tested microphones table')
    # PLACEHOLDER COPY (1297)
    p.add_argument('--json', action='store_true', help='Print only JSON when re-judging with --from-folder')
    p.add_argument('--config', type=Path, help='Explicit offline settings JSON; no live settings are discovered')
    p.add_argument('--profile', type=Path, help='Output/cache profile (defaults to the Samsara profile path)')
    p.add_argument('--output-dir', type=Path, help='Run storage root; overrides mic_lab.output_dir')
    p.add_argument('--model', default='small.en', help='Cached faster-whisper name or local model folder; never downloaded')
    p.add_argument('--backend', choices=('auto', 'cpu', 'cuda'), default='auto')
    p.add_argument('--mount', default='unspecified')
    p.add_argument('--distance-cm', type=float, default=20.)
    p.add_argument('--pop-filter', choices=('yes', 'no', 'unknown'), default='unknown')
    p.add_argument('--read-seconds', type=float, default=12.,
                   help='Reading time before each Speak now cue (default 12)')
    p.add_argument('--gap-seconds', type=float, default=4., help='Rest after each take (default 4)')
    p.add_argument('--sandbox', action='append', choices=('desk_typing',), default=[],
                   help='Optional mounting check; does not affect the grade')
    return p


def _resolve_device(device, backend):
    from samsara.audio_engine.device_resolver import resolve
    if device.isdecimal():
        index = int(device)
        info = backend.query_devices(index, kind='input')
        if info['max_input_channels'] < 1:
            raise ValueError('Chosen device has no input channels')
        return index
    return resolve(backend, device)[0]  # Same host API preference as production.


def _load_model(name, backend):
    from samsara.cuda_detect import prepare_cuda_dll_search
    prepare_cuda_dll_search()
    import ctranslate2
    from faster_whisper import WhisperModel
    ctranslate2.set_random_seed(0)  # Before construction, as in the replay harness.
    if backend == 'auto':
        backend = 'cuda' if ctranslate2.get_cuda_device_count() else 'cpu'
    compute = 'float16' if backend == 'cuda' else 'int8'
    model = WhisperModel(name, device=backend, compute_type=compute,
                         cpu_threads=4, num_workers=1, local_files_only=True)
    return model, backend, compute


def headless_app(model, config, backend='cpu'):
    """Only the non-UI dependencies of decode_hold, all delivery callbacks inert."""
    from tools.voice_corpus.replay import Dictionary
    app = NS(config=config, model=model, model_rate=16000, device_type=backend,
        model_lock=threading.Lock(), dictionary_store=Dictionary(), recording=False,
        _hotkey_recording=False, _running=True, _tts_last_speaking=0.,
        _schedule_ui=lambda fn, *a: fn(*a), _show_outcome_chip=lambda *a, **k: None,
        _log_history=lambda **k: None, _notify_main_window=lambda *a: None,
        _filter_dictation_language=lambda text, info: text)
    return app


class RealCapture:
    """Own a standalone CLI stream. Never attach to or signal another process.

    Timed cues replace physical key presses. activate/drain_after_release and
    decode_hold are the production batch hold path. Step 10 requires a real
    hands-free app session and is deliberately not offered as a batch proxy.
    """
    def __init__(self, device, model, config, backend, *, output=print, sleep=time.sleep,
                 sound_backend=None):
        from samsara.audio_engine import AudioCaptureEngine, FrameBus, DictationSessionConsumer
        self.device, self.model, self.config = device, model, config
        self.app = headless_app(model, config, backend)
        self.bus = FrameBus()
        self.engine = AudioCaptureEngine(self.bus, config)
        self.consumer = DictationSessionConsumer(self.engine, self.app)
        self.app._dictation_consumer = self.consumer
        self.output, self.sleep = output, sleep
        self.sound_backend = sound_backend
        self.events = Queue()
        self.started = False

    read_seconds, gap_seconds = 12., 4.

    def _countdown(self, seconds):
        """Reading time before a cue, counted down so the cue never arrives unannounced."""
        left = max(1, int(round(seconds)))
        self.output(f'    Read it first. Speak at the cue, in {left} seconds.')
        while left > 0:
            if left <= 3:
                self.output(f'    {left}...')
            self.sleep(1.)
            left -= 1

    def _decode_reference(self, audio, params):
        segments, _ = self.model.transcribe(audio, **params)
        return ''.join(s.text for s in segments).strip()

    def _native(self, seconds, *, load=False):
        """Native stereo probe; inference overlaps the entire load capture."""
        ready, done = threading.Event(), threading.Event()
        failures = []
        def infer():
            try:
                ready.set()
                while not done.is_set():
                    self._decode_reference(np.zeros(16000, np.float32), dict(
                        language='en', beam_size=1, temperature=0., vad_filter=False))
            except Exception as exc:
                failures.append(type(exc).__name__)
                ready.set()
        worker = threading.Thread(target=infer, name='mic-lab-load', daemon=True) if load else None
        if worker:
            worker.start()
            ready.wait(10)
        try:
            result = native_preflight(self.device, seconds, backend=self.sound_backend)
        finally:
            done.set()
            if worker:
                worker.join(30)
                if worker.is_alive():
                    raise RuntimeError('Native load probe model did not settle')
        if not result.known:
            raise RuntimeError(result.reason)
        if failures:
            raise RuntimeError('Native load probe decode failed: ' + failures[0])
        return result.value

    def __call__(self, step, number):
        from samsara.audio_engine.frame import FRAME_SIZE
        from samsara.audio_engine.capture_snapshot import fade_capture
        from samsara import decode_recovery, hold_diagnostics
        from samsara.speech_presence import HoldEvidence
        from samsara.transcription_params import get_transcription_params, build_hotkey_transcribe_params
        if step.id == 'handsfree_e2e':
            raise ValueError('Hands-free sandbox needs an app-owned capture adapter')
        self.output(f'\n{step.id} · take {number}: {step.instruction} {step.distance_note}')
        native = {}
        if step.id == 'native_probe':
            self.output('Silence: five seconds idle, then two seconds during local inference.')
            native['native_idle'] = self._native(5.)
            native['native_load'] = self._native(2., load=True)
            self.engine.start()
            self.started = True
            self.sleep(.5)  # Let the production prebuffer fill.
        else:
            if self.gap_seconds > 0:
                self.sleep(self.gap_seconds)
            self.output(f'    SAY:  "{step.target}"')
            if step.id == 'dyn_contrast':
                self.output('    Four cues follow, about four seconds each: normal voice, LOUD, stay silent, quiet voice.')
            self._countdown(self.read_seconds)
        before = endpoint_volume(name=self.config['microphone_name'])
        hold_diagnostics.begin(self.app)
        self.app._speech_presence_capture = HoldEvidence(time.perf_counter())
        self.app.recording = self.app._hotkey_recording = True
        trigger_seq = self.bus.write_cursor
        if not self.consumer.activate():
            raise RuntimeError('Production hold capture refused activation')
        overflow = self.engine._overflow_count
        self.output('Silence now.' if step.id == 'native_probe' else 'Speak now.')
        marks = {}
        if step.id == 'dyn_contrast':
            # Regions come from the actual ring write cursor at each cue.
            marks['normal_start'] = self.bus.write_cursor
            self.output('Normal: Normal level now.')
            self.sleep(4.)
            marks['normal_end'] = marks['loud_start'] = self.bus.write_cursor
            self.output('LOUD: LOUD PEAK ONE TWO!')
            self.sleep(4.)
            marks['pause_start'] = self.bus.write_cursor
            self.output('Silence for three seconds.')
            self.sleep(3.)
            marks['pause_end'] = marks['quiet_start'] = self.bus.write_cursor
            self.output('Quiet: quiet voice returns.')
            self.sleep(4.)
            marks['quiet_end'] = self.bus.write_cursor
        else:
            self.sleep(step.seconds)
        self.output('Stop speaking. Checking this take…')
        raw = self.consumer.drain_after_release()
        self.app.recording = self.app._hotkey_recording = False
        if raw is None or not len(raw):
            raise RuntimeError('No audio arrived from the chosen input')
        after = endpoint_volume(name=self.config['microphone_name'])
        receipt = raw.capture_snapshot.receipt()
        frames = receipt['frames']
        def sample_at(seq):
            return sum(r['end_sample'] - r['start_sample'] for r in frames if r['seq'] < seq)
        extra = dict(native, sample_rate=16000, trigger_sample=sample_at(trigger_seq),
            input_overflows=max(0, self.engine._overflow_count - overflow),
            gain_before=before.value if before.known else None,
            gain_after=after.value if after.known else None)
        if marks:
            # Exclude burst reverberation and compare the two ends of the pause.
            pause_a, pause_b = sample_at(marks['pause_start']), sample_at(marks['pause_end'])
            extra['dynamic_regions'] = dict(baseline=(sample_at(marks['normal_start']), sample_at(marks['normal_end'])),
                quiet_after=(sample_at(marks['quiet_start']), sample_at(marks['quiet_end'])),
                pause_start=(pause_a + 8000, pause_a + 16000),
                pause_end=(pause_b - 8000, pause_b))
            if len(frames) > 1:
                extra['clock_timing'] = dict(frames_captured=frames[-1]['start_sample'] - frames[0]['start_sample'],
                    sample_rate=16000, wall_seconds=frames[-1]['captured_at'] - frames[0]['captured_at'])
        if step.id == 'native_probe':
            return dict(id='native-probe', route='native_and_ring_probe', raw=np.array(raw, copy=True),
                slices=[], text='', raw_coordinates=diagnostics._mic_lab_coordinates(raw),
                capture_receipt=receipt, **extra)
        params = build_hotkey_transcribe_params(get_transcription_params(self.config, language='en',
            initial_prompt=None, no_speech_threshold=.6, log_prob_threshold=-1.), command_mode_recording=False)
        self.app._capture_loss_1125 = self.consumer.capture_loss
        decoded = decode_recovery.decode_hold(self.app, fade_capture(raw, 16000, 50), params, len(raw) / 16000)
        # This standalone runner's delivered result is the decoder text; there
        # is no clipboard or application correction/command dispatch here.
        diagnostics.record(diagnostics.DiagRecord(mode='hotkey', audio_s=len(raw) / 16000,
            model_name=self.config['model_size'], device=self.app.device_type,
            compute_type=self.config['compute_type'], text=decoded.text), app=self.app)
        if not diagnostics.flush_mic_lab_observer():
            raise RuntimeError('Mic Lab observer timed out')
        try:
            event = self.events.get_nowait()
        except Empty as exc:
            raise RuntimeError('Production decode did not produce a Mic Lab observation') from exc
        return dict(event, **extra)

    def __enter__(self):
        if diagnostics._mic_lab_observer is not None:
            raise RuntimeError('Another Mic Lab observer is already registered')
        diagnostics.set_mic_lab_observer(self.events.put)
        return self

    def __exit__(self, *args):
        try:
            self.consumer.deactivate()
        finally:
            try:
                if self.started:
                    self.engine.stop()  # Only this CLI-owned object/stream.
            finally:
                diagnostics.flush_mic_lab_observer()
                diagnostics.set_mic_lab_observer(None)


def print_step(step_id, result, output=print):
    # PLACEHOLDER COPY (1297)
    output(f"{step_id}: {result['verdict']}")
    seen = set()
    for key in ('reasons', 'observations', 'unknown'):
        for reason in result.get(key, []):
            if reason not in seen:
                output(f'  {reason}')
                seen.add(reason)


def print_summary(report, output=print):
    # PLACEHOLDER COPY (1297)
    output(f"Overall: {report['verdict']}.")
    for finding in report['findings'][:3]:
        output(f'  {finding}')
    if report['unknown']:
        output('Could not measure (excluded from grade):')
        for reason in report['unknown']:
            output(f'  {reason}')


def main(argv=None, *, capture_factory=RealCapture, model_loader=_load_model, output=print):
    args = parser().parse_args(argv)
    if args.from_folder:
        report = rejudge_run(load_run(args.from_folder))
        if args.json:
            output(json.dumps(report, indent=2))
        else:
            for result in report['steps']:
                print_step(result['step_id'], result, output)
            print_summary(report, output)
        return 0 if report['complete'] else 2
    if args.json:
        parser().error('--json requires --from-folder')
    if args.devices or args.rebuild_devices:
        from samsara.mic_lab_ledger import rebuild_devices, show_devices
        from samsara.paths import samsara_home_dir

        config = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
        root = args.output_dir or storage_root(args.profile or samsara_home_dir(), config)
        table = rebuild_devices(root) if args.rebuild_devices else show_devices(root)
        output(table.rstrip('\n'))
        return 0
    if not args.device or not args.label:
        parser().error('--device and --label are required for a microphone run')
    if not np.isfinite(args.distance_cm) or args.distance_cm <= 0:
        parser().error('--distance-cm must be positive')
    config = json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config else {}
    from samsara.config_defaults import DEFAULTS
    settings = copy.deepcopy(DEFAULTS)
    settings.update(config)
    from samsara.paths import samsara_home_dir
    profile = args.profile or samsara_home_dir()
    # Only this CLI process is isolated; no application settings are read.
    os.environ['SAMSARA_HOME_DIR'] = str(profile)
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    import sounddevice as sd
    device = _resolve_device(args.device, sd)
    info = sd.query_devices(device, kind='input')
    observed = capture_configuration(device, backend=sd)
    pin = dict(label=args.label, mount=args.mount, distance_cm=args.distance_cm,
        pop_filter=args.pop_filter, capture=json_value(observed),
        endpoint=json_value(inspect_endpoint(name=info['name'])))
    volume = endpoint_volume(name=info['name'])
    pin['windows_gain_percent'] = volume.value * 100 if volume.known else None
    output('Audio is saved locally. Follow the timed prompts; no key presses are needed.')
    output(f'Each sentence is shown for {max(1., args.read_seconds):.0f} seconds before its cue. '
           'Too fast or too slow: add --read-seconds <number>.')
    model, backend, compute = model_loader(args.model, args.backend)
    settings.update(microphone=device, microphone_name=info['name'], _capture_rate=int(info['default_samplerate']),
        model_size=args.model, device=backend, compute_type=compute, mode='hold',
        diagnostics={'write_jsonl': False}, kept_dictations={'enabled': False}, benchmark={'enabled': False})
    with MicLabStore(args.output_dir or storage_root(profile, config)) as store:
        run_id = store.start_run(pin, metadata=dict(model=args.model, backend=backend, compute_type=compute,
            sample_rate=16000, capture_route='production_batch_hold', random_seed=0))
        output(f'Run: {store.root / run_id}')
        with capture_factory(device, model, settings, backend, output=output, sound_backend=sd) as capture:
            capture.read_seconds, capture.gap_seconds = max(1., args.read_seconds), max(0., args.gap_seconds)
            runner = BatteryRunner(sandbox=args.sandbox)
            def progress(step, result):
                print_step(step.id, result, output)
            report = runner.run(capture, capture._decode_reference,
                save=lambda step, number, take: store.save_take(run_id, f'{step.id}-{number}', take).result(),
                progress=progress)
            store.finish(run_id, report).result()
            print_summary(report, output)
            output(f'Saved: {store.root / run_id}')
    return 0


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)  # Avoid persisting application transcripts.
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as error:
        print(f'Mic Lab could not finish: {error}', file=sys.stderr)
        raise SystemExit(2)
