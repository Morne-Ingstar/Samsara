"""Alarms settings tab.

Sections: Alarm Settings (hotkeys, nag interval), Alarm list with
add/edit/delete/toggle/test/reset-stats actions.
"""

import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk

from samsara.alarms import get_default_alarm_config


class AlarmsTab:
    """Alarms tab: global alarm settings, alarm list management."""

    def __init__(self, parent_frame, app, settings_window):
        self.parent = parent_frame
        self.app    = app
        self.sw     = settings_window
        self._built = False

        # tk.Vars — set during build()
        self.alarms_enabled_var  = None
        self.alarm_complete_var  = None
        self.alarm_complete_entry = None
        self.alarm_complete_btn  = None
        self.alarm_dismiss_var   = None
        self.alarm_dismiss_entry = None
        self.alarm_dismiss_btn   = None
        self.alarm_nag_var       = None
        self.alarm_tree          = None

    # ------------------------------------------------------------------
    # Build (generator)
    # ------------------------------------------------------------------

    def build(self):
        alarms_scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        alarms_scroll.pack(fill='both', expand=True)

        # ---- Global Settings -------------------------------------------
        ctk.CTkLabel(alarms_scroll, text="Alarm Settings",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        alarm_settings_frame = ctk.CTkFrame(alarms_scroll, corner_radius=10)
        alarm_settings_frame.pack(fill='x', pady=(0, 20))

        alarm_config = self.app.config.get('alarms', get_default_alarm_config())

        self.alarms_enabled_var = tk.BooleanVar(value=alarm_config.get('enabled', True))
        ctk.CTkCheckBox(alarm_settings_frame, text="Enable alarm reminders",
                        variable=self.alarms_enabled_var
                        ).pack(anchor='w', padx=15, pady=(15, 10))

        # Complete hotkey row
        complete_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        complete_row.pack(fill='x', padx=15, pady=(0, 8))
        ctk.CTkLabel(complete_row, text="Complete hotkey:", width=120, anchor='w').pack(side='left')
        self.alarm_complete_var = tk.StringVar(
            value=alarm_config.get('complete_hotkey', 'f7'))
        self.alarm_complete_entry = ctk.CTkEntry(
            complete_row, textvariable=self.alarm_complete_var, width=80, state='disabled')
        self.alarm_complete_entry.pack(side='left', padx=(0, 10))
        self.alarm_complete_btn = ctk.CTkButton(
            complete_row, text="Change", width=80,
            command=lambda: self.sw.start_capture('alarm_complete_hotkey'))
        self.alarm_complete_btn.pack(side='left')
        ctk.CTkLabel(complete_row, text="✓ Gets streak credit",
                     text_color="#00CED1").pack(side='left', padx=(10, 0))

        # Dismiss hotkey row
        dismiss_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        dismiss_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(dismiss_row, text="Dismiss hotkey:", width=120, anchor='w').pack(side='left')
        self.alarm_dismiss_var = tk.StringVar(
            value=alarm_config.get('dismiss_hotkey', 'f8'))
        self.alarm_dismiss_entry = ctk.CTkEntry(
            dismiss_row, textvariable=self.alarm_dismiss_var, width=80, state='disabled')
        self.alarm_dismiss_entry.pack(side='left', padx=(0, 10))
        self.alarm_dismiss_btn = ctk.CTkButton(
            dismiss_row, text="Change", width=80,
            command=lambda: self.sw.start_capture('alarm_dismiss_hotkey'))
        self.alarm_dismiss_btn.pack(side='left')
        ctk.CTkLabel(dismiss_row, text="✗ Breaks streak",
                     text_color="#ff6b6b").pack(side='left', padx=(10, 0))

        # Register hotkey vars/buttons on SettingsWindow so HotkeysTab.start_capture
        # can find them via getattr(self.sw, 'alarm_complete_var', None).
        self.sw.hotkey_buttons['alarm_complete_hotkey'] = self.alarm_complete_btn
        self.sw.hotkey_buttons['alarm_dismiss_hotkey']  = self.alarm_dismiss_btn
        self.sw.alarm_complete_var  = self.alarm_complete_var
        self.sw.alarm_complete_btn  = self.alarm_complete_btn
        self.sw.alarm_dismiss_var   = self.alarm_dismiss_var
        self.sw.alarm_dismiss_btn   = self.alarm_dismiss_btn

        # Nag interval row
        nag_row = ctk.CTkFrame(alarm_settings_frame, fg_color="transparent")
        nag_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkLabel(nag_row, text="Repeat interval:", width=120, anchor='w').pack(side='left')
        self.alarm_nag_var = tk.IntVar(value=alarm_config.get('nag_interval_seconds', 60))
        ctk.CTkEntry(nag_row, textvariable=self.alarm_nag_var, width=80
                     ).pack(side='left', padx=(0, 10))
        ctk.CTkLabel(nag_row, text="seconds (how often to replay until dismissed)"
                     ).pack(side='left')

        # ---- Alarm List ------------------------------------------------
        ctk.CTkLabel(alarms_scroll, text="Your Alarms",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        alarm_list_frame = ctk.CTkFrame(alarms_scroll, corner_radius=10)
        alarm_list_frame.pack(fill='both', expand=True, pady=(0, 10))

        alarm_tree_container = ctk.CTkFrame(alarm_list_frame, fg_color="transparent")
        alarm_tree_container.pack(fill='both', expand=True, padx=10, pady=10)

        alarm_tree_scroll = ttk.Scrollbar(alarm_tree_container)
        alarm_tree_scroll.pack(side='right', fill='y')

        style = ttk.Style()
        style.configure("Alarms.Treeview",
                        background="#2b2b2b", foreground="white",
                        fieldbackground="#2b2b2b", rowheight=36)
        style.configure("Alarms.Treeview.Heading",
                        background="#1f6aa5", foreground="white",
                        font=('Segoe UI', 10, 'bold'), relief='flat')
        style.map("Alarms.Treeview.Heading", background=[('active', '#2980b9')])
        style.map("Alarms.Treeview",         background=[('selected', '#1f6aa5')])

        self.alarm_tree = ttk.Treeview(
            alarm_tree_container,
            columns=('enabled', 'name', 'interval', 'streak'),
            show='headings',
            yscrollcommand=alarm_tree_scroll.set,
            style="Alarms.Treeview",
            height=8)
        self.alarm_tree.pack(side='left', fill='both', expand=True)
        alarm_tree_scroll.config(command=self.alarm_tree.yview)

        self.alarm_tree.heading('enabled', text='On')
        self.alarm_tree.heading('name',    text='Name')
        self.alarm_tree.heading('interval', text='Interval')
        self.alarm_tree.heading('streak',   text='Streak / Best')

        self.alarm_tree.column('enabled',  width=40, minwidth=40, anchor='center')
        self.alarm_tree.column('name',     width=150, minwidth=100)
        self.alarm_tree.column('interval', width=80, minwidth=60)
        self.alarm_tree.column('streak',   width=100, minwidth=80, anchor='center')

        self.populate_alarms_list()
        yield

        # ---- Button row ------------------------------------------------
        alarm_btn_frame = ctk.CTkFrame(alarms_scroll, fg_color="transparent")
        alarm_btn_frame.pack(fill='x', pady=(0, 10))

        ctk.CTkButton(alarm_btn_frame, text="Add Alarm", width=100,
                      command=self.add_alarm_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Edit", width=70,
                      command=self.edit_alarm_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Delete", width=70,
                      fg_color="#cc4444", hover_color="#aa3333",
                      command=self.delete_alarm).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Toggle", width=70, fg_color="gray40",
                      command=self.toggle_alarm_enabled).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Test", width=60, fg_color="gray40",
                      command=self.test_alarm_sound).pack(side='left', padx=(0, 5))
        ctk.CTkButton(alarm_btn_frame, text="Reset Stats", width=90, fg_color="gray40",
                      command=self.reset_alarm_stats).pack(side='left')

        ctk.CTkLabel(
            alarms_scroll,
            text=(f"Complete ({self.alarm_complete_var.get().upper()}) to get streak credit. "
                  f"Dismiss ({self.alarm_dismiss_var.get().upper()}) to silence without credit."),
            text_color="gray", wraplength=600,
        ).pack(anchor='w', pady=(5, 0))

        self._built = True

    # ------------------------------------------------------------------
    # Alarm list helpers
    # ------------------------------------------------------------------

    def populate_alarms_list(self) -> None:
        for item in self.alarm_tree.get_children():
            self.alarm_tree.delete(item)

        alarm_config = self.app.config.get('alarms', get_default_alarm_config())
        for alarm in alarm_config.get('items', []):
            alarm_id = alarm.get('id', alarm.get('name', 'unknown'))
            enabled  = "✓" if alarm.get('enabled', False) else ""
            name     = alarm.get('name', 'Unnamed')
            interval = f"{alarm.get('interval_minutes', 60)} min"

            if hasattr(self.app, 'alarm_manager'):
                stats   = self.app.alarm_manager.get_stats(alarm_id)
                current = stats.get('current_streak', 0)
                best    = stats.get('best_streak', 0)
                streak_text = f"🔥 {current} / {best}" if (current > 0 or best > 0) else "—"
            else:
                streak_text = "—"

            self.alarm_tree.insert('', 'end', iid=alarm_id,
                                   values=(enabled, name, interval, streak_text))

    def get_selected_alarm(self):
        selection = self.alarm_tree.selection()
        return selection[0] if selection else None

    # ------------------------------------------------------------------
    # Alarm CRUD
    # ------------------------------------------------------------------

    def add_alarm_dialog(self) -> None:
        self._show_alarm_dialog()

    def edit_alarm_dialog(self) -> None:
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to edit.",
                                   parent=self.sw.window)
            return
        self._show_alarm_dialog(edit_id=alarm_id)

    def _show_alarm_dialog(self, edit_id=None) -> None:
        dialog = ctk.CTkToplevel(self.sw.window)
        dialog.title("Edit Alarm" if edit_id else "Add Alarm")
        dialog.geometry("400x350")
        dialog.resizable(False, False)
        dialog.transient(self.sw.window)
        dialog.grab_set()

        existing_data = {}
        if edit_id and hasattr(self.app, 'alarm_manager'):
            existing_data = self.app.alarm_manager.get_alarm(edit_id) or {}

        ctk.CTkLabel(dialog, text="Alarm Name:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(20, 5))
        name_var = tk.StringVar(value=existing_data.get('name', ''))
        ctk.CTkEntry(dialog, textvariable=name_var, width=300).pack(anchor='w', padx=20)

        ctk.CTkLabel(dialog, text="Interval (minutes):",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        interval_var = tk.IntVar(value=existing_data.get('interval_minutes', 60))
        interval_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        interval_frame.pack(anchor='w', padx=20)
        ctk.CTkEntry(interval_frame, textvariable=interval_var, width=100).pack(side='left')
        ctk.CTkLabel(interval_frame, text="minutes",
                     text_color="gray").pack(side='left', padx=(10, 0))

        ctk.CTkLabel(dialog, text="Sound:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))

        sound_options  = ['Alarm', 'Chime', 'Bell', 'Gentle']
        current_sound  = existing_data.get('sound', 'alarm')
        if current_sound in ['alarm', 'chime', 'bell', 'gentle']:
            current_display = current_sound.title()
        else:
            current_display = (
                Path(current_sound).stem.replace('_', ' ').title()
                if current_sound else 'Alarm')
            if current_display not in sound_options:
                sound_options.append(current_display)

        sound_var   = tk.StringVar(value=current_display)
        sound_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        sound_frame.pack(anchor='w', padx=20, fill='x')

        sound_combo = ctk.CTkComboBox(sound_frame, variable=sound_var,
                                       values=sound_options, width=150)
        sound_combo.pack(side='left')

        def _browse_sound():
            from tkinter import filedialog
            filepath = filedialog.askopenfilename(
                parent=dialog,
                title="Select Sound File",
                filetypes=[("Audio Files", "*.wav *.mp3"),
                            ("WAV Files", "*.wav"), ("MP3 Files", "*.mp3")])
            if filepath:
                filename = Path(filepath).stem.replace('_', ' ').title()
                current_values = list(sound_combo.cget('values'))
                if filename not in current_values:
                    current_values.append(filename)
                    sound_combo.configure(values=current_values)
                sound_var.set(filename)
                dialog.custom_sound_path = filepath

        ctk.CTkButton(sound_frame, text="Browse...", width=80,
                      command=_browse_sound).pack(side='left', padx=(10, 0))

        def _preview_sound():
            sound_name = sound_var.get().lower().replace(' ', '_')
            sound_path = (getattr(dialog, 'custom_sound_path', None)
                          or (sound_name if sound_name in ['alarm', 'chime', 'bell', 'gentle']
                              else sound_name))
            if hasattr(self.app, 'alarm_manager'):
                threading.Thread(
                    target=lambda: self.app.alarm_manager.play_sound_file(sound_path),
                    daemon=True).start()

        ctk.CTkButton(sound_frame, text="Test", width=60, fg_color="gray40",
                      command=_preview_sound).pack(side='left', padx=(10, 0))

        enabled_var = tk.BooleanVar(value=existing_data.get('enabled', True))
        ctk.CTkCheckBox(dialog, text="Enabled",
                        variable=enabled_var).pack(anchor='w', padx=20, pady=(15, 0))

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill='x', padx=20, pady=20)

        def _save_alarm():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Error", "Please enter an alarm name.", parent=dialog)
                return
            interval = interval_var.get()
            if interval < 1:
                messagebox.showerror("Error", "Interval must be at least 1 minute.",
                                     parent=dialog)
                return

            sound_display = sound_var.get()
            sound = (getattr(dialog, 'custom_sound_path', None)
                     or sound_display.lower().replace(' ', '_'))

            if edit_id:
                self.app.alarm_manager.update_alarm(
                    edit_id, name=name, interval_minutes=interval,
                    sound=sound, enabled=enabled_var.get())
            else:
                self.app.alarm_manager.add_alarm(
                    name=name, interval_minutes=interval,
                    sound=sound, enabled=enabled_var.get())

            self.populate_alarms_list()
            dialog.destroy()
            messagebox.showinfo("Success", f"Alarm '{name}' saved!", parent=self.sw.window)

        ctk.CTkButton(btn_frame, text="Save",   width=100, command=_save_alarm).pack(side='right', padx=(10, 0))
        ctk.CTkButton(btn_frame, text="Cancel", width=100, fg_color="gray40",
                      command=dialog.destroy).pack(side='right')

    def delete_alarm(self) -> None:
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to delete.",
                                   parent=self.sw.window)
            return
        alarm      = self.app.alarm_manager.get_alarm(alarm_id) if hasattr(self.app, 'alarm_manager') else None
        alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id
        if messagebox.askyesno("Confirm Delete",
                               f"Are you sure you want to delete the alarm '{alarm_name}'?",
                               parent=self.sw.window):
            if hasattr(self.app, 'alarm_manager'):
                self.app.alarm_manager.remove_alarm(alarm_id)
                self.populate_alarms_list()
                messagebox.showinfo("Deleted", f"Alarm '{alarm_name}' deleted.",
                                    parent=self.sw.window)

    def toggle_alarm_enabled(self) -> None:
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to toggle.",
                                   parent=self.sw.window)
            return
        if hasattr(self.app, 'alarm_manager'):
            new_state = self.app.alarm_manager.toggle_alarm(alarm_id)
            if new_state is not None:
                self.populate_alarms_list()
                print(f"[ALARM] Toggled {alarm_id}: {'enabled' if new_state else 'disabled'}")

    def test_alarm_sound(self) -> None:
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to test.",
                                   parent=self.sw.window)
            return
        if hasattr(self.app, 'alarm_manager'):
            alarm = self.app.alarm_manager.get_alarm(alarm_id)
            if alarm:
                threading.Thread(
                    target=lambda: self.app.alarm_manager.play_sound(alarm),
                    daemon=True).start()

    def reset_alarm_stats(self) -> None:
        alarm_id = self.get_selected_alarm()
        if not alarm_id:
            messagebox.showwarning("No Selection", "Please select an alarm to reset stats.",
                                   parent=self.sw.window)
            return
        alarm      = self.app.alarm_manager.get_alarm(alarm_id) if hasattr(self.app, 'alarm_manager') else None
        alarm_name = alarm.get('name', alarm_id) if alarm else alarm_id
        if messagebox.askyesno(
                "Reset Stats",
                f"Reset all stats for '{alarm_name}'?\n\nThis will clear:\n"
                "• Current streak\n• Best streak\n• Total completions",
                parent=self.sw.window):
            if hasattr(self.app, 'alarm_manager'):
                self.app.alarm_manager.reset_stats(alarm_id)
                self.populate_alarms_list()
                messagebox.showinfo("Stats Reset",
                                    f"Stats for '{alarm_name}' have been reset.",
                                    parent=self.sw.window)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self) -> None:
        if not self._built:
            return

        if 'alarms' not in self.app.config:
            self.app.update_config({'alarms': get_default_alarm_config()}, save=False)

        alarms = self.app.config['alarms']
        alarms['enabled']              = self.alarms_enabled_var.get()
        alarms['complete_hotkey']      = self.alarm_complete_var.get()
        alarms['dismiss_hotkey']       = self.alarm_dismiss_var.get()
        alarms['nag_interval_seconds'] = self.alarm_nag_var.get()

        if hasattr(self.app, 'alarm_manager'):
            if self.alarms_enabled_var.get() and not self.app.alarm_manager.running:
                self.app.alarm_manager.start()
            elif not self.alarms_enabled_var.get() and self.app.alarm_manager.running:
                self.app.alarm_manager.stop()
