"""Runner-only disposable native Edit windows; no files or owner windows opened."""
import argparse
import json
import os
from pathlib import Path


def main():
    import win32con as c
    import win32gui as g
    import winsound
    import struct
    import wave
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    from common import ROOT, reject_links, write_json
    reject_links(args.home)
    if args.home.resolve() != (ROOT / 'home').resolve():
        raise ValueError('Scratch windows require the isolated home')
    windows = []

    def wndproc(hwnd, message, wparam, lparam):
        if message == c.WM_CLOSE:
            g.DestroyWindow(hwnd)
            windows.remove(hwnd)
            if not windows:
                g.PostQuitMessage(0)
            return 0
        return g.DefWindowProc(hwnd, message, wparam, lparam)

    wc = g.WNDCLASS()
    wc.lpszClassName, wc.lpfnWndProc = 'BuildReceipts1241', wndproc
    wc.hCursor = g.LoadCursor(0, c.IDC_ARROW)
    atom = g.RegisterClass(wc)
    edits = []
    for index in range(2):
        hwnd = g.CreateWindow(atom, '1241 disposable editor ' + str(index + 1),
                              c.WS_OVERLAPPEDWINDOW | c.WS_VISIBLE,
                              70 + index * 460, 120, 430, 300, 0, 0, 0, None)
        windows.append(hwnd)
        edit = g.CreateWindowEx(c.WS_EX_CLIENTEDGE, 'Edit', '',
                                c.WS_CHILD | c.WS_VISIBLE | c.ES_MULTILINE | c.WS_VSCROLL,
                                10, 10, 390, 240, hwnd, 0, 0, None)
        edits.append(edit)
    # Silent, continuously open, owned WASAPI session. Never change master volume.
    audio = args.home / 'receipt_silence.wav'
    with wave.open(str(audio), 'wb') as stream:
        stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        stream.writeframes(struct.pack('<h', 0) * 16000)
    winsound.PlaySound(str(audio), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_LOOP)
    write_json(args.home / 'scratch.json', dict(pid=os.getpid(), windows=windows, edits=edits))
    try:
        g.PumpMessages()
    finally:
        winsound.PlaySound(None, 0)


if __name__ == '__main__':
    main()
