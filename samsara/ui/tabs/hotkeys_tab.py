"""Hotkeys & Modes settings tab.

Sections: Recording Mode, Keyboard Shortcuts.
Hotkey capture logic lives here and handles all hotkeys including
alarm hotkeys registered by build_alarms_tab via SettingsWindow.
"""

import tkinter as tk

import customtkinter as ctk


class HotkeysTab:
    """Hotkeys & Modes tab: recording mode, keyboard shortcuts, hotkey capture."""

    def __init__(self, parent_frame, app, settings_window):
        self.parent = parent_frame
        self.app    = app
        self.sw     = settings_window
        self._built = False

        # Capture state
        self._capturing_hotkey = None
        self._captured_keys    = set()
        self._hotkey_pairs     = {}

        # tk.Vars and button refs — set during build()
        self.mode_var              = None
        self.wake_word_enabled_var = None
        self.hotkey_var            = None
        self.hotkey_btn            = None
        self.hotkey_entry          = None
        self.cont_hotkey_var       = None
        self.cont_hotkey_btn       = None
        self.cont_hotkey_entry     = None
        self.wake_hotkey_var       = None
        self.wake_hotkey_btn       = None
        self.wake_hotkey_entry     = None
        self.cmd_hotkey_var        = None
        self.cmd_hotkey_btn        = None
        self.cmd_hotkey_entry      = None
        self.cancel_hotkey_var     = None
        self.cancel_hotkey_btn     = None
        self.cancel_hotkey_entry   = None

    # ------------------------------------------------------------------
    # Build (generator)
    # ------------------------------------------------------------------

    def build(self):
        hotkey_scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        hotkey_scroll.pack(fill='both', expand=True)

        # --- Recording Mode ---
        ctk.CTkLabel(hotkey_scroll, text="Recording Mode",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        mode_frame = ctk.CTkFrame(hotkey_scroll, corner_radius=10)
        mode_frame.pack(fill='x', pady=(0, 20))

        self.mode_var = tk.StringVar(value=self.app.config.get('mode', 'hold'))

        ctk.CTkRadioButton(mode_frame,
                           text="Hold to record (hold key, release to transcribe)",
                           variable=self.mode_var, value='hold'
                           ).pack(anchor='w', padx=15, pady=(15, 8))
        ctk.CTkRadioButton(mode_frame,
                           text="Toggle mode (press to start/stop recording)",
                           variable=self.mode_var, value='toggle'
                           ).pack(anchor='w', padx=15, pady=(0, 8))
        ctk.CTkRadioButton(mode_frame,
                           text="Continuous (auto-transcribe on speech pause)",
                           variable=self.mode_var, value='continuous'
                           ).pack(anchor='w', padx=15, pady=(0, 15))

        self.wake_word_enabled_var = tk.BooleanVar(
            value=self.app.config.get('wake_word_enabled', False))
        ctk.CTkCheckBox(mode_frame,
                        text="Enable wake word listener (works with any mode above)",
                        variable=self.wake_word_enabled_var
                        ).pack(anchor='w', padx=15, pady=(0, 15))
        yield

        # --- Keyboard Shortcuts ---
        ctk.CTkLabel(hotkey_scroll, text="Keyboard Shortcuts",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        hotkey_frame = ctk.CTkFrame(hotkey_scroll, corner_radius=10)
        hotkey_frame.pack(fill='x')

        # Record hotkey
        row1 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row1.pack(fill='x', padx=15, pady=(15, 8))
        ctk.CTkLabel(row1, text="Record hotkey:", width=150, anchor='w').pack(side='left')
        self.hotkey_var = tk.StringVar(value=self.app.config.get('hotkey', 'ctrl+shift'))
        self.hotkey_entry = ctk.CTkEntry(row1, textvariable=self.hotkey_var,
                                         width=180, state='disabled')
        self.hotkey_entry.pack(side='left', padx=(0, 10))
        self.hotkey_btn = ctk.CTkButton(row1, text="Change", width=80,
                                        command=lambda: self.start_capture('hotkey'))
        self.hotkey_btn.pack(side='left')
        self.sw.hotkey_buttons['hotkey'] = self.hotkey_btn

        # Continuous hotkey
        row2 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row2.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row2, text="Toggle continuous:", width=150, anchor='w').pack(side='left')
        self.cont_hotkey_var = tk.StringVar(
            value=self.app.config.get('continuous_hotkey', 'ctrl+alt+d'))
        self.cont_hotkey_entry = ctk.CTkEntry(row2, textvariable=self.cont_hotkey_var,
                                               width=180, state='disabled')
        self.cont_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cont_hotkey_btn = ctk.CTkButton(row2, text="Change", width=80,
                                             command=lambda: self.start_capture('continuous_hotkey'))
        self.cont_hotkey_btn.pack(side='left')
        self.sw.hotkey_buttons['continuous_hotkey'] = self.cont_hotkey_btn

        # Wake word hotkey
        row3 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row3.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row3, text="Toggle wake word:", width=150, anchor='w').pack(side='left')
        self.wake_hotkey_var = tk.StringVar(
            value=self.app.config.get('wake_word_hotkey', 'ctrl+alt+w'))
        self.wake_hotkey_entry = ctk.CTkEntry(row3, textvariable=self.wake_hotkey_var,
                                               width=180, state='disabled')
        self.wake_hotkey_entry.pack(side='left', padx=(0, 10))
        self.wake_hotkey_btn = ctk.CTkButton(row3, text="Change", width=80,
                                             command=lambda: self.start_capture('wake_word_hotkey'))
        self.wake_hotkey_btn.pack(side='left')
        self.sw.hotkey_buttons['wake_word_hotkey'] = self.wake_hotkey_btn

        # Command-only hotkey
        row3b = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row3b.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(row3b, text="Command only:", width=150, anchor='w').pack(side='left')
        self.cmd_hotkey_var = tk.StringVar(
            value=self.app.config.get('command_hotkey', 'ctrl+alt+c'))
        self.cmd_hotkey_entry = ctk.CTkEntry(row3b, textvariable=self.cmd_hotkey_var,
                                              width=180, state='disabled')
        self.cmd_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cmd_hotkey_btn = ctk.CTkButton(row3b, text="Change", width=80,
                                             command=lambda: self.start_capture('command_hotkey'))
        self.cmd_hotkey_btn.pack(side='left')
        self.sw.hotkey_buttons['command_hotkey'] = self.cmd_hotkey_btn

        # Cancel recording hotkey
        row4 = ctk.CTkFrame(hotkey_frame, fg_color="transparent")
        row4.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(row4, text="Cancel recording:", width=150, anchor='w').pack(side='left')
        self.cancel_hotkey_var = tk.StringVar(
            value=self.app.config.get('cancel_hotkey', 'escape'))
        self.cancel_hotkey_entry = ctk.CTkEntry(row4, textvariable=self.cancel_hotkey_var,
                                                 width=180, state='disabled')
        self.cancel_hotkey_entry.pack(side='left', padx=(0, 10))
        self.cancel_hotkey_btn = ctk.CTkButton(row4, text="Change", width=80,
                                               command=lambda: self.start_capture('cancel_hotkey'))
        self.cancel_hotkey_btn.pack(side='left')
        self.sw.hotkey_buttons['cancel_hotkey'] = self.cancel_hotkey_btn

        self._built = True

    # ------------------------------------------------------------------
    # Hotkey capture
    # ------------------------------------------------------------------

    def _build_pairs(self):
        """Return a dict of hotkey_name -> (var, btn) for all registered hotkeys.

        Alarm hotkeys are built later by build_alarms_tab and live on
        SettingsWindow; they are looked up via getattr so that capture
        still works even if the Alarms tab hasn't been visited yet.
        """
        pairs = {}
        if self._built:
            pairs.update({
                'hotkey':            (self.hotkey_var,       self.hotkey_btn),
                'continuous_hotkey': (self.cont_hotkey_var,  self.cont_hotkey_btn),
                'wake_word_hotkey':  (self.wake_hotkey_var,  self.wake_hotkey_btn),
                'command_hotkey':    (self.cmd_hotkey_var,   self.cmd_hotkey_btn),
                'cancel_hotkey':     (self.cancel_hotkey_var, self.cancel_hotkey_btn),
            })
        for alarm_key, var_attr, btn_attr in [
            ('alarm_complete_hotkey', 'alarm_complete_var', 'alarm_complete_btn'),
            ('alarm_dismiss_hotkey',  'alarm_dismiss_var',  'alarm_dismiss_btn'),
        ]:
            v = getattr(self.sw, var_attr, None)
            b = getattr(self.sw, btn_attr, None)
            if v is not None and b is not None:
                pairs[alarm_key] = (v, b)
        return pairs

    def start_capture(self, hotkey_name):
        self._capturing_hotkey = hotkey_name
        self._captured_keys    = set()
        self._hotkey_pairs     = self._build_pairs()

        pair = self._hotkey_pairs.get(hotkey_name)
        if pair:
            pair[0].set("Press keys...")
            pair[1].configure(text="...")

        self.sw.window.bind('<KeyPress>',   self.on_capture_key)
        self.sw.window.bind('<KeyRelease>', self.on_capture_release)

    def on_capture_key(self, event):
        if self._capturing_hotkey is None:
            return

        key = event.keysym.lower()
        if key in ('control_l', 'control_r'):
            key = 'ctrl'
        elif key in ('shift_l', 'shift_r'):
            key = 'shift'
        elif key in ('alt_l', 'alt_r'):
            key = 'alt'
        elif key in ('super_l', 'super_r', 'win_l', 'win_r'):
            key = 'win'

        self._captured_keys.add(key)
        hotkey_str = '+'.join(sorted(self._captured_keys))

        pair = self._hotkey_pairs.get(self._capturing_hotkey)
        if pair:
            pair[0].set(hotkey_str)

    def on_capture_release(self, event):
        if self._capturing_hotkey is None:
            return

        hotkey_str = '+'.join(sorted(self._captured_keys))
        if hotkey_str:
            pair = self._hotkey_pairs.get(self._capturing_hotkey)
            if pair:
                pair[0].set(hotkey_str)
                pair[1].configure(text="Set")

        self.sw.window.unbind('<KeyPress>')
        self.sw.window.unbind('<KeyRelease>')
        self._capturing_hotkey = None
        self._captured_keys    = set()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        if not self._built:
            return
        self.app.update_config({
            'mode':              self.mode_var.get(),
            'wake_word_enabled': self.wake_word_enabled_var.get(),
            'hotkey':            self.hotkey_var.get(),
            'continuous_hotkey': self.cont_hotkey_var.get(),
            'wake_word_hotkey':  self.wake_hotkey_var.get(),
            'command_hotkey':    self.cmd_hotkey_var.get(),
            'cancel_hotkey':     self.cancel_hotkey_var.get(),
        }, save=False)
