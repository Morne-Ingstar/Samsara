"""Queue 203: DictationApp retains its hotkey API while logic lives elsewhere."""

import inspect


METHODS = (
    "parse_hotkey", "get_key_name", "get_active_keys", "check_hotkey_state",
    "get_pressed_keys_debug", "_hands_free_dictation_commit_available",
    "_commit_pending_hands_free_dictation", "on_key_press", "_start_recording_declined",
    "_other_hotkey_held", "_hotkey_state_text", "on_key_release",
    "_install_capslock_hook", "_uninstall_capslock_hook", "_on_capslock_event",
    "_capslock_start_streaming", "_capslock_stop_streaming", "_mouse_hook_bindings",
    "_install_mouse_listener", "refresh_mouse_hook", "_on_mouse_hook_failed",
    "release_mouse_buttons", "_mouse_fallback_to_keyboard", "mouse_hotkey_status",
    "reenable_mouse_hotkey", "_on_mouse_button", "_main_hotkey_toggle_off",
    "_mouse_guard", "_on_main_hotkey_mouse", "_on_command_button",
    "_check_command_mode_key",
)


def test_dictation_hotkey_api_is_thin_same_signature_delegates():
    # Import stays inside the pytest process: importing dictation bare attaches
    # an extra handler to the live log.
    import dictation
    from samsara.hotkeys import HotkeyCluster

    for name in METHODS:
        app_method = getattr(dictation.DictationApp, name)
        extracted = getattr(HotkeyCluster, name)
        assert inspect.signature(app_method) == inspect.signature(extracted), name
        source = inspect.getsource(app_method)
        assert "HotkeyCluster." + name in source, name
