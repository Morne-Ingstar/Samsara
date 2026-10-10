"""Opt-in live delivery probe. Takes focus; workers must never run --run.

Run from the repository with the Samsara interpreter:
    python tools/delivery_probe.py --run
Uses the real held-dictation seam without importing/starting dictation.py.
Only probe-owned windows are closed. Does not start/stop Samsara or Daily.
"""
from __future__ import annotations

import argparse
import ctypes
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

TEXT = 'Synthetic delivery probe sentence 474.'


def tk_child():
    """Keep Tk's event loop running while the parent performs a blocking paste."""
    import queue
    import threading
    import tkinter as tk
    root = tk.Tk()
    root.title('Samsara delivery probe 474 - synthetic text only')
    box = tk.Text(root, width=70, height=8)
    box.pack()
    root.update()
    from ctypes import wintypes
    u = ctypes.windll.user32
    u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    u.GetAncestor.restype = wintypes.HWND
    print(int(u.GetAncestor(box.winfo_id(), 2)), flush=True)
    requests = queue.Queue()
    def reader():
        for line in sys.stdin:
            requests.put(line.strip())
        requests.put('close')
    threading.Thread(target=reader, daemon=True).start()
    def poll():
        while not requests.empty():
            command = requests.get()
            if command == 'close':
                root.destroy()
                return
            if command == 'clear':
                box.delete('1.0', 'end')
                box.focus_force()
                print('cleared', flush=True)
            if command == 'count':
                print(len(box.get('1.0', 'end-1c')), flush=True)
        root.after(10, poll)
    root.after(10, poll)
    root.mainloop()


