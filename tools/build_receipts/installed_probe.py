"""Runner-installed personal plugin, loaded by the COPIED build, never by pytest.

Instrumentation is deliberately declared: it calls installed command/UI routes
and reads installed state. It neither replaces product modules nor mocks effects.
Only fixed operations are accepted, through the disposable profile's mailbox.
"""
from pathlib import Path
import json
import os
import re
import stat
import sys
import threading
import time


ROOT_ENV = 'SAMSARA_RECEIPT_SCRATCH_ROOT'
MAILBOX_ENV = 'SAMSARA_RECEIPT_MAILBOX'
LIVE_PROFILES = {'.samsara', '.samsara-daily', '.samsara-private'}
TRANSACTION_ID = re.compile(r'[0-9a-f]{32}')


def reject_links(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Isolation path contains a link or reparse point')


def prepared_root(root):
    if not root:
        raise ValueError('Specify the prepared scratch root')
    root = Path(root)
    if (not root.is_absolute() or root == Path(root.anchor) or '..' in root.parts
            or any(part.casefold() in LIVE_PROFILES for part in root.parts)):
        raise ValueError('Scratch root must be an absolute, dedicated directory')
    reject_links(root)
    if not root.is_dir():
        raise ValueError('Prepare the scratch root before running receipts')
    return root


def copied_executable(root):
    executable = root / 'app' / 'Samsara.exe'
    reject_links(executable)
    if not executable.is_file():
        raise ValueError('Copy the installed Daily build into the scratch app directory first')
    return executable


def validate_profile(home, root, *, daily):
    if daily:
        valid = (home.name == 'home' and home.parent.parent == root / 'runs'
                 and home.parent.name.casefold() not in LIVE_PROFILES | {'..'})
    else:
        valid = home == root / 'home'
    if not valid:
        raise ValueError('Use a dedicated receipt home under the scratch root')
    reject_links(home)


def staged_home(*, daily):
    try:
        root = prepared_root(os.environ.get(ROOT_ENV))
        home = Path(os.environ['SAMSARA_HOME_DIR'])
        validate_profile(home, root, daily=daily)
        executable = Path(sys.executable)
        reject_links(executable)
        if executable.resolve() != copied_executable(root).resolve():
            return None
        plugin = Path(__file__)
        if plugin != home / 'personal_plugins' / 'receipt_probe.py':
            return None
        reject_links(plugin)
        if not plugin.is_file():
            return None
    except (OSError, KeyError, ValueError):
        return None
    return home


def start_services(app):
    daily = os.environ.get('SAMSARA_RECEIPTS_1262') == '1'
    if not daily and os.environ.get('SAMSARA_RECEIPTS_1241') != '1':
        return
    expected = staged_home(daily=daily)
    if expected is None:
        return
    if daily:
        sys.path.insert(0, str(Path(__file__).parent))
        import receipt_daily_probe
        receipt_daily_probe.start(app, expected)
        return
    # Windows QSettings normally shares HKCU across profiles. Redirect before
    # the main/history/dictionary windows are constructed; no registry UI state.
    from PySide6.QtCore import QSettings
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(expected / 'qt'))
    from samsara.runtime import thread_registry
    thread_registry.spawn('build-receipts-1241', lambda: serve(app, expected), daemon=True)


def publish(path, data):
    """Publish once. Neither endpoint changes a file after it becomes visible."""
    pending = path.with_suffix('.tmp')
    with pending.open('x', encoding='utf-8') as stream:
        json.dump(data, stream, allow_nan=False)
    if os.name == 'nt':
        pending.rename(path)  # Windows rename refuses an existing destination.
    else:
        os.link(pending, path)
        pending.unlink()


def error_details(error):
    if isinstance(error, UiFailure):
        return error.details
    code = type(error).__name__
    return dict(code=code if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,79}', code) else 'Unknown',
                winerror=getattr(error, 'winerror', None), errno=getattr(error, 'errno', None))


def mailbox_path(home, launch_id):
    if not TRANSACTION_ID.fullmatch(launch_id):
        raise ValueError('Invalid receipt mailbox identity')
    return home / 'receipt_channel' / launch_id


class UiFailure(RuntimeError):
    def __init__(self, details):
        super().__init__(details['code'])
        self.details = details


def on_ui(fn):
    from samsara.ui import qt_runtime
    event, result = threading.Event(), {}

    def run():
        try:
            result['value'] = fn()
        except Exception as error:
            result['error'] = error_details(error)
        finally:
            event.set()
    qt_runtime.post(run)
    if not event.wait(20):
        raise TimeoutError('UI route did not complete')
    if 'error' in result:
        raise UiFailure(result['error'])
    return result.get('value')


