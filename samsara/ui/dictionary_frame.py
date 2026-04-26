"""Reusable Dictionary editor frame.

Three sub-tabs:
  - Vocabulary: words injected into Whisper's initial_prompt. Backed by
    the VoiceTrainingWindow's custom_vocab list (via training_data.json).
  - Corrections: phonetic-wash overrides for command pipeline. Backed by
    samsara.phonetic_wash user JSON.
  - Wake Words: wake-word misrecognition map. Backed by
    samsara.wake_corrections user JSON.

Used by both the Voice Training window and the main hub window. UI talks
to services (app.voice_training_window for vocab, the corrections modules
for the rest). It does not open files or DBs directly.
"""

import json
import logging
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

logger = logging.getLogger(__name__)


class DictionaryFrame(ctk.CTkFrame):
    """Three-tab editor: Vocabulary / Corrections / Wake Words."""

    def __init__(self, parent, app, **kwargs):
        super().__init__(parent, **kwargs)
        self.app = app
        self._alive = True
        self._build_ui()

    def destroy(self):
        self._alive = False
        super().destroy()

    # ---- Service accessors (audit constraint: UI talks to services) ------

    @property
    def _vt(self):
        """The VoiceTrainingWindow instance -- owns the vocabulary list."""
        return getattr(self.app, 'voice_training_window', None)

    @property
    def _custom_vocab(self):
        vt = self._vt
        return vt.custom_vocab if vt is not None else []

    def _save_training_data(self):
        vt = self._vt
        if vt is not None:
            vt.save_training_data()

    def _sync_legacy_vocab_listbox(self):
        """If the legacy Vocabulary tab is open, keep its listbox in sync."""
        vt = self._vt
        if vt is None:
            return
        if hasattr(vt, 'vocab_listbox') and vt.vocab_listbox is not None:
            try:
                vt.refresh_vocab_list()
            except Exception:
                pass

    # ---- Layout ----------------------------------------------------------

    def _build_ui(self):
        ctk.CTkLabel(
            self, text="Dictionary",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(15, 4))
        ctk.CTkLabel(
            self,
            text="Custom vocabulary, command corrections, and wake-word fixes. "
                 "User entries take effect on the next dictation -- no restart needed.",
            text_color="gray", wraplength=620, justify='left',
        ).pack(anchor='w', pady=(0, 10))

        sub = ctk.CTkTabview(self, corner_radius=8)
        sub.pack(fill='both', expand=True)
        self.subtabs = sub
        sub.add("Vocabulary")
        sub.add("Corrections")
        sub.add("Wake Words")

        self._build_vocab_subtab(sub.tab("Vocabulary"))
        self._build_corrections_subtab(sub.tab("Corrections"))
        self._build_wake_subtab(sub.tab("Wake Words"))

    def _toplevel(self):
        """Return the Toplevel hosting this frame, for dialog parents."""
        try:
            return self.winfo_toplevel()
        except Exception:
            return None

    # ---- Vocabulary ------------------------------------------------------

    def _build_vocab_subtab(self, parent):
        ctk.CTkLabel(
            parent,
            text="Words injected into Whisper's initial_prompt. Add proper nouns, "
                 "technical terms, or anything Whisper consistently mishears.",
            text_color="gray", wraplength=600, justify='left',
        ).pack(anchor='w', pady=(8, 8))

        input_frame = ctk.CTkFrame(parent, fg_color="transparent")
        input_frame.pack(fill='x', pady=(0, 8))
        ctk.CTkLabel(input_frame, text="Word/Phrase:").pack(side='left')
        self._vocab_entry = ctk.CTkEntry(input_frame, width=280)
        self._vocab_entry.pack(side='left', padx=8)
        self._vocab_entry.bind('<Return>', lambda _e: self._vocab_add())
        ctk.CTkButton(input_frame, text="Add", width=70,
                      command=self._vocab_add).pack(side='left')

        list_frame = ctk.CTkFrame(parent, corner_radius=8)
        list_frame.pack(fill='both', expand=True, pady=(4, 8))
        listbox_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
        listbox_frame.pack(fill='both', expand=True, padx=10, pady=(10, 6))
        scrollbar = ttk.Scrollbar(listbox_frame)
        scrollbar.pack(side='right', fill='y')
        self._vocab_listbox = tk.Listbox(
            listbox_frame, yscrollcommand=scrollbar.set, height=10,
            bg='#333333', fg='white', selectbackground='#1f6aa5')
        self._vocab_listbox.pack(side='left', fill='both', expand=True)
        scrollbar.configure(command=self._vocab_listbox.yview)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill='x')
        ctk.CTkButton(btn_row, text="Remove Selected", width=130,
                      command=self._vocab_remove).pack(side='left', padx=(0, 6))
        ctk.CTkButton(btn_row, text="Import JSON", width=110, fg_color="gray40",
                      command=self._vocab_import).pack(side='left', padx=6)
        ctk.CTkButton(btn_row, text="Export JSON", width=110, fg_color="gray40",
                      command=self._vocab_export).pack(side='left', padx=6)

        self._refresh_vocab_list()

    def _refresh_vocab_list(self):
        try:
            self._vocab_listbox.delete(0, tk.END)
            for word in self._custom_vocab:
                self._vocab_listbox.insert(tk.END, word)
            self._sync_legacy_vocab_listbox()
        except Exception as e:
            logger.error(f"Error refreshing vocab list: {e}", exc_info=True)

    def _vocab_add(self):
        try:
            word = self._vocab_entry.get().strip()
            vocab = self._custom_vocab
            if word and word not in vocab:
                vocab.append(word)
                self._vocab_entry.delete(0, tk.END)
                self._save_training_data()
                self._refresh_vocab_list()
                logger.info(f"[DICT] Added vocab: {word}")
        except Exception as e:
            logger.error(f"Error adding vocab: {e}", exc_info=True)

    def _vocab_remove(self):
        try:
            sel = self._vocab_listbox.curselection()
            if not sel:
                return
            word = self._vocab_listbox.get(sel[0])
            vocab = self._custom_vocab
            if word in vocab:
                vocab.remove(word)
                self._save_training_data()
                self._refresh_vocab_list()
                logger.info(f"[DICT] Removed vocab: {word}")
        except Exception as e:
            logger.error(f"Error removing vocab: {e}", exc_info=True)

    def _vocab_export(self):
        try:
            path = filedialog.asksaveasfilename(
                parent=self._toplevel(),
                defaultextension=".json",
                filetypes=[("JSON files", "*.json")],
                initialfile="samsara-vocabulary.json",
            )
            if not path:
                return
            with open(path, 'w', encoding='utf-8') as f:
                json.dump({"vocabulary": list(self._custom_vocab)}, f, indent=2)
            messagebox.showinfo("Export", f"Exported {len(self._custom_vocab)} words.")
        except Exception as e:
            logger.error(f"Vocab export failed: {e}", exc_info=True)
            messagebox.showerror("Export failed", str(e))

    def _vocab_import(self):
        try:
            path = filedialog.askopenfilename(
                parent=self._toplevel(),
                filetypes=[("JSON files", "*.json")],
            )
            if not path:
                return
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            words = data.get('vocabulary') if isinstance(data, dict) else data
            if not isinstance(words, list):
                messagebox.showerror(
                    "Import failed",
                    "Expected JSON with a 'vocabulary' list of strings.")
                return
            vocab = self._custom_vocab
            added = 0
            for w in words:
                w = str(w).strip()
                if w and w not in vocab:
                    vocab.append(w)
                    added += 1
            self._save_training_data()
            self._refresh_vocab_list()
            messagebox.showinfo(
                "Import",
                f"Imported {added} new word(s) (skipped duplicates).")
        except Exception as e:
            logger.error(f"Vocab import failed: {e}", exc_info=True)
            messagebox.showerror("Import failed", str(e))

    # ---- Corrections (phonetic wash) ------------------------------------

    def _build_corrections_subtab(self, parent):
        ctk.CTkLabel(
            parent,
            text='Fixes Whisper misrecognitions of command phrases (e.g. "open crow" -> "open chrome"). '
                 "Multi-word entries match phrases; single words match tokens.",
            text_color="gray", wraplength=600, justify='left',
        ).pack(anchor='w', pady=(8, 8))

        input_frame = ctk.CTkFrame(parent, fg_color="transparent")
        input_frame.pack(fill='x', pady=(0, 8))
        ctk.CTkLabel(input_frame, text="Heard:").pack(side='left')
        self._corr_heard = ctk.CTkEntry(input_frame, width=180)
        self._corr_heard.pack(side='left', padx=(6, 8))
        ctk.CTkLabel(input_frame, text="->").pack(side='left')
        ctk.CTkLabel(input_frame, text="Should be:").pack(side='left', padx=(8, 0))
        self._corr_right = ctk.CTkEntry(input_frame, width=180)
        self._corr_right.pack(side='left', padx=(6, 8))
        self._corr_right.bind('<Return>', lambda _e: self._corr_add())
        ctk.CTkButton(input_frame, text="Add", width=70,
                      command=self._corr_add).pack(side='left')

        list_frame = ctk.CTkFrame(parent, corner_radius=8)
        list_frame.pack(fill='both', expand=True, pady=(4, 8))
        tree_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
        tree_frame.pack(fill='both', expand=True, padx=10, pady=(10, 6))
        scrollbar = ttk.Scrollbar(tree_frame)
        scrollbar.pack(side='right', fill='y')
        self._corr_tree = ttk.Treeview(
            tree_frame, columns=('heard', 'right', 'src'),
            show='headings', yscrollcommand=scrollbar.set, height=11)
        self._corr_tree.heading('heard', text='Heard')
        self._corr_tree.heading('right', text='Should be')
        self._corr_tree.heading('src', text='Source')
        self._corr_tree.column('heard', width=220)
        self._corr_tree.column('right', width=220)
        self._corr_tree.column('src', width=80, anchor='center')
        self._corr_tree.pack(side='left', fill='both', expand=True)
        scrollbar.configure(command=self._corr_tree.yview)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill='x')
        ctk.CTkButton(btn_row, text="Remove Selected (user only)", width=200,
                      command=self._corr_remove).pack(side='left', padx=(0, 6))
        ctk.CTkLabel(btn_row,
                     text="Defaults are read-only; remove user entries only.",
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).pack(side='left', padx=10)

        self._refresh_corr_tree()

    def _refresh_corr_tree(self):
        from samsara import phonetic_wash as pw
        try:
            for item in self._corr_tree.get_children():
                self._corr_tree.delete(item)
            user = pw.get_user_corrections()
            phrase_def = pw.get_default_phrase_corrections()
            word_def = pw.get_default_word_corrections()
            for k in sorted(user):
                self._corr_tree.insert('', tk.END, values=(k, user[k], 'user'))
            for k in sorted(phrase_def):
                if k in user:
                    continue
                self._corr_tree.insert('', tk.END, values=(k, phrase_def[k], 'default'))
            for k in sorted(word_def):
                if k in user:
                    continue
                self._corr_tree.insert('', tk.END, values=(k, word_def[k], 'default'))
        except Exception as e:
            logger.error(f"Error refreshing corrections tree: {e}", exc_info=True)

    def _corr_user_add(self, heard, should_be):
        """Programmatic helper for the Edit-to-Learn loop."""
        from samsara import phonetic_wash as pw

        def do_save():
            try:
                cur = pw.get_user_corrections()
                cur[heard.strip().lower()] = should_be.strip()
                pw.set_user_corrections(cur)
            except Exception as e:
                logger.error(f"Failed to save user correction: {e}", exc_info=True)
            if self._alive:
                try:
                    self.after(0, self._refresh_corr_tree)
                except Exception:
                    pass

        threading.Thread(target=do_save, daemon=True).start()

    def _corr_add(self):
        try:
            heard = self._corr_heard.get().strip()
            right = self._corr_right.get().strip()
            if not heard or not right:
                return
            self._corr_heard.delete(0, tk.END)
            self._corr_right.delete(0, tk.END)
            self._corr_user_add(heard, right)
            logger.info(f"[DICT] Added correction: '{heard}' -> '{right}'")
        except Exception as e:
            logger.error(f"Error adding correction: {e}", exc_info=True)

    def _corr_remove(self):
        from samsara import phonetic_wash as pw
        try:
            sel = self._corr_tree.selection()
            if not sel:
                return
            values = self._corr_tree.item(sel[0], 'values')
            if not values or values[2] != 'user':
                messagebox.showinfo(
                    "Read-only",
                    "Built-in defaults can't be removed from the UI. "
                    "Add a user override with the new mapping instead.")
                return
            heard = values[0]
            if not messagebox.askyesno(
                    "Remove correction",
                    f"Remove user correction '{heard}'?"):
                return

            def do_remove():
                try:
                    cur = pw.get_user_corrections()
                    cur.pop(heard, None)
                    pw.set_user_corrections(cur)
                except Exception as e:
                    logger.error(f"Failed to remove correction: {e}", exc_info=True)
                if self._alive:
                    try:
                        self.after(0, self._refresh_corr_tree)
                    except Exception:
                        pass

            threading.Thread(target=do_remove, daemon=True).start()
        except Exception as e:
            logger.error(f"Error removing correction: {e}", exc_info=True)

    # ---- Wake Words ------------------------------------------------------

    def _build_wake_subtab(self, parent):
        ctk.CTkLabel(
            parent,
            text="Maps Whisper misrecognitions of your wake phrase back to its canonical form. "
                 "Token-level: 'charvis' anywhere in the transcription becomes 'jarvis'.",
            text_color="gray", wraplength=600, justify='left',
        ).pack(anchor='w', pady=(8, 8))

        input_frame = ctk.CTkFrame(parent, fg_color="transparent")
        input_frame.pack(fill='x', pady=(0, 8))
        ctk.CTkLabel(input_frame, text="Heard:").pack(side='left')
        self._wake_heard = ctk.CTkEntry(input_frame, width=180)
        self._wake_heard.pack(side='left', padx=(6, 8))
        ctk.CTkLabel(input_frame, text="->").pack(side='left')
        ctk.CTkLabel(input_frame, text="Wake word:").pack(side='left', padx=(8, 0))
        self._wake_right = ctk.CTkEntry(input_frame, width=180)
        self._wake_right.pack(side='left', padx=(6, 8))
        self._wake_right.bind('<Return>', lambda _e: self._wake_add())
        ctk.CTkButton(input_frame, text="Add", width=70,
                      command=self._wake_add).pack(side='left')

        list_frame = ctk.CTkFrame(parent, corner_radius=8)
        list_frame.pack(fill='both', expand=True, pady=(4, 8))
        tree_frame = ctk.CTkFrame(list_frame, fg_color="transparent")
        tree_frame.pack(fill='both', expand=True, padx=10, pady=(10, 6))
        scrollbar = ttk.Scrollbar(tree_frame)
        scrollbar.pack(side='right', fill='y')
        self._wake_tree = ttk.Treeview(
            tree_frame, columns=('heard', 'right', 'src'),
            show='headings', yscrollcommand=scrollbar.set, height=11)
        self._wake_tree.heading('heard', text='Heard')
        self._wake_tree.heading('right', text='Wake word')
        self._wake_tree.heading('src', text='Source')
        self._wake_tree.column('heard', width=220)
        self._wake_tree.column('right', width=220)
        self._wake_tree.column('src', width=80, anchor='center')
        self._wake_tree.pack(side='left', fill='both', expand=True)
        scrollbar.configure(command=self._wake_tree.yview)

        btn_row = ctk.CTkFrame(parent, fg_color="transparent")
        btn_row.pack(fill='x')
        ctk.CTkButton(btn_row, text="Remove Selected (user only)", width=200,
                      command=self._wake_remove).pack(side='left', padx=(0, 6))
        ctk.CTkLabel(btn_row,
                     text="Defaults are read-only; remove user entries only.",
                     text_color="gray", font=ctk.CTkFont(size=11)
                     ).pack(side='left', padx=10)

        self._refresh_wake_tree()

    def _refresh_wake_tree(self):
        from samsara import wake_corrections as wc
        try:
            for item in self._wake_tree.get_children():
                self._wake_tree.delete(item)
            user = wc.get_user_corrections()
            defaults = wc.get_default_corrections()
            for k in sorted(user):
                self._wake_tree.insert('', tk.END, values=(k, user[k], 'user'))
            for k in sorted(defaults):
                if k in user:
                    continue
                self._wake_tree.insert('', tk.END, values=(k, defaults[k], 'default'))
        except Exception as e:
            logger.error(f"Error refreshing wake tree: {e}", exc_info=True)

    def _wake_add(self):
        from samsara import wake_corrections as wc
        try:
            heard = self._wake_heard.get().strip().lower()
            right = self._wake_right.get().strip().lower()
            if not heard or not right:
                return
            self._wake_heard.delete(0, tk.END)
            self._wake_right.delete(0, tk.END)

            def do_save():
                try:
                    cur = wc.get_user_corrections()
                    cur[heard] = right
                    wc.set_user_corrections(cur)
                except Exception as e:
                    logger.error(f"Failed to save wake correction: {e}", exc_info=True)
                if self._alive:
                    try:
                        self.after(0, self._refresh_wake_tree)
                    except Exception:
                        pass

            threading.Thread(target=do_save, daemon=True).start()
            logger.info(f"[DICT] Added wake correction: '{heard}' -> '{right}'")
        except Exception as e:
            logger.error(f"Error adding wake correction: {e}", exc_info=True)

    def _wake_remove(self):
        from samsara import wake_corrections as wc
        try:
            sel = self._wake_tree.selection()
            if not sel:
                return
            values = self._wake_tree.item(sel[0], 'values')
            if not values or values[2] != 'user':
                messagebox.showinfo(
                    "Read-only",
                    "Built-in defaults can't be removed from the UI. "
                    "Add a user override with a different mapping instead.")
                return
            heard = values[0]
            if not messagebox.askyesno(
                    "Remove correction",
                    f"Remove user wake correction '{heard}'?"):
                return

            def do_remove():
                try:
                    cur = wc.get_user_corrections()
                    cur.pop(heard, None)
                    wc.set_user_corrections(cur)
                except Exception as e:
                    logger.error(f"Failed to remove wake correction: {e}", exc_info=True)
                if self._alive:
                    try:
                        self.after(0, self._refresh_wake_tree)
                    except Exception:
                        pass

            threading.Thread(target=do_remove, daemon=True).start()
        except Exception as e:
            logger.error(f"Error removing wake correction: {e}", exc_info=True)
