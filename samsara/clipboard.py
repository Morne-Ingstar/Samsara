"""
Samsara Clipboard Module

Centralized clipboard operations with proper Windows API type handling for 64-bit systems.
Provides save/restore functionality to preserve user's clipboard during paste operations.
"""

import ctypes
import sys
import threading
import time
from typing import Dict, Optional

from samsara.constants import CLIPBOARD_PASTE_DELAY, CLIPBOARD_RESTORE_DELAY

# Global lock to prevent concurrent clipboard operations
clipboard_lock = threading.Lock()

# Try to import pyperclip for cross-platform fallback
try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    pyperclip = None
    HAS_PYPERCLIP = False


def _log_error(msg, exc=None):
    """Log clipboard errors. Visible in console, not disruptive to user."""
    if exc:
        print(f"[CLIPBOARD] {msg}: {exc}")
    else:
        print(f"[CLIPBOARD] {msg}")


def _setup_win32_api():
    """Set up Windows API function signatures for proper 64-bit handling."""
    if sys.platform != 'win32':
        return None, None
    
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    
    # Define proper argument and return types for 64-bit Windows
    # HANDLE is a pointer-sized type (64-bit on 64-bit Windows)
    HANDLE = ctypes.c_void_p
    HWND = ctypes.c_void_p
    UINT = ctypes.c_uint
    SIZE_T = ctypes.c_size_t
    LPVOID = ctypes.c_void_p
    BOOL = ctypes.c_int
    
    # OpenClipboard
    user32.OpenClipboard.argtypes = [HWND]
    user32.OpenClipboard.restype = BOOL
    
    # CloseClipboard
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = BOOL
    
    # EmptyClipboard
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = BOOL
    
    # EnumClipboardFormats
    user32.EnumClipboardFormats.argtypes = [UINT]
    user32.EnumClipboardFormats.restype = UINT
    
    # GetClipboardData
    user32.GetClipboardData.argtypes = [UINT]
    user32.GetClipboardData.restype = HANDLE
    
    # SetClipboardData
    user32.SetClipboardData.argtypes = [UINT, HANDLE]
    user32.SetClipboardData.restype = HANDLE
    
    # GlobalSize
    kernel32.GlobalSize.argtypes = [HANDLE]
    kernel32.GlobalSize.restype = SIZE_T
    
    # GlobalLock
    kernel32.GlobalLock.argtypes = [HANDLE]
    kernel32.GlobalLock.restype = LPVOID
    
    # GlobalUnlock
    kernel32.GlobalUnlock.argtypes = [HANDLE]
    kernel32.GlobalUnlock.restype = BOOL
    
    # GlobalAlloc
    kernel32.GlobalAlloc.argtypes = [UINT, SIZE_T]
    kernel32.GlobalAlloc.restype = HANDLE
    
    # GlobalFree
    kernel32.GlobalFree.argtypes = [HANDLE]
    kernel32.GlobalFree.restype = HANDLE
    
    return user32, kernel32


# Set up API on module load
_user32, _kernel32 = _setup_win32_api()


def save_clipboard() -> Dict[int, bytes]:
    """
    Save all clipboard formats using Windows API.
    
    Returns:
        Dict mapping format ID to raw bytes data.
        Empty dict if clipboard is empty or on error.
    """
    if sys.platform != 'win32':
        # Fallback for non-Windows: just save text
        if HAS_PYPERCLIP:
            try:
                text = pyperclip.paste()
                if text:
                    return {'text': text.encode('utf-8')}
            except Exception as e:
                _log_error("Failed to save clipboard text fallback", e)
        return {}
    
    saved: Dict[int, bytes] = {}
    
    # Retry logic - clipboard may be locked by another application
    max_retries = 15
    retry_delay = 0.02  # Start with 20ms
    clipboard_opened = False
    
    for attempt in range(max_retries):
        if _user32.OpenClipboard(None):
            clipboard_opened = True
            break
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 0.1)  # Cap at 100ms
    
    if not clipboard_opened:
        print("[WARN] Could not open clipboard for save after retries")
        return saved
    
    # Formats that are NOT GlobalAlloc memory handles - calling GlobalSize/GlobalLock on these
    # can cause heap corruption crashes. Only save formats that are actual memory blocks.
    # Standard clipboard formats that use memory handles:
    SAFE_FORMATS = {
        1,   # CF_TEXT
        7,   # CF_OEMTEXT
        13,  # CF_UNICODETEXT
        16,  # CF_LOCALE
    }
    # Additional registered text formats we can safely handle
    # Formats above 0xC000 are registered formats - we'll try common text ones
    
    try:
        fmt = 0
        while True:
            fmt = _user32.EnumClipboardFormats(fmt)
            if fmt == 0:
                break
            
            # Skip formats that aren't memory handles
            # Low formats (2-6, 8-12, 14-15, 17) include bitmaps, metafiles, etc.
            # that use special handles, not GlobalAlloc memory
            if fmt not in SAFE_FORMATS and fmt < 0xC000:
                continue
            
            try:
                handle = _user32.GetClipboardData(fmt)
                if not handle:
                    continue
                
                size = _kernel32.GlobalSize(handle)
                if size <= 0 or size > 100 * 1024 * 1024:  # Skip invalid or >100MB
                    continue
                
                ptr = _kernel32.GlobalLock(handle)
                if ptr:
                    try:
                        raw = ctypes.string_at(ptr, size)
                        saved[fmt] = raw
                    finally:
                        _kernel32.GlobalUnlock(handle)
            except Exception as e:
                _log_error(f"Could not read clipboard format {fmt}", e)
    finally:
        _user32.CloseClipboard()
    
    if saved:
        print(f"[DEBUG] Clipboard saved: {len(saved)} format(s)")
    
    return saved


