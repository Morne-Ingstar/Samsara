"""1245: registered names classify content independently of diagnostic labels.

All clipboard, owner and memory operations use the in-memory audit6 adapters.
No registration or clipboard access reaches Windows.
"""
import logging

import pytest

from samsara import clipboard as cb
from tests.test_clipboard_audit6 import fake, target  # noqa: F401


CONTENT_NAMES = ('PNG', 'HTML Format', 'Rich Text Format', 'JFIF', 'GIF',
                 'WebP', 'image/svg+xml')
POLICY_NAMES = ('EnterpriseDataProtectionId', *cb.PRIVATE_CLIPBOARD_FORMATS)
UNKNOWN_NAMES = ('FileContents', 'Object Descriptor', 'Embed Source', 'Unknown Format')


@pytest.fixture
def names(fake, monkeypatch):
    registered = {}

    def lookup(fmt, buf, size):
        buf.value = registered.get(fmt, '')[:size - 1]
        return len(buf.value)

    monkeypatch.setattr(cb._user32, 'GetClipboardFormatNameW', lookup)
    return registered


@pytest.mark.parametrize('fmt', [0xC001, 49372, 0xE123, 0xFFFF])
@pytest.mark.parametrize('name', CONTENT_NAMES + POLICY_NAMES)
def test_dynamic_ids_use_raw_names_with_windows_case_rules(names, fmt, name):
    for spelling in (name, name.swapcase()):
        names[fmt] = spelling
        assert cb.registered_format_name(fmt) == spelling
        assert cb.format_label(fmt) == f'{spelling!r} ({fmt})'
        assert cb.is_known_private_format(fmt) is (name in POLICY_NAMES)
        assert cb.is_user_content_format(fmt) is (name in CONTENT_NAMES)


@pytest.mark.parametrize('name', CONTENT_NAMES + POLICY_NAMES)
def test_display_labels_never_determine_policy(names, monkeypatch, name):
    fmt = 0xE321
    names[fmt] = name
    monkeypatch.setattr(cb, 'format_label', lambda _: 'unrelated diagnostic label')
    assert cb.is_known_private_format(fmt) is (name in POLICY_NAMES)
    assert cb.is_user_content_format(fmt) is (name in CONTENT_NAMES)


@pytest.mark.parametrize('fmt', [cb.CF_UNICODETEXT, cb.CF_DIB, cb.CF_HDROP])
def test_standard_content_needs_no_registered_lookup(fake, monkeypatch, fmt):
    def unexpected(*_):
        pytest.fail('standard formats must not look up a registered name')
    monkeypatch.setattr(cb._user32, 'GetClipboardFormatNameW', unexpected)
    assert cb.is_user_content_format(fmt)
    assert not cb.is_known_private_format(fmt)
    assert cb.format_label(fmt) == cb._FORMAT_NAMES[fmt]


@pytest.mark.parametrize('failure', ['zero', 'exception', 'unavailable'])
def test_lookup_failure_grants_no_private_id_exception(fake, monkeypatch, failure):
    if failure == 'exception':
        def lookup(*_):
            raise OSError('synthetic name lookup failure')
        monkeypatch.setattr(cb._user32, 'GetClipboardFormatNameW', lookup)
    elif failure == 'unavailable':
        monkeypatch.setattr(cb, '_user32', None)
    assert cb.registered_format_name(49372) is None
    assert cb.format_label(49372) == 'format 49372'
    assert not cb.is_known_private_format(49372)
    assert not cb.is_user_content_format(49372)


@pytest.mark.parametrize('name', CONTENT_NAMES)
def test_unreadable_registered_content_refuses_preservation_paste(names, fake, target, name):
    fmt = 49372  # Previously treated as private regardless of its actual name.
    names[fmt] = name
    original = {fmt: b'synthetic unreadable content'}
    fake.formats = dict(original)
    fake.no_data_formats.add(fmt)
    snapshot = cb.save_clipboard()
    assert snapshot.lost == {fmt: 'owner provided no data'}
    assert not snapshot.omitted and not snapshot.complete
    assert not cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0)
    assert not target.pasted and fake.empties == 0 and fake.formats == original


