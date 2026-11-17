"""Runner-only installed-build checks. Never run from a worker or from pytest.

Every PASS is scoped. acceptance_eligible defaults false; component checks must
not be promoted to full voice/game/microphone/recording acceptance by automation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid
import re

try:
    from .common import TICKETS, build_identity, receipt, validate_paths, wait_for, write_json
except ImportError:
    from common import TICKETS, build_identity, receipt, validate_paths, wait_for, write_json


class ProbeError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and re.fullmatch('[A-Za-z]{1,80}', code) else 'Unknown'
        super().__init__(self.code)


class Channel:
    def __init__(self, home, process):
        self.directory, self.process = home / 'receipt_channel', process

    def read(self, name):
        if self.process.poll() is not None:
            raise RuntimeError('Isolated app exited before the observation')
        try:
            return json.loads((self.directory / name).read_text(encoding='utf-8'))
        except (FileNotFoundError, ValueError):
            return None

    def call(self, operation, timeout=40):
        request_id = uuid.uuid4().hex
        write_json(self.directory / 'request.json', dict(id=request_id, operation=operation))
        def response():
            value = self.read('response.json')
            return value if value and value.get('id') == request_id else None
        reply = wait_for(response, timeout=timeout)
        if not reply.get('ok'):
            raise ProbeError(reply.get('error', 'Unknown'))
        return reply['value']


class Harness:
    def __init__(self, app, home, report, identity):
        self.app, self.home, self.report, self.identity = app, home, report, identity
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        self.artifacts = report / 'artifacts' / 'claude' / stamp
        self.artifacts.mkdir(parents=True)
        self.rows, self.process, self.channel, self.scratch_process = [], None, None, None
        self.scratch = None
        self.lifecycle = []

    def launch(self):
        channel = self.home / 'receipt_channel'
        # Old responses belong to the first launch; do not consume them after restart.
        if channel.exists():
            for name in ('request.json', 'response.json', 'ready.json'):
                (channel / name).unlink(missing_ok=True)
        env = dict(os.environ)
        for key in ('SAMSARA_DUCKING_HOST', 'PYTHONPATH', 'PYTHONHOME', 'QT_QPA_PLATFORM',
                    'QT_QPA_FONTDIR', 'SAMSARA_PROFILE', 'SAMSARA_DAILY'):
            env.pop(key, None)
        for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA', 'LOCALAPPDATA',
                    'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'TEMP', 'TMP', 'TMPDIR'):
            env[key] = str(self.home)
        env.update(SAMSARA_RECEIPTS_1241='1', HF_HUB_OFFLINE='1',
                   HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1')
        self.process = subprocess.Popen([str(self.app / 'Samsara.exe')], cwd=str(self.app), env=env)
        self.channel = Channel(self.home, self.process)
        ready = wait_for(lambda: self.channel.read('ready.json'), timeout=180)
        if ready['pid'] != self.process.pid:
            raise RuntimeError('Probe belongs to a different process')
        import psutil
        if Path(psutil.Process(self.process.pid).exe()).resolve() != (self.app / 'Samsara.exe').resolve():
            raise RuntimeError('Unexpected executable identity')
        wait_for(lambda: self.channel.call('state')['ready'], timeout=60)
        self.lifecycle.append(dict(event='started', pid=self.process.pid))

    def quit(self):
        if self.process is None or self.process.poll() is not None:
            return
        # It can exit before the mailbox reply is read: normal, but only accept
        # a successful exit AND removal of the existing session lifecycle marker.
        try:
            self.channel.call('quit', timeout=10)
        except RuntimeError:
            if self.process.poll() is None:
                raise
        self.process.wait(timeout=45)
        marker = self.home / 'logs' / 'session.running'
        good = self.process.returncode == 0 and not marker.exists()
        self.lifecycle.append(dict(event='normal_exit', pid=self.process.pid, verified=good))
        if not good:
            raise RuntimeError('Normal shutdown was not verified')

    def start_scratch(self):
        self.scratch_process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('scratch.py')),
                                                  '--home', str(self.home)], cwd=str(self.home))
        def ready():
            if self.scratch_process.poll() is not None:
                raise RuntimeError('Scratch editor exited')
            try:
                return json.loads((self.home / 'scratch.json').read_text())
            except (FileNotFoundError, ValueError):
                return None
        self.scratch = wait_for(ready)
        if self.scratch['pid'] != self.scratch_process.pid:
            raise RuntimeError('Scratch identity mismatch')

    def editor(self, index=0):
        from pywinauto.controls.hwndwrapper import HwndWrapper
        from pywinauto.controls.win32_controls import EditWrapper
        parent = HwndWrapper(self.scratch['windows'][index])
        edit = EditWrapper(self.scratch['edits'][index])
        if parent.process_id() != self.scratch_process.pid or edit.process_id() != self.scratch_process.pid:
            raise RuntimeError('Scratch window identity changed')
        parent.set_focus()
        edit.set_focus()
        return parent, edit

    def check(self, ticket, trigger, fn, limitation, eligible=False):
        evidence = self.artifacts / (ticket + '.json')
        try:
            observations = fn()
            row = receipt(ticket, self.identity, trigger, evidence, observations,
                          limitation=limitation, eligible=eligible)
        except Exception as error:
            # No repr/error message: UI/COM/OS exceptions can include private text.
            row = receipt(ticket, self.identity, trigger, evidence,
                          {'completed': False}, limitation=limitation + '; ' + type(error).__name__)
            row['failure_code'] = error.code if isinstance(error, ProbeError) else type(error).__name__
        write_json(evidence, row)
        self.rows.append(row)
        self.save()
        print(f"{ticket} {row['result']} | {trigger} | {evidence}", flush=True)

    def save(self):
        write_json(self.report / 'receipts.json', self.rows)
        write_json(self.artifacts / 'lifecycle.json', self.lifecycle)

    def ticket(self):
        # Real installed Cockpit reader, never serialize or echo its contents.
        result = self.channel.call('ticket_lookup')
        self.channel.call('ticket')
        reply = wait_for(lambda: (s if (s := self.channel.call('state'))['ticket_answer'] else None))
        return dict(**result, command_reply_queued=reply['ticket_success'])

    def pace(self):
        self.channel.call('settings')
        from pywinauto import Application
        ui = Application(backend='uia').connect(process=self.process.pid)
        def slider():
            candidates = [control for window in ui.windows() for control in
                          window.descendants(control_type='Slider') if control.is_visible()
                          and control.window_text() == 'How long you can pause']
            return candidates[0] if len(candidates) == 1 else None
        control = wait_for(slider)
        control.set_focus()
        control.type_keys('{HOME}{RIGHT 90}', pause=.005)
        changed = abs(control.value() - 500) < .01
        buttons = [control for window in ui.windows() for control in
                   window.descendants(control_type='Button') if control.is_visible()
                   and control.window_text().replace('&', '').strip() == 'Apply  Close']
        # UIA may collapse the mnemonic ampersand's surrounding spaces.
        if not buttons:
            buttons = [control for window in ui.windows() for control in
                       window.descendants(control_type='Button') if control.is_visible()
                       and ' '.join(control.window_text().replace('&', '').split()) == 'Apply Close']
        if len(buttons) != 1:
            raise RuntimeError('Apply control unavailable')
        buttons[0].invoke()
        wait_for(lambda: self.channel.call('state')['pause'] == 5)
        self.quit()
        self.launch()
        saved = self.channel.call('state')['pause'] == 5
        self.channel.call('settings')
        ui = Application(backend='uia').connect(process=self.process.pid)
        reopened = abs(wait_for(slider).value() - 500) < .01
        return dict(slider_changed=changed, persisted_after_normal_restart=saved,
                    slider_reopened_at_five_seconds=reopened)

    def selection(self):
        self.channel.call('seed_last_dictation')
        _, edit = self.editor(0)
        edit.set_edit_text('This helps me. Live independently.')
        edit.select(0, len(edit.text_block()))
        captured = self.channel.call('selection')
        try:
            self.channel.call('cleanup')
            staged = wait_for(lambda: self.channel.call('state')['edit_pending'])
            uses_selection = self.channel.call('state')['edit_uses_selection']
            self.channel.call('apply')
            wait_for(lambda: edit.text_block() == 'This helps me live independently.')
            return dict(**captured, proposal_staged=bool(staged), uses_selection=uses_selection,
                        selected_text_replaced=True, **self.channel.call('last_dictation_unchanged'))
        finally:
            if self.channel.call('state')['edit_pending']:
                self.channel.call('discard')

    def game(self):
        before = self.channel.call('state')
        try:
            self.channel.call('game_on')
            on = wait_for(lambda: (s if (s := self.channel.call('state'))['game_active'] else None), timeout=120)
            cpu = on['device'] == 'cpu' and 'on CPU (game mode)' in on['game_runtime']
        finally:
            self.channel.call('game_off')
        off = wait_for(lambda: (s if not (s := self.channel.call('state'))['game_active'] else None))
        log = (self.home / 'logs' / 'samsara.log').read_text(encoding='utf-8', errors='replace')
        return dict(cpu_runtime_observed=cpu, original_model_restored=off['model_id'] == before['model_id'],
                    original_device_restored=off['device'] == before['device'],
                    loaded_cpu_log='[GAME-MODE] CPU model loaded' in log,
                    on_log='[GAME-MODE] On reason=manual' in log,
                    off_log='[GAME-MODE] Off reason=manual' in log)

    def timing(self):
        time.sleep(.6)  # Do not turn the app's repeated-command debounce into a timing failure.
        result = self.channel.call('game_off')
        return dict(command_dispatched=result['dispatched'], no_pending_confirmation=not result['pending'],
                    returned_under_one_second=result['elapsed_s'] < 1,
                    **self.channel.call('confirmation_policy'))

    def layouts(self):
        import win32gui
        before = [win32gui.GetWindowRect(h) for h in self.scratch['windows']]
        self.channel.call('layout_save')
        path = self.home / 'Samsara' / 'window_layouts.json'
        saved = json.loads(path.read_text())['receipt']['windows']
        scratch_entries = [entry for entry in saved if entry['title_pattern'].startswith('1241 disposable editor ')]
        if len(scratch_entries) != 2:
            raise RuntimeError('Scratch layout missing')
        # Restore only the disposable editors, never even the app's own windows.
        # Save itself ran unmodified; discard extra isolated-app entries before restore.
        write_json(path, {'receipt': {'windows': scratch_entries}})
        for hwnd in self.scratch['windows']:
            win32gui.SetWindowPos(hwnd, 0, 180, 200, 350, 260, 0x14)
        self.channel.call('layout_restore')
        wait_for(lambda: [win32gui.GetWindowRect(h) for h in self.scratch['windows']] == before)
        return dict(two_disposable_windows_saved=True, moved_then_restored=True)

    def mic(self):
        self.channel.call('mic')
        print('T80: real microphone capture; a spoken synthetic sentence is needed for PASS.', flush=True)
        self.channel.call('mic_start')
        try:
            result = wait_for(lambda: (s if not (s := self.channel.call('mic_state'))['busy'] else None), timeout=60)
            return dict(completed=result['complete'], metrics_saved=result['result_saved'],
                        real_speech_recognised=result['recognised'])
        finally:
            self.channel.call('mic_close')

    def paste(self):
        self.channel.call('seed_paste')
        _, original = self.editor(0)
        original.set_edit_text('Original editor stays unchanged.')
        _, intended = self.editor(1)
        intended.set_edit_text('')
        self.channel.call('paste')
        wait_for(lambda: intended.text_block() == 'The receipt sample is ready.')
        return dict(intended_editor_received_text=True,
                    previous_editor_unchanged=original.text_block() == 'Original editor stays unchanged.')

    def run(self):
        self.start_scratch()
        self.launch()
        self.check('T29', 'Settings > Dictation > How long you can pause: 5 s > Apply & Close > normal restart',
                   self.pace, 'Persistence only; does not prove endpointing with real speech.', True)
        self.check('T10', 'hey ava resume ticket T10 through installed executor; installed ticket-reader field checks',
                   self.ticket, 'Requires Cockpit API; text-command route and TTS queue only, no ASR or audible-answer observation.')
        self.check('T67', 'Select synthetic text in native Edit > clean up selection > apply',
                   self.selection, 'Offline cleanup with a synthetic contrasting committed source; no natural Ava request, ASR or model rewrite.')
        self.check('T68', 'game mode on > observe installed CPU model/runtime/log > game mode off',
                   self.game, 'Manual command only; real-game detection, fullscreen/GPU classification and automatic exit grace remain unproved.')
        self.check('T70', 'game mode off via CommandExecutor; read-only authorize(restart samsara, quiet=True)',
                   self.timing, 'One ordinary command plus irreversible authorization decision; no actual restart or security/privacy confirmation UI exercised.')
        self.check('T73', 'save layout as receipt > move two disposable editors > restore layout receipt',
                   self.layouts, 'Refuses unrelated movable windows; no missing-app launch, browser tabs or monitor hotplug proof.')
        self.check('T80', 'check my mic > Start check > wait for real capture/decoder and diagnostics',
                   self.mic, 'Needs a microphone and real speech during the eight-second capture; silence is a FAIL, never a fabricated pass.')
        self.check('T81', 'Synthetic History observer seed > focus second native editor > paste that again',
                   self.paste, 'Repeat delivery to intended window only; initial voice dictation and focus-change race remain unproved.')
        self.check('T87', 'Installed SessionDucker: scratch audio 100 > 20 > nested 10 > 50 > 100; intentional 73 retained',
                   lambda: self.channel.call('duck'), 'Real WASAPI scratch session and installed engine only; recording lifecycle, Brave and vanished-session recovery remain unproved.')

    def close_scratch(self):
        if self.scratch_process is not None and self.scratch_process.poll() is None and self.scratch:
            import win32gui
            for hwnd in self.scratch['windows']:
                if win32gui.IsWindow(hwnd) and win32gui.GetWindowText(hwnd).startswith('1241 disposable editor '):
                    # Identity was captured from our own child; recheck before WM_CLOSE.
                    import win32process
                    if win32process.GetWindowThreadProcessId(hwnd)[1] == self.scratch_process.pid:
                        win32gui.PostMessage(hwnd, 0x10, 0, 0)
            self.scratch_process.wait(timeout=10)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        app, home = validate_paths(args.app, args.home)
        identity = build_identity(app)
        # Dependencies checked before any launch/profile mutation. No auto-install.
        import psutil  # noqa: F401
        import comtypes.client
        # comtypes supports in-memory generation when gen_dir is None.
        comtypes.client.gen_dir = None
        import pywinauto  # noqa: F401
        import win32gui  # noqa: F401
    except Exception as failure:
        identity = locals().get('identity', {'build_ts': None})
        evidence = args.report / 'artifacts' / 'claude' / ('preflight-' + uuid.uuid4().hex + '.json')
        rows = [receipt(ticket, identity, 'Preflight; feature trigger not executed', evidence,
                        {'preflight': False}, limitation=type(failure).__name__) for ticket in TICKETS]
        write_json(evidence, rows)
        write_json(args.report / 'receipts.json', rows)
        for row in rows:
            print(f"{row['ticket']} FAIL | {row['trigger']} | {evidence}", flush=True)
        return 2
    home.mkdir(parents=True, exist_ok=True)
    plugins = home / 'personal_plugins'
    plugins.mkdir()
    shutil.copyfile(Path(__file__).with_name('installed_probe.py'), plugins / 'receipt_probe.py')
    write_json(home / 'config.json', {
        'first_run_complete': True, 'microphone': 0, 'mode': 'hold',
        'wake_word_enabled': False, 'launch_at_login': False,
        'updates': {'automatic_checks': False},
        'plugins': {'personal_dir': 'personal_plugins'},
        'command_packs': {'window-management': True, 'ai': True},
        'audio_coordinator': {'duck_factor': 1.0},
        'ducking': {'enabled': False, 'recording_mode': 'off', 'hands_free_enabled': False},
        'ava_edit': {'enabled': False}, 'ui': {'live_surface': {'enabled': True}},
    })
    harness = Harness(app, home, args.report.resolve(), identity)
    error = None
    try:
        harness.run()
    except Exception as failure:
        error = type(failure).__name__
    finally:
        # Never kill, signal or discover an owner's process. A failed normal Exit
        # leaves the isolated process for the runner, with lifecycle FAIL evidence.
        try:
            harness.quit()
        except Exception as failure:
            error = error or type(failure).__name__
            harness.lifecycle.append(dict(event='normal_exit_failed', error=type(failure).__name__))
        try:
            harness.close_scratch()
        except Exception as failure:
            error = error or type(failure).__name__
        for ticket in TICKETS:
            if ticket not in {row['ticket'] for row in harness.rows}:
                harness.check(ticket, 'Run precondition / lifecycle',
                              lambda: {'executed': False}, 'Not executed: ' + (error or 'precondition failed'))
        harness.save()
        normal = error is None and bool(harness.lifecycle) and harness.lifecycle[-1].get('verified') is True
        for row in harness.rows:
            row['normal_shutdown_verified'] = normal
            row['acceptance_eligible'] = row['acceptance_eligible'] and normal
            write_json(row['evidence'], row)
        harness.save()
        write_json(harness.artifacts / 'run.json', dict(error=error, lifecycle=harness.lifecycle,
                    instrumented_personal_plugin=True, app=str(app), home=str(home), build=identity))
    return int(error is not None or any(row['result'] != 'PASS' for row in harness.rows))


if __name__ == '__main__':
    raise SystemExit(main())