def restore_clipboard(saved: Dict[int, bytes]) -> bool:
    """
    Restore clipboard formats previously saved by save_clipboard().
    
    Args:
        saved: Dict from save_clipboard()
        
    Returns:
        True if restoration was successful
    """
    if not saved:
        return True  # Nothing to restore is success
    
    if sys.platform != 'win32':
        # Fallback for non-Windows
        text = saved.get('text')
        if text and HAS_PYPERCLIP:
            try:
                pyperclip.copy(text.decode('utf-8'))
                return True
            except Exception as e:
                _log_error("Failed to restore clipboard text fallback", e)
        return False
    
    GMEM_MOVEABLE = 0x0002
    
    # Retry logic - clipboard may be locked
    max_retries = 15
    retry_delay = 0.02
    
    for attempt in range(max_retries):
        if _user32.OpenClipboard(None):
            break
        time.sleep(retry_delay)
        retry_delay = min(retry_delay * 1.5, 0.1)
    else:
        print("[WARN] Could not open clipboard for restore after retries")
        return False
    
    restored_count = 0
    try:
        _user32.EmptyClipboard()
        
        for fmt, raw in saved.items():
            try:
                h = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
                if not h:
                    continue
                
                ptr = _kernel32.GlobalLock(h)
                if ptr:
                    try:
                        ctypes.memmove(ptr, raw, len(raw))
                    finally:
                        _kernel32.GlobalUnlock(h)
                    
                    if _user32.SetClipboardData(fmt, h):
                        restored_count += 1
                    else:
                        # SetClipboardData failed, we need to free the memory
                        _kernel32.GlobalFree(h)
                else:
                    _kernel32.GlobalFree(h)
            except Exception as e:
                _log_error(f"Failed to restore clipboard format {fmt}", e)
    finally:
        _user32.CloseClipboard()
    
    if saved:
        print(f"[DEBUG] Clipboard restored: {restored_count}/{len(saved)} format(s)")
    
    return restored_count > 0 or len(saved) == 0


def paste_with_preservation(text: str, paste_delay: float = CLIPBOARD_PASTE_DELAY, restore_delay: float = CLIPBOARD_RESTORE_DELAY) -> bool:
    """
    Paste text via clipboard while preserving original clipboard content.
    
    This is the main entry point for dictation paste operations.
    
    Args:
        text: Text to paste
        paste_delay: Delay after copying before pasting (seconds)
        restore_delay: Delay after pasting before restoring clipboard (seconds)
        
    Returns:
        True if paste was successful
    """
    if not HAS_PYPERCLIP:
        print("[ERROR] pyperclip not available")
        return False
    
    try:
        import pyautogui
    except ImportError:
        print("[ERROR] pyautogui not available")
        return False
    
    with clipboard_lock:
        saved = save_clipboard()
        
        try:
            # Copy the text to paste
            pyperclip.copy(text)
            
            # Small delay to ensure clipboard is ready
            time.sleep(paste_delay)
            
            # Simulate Ctrl+V
            pyautogui.hotkey('ctrl', 'v')
            
            # Wait for the target application to read the clipboard
            # This is necessary because some apps read clipboard asynchronously
            time.sleep(restore_delay)
            
            return True
            
        except Exception as e:
            print(f"[ERROR] Paste failed: {e}")
            return False
            
        finally:
            # Always restore clipboard, even if paste failed
            if saved:
                restore_clipboard(saved)


# Convenience function for testing
def test_clipboard_preservation() -> bool:
    """
    Test that clipboard preservation works correctly.
    
    Returns:
        True if test passed
    """
    if not HAS_PYPERCLIP:
        print("pyperclip not available for testing")
        return False
    
    original = "ORIGINAL_TEST_CONTENT_" + str(time.time())
    paste_text = "PASTE_TEXT_" + str(time.time())
    
    # Set original content
    pyperclip.copy(original)
    time.sleep(0.1)
    
    # Save
    saved = save_clipboard()
    if not saved:
        print("FAILED: Could not save clipboard")
        return False
    
    # Overwrite
    pyperclip.copy(paste_text)
    time.sleep(0.1)
    
    # Verify overwrite
    if pyperclip.paste() != paste_text:
        print("FAILED: Clipboard overwrite didn't work")
        return False
    
    # Restore
    if not restore_clipboard(saved):
        print("FAILED: Restore returned False")
        return False
    
    time.sleep(0.1)
    
    # Verify restoration
    restored = pyperclip.paste()
    if restored == original:
        print(f"SUCCESS: Clipboard preserved correctly")
        return True
    else:
        print(f"FAILED: Expected '{original}', got '{restored}'")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Clipboard Preservation Test")
    print("=" * 60)
    success = test_clipboard_preservation()
    sys.exit(0 if success else 1)
