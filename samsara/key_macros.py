"""Keyboard macro system for accessibility.

Supports:
- Tap combos (e.g., triple-tap W to toggle hold W)
- Hotkey triggers (e.g., Ctrl+Shift+R to toggle hold Shift)
- Toggle hold actions (hold/release a key until toggled again)
"""
import time
import threading
from pynput import keyboard
from pynput.keyboard import Controller, Key


# Map string key names to pynput Key objects
SPECIAL_KEYS = {
    'ctrl': Key.ctrl,
    'control': Key.ctrl,
    'shift': Key.shift,
    'alt': Key.alt,
    'tab': Key.tab,
    'enter': Key.enter,
    'return': Key.enter,
    'space': Key.space,
    'backspace': Key.backspace,
    'delete': Key.delete,
    'escape': Key.esc,
    'esc': Key.esc,
    'up': Key.up,
    'down': Key.down,
    'left': Key.left,
    'right': Key.right,
    'home': Key.home,
    'end': Key.end,
    'page_up': Key.page_up,
    'page_down': Key.page_down,
    'caps_lock': Key.caps_lock,
    'f1': Key.f1,
    'f2': Key.f2,
    'f3': Key.f3,
    'f4': Key.f4,
    'f5': Key.f5,
    'f6': Key.f6,
    'f7': Key.f7,
    'f8': Key.f8,
    'f9': Key.f9,
    'f10': Key.f10,
    'f11': Key.f11,
    'f12': Key.f12,
}


def get_key_object(key_name):
    """Convert a key name string to a pynput key object."""
    key_lower = key_name.lower()
    if key_lower in SPECIAL_KEYS:
        return SPECIAL_KEYS[key_lower]
    # Single character keys
    if len(key_name) == 1:
        return key_name.lower()
    return key_name.lower()


def get_key_name(key):
    """Get the string name of a pynput key."""
    try:
        if hasattr(key, 'char') and key.char:
            return key.char.lower()
        elif hasattr(key, 'name'):
            return key.name.lower()
    except AttributeError:
        pass
    return None


