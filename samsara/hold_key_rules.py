"""Hold-key rules and shared capture/listener seams. No hooks on import.

The rules are pure; Qt and pynput are imported only when constructing their
adapters. Saved bindings are never migrated or validated on load.
"""
from dataclasses import dataclass
from types import SimpleNamespace
import weakref

_capture_ref = None
_captured_mouse = set()


def route_capture(name, pressed):
    """Queue physical events to the current Qt picker, before app/foreground work."""
    button = _capture_ref() if _capture_ref is not None else None
    if button is None or not getattr(button, '_capturing', False):
        return False
    from samsara.ui import qt_runtime
    if not qt_runtime.is_alive():
        return False

    def deliver():
        current = _capture_ref() if _capture_ref is not None else None
        if current is button and getattr(button, '_capturing', False):
            button.capture_physical(name, pressed)
    qt_runtime.post(deliver)
    return True


def route_mouse_capture(name, pressed):
    captured = route_capture(name, pressed)
    if pressed and captured:
        _captured_mouse.add(name)
    if not pressed and name in _captured_mouse:
        _captured_mouse.discard(name)
        return True
    return captured

ALIASES = {
    'rctrl': 'right_ctrl', 'lctrl': 'left_ctrl', 'ctrl_r': 'right_ctrl',
    'ctrl_l': 'left_ctrl', 'control': 'ctrl',
    'ralt': 'right_alt', 'lalt': 'left_alt', 'alt_r': 'right_alt',
    'alt_l': 'left_alt', 'alt_gr': 'right_alt',
    'rshift': 'right_shift', 'lshift': 'left_shift',
    'shift_r': 'right_shift', 'shift_l': 'left_shift',
    'esc': 'escape', 'return': 'enter', 'del': 'delete', 'ins': 'insert',
    'pageup': 'page_up', 'pagedown': 'page_down', 'pgup': 'page_up',
    'pgdn': 'page_down', 'caps lock': 'capslock', 'caps_lock': 'capslock',
    'num lock': 'numlock', 'num_lock': 'numlock',
    'scroll lock': 'scroll_lock', 'scrolllock': 'scroll_lock',
    'menu': 'menu', 'apps': 'menu', 'cmd': 'win', 'cmd_l': 'win',
    'cmd_r': 'win', 'super': 'win', 'left_win': 'win', 'right_win': 'win',
    'break': 'pause', 'pause/break': 'pause',
}
MODIFIERS = frozenset(('ctrl', 'alt', 'shift', 'left_ctrl', 'right_ctrl',
                       'left_alt', 'right_alt', 'left_shift', 'right_shift'))
KEYPAD_SCANS = dict(zip((82, 79, 80, 81, 75, 76, 77, 71, 72, 73, 83),
                       [f'numpad{n}' for n in range(10)] + ['numpad_decimal']))
