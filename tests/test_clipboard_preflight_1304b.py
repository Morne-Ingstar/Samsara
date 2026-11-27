"""Exercise the native transaction against an in-memory Win32 clipboard."""
from io import BytesIO
import struct
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock


from PIL import Image
import pytest

from samsara import clipboard as cb, clipboard_preflight_worker as worker, clipboard_snapshot, paste
from tests.test_clipboard_audit6 import _dropfiles, _utf16, fake, target


_real_capture = worker.capture


def _deliver(**kwargs):
    return cb.paste_with_preservation('synthetic dictation', paste_delay=0, restore_delay=0,
                                      clipboard_fence=True, **kwargs)


def _png():
    image = Image.new('RGBA', (3, 2), (39, 72, 151, 123))
    image.putpixel((2, 1), (19, 14, 45, 231))
    output = BytesIO()
    image.save(output, format='PNG')
    return image, output.getvalue()


def test_null_delayed_render_has_no_write_and_leaves_sequence_intact(fake, target, monkeypatch):
    fmt = fake.RegisterClipboardFormatW('Rich Text Format')
    fake.owner = 42
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original'), fmt: b'delayed'}
    fake.no_data_formats.add(fmt)
    publish = Mock(side_effect=AssertionError('unexpected write'))
    monkeypatch.setattr(cb._user32, 'SetClipboardData', publish)
    initial_seq = fake.seq
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.UNREADABLE)
    assert fake.seq == initial_seq and fake.empties == 0 and not fake.is_open
    assert not target.pasted
    publish.assert_not_called()


def test_stage_sequence_check_runs_inside_open_clipboard(fake, target, monkeypatch):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    native_open = fake.OpenClipboard

    def open_clipboard(hwnd):
        if hwnd:
            fake.user_copies_text('copy during native open')
        return native_open(hwnd)

    monkeypatch.setattr(cb._user32, 'OpenClipboard', open_clipboard)
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.CHANGED)
    assert fake.text() == 'copy during native open'
    assert fake.empties == 0 and not target.pasted and not fake.allocated


def test_cancel_after_preflight_never_stages(fake, target):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    guard = Mock(side_effect=[True, False])
    assert _deliver(before_stage=guard) is False
    assert fake.empties == 0 and not target.pasted and not fake.allocated


def test_refused_legacy_target_preparation_never_stages(fake, target):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    guard = Mock(return_value=False)
    assert _deliver(before_paste=guard) is False
    guard.assert_called_once_with()
    assert fake.empties == 0 and not target.pasted


def test_restore_handle_failure_refuses_before_staging(fake, target):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    fake.alloc_fail_on = 2
    assert _deliver() is False
    assert fake.text() == 'original' and fake.empties == 0 and not target.pasted
    assert not fake.allocated


def test_source_seq_tracks_legitimate_delayed_render_under_lock(fake, target, monkeypatch):
    fake.owner = 42
    fake.formats = {cb.CF_UNICODETEXT: _utf16('delayed original')}
    native_get = fake.GetClipboardData

    def render(fmt):
        fake.seq += 1
        return native_get(fmt)

    monkeypatch.setattr(cb._user32, 'GetClipboardData', render)
    assert _deliver() is True
    assert target.pasted == ['synthetic dictation'] and fake.text() == 'delayed original'


def test_png_survives_missing_bitmap_and_dib_with_pixel_equivalent_dibv5(fake, target, monkeypatch):
    image, original = _png()
    png = fake.RegisterClipboardFormatW('PNG')
    fake.formats = {png: original, cb.CF_BITMAP: b'GDI handle', cb.CF_DIBV5: b'',
                    cb.CF_PALETTE: b'synthesized GDI palette'}
    fake.no_data_formats.add(cb.CF_DIBV5)
    native_get = fake.GetClipboardData

    def read(fmt):
        assert fmt == png, 'complete PNG must prevent forcing redundant formats to render'
        return native_get(fmt)

    monkeypatch.setattr(cb._user32, 'GetClipboardData', read)
    assert _deliver() is True
    assert target.pasted == ['synthetic dictation'] and fake.formats[png] == original
    with clipboard_snapshot._open_dib(fake.formats[cb.CF_DIBV5]) as restored:
        assert restored.size == image.size
        assert restored.convert('RGBA').tobytes() == image.tobytes()


def test_plain_dib_restores_both_full_resolution_representations(fake, target):
    image, _ = _png()
    bitmap = BytesIO()
    image = image.convert('RGB')
    image.save(bitmap, format='BMP')
    original = bitmap.getvalue()[14:]
    fake.formats = {cb.CF_DIB: original}
    assert _deliver() is True
    assert fake.formats[cb.CF_DIB] == original and cb.CF_DIBV5 in fake.formats
    with Image.open(BytesIO(fake.formats[fake.registered['PNG']])) as restored:
        assert restored.size == image.size
        assert restored.tobytes() == image.tobytes()


