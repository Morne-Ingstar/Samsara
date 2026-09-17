"""Manual, real-Windows clipboard preservation checks.

These tests are deliberately marked ``clipboard`` because they operate on the
real clipboard.  The autouse fixture takes the central multi-format snapshot
before each case and restores it in ``finally``; an incomplete snapshot skips
the test rather than replacing content it cannot put back.
"""

import ctypes
import sys
import time

import pytest

from samsara.clipboard import (
    restore_clipboard,
    save_clipboard,
    test_clipboard_preservation as run_module_test,
    clipboard_lock,
)


pytestmark = pytest.mark.clipboard


@pytest.fixture(autouse=True)
def preserve_real_clipboard():
    """Restore every format the central snapshot can safely preserve."""
    snapshot = save_clipboard()
    if not getattr(snapshot, "complete", True):
        pytest.skip("clipboard has content that cannot be safely restored")
    was_empty = not snapshot
    try:
        yield
    finally:
        if was_empty:
            if not ctypes.windll.user32.OpenClipboard(None):
                pytest.fail("could not open clipboard to restore its empty state")
            try:
                if not ctypes.windll.user32.EmptyClipboard():
                    pytest.fail("could not restore the clipboard's empty state")
            finally:
                ctypes.windll.user32.CloseClipboard()
        elif not restore_clipboard(snapshot):
            pytest.fail("could not restore the clipboard captured before this test")


def test_basic_preservation():
    import pyperclip

    original_content = "ORIGINAL_CLIPBOARD_CONTENT_12345"
    paste_content = "This is the dictated text that gets pasted"
    pyperclip.copy(original_content)
    time.sleep(0.1)
    assert pyperclip.paste() == original_content

    saved = save_clipboard()
    assert saved
    pyperclip.copy(paste_content)
    time.sleep(0.1)
    assert pyperclip.paste() == paste_content

    assert restore_clipboard(saved)
    time.sleep(0.1)
    assert pyperclip.paste() == original_content


def test_empty_clipboard():
    assert ctypes.windll.user32.OpenClipboard(None)
    try:
        assert ctypes.windll.user32.EmptyClipboard()
    finally:
        ctypes.windll.user32.CloseClipboard()
    assert not save_clipboard()


def test_rapid_operations():
    import pyperclip

    original = "PERSISTENT_CONTENT_SHOULD_SURVIVE"
    pyperclip.copy(original)
    time.sleep(0.1)
    for index in range(5):
        with clipboard_lock:
            saved = save_clipboard()
            pyperclip.copy(f"DICTATION_{index}")
            time.sleep(0.05)
            assert restore_clipboard(saved)
        time.sleep(0.05)
        assert pyperclip.paste() == original


def test_module_builtin():
    assert run_module_test()
