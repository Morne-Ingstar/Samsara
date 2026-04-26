"""The main hub window. Opens on launch, primary entry point.

Layout:
    +------------------------------------------------------+
    | Title: Samsara              [status indicator]       |
    +-------------+----------------------------------------+
    | History     |                                        |
    | Dictionary  |       (active frame goes here)         |
    | Settings    |                                        |
    +-------------+----------------------------------------+
    | mode | wake | mic | last transcription preview       |
    +------------------------------------------------------+

Tabs swap reusable CTkFrame subclasses (HistoryFrame, DictionaryFrame).
The Settings tab opens the existing SettingsWindow Toplevel -- a future
SettingsFrame extraction is its own task (settings_window.py is 2300
lines of tightly-coupled tab builders that don't lift cleanly).

Lifecycle: the close button minimizes to tray (the parent app rebinds
WM_DELETE_WINDOW). Position + size persist to config across restarts.

Status updates use a hybrid model:
  - Mode / mic / wake-state: polled every 2s.
  - Last transcription: pushed via on_dictation_complete() from the
    transcription paths -- never polled.
"""

import logging
import tkinter as tk

import customtkinter as ctk

logger = logging.getLogger(__name__)

DEFAULT_WIDTH = 900
DEFAULT_HEIGHT = 650
MIN_WIDTH = 700
MIN_HEIGHT = 500
STATUS_POLL_MS = 2000
PREVIEW_CHARS = 40


