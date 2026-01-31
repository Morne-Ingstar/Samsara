"""
Profile Manager UI for Samsara
Popup window for managing dictionary and command profiles.
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk
from pathlib import Path


class ProfileManagerWindow:
    """UI for managing dictionary and command profiles."""
    
    def __init__(self, parent_window, profile_manager, on_profiles_changed=None):
        """
        Initialize the profile manager window.
        
        Args:
            parent_window: The parent CTk window (Settings window)
            profile_manager: ProfileManager instance
            on_profiles_changed: Callback when profiles are loaded (to reload app data)
        """
        self.parent = parent_window
        self.pm = profile_manager
        self.on_profiles_changed = on_profiles_changed
        self.window = None
    
    def show(self):
        """Show the profile manager window."""
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None
        
        self.window = ctk.CTkToplevel(self.parent)
        self.window.title("Profile Manager")
        self.window.geometry("600x780")
        self.window.resizable(True, True)
        self.window.minsize(500, 500)
        
        # Make it modal-ish (stay on top of settings)
        self.window.transient(self.parent)
        self.window.lift()
        self.window.focus_force()
        
        # Main scrollable container
        main_scroll = ctk.CTkScrollableFrame(self.window, fg_color="transparent")
        main_scroll.pack(fill='both', expand=True, padx=15, pady=15)
        
        # =====================================================================
        # Voice Training Profiles Section
        # =====================================================================
        dict_label = ctk.CTkLabel(main_scroll, text="Voice Training Profiles",
                                  font=ctk.CTkFont(size=18, weight="bold"))
        dict_label.pack(anchor='w', pady=(0, 10))
        
        dict_desc = ctk.CTkLabel(main_scroll, 
                                 text="Manage vocabulary, corrections, and prompt profiles",
                                 text_color="gray")
        dict_desc.pack(anchor='w', pady=(0, 10))
        
        dict_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        dict_frame.pack(fill='x', pady=(0, 20))
        
        # Current profile display
        active = self.pm.get_active_profile_names()
        self.dict_active_label = ctk.CTkLabel(
            dict_frame, 
            text=f"Active: {active['dictionary'] or '(unsaved)'}"
        )
        self.dict_active_label.pack(anchor='w', padx=15, pady=(15, 10))
        
        # Profile selector
        selector_frame = ctk.CTkFrame(dict_frame, fg_color="transparent")
        selector_frame.pack(fill='x', padx=15, pady=(0, 10))
        
        ctk.CTkLabel(selector_frame, text="Select profile:").pack(side='left')
        
        dict_profiles = self.pm.list_dictionary_profiles()
        self.dict_profile_var = tk.StringVar(value=dict_profiles[0] if dict_profiles else "")
        self.dict_combo = ctk.CTkComboBox(
            selector_frame, 
            variable=self.dict_profile_var,
            values=dict_profiles if dict_profiles else ["(none)"],
            width=250,
            state='readonly' if dict_profiles else 'disabled'
        )
        self.dict_combo.pack(side='left', padx=(10, 0))
        
        # Buttons row 1: Load, Save
        btn_row1 = ctk.CTkFrame(dict_frame, fg_color="transparent")
        btn_row1.pack(fill='x', padx=15, pady=(5, 5))
        
        ctk.CTkButton(btn_row1, text="Load (Replace)", width=120,
                     command=lambda: self._load_profile('dictionary', merge=False)
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(btn_row1, text="Load (Merge)", width=120,
                     command=lambda: self._load_profile('dictionary', merge=True)
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(btn_row1, text="Save Current...", width=120,
                     command=lambda: self._save_profile('dictionary')
                     ).pack(side='left')
        
        # Buttons row 2: Import, Export, Delete
        btn_row2 = ctk.CTkFrame(dict_frame, fg_color="transparent")
        btn_row2.pack(fill='x', padx=15, pady=(0, 15))
        
        ctk.CTkButton(btn_row2, text="Import...", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=lambda: self._import_profile('dictionary')
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(btn_row2, text="Export...", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=lambda: self._export_profile('dictionary')
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(btn_row2, text="Delete", width=100,
                     fg_color="#8B0000", hover_color="#A52A2A",
                     command=lambda: self._delete_profile('dictionary')
                     ).pack(side='left')
        
        # =====================================================================
        # Command Profiles Section
        # =====================================================================
        cmd_label = ctk.CTkLabel(main_scroll, text="Command Profiles",
                                 font=ctk.CTkFont(size=18, weight="bold"))
        cmd_label.pack(anchor='w', pady=(10, 10))
        
        cmd_desc = ctk.CTkLabel(main_scroll,
                                text="Manage voice command profiles",
                                text_color="gray")
        cmd_desc.pack(anchor='w', pady=(0, 10))
        
        cmd_frame = ctk.CTkFrame(main_scroll, corner_radius=10)
        cmd_frame.pack(fill='x', pady=(0, 20))
        
        # Current profile display
        self.cmd_active_label = ctk.CTkLabel(
            cmd_frame,
            text=f"Active: {active['commands'] or '(unsaved)'}"
        )
        self.cmd_active_label.pack(anchor='w', padx=15, pady=(15, 10))
        
        # Profile selector
        cmd_selector = ctk.CTkFrame(cmd_frame, fg_color="transparent")
        cmd_selector.pack(fill='x', padx=15, pady=(0, 10))
        
        ctk.CTkLabel(cmd_selector, text="Select profile:").pack(side='left')
        
        cmd_profiles = self.pm.list_command_profiles()
        self.cmd_profile_var = tk.StringVar(value=cmd_profiles[0] if cmd_profiles else "")
        self.cmd_combo = ctk.CTkComboBox(
            cmd_selector,
            variable=self.cmd_profile_var,
            values=cmd_profiles if cmd_profiles else ["(none)"],
            width=250,
            state='readonly' if cmd_profiles else 'disabled'
        )
        self.cmd_combo.pack(side='left', padx=(10, 0))
        
        # Buttons row 1
        cmd_btn_row1 = ctk.CTkFrame(cmd_frame, fg_color="transparent")
        cmd_btn_row1.pack(fill='x', padx=15, pady=(5, 5))
        
        ctk.CTkButton(cmd_btn_row1, text="Load (Replace)", width=120,
                     command=lambda: self._load_profile('commands', merge=False)
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_row1, text="Load (Merge)", width=120,
                     command=lambda: self._load_profile('commands', merge=True)
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_row1, text="Save Current...", width=120,
                     command=lambda: self._save_profile('commands')
                     ).pack(side='left')
        
        # Buttons row 2
        cmd_btn_row2 = ctk.CTkFrame(cmd_frame, fg_color="transparent")
        cmd_btn_row2.pack(fill='x', padx=15, pady=(0, 15))
        
        ctk.CTkButton(cmd_btn_row2, text="Import...", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=lambda: self._import_profile('commands')
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_row2, text="Export...", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=lambda: self._export_profile('commands')
                     ).pack(side='left', padx=(0, 5))
        ctk.CTkButton(cmd_btn_row2, text="Delete", width=100,
                     fg_color="#8B0000", hover_color="#A52A2A",
                     command=lambda: self._delete_profile('commands')
                     ).pack(side='left')
        
        # =====================================================================
        # Info Section
        # =====================================================================
        info_label = ctk.CTkLabel(main_scroll, text="Profile Tips",
                                  font=ctk.CTkFont(size=14, weight="bold"))
        info_label.pack(anchor='w', pady=(10, 5))
        
        tips = """• Load (Replace): Clears current data and loads the profile
