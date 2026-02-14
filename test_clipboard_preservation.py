#!/usr/bin/env python3
"""
Test script for clipboard preservation functionality.
Run this on Windows to verify clipboard save/restore works correctly.

This test uses the centralized clipboard module in samsara/clipboard.py
which has proper 64-bit Windows API type definitions.
"""

import sys
import time

# Check if we're on Windows
if sys.platform != 'win32':
    print("This test must be run on Windows")
    sys.exit(1)

try:
    import pyperclip
except ImportError:
    print("Please install pyperclip: pip install pyperclip")
    sys.exit(1)

# Import the centralized clipboard module
from samsara.clipboard import (
    save_clipboard,
    restore_clipboard,
    paste_with_preservation,
    test_clipboard_preservation as run_module_test,
    clipboard_lock
)


def test_basic_preservation():
    """Test basic save/restore cycle."""
    print("=" * 60)
    print("Test 1: Basic Clipboard Preservation")
    print("=" * 60)
    
    original_content = "ORIGINAL_CLIPBOARD_CONTENT_12345"
    paste_content = "This is the dictated text that gets pasted"
    
    # Step 1: Put original content in clipboard
    print(f"\n1. Setting clipboard to: '{original_content}'")
    pyperclip.copy(original_content)
    time.sleep(0.1)
    
    # Verify it was set
    current = pyperclip.paste()
    if current != original_content:
        print(f"   FAILED: Clipboard is '{current}' instead of expected content")
        return False
    print("   OK: Clipboard set successfully")
    
    # Step 2: Save clipboard
    print("\n2. Saving clipboard content...")
    saved = save_clipboard()
    if not saved:
        print("   FAILED: No clipboard content was saved!")
        return False
    print(f"   OK: Saved {len(saved)} format(s)")
    
    # Step 3: Overwrite with paste content
    print(f"\n3. Overwriting clipboard with: '{paste_content}'")
    pyperclip.copy(paste_content)
    time.sleep(0.1)
    
    current = pyperclip.paste()
    if current != paste_content:
        print(f"   FAILED: Clipboard is '{current}' instead of paste content")
        return False
    print("   OK: Clipboard overwritten")
    
    # Step 4: Simulate paste wait
    print("\n4. Waiting 0.4s (simulating paste operation)...")
    time.sleep(0.4)
    
    # Step 5: Restore clipboard
    print("\n5. Restoring original clipboard content...")
    success = restore_clipboard(saved)
    if not success:
        print("   WARNING: restore_clipboard returned False")
    
    # Step 6: Verify restoration
    print("\n6. Verifying restoration...")
    time.sleep(0.1)
    current = pyperclip.paste()
    
    if current == original_content:
        print(f"   SUCCESS: Clipboard restored to '{current}'")
        return True
    else:
        print(f"   FAILED: Clipboard is '{current}'")
        print(f"           Expected: '{original_content}'")
        return False


def test_empty_clipboard():
    """Test behavior when clipboard is empty."""
    print("\n" + "=" * 60)
    print("Test 2: Empty Clipboard Handling")
    print("=" * 60)
    
    # Clear clipboard (Windows-specific)
    import ctypes
    ctypes.windll.user32.OpenClipboard(None)
    ctypes.windll.user32.EmptyClipboard()
    ctypes.windll.user32.CloseClipboard()
    time.sleep(0.1)
    
    print("\n1. Clipboard cleared")
    
    # Save empty clipboard
    print("\n2. Saving empty clipboard...")
    saved = save_clipboard()
    print(f"   Saved formats: {len(saved)}")
    
    # Restore (should handle empty gracefully)
    print("\n3. Restoring empty clipboard...")
    success = restore_clipboard(saved)
    print(f"   Restore result: {success}")
    
    print("\n   OK: Empty clipboard handled without errors")
    return True


def test_rapid_operations():
    """Test rapid save/restore cycles (simulates continuous dictation)."""
    print("\n" + "=" * 60)
    print("Test 3: Rapid Operations (Continuous Mode Simulation)")
    print("=" * 60)
    
    original = "PERSISTENT_CONTENT_SHOULD_SURVIVE"
    pyperclip.copy(original)
    time.sleep(0.1)
    
    print(f"\n1. Original content: '{original}'")
    
    success_count = 0
    iterations = 5
    
    for i in range(iterations):
        with clipboard_lock:
            saved = save_clipboard()
            pyperclip.copy(f"DICTATION_{i}")
            time.sleep(0.05)  # Quick paste simulation
            restore_clipboard(saved)
        time.sleep(0.05)  # Small gap between operations
        
        current = pyperclip.paste()
        if current == original:
            success_count += 1
        else:
            print(f"   Iteration {i+1}: FAILED - got '{current}'")
    
    print(f"\n2. Results: {success_count}/{iterations} successful restorations")
    
    if success_count == iterations:
        print("   SUCCESS: All rapid operations preserved clipboard")
        return True
    else:
        print("   PARTIAL: Some operations lost clipboard content")
        return False


def test_module_builtin():
    """Run the module's built-in test."""
    print("\n" + "=" * 60)
    print("Test 4: Module Built-in Test")
    print("=" * 60)
    
    result = run_module_test()
    return result


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("SAMSARA CLIPBOARD PRESERVATION TESTS")
    print("=" * 60)
    
    results = []
    
    # Run all tests
    results.append(("Basic Preservation", test_basic_preservation()))
    results.append(("Empty Clipboard", test_empty_clipboard()))
    results.append(("Rapid Operations", test_rapid_operations()))
    results.append(("Module Built-in", test_module_builtin()))
    
    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    all_passed = True
    for name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("ALL TESTS PASSED!")
    else:
        print("SOME TESTS FAILED!")
    print("=" * 60)
    
    sys.exit(0 if all_passed else 1)