class KeyMacroManager:
    """Manages keyboard macros for accessibility features."""

    def __init__(self, config):
        """
        Initialize the macro manager.

        Args:
            config: Dict containing 'key_macros' configuration
        """
        self.config = config
        self.keyboard = Controller()
        self.tap_history = {}  # key -> list of timestamps
        self.held_keys = set()  # Keys currently held by macros
        self.current_modifiers = set()  # Currently pressed modifier keys
        self.listener = None
        self.running = False
        self.on_macro_triggered = None  # Callback for UI feedback

    def start(self):
        """Start listening for macro triggers."""
        macro_config = self.config.get('key_macros', {})
        if not macro_config.get('enabled', False):
            print("[MACROS] Key macros disabled in config")
            return False

        if self.running:
            return True

        self.running = True
        self.listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False  # Don't suppress keys, let them pass through
        )
        self.listener.start()
        print("[MACROS] Key macro listener started")
        return True

    def stop(self):
        """Stop the macro listener and release any held keys."""
        self.running = False
        if self.listener:
            self.listener.stop()
            self.listener = None

        # Release any keys we're holding
        for key in list(self.held_keys):
            self._release_key(key)

        print("[MACROS] Key macro listener stopped")

    def reload_config(self, config):
        """Reload configuration and restart if needed."""
        was_running = self.running
        if was_running:
            self.stop()

        self.config = config

        if was_running:
            self.start()

    def get_held_keys(self):
        """Return the set of currently held keys."""
        return set(self.held_keys)

    def release_all_held(self):
        """Release all keys currently held by macros."""
        for key in list(self.held_keys):
            self._release_key(key)
        print("[MACROS] Released all held keys")

    def _on_press(self, key):
        """Handle key press events."""
        if not self.running:
            return

        key_name = get_key_name(key)
        if not key_name:
            return

        # Track modifier keys
        if key_name in ('ctrl', 'shift', 'alt', 'ctrl_l', 'ctrl_r',
                        'shift_l', 'shift_r', 'alt_l', 'alt_r'):
            # Normalize modifier names
            if 'ctrl' in key_name:
                self.current_modifiers.add('ctrl')
            elif 'shift' in key_name:
                self.current_modifiers.add('shift')
            elif 'alt' in key_name:
                self.current_modifiers.add('alt')

        macros = self.config.get('key_macros', {}).get('macros', [])

        for macro in macros:
            if not macro.get('enabled', True):
                continue

            trigger = macro.get('trigger', {})
            trigger_type = trigger.get('type')

            if trigger_type == 'tap_combo':
                if trigger.get('key', '').lower() == key_name:
                    self._handle_tap(key_name, macro)

            elif trigger_type == 'hotkey':
                if self._check_hotkey_match(trigger, key_name):
                    self._execute_action(macro)

    def _on_release(self, key):
        """Handle key release events."""
        if not self.running:
            return

        key_name = get_key_name(key)
        if not key_name:
            return

        # Track modifier releases
        if 'ctrl' in key_name:
            self.current_modifiers.discard('ctrl')
        elif 'shift' in key_name:
            self.current_modifiers.discard('shift')
        elif 'alt' in key_name:
            self.current_modifiers.discard('alt')

    def _handle_tap(self, key_name, macro):
        """Handle tap combo detection."""
        now = time.time()
        trigger = macro['trigger']
        window = trigger.get('window_ms', 500) / 1000
        required_taps = trigger.get('taps', 3)

        # Clean old taps outside the window
        if key_name not in self.tap_history:
            self.tap_history[key_name] = []

        self.tap_history[key_name] = [
            t for t in self.tap_history[key_name]
            if now - t < window
        ]
        self.tap_history[key_name].append(now)

        # Check if we've reached the required tap count
        if len(self.tap_history[key_name]) >= required_taps:
            self.tap_history[key_name] = []  # Reset tap counter
            self._execute_action(macro)

    def _check_hotkey_match(self, trigger, pressed_key):
        """Check if a hotkey trigger matches the current key state."""
        required_keys = set(k.lower() for k in trigger.get('keys', []))

        # The pressed key should be the non-modifier key in the combo
        non_modifiers = required_keys - {'ctrl', 'shift', 'alt'}
        if not non_modifiers:
            return False

        # Check if pressed key is the trigger key
        if pressed_key not in non_modifiers:
            return False

        # Check if required modifiers are held
        required_modifiers = required_keys & {'ctrl', 'shift', 'alt'}
        return required_modifiers == (self.current_modifiers & required_modifiers)

    def _execute_action(self, macro):
        """Execute a macro's action."""
        action = macro.get('action', {})
        action_type = action.get('type')
        key = action.get('key', '')

        macro_name = macro.get('name', 'Unknown')

        if action_type == 'toggle_hold':
            if key in self.held_keys:
                self._release_key(key)
                print(f"[MACROS] '{macro_name}': Released {key}")
                if self.on_macro_triggered:
                    self.on_macro_triggered(macro_name, 'released', key)
            else:
                self._hold_key(key)
                print(f"[MACROS] '{macro_name}': Holding {key}")
                if self.on_macro_triggered:
                    self.on_macro_triggered(macro_name, 'holding', key)

        elif action_type == 'press':
            # Single key press
            key_obj = get_key_object(key)
            self.keyboard.press(key_obj)
            self.keyboard.release(key_obj)
            print(f"[MACROS] '{macro_name}': Pressed {key}")
            if self.on_macro_triggered:
                self.on_macro_triggered(macro_name, 'pressed', key)

        elif action_type == 'type':
            # Type a string
            text = action.get('text', '')
            self.keyboard.type(text)
            print(f"[MACROS] '{macro_name}': Typed text")
            if self.on_macro_triggered:
                self.on_macro_triggered(macro_name, 'typed', text[:20])

    def _hold_key(self, key):
        """Start holding a key."""
        self.held_keys.add(key)
        key_obj = get_key_object(key)
        self.keyboard.press(key_obj)

    def _release_key(self, key):
        """Release a held key."""
        self.held_keys.discard(key)
        key_obj = get_key_object(key)
        self.keyboard.release(key_obj)


def get_default_macro_config():
    """Return the default key_macros configuration."""
    return {
        "enabled": False,
        "macros": [
            {
                "name": "Auto-run W",
                "enabled": True,
                "trigger": {
                    "type": "tap_combo",
                    "key": "w",
                    "taps": 3,
                    "window_ms": 500
                },
                "action": {
                    "type": "toggle_hold",
                    "key": "w"
                }
            },
            {
                "name": "Sprint Toggle",
                "enabled": False,
                "trigger": {
                    "type": "hotkey",
                    "keys": ["ctrl", "shift", "r"]
                },
                "action": {
                    "type": "toggle_hold",
                    "key": "shift"
                }
            }
        ]
    }