• Load (Merge): Adds profile data to your current data (no duplicates)
• Save Current: Saves your active vocabulary/commands as a new profile
• Profiles are stored in the 'profiles' folder and can be shared"""
        
        tips_label = ctk.CTkLabel(main_scroll, text=tips, justify='left',
                                  text_color="gray")
        tips_label.pack(anchor='w', pady=(0, 15))
        
        # Close button
        close_btn = ctk.CTkButton(main_scroll, text="Close", width=100,
                                  command=self.window.destroy)
        close_btn.pack(pady=(10, 0))
    
    def _refresh_combos(self):
        """Refresh the profile dropdown lists."""
        # Dictionary profiles
        dict_profiles = self.pm.list_dictionary_profiles()
        if dict_profiles:
            self.dict_combo.configure(values=dict_profiles, state='readonly')
            if self.dict_profile_var.get() not in dict_profiles:
                self.dict_profile_var.set(dict_profiles[0])
        else:
            self.dict_combo.configure(values=["(none)"], state='disabled')
            self.dict_profile_var.set("")
        
        # Command profiles
        cmd_profiles = self.pm.list_command_profiles()
        if cmd_profiles:
            self.cmd_combo.configure(values=cmd_profiles, state='readonly')
            if self.cmd_profile_var.get() not in cmd_profiles:
                self.cmd_profile_var.set(cmd_profiles[0])
        else:
            self.cmd_combo.configure(values=["(none)"], state='disabled')
            self.cmd_profile_var.set("")
        
        # Update active labels
        active = self.pm.get_active_profile_names()
        self.dict_active_label.configure(
            text=f"Active: {active['dictionary'] or '(unsaved)'}"
        )
        self.cmd_active_label.configure(
            text=f"Active: {active['commands'] or '(unsaved)'}"
        )
    
    def _load_profile(self, profile_type: str, merge: bool):
        """Load a profile (dictionary or commands)."""
        if profile_type == 'dictionary':
            name = self.dict_profile_var.get()
            if not name or name == "(none)":
                messagebox.showwarning("No Profile", "Select a profile to load.")
                return
            
            # Confirmation dialog
            action = "merge with" if merge else "replace"
            if not messagebox.askyesno("Confirm Load",
                f"This will {action} your current vocabulary and corrections.\n\nContinue?"):
                return
            
            success, msg = self.pm.load_dictionary_profile(name, merge=merge)
            if success:
                self.pm.set_active_profile_names(dictionary=name)
                self._refresh_combos()
                if self.on_profiles_changed:
                    self.on_profiles_changed()
            messagebox.showinfo("Load Profile", msg)
        
        else:  # commands
            name = self.cmd_profile_var.get()
            if not name or name == "(none)":
                messagebox.showwarning("No Profile", "Select a profile to load.")
                return
            
            action = "merge with" if merge else "replace"
            if not messagebox.askyesno("Confirm Load",
                f"This will {action} your current commands.\n\nContinue?"):
                return
            
            success, msg = self.pm.load_command_profile(name, merge=merge)
            if success:
                self.pm.set_active_profile_names(commands=name)
                self._refresh_combos()
                if self.on_profiles_changed:
                    self.on_profiles_changed()
            messagebox.showinfo("Load Profile", msg)
    
    def _save_profile(self, profile_type: str):
        """Save current data as a new profile."""
        dialog = SaveProfileDialog(self.window, profile_type)
        result = dialog.get_result()
        
        if result is None:
            return  # Cancelled
        
        name, description, author = result
        
        if profile_type == 'dictionary':
            # Check if exists
            existing = self.pm.list_dictionary_profiles()
            overwrite = False
            if name in existing:
                if not messagebox.askyesno("Overwrite?",
                    f"Profile '{name}' already exists. Overwrite?"):
                    return
                overwrite = True
            
            success, msg = self.pm.save_dictionary_profile(
                name, description, author, overwrite=overwrite
            )
            if success:
                self.pm.set_active_profile_names(dictionary=name)
                self._refresh_combos()
        else:
            existing = self.pm.list_command_profiles()
            overwrite = False
            if name in existing:
                if not messagebox.askyesno("Overwrite?",
                    f"Profile '{name}' already exists. Overwrite?"):
                    return
                overwrite = True
            
            success, msg = self.pm.save_command_profile(
                name, description, author, overwrite=overwrite
            )
            if success:
                self.pm.set_active_profile_names(commands=name)
                self._refresh_combos()
        
        messagebox.showinfo("Save Profile", msg)
    
    def _import_profile(self, profile_type: str):
        """Import a profile from a file."""
        file_path = filedialog.askopenfilename(
            title=f"Import {profile_type.title()} Profile",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        
        if not file_path:
            return
        
        if profile_type == 'dictionary':
            success, msg = self.pm.import_dictionary_profile(file_path)
        else:
            success, msg = self.pm.import_command_profile(file_path)
        
        if success:
            self._refresh_combos()
        
        messagebox.showinfo("Import Profile", msg)
    
    def _export_profile(self, profile_type: str):
        """Export a profile to a file."""
        if profile_type == 'dictionary':
            name = self.dict_profile_var.get()
        else:
            name = self.cmd_profile_var.get()
        
        if not name or name == "(none)":
            messagebox.showwarning("No Profile", "Select a profile to export.")
            return
        
        file_path = filedialog.asksaveasfilename(
            title=f"Export {profile_type.title()} Profile",
            defaultextension=".json",
            initialfile=f"{name}.json",
            filetypes=[("JSON files", "*.json")]
        )
        
        if not file_path:
            return
        
        if profile_type == 'dictionary':
            success, msg = self.pm.export_dictionary_profile(name, file_path)
        else:
            success, msg = self.pm.export_command_profile(name, file_path)
        
        messagebox.showinfo("Export Profile", msg)
    
    def _delete_profile(self, profile_type: str):
        """Delete a profile."""
        if profile_type == 'dictionary':
            name = self.dict_profile_var.get()
        else:
            name = self.cmd_profile_var.get()
        
        if not name or name == "(none)":
            messagebox.showwarning("No Profile", "Select a profile to delete.")
            return
        
        if not messagebox.askyesno("Confirm Delete",
            f"Permanently delete profile '{name}'?\n\nThis cannot be undone."):
            return
        
        if profile_type == 'dictionary':
            success, msg = self.pm.delete_dictionary_profile(name)
        else:
            success, msg = self.pm.delete_command_profile(name)
        
        if success:
            self._refresh_combos()
        
        messagebox.showinfo("Delete Profile", msg)


class SaveProfileDialog:
    """Dialog for saving a new profile with name, description, author."""
    
    def __init__(self, parent, profile_type: str):
        self.result = None
        
        self.dialog = ctk.CTkToplevel(parent)
        self.dialog.title(f"Save {profile_type.title()} Profile")
        self.dialog.geometry("400x320")
        self.dialog.resizable(False, False)
        self.dialog.transient(parent)
        self.dialog.grab_set()
        
        # Center on parent
        self.dialog.update_idletasks()
        
        frame = ctk.CTkFrame(self.dialog, fg_color="transparent")
        frame.pack(fill='both', expand=True, padx=20, pady=20)
        
        # Name field (required)
        ctk.CTkLabel(frame, text="Profile Name *").pack(anchor='w')
        self.name_entry = ctk.CTkEntry(frame, width=350)
        self.name_entry.pack(anchor='w', pady=(5, 15))
        self.name_entry.focus()
        
        # Description field (optional)
        ctk.CTkLabel(frame, text="Description").pack(anchor='w')
        self.desc_entry = ctk.CTkEntry(frame, width=350)
        self.desc_entry.pack(anchor='w', pady=(5, 15))
        
        # Author field (optional)
        ctk.CTkLabel(frame, text="Author").pack(anchor='w')
        self.author_entry = ctk.CTkEntry(frame, width=350)
        self.author_entry.pack(anchor='w', pady=(5, 20))
        
        # Buttons
        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(fill='x')
        
        ctk.CTkButton(btn_frame, text="Cancel", width=100,
                     fg_color="gray40", hover_color="gray30",
                     command=self.dialog.destroy).pack(side='right')
        ctk.CTkButton(btn_frame, text="Save", width=100,
                     command=self._save).pack(side='right', padx=(0, 10))
        
        # Bind Enter key
        self.dialog.bind('<Return>', lambda e: self._save())
        
        # Wait for dialog to close
        self.dialog.wait_window()
    
    def _save(self):
        name = self.name_entry.get().strip()
        if not name:
            messagebox.showwarning("Name Required", "Please enter a profile name.")
            return
        
        # Sanitize name (remove invalid filename characters)
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            name = name.replace(char, '')
        
        self.result = (
            name,
            self.desc_entry.get().strip(),
            self.author_entry.get().strip()
        )
        self.dialog.destroy()
    
    def get_result(self):
        return self.result