class MainWindow:
    """Hub window with sidebar nav. Singleton: show() reopens if already up."""

    NAV_ITEMS = ("History", "Dictionary", "Settings")

    def __init__(self, app):
        self.app = app
        self._toplevel = None
        self._content_frames = {}      # name -> CTkFrame
        self._nav_buttons = {}         # name -> CTkButton
        self._active_view = None
        self._poll_after_id = None

        # Status bar widgets (created on first show)
        self._status_mode_label = None
        self._status_wake_label = None
        self._status_mic_label = None
        self._status_preview_label = None
        self._status_indicator = None

    # ---- Public API ------------------------------------------------------

    def show(self):
        """Open the window. Singleton: deiconify + focus if already up."""
        if self._toplevel is not None and self._is_alive():
            try:
                self._toplevel.deiconify()
                self._toplevel.lift()
                self._toplevel.focus_force()
                return
            except Exception:
                self._toplevel = None

        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")

        win = ctk.CTkToplevel(self.app.root)
        self._toplevel = win
        win.title("Samsara")
        self._restore_geometry(win)
        win.minsize(MIN_WIDTH, MIN_HEIGHT)

        # Apply the Samsara window icon (taskbar + top-left). CTkToplevel
        # races with its own default icon ~200ms after construction, so
        # we apply now AND defer a second pass to win that race.
        if hasattr(self.app, '_apply_window_icon'):
            self.app._apply_window_icon(win)
            try:
                win.after(300, lambda: self.app._apply_window_icon(win))
            except Exception:
                pass

        # Hide while building so users don't see incremental layout
        win.withdraw()

        win.grid_rowconfigure(1, weight=1)   # row 0 = header, 1 = body, 2 = status
        win.grid_columnconfigure(0, weight=1)

        self._build_header(win)
        self._build_body(win)
        self._build_status_bar(win)

        # Open History first (most-used)
        self._activate("History")

        # Persist geometry on close & resize
        win.bind('<Configure>', self._on_configure)
        # Note: WM_DELETE_WINDOW is bound by the app (minimize-to-tray).

        win.deiconify()
        win.lift()
        win.focus_force()
        win.after(100, lambda: win.lift())

        # Start status polling
        self._schedule_poll()

    def hide(self):
        """Hide the window without destroying it (used by minimize-to-tray)."""
        if self._toplevel is not None and self._is_alive():
            try:
                self._save_geometry()
                self._toplevel.withdraw()
            except Exception:
                pass

    def close(self):
        """Tear down the window completely (called on app shutdown)."""
        self._cancel_poll()
        if self._toplevel is not None:
            try:
                self._save_geometry()
                self._toplevel.destroy()
            except Exception:
                pass
            self._toplevel = None

    def on_dictation_complete(self, text):
        """Called by the transcription paths after a successful dictation.

        Updates the status preview without polling, and refreshes the
        history view if it's currently visible.
        """
        if not self._is_alive():
            return
        try:
            self._toplevel.after(0, self._apply_dictation_update, text)
        except Exception:
            pass

    def _apply_dictation_update(self, text):
        if self._status_preview_label is not None:
            preview = (text or "").replace('\n', ' ').strip()
            if len(preview) > PREVIEW_CHARS:
                preview = preview[:PREVIEW_CHARS - 1] + '...'
            self._status_preview_label.configure(
                text=f"Last: {preview}" if preview else "")
        # Push refresh into the history frame if mounted
        history = self._content_frames.get("History")
        if history is not None and hasattr(history, 'refresh'):
            try:
                history.refresh()
            except Exception:
                pass

    # ---- Internal: layout ------------------------------------------------

    def _build_header(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent", height=48)
        header.grid(row=0, column=0, sticky='ew', padx=16, pady=(12, 4))
        header.grid_propagate(False)

        ctk.CTkLabel(
            header, text="Samsara",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side='left')

        self._status_indicator = ctk.CTkLabel(
            header, text="...",
            text_color="gray", font=ctk.CTkFont(size=12),
        )
        self._status_indicator.pack(side='right')

    def _build_body(self, parent):
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky='nsew', padx=16, pady=4)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # Sidebar
        sidebar = ctk.CTkFrame(body, width=160, corner_radius=10)
        sidebar.grid(row=0, column=0, sticky='ns', padx=(0, 12))
        sidebar.grid_propagate(False)

        ctk.CTkLabel(
            sidebar, text="", height=8,
        ).pack()  # top spacer

        for name in self.NAV_ITEMS:
            btn = ctk.CTkButton(
                sidebar, text=name, width=140, height=36,
                anchor='w',
                fg_color="gray25", hover_color="gray35",
                command=lambda n=name: self._activate(n),
            )
            btn.pack(padx=10, pady=(4, 0))
            self._nav_buttons[name] = btn

        # Content area
        self._content_host = ctk.CTkFrame(body, corner_radius=10)
        self._content_host.grid(row=0, column=1, sticky='nsew')
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)

    def _build_status_bar(self, parent):
        bar = ctk.CTkFrame(parent, fg_color="transparent", height=32)
        bar.grid(row=2, column=0, sticky='ew', padx=16, pady=(4, 12))
        bar.grid_propagate(False)

        small = ctk.CTkFont(size=11)
        self._status_mode_label = ctk.CTkLabel(
            bar, text="mode: ...", text_color="gray", font=small)
        self._status_mode_label.pack(side='left', padx=(0, 12))

        self._status_wake_label = ctk.CTkLabel(
            bar, text="wake: ...", text_color="gray", font=small)
        self._status_wake_label.pack(side='left', padx=(0, 12))

        self._status_mic_label = ctk.CTkLabel(
            bar, text="mic: ...", text_color="gray", font=small)
        self._status_mic_label.pack(side='left', padx=(0, 12))

        self._status_preview_label = ctk.CTkLabel(
            bar, text="", text_color="gray", font=small,
            anchor='e', justify='right')
        self._status_preview_label.pack(side='right')

    # ---- Internal: navigation -------------------------------------------

    def _activate(self, name):
        """Switch the content area to the given view."""
        if name == "Settings":
            # Settings stays as a Toplevel for now -- 2300 lines of
            # tightly-coupled tab builders to extract is its own task.
            try:
                if hasattr(self.app, 'settings_window') and self.app.settings_window:
                    self.app.settings_window.show()
            except Exception as e:
                logger.error(f"Failed to open settings: {e}", exc_info=True)
            self._highlight_nav(name)
            return

        # Lazy-mount frame the first time it's requested
        frame = self._content_frames.get(name)
        if frame is None:
            frame = self._create_frame(name)
            if frame is None:
                return
            self._content_frames[name] = frame

        # Hide other frames, show this one
        for n, f in self._content_frames.items():
            if n != name:
                try:
                    f.grid_forget()
                except Exception:
                    pass
        try:
            frame.grid(row=0, column=0, sticky='nsew', padx=10, pady=10)
        except Exception:
            pass

        self._active_view = name
        self._highlight_nav(name)

    def _create_frame(self, name):
        if name == "History":
            from samsara.ui.history_frame import HistoryFrame
            return HistoryFrame(
                self._content_host, self.app,
                is_visible=lambda: (self._is_alive()
                                    and self._active_view == "History"),
            )
        if name == "Dictionary":
            from samsara.ui.dictionary_frame import DictionaryFrame
            return DictionaryFrame(self._content_host, self.app)
        return None

    def _highlight_nav(self, name):
        for n, btn in self._nav_buttons.items():
            if n == name:
                btn.configure(fg_color=("#1f6aa5", "#1f6aa5"))
            else:
                btn.configure(fg_color="gray25")

    # ---- Internal: status polling ---------------------------------------

    def _schedule_poll(self):
        if not self._is_alive():
            return
        self._refresh_status()
        try:
            self._poll_after_id = self._toplevel.after(
                STATUS_POLL_MS, self._schedule_poll)
        except Exception:
            self._poll_after_id = None

    def _cancel_poll(self):
        if self._poll_after_id is not None and self._is_alive():
            try:
                self._toplevel.after_cancel(self._poll_after_id)
            except Exception:
                pass
        self._poll_after_id = None

    def _refresh_status(self):
        """Pull mode / wake / mic from the app config and reflect in status bar."""
        if not self._is_alive():
            return
        cfg = getattr(self.app, 'config', {}) or {}
        mode = cfg.get('mode', 'hold').title()
        if self._status_mode_label is not None:
            self._status_mode_label.configure(text=f"mode: {mode}")

        wake_enabled = cfg.get('wake_word_enabled', False)
        wake_phrase = cfg.get('wake_word_config', {}).get('phrase', 'samsara')
        wake_text = (f"wake: {wake_phrase} (on)" if wake_enabled
                     else "wake: off")
        if self._status_wake_label is not None:
            self._status_wake_label.configure(text=wake_text)

        # Mic name lookup -- avoid blocking; just match by id.
        mic_id = cfg.get('microphone')
        mic_name = "default"
        for m in getattr(self.app, 'available_mics', []) or []:
            if m.get('id') == mic_id:
                mic_name = m.get('name', 'default')
                break
        if len(mic_name) > 36:
            mic_name = mic_name[:35] + '...'
        if self._status_mic_label is not None:
            self._status_mic_label.configure(text=f"mic: {mic_name}")

        # Top-right indicator
        if self._status_indicator is not None:
            if getattr(self.app, 'snoozed', False):
                self._status_indicator.configure(
                    text="snoozed", text_color="#e2a555")
            elif getattr(self.app, 'recording', False):
                self._status_indicator.configure(
                    text="recording", text_color="#e25555")
            elif getattr(self.app, 'continuous_active', False) or \
                    getattr(self.app, 'wake_word_active', False):
                self._status_indicator.configure(
                    text="listening", text_color="#3ad26a")
            else:
                self._status_indicator.configure(
                    text="ready", text_color="gray")

    # ---- Internal: geometry persistence ---------------------------------

    def _restore_geometry(self, win):
        cfg = getattr(self.app, 'config', {}) or {}
        w = int(cfg.get('window_width', DEFAULT_WIDTH) or DEFAULT_WIDTH)
        h = int(cfg.get('window_height', DEFAULT_HEIGHT) or DEFAULT_HEIGHT)
        x = cfg.get('window_x')
        y = cfg.get('window_y')
        w = max(MIN_WIDTH, w)
        h = max(MIN_HEIGHT, h)
        if x is not None and y is not None:
            try:
                win.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
                return
            except Exception:
                pass
        win.geometry(f"{w}x{h}")

    def _save_geometry(self):
        if not self._is_alive():
            return
        try:
            geom = self._toplevel.geometry()  # "WIDTHxHEIGHT+X+Y"
            size, _, pos = geom.partition('+')
            w_str, _, h_str = size.partition('x')
            w = int(w_str)
            h = int(h_str)
            x_str, _, y_str = pos.partition('+')
            cfg = self.app.config
            cfg['window_width'] = w
            cfg['window_height'] = h
            if x_str.lstrip('-').isdigit() and y_str.lstrip('-').isdigit():
                cfg['window_x'] = int(x_str)
                cfg['window_y'] = int(y_str)
            self.app.save_config()
        except Exception as e:
            logger.error(f"Failed to save window geometry: {e}")

    def _on_configure(self, event):
        # Throttle: only persist on the toplevel resizing
        if event.widget is not self._toplevel:
            return
        # Debounce a bit to avoid hammering save_config on drag
        if hasattr(self, '_geom_save_id') and self._geom_save_id:
            try:
                self._toplevel.after_cancel(self._geom_save_id)
            except Exception:
                pass
        try:
            self._geom_save_id = self._toplevel.after(800, self._save_geometry)
        except Exception:
            pass

    # ---- Internal: util --------------------------------------------------

    def _is_alive(self):
        if self._toplevel is None:
            return False
        try:
            return bool(self._toplevel.winfo_exists())
        except Exception:
            return False