def npp_executable():
    import shutil
    for candidate in (shutil.which('notepad++'), r'C:\Program Files\Notepad++\notepad++.exe',
                      r'C:\Program Files (x86)\Notepad++\notepad++.exe'):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def run():
    if sys.platform != 'win32':
        raise SystemExit('This live probe requires Windows.')
    # Importing clipboard installs a standalone logger. Keep that logger
    # and all Samsara path helpers away from the owner's running app home.
    os.environ['SAMSARA_HOME_DIR'] = tempfile.mkdtemp(prefix='samsara-delivery-probe-474-')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import win32clipboard as wc
    import win32con
    import win32gui as wg
    import win32process as wp
    from PIL import Image
    from samsara import clipboard as cb, paste

    original = cb.save_clipboard()
    if not original.complete:
        raise SystemExit('Aborted: original clipboard cannot be preserved exactly.')
    app = SimpleNamespace(config={}, _TYPED_INJECTION_PROCESSES=(),
        _foreground_wants_typed_injection=lambda: False,
        _flight_foreground_process_name=paste.flight_foreground_process_name,
        _record_undoable_paste=lambda *a, **kw: None,
        adaptive_learner=SimpleNamespace(record_transcription=lambda _text: None))
    image = Image.new('RGB', (1024, 768), '#4080c0')
    png, bmp = io.BytesIO(), io.BytesIO()
    image.save(png, 'PNG')
    image.save(bmp, 'BMP')
    png_fmt = wc.RegisterClipboardFormat('PNG')

    targets = []
    owned_processes = []
    last_owned_seq = cb.get_clipboard_sequence_number()
    modified = False
    failures = 0
    def request(proc, command):
        proc.stdin.write(command + '\n')
        proc.stdin.flush()
        return proc.stdout.readline().strip()
    try:
        tk = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--tk-child'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        owned_processes.append(('tk', tk, None))
        tk_hwnd = int(tk.stdout.readline().strip())
        targets.append(('Tk', tk_hwnd, lambda: request(tk, 'clear'), lambda: int(request(tk, 'count'))))
        exe = npp_executable()
        if exe:
            npp = subprocess.Popen([exe, '-multiInst', '-nosession', '-noPlugin'])
            owned_processes.append(('npp', npp, None))
            found = []
            deadline = time.monotonic() + 10
            while not found and time.monotonic() < deadline:
                def top_window(hwnd, _unused):
                    if wg.IsWindowVisible(hwnd) and wp.GetWindowThreadProcessId(hwnd)[1] == npp.pid:
                        found.append(hwnd)
                wg.EnumWindows(top_window, None)
                time.sleep(.1)
            if not found:
                raise RuntimeError('Probe Notepad++ window did not appear.')
            top = found[0]
            editors = []
            wg.EnumChildWindows(top, lambda h, _: editors.append(h)
                if wg.GetClassName(h) == 'Scintilla' and wg.IsWindowVisible(h) else None, None)
            if not editors:
                raise RuntimeError('Probe Notepad++ editor not found.')
            editor = editors[0]
            owned_processes[-1] = ('npp', npp, (top, editor))
            targets.append(('Notepad++', top,
                lambda: wg.SendMessage(editor, 2004, 0, 0),
                lambda: wg.SendMessage(editor, 2006, 0, 0)))
        else:
            print('target=Notepad++ SKIP reason=not_installed', flush=True)

        for name, hwnd, clear, count in targets:
            for starting in ('text', 'png'):
                for method in (('held_delivery', 'keystroke', 'message', 'typed') if name == 'Notepad++'
                               else ('held_delivery', 'typed')):
                  for repetition in range(0, 4):  # uncounted warm-up per column

                    clear()
                    wg.ShowWindow(hwnd, win32con.SW_RESTORE)
                    wg.SetForegroundWindow(hwnd)
                    time.sleep(.2)
                    if wg.GetForegroundWindow() != hwnd:
                        raise RuntimeError('Aborted: probe target does not have focus.')
                    if last_owned_seq is not None and cb.get_clipboard_sequence_number() != last_owned_seq:
                        raise RuntimeError('Aborted: clipboard changed outside the probe.')
                    wc.OpenClipboard(hwnd)
                    try:
                        modified = True
                        wc.EmptyClipboard()
                        if starting == 'text':
                            wc.SetClipboardData(wc.CF_UNICODETEXT, 'Synthetic prior clipboard 474')
                        else:
                            wc.SetClipboardData(png_fmt, png.getvalue())
                            wc.SetClipboardData(wc.CF_DIB, bmp.getvalue()[14:])
                    finally:
                        wc.CloseClipboard()
                        last_owned_seq = cb.get_clipboard_sequence_number()
                    expected = cb.save_clipboard()
                    measured = {}
                    def observed(confirmed):
                        time.sleep(.1)
                        measured['chars'] = count()  # BEFORE restore, including keystroke failures
                    def central(text, **kwargs):
                        observer = kwargs.pop('delivery_observer', None)
                        def combined(confirmed):
                            observed(confirmed)
                            if observer:
                                observer(confirmed)
                        return cb.paste_with_preservation(text, delivery_observer=combined, **kwargs)
                    real_message = cb.message_paste_to_focused_control
                    try:
                        if method == 'keystroke':
                            cb.message_paste_to_focused_control = lambda: cb.DeliveryOutcome.SENT
                        if method == 'typed':
                            sent = cb.type_text_unicode(TEXT)  # reproduce burst losses for architect
                            acknowledged = False
                            outcome = cb.DeliveryOutcome.UNVERIFIED
                            observed(False)
                        else:
                            result = paste.paste_preserving_clipboard(app, TEXT,
                                before_paste=lambda: wg.GetForegroundWindow() == hwnd,
                                return_delivery_confirmation=True, paste_with_preservation_fn=central)
                            sent, acknowledged = result
                            outcome = result.outcome
                    finally:
                        cb.message_paste_to_focused_control = real_message
                    actual = cb.save_clipboard()
                    restored = actual.complete and dict(actual) == dict(expected)
                    if restored:
                        last_owned_seq = cb.get_clipboard_sequence_number()
                    time.sleep(.4)
                    focused = wg.GetForegroundWindow() == hwnd
                    got = measured.get('chars', 0)
                    passed = sent and focused and restored and got == len(TEXT)
                    # Keystroke/typed columns diagnose known failures; the real
                    # held seam and Scintilla message column are the gate.
                    if repetition and method in ('held_delivery', 'message'):
                        failures += int(not passed)
                    if repetition:
                      print(f'target={name} clipboard={starting} run={repetition} method={method} '
                          f'chars={got}/{len(TEXT)} sent={sent} outcome={outcome.value} acknowledged={acknowledged} '
                          f'focus={focused} clipboard_restored={restored} result={"PASS" if passed else "FAIL"}', flush=True)
                    if not focused or not restored:
                        raise RuntimeError('Aborted: focus or clipboard ownership changed.')
    finally:
        # Guard against a real user copy while the probe is running.
        # Empty original snapshots also need the original empty clipboard back.
        original.seq = last_owned_seq
        if modified and original:
            if not cb.restore_clipboard(original):
                print('original_clipboard_restored=False', flush=True)
                failures += 1
        elif modified and last_owned_seq is not None and cb.get_clipboard_sequence_number() == last_owned_seq:
            wc.OpenClipboard()
            try:
                wc.EmptyClipboard()
            finally:
                wc.CloseClipboard()
        for kind, proc, hwnd in owned_processes:
            if kind == 'tk':
                request(proc, 'close')
            elif hwnd:
                top, editor = hwnd
                wg.SendMessage(editor, 2004, 0, 0)  # synthetic document only
                wg.SendMessage(editor, 2014, 0, 0)  # mark document saved; no save dialog
                wg.PostMessage(top, win32con.WM_CLOSE, 0, 0)
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Run live cases; takes keyboard focus.')
    parser.add_argument('--tk-child', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.tk_child:
        tk_child()
        return 0
    if not args.run:
        parser.print_help()
        return 0
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
