"""
Samsara History Window

Displays dictation and command history with copy/clear functionality.
"""

import tkinter as tk
from tkinter import messagebox
from datetime import datetime

import customtkinter as ctk


class HistoryWindow:
    """Window to view dictation history"""

    def __init__(self, app):
        self.app = app
        self.window = None

    def show(self):
        if self.window is not None:
            try:
                self.window.lift()
                self.window.focus_force()
                return
            except:
                self.window = None

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        self.window = ctk.CTkToplevel(self.app.root)
        self.window.title("Dictation History")
        self.window.geometry("600x500")
        self.window.resizable(True, True)
        self.window.minsize(400, 300)

        self.window.lift()
        self.window.focus_force()
        self.window.after(100, lambda: self.window.lift())

        # Use grid layout
        self.window.grid_rowconfigure(0, weight=1)
        self.window.grid_columnconfigure(0, weight=1)

        # Main frame
        main_frame = ctk.CTkFrame(self.window)
        main_frame.grid(row=0, column=0, sticky='nsew', padx=20, pady=(20, 10))
        main_frame.grid_rowconfigure(0, weight=1)
        main_frame.grid_columnconfigure(0, weight=1)

        # Treeview for history
        tree_container = ctk.CTkFrame(main_frame, fg_color="transparent")
        tree_container.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)
        tree_container.grid_rowconfigure(0, weight=1)
        tree_container.grid_columnconfigure(0, weight=1)

        # Scrollbar
        tree_scroll = ttk.Scrollbar(tree_container)
        tree_scroll.grid(row=0, column=1, sticky='ns')

        # Style for dark mode
        style = ttk.Style()
        # Use 'clam' theme which allows heading customization (Windows default ignores it)
        style.theme_use('clam')
        style.configure("History.Treeview",
                       background="#2b2b2b",
                       foreground="white",
                       fieldbackground="#2b2b2b",
                       rowheight=28)
        style.configure("History.Treeview.Heading",
                       background="#1f6aa5",
                       foreground="white",
                       font=('Segoe UI', 10, 'bold'),
                       relief='flat')
        style.map("History.Treeview.Heading",
                 background=[('active', '#2980b9')])
        style.map("History.Treeview", background=[('selected', '#1f6aa5')])

        self.tree = ttk.Treeview(tree_container, columns=('time', 'type', 'text'),
                                 show='headings', yscrollcommand=tree_scroll.set,
                                 style="History.Treeview")
        self.tree.grid(row=0, column=0, sticky='nsew')
        tree_scroll.config(command=self.tree.yview)

        # Column headings
        self.tree.heading('time', text='Time')
        self.tree.heading('type', text='Type')
        self.tree.heading('text', text='Text')

        # Column widths
        self.tree.column('time', width=140, minwidth=120)
        self.tree.column('type', width=80, minwidth=60)
        self.tree.column('text', width=400, minwidth=200)

        # Populate history
        self.refresh_history()

        # Button frame
        btn_frame = ctk.CTkFrame(self.window, fg_color="transparent", height=60)
        btn_frame.grid(row=1, column=0, sticky='ew', padx=20, pady=(0, 20))
        btn_frame.grid_propagate(False)

        ctk.CTkButton(btn_frame, text="Copy Selected", width=120,
                     command=self.copy_selected).pack(side='left', padx=(0, 5), pady=10)
        ctk.CTkButton(btn_frame, text="Copy All", width=100,
                     command=self.copy_all).pack(side='left', padx=(0, 5), pady=10)
        ctk.CTkButton(btn_frame, text="Clear History", width=100,
                     fg_color="#cc4444", hover_color="#aa3333",
                     command=self.clear_history).pack(side='left', pady=10)

        ctk.CTkButton(btn_frame, text="Refresh", width=80,
                     fg_color="gray40", command=self.refresh_history).pack(side='right', padx=(5, 0), pady=10)
        ctk.CTkButton(btn_frame, text="Close", width=80,
                     fg_color="gray40", command=self.close).pack(side='right', pady=10)

        self.window.protocol("WM_DELETE_WINDOW", self.close)

    def refresh_history(self):
        """Refresh the history list"""
        # Clear existing
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Add history items (newest first)
        for timestamp, text, is_command in reversed(self.app.history):
            item_type = "Command" if is_command else "Dictation"
            # Truncate long text for display
            display_text = text if len(text) <= 80 else text[:77] + "..."
            self.tree.insert('', 'end', values=(timestamp, item_type, display_text),
                           tags=('command' if is_command else 'dictation',))

        # Style tags
        self.tree.tag_configure('command', foreground='#00CED1')
        self.tree.tag_configure('dictation', foreground='white')

    def copy_selected(self):
        """Copy selected item to clipboard"""
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("No Selection", "Please select an item to copy.", parent=self.window)
            return

        # Get full text from history (not truncated display text)
        item = self.tree.item(selection[0])
        index = self.tree.index(selection[0])
        # History is reversed in display, so we need to reverse index
        history_index = len(self.app.history) - 1 - index
        if 0 <= history_index < len(self.app.history):
            _, text, _ = self.app.history[history_index]
            pyperclip.copy(text)
            messagebox.showinfo("Copied", "Text copied to clipboard.", parent=self.window)

    def copy_all(self):
        """Copy all dictation text to clipboard"""
        texts = [text for _, text, is_cmd in self.app.history if not is_cmd]
        if texts:
            pyperclip.copy('\n'.join(texts))
            messagebox.showinfo("Copied", f"Copied {len(texts)} dictations to clipboard.", parent=self.window)
        else:
            messagebox.showinfo("Empty", "No dictation history to copy.", parent=self.window)

    def clear_history(self):
        """Clear all history"""
        if messagebox.askyesno("Clear History",
                              "Are you sure you want to clear all history?",
                              parent=self.window):
            self.app.history.clear()
            self.app.save_history()  # Save empty history to file
            self.refresh_history()

    def close(self):
        if self.window:
            try:
                self.window.destroy()
            except:
                pass
            finally:
                self.window = None


