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
    from .runner import Harness, ProbeError, ReceiptFailure, ReplyReadLocked, error_details
except ImportError:
    import common, desktop, replay_input
    from runner import Harness, ProbeError, ReceiptFailure, ReplyReadLocked, error_details


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


def validate_daily(app, home, *, root):
    root = common.prepared_root(root)
    app = common.validate_app(app, root)
    home = Path(home).absolute()
    common.validate_profile(home, root, daily=True)
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


def write_summary(report, identity, rows, *, elapsed, lifecycle_ok, error=None, failure_evidence=None,
                  cleanup_outcome='not_started'):
    common.write_json(report / 'receipts.json', rows)
    passed = ({r['ticket'] for r in rows} == FEATURES.keys() and len(rows) == len(FEATURES)
              and all(r['result'] == ('NOT COVERED' if r['ticket'] in NOT_COVERED else 'PASS') for r in rows)
              and lifecycle_ok and not error)
    status = 0 if passed else 1
    lines = ['# Build receipts', '', f"Build head: {identity.get('commit') or 'UNAVAILABLE'}",
             f"Built: {identity.get('build_ts') or 'UNAVAILABLE'}",
             f'Elapsed: {elapsed:.1f} seconds; exit: {status}; normal shutdown: {lifecycle_ok}', '',
             'PASS covers only the recorded scope. NOT COVERED is never acceptance.', '']
    for row in rows:
        lines.append(f"- {row['result']} — {FEATURES[row['ticket']]} ({row['ticket']}) | "
                     f"{row['trigger']} | [evidence]({Path(row['evidence']).as_posix()}) | {row['limitation']}")
        diagnostic = row.get('ticket_lookup_diagnostic')
        if diagnostic:
            fields = ', '.join(f'{key}={value}' for key, value in diagnostic.items())
            evidence = Path(row['ticket_lookup_diagnostic_evidence']).as_posix()
            # PLACEHOLDER COPY (1388c)
            lines.append(f'  - T10 lookup diagnostic: {fields}. [Diagnostic evidence]({evidence})')
    if error:
        # PLACEHOLDER COPY (1351)
        ran = sum(row['ticket'] not in NOT_COVERED for row in rows)
        lines.extend(['', f'Run failed: {ran} of {len(FEATURES) - len(NOT_COVERED)} checks ran.'])
        if failure_evidence:
            lines[-1] += f' [Failure evidence]({failure_evidence.as_posix()})'
        lines.append('Run failure code: ' + error)  # Read by the Daily worker.
    pending = report / 'RECEIPTS.md.tmp'
    pending.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    pending.replace(report / 'RECEIPTS.md')
    common.write_json(report / 'completion.json', dict(build=identity, exit_code=status,
                      elapsed_s=elapsed, normal_shutdown_verified=lifecycle_ok, cleanup_outcome=cleanup_outcome))
    return status


def process_context():
    account, session = None, None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        name = ctypes.create_unicode_buffer(256)
        size = wintypes.ULONG(len(name))
        get_name = ctypes.WinDLL('secur32').GetUserNameExW
        get_name.argtypes = (ctypes.c_int, wintypes.LPWSTR, ctypes.POINTER(wintypes.ULONG))
        get_name.restype = ctypes.c_ubyte
        if get_name(2, name, ctypes.byref(size)):  # NameSamCompatible
            account = re.sub(r'[^\w.@\\ -]', '?', name.value)
        number = wintypes.DWORD()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(number)):
            session = number.value
    return dict(pid=os.getpid(), account=account, session_id=session)


def run_diagnostics(failure, *, phase, operation, path):
    if isinstance(failure, ReceiptFailure):
        # A feature's finally block can fail while it is unwinding an earlier request.
        earlier = failure.__context__
        if isinstance(earlier, ReceiptFailure):
            return run_diagnostics(earlier, phase=phase, operation=operation, path=path) + [failure.diagnostic]
        return [failure.diagnostic]
    return [dict(phase=phase, operation=operation, action=operation, path=path, **error_details(failure))]