def state(app):
    from samsara import execution_policy
    from samsara.ava_edit import session
    game = getattr(app, '_game_mode', None)
    staged = session.pending()
    outcome = getattr(app, '_ava_turn_outcome', None)
    return dict(pid=os.getpid(), ready=bool(getattr(app, 'command_executor', None)
                and getattr(app, 'tray_icon', None)),
                game_active=bool(getattr(game, 'active', False)),
                game_runtime=game.runtime_text() if game else '',
                device=str(getattr(app, 'device_type', '')), model_id=id(getattr(app, 'model', None)),
                pause=(app.config.get('accessibility') or {}).get('speech_pause_s'),
                pending=execution_policy.pending_operation() is not None,
                edit_pending=staged is not None,
                edit_uses_selection=bool(staged and staged.selection_target),
                # Never return proposal, selection or ticket text.
                ticket_answer=bool(outcome and outcome[0] == 'Ticket record'),
                ticket_success=bool(outcome and outcome == ('Ticket record', 'success')))


COMMANDS = {
    'ticket': 'hey ava resume ticket T10',
    'game_on': 'game mode on', 'game_off': 'game mode off',
    'layout_save': 'save layout as receipt', 'layout_restore': 'restore layout receipt',
    'mic': 'check my mic', 'cleanup': 'clean up selection',
    'apply': 'apply', 'discard': 'discard', 'paste': 'paste that again',
}


class UnrelatedWindowsPresent(RuntimeError):
    pass


def perform(app, home, operation):
    if operation == 'state':
        return state(app)
    if operation in COMMANDS:
        if operation.startswith('layout_'):
            # Global save/restore is unsafe if any movable window is not ours.
            from plugins.commands import windows
            scratch = json.loads((home / 'scratch.json').read_text())
            allowed = {os.getpid(), scratch['pid']}
            if any(pid not in allowed for _, _, pid in windows.get_all_movable_windows()):
                raise UnrelatedWindowsPresent()
        started = time.monotonic()
        result = app.command_executor.process_text(COMMANDS[operation], app, force_commands=True)
        return dict(dispatched=result.state.value == 'completed',
                    dispatch_state=result.state.value, elapsed_s=time.monotonic() - started,
                    **state(app))
    if operation == 'settings':
        on_ui(app.open_settings)
        def show():
            window = app._settings_qt._window
            window.show_tab('Dictation')
            window.show()
            window.raise_()
            return True
        on_ui(show)
        return state(app)
    if operation == 'selection':
        from samsara.ava_edit import actions, targets
        request = actions.from_utterance(app, 'clean up selection')
        try:
            bound, _ = targets.bind(request)
            committed = request.snapshot.committed
            return dict(selected=request.snapshot.selection.state == 'selected',
                        contrasting_last_dictation=bool(committed and committed.text != request.snapshot.selection.text),
                        bound_selection=bool(bound and bound.kind == 'selection'),
                        valid=bool(bound and targets.source_valid(app, bound)))
        finally:
            request.snapshot.release()
    if operation == 'seed_last_dictation':
        # Fixture source only: do not replace any resolver, provider or paste method.
        from samsara.session_modes import StackItem
        import psutil
        scratch = json.loads((home / 'scratch.json').read_text())
        manager = app._session_mode_manager
        with manager._dispatch_lock:
            manager._stack.push(StackItem(kind='dictation_chunk', payload='A different previous passage.',
                mode=manager.mode, timestamp=time.monotonic(), extra={
                    'hwnd': scratch['windows'][0],
                    'target_process': psutil.Process(scratch['pid']).name()}))
        return dict(seeded=True)
    if operation == 'last_dictation_unchanged':
        chunk = app._session_mode_manager.top_dictation_chunk()
        return dict(last_dictation_unchanged=bool(chunk and chunk.payload == 'A different previous passage.'))
    if operation == 'confirmation_policy':
        from samsara import execution_policy as policy
        decision = policy.authorize(policy.Invocation('restart samsara', {}, policy.Route.EXACT,
            policy.capture_generation(app)), app=app, executor=app.command_executor, quiet=True)
        return dict(irreversible_requires_confirmation=isinstance(decision, policy.NeedsConfirmation))
    if operation == 'seed_paste':
        # Synthetic input at the existing History observer seam. No voice proof claimed.
        from samsara import paste_again
        paste_again.remember_history(app, dict(entry_type='dictation', status='success',
                                               display_text='The receipt sample is ready.'))
        return dict(seed_present=bool(paste_again.last_text(app)))
    if operation in {'ticket_lookup', 'ticket_lookup_diagnostic'}:
        # Contents stay in memory. This checks the installed reader, not voice/ASR.
        from samsara import ava_tickets
        answer = ava_tickets.lookup('T10')
        if operation == 'ticket_lookup_diagnostic':
            return dict(reason=answer.reason, http_status=answer.http_status)
        return dict(lookup_success=answer.kind == 'success',
                    has_id=answer.text.startswith('T10:'),
                    has_checkpoint='Left off:' in answer.text and 'Next:' in answer.text)
    if operation == 'mic_start':
        def start():
            window = app._mic_check_window
            window.start_check()
            return dict(started=window.busy)
        return on_ui(start)
    if operation == 'mic_state':
        def observe():
            window = app._mic_check_window
            text = window.heard.text()
            return dict(busy=window.busy, complete='Check complete.' in window.instruction.text(),
                        recognised=text.startswith('Samsara heard: ') and text not in {
                            'Samsara heard: waiting for the sentence', 'Samsara heard: not checked',
                            'Samsara heard: nothing recognised', 'Samsara heard: transcription unavailable'},
                        result_saved=(home / 'diagnostics' / 'mic_check.json').is_file())
        return on_ui(observe)
    if operation == 'mic_close':
        on_ui(lambda: app._mic_check_window.close())
        return dict(closed=True)
    if operation == 'duck':
        return duck_probe(home)
    if operation == 'quit':
        # Same normal lifecycle used by tray Exit, never TerminateProcess.
        from samsara.ui import qt_runtime
        qt_runtime.post(lambda: app.quit_app(reason='build-receipts-1241'))
        return dict(quit_requested=True)
    raise ValueError('UnknownOperation')


