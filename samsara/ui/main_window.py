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

Visuals follow the Samsara design system: teal accent on dark blue-tinted
surfaces. Tokens live at the top of this module.
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

# ---- Samsara design tokens ----------------------------------------------

COLOR_BG_DEEPEST     = "#0b0e14"   # window bg
COLOR_BG_SURFACE     = "#131820"   # sidebar, cards
COLOR_BG_ELEVATED    = "#1a2030"   # hover
COLOR_BG_HIGHLIGHT   = "#212838"   # selected, focused inputs
COLOR_BORDER         = "#2a3345"
COLOR_BORDER_ACCENT  = "#3a4a5a"

COLOR_TEXT_PRIMARY   = "#e4e8ef"
COLOR_TEXT_BODY      = "#c0c8d4"
COLOR_TEXT_SECONDARY = "#7a8599"
COLOR_TEXT_DISABLED  = "#4a5568"

COLOR_ACCENT         = "#5cc4d4"
COLOR_ACCENT_HOVER   = "#7ad4e2"
COLOR_ACCENT_DIM     = "#1a3a42"

COLOR_SUCCESS        = "#6ee7a0"
COLOR_ERROR          = "#f87171"
COLOR_WARNING        = "#fbbf24"

PAGE_MARGIN     = 20
SECTION_GAP     = 16
ELEMENT_GAP     = 8
INNER_PADDING   = 12
CARD_PADDING    = 16

SIDEBAR_WIDTH   = 200
NAV_ITEM_HEIGHT = 44
NAV_STRIPE_W    = 3
STATUS_BAR_H    = 28

FONT_FAMILY = "Segoe UI"


def _font_heading():
    # Spec: 18px / weight 600 -- CTk's font weight is "normal"/"bold"
    return ctk.CTkFont(family=FONT_FAMILY, size=18, weight="bold")


def _font_subheading():
    return ctk.CTkFont(family=FONT_FAMILY, size=14, weight="normal")


def _font_body():
    return ctk.CTkFont(family=FONT_FAMILY, size=13, weight="normal")


def _font_caption():
    return ctk.CTkFont(family=FONT_FAMILY, size=11, weight="normal")


def _font_nav():
    # Slightly heavier than body to read clearly inside 44px sidebar items
    return ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")