def test_required_image_representations_fit_in_the_ram_cap(fake, target, monkeypatch):
    _, raw = _png()
    monkeypatch.setattr(clipboard_snapshot, 'PREFLIGHT_BYTES', len(raw) + 124)
    fake.formats = {fake.RegisterClipboardFormatW('PNG'): raw}
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.TOO_LARGE)
    assert not fake.empties and not target.pasted


def test_hung_owner_returns_after_ping_without_opening_clipboard(fake, target, monkeypatch):
    fake.owner = 42
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    ping = Mock(return_value=0)
    monkeypatch.setattr(cb._user32, 'SendMessageTimeoutW', ping)
    started = time.monotonic()
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.HUNG_OWNER)
    assert time.monotonic() - started < clipboard_snapshot.PREFLIGHT_TIMEOUT_S
    assert ping.call_args.args[:6] == (42, 0, 0, 0, 0x0002, 150)
    assert fake.open_attempts == 0 and fake.empties == 0 and not target.pasted


@pytest.fixture
def preflight_process(fake, monkeypatch):
    monkeypatch.setattr(worker, 'capture', _real_capture)
    monkeypatch.setattr(worker, '_window_station', lambda: 'synthetic-station')
    process = Mock(returncode=0)
    process.poll.return_value = 0

    def launch(*args, **kwargs):
        assert fake.open_attempts == 0 and not fake.is_open
        return process

    monkeypatch.setattr(worker, 'subprocess', SimpleNamespace(
        Popen=Mock(side_effect=launch), PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        CREATE_NO_WINDOW=0, TimeoutExpired=subprocess.TimeoutExpired))
    return process


def test_stalled_helper_is_terminated_before_clipboard_publication(fake, target, preflight_process):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    initial_seq = fake.seq
    preflight_process.poll.return_value = None
    preflight_process.communicate.side_effect = [
        subprocess.TimeoutExpired('synthetic helper', 0.5), (b'', None),
    ]
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.HUNG_OWNER)
    preflight_process.kill.assert_called_once_with()
    assert [call.kwargs['timeout'] for call in preflight_process.communicate.call_args_list] == [
        clipboard_snapshot.PREFLIGHT_TIMEOUT_S, worker._EXIT_GRACE_S,
    ]
    assert fake.open_attempts == fake.empties == 0 and not target.pasted
    assert fake.seq == initial_seq and fake.text() == 'original'


def test_helper_drains_payload_larger_than_a_pipe_buffer(fake, preflight_process):
    payload = b'x' * (128 * 1024)
    fake.owner = 0x1020030042
    marker = fake.RegisterClipboardFormatW(cb.PRIVATE_CLIPBOARD_FORMATS[0])
    original = {cb.CF_UNICODETEXT: payload, marker: struct.pack('<I', 0)}
    fake.formats = dict(original)
    saved = worker.read_snapshot(cb, clipboard_snapshot.PREFLIGHT_TIMEOUT_S)
    assert not fake.is_open
    fake.open_attempts = 0
    output = BytesIO()
    worker._write_result(output, saved)
    preflight_process.communicate.return_value = (output.getvalue(), None)

    received = cb.preflight_clipboard()
    assert received == original
    assert received.source_seq == fake.seq and received.source_owner == fake.owner
    assert received.preflight and received.privacy_markers == 1
    preflight_process.communicate.assert_called_once_with(timeout=clipboard_snapshot.PREFLIGHT_TIMEOUT_S)
    preflight_process.kill.assert_not_called()
    assert fake.open_attempts == fake.empties == 0


def test_worker_rejects_other_window_station_without_reading(monkeypatch, capsys):
    monkeypatch.setattr(worker, '_window_station', lambda: 'other-station')
    monkeypatch.setattr(worker.sys, 'argv', ['worker', '0', 'expected-station'])
    read = Mock()
    monkeypatch.setattr(worker, 'read_snapshot', read)
    assert worker.main() == 0
    assert capsys.readouterr().out == 'unreadable'
    read.assert_not_called()


def test_source_and_frozen_worker_commands(monkeypatch):
    monkeypatch.setattr(worker.sys, 'frozen', False, raising=False)
    command = worker._worker_command()
    assert command[1:3] == ['-I', '-B']
    assert command[-1].endswith('clipboard_preflight_worker.py')
    monkeypatch.setattr(worker.sys, 'frozen', True)
    assert len(worker._worker_command()) == 1
    assert worker._worker_command()[0].endswith('samsara-clipboard-preflight.exe')


@pytest.mark.parametrize('fmt', [0x0080, 0x0200, 0x03FF, 'FileGroupDescriptorW', 'FileContents', 'Biff12'])
def test_complex_formats_refused_before_any_payload_read(fake, target, monkeypatch, fmt):
    fmt = fake.RegisterClipboardFormatW(fmt) if isinstance(fmt, str) else fmt
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original'), fmt: b'data'}
    read = Mock()
    monkeypatch.setattr(cb._user32, 'GetClipboardData', read)
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.COMPLEX)
    read.assert_not_called()
    assert fake.empties == 0 and not target.pasted