@pytest.mark.parametrize('name', POLICY_NAMES)
def test_no_handle_policy_marker_is_not_missing_user_content(names, fake, target, caplog, name):
    fmt = 0xE456
    names[fmt] = name
    fake.formats = {cb.CF_UNICODETEXT: b'o\x00l\x00d\x00\0\0', fmt: b'marker'}
    fake.no_data_formats.add(fmt)
    with caplog.at_level(logging.WARNING):
        snapshot = cb.save_clipboard()
        assert snapshot.complete and not snapshot.omitted
        assert cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0)
    assert target.pasted == ['synthetic transport'] and fake.text() == 'old'
    assert not [r for r in caplog.records if 'snapshot omitted formats' in r.getMessage()]


@pytest.mark.parametrize('name', CONTENT_NAMES + POLICY_NAMES + UNKNOWN_NAMES)
def test_readable_registered_bytes_are_restored_exactly(names, fake, target, name):
    fmt = 0xE567
    names[fmt] = name
    # Includes a real-sized DWORD FALSE for the policy formats; names never
    # justify dropping readable bytes (including history/cloud policy data).
    original = {fmt: b'\0\0\0\0'}
    fake.formats = dict(original)
    assert cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0)
    assert target.pasted == ['synthetic transport']
    assert fake.formats == original and not fake.allocated


@pytest.mark.parametrize('name', UNKNOWN_NAMES + ('',))
def test_no_handle_unknown_or_ole_is_reported_without_blanket_private_exemption(names, fake, name):
    fmt = 0xE678
    names[fmt] = name
    fake.formats = {fmt: b'owner data'}
    fake.no_data_formats.add(fmt)
    snapshot = cb.save_clipboard()
    assert not cb.is_known_private_format(fmt)
    assert snapshot.complete and snapshot.omitted == {fmt: 'owner provided no data'}


@pytest.mark.parametrize('name', ('PNG', 'HTML Format', 'Embed Source', *POLICY_NAMES))
@pytest.mark.parametrize('failure', ['lock', 'read'])
def test_unreadable_memory_still_blocks_even_for_policy_or_ole(names, fake, monkeypatch, name, failure):
    fmt = 0xE789
    names[fmt] = name
    fake.formats = {fmt: b'synthetic bytes'}
    if failure == 'lock':
        monkeypatch.setattr(cb._kernel32, 'GlobalLock', lambda _: 0)
        reason = 'could not lock its memory'
    else:
        def read(*_):
            raise OSError('synthetic unreadable bytes')
        monkeypatch.setattr(cb.ctypes, 'string_at', read)
        reason = 'read error'
    snapshot = cb.save_clipboard()
    assert not snapshot.complete and snapshot.lost == {fmt: reason}


def test_copy_arriving_before_snapshot_returns_wins_over_paste_and_warn(names, fake, target, monkeypatch):
    fmt = 0xE890
    names[fmt] = 'PNG'
    fake.formats = {fmt: b'unreadable image'}
    fake.no_data_formats.add(fmt)
    close = cb._user32.CloseClipboard

    def copy_after_read():
        result = close()
        # A waiting copy can acquire the clipboard once the owner's read
        # releases it, before classification/return to the paste caller.
        fake.user_copies_text('synthetic newer copy')
        return result

    monkeypatch.setattr(cb._user32, 'CloseClipboard', copy_after_read)
    assert not cb.paste_with_preservation('synthetic transport', paste_delay=0, restore_delay=0,
                                          incomplete_snapshot_policy=cb.PASTE_AND_WARN)
    assert fake.text() == 'synthetic newer copy'
    assert fake.empties == 0 and not target.pasted