class MainWindow:
    """Hub window with sidebar nav. Singleton: show() reopens if already up."""

    NAV_ITEMS = ("History", "Dictionary", "Settings")

    def __init__(self, app):
        self.app = app
        self._toplevel = None
        self._content_frames = {}      # name -> CTkFrame
        self._nav_buttons = {}         # name -> CTkButton
        self._nav_stripes = {}         # name -> CTkFrame (left accent bar)
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

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        win = ctk.CTkToplevel(self.app.root)
        self._toplevel = win
        win.title("Samsara")
        try:
            win.configure(fg_color=COLOR_BG_DEEPEST)
        except Exception:
            pass
        try:
            print(f"[UI] Window bg: {win.cget('fg_color')}")
        except Exception as e:
            print(f"[UI] Could not read window bg: {e}")
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
        # Push the single new entry into the history frame if mounted.
        # Cheap path: prepends one card instead of full rebuild.
        history = self._content_frames.get("History")
        if history is not None:
            try:
                if hasattr(history, 'on_new_entry'):
                    history.on_new_entry()
                elif hasattr(history, 'refresh'):
                    history.refresh()
            except Exception:
                pass

    # ---- Internal: layout ------------------------------------------------

    def _build_header(self, parent):
        header = ctk.CTkFrame(parent, fg_color="transparent", height=48)
        header.grid(row=0, column=0, sticky='ew',
                    padx=PAGE_MARGIN, pady=(PAGE_MARGIN, ELEMENT_GAP))
        header.grid_propagate(False)

        ctk.CTkLabel(
            header, text="Samsara",
            font=_font_heading(),
            text_color=COLOR_TEXT_PRIMARY,
        ).pack(side='left', anchor='w')

        self._status_indicator = ctk.CTkLabel(
            header, text="...",
            text_color=COLOR_TEXT_SECONDARY,
            font=_font_caption(),
        )
        self._status_indicator.pack(side='right', anchor='e')

    def _build_body(self, parent):
        # Body fills the row 1 cell with no outer padding so the sidebar
        # reaches the window's left edge. Page margin is applied inside
        # the content host instead, leaving the sidebar surface flush.
        body = ctk.CTkFrame(parent, fg_color="transparent")
        body.grid(row=1, column=0, sticky='nsew', padx=0, pady=0)
        body.grid_columnconfigure(2, weight=1)   # content column expands
        body.grid_rowconfigure(0, weight=1)

        # Sidebar -- bg_surface, fixed 200px wide, square (no rounding),
        # no border (the separator column handles the right edge).
        sidebar = ctk.CTkFrame(
            body, width=SIDEBAR_WIDTH,
            fg_color=COLOR_BG_SURFACE, corner_radius=0,
            border_width=0,
        )
        sidebar.grid(row=0, column=0, sticky='ns')
        sidebar.grid_propagate(False)

        # Top spacer
        ctk.CTkFrame(sidebar, fg_color="transparent", height=ELEMENT_GAP) \
            .pack(fill='x')

        for name in self.NAV_ITEMS:
            # Each row is a 44px container with a 3px stripe on the left
            # and a CTkButton filling the rest. The stripe recolors to
            # accent for the active item.
            row = ctk.CTkFrame(
                sidebar, fg_color=COLOR_BG_SURFACE,
                height=NAV_ITEM_HEIGHT, corner_radius=0)
            row.pack(fill='x', pady=(0, 2))
            row.pack_propagate(False)

            stripe = ctk.CTkFrame(
                row, width=NAV_STRIPE_W,
                fg_color=COLOR_BG_SURFACE,  # invisible by default
                corner_radius=0)
            stripe.pack(side='left', fill='y')
            stripe.pack_propagate(False)
            self._nav_stripes[name] = stripe

            btn = ctk.CTkButton(
                row,
                text=name,
                anchor='w',
                height=NAV_ITEM_HEIGHT,
                corner_radius=0,
                fg_color=COLOR_BG_SURFACE,
                hover_color=COLOR_BG_ELEVATED,
                text_color=COLOR_TEXT_SECONDARY,
                font=_font_nav(),
                command=lambda n=name: self._activate(n),
            )
            # padx leaves 9px between stripe and text -> total 12px from
            # sidebar edge to glyph, matching the spec's 12px left padding.
            btn.pack(side='left', fill='both', expand=True,
                     padx=(9, INNER_PADDING))
            self._nav_buttons[name] = btn

        # 1px separator between sidebar and content area.
        sep = ctk.CTkFrame(body, width=1, fg_color=COLOR_BORDER,
                           corner_radius=0)
        sep.grid(row=0, column=1, sticky='ns')

        # Content area -- transparent so the window's #0b0e14 shows
        # through. No card, no border. Mounted frames are placed with
        # PAGE_MARGIN padding so content has 20px breathing room on
        # every side (including from the separator).
        self._content_host = ctk.CTkFrame(
            body, fg_color="transparent",
            corner_radius=0, border_width=0)
        self._content_host.grid(row=0, column=2, sticky='nsew')
        self._content_host.grid_rowconfigure(0, weight=1)
        self._content_host.grid_columnconfigure(0, weight=1)
        try:
            print(f"[UI] Sidebar bg: {sidebar.cget('fg_color')}, "
                  f"content host bg: {self._content_host.cget('fg_color')}")
        except Exception:
            pass

    def _build_status_bar(self, parent):
        # Wrap separator + bar in a single grid cell so layout stays simple.
        wrap = ctk.CTkFrame(parent, fg_color="transparent",
                            height=STATUS_BAR_H + 1)
        wrap.grid(row=2, column=0, sticky='ew')
        wrap.grid_propagate(False)

        sep = ctk.CTkFrame(wrap, fg_color=COLOR_BORDER, height=1,
                           corner_radius=0)
        sep.pack(side='top', fill='x')

        bar = ctk.CTkFrame(wrap, fg_color=COLOR_BG_SURFACE,
                           height=STATUS_BAR_H, corner_radius=0)
        bar.pack(side='top', fill='x')
        bar.pack_propagate(False)

        cap = _font_caption()
        # Inner row so we can pad once and pack labels into it.
        inner = ctk.CTkFrame(bar, fg_color="transparent")
        inner.pack(fill='both', expand=True,
                   padx=PAGE_MARGIN, pady=0)

        self._status_mode_label = ctk.CTkLabel(
            inner, text="mode: ...",
            text_color=COLOR_TEXT_SECONDARY, font=cap)
        self._status_mode_label.pack(side='left', padx=(0, INNER_PADDING))

        self._status_wake_label = ctk.CTkLabel(
            inner, text="wake: ...",
            text_color=COLOR_TEXT_SECONDARY, font=cap)
        self._status_wake_label.pack(side='left', padx=(0, INNER_PADDING))

        self._status_mic_label = ctk.CTkLabel(
            inner, text="mic: ...",
            text_color=COLOR_TEXT_SECONDARY, font=cap)
        self._status_mic_label.pack(side='left', padx=(0, INNER_PADDING))

        self._status_preview_label = ctk.CTkLabel(
            inner, text="",
            text_color=COLOR_TEXT_SECONDARY, font=cap,
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
            frame.grid(row=0, column=0, sticky='nsew',
                       padx=PAGE_MARGIN, pady=PAGE_MARGIN)
        except Exception:
            pass

        self._active_view = name
        self._highlight_nav(name)

    def _create_frame(self, name):
        # fg_color="transparent" makes the frame inherit the content host's
        # bg (which is itself transparent -> shows the window's #0b0e14).
        # Without this, CTkFrame paints its theme default and the content
        # area reads as a grey box.
        if name == "History":
            from samsara.ui.history_frame import HistoryFrame
            return HistoryFrame(
                self._content_host, self.app,
                is_visible=lambda: (self._is_alive()
                                    and self._active_view == "History"),
                fg_color="transparent",
            )
        if name == "Dictionary":
            from samsara.ui.dictionary_frame import DictionaryFrame
            return DictionaryFrame(
                self._content_host, self.app,
                fg_color="transparent",
            )
        return None

    def _highlight_nav(self, name):
        """Apply the active/inactive design tokens to each sidebar row."""
        for n, btn in self._nav_buttons.items():
            stripe = self._nav_stripes.get(n)
            if n == name:
                btn.configure(
                    fg_color=COLOR_ACCENT_DIM,
                    hover_color=COLOR_ACCENT_DIM,
                    text_color=COLOR_ACCENT,
                )
                if stripe is not None:
                    try:
                        stripe.configure(fg_color=COLOR_ACCENT)
                    except Exception:
                        pass
            else:
                btn.configure(
                    fg_color=COLOR_BG_SURFACE,
                    hover_color=COLOR_BG_ELEVATED,
                    text_color=COLOR_TEXT_SECONDARY,
                )
                if stripe is not None:
                    try:
                        stripe.configure(fg_color=COLOR_BG_SURFACE)
                    except Exception:
                        pass

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

        # Top-right indicator -- design-system semantic colors.
        if self._status_indicator is not None:
            if getattr(self.app, 'snoozed', False):
                self._status_indicator.configure(
                    text="snoozed", text_color=COLOR_WARNING)
            elif getattr(self.app, 'recording', False):
                self._status_indicator.configure(
                    text="recording", text_color=COLOR_ERROR)
            elif getattr(self.app, 'continuous_active', False) or \
                    getattr(self.app, 'wake_word_active', False):
                self._status_indicator.configure(
                    text="listening", text_color=COLOR_SUCCESS)
            else:
                self._status_indicator.configure(
                    text="ready", text_color=COLOR_TEXT_SECONDARY)

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
