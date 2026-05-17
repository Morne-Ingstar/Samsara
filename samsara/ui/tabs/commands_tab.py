"""Commands settings tab.

Sections: Command Mode Input, Command Packs, Voice Commands list
with add/edit/delete/test/reload actions.
"""

import json
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import customtkinter as ctk


class CommandsTab:
    """Commands tab: command mode button, pack toggles, command browser."""

    # Maps human-readable dropdown labels to config values for command_mode.button
    _CMD_BUTTON_OPTIONS = {
        'Mouse 4 (default)': 'mouse4',
        'Mouse 5':           'mouse5',
        'Right Ctrl':        'rctrl',
        'Left Ctrl':         'lctrl',
        'Right Alt':         'ralt',
        'Left Alt':          'lalt',
        'Right Shift':       'rshift',
        'Left Shift':        'lshift',
        **{f'F{n}': f'f{n}' for n in range(13, 25)},
    }
    _CMD_BUTTON_KEY_TO_LABEL: dict = {}

    @classmethod
    def _cmd_button_key_to_label(cls) -> dict:
        if not cls._CMD_BUTTON_KEY_TO_LABEL:
            cls._CMD_BUTTON_KEY_TO_LABEL = {v: k for k, v in cls._CMD_BUTTON_OPTIONS.items()}
        return cls._CMD_BUTTON_KEY_TO_LABEL

    def __init__(self, parent_frame, app, settings_window):
        self.parent = parent_frame
        self.app    = app
        self.sw     = settings_window
        self._built = False

        # tk.Vars — set during build()
        self.cmd_mode_button_var  = None
        self.cmd_mode_suppress_var = None
        self._pack_vars            = {}
        self._packs_restart_label  = None
        self.cmd_search_var        = None
        self.cmd_tree              = None

    # ------------------------------------------------------------------
    # Build (generator)
    # ------------------------------------------------------------------

    def build(self):
        from samsara.command_packs import PACKS

        # ---- Command Mode Input -------------------------------------------
        ctk.CTkLabel(self.parent, text="Command Mode Input",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 4))
        ctk.CTkLabel(
            self.parent,
            text="Choose which button activates Mouse 4 command mode (walkie-talkie hold-to-talk).",
            text_color="gray", wraplength=580,
        ).pack(anchor='w', pady=(0, 8))

        btn_row = ctk.CTkFrame(self.parent, fg_color="transparent")
        btn_row.pack(fill='x', pady=(0, 12))
        ctk.CTkLabel(btn_row, text="Command Mode Button:", width=190, anchor='w').pack(side='left')

        current_btn_key = self.app.config.get('command_mode', {}).get('button', 'mouse4')
        current_btn_label = self._cmd_button_key_to_label().get(current_btn_key, 'Mouse 4 (default)')
        self.cmd_mode_button_var = tk.StringVar(value=current_btn_label)
        ctk.CTkComboBox(
            btn_row,
            variable=self.cmd_mode_button_var,
            values=list(self._CMD_BUTTON_OPTIONS.keys()),
            state='readonly',
            width=200,
        ).pack(side='left')

        suppress_val = self.app.config.get('command_mode', {}).get('suppress_button', True)
        self.cmd_mode_suppress_var = tk.BooleanVar(value=suppress_val)
        ctk.CTkCheckBox(
            self.parent,
            text="Suppress browser-back when using Mouse 4/5 for commands",
            variable=self.cmd_mode_suppress_var,
        ).pack(anchor='w', padx=2, pady=(4, 0))
        ctk.CTkLabel(
            self.parent,
            text="    When enabled, Mouse 4/5 only triggers command mode and never navigates back in browsers.",
            text_color="gray", font=ctk.CTkFont(size=11), anchor='w',
        ).pack(anchor='w', pady=(0, 8))

        ctk.CTkFrame(self.parent, height=1, fg_color="gray30").pack(fill='x', pady=(4, 12))

        # ---- Command Packs -----------------------------------------------
        ctk.CTkLabel(self.parent, text="Command Packs",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 4))
        ctk.CTkLabel(
            self.parent,
            text="Enable the packs you use. Disabling unused packs improves recognition accuracy.",
            text_color="gray", wraplength=580,
        ).pack(anchor='w', pady=(0, 8))

        packs_scroll = ctk.CTkScrollableFrame(self.parent, height=220, corner_radius=10)
        packs_scroll.pack(fill='x', pady=(0, 4))

        current_packs = self.app.config.get('command_packs', {})
        self._pack_vars = {}

        # Count commands per pack (builtin + plugin)
        pack_counts = {}
        try:
            import json as _json
            d = _json.load(open('commands.json', encoding='utf-8'))
            for cmd in d.get('commands', d).values():
                p = cmd.get('pack', 'core')
                pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception:
            pass
        try:
            from samsara import plugin_commands as _pc
            seen = set()
            for entry_data in _pc._REGISTRY.values():
                eid = id(entry_data)
                if eid in seen:
                    continue
                seen.add(eid)
                p = entry_data.get('pack', 'core')
                pack_counts[p] = pack_counts.get(p, 0) + 1
        except Exception:
            pass

        for pack_id, meta in PACKS.items():
            is_always_on = meta['always_on']
            default      = meta['default_enabled']
            enabled      = current_packs.get(pack_id, default)

            row = ctk.CTkFrame(packs_scroll, fg_color="transparent")
            row.pack(fill='x', pady=2)

            var = tk.BooleanVar(value=enabled or is_always_on)
            self._pack_vars[pack_id] = var

            ctk.CTkCheckBox(
                row, text="", variable=var, width=24,
                state='disabled' if is_always_on else 'normal',
            ).pack(side='left')

            count      = pack_counts.get(pack_id, 0)
            count_str  = f"  ({count} commands)" if count else ""
            label_text = (f"{meta['label']}"
                          f"{'  •  always on' if is_always_on else ''}{count_str}")
            ctk.CTkLabel(
                row, text=label_text,
                font=ctk.CTkFont(size=12, weight="bold" if is_always_on else "normal"),
                text_color="gray60" if is_always_on else "white",
                anchor='w').pack(side='left', padx=(4, 8))
            ctk.CTkLabel(
                row, text=meta['description'],
                text_color="gray", font=ctk.CTkFont(size=11),
                anchor='w').pack(side='left')

        # Restart notice — shown/hidden based on whether any pack was toggled
        self._packs_restart_label = ctk.CTkLabel(
            self.parent,
            text="Restart Samsara to apply pack changes.",
            text_color="#e2c355",
            font=ctk.CTkFont(size=11),
        )

        def _on_pack_toggle(*_):
            try:
                self._packs_restart_label.pack(anchor='w', pady=(0, 4))
            except Exception:
                pass

        for var in self._pack_vars.values():
            var.trace_add('write', _on_pack_toggle)

        ctk.CTkFrame(self.parent, height=1, fg_color="gray30").pack(fill='x', pady=(6, 10))

        # ---- Command list ------------------------------------------------
        cmd_header = ctk.CTkFrame(self.parent, fg_color="transparent")
        cmd_header.pack(fill='x', pady=(15, 10))
        ctk.CTkLabel(cmd_header, text="Voice Commands",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(side='left')

        self.cmd_search_var = tk.StringVar()
        self.cmd_search_var.trace('w', lambda *args: self.filter_commands())
        ctk.CTkEntry(cmd_header, textvariable=self.cmd_search_var,
                     placeholder_text="Search commands...", width=200
                     ).pack(side='right')

        list_frame = ctk.CTkFrame(self.parent, corner_radius=10)
        list_frame.pack(fill='both', expand=True, pady=(0, 10))

        tree_container = ctk.CTkFrame(list_frame, fg_color="transparent")
        tree_container.pack(fill='both', expand=True, padx=10, pady=10)

        tree_scroll = ttk.Scrollbar(tree_container)
        tree_scroll.pack(side='right', fill='y')

        style = ttk.Style()
        style.theme_use('clam')
        style.configure("Commands.Treeview",
                        background="#2b2b2b", foreground="white",
                        fieldbackground="#2b2b2b", rowheight=28)
        style.configure("Commands.Treeview.Heading",
                        background="#1f6aa5", foreground="white",
                        font=('Segoe UI', 10, 'bold'), relief='flat')
        style.map("Commands.Treeview.Heading", background=[('active', '#2980b9')])
        style.map("Commands.Treeview",         background=[('selected', '#1f6aa5')])

        self.cmd_tree = ttk.Treeview(
            tree_container,
            columns=('phrase', 'type', 'action', 'description'),
            show='headings',
            yscrollcommand=tree_scroll.set,
            style="Commands.Treeview",
            height=12)
        self.cmd_tree.pack(side='left', fill='both', expand=True)
        tree_scroll.config(command=self.cmd_tree.yview)

        self.cmd_tree.heading('phrase',      text='Voice Phrase')
        self.cmd_tree.heading('type',        text='Type')
        self.cmd_tree.heading('action',      text='Action')
        self.cmd_tree.heading('description', text='Description')

        self.cmd_tree.column('phrase',      width=140, minwidth=100)
        self.cmd_tree.column('type',        width=70,  minwidth=60)
        self.cmd_tree.column('action',      width=150, minwidth=100)
        self.cmd_tree.column('description', width=180, minwidth=100)

        self.populate_commands_list()
        yield

        # ---- Button row --------------------------------------------------
        cmd_btn_frame = ctk.CTkFrame(self.parent, fg_color="transparent")
        cmd_btn_frame.pack(fill='x', pady=(0, 5))

        ctk.CTkButton(cmd_btn_frame, text="Add Command", width=120,
                      command=self.add_command_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Edit", width=80,
                      command=self.edit_command_dialog).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Delete", width=80,
                      fg_color="#cc4444", hover_color="#aa3333",
                      command=self.delete_command).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Test", width=80, fg_color="gray40",
                      command=self.test_command).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_frame, text="Reload", width=80, fg_color="gray40",
                      command=self.reload_commands).pack(side='right')

        ctk.CTkLabel(self.parent,
                     text="Say these phrases while dictating to trigger actions. Commands work in all modes.",
                     text_color="gray").pack(anchor='w')

        self._built = True

    # ------------------------------------------------------------------
    # Command list helpers
    # ------------------------------------------------------------------

    def get_command_action_text(self, cmd_data: dict) -> str:
        cmd_type = cmd_data.get('type', '')
        if cmd_type == 'hotkey':
            return '+'.join(k.capitalize() for k in cmd_data.get('keys', []))
        if cmd_type == 'launch':
            target = cmd_data.get('target', '')
            return ('...' + target[-27:]) if len(target) > 30 else target
        if cmd_type == 'press':
            return f"Press {cmd_data.get('key', '').upper()}"
        if cmd_type == 'key_down':
            return f"Hold {cmd_data.get('key', '').upper()}"
        if cmd_type == 'key_up':
            return f"Release {cmd_data.get('key', '').upper()}"
        if cmd_type == 'mouse':
            return f"{cmd_data.get('action','click').replace('_',' ').title()} ({cmd_data.get('button','left')})"
        if cmd_type == 'release_all':
            return "Release all keys"
        return str(cmd_data)

    def populate_commands_list(self, filter_text: str = '') -> None:
        for item in self.cmd_tree.get_children():
            self.cmd_tree.delete(item)

        commands = self.app.command_executor.commands
        for phrase, cmd_data in sorted(commands.items()):
            if filter_text:
                sl = filter_text.lower()
                if (sl not in phrase.lower()
                        and sl not in cmd_data.get('type', '').lower()
                        and sl not in cmd_data.get('description', '').lower()):
                    continue
            self.cmd_tree.insert('', 'end', values=(
                phrase,
                cmd_data.get('type', 'unknown'),
                self.get_command_action_text(cmd_data),
                cmd_data.get('description', ''),
            ))

    def filter_commands(self) -> None:
        self.populate_commands_list(self.cmd_search_var.get())

    def get_selected_command(self):
        selection = self.cmd_tree.selection()
        if not selection:
            return None
        item = self.cmd_tree.item(selection[0])
        return item['values'][0] if item['values'] else None

    # ------------------------------------------------------------------
    # Command CRUD
    # ------------------------------------------------------------------

    def add_command_dialog(self) -> None:
        self.open_command_editor(None)

    def edit_command_dialog(self) -> None:
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to edit.",
                                   parent=self.sw.window)
            return
        self.open_command_editor(phrase)

    def open_command_editor(self, edit_phrase=None) -> None:
        dialog = ctk.CTkToplevel(self.sw.window)
        dialog.title("Edit Command" if edit_phrase else "Add Command")
        dialog.geometry("500x400")
        dialog.resizable(False, False)
        dialog.transient(self.sw.window)
        dialog.grab_set()

        dialog.update_idletasks()
        x = self.sw.window.winfo_x() + (self.sw.window.winfo_width()  - 500) // 2
        y = self.sw.window.winfo_y() + (self.sw.window.winfo_height() - 400) // 2
        dialog.geometry(f"+{x}+{y}")

        existing_data = {}
        if edit_phrase:
            existing_data = self.app.command_executor.commands.get(edit_phrase, {})

        ctk.CTkLabel(dialog, text="Voice Phrase:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(20, 5))
        phrase_var = tk.StringVar(value=edit_phrase or '')
        ctk.CTkEntry(dialog, textvariable=phrase_var, width=300).pack(anchor='w', padx=20)
        ctk.CTkLabel(dialog, text="What you say to trigger this command",
                     text_color="gray").pack(anchor='w', padx=20)

        ctk.CTkLabel(dialog, text="Command Type:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        type_var = tk.StringVar(value=existing_data.get('type', 'hotkey'))
        type_combo = ctk.CTkComboBox(
            dialog, variable=type_var, width=200, state='readonly',
            values=['hotkey', 'text', 'launch', 'press', 'key_down', 'key_up', 'mouse', 'release_all'])
        type_combo.pack(anchor='w', padx=20)

        fields_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        fields_frame.pack(fill='x', padx=20, pady=(15, 0))

        keys_var         = tk.StringVar(value='+'.join(existing_data.get('keys', [])))
        target_var       = tk.StringVar(value=existing_data.get('target', ''))
        key_var          = tk.StringVar(value=existing_data.get('key', ''))
        text_var         = tk.StringVar(value=existing_data.get('text', ''))
        mouse_action_var = tk.StringVar(value=existing_data.get('action', 'click'))
        mouse_button_var = tk.StringVar(value=existing_data.get('button', 'left'))
        field_widgets    = []

        def _update_fields(*args):
            for w in field_widgets:
                w.destroy()
            field_widgets.clear()
            ct = type_var.get()

            if ct == 'hotkey':
                lbl = ctk.CTkLabel(fields_frame, text="Keys (e.g., ctrl+shift+a):")
                lbl.pack(anchor='w'); field_widgets.append(lbl)
                e = ctk.CTkEntry(fields_frame, textvariable=keys_var, width=300)
                e.pack(anchor='w'); field_widgets.append(e)
                h = ctk.CTkLabel(fields_frame,
                                 text="Use + to combine keys: ctrl, shift, alt, win, a-z, 0-9, f1-f12, etc.",
                                 text_color="gray")
                h.pack(anchor='w'); field_widgets.append(h)

            elif ct == 'text':
                lbl = ctk.CTkLabel(fields_frame, text="Text to insert:")
                lbl.pack(anchor='w'); field_widgets.append(lbl)
                e = ctk.CTkEntry(fields_frame, textvariable=text_var, width=300)
                e.pack(anchor='w'); field_widgets.append(e)
                h = ctk.CTkLabel(fields_frame, text="Punctuation, symbols, or any text to paste",
                                 text_color="gray")
                h.pack(anchor='w'); field_widgets.append(h)

            elif ct == 'launch':
                lbl = ctk.CTkLabel(fields_frame, text="Program/Command to run:")
                lbl.pack(anchor='w'); field_widgets.append(lbl)
                e = ctk.CTkEntry(fields_frame, textvariable=target_var, width=400)
                e.pack(anchor='w'); field_widgets.append(e)
                h = ctk.CTkLabel(fields_frame, text="e.g., chrome.exe, notepad.exe, or full path",
                                 text_color="gray")
                h.pack(anchor='w'); field_widgets.append(h)

            elif ct in ('press', 'key_down', 'key_up'):
                lbl = ctk.CTkLabel(fields_frame, text="Key:")
                lbl.pack(anchor='w'); field_widgets.append(lbl)
                e = ctk.CTkEntry(fields_frame, textvariable=key_var, width=150)
                e.pack(anchor='w'); field_widgets.append(e)
                h = ctk.CTkLabel(fields_frame, text="Single key: a, space, enter, shift, w, etc.",
                                 text_color="gray")
                h.pack(anchor='w'); field_widgets.append(h)

            elif ct == 'mouse':
                l1 = ctk.CTkLabel(fields_frame, text="Mouse Action:")
                l1.pack(anchor='w'); field_widgets.append(l1)
                ac = ctk.CTkComboBox(fields_frame, variable=mouse_action_var, width=150,
                                     state='readonly', values=['click', 'double_click'])
                ac.pack(anchor='w'); field_widgets.append(ac)
                l2 = ctk.CTkLabel(fields_frame, text="Button:")
                l2.pack(anchor='w', pady=(10, 0)); field_widgets.append(l2)
                bc = ctk.CTkComboBox(fields_frame, variable=mouse_button_var, width=150,
                                     state='readonly', values=['left', 'right', 'middle'])
                bc.pack(anchor='w'); field_widgets.append(bc)

            elif ct == 'release_all':
                lbl = ctk.CTkLabel(fields_frame,
                                   text="No additional settings needed.\nThis releases all held keys.",
                                   text_color="gray")
                lbl.pack(anchor='w'); field_widgets.append(lbl)

        type_var.trace('w', _update_fields)
        _update_fields()

        ctk.CTkLabel(dialog, text="Description:",
                     font=ctk.CTkFont(weight="bold")).pack(anchor='w', padx=20, pady=(15, 5))
        desc_var = tk.StringVar(value=existing_data.get('description', ''))
        ctk.CTkEntry(dialog, textvariable=desc_var, width=400).pack(anchor='w', padx=20)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill='x', padx=20, pady=20)

        def _save():
            phrase = phrase_var.get().strip().lower()
            if not phrase:
                messagebox.showerror("Error", "Voice phrase is required.", parent=dialog)
                return
            if not edit_phrase or phrase != edit_phrase.lower():
                if phrase in self.app.command_executor.commands:
                    messagebox.showerror("Error",
                                         f"A command with phrase '{phrase}' already exists.",
                                         parent=dialog)
                    return

            ct = type_var.get()
            cmd_data = {'type': ct, 'description': desc_var.get().strip()}

            if ct == 'hotkey':
                keys = [k.strip().lower() for k in keys_var.get().split('+') if k.strip()]
                if not keys:
                    messagebox.showerror("Error", "Please specify at least one key.", parent=dialog)
                    return
                cmd_data['keys'] = keys
            elif ct == 'launch':
                target = target_var.get().strip()
                if not target:
                    messagebox.showerror("Error", "Please specify a program to launch.", parent=dialog)
                    return
                cmd_data['target'] = target
            elif ct in ('press', 'key_down', 'key_up'):
                key = key_var.get().strip().lower()
                if not key:
                    messagebox.showerror("Error", "Please specify a key.", parent=dialog)
                    return
                cmd_data['key'] = key
            elif ct == 'mouse':
                cmd_data['action'] = mouse_action_var.get()
                cmd_data['button'] = mouse_button_var.get()
            elif ct == 'text':
                text_to_insert = text_var.get().strip()
                if not text_to_insert:
                    messagebox.showerror("Error", "Please specify text to insert.", parent=dialog)
                    return
                cmd_data['text'] = text_to_insert

            if edit_phrase and phrase != edit_phrase.lower():
                del self.app.command_executor.commands[edit_phrase]

            self.app.command_executor.commands[phrase] = cmd_data
            self.save_commands()
            self.populate_commands_list(self.cmd_search_var.get())
            dialog.destroy()
            messagebox.showinfo("Success", f"Command '{phrase}' saved successfully!",
                                parent=self.sw.window)

        ctk.CTkButton(btn_frame, text="Save",   width=100, command=_save).pack(side='right', padx=(10, 0))
        ctk.CTkButton(btn_frame, text="Cancel", width=100, fg_color="gray40",
                      command=dialog.destroy).pack(side='right')

    def delete_command(self) -> None:
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to delete.",
                                   parent=self.sw.window)
            return
        if messagebox.askyesno("Confirm Delete",
                               f"Are you sure you want to delete the command '{phrase}'?",
                               parent=self.sw.window):
            if phrase in self.app.command_executor.commands:
                del self.app.command_executor.commands[phrase]
                self.save_commands()
                self.populate_commands_list(self.cmd_search_var.get())
                messagebox.showinfo("Deleted", f"Command '{phrase}' deleted.",
                                    parent=self.sw.window)

    def test_command(self) -> None:
        phrase = self.get_selected_command()
        if not phrase:
            messagebox.showwarning("No Selection", "Please select a command to test.",
                                   parent=self.sw.window)
            return
        self.sw.window.iconify()
        self.sw.window.after(500, lambda: self._execute_test_command(phrase))

    def _execute_test_command(self, phrase: str) -> None:
        try:
            result = self.app.command_executor.execute_command(phrase)
            self.sw.window.after(500, self.sw.window.deiconify)
            if result:
                messagebox.showinfo("Test Result",
                                    f"Command '{phrase}' executed successfully!",
                                    parent=self.sw.window)
            else:
                messagebox.showwarning("Test Result",
                                       f"Command '{phrase}' not found or failed.",
                                       parent=self.sw.window)
        except Exception as e:
            self.sw.window.deiconify()
            messagebox.showerror("Test Error", f"Error executing command:\n{e}",
                                 parent=self.sw.window)

    def reload_commands(self) -> None:
        try:
            self.app.command_executor.load_commands()
            self.populate_commands_list(self.cmd_search_var.get())
            messagebox.showinfo("Reloaded",
                                f"Loaded {len(self.app.command_executor.commands)} commands.",
                                parent=self.sw.window)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload commands:\n{e}",
                                 parent=self.sw.window)

    def save_commands(self) -> None:
        commands_path = Path(__file__).parent.parent / 'commands.json'
        try:
            data = {'commands': self.app.command_executor.commands}
            with open(commands_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save commands:\n{e}",
                                 parent=self.sw.window)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self) -> None:
        if not self._built:
            return

        # Command mode button + suppress
        if self.cmd_mode_button_var is not None:
            new_btn_label = self.cmd_mode_button_var.get()
            new_btn_key   = self._CMD_BUTTON_OPTIONS.get(new_btn_label, 'mouse4')
            cm_cfg = dict(self.app.config.get('command_mode', {}) or {})
            cm_cfg['button'] = new_btn_key
            if self.cmd_mode_suppress_var is not None:
                cm_cfg['suppress_button'] = bool(self.cmd_mode_suppress_var.get())
            self.app.update_config({'command_mode': cm_cfg}, save=False)

        # Command packs
        if self._pack_vars:
            from samsara.command_packs import PACKS
            new_packs = dict(self.app.config.get('command_packs', {}) or {})
            for pack_id, var in self._pack_vars.items():
                meta = PACKS.get(pack_id, {})
                if not meta.get('always_on'):
                    try:
                        new_packs[pack_id] = bool(var.get())
                    except Exception:
                        pass
            self.app.update_config({'command_packs': new_packs}, save=False)