VK_NAMES = {
    8: 'backspace', 9: 'tab', 13: 'enter', 16: 'shift', 17: 'ctrl', 18: 'alt',
    19: 'pause', 20: 'capslock', 27: 'escape', 32: 'space',
    33: 'page_up', 34: 'page_down', 35: 'end', 36: 'home',
    37: 'left', 38: 'up', 39: 'right', 40: 'down', 45: 'insert', 46: 'delete',
    91: 'win', 92: 'win', 93: 'menu', 144: 'numlock', 145: 'scroll_lock',
    160: 'left_shift', 161: 'right_shift', 162: 'left_ctrl', 163: 'right_ctrl',
    164: 'left_alt', 165: 'right_alt',
    106: 'numpad_multiply', 107: 'numpad_add', 109: 'numpad_subtract',
    110: 'numpad_decimal', 111: 'numpad_divide',
    **{96 + n: f'numpad{n}' for n in range(10)},
    **{111 + n: f'f{n}' for n in range(1, 25)},
    **{ord(c): c.lower() for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'},
    186: ';', 187: '=', 188: ',', 189: '-', 190: '.', 191: '/',
    192: '`', 219: '[', 220: '\\', 221: ']', 222: "'",
}
ALONE = frozenset([f'f{n}' for n in range(1, 25)] +
                  [f'numpad{n}' for n in range(10)] +
                  ['numpad_add', 'numpad_subtract', 'numpad_multiply',
                   'numpad_divide', 'numpad_decimal', 'numpad_enter',
                   'insert', 'delete', 'home', 'end', 'page_up', 'page_down',
                   'pause', 'scroll_lock', 'right_ctrl', 'right_alt',
                   'right_shift', 'menu', 'mouse4', 'mouse5'])
KNOWN = ALONE | MODIFIERS | frozenset(VK_NAMES.values())
WARNING = 'This key will also act in the program in front.'  # owner approved
REFUSALS = {
    'secure': 'Windows reserves Ctrl+Alt+Delete; Samsara cannot capture it.',
    'win': 'The Windows key is reserved for Windows shortcuts.',
    'capslock': 'Caps Lock changes letter case; choose another key.',
    'numlock': 'Num Lock changes the number pad; choose another key.',
    'typing': 'Use a modifier with this key so ordinary typing still works.',
    'unknown': 'Samsara cannot capture this key on the desktop.',
    'shape': 'Choose one key, with optional Ctrl, Alt or Shift.',
    'mouse_combo': 'Use Mouse 4 or Mouse 5 on its own.',
}  # owner approved copy
HOLD_DEFAULTS = {
    'hotkey': ('Primary dictation', 'ctrl+shift'),
    'command_hotkey': ('Command only', 'ctrl+alt+c'),
    'memo_hotkey': ('Voice memo', 'ctrl+alt+m'),
    'task_hotkey': ('Task', 'ctrl+alt+t'),
    'ava_mode_key': ('Ava', 'right_alt'),
    'command_mode.button': ('Hands-free', 'rctrl'),
}
OTHER_DEFAULTS = {
    'continuous_hotkey': ('Continuous toggle', 'ctrl+alt+d'),
    'wake_word_hotkey': ('Wake toggle', 'ctrl+alt+w'),
    'cancel_hotkey': ('Cancel recording', 'escape'),
    'undo_hotkey': ('Undo', 'ctrl+alt+z'),
    'correction_hotkey': ('Correction report', 'ctrl+alt+r'),
    'hotkeys.capture_correction': ('Correction capture', 'ctrl+alt+x'),
    'alarms.complete_hotkey': ('Complete alarm', 'f7'),
    'alarms.dismiss_hotkey': ('Dismiss alarm', 'f8'),
    'ui.live_surface.pause_hotkey': ('Pause display', 'pause'),
    'ui.live_surface.focus_hotkey': ('Focus display', 'ctrl+alt+f10'),
    'ui.live_surface.reset_hotkey': ('Reset display', 'ctrl+alt+shift+f10'),
}


def parts(binding):
    return frozenset(ALIASES.get(p.strip().lower(), p.strip().lower())
                     for p in str(binding or '').split('+') if p.strip())


def base_modifier(name):
    return name.split('_')[-1] if name in MODIFIERS else name


def combo_string(keys):
    order = {'ctrl': 0, 'shift': 1, 'alt': 2, 'win': 3}
    return '+'.join(sorted(keys, key=lambda k: (order.get(base_modifier(k), 9), k)))


def binding_down(binding, down):
    wanted = parts(binding)
    available = set(down) | {base_modifier(k) for k in down}
    return bool(wanted) and wanted <= available


def overlapping_bindings(a, b):
    """Generic modifiers conflict with either side, never with another chord."""
    aa, bb = parts(a), parts(b)
    if len(aa) != len(bb):
        return False
    for key in aa:
        if not any(key == other or
                   (key in MODIFIERS and other in MODIFIERS and
                    base_modifier(key) == base_modifier(other) and
                    (key == base_modifier(key) or other == base_modifier(other)))
                   for other in bb):
            return False
    return True


def config_value(config, path, default=None):
    for key in path.split('.'):
        if not isinstance(config, dict) or key not in config:
            return default
        config = config[key]
    return config


def assignments(config, overrides=None):
    from samsara.constants import DEFAULT_CONTINUOUS_COMMIT_HOTKEY
    defaults = dict(HOLD_DEFAULTS, **OTHER_DEFAULTS)
    for key, label in [('dictate_commit_hotkey', 'Paste staged thought'),
                       ('continuous_commit_hotkey', 'Continuous commit')]:
        defaults[key] = (label, DEFAULT_CONTINUOUS_COMMIT_HOTKEY)
    result = {key: (label, config_value(config, key, default))
              for key, (label, default) in defaults.items()}
    for key, value in (overrides or {}).items():
        if key in result:
            result[key] = (result[key][0], value)
    return result


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ''
    warning: str = ''


def validate(binding, occupied=None, action=None):
    keys = parts(binding)
    generic = {base_modifier(k) for k in keys}
    if {'ctrl', 'alt', 'delete'} <= generic:
        return Decision(False, REFUSALS['secure'])
    for reserved in ('win', 'capslock', 'numlock'):
        if reserved in keys:
            return Decision(False, REFUSALS[reserved])
    if not keys or not keys <= KNOWN:
        return Decision(False, REFUSALS['unknown'])
    nonmods = keys - MODIFIERS
    if ('mouse4' in keys or 'mouse5' in keys) and len(keys) > 1:
        return Decision(False, REFUSALS['mouse_combo'])
    if len(nonmods) > 1:
        return Decision(False, REFUSALS['shape'])
    if len(keys) == 1 and not keys <= ALONE:
        return Decision(False, REFUSALS['typing'])
    for other, (label, saved) in (occupied or {}).items():
        if other != action and overlapping_bindings(binding, saved):
            return Decision(False, f'Already used by {label}; choose another key.')
    return Decision(True, warning=WARNING if len(keys) > 1 else '')


def physical_key(vk, scan=0, flags=0):
    """Distinguish keypad navigation from dedicated navigation via scan/extended."""
    extended = bool(flags & 1)
    if scan in KEYPAD_SCANS and not extended and vk != 19:
        return KEYPAD_SCANS[scan]
    if scan == 28 and extended:
        return 'numpad_enter'
    if vk in (16, 17, 18):
        return {16: 'right_shift' if scan == 54 else 'left_shift',
                17: 'right_ctrl' if extended else 'left_ctrl',
                18: 'right_alt' if extended else 'left_alt'}[vk]
    return VK_NAMES.get(vk)


def swallow_binding(binding):
    """Legacy refused keys are left alone; chord modifiers remain foreground-visible."""
    decision = validate(binding)
    keys = parts(binding)
    if not decision.allowed:
        return frozenset()
    return keys if len(keys) == 1 else keys - MODIFIERS


class SuppressionState:
    """Pure Win32 filter seam, including repeat/up and remapping while held."""
    def __init__(self, config):
        self.config = config
        self.down = set()
        self.swallowed = set()

    def event(self, name, pressed, injected=False, captured=False):
        if injected or name is None:
            return False
        if pressed:
            self.down.add(name)
            if captured:
                self.swallowed.add(name)
                return True
            if name in self.swallowed:
                return True
            config = self.config()
            occupied = assignments(config)
            for path, (_, default) in HOLD_DEFAULTS.items():
                if path == 'ava_mode_key':
                    from samsara.ai_preferences import ava_master_enabled
                    if not ava_master_enabled(config):
                        continue
                if path == 'command_mode.button' and not config_value(config, 'command_mode.enabled', False):
                    continue
                binding = config_value(config, path, default)
                if (validate(binding, occupied, path).allowed and name in swallow_binding(binding)
                        and binding_down(binding, self.down)):
                    self.swallowed.add(name)
                    return True
            return False
        self.down.discard(name)
        consumed = name in self.swallowed
        self.swallowed.discard(name)
        return consumed or captured


def hold_event_matches(app, key, binding, pressed, was_held=False):
    name = getattr(key, 'hold_name', None)
    if name is None:
        return None  # legacy callers retain their existing resolver
    wanted = parts(binding)
    member = name in wanted or base_modifier(name) in wanted
    if not pressed:
        return bool(was_held and member)
    return member and binding_down(binding, key.hold_keys)


def hold_listener(app, keyboard, press, release):
    """Use the existing pynput hook and queue, never run app work in the LL filter."""
    state = SuppressionState(lambda: app.config)

    class Listener(keyboard.Listener):
        _HOLD_MESSAGE = 0x4000
        _CAPTURE_MESSAGE = 0x8000

        def __init__(self):
            self.event_down = set()
            super().__init__(on_press=press, on_release=release,
                             win32_event_filter=self._hold_filter)

        def _hold_filter(self, msg, data):
            # Injected paste/automation must continue through the original path.
            if data.flags & 0x12:
                return True
            name = physical_key(data.vkCode, data.scanCode, data.flags)
            if name is None:
                return True
            pressed = msg in self._PRESS_MESSAGES
            packed = int(data.vkCode) | (int(data.scanCode) << 8) | ((int(data.flags) & 1) << 24)
            captured = route_capture(name, pressed)
            message = msg | self._HOLD_MESSAGE | (self._CAPTURE_MESSAGE if captured else 0)
            self._message_loop.post(self._WM_PROCESS, message, packed)
            if state.event(name, pressed, captured=captured):
                self.suppress_event()  # native hook returns 1, after queueing our event
            return False  # callback already queued; this alone never swallows

        def _process(self, msg, packed):
            if not msg & self._HOLD_MESSAGE:
                return super()._process(msg, packed)
            name = physical_key(packed & 255, (packed >> 8) & 65535, (packed >> 24) & 1)
            pressed = (msg & ~(self._HOLD_MESSAGE | self._CAPTURE_MESSAGE)) in self._PRESS_MESSAGES
            self.deliver(name, pressed, captured=bool(msg & self._CAPTURE_MESSAGE))

        def deliver(self, name, pressed, captured=False):
            if pressed:
                self.event_down.add(name)
            else:
                self.event_down.discard(name)
            if captured:
                keys = getattr(app, 'current_keys', None)
                if keys is not None:
                    keys.clear()
                    keys.update(base_modifier(key) for key in self.event_down)
                return
            key = SimpleNamespace(name=base_modifier(name), hold_name=name,
                                  hold_keys=frozenset(self.event_down))
            (self.on_press if pressed else self.on_release)(key)

    listener = Listener()
    app._hold_key_listener = listener
    return listener


def qt_key_name(event):
    """Physical Qt key identity, with a deterministic fallback for synthetic events."""
    from PySide6.QtCore import Qt
    vk, scan = event.nativeVirtualKey(), event.nativeScanCode()
    if vk:
        if vk == 13 and event.key() == Qt.Key.Key_Enter:
            return 'numpad_enter'
        extended = bool(scan & 0x100) or scan & 0xFF00 == 0xE000
        native = physical_key(vk, scan & 255, int(extended))
        if native:
            return native
    key = event.key()
    specials = {
        'Control': 'ctrl', 'Shift': 'shift', 'Alt': 'alt', 'AltGr': 'right_alt',
        'Meta': 'win', 'Escape': 'escape', 'Tab': 'tab', 'Backtab': 'tab',
        'Return': 'enter', 'Enter': 'numpad_enter', 'Space': 'space',
        'Backspace': 'backspace', 'CapsLock': 'capslock', 'NumLock': 'numlock',
        'ScrollLock': 'scroll_lock', 'Insert': 'insert', 'Delete': 'delete',
        'Home': 'home', 'End': 'end', 'PageUp': 'page_up', 'PageDown': 'page_down',
        'Pause': 'pause', 'Menu': 'menu', 'Left': 'left', 'Right': 'right',
        'Up': 'up', 'Down': 'down',
        **{f'F{n}': f'f{n}' for n in range(1, 25)},
    }
    name = next((value for attr, value in specials.items()
                 if key == getattr(Qt.Key, 'Key_' + attr)), None)
    if name is None and 33 <= key <= 126:
        name = chr(key).lower()
    if event.modifiers() & Qt.KeyboardModifier.KeypadModifier:
        numpad = {'insert': 'numpad0', 'end': 'numpad1', 'down': 'numpad2',
                  'page_down': 'numpad3', 'left': 'numpad4', 'clear': 'numpad5',
                  'right': 'numpad6', 'home': 'numpad7', 'up': 'numpad8',
                  'page_up': 'numpad9', 'delete': 'numpad_decimal',
                  '.': 'numpad_decimal', '+': 'numpad_add', '-': 'numpad_subtract',
                  '*': 'numpad_multiply', '/': 'numpad_divide',
                  **{str(n): f'numpad{n}' for n in range(10)}}
        if key == Qt.Key.Key_Clear:
            return 'numpad5'
        name = numpad.get(name, name)
    return name


class HoldKeyCapture:
    """Mixin used by the Settings and setup buttons, with validation on capture only."""
    def configure_hold(self, action, config, overrides=None):
        from samsara.ui import theme
        self._hold_action = action
        self._hold_config = config
        self._hold_overrides = overrides or (lambda: {})
        self._allow_mouse = True
        self._capture_reason = ''
        self.setMinimumHeight(theme.HIT_TARGET_MIN)
        self.setAccessibleName(HOLD_DEFAULTS[action][0] + ' key')
        self.setText(self._idle_text(self._combo))
        self.setToolTip('Click, then press a key or a modifier and key.')
        return self

    def activate_capture(self):
        global _capture_ref
        _capture_ref = weakref.ref(self)

    def capture_physical(self, name, pressed):
        if pressed:
            self._held.add(name)
            self.setText(combo_string(self._held))
        else:
            self._finish_capture()

    def reason_label(self):
        from PySide6.QtWidgets import QLabel
        from samsara.ui import theme
        label = QLabel()
        label.setWordWrap(True)
        label.setStyleSheet(f'color:{theme.WARNING};font-size:{theme.TYPE_BODY}px;'
                           'border:0;background:transparent;')
        label.setVisible(False)
        self._hold_reason_label = label
        return label

    def configure_shortcut(self, action, config, overrides=None):
        self._shortcut_action = action
        self._shortcut_config = config
        self._shortcut_overrides = overrides or (lambda: {})
        return self

    def capture_candidate(self, candidate):
        action = getattr(self, '_hold_action', None)
        if action:
            config = self._hold_config()
            decision = validate(candidate, assignments(config, self._hold_overrides()), action)
            if (decision.allowed and action == 'command_mode.button' and
                    candidate in ('mouse4', 'mouse5') and
                    not config_value(config, 'command_mode.suppress_button', True)):
                decision = Decision(True, warning=WARNING)
        else:
            # Tap shortcuts and alarm captures also must not take a hold key.
            # Resolve the existing Settings owner after controls are reparented.
            decision = Decision(True)
            shortcut = getattr(self, '_shortcut_action', None)
            if shortcut:
                occupied = assignments(self._shortcut_config(), self._shortcut_overrides())
                for other, (label, saved) in occupied.items():
                    if other != shortcut and overlapping_bindings(candidate, saved):
                        decision = Decision(False, f'Already used by {label}; choose another key.')
                        break
            owner = self.parentWidget()
            while owner is not None and not hasattr(owner, '_widgets'):
                owner = owner.parentWidget()
            if owner is not None and hasattr(owner, 'app'):
                widget_key = next((key for key, widget in owner._widgets.items() if widget is self), None)
                aliases = {'capture_correction_hotkey': 'hotkeys.capture_correction',
                           'alarms_complete_key': 'alarms.complete_hotkey',
                           'alarms_dismiss_key': 'alarms.dismiss_hotkey'}
                action = aliases.get(widget_key, widget_key)
                for other, (label, saved) in assignments(owner.app.config, capture_overrides(owner._widgets)).items():
                    if other != action and overlapping_bindings(candidate, saved):
                        decision = Decision(False, f'Already used by {label}; choose another key.')
                        break
                row = getattr(owner, '_rows_by_widget', {}).get(widget_key)
                if row is not None and not hasattr(self, '_hold_reason_label'):
                    label = self.reason_label()
                    row.grid.addWidget(label, row.grid.rowCount(), 0, 1, 3)
        if decision.allowed:
            self._combo = candidate
        self._capture_reason = decision.reason or decision.warning
        label = getattr(self, '_hold_reason_label', None)
        if label is not None:
            label.setText(self._capture_reason)
            label.setVisible(bool(self._capture_reason))
        self.setAccessibleDescription(self._capture_reason)
        self.setToolTip(self._capture_reason or 'Click, then press a key or a modifier and key.')
        if decision.allowed and self._on_change is not None:
            self._on_change()
        if decision.allowed:
            self.bindingChanged.emit(self._combo)
        return decision

    def _finish_capture(self):
        self._capturing = False
        if self._held:
            keys = set(self._held)
            if len(keys) > 1:
                keys = {base_modifier(key) for key in keys}
            self.capture_candidate(combo_string(keys))
        self.setText(self._idle_text(self._combo))
        self.setStyleSheet(self._idle_qss())

    def mousePressEvent(self, event):
        from PySide6.QtCore import Qt
        if self._capturing and self._allow_mouse:
            candidate = self._MOUSE_CAPTURE.get(event.button())
            if candidate:
                self._held.add(candidate)
                self._finish_capture()
                event.accept()
                return
            if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton):
                event.accept()
                return
        super().mousePressEvent(event)

    def event(self, event):
        from PySide6.QtCore import Qt, QEvent
        if getattr(self, '_capturing', False):
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.keyPressEvent(event)  # QWidget normally consumes Tab for focus traversal
                return True
        return super().event(event)

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return
        if not event.isAutoRepeat():
            name = qt_key_name(event)
            from PySide6.QtCore import Qt
            if name == 'right_alt' and event.key() == Qt.Key.Key_AltGr:
                self._held -= {'ctrl', 'left_ctrl'}  # Windows synthesizes Ctrl for AltGr
            self._held.add(name or 'unknown')
            self.setText(combo_string(self._held))
        event.accept()

    def keyReleaseEvent(self, event):
        if not self._capturing:
            super().keyReleaseEvent(event)
            return
        if not event.isAutoRepeat():
            self._finish_capture()
        event.accept()

    def focusOutEvent(self, event):
        if self._capturing:
            self._capturing = False
            self._held = set()  # focus loss cancels, never silently commits
            self.setText(self._idle_text(self._combo))
            self.setStyleSheet(self._idle_qss())
        super().focusOutEvent(event)


def capture_overrides(widgets):
    aliases = {'cmd_tab_button': 'command_mode.button',
               'capture_correction_hotkey': 'hotkeys.capture_correction',
               'alarms_complete_key': 'alarms.complete_hotkey',
               'alarms_dismiss_key': 'alarms.dismiss_hotkey'}
    return {aliases.get(key, key): widget.combo for key, widget in widgets.items()
            if hasattr(widget, 'combo')}


def capture_config(config, widgets):
    """Include a pending session mouse-suppression choice in capture warnings."""
    result = dict(config)
    checkbox = widgets.get('cmd_tab_suppress')
    if checkbox is not None:
        result['command_mode'] = dict(config.get('command_mode', {}) or {})
        result['command_mode']['suppress_button'] = checkbox.isChecked()
    return result