@pytest.mark.parametrize('process, name', [('rdpclip.exe', 'HTML Format'), ('excel.exe', 'Link Source')])
def test_live_owner_is_refused(fake, target, monkeypatch, process, name):
    fake.owner = 42
    fake.formats = {fake.RegisterClipboardFormatW(name): b'data'}
    monkeypatch.setattr(clipboard_snapshot, '_owner_process', lambda *_: process)
    assert _deliver() == cb.Unsaveable(cb.UnsaveableKind.COMPLEX)
    assert not fake.empties and not target.pasted


@pytest.mark.parametrize('secret', [True, False])
def test_cut_payload_and_privacy_markers_on_stage_and_restore(fake, target, monkeypatch, secret):
    exclude, history, cloud, viewer = (fake.RegisterClipboardFormatW(name)
                                       for name in cb._FENCED_PRIVATE_CLIPBOARD_FORMATS)
    effect = fake.RegisterClipboardFormatW('Preferred DropEffect')
    shell_ids = fake.RegisterClipboardFormatW('Shell IDList Array')
    original = {cb.CF_HDROP: _dropfiles(r'C:\example.txt'),
                effect: struct.pack('<I', 2), shell_ids: b'synthetic IDList'}
    do_not_read = set()
    for name in ('DataObject', 'Ole Private Data'):
        do_not_read.add(fake.RegisterClipboardFormatW(name))
    if secret:
        original.update({exclude: struct.pack('<I', 1), viewer: struct.pack('<I', 0)})
    original.update({history: struct.pack('<I', 1), cloud: struct.pack('<I', 1)})
    fake.formats = {**original, **dict.fromkeys(do_not_read, b'owner-local marker')}
    native_get, native_close = fake.GetClipboardData, fake.CloseClipboard
    writes = []

    def read(fmt):
        assert fmt not in do_not_read
        return native_get(fmt)

    def close():
        if fake.opener:
            writes.append(dict(fake.formats))
        return native_close()

    monkeypatch.setattr(cb._user32, 'GetClipboardData', read)
    monkeypatch.setattr(cb._user32, 'CloseClipboard', close)
    assert _deliver() is True
    one, zero = struct.pack('<I', 1), struct.pack('<I', 0)
    suppression = {history: zero, cloud: zero, viewer: one}
    expected_original = {**suppression, **original}
    assert writes == [{**suppression, exclude: one, cb.CF_UNICODETEXT: _utf16('synthetic dictation')},
                      expected_original]
    assert target.pasted == ['synthetic dictation'] and fake.formats == expected_original


def test_new_owner_with_matching_text_wins_over_restore(fake, target):
    fake.formats = {cb.CF_UNICODETEXT: _utf16('original')}
    html = fake.RegisterClipboardFormatW('HTML Format')

    def external_copy():
        fake.user_copies_text('synthetic dictation')
        fake.formats[html] = b'new rich copy'

    target.after_paste.append(external_copy)
    assert _deliver() is True
    assert fake.formats[html] == b'new rich copy' and fake.empties == 1


def test_clipboard_with_only_a_privacy_marker_preserves_the_marker(fake, target):
    exclude = fake.RegisterClipboardFormatW(cb._FENCED_PRIVATE_CLIPBOARD_FORMATS[0])
    fake.formats = {exclude: struct.pack('<I', 1)}
    assert _deliver() is True
    assert fake.formats[exclude] == struct.pack('<I', 1)
    assert cb.CF_UNICODETEXT not in fake.formats


def test_incomplete_restore_cannot_report_put_back(fake):
    saved = cb.ClipboardSnapshot({cb.CF_UNICODETEXT: _utf16('captured')})
    saved.lost[cb.CF_DIB] = 'unavailable'
    fake.owner = saved.transport_owner = 777
    payloads = dict(saved)
    handles = clipboard_snapshot._prepare_handles(cb, payloads)
    assert clipboard_snapshot._restore_fenced_clipboard(cb, saved, prepared=(payloads, handles)) is False
    assert saved.restore_status == 'partial' and fake.text() == 'captured'


def test_hands_free_refusal_keeps_history():
    from samsara.delivery_text import SessionTextDelivery

    receipt = cb.Unsaveable(cb.UnsaveableKind.TOO_LARGE)
    app = SimpleNamespace(_session_mode_manager=None,
                          _paste_preserving_clipboard=Mock(return_value=paste.DeliveryResult(False, False, receipt)),
                          add_to_history=Mock(), _log_history=Mock())
    adapter = SessionTextDelivery(app)
    assert adapter.inject_prepared('synthetic text') is False
    # 1304i: draft failure must leave the existing History and draft paths alone.
    app.add_to_history.assert_not_called()
    app._log_history.assert_not_called()
