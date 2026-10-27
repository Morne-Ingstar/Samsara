"""CI step supervisor: diagnostics and deadlines outside pytest's interpreter.

Own only the command launched here and its descendants, never global Python PIDs.
The child inherits the console; reporter output is unbuffered and also persisted.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import queue
import threading
import subprocess
import time
from xml.etree import ElementTree

import psutil


class ConsoleRelay:
    """Console backpressure must never hold the deadline/termination loop."""

    def __init__(self):
        self.pending = queue.Queue(maxsize=128)
        self.thread = threading.Thread(target=self._run, name='ci-console', daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            line = self.pending.get()
            if line is None:
                return
            try:
                print(line, flush=True)
            except (OSError, ValueError):
                return  # The durable reporter log remains authoritative.

    def emit(self, line):
        try:
            self.pending.put_nowait(line)
        except queue.Full:
            pass  # Every line was already written to reporter.log.

    def close(self):
        self.emit(None)
        self.thread.join(timeout=0.1)


def expired_items(states, now):
    # A different worker's progress cannot extend this item's hard deadline.
    return sorted(state['nodeid'] for state in states.values()
                  if state.get('phase') == 'test' and state.get('deadline') is not None
                  and now >= state['deadline'])


def heartbeat_state(directory, *, pids=None):
    states = {}
    for path in directory.glob('*.json'):
        try:
            state = json.loads(path.read_text(encoding='utf-8'))
            # 944: crashed workers leave files behind. Their deadlines must
            # not terminate the replacement workers or the rest of the shard.
            if pids is None or state.get('pid') in pids:
                states[path.name] = state
        except (OSError, ValueError):
            continue  # An atomic replacement can briefly deny access on Windows.
    return states


def process_tree(root):
    try:
        return [root, *root.children(recursive=True)]
    except psutil.NoSuchProcess:
        return []


def describe(processes):
    rows = []
    for process in processes:
        try:
            cpu = process.cpu_times()
            rows.append(dict(pid=process.pid, name=process.name(),
                             cpu_seconds=round(cpu.user + cpu.system, 3)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def dump_stacks(processes, directory, emit):
    spy = shutil.which('py-spy')
    if not spy:
        emit('py-spy unavailable; requesting cooperative faulthandler dumps')
    for process in processes:
        try:
            if 'python' not in process.name().lower():
                continue
            if spy:
                result = subprocess.run([spy, 'dump', '--pid', str(process.pid)],
                                        capture_output=True, timeout=15)
                output = result.stdout + result.stderr
                (directory / f'stacks-{process.pid}.txt').write_bytes(output)
                emit(f'stacks pid={process.pid} exit={result.returncode}\n' +
                     output.decode('utf-8', 'replace'))
            else:
                (directory / f'{process.pid}.dump').touch()
        except (OSError, psutil.Error, subprocess.TimeoutExpired) as exc:
            emit(f'stack dump pid={process.pid}: {type(exc).__name__}')
    if not spy:
        time.sleep(1)  # Bounded fallback; a GIL hold need not acknowledge it.
        for path in directory.glob('faulthandler-*.txt'):
            emit(path.name + '\n' + path.read_text(encoding='utf-8', errors='replace'))


def terminate_tree(processes, emit):
    # psutil.Process checks creation time before kill: a reused PID is safe.
    # Kill before printing so a blocked console cannot defer termination.
    killed = []
    for process in reversed(processes):
        try:
            process.kill()
            killed.append(process.pid)
        except psutil.NoSuchProcess:
            pass
    emit('killed owned process tree: ' + str(killed))


def report_interrupted_session(path, states, elapsed, emit):
    """Record an infrastructure failure without inventing completed test results."""
    root = ElementTree.Element('testsuites')
    if path.exists():
        try:
            existing = ElementTree.parse(path).getroot()
            if existing.tag == 'testsuites':
                root = existing
            elif existing.tag == 'testsuite':
                root.append(existing)
        except (OSError, ElementTree.ParseError):
            # Retain the partial writer output for diagnosis.
            shutil.copyfile(path, path.with_suffix(path.suffix + '.interrupted'))
    pending = sorted({state.get('nodeid') for state in states.values()
                      if state.get('phase') == 'test' and state.get('nodeid')})
    names = pending or ['pytest session (no completed progress available)']
    suite = ElementTree.SubElement(root, 'testsuite', name='ci-interrupted-session',
                                   tests=str(len(names)), failures=str(len(names)),
                                   errors='0', skipped='0', time=f'{elapsed:.3f}')
    detail = ('CI deadline exceeded; the session was terminated. These are last '
              'observed items, not proof that each item itself hung.\n' +
              json.dumps(states, sort_keys=True, indent=2))
    for name in names:
        case = ElementTree.SubElement(suite, 'testcase', classname='ci.interrupted',
                                      name=name, time='0')
        ElementTree.SubElement(case, 'failure', type='CIHang',
                               message='pytest did not finish before the CI deadline').text = detail
    # Aggregate attributes, when present, must include the added failures too.
    for attribute in ('tests', 'failures', 'errors', 'skipped'):
        if attribute in root.attrib:
            root.set(attribute, str(sum(int(s.get(attribute, '0')) for s in root)))
    path.parent.mkdir(parents=True, exist_ok=True)
    pending_path = path.with_suffix(path.suffix + '.tmp')
    ElementTree.ElementTree(root).write(pending_path, encoding='utf-8', xml_declaration=True)
    pending_path.replace(path)
    emit('=========================== short test summary info ===========================')
    for name in names:
        emit(f'FAILED {name} - CI deadline exceeded (last observed item)')
    emit(f'================= {len(names)} failed (interrupted session) in {elapsed:.2f}s =================')
    emit(f'generated interrupted-session junit: {path}')


@contextmanager
def owned_command(command, env):
    """An error in diagnostics must not leave a blocked Popen.__exit__ wait."""
    child = subprocess.Popen(command, env=env)
    try:
        yield child
    finally:
        if child.poll() is None:
            terminate_tree(process_tree(psutil.Process(child.pid)), lambda message: None)
        child.wait(timeout=10)


def supervise(command, directory, *, idle_seconds=600, report_seconds=300,
              max_seconds=5400, poll_seconds=1, junit_path=None):
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SAMSARA_CI_HEARTBEAT_DIR=str(directory.resolve()),
               PYTHONUNBUFFERED='1')
    from contextlib import closing
    with closing(ConsoleRelay()) as console, (directory / 'reporter.log').open(
            'a', encoding='utf-8', buffering=1) as log:
        def emit(message):
            line = f'[CI hang reporter {time.strftime("%H:%M:%S")}] {message}'
            log.write(line + '\n')
            console.emit(line)

        # Start the supervisor first, then pytest as its background child. This
        # avoids a PowerShell job owning/losing the reporter's console handles.
        with owned_command(command, env) as child:
            root = psutil.Process(child.pid)
            started = progress = last_report = time.monotonic()
            previous = heartbeat_state(directory, pids={p.pid for p in process_tree(root)})
            emit(f'started pytest pid={child.pid}; idle limit={idle_seconds:g}s')
            while child.poll() is None:
                now = time.monotonic()
                processes = process_tree(root)
                current = heartbeat_state(directory, pids={p.pid for p in processes})
                if current != previous:
                    previous, progress = current, now
                overdue = expired_items(current, now)
                expired = bool(overdue) or now - progress >= idle_seconds or now - started >= max_seconds
                if expired or now - last_report >= report_seconds:
                    emit(json.dumps(dict(processes=describe(processes), heartbeats=current),
                                    sort_keys=True))
                    last_report = now
                if expired:
                    emit('deadline exceeded; overdue items=' + repr(overdue) +
                         '; dumping stacks before terminating workers/controller')
                    try:
                        dump_stacks(processes, directory, emit)
                    finally:
                        terminate_tree(processes, emit)
                        child.wait(timeout=10)
                        if junit_path is not None:
                            report_interrupted_session(junit_path, current,
                                                       time.monotonic() - started, emit)
                    return 124
                time.sleep(poll_seconds)
            emit(f'pytest exited {child.returncode}')
            return child.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--idle-seconds', type=float, default=600)
    parser.add_argument('--report-seconds', type=float, default=300)
    parser.add_argument('--max-seconds', type=float, default=5400)
    parser.add_argument('--junit-path', type=Path,
                        help='write interrupted-session failures here if pytest cannot finish')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command or min(args.idle_seconds, args.report_seconds, args.max_seconds) <= 0:
        parser.error('a command and positive deadlines are required')
    return supervise(command, args.state_dir, idle_seconds=args.idle_seconds,
                     report_seconds=args.report_seconds, max_seconds=args.max_seconds,
                     junit_path=args.junit_path)


if __name__ == '__main__':
    raise SystemExit(main())
