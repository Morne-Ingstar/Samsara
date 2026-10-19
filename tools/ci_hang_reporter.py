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
import subprocess
import time

import psutil


def heartbeat_state(directory):
    states = {}
    for path in directory.glob('*.json'):
        try:
            states[path.name] = json.loads(path.read_text(encoding='utf-8'))
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
              max_seconds=5400, poll_seconds=1):
    directory.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, SAMSARA_CI_HEARTBEAT_DIR=str(directory.resolve()),
               PYTHONUNBUFFERED='1')
    with (directory / 'reporter.log').open('a', encoding='utf-8', buffering=1) as log:
        def emit(message):
            line = f'[CI hang reporter {time.strftime("%H:%M:%S")}] {message}'
            log.write(line + '\n')
            print(line, flush=True)

        # Start the supervisor first, then pytest as its background child. This
        # avoids a PowerShell job owning/losing the reporter's console handles.
        with owned_command(command, env) as child:
            root = psutil.Process(child.pid)
            started = progress = last_report = time.monotonic()
            previous = heartbeat_state(directory)
            emit(f'started pytest pid={child.pid}; idle limit={idle_seconds:g}s')
            while child.poll() is None:
                now = time.monotonic()
                current = heartbeat_state(directory)
                if current != previous:
                    previous, progress = current, now
                expired = now - progress >= idle_seconds or now - started >= max_seconds
                if expired or now - last_report >= report_seconds:
                    processes = process_tree(root)
                    emit(json.dumps(dict(processes=describe(processes), heartbeats=current),
                                    sort_keys=True))
                    last_report = now
                if expired:
                    emit('deadline exceeded; dumping stacks before terminating workers/controller')
                    try:
                        dump_stacks(processes, directory, emit)
                    finally:
                        terminate_tree(processes, emit)
                        child.wait(timeout=10)
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
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command or min(args.idle_seconds, args.report_seconds, args.max_seconds) <= 0:
        parser.error('a command and positive deadlines are required')
    return supervise(command, args.state_dir, idle_seconds=args.idle_seconds,
                     report_seconds=args.report_seconds, max_seconds=args.max_seconds)


if __name__ == '__main__':
    raise SystemExit(main())
