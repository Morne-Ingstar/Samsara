"""Clipboard preservation against the shared memory backend; no desktop I/O (1148)."""

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


def test_empty_clipboard(fake_clipboard):
    assert fake_clipboard.OpenClipboard(None)
    try:
        assert fake_clipboard.EmptyClipboard()
    finally:
        fake_clipboard.CloseClipboard()
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