class DailyHarness(Harness):
    def __init__(self, app, home, report, identity, take, model, rect, *, root):
        super().__init__(app, home, report, identity, root=root)
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
        self.wait_startup_state(('model_loaded', 'desktop_ready', 'capture_ready'), timeout=120)

    def check(self, feature, trigger, fn, limitation, eligible=False):
        evidence = self.artifacts / (feature + '.json')
        diagnostics = None
        try:
            row = make_row(feature, self.identity, trigger, evidence, fn(), limitation=limitation)
        except Exception as error:
            if isinstance(error, ReceiptFailure):
                if error.diagnostic['phase'] != 'transaction' or error.code == 'ProbeExited':
                    raise
                reply_locked = isinstance(error, ReplyReadLocked)
                dispatch_failed = error.diagnostic['action'] == 'dispatch'
                # A probe dispatch error arrives through a working mailbox.
                if not (reply_locked or dispatch_failed):
                    raise
                diagnostics = run_diagnostics(error, phase='transaction', operation='run_checks', path=None)
                if reply_locked:
                    # PLACEHOLDER COPY (1372)
                    limitation += ' The reply file stayed locked until the read deadline.'
            code = error.code if isinstance(error, ProbeError) else type(error).__name__
            row = make_row(feature, self.identity, trigger, evidence, {'completed': False}, limitation=limitation)
            row['failure_code'] = code
            if diagnostics is not None:
                row['failure_diagnostics'] = diagnostics
        else:
            self.add_ticket_diagnostic(row)
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
        evidence = {}
        try:
            self.channel.call('mic')
            for case in ('match', 'difference'):
                started = self.channel.call('mic_' + case)['started']
                result = common.wait_for(lambda: (s if not (s := self.channel.call('mic_check_state'))['busy'] else None),
                                         timeout=40) if started else {}
                evidence[case] = dict(started=started, **result)
        finally:
            common.write_json(self.artifacts / 'mic-check.json', evidence)
            self.channel.call('mic_check_close')
        return dict(**{case + '_' + key: evidence[case].get(key) is True
                       for case in evidence for key in ('capture_verified', 'metrics_saved', 'checklist_visible',
                                                        'transcript_visibility_verified')})

    def game(self):
        before = self.channel.call('state')
        if before.get('manual_game_fixture') is not True or before['game_active']:
            return dict(manual_fixture_ready=False)
        return dict(manual_fixture_ready=True, **super().game())

    def silent(self):
        # Quiet continuous input is expected. A completed silent hold still warns.
        capture = self.mouse(hold_seconds=1.0)
        notice = dict(notice_visible=False, notice_title_correct=False)

        def visible():
            state = self.channel.call('silence_state')
            notice.update(notice_visible=state['notice_visible'] is True,
                          notice_title_correct=state['notice_title_correct'] is True)
            return notice['notice_visible']

        try:
            common.wait_for(visible, timeout=25)
        except TimeoutError:
            # Retain the completed hold and last notice observation in a failed row.
            pass
        return dict(**capture, **notice)

    def reread(self):
        try:
            self.channel.call('silence_start')
            staged = self.channel.call('reread_stage')
            result = common.wait_for(lambda: (s if (s := self.channel.call('reread_state'))['reread_visible'] else None))
            return dict(**staged, **result)
        finally:
            self.channel.call('continuous_stop')

    def mouse(self, *, hold_seconds=.3):
        try:
            down = self.channel.call('mouse_down')
            common.wait_for(lambda: self.channel.call('mouse_state')['recording'], timeout=5)
            time.sleep(hold_seconds)
        finally:
            self.channel.call('mouse_up')
        common.wait_for(lambda: not any(self.channel.call('mouse_state').values()), timeout=30)
        return dict(capture_started=down['recording'], capture_stopped=True)

    def run(self):
        # No scratch editors, UIA, global input, clipboard or foreground APIs.
        self.launch()
        self.check('T29', 'Settings pace slider to 5 s > Apply & Close > normal restart', self.pace,
                   'Actual Qt controls/signals; no keyboard/UIA; persistence only.')
        self.check('T10', 'hey ava resume ticket T10 via installed executor', self.ticket,
                   'Existing Cockpit reader and reply queue; no voice recognition or audible judgement.')
        self.check('T68', 'game mode on > CPU runtime > game mode off > original model', self.game,
                   'Manual commands; no game detection or automatic exit proof.')
        self.check('T70', 'game mode off; authorize restart without executing restart', self.timing,
                   'One ordinary command and confirmation decision only.')
        self.check('T80', 'Check my mic > quiet and recorded phrase > verify checklist and transcript visibility', self.mic,
                   'Installed capture, metrics, save and UI; controlled recognizer replies; no physical mic or ASR proof.')
        self.check('1252', 'Hold capture with zero PCM > release > visible dead-mic notice', self.silent,
                   'Installed hold silence observer and notice; no physical unplug or stalled-device coverage.')
        self.check('1254', 'Mouse 5 callback down > recording > callback up > stopped', self.mouse,
                   'Behaviour below hook boundary, silence only; physical button, global hook and helper IPC NOT COVERED.')
        self.check('1251', 'Continuous DICTATE > synthetic staged draft > visible Re-read', self.reread,
                   'Real draft/UI with text fixture; full display only; no hands-free ASR or Re-read decode-quality proof.')
        self.check('1135', 'What is a wake word? via installed command executor',
                   lambda: self.channel.call('help'), 'Correct reply sent to real speech coordinator; no ASR/audible judgement.')
        placement = self.channel.call('desktop_state')
        common.write_json(self.artifacts / 'desktop.json', placement)
        if not placement or not all(v is True for v in placement.values()):
            raise ReceiptFailure('transaction', 'desktop_state', 'verify_placement', 'desktop.json',
                                 dict(code='MonitorPlacementNotVerified'))
        for feature, reason in NOT_COVERED.items():
            self.uncovered(feature, reason)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch-root', type=Path, required=True)
    parser.add_argument('--app', type=Path, help='Copied app directory (defaults to SCRATCH_ROOT/app)')
    parser.add_argument('--corpus', type=Path, required=True)
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
    failures = []
    lock = report / 'RUNNING.lock'
    try:
        # Exclusive file is released on normal completion; crash evidence is
        # preserved and a stale lock requires runner inspection, never takeover.
        with lock.open('x', encoding='utf-8') as stream:
            stream.write(str(os.getpid()))
    except FileExistsError:
        return 1
    previous_deadline = common.DEADLINE
    phase, operation, path = 'startup', 'validate_scratch', 'app/Samsara.exe'
    try:
        root = common.prepared_root(args.scratch_root)
        app = common.validate_app(args.app, root)
        operation, path = 'read_build_identity', 'app/BUILD_INFO.txt'
        identity = common.build_identity(app)
        if not identity.get('commit') or not re.fullmatch(r'[0-9a-fA-F]{7,40}', identity['commit']):
            raise ValueError('BuildHeadRequired')
        completion = report / 'completion.json'
        if completion.exists() and not args.force:
            operation, path = 'read_completion', 'completion.json'
            previous = json.loads(completion.read_text(encoding='utf-8'))
            return previous['exit_code'] if previous['build'] == identity else 1
        run_name = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        operation, path = 'prepare_profile', 'runs/<run>/home'
        app, home = validate_daily(app, root / 'runs' / run_name / 'home', root=root)
        operation, path = 'load_recorded_input', 'corpus/takes'
        take = replay_input.choose_take(args.corpus)
        common.reject_links(take)
        operation, path = 'resolve_model', 'model cache'
        model = args.model
        if model is None:
            # Offline recorder configuration only; never a live Samsara profile.
            model = Path(json.loads((args.corpus / 'recorder-settings.json').read_text(encoding='utf-8-sig'))['model_path'])
        model_name = local_model_name(model)
        operation, path = 'verify_monitor', None
        import win32api
        rect = desktop.monitor_rect([win32api.GetMonitorInfo(m[0]) for m in win32api.EnumDisplayMonitors()])
        import psutil  # noqa: F401
        operation, path = 'prepare_profile', 'runs/<run>/home'
        home.mkdir(parents=True)
        plugins = home / 'personal_plugins'
        plugins.mkdir()
        for source, target in [('installed_probe.py', 'receipt_probe.py'), ('daily_probe.py', 'receipt_daily_probe.py'),
                               ('replay_input.py', 'receipt_replay.py'), ('desktop.py', 'receipt_desktop.py')]:
            shutil.copyfile(Path(__file__).with_name(source), plugins / target)
        common.write_json(home / 'config.json', {
            'first_run_complete': True, 'microphone': None, 'mode': 'hold', 'hotkey': 'f24',
            'model_size': model_name, 'device': 'cpu', 'compute_type': 'int8',
            'game_mode': {'cpu_model': model_name, 'detect': 'manual'},
            'wake_word_enabled': False, 'launch_at_login': False,
            'command_mode': {'enabled': False},
            'updates': {'automatic_checks': False}, 'plugins': {'personal_dir': 'personal_plugins'},
            'command_packs': {'window-management': True, 'ai': True},
            'audio_coordinator': {'duck_factor': 1.0},
            # Exercise the real local reply queue at zero playback volume.
            'tts': {'enabled': True, 'feedback_level': 'brief', 'engine': 'winrt', 'volume': 0.0},
            'ducking': {'enabled': False, 'recording_mode': 'off', 'hands_free_enabled': False},
            'ava_edit': {'enabled': False}, 'ui': {'live_surface': {'enabled': True}},
            'history': {'enabled': False}, 'privacy_mode': True,
        })
        harness = DailyHarness(app, home, report, identity, take, model, rect, root=root)
        common.write_json(harness.artifacts / 'input.json', dict(sha256=common.file_hash(take),
                          adapter='InputStream boundary; installed capture, metrics and checklist'))
        common.DEADLINE = began + 480  # leave time for bounded shutdown of the owned app tree
        phase, operation, path = 'transaction', 'run_checks', None
        harness.run()
    except KeyboardInterrupt as failure:
        failures.extend(run_diagnostics(failure, phase=phase, operation=operation, path=path))
        raise
    except Exception as failure:
        failures.extend(run_diagnostics(failure, phase=phase, operation=operation, path=path))
    finally:
        common.DEADLINE = None  # cleanup has its own bounded normal-exit wait
        try:
            lifecycle_ok, rows = False, []
            cleanup = 'not_started'
            cleanup_outcome = 'not_started'
            interrupted = None
            if harness is not None:
                try:
                    harness.quit()
                    lifecycle_ok = harness.shutdown_verified
                except (Exception, KeyboardInterrupt) as failure:
                    failures.extend(run_diagnostics(failure, phase='shutdown', operation='quit', path=None))
                    cleanup = 'normal_exit_unverified'
                    if isinstance(failure, KeyboardInterrupt):
                        interrupted = failure
                if lifecycle_ok:
                    cleanup = 'normal_exit_verified'
                cleanup_outcome = harness.cleanup_outcome
                rows = harness.rows
                for row in rows:
                    row['normal_shutdown_verified'] = lifecycle_ok
                    row['cleanup_outcome'] = cleanup_outcome
                    common.write_json(Path(row['evidence']), row)
                harness.save()
            evidence = None
            if failures:
                evidence = (harness.artifacts / 'run-failure.json' if harness else
                            report / 'artifacts' / 'claude' / ('preflight-' + uuid.uuid4().hex + '.json'))
                common.write_json(evidence, dict(failures=failures, process=process_context(), cleanup=cleanup,
                                                 cleanup_outcome=cleanup_outcome))
            # A cached daily result must remain byte-for-byte intact.
            if harness is not None or failures:
                status = write_summary(report, identity, rows, elapsed=time.monotonic() - began,
                                       lifecycle_ok=lifecycle_ok, error=failures[0]['code'] if failures else None,
                                       failure_evidence=evidence, cleanup_outcome=cleanup_outcome)
            if interrupted is not None:
                raise interrupted
        finally:
            common.DEADLINE = previous_deadline
            lock.unlink(missing_ok=True)
    return status


if __name__ == '__main__':
    raise SystemExit(main())