def duck_probe(home):
    """Real installed WASAPI host + engine, scoped to the scratch session only."""
    from samsara.audio_ducking import SessionDucker, _iter_audio_sessions
    scratch_pid = json.loads((home / 'scratch.json').read_text())['pid']
    sessions = list(_iter_audio_sessions())
    ours = [s for s in sessions if s.pid == scratch_pid]
    if len(ours) != 1:
        raise RuntimeError('ScratchAudioSessionUnavailable')
    target = ours[0]
    # Enumerated PID exclusions protect every other session. Sessions arriving
    # during a lease are not enrolled by the installed 1182 implementation.
    excludes = {s.pid for s in sessions if s.pid != scratch_pid}
    original = target.get_master_volume()
    first, second = SessionDucker(.2, excludes), SessionDucker(.5, excludes)
    readings = {}
    try:
        target.set_master_volume(1.0)
        first.start()
        time.sleep(.2)
        readings['ducked_to_20'] = abs(target.get_master_volume() - .2) < .01
        second.start()
        time.sleep(.2)
        readings['nested_to_10'] = abs(target.get_master_volume() - .1) < .01
        first.stop()
        readings['remaining_lease_50'] = abs(target.get_master_volume() - .5) < .01
        second.stop()
        readings['restored_100'] = abs(target.get_master_volume() - 1) < .01
        first.start()
        time.sleep(.2)
        target.set_master_volume(.73)
        first.stop()
        readings['intentional_change_kept'] = abs(target.get_master_volume() - .73) < .01
    finally:
        first.stop()
        second.stop()
        target.set_master_volume(original)
        for handle in sessions:
            handle.close()
    return readings


def service_requests(channel, handled, dispatch):
    for request in sorted(channel.glob('request-*.json')):
        request_id = request.stem.removeprefix('request-')
        if request_id in handled or not TRANSACTION_ID.fullmatch(request_id):
            continue
        handled.add(request_id)
        response = channel / f'response-{request_id}.json'
        action = 'read_request'
        try:
            data = json.loads(request.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or set(data) != {'id', 'operation'}
                    or data['id'] != request_id or not isinstance(data['operation'], str)):
                raise ValueError('Invalid receipt request')
            try:
                value = dispatch(data['operation'])
                reply = dict(id=request_id, ok=True, value=value)
            except Exception as error:
                # Exception messages may contain ticket/source/device details.
                reply = dict(id=request_id, ok=False, error=error_details(error))
            action = 'publish_response'
            publish(response, reply)
        except (OSError, ValueError, TypeError) as error:
            # Keep serving new requests, including quit. Never redispatch a
            # request when only its reply failed to publish.
            publish_error(channel / f'error-{request_id}.json', error, action)
        else:
            if reply['ok'] and data['operation'] == 'quit':
                return True
    return False


def publish_error(path, error, action):
    try:
        publish(path, dict(error_details(error), action=action))
    except OSError:
        # The runner still bounds the wait if the entire mailbox is inaccessible.
        pass


def serve(app, home, *, handler=perform):
    channel = mailbox_path(home, os.environ.get(MAILBOX_ENV, ''))
    reject_links(channel)
    # Plugin services start during executor construction: wait for app assignment.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if getattr(app, 'command_executor', None) and getattr(app, 'tray_icon', None):
            break
        time.sleep(.2)
    try:
        publish(channel / 'ready.json', dict(pid=os.getpid()))
    except OSError as error:
        publish_error(channel / 'startup-error.json', error, 'publish_ready')
    handled = set()
    while not service_requests(channel, handled, lambda operation: handler(app, home, operation)):
        time.sleep(.1)
