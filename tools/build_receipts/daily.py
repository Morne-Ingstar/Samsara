"""Runner-only unattended installed-build receipts. Never launches on import."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

try:
    from . import common, desktop, replay_input
    from .runner import Harness, ProbeError
except ImportError:
    import common, desktop, replay_input
    from runner import Harness, ProbeError


FEATURES = {
    'T10': 'Ticket recall', 'T29': 'Speech pace persists', 'T67': 'Edit selected text',
    'T68': 'Game mode model restore', 'T70': 'Immediate ordinary commands',
    'T73': 'Named layouts', 'T80': 'Check my mic with recorded input',
    'T81': 'Paste that again', 'T87': 'Recording volume restore',
    '1252': 'Dead-mic notice', '1251': 'Re-read on a hands-free draft',
    '1135': 'Spoken help answer', '1254': 'Mouse 5 capture starts and stops',
}
NOT_COVERED = {
    'T67': 'Selected-text replacement needs foreground selection; background run must not take focus.',
    'T73': 'Global layout enumeration/save can include owner windows; disposable desktop required.',
    'T81': 'OS paste needs foreground delivery and clipboard changes; background run must not take focus.',
    'T87': 'Recording lifecycle ducking of other apps is not exercised; no owner audio is changed.',
}
NEW_LANDINGS = {
    '1252': '2026-11-17T19:44:42-08:00', '1251': '2026-11-17T20:10:30-08:00',
    '1254': '2026-11-18T09:14:03-08:00', '1135': '2026-11-17T12:05:24-08:00',
}


def local_model_name(model):
    """Use the installed model enum with the explicit existing HF cache."""
    name = model.parent.parent.name.removeprefix('models--Systran--faster-whisper-')
    if (model.parent.name != 'snapshots' or name not in
            {'tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en', 'medium', 'medium.en', 'large-v3'}
            or not (model / 'model.bin').is_file()):
        raise ValueError('Require an existing supported faster-whisper cache snapshot')
    return name


def validate_daily(app, home, *, root=common.ROOT):
    app, home, root = [Path(p).absolute() for p in (app, home, root)]
    for path in (app, home):
        common.reject_links(path)
    if app != root / 'app' or home.name != 'home' or home.parent.parent != root / 'runs':
        raise ValueError('Use the fixed copied app and a fresh run-specific home')
    if not (app / 'Samsara.exe').is_file():
        raise ValueError('Runner must copy Daily to D:/samsara_tmp1241/app first')
    if home.exists():
        raise ValueError('Preserve previous run; home must not already exist')
    return app, home


def make_row(feature, identity, trigger, evidence, observations=None, *, limitation='', result=None):
    observations = observations or {}
    if result is None:
        if feature in common.TICKETS:
            return common.receipt(feature, identity, trigger, evidence, observations, limitation=limitation)
        try:
            built = datetime.fromisoformat(identity['build_ts'])
            newer = built.tzinfo is not None and built > datetime.fromisoformat(NEW_LANDINGS[feature])
        except (ValueError, TypeError, KeyError):
            newer = False
        observations = dict(observations, build_newer_than_landing=newer)
        result = 'PASS' if observations and all(v is True for v in observations.values()) else 'FAIL'
    if result not in {'PASS', 'FAIL', 'NOT COVERED'}:
        raise ValueError('Unknown receipt result')
    return dict(ticket=feature, feature=FEATURES[feature], build_ts=identity.get('build_ts'),
                build=identity, trigger=trigger, result=result, evidence=str(Path(evidence).absolute()),
                observations=observations, limitation=limitation, acceptance_eligible=False)


def write_summary(report, identity, rows, *, elapsed, lifecycle_ok, error=None):
    common.write_json(report / 'receipts.json', rows)
    passed = len(rows) == len(FEATURES) and all(r['result'] != 'FAIL' for r in rows) and lifecycle_ok and not error
    status = 0 if passed else 1
    lines = ['# Build receipts', '', f"Build head: {identity.get('commit') or 'UNAVAILABLE'}",
             f"Built: {identity.get('build_ts') or 'UNAVAILABLE'}",
             f'Elapsed: {elapsed:.1f} seconds; exit: {status}; normal shutdown: {lifecycle_ok}', '',
             'PASS covers only the recorded scope. NOT COVERED is never acceptance.', '']
    for row in rows:
        lines.append(f"- {row['result']} — {FEATURES[row['ticket']]} ({row['ticket']}) | "
                     f"{row['trigger']} | [evidence]({Path(row['evidence']).as_posix()}) | {row['limitation']}")
    if error:
        lines.extend(['', 'Run failure code: ' + error])
    pending = report / 'RECEIPTS.md.tmp'
    pending.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    pending.replace(report / 'RECEIPTS.md')
    common.write_json(report / 'completion.json', dict(build=identity, exit_code=status,
                      elapsed_s=elapsed, normal_shutdown_verified=lifecycle_ok))
    return status


class DailyHarness(Harness):
    def __init__(self, app, home, report, identity, take, model, rect):
        super().__init__(app, home, report, identity)
        self.take, self.model, self.rect = take, model, rect

    def launch_environment(self, env):
        env.update(SAMSARA_RECEIPTS_1262='1', SAMSARA_RECEIPT_TAKE=str(self.take),
                   HF_HUB_CACHE=str(self.model.parent.parent.parent),
                   HUGGINGFACE_HUB_CACHE=str(self.model.parent.parent.parent),
                   HF_HOME=str(self.model.parent.parent.parent.parent))
        return env

    def launch_options(self):
        return desktop.startup_options(self.rect)

    def launch(self):
        super().launch()
        common.wait_for(lambda: all(self.channel.call('state')[key]
                        for key in ('model_loaded', 'desktop_ready', 'capture_ready')), timeout=120)

    def check(self, feature, trigger, fn, limitation, eligible=False):
        evidence = self.artifacts / (feature + '.json')
        try:
            row = make_row(feature, self.identity, trigger, evidence, fn(), limitation=limitation)
        except Exception as error:
            code = error.code if isinstance(error, ProbeError) else type(error).__name__
            row = make_row(feature, self.identity, trigger, evidence, {'completed': False}, limitation=limitation)
            row['failure_code'] = code
        common.write_json(evidence, row)
        self.rows.append(row)
        self.save()

    def uncovered(self, feature, reason):
        evidence = self.artifacts / (feature + '.json')
        row = make_row(feature, self.identity, 'Not executed in background desktop', evidence,
                       limitation=reason, result='NOT COVERED')
        common.write_json(evidence, row)
        self.rows.append(row)
        self.save()

    def pace(self):
        changed = self.channel.call('pace_set')
        common.wait_for(lambda: self.channel.call('state')['pause'] == 5)
        self.quit()
        self.launch()
        return dict(**changed, **self.channel.call('pace_read'))

    def mic(self):
        self.channel.call('mic')
        self.channel.call('mic_start')
        try:
            result = common.wait_for(lambda: (s if not (s := self.channel.call('mic_state'))['busy'] else None), timeout=60)
            return dict(completed=result['complete'], metrics_saved=result['result_saved'],
                        recorded_speech_recognised=result['recognised'])
        finally:
            self.channel.call('mic_close')

    def silent(self):
        started = self.channel.call('silence_start')
        try:
            result = common.wait_for(lambda: (s if (s := self.channel.call('silence_state'))['notice_visible'] else None), timeout=25)
            return dict(**started, **result)
        finally:
            self.channel.call('continuous_stop')

    def reread(self):
        self.channel.call('silence_start')
        try:
            staged = self.channel.call('reread_stage')
            result = common.wait_for(lambda: (s if (s := self.channel.call('reread_state'))['reread_visible'] else None))
            return dict(**staged, **result)
        finally:
            self.channel.call('continuous_stop')

    def mouse(self):
        down = self.channel.call('mouse_down')
        try:
            common.wait_for(lambda: self.channel.call('mouse_state')['recording'], timeout=5)
            time.sleep(.3)
        finally:
            self.channel.call('mouse_up')
        common.wait_for(lambda: not any(self.channel.call('mouse_state').values()), timeout=30)
        return dict(capture_started=down['recording'], capture_stopped=True)

    def run(self):
        # No scratch editors, UIA, global input, clipboard or foreground APIs.
        self.launch()
        for feature, reason in NOT_COVERED.items():
            self.uncovered(feature, reason)
        self.check('T29', 'Settings pace slider to 5 s > Apply & Close > normal restart', self.pace,
                   'Actual Qt controls/signals; no keyboard/UIA; persistence only.')
        self.check('T10', 'hey ava resume ticket T10 via installed executor', self.ticket,
                   'Existing Cockpit reader and reply queue; no voice recognition or audible judgement.')
        self.check('T68', 'game mode on > CPU runtime > game mode off > original model', self.game,
                   'Manual commands; no game detection or automatic exit proof.')
        self.check('T70', 'game mode off; authorize restart without executing restart', self.timing,
                   'One ordinary command and confirmation decision only.')
        self.check('T80', 'Check my mic > stream-boundary recorded WAV > installed decoder > heard label', self.mic,
                   'Real installed capture/ring/decoder/UI delivery; physical mic/driver bypassed; no accuracy score.')
        self.check('1252', 'Continuous listening > zero PCM for 10 seconds > visible dead-mic notice', self.silent,
                   'Installed silence observer and actual notice; no physical unplug or muted-device diagnosis.')
        self.check('1254', 'Mouse 5 callback down > recording > callback up > stopped', self.mouse,
                   'Behaviour below hook boundary, silence only; physical button, global hook and helper IPC NOT COVERED.')
        self.check('1251', 'Continuous DICTATE > synthetic staged draft > visible Re-read', self.reread,
                   'Real draft/UI with text fixture; full display only; no hands-free ASR or Re-read decode-quality proof.')
        self.check('1135', 'What is a wake word? via installed command executor',
                   lambda: self.channel.call('help'), 'Correct reply sent to real speech coordinator; no ASR/audible judgement.')
        placement = self.channel.call('desktop_state')
        common.write_json(self.artifacts / 'desktop.json', placement)
        if not placement or not all(v is True for v in placement.values()):
            raise RuntimeError('MonitorPlacementNotVerified')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=common.ROOT / 'app')
    parser.add_argument('--corpus', type=Path, default=Path('D:/SamsaraCorpus'))
    parser.add_argument('--model', type=Path)
    parser.add_argument('--report-root', type=Path, default=Path('reports/receipts'))
    parser.add_argument('--force', action='store_true', help='Explicitly rerun today; preserve earlier artifacts')
    args = parser.parse_args(argv)
    began = time.monotonic()
    report = args.report_root.absolute() / datetime.now().astimezone().date().isoformat()
    common.reject_links(report)
    report.mkdir(parents=True, exist_ok=True)
    identity = {'build_ts': None, 'commit': None}
    harness = None
    error = None
    lock = report / 'RUNNING.lock'
    try:
        # Exclusive file is released on normal completion; crash evidence is
        # preserved and a stale lock requires runner inspection, never takeover.
        with lock.open('x', encoding='utf-8') as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        return 1
    previous_deadline = common.DEADLINE
    try:
        identity = common.build_identity(args.app)
        if not identity.get('commit') or not re.fullmatch(r'[0-9a-fA-F]{7,40}', identity['commit']):
            raise ValueError('BuildHeadRequired')
        completion = report / 'completion.json'
        if completion.exists() and not args.force:
            previous = json.loads(completion.read_text(encoding='utf-8'))
            return previous['exit_code'] if previous['build'] == identity else 1
        home = common.ROOT / 'runs' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]) / 'home'
        app, home = validate_daily(args.app, home)
        take = replay_input.choose_take(args.corpus)
        common.reject_links(take)
        model = args.model
        if model is None:
            # Offline recorder configuration only; never a live Samsara profile.
            model = Path(json.loads((args.corpus / 'recorder-settings.json').read_text(encoding='utf-8-sig'))['model_path'])
        model_name = local_model_name(model)
        import win32api
        rect = desktop.monitor_rect([win32api.GetMonitorInfo(m[0]) for m in win32api.EnumDisplayMonitors()])
        import psutil  # noqa: F401
        home.mkdir(parents=True)
        plugins = home / 'personal_plugins'
        plugins.mkdir()
        for source, target in [('installed_probe.py', 'receipt_probe.py'), ('daily_probe.py', 'receipt_daily_probe.py'),
                               ('replay_input.py', 'receipt_replay.py'), ('desktop.py', 'receipt_desktop.py')]:
            shutil.copyfile(Path(__file__).with_name(source), plugins / target)
        common.write_json(home / 'config.json', {
            'first_run_complete': True, 'microphone': None, 'mode': 'hold', 'hotkey': 'f24',
            'model_size': model_name, 'device': 'cpu', 'compute_type': 'int8',
            'game_mode': {'cpu_model': model_name},
            'wake_word_enabled': False, 'launch_at_login': False,
            'command_mode': {'enabled': False},
            'updates': {'automatic_checks': False}, 'plugins': {'personal_dir': 'personal_plugins'},
            'command_packs': {'window-management': True, 'ai': True},
            'audio_coordinator': {'duck_factor': 1.0},
            'ducking': {'enabled': False, 'recording_mode': 'off', 'hands_free_enabled': False},
            'ava_edit': {'enabled': False}, 'ui': {'live_surface': {'enabled': True}},
            'history': {'enabled': False}, 'privacy_mode': True,
        })
        harness = DailyHarness(app, home, report, identity, take, model, rect)
        common.write_json(harness.artifacts / 'input.json', dict(sha256=common.file_hash(take),
                          adapter='InputStream boundary; installed ACE ring, mic check capture/decoder/label'))
        common.DEADLINE = began + 480  # leave <= 65s for normal exit; never force kill
        harness.run()
    except Exception as failure:
        error = type(failure).__name__
    finally:
        common.DEADLINE = None  # cleanup has its own bounded normal-exit wait
        lifecycle_ok = False
        if harness is not None:
            try:
                harness.quit()
                lifecycle_ok = bool(harness.lifecycle) and harness.lifecycle[-1].get('verified') is True
            except Exception as failure:
                error = error or type(failure).__name__
            for feature in FEATURES:
                if feature not in {r['ticket'] for r in harness.rows}:
                    harness.check(feature, 'Run precondition/budget; trigger not executed',
                                  lambda: {'executed': False}, 'No installed observation obtained.')
            for row in harness.rows:
                row['normal_shutdown_verified'] = lifecycle_ok
                common.write_json(Path(row['evidence']), row)
            harness.save()
        # A cached daily result must remain byte-for-byte intact.
        if harness is not None or error:
            if harness is None:
                evidence = report / 'artifacts' / 'claude' / ('preflight-' + uuid.uuid4().hex + '.json')
                rows = [make_row(f, identity, 'Preflight; trigger not executed', evidence,
                                 {'preflight': False}, limitation='Preflight refused: ' + error) for f in FEATURES]
                common.write_json(evidence, dict(error=error, rows=rows))
            else:
                rows = harness.rows
            status = write_summary(report, identity, rows, elapsed=time.monotonic() - began,
                                   lifecycle_ok=lifecycle_ok, error=error)
        common.DEADLINE = previous_deadline
        lock.unlink(missing_ok=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
