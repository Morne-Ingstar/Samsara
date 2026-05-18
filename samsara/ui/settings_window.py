"""
Samsara Settings Window

Full settings UI with tabbed interface for configuring all app options.
Extracted from dictation.py to reduce monolith size.
"""

import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

import customtkinter as ctk

from samsara.profiles import ProfileManager
from samsara.ui.profile_manager_ui import ProfileManagerWindow
from samsara.ui.tabs.general_tab import GeneralTab
from samsara.ui.tabs.advanced_tab import AdvancedTab
from samsara.ui.tabs.cloud_llm_tab import CloudLLMTab
from samsara.ui.tabs.hotkeys_tab import HotkeysTab
from samsara.ui.tabs.sounds_tab import SoundsTab
from samsara.ui.tabs.commands_tab import CommandsTab
from samsara.ui.tabs.alarms_tab import AlarmsTab
from samsara.ui.tabs.health_tab import HealthTab
from samsara.ui.tts_settings_tab import TTSSettingsTab


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.window = None
        self.available_mics = []
        # Populated by HotkeysTab.build() and build_alarms_tab() as tabs are visited.
        self.hotkey_buttons = {}

    def show(self):
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None

        # Use cached mic list for instant open; refresh in background
        self.available_mics = self.app.available_mics or []

        # Set CustomTkinter appearance based on system
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        # Create modern CTk window
        self.window = ctk.CTkToplevel(self.app.root)
        self.window.title("Samsara Settings")
        self.window.geometry("920x700")
        self.window.resizable(True, True)
        self.window.minsize(860, 600)

        # Hide window while building UI to prevent incremental rendering
        self.window.withdraw()

        # Apply the Samsara icon (CTkToplevel races with its default icon
        # ~200ms after construction, so apply now and re-apply after).
        if hasattr(self.app, '_apply_window_icon'):
            self.app._apply_window_icon(self.window)
            try:
                self.window.after(300, lambda: self.app._apply_window_icon(self.window))
            except Exception:
                pass

        # Use grid layout for reliable button placement
        self.window.grid_rowconfigure(0, weight=1)
        self.window.grid_columnconfigure(0, weight=1)

        # Create tabview (modern tabs)
        self.tabview = ctk.CTkTabview(self.window, corner_radius=10, command=self.on_tab_changed)
        self.tabview.grid(row=0, column=0, sticky='nsew', padx=20, pady=(20, 10))

        # Bottom buttons frame
        btn_frame = ctk.CTkFrame(self.window, fg_color="transparent", height=60)
        btn_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 20))
        btn_frame.grid_propagate(False)

        # Buttons inside the frame
        self.apply_btn = ctk.CTkButton(btn_frame, text="Apply & Close", width=140, height=40,
                                       command=self.save_and_close)
        self.apply_btn.pack(side='right', padx=(10, 0), pady=10)

        self.cancel_btn = ctk.CTkButton(btn_frame, text="Cancel", width=100, height=40,
                                        fg_color="gray40", hover_color="gray30",
                                        command=self.close)
        self.cancel_btn.pack(side='right', pady=10)

        # Add tabs
        self.tabview.add("General")
        self.tabview.add("Hotkeys & Modes")
        self.tabview.add("Commands")
        self.tabview.add("Sounds")
        self.tabview.add("Text-to-Speech")
        self.tabview.add("Ava / Cloud")
        self.tabview.add("Alarms")
        self.tabview.add("Health")
        self.tabview.add("Smart Actions")
        self.tabview.add("Advanced")

        # Instantiate extracted tab classes
        self.general_tab   = GeneralTab(self.tabview.tab("General"), self.app, self)
        self.hotkeys_tab   = HotkeysTab(self.tabview.tab("Hotkeys & Modes"), self.app, self)
        self.sounds_tab    = SoundsTab(self.tabview.tab("Sounds"), self.app, self)
        self.commands_tab  = CommandsTab(self.tabview.tab("Commands"), self.app, self)
        self.alarms_tab    = AlarmsTab(self.tabview.tab("Alarms"), self.app, self)
        self.health_tab    = HealthTab(self.tabview.tab("Health"), self.app, self)
        self.tts_tab       = TTSSettingsTab(self)
        self.advanced_tab  = AdvancedTab(self.tabview.tab("Advanced"), self.app)
        self.cloud_llm_tab = CloudLLMTab(self.tabview.tab("Ava / Cloud"), self.app)

        # Lazy tab loading -- only build tabs on first visit
        self._tab_builders = {
            "General":        {"built": False, "builder": self.general_tab.build},
            "Hotkeys & Modes":{"built": False, "builder": self.hotkeys_tab.build},
            "Commands":       {"built": False, "builder": self.commands_tab.build},
            "Sounds":         {"built": False, "builder": self.sounds_tab.build},
            "Text-to-Speech": {"built": False, "builder": self.tts_tab.build},
            "Ava / Cloud":    {"built": False, "builder": self.cloud_llm_tab.build},
            "Alarms":         {"built": False, "builder": self.alarms_tab.build},
            "Health":         {"built": False, "builder": self.health_tab.build},
            "Smart Actions":  {"built": False, "builder": self.build_smart_actions_tab},
            "Advanced":       {"built": False, "builder": self.advanced_tab.build},
        }
        # For backward compat with save_settings which checks self.built_tabs
        self.built_tabs = set()

        # Build only the default tab (General) via staged loading
        self._build_tab("General")

        self.window.protocol("WM_DELETE_WINDOW", self.close)

        # Show the fully-built window and bring to front
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

        # Refresh mic list in background so the window opens instantly
        def _refresh_mics():
            fresh = self.app.get_available_microphones()
            if self.window:
                self.window.after(0, self._apply_mic_refresh, fresh)
        threading.Thread(target=_refresh_mics, daemon=True).start()

    def _apply_mic_refresh(self, fresh_mics):
        """Update the mic combobox with a freshly-enumerated mic list."""
        self.available_mics = fresh_mics
        self.app.available_mics = fresh_mics
        if hasattr(self, 'general_tab') and self.general_tab.mic_combo is not None:
            try:
                names = [m['name'] for m in fresh_mics]
                self.general_tab.mic_combo.configure(values=names)
            except Exception:
                pass

    def on_tab_changed(self):
        """Handle tab changes -- build tab on first visit."""
        current_tab = self.tabview.get()
        self._build_tab(current_tab)

    def _build_tab(self, tab_name):
        """Build a tab if not already built, using staged loading."""
        entry = self._tab_builders.get(tab_name)
        if not entry or entry["built"]:
            return
        entry["built"] = True
        self.built_tabs.add(tab_name)

        builder = entry["builder"]
        gen = builder()
        if gen is None:
            return  # builder is not a generator (legacy)

        def _step():
            try:
                next(gen)
                if self.window:
                    self.window.after(5, _step)
            except StopIteration:
                pass
        _step()

    # build_general_tab()   → samsara/ui/tabs/general_tab.py  (GeneralTab)
    # build_hotkeys_modes_tab() → samsara/ui/tabs/hotkeys_tab.py (HotkeysTab)
    # build_sounds_tab()    → samsara/ui/tabs/sounds_tab.py   (SoundsTab)
    # build_tts_tab()       → samsara/ui/tts_settings_tab.py  (TTSSettingsTab)

    # build_sounds_tab() extracted to samsara/ui/tabs/sounds_tab.py (SoundsTab)

    # build_tts_tab() extracted to samsara/ui/tts_settings_tab.py (TTSSettingsTab)

    def build_smart_actions_tab(self):
        """Build the Smart Actions tab -- brain dump path + earcons."""
        from tkinter import filedialog

        # Lazy import so tests that exercise the plugin directly don't need
        # the full settings stack loaded.
        try:
            from plugins.commands import smart_actions as _smart_actions
        except Exception:
            _smart_actions = None

        sa_tab = self.tabview.tab("Smart Actions")
        sa_scroll = ctk.CTkScrollableFrame(sa_tab, fg_color="transparent")
        sa_scroll.pack(fill='both', expand=True)

        # Header / description
        header = ctk.CTkLabel(sa_scroll, text="Brain Dump",
                              font=ctk.CTkFont(size=16, weight="bold"))
        header.pack(anchor='w', pady=(15, 10))

        desc = ctk.CTkLabel(
            sa_scroll,
            text=("Say 'Jarvis, note ...' or 'Jarvis, brain dump ...' to append a\n"
                  "timestamped entry to your brain dump file."),
            text_color="gray", justify='left')
        desc.pack(anchor='w', padx=2, pady=(0, 10))

        sa_frame = ctk.CTkFrame(sa_scroll, corner_radius=10)
        sa_frame.pack(fill='x', pady=(0, 20))

        sa_config = self.app.config.get('smart_actions', {}) or {}
        default_path = sa_config.get('brain_dump_path', '')
        if not default_path and _smart_actions is not None:
            default_path = str(_smart_actions.default_brain_dump_path())

        # --- Path row ---
        path_label = ctk.CTkLabel(sa_frame, text="Brain dump location:")
        path_label.pack(anchor='w', padx=15, pady=(15, 4))

        path_row = ctk.CTkFrame(sa_frame, fg_color="transparent")
        path_row.pack(fill='x', padx=15, pady=(0, 4))

        self.smart_actions_path_var = tk.StringVar(value=default_path)
        path_entry = ctk.CTkEntry(path_row, textvariable=self.smart_actions_path_var,
                                  width=440)
        path_entry.pack(side='left', padx=(0, 6))

        def _browse_path():
            initial = self.smart_actions_path_var.get().strip()
            initial_dir = str(Path(initial).expanduser().parent) if initial else str(Path.home())
            initial_file = Path(initial).name if initial else "Samsara Brain Dump.md"
            chosen = filedialog.asksaveasfilename(
                title="Choose brain dump file",
                defaultextension=".md",
                initialdir=initial_dir,
                initialfile=initial_file,
                filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")],
                parent=self.window,
            )
            if chosen:
                self.smart_actions_path_var.set(chosen)
                self._validate_smart_actions_path()

        ctk.CTkButton(path_row, text="Browse...", width=90,
                      command=_browse_path).pack(side='left')

        # Validation feedback label
        self._smart_actions_status_label = ctk.CTkLabel(
            sa_frame, text="", text_color="gray", justify='left', wraplength=540)
        self._smart_actions_status_label.pack(anchor='w', padx=15, pady=(0, 10))

        # Re-validate when the user finishes typing
        self.smart_actions_path_var.trace_add(
            'write', lambda *_: self._validate_smart_actions_path())
        self._validate_smart_actions_path()

        # --- Earcons toggle ---
        self.smart_actions_earcons_var = tk.BooleanVar(
            value=bool(sa_config.get('earcons_enabled', True)))
        ctk.CTkCheckBox(sa_frame, text="Play earcons on capture (capture started + saved)",
                        variable=self.smart_actions_earcons_var
                        ).pack(anchor='w', padx=15, pady=(0, 10))

        # --- Open file button ---
        def _open_file():
            if _smart_actions is None:
                messagebox.showerror(
                    "Smart Actions",
                    "Smart Actions plugin is not loaded.",
                    parent=self.window)
                return
            current = self.smart_actions_path_var.get().strip()
            if not _smart_actions.open_brain_dump_file(current):
                messagebox.showerror(
                    "Smart Actions",
                    f"Could not open brain dump file:\n{current}",
                    parent=self.window)

        ctk.CTkButton(sa_frame, text="Open brain dump file", width=200,
                      command=_open_file).pack(anchor='w', padx=15, pady=(0, 15))

        # ------------------------------------------------------------------ #
        # Phase 2: Agent Endpoint                                              #
        # ------------------------------------------------------------------ #
        ctk.CTkLabel(sa_scroll, text="Agent Endpoint",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(anchor='w', pady=(10, 6))

        agent_frame = ctk.CTkFrame(sa_scroll, corner_radius=10)
        agent_frame.pack(fill='x', pady=(0, 16))

        # Enable toggle
        self._sa_enabled_var = tk.BooleanVar(
            value=bool(sa_config.get('enabled', False)))
        ctk.CTkCheckBox(agent_frame, text="Enable Smart Actions agent routing",
                        variable=self._sa_enabled_var
                        ).pack(anchor='w', padx=15, pady=(14, 6))

        # URL row
        url_row = ctk.CTkFrame(agent_frame, fg_color="transparent")
        url_row.pack(fill='x', padx=15, pady=(0, 4))
        ctk.CTkLabel(url_row, text="Endpoint URL:", width=120, anchor='w').pack(side='left')
        self._sa_url_var = tk.StringVar(value=sa_config.get('endpoint_url', ''))
        ctk.CTkEntry(url_row, textvariable=self._sa_url_var,
                     width=360, placeholder_text="https://your-agent.example.com/v1/chat"
                     ).pack(side='left', padx=(0, 6))

        # Auth row
        auth_row = ctk.CTkFrame(agent_frame, fg_color="transparent")
        auth_row.pack(fill='x', padx=15, pady=(0, 4))
        ctk.CTkLabel(auth_row, text="Auth header:", width=120, anchor='w').pack(side='left')
        self._sa_auth_var = tk.StringVar(value=sa_config.get('auth_header', ''))
        auth_entry = ctk.CTkEntry(auth_row, textvariable=self._sa_auth_var,
                                  width=300, show='*',
                                  placeholder_text="Bearer sk-...")
        auth_entry.pack(side='left', padx=(0, 6))

        def _toggle_auth_show():
            auth_entry.configure(show='' if auth_entry.cget('show') == '*' else '*')
        ctk.CTkButton(auth_row, text="Show/Hide", width=90,
                      command=_toggle_auth_show).pack(side='left')

        # Test connection button + status
        test_row = ctk.CTkFrame(agent_frame, fg_color="transparent")
        test_row.pack(fill='x', padx=15, pady=(6, 14))
        self._sa_test_label = ctk.CTkLabel(test_row, text="", text_color="gray",
                                           font=ctk.CTkFont(size=11))

        def _test_connection():
            from samsara.smart_actions_bridge import SmartActionsBridge
            url = self._sa_url_var.get().strip()
            auth = self._sa_auth_var.get().strip()
            self._sa_test_label.configure(text="Testing...", text_color="gray")
            self._sa_test_label.pack(side='left', padx=(10, 0))

            def _do_test():
                bridge = SmartActionsBridge({'endpoint_url': url, 'auth_header': auth})
                ok, msg = bridge.test_connection(timeout_s=5)
                color = "#1f6aa5" if ok else "#c0392b"
                prefix = "Connected" if ok else "Unreachable"
                try:
                    self._sa_test_label.configure(
                        text=f"{prefix}: {msg}", text_color=color)
                except Exception:
                    pass

            import threading
            threading.Thread(target=_do_test, daemon=True).start()

        ctk.CTkButton(test_row, text="Test Connection", width=140,
                      command=_test_connection).pack(side='left')
        self._sa_test_label.pack(side='left', padx=(10, 0))

        # Session window
        session_row = ctk.CTkFrame(agent_frame, fg_color="transparent")
        session_row.pack(fill='x', padx=15, pady=(0, 4))
        ctk.CTkLabel(session_row, text="Session window:", width=120,
                     anchor='w').pack(side='left')
        self._sa_session_var = tk.IntVar(
            value=int(sa_config.get('session_window_minutes', 5)))
        ctk.CTkEntry(session_row, textvariable=self._sa_session_var,
                     width=60).pack(side='left', padx=(0, 6))
        ctk.CTkLabel(session_row, text="minutes of inactivity before new session"
                     ).pack(side='left')

        # ------------------------------------------------------------------ #
        # Phase 2: Security                                                    #
        # ------------------------------------------------------------------ #
        ctk.CTkLabel(sa_scroll, text="Security",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(anchor='w', pady=(10, 6))

        sec_frame = ctk.CTkFrame(sa_scroll, corner_radius=10)
        sec_frame.pack(fill='x', pady=(0, 16))

        # Allowed directories
        ctk.CTkLabel(sec_frame, text="Allowed directories for file operations:",
                     anchor='w').pack(anchor='w', padx=15, pady=(14, 4))

        self._sa_dirs_var = tk.StringVar(
            value='\n'.join(sa_config.get('allowed_directories',
                                         [str(Path.home() / 'Documents')])))
        dirs_box = ctk.CTkTextbox(sec_frame, height=70, width=500)
        dirs_box.insert('1.0', self._sa_dirs_var.get())
        dirs_box.pack(padx=15, pady=(0, 6))

        # Allowed domains
        ctk.CTkLabel(sec_frame, text="Allowed domains for webhook triggers:",
                     anchor='w').pack(anchor='w', padx=15, pady=(6, 4))
        self._sa_domains_var = tk.StringVar(
            value='\n'.join(sa_config.get('allowed_domains', [])))
        domains_box = ctk.CTkTextbox(sec_frame, height=70, width=500)
        domains_box.insert('1.0', self._sa_domains_var.get())
        domains_box.pack(padx=15, pady=(0, 6))

        # Memorized approvals
        ctk.CTkLabel(sec_frame, text="Memorized approvals:",
                     anchor='w').pack(anchor='w', padx=15, pady=(6, 4))
        approvals = sa_config.get('tier2_approvals', {})
        approvals_text = '\n'.join(approvals.keys()) if approvals else '(none)'
        approvals_box = ctk.CTkTextbox(sec_frame, height=60, width=500,
                                       state='disabled')
        approvals_box.configure(state='normal')
        approvals_box.insert('1.0', approvals_text)
        approvals_box.configure(state='disabled')
        approvals_box.pack(padx=15, pady=(0, 6))

        def _revoke_approvals():
            from tkinter import messagebox
            if not approvals:
                return
            if messagebox.askyesno(
                    "Revoke approvals",
                    "Remove ALL memorized approvals? You will be asked to confirm "
                    "each action again.",
                    parent=self.window):
                if hasattr(self.app, 'revoke_tier2_approvals'):
                    self.app.revoke_tier2_approvals()
                approvals_box.configure(state='normal')
                approvals_box.delete('1.0', 'end')
                approvals_box.insert('1.0', '(none)')
                approvals_box.configure(state='disabled')

        ctk.CTkButton(sec_frame, text="Revoke All Approvals", width=180,
                      fg_color="#7a2a2a", hover_color="#9a3030",
                      command=_revoke_approvals
                      ).pack(anchor='w', padx=15, pady=(0, 14))

        # Stash references needed by save_settings
        self._sa_dirs_box = dirs_box
        self._sa_domains_box = domains_box

    def _validate_smart_actions_path(self):
        """Refresh the validation status label under the brain dump path entry."""
        if not hasattr(self, '_smart_actions_status_label'):
            return
        try:
            from plugins.commands import smart_actions as _smart_actions
        except Exception:
            self._smart_actions_status_label.configure(
                text="(plugin not loaded -- save still works, path will be used at runtime)",
                text_color="gray")
            return

        raw = self.smart_actions_path_var.get()
        ok, msg = _smart_actions.validate_brain_dump_path(raw)
        self._smart_actions_status_label.configure(
            text=msg,
            text_color=("#1f6aa5" if ok else "#c0392b"))

    def start_capture(self, hotkey_name):
        """Delegate hotkey capture to HotkeysTab (which owns all capture state)."""
        if hasattr(self, 'hotkeys_tab') and self.hotkeys_tab is not None:
            self.hotkeys_tab.start_capture(hotkey_name)

    def save_settings(self):

        old_model = self.app.config.get('model_size', 'base')

        # General tab: mic, basic options, model, listening indicator
        gen_result = self.general_tab.save()
        new_model = gen_result['new_model']
        model_changed = old_model != new_model
        mic_changed = gen_result['mic_changed']

        # Hotkeys & Modes tab -- only if visited
        if "Hotkeys & Modes" in self.built_tabs:
            self.hotkeys_tab.save()

        # Sounds tab -- only if visited
        if "Sounds" in self.built_tabs:
            self.sounds_tab.save()

        # Advanced tab: device, thresholds, AEC, wake word
        adv_info = self.advanced_tab.save()

        # Alarms tab -- only if visited
        if "Alarms" in self.built_tabs:
            self.alarms_tab.save()

        # Save Text-to-Speech settings -- only if the tab was visited
        if "Text-to-Speech" in self.built_tabs:
            self.tts_tab.save()

        # Save Ava / Cloud LLM settings -- only if the tab was visited
        if "Ava / Cloud" in self.built_tabs and hasattr(self, 'cloud_llm_tab'):
            self.cloud_llm_tab.save()

        # Commands tab -- only if visited
        if "Commands" in self.built_tabs:
            self.commands_tab.save()

        # Save Smart Actions settings -- only if the tab was visited
        if "Smart Actions" in self.built_tabs:
            sa_cfg = dict(self.app.config.get('smart_actions', {}) or {})
            sa_cfg['brain_dump_path'] = self.smart_actions_path_var.get().strip() or sa_cfg.get('brain_dump_path', '')
            sa_cfg['earcons_enabled'] = bool(self.smart_actions_earcons_var.get())
            # Phase 2 fields
            if hasattr(self, '_sa_enabled_var'):
                sa_cfg['enabled'] = bool(self._sa_enabled_var.get())
            if hasattr(self, '_sa_url_var'):
                sa_cfg['endpoint_url'] = self._sa_url_var.get().strip()
            if hasattr(self, '_sa_auth_var'):
                sa_cfg['auth_header'] = self._sa_auth_var.get().strip()
            if hasattr(self, '_sa_session_var'):
                try:
                    sa_cfg['session_window_minutes'] = int(self._sa_session_var.get())
                except (ValueError, tk.TclError):
                    pass
            if hasattr(self, '_sa_dirs_box'):
                raw = self._sa_dirs_box.get('1.0', 'end').strip()
                sa_cfg['allowed_directories'] = [
                    d.strip() for d in raw.splitlines() if d.strip()]
            if hasattr(self, '_sa_domains_box'):
                raw = self._sa_domains_box.get('1.0', 'end').strip()
                sa_cfg['allowed_domains'] = [
                    d.strip() for d in raw.splitlines() if d.strip()]
            self.app.update_config({'smart_actions': sa_cfg}, save=False)

        self.app.command_mode_enabled = self.app.config['command_mode_enabled']

        self.app.persist_config()

        # Apply mode change at runtime (delegates to DictationApp.apply_mode)
        new_mode = self.app.config['mode']
        if self.app.apply_mode(new_mode):
            self.app.persist_config()
            if hasattr(self.app, 'tray_icon') and hasattr(self.app, 'get_menu'):
                try:
                    self.app.tray_icon.menu = self.app.get_menu()
                except Exception as e:
                    print(f"[MODE] Failed to refresh tray menu: {e}")

        # Apply wake word enable/disable at runtime
        new_ww = self.app.config.get('wake_word_enabled', False)
        self.app.set_wake_word_enabled(new_ww)

        if mic_changed and hasattr(self.app, 'tray_icon') and hasattr(self.app, 'get_menu'):
            self.app.tray_icon.menu = self.app.get_menu()
            self.app._update_tray_tooltip()
            print(f"Microphone changed to: {self.app.get_current_microphone_name()}")

        print("Settings saved successfully!")

        if model_changed:
            self.prompt_restart_for_model(old_model, new_model)
        elif adv_info['device_changed']:
            self.prompt_restart_for_device(adv_info['old_device'], adv_info['new_device'])

    def prompt_restart_for_device(self, old_device, new_device):
        """Ask user if they want to restart to apply new device"""
        device_names = {'cpu': 'CPU', 'cuda': 'CUDA (GPU)'}
        old_name = device_names.get(old_device, old_device)
        new_name = device_names.get(new_device, new_device)
        result = messagebox.askyesno(
            "Restart Required",
            f"Device changed from '{old_name}' to '{new_name}'.\n\n"
            f"The app needs to restart to use the new device.\n\n"
            f"Restart now?",
            parent=self.window
        )
        if result:
            self.restart_app()

    def prompt_restart_for_model(self, old_model, new_model):
        """Ask user if they want to restart to apply new model"""
        result = messagebox.askyesno(
            "Restart Required",
            f"Model changed from '{old_model}' to '{new_model}'.\n\n"
            f"The app needs to restart to load the new model.\n"
            f"(The new model will be downloaded if needed)\n\n"
            f"Restart now?",
            parent=self.window
        )
        if result:
            self.restart_app()

    def restart_app(self):
        """Restart the application"""
        import subprocess
        print("Restarting application...")
        
        # Stop background services first
        if hasattr(self.app, 'tray_icon'):
            try:
                self.app.tray_icon.stop()
            except:
                pass
        if hasattr(self.app, 'keyboard_listener'):
            try:
                self.app.keyboard_listener.stop()
            except:
                pass
        
        # Spawn new process BEFORE exiting
        if sys.platform == 'win32':
            # Use the VBS launcher on Windows for proper console-less restart
            launcher = Path(__file__).parent / "_launcher.vbs"
            if launcher.exists():
                subprocess.Popen(['wscript', str(launcher)], 
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            else:
                # Fallback: direct pythonw launch
                script = Path(__file__).resolve()
                subprocess.Popen([sys.executable, str(script)],
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | 
                                           subprocess.DETACHED_PROCESS)
        else:
            # Unix: spawn detached process
            script = Path(__file__).resolve()
            subprocess.Popen([sys.executable, str(script)],
                           start_new_session=True)
        
        # Now exit current instance
        self.close()
        sys.exit(0)

    def save_and_close(self):
        """Save settings and close window"""
        self.save_settings()
        self.close()

    def get_startup_path(self):
        """Get the platform-specific startup/autostart file path"""
        if sys.platform == 'win32':
            startup_folder = Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup'
            return startup_folder / 'Samsara.vbs'
        elif sys.platform == 'darwin':  # macOS
            return Path.home() / 'Library' / 'LaunchAgents' / 'com.samsara.plist'
        else:  # Linux
            config_home = os.environ.get('XDG_CONFIG_HOME', '')
            if config_home:
                return Path(config_home) / 'autostart' / 'samsara.desktop'
            return Path.home() / '.config' / 'autostart' / 'samsara.desktop'

    def check_auto_start(self):
        """Check if auto-start is enabled"""
        startup_file = self.get_startup_path()
        return startup_file.exists()

    def toggle_auto_start(self):
        """Enable or disable auto-start (cross-platform)"""
        startup_file = self.get_startup_path()
        script_path = Path(__file__)
        python_exe = sys.executable

        if self.general_tab.auto_start_var.get():
            # Enable auto-start: create platform-specific startup entry
            try:
                startup_file.parent.mkdir(parents=True, exist_ok=True)

                if sys.platform == 'win32':
                    # Windows VBS script
                    vbs_content = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "{script_path.parent}"
WshShell.Run """" & "{python_exe}" & """ """ & "{script_path}" & """", 0, False
Set WshShell = Nothing
'''
                    startup_file.write_text(vbs_content)

                elif sys.platform == 'darwin':
                    # macOS launchd plist
                    plist_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.samsara</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_exe}</string>
        <string>{script_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{script_path.parent}</string>
</dict>
</plist>
'''
                    startup_file.write_text(plist_content)

                else:
                    # Linux .desktop file
                    desktop_content = f'''[Desktop Entry]
Type=Application
Name=Samsara
Exec={python_exe} {script_path}
Path={script_path.parent}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
'''
                    startup_file.write_text(desktop_content)

                platform_name = "Windows" if sys.platform == 'win32' else ("macOS" if sys.platform == 'darwin' else "Linux")
                messagebox.showinfo("Auto-Start Enabled",
                    f"Samsara will now start automatically when {platform_name} starts.",
                    parent=self.window)
            except Exception as e:
                messagebox.showerror("Error",
                    f"Failed to enable auto-start:\n{e}",
                    parent=self.window)
                self.general_tab.auto_start_var.set(False)
        else:
            # Disable auto-start: remove startup entry
            try:
                if startup_file.exists():
                    startup_file.unlink()
                messagebox.showinfo("Auto-Start Disabled",
                    "Samsara will no longer start automatically.",
                    parent=self.window)
            except Exception as e:
                messagebox.showerror("Error",
                    f"Failed to disable auto-start:\n{e}",
                    parent=self.window)
                self.general_tab.auto_start_var.set(True)

    def open_profile_manager(self):
        """Open the profile manager window."""
        # Get the app directory for ProfileManager
        app_dir = Path(__file__).parent
        
        # Initialize profile manager
        pm = ProfileManager(str(app_dir))
        
        # Define callback for when profiles change
        def on_profiles_changed():
            # Reload the commands in the app
            if hasattr(self.app, 'load_commands'):
                self.app.load_commands()
            # Reload training data (vocabulary/corrections)
            if hasattr(self.app, 'load_training_data'):
                self.app.load_training_data()
        
        # Open the profile manager window
        profile_window = ProfileManagerWindow(
            self.window,
            pm,
            on_profiles_changed=on_profiles_changed
        )
        profile_window.show()

    def open_voice_training(self):
        """Open the voice training window from settings."""
        self.app.open_voice_training()

    def open_mic_setup_guide(self):
        """Open the guided mic setup wizard from settings."""
        self.app.open_mic_setup_guide()

    def open_wake_word_debug(self):
        """Open the wake word debug window from settings."""
        self.app.open_wake_word_debug()

    def close(self):
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            finally:
                self.window = None

    def refresh_microphone_list(self):
        """Refresh the microphone list when 'show all devices' is toggled."""
        self.app.config['show_all_audio_devices'] = self.general_tab.show_all_devices_var.get()
        self.available_mics = self.app.get_available_microphones()
        mic_names = [mic['name'] for mic in self.available_mics]
        self.general_tab.mic_combo.configure(values=mic_names)
        if self.general_tab.mic_var.get() not in mic_names and mic_names:
            self.general_tab.mic_var.set(mic_names[0])

