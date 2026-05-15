"""
Samsara Command Cheat Sheet

A floating, always-on-top command reference window. Resizable by dragging
edges/corners, repositionable by dragging the title bar, filterable by phrase.
Commands can be pinned to the top and executed by clicking (300ms teal flash
as visual confirmation). State persists to command_palette.json.
"""

import json
import tkinter as tk
from pathlib import Path
from typing import Callable, List, Optional

_BG = "#0b0e14"
_SURFACE = "#131820"
_ELEVATED = "#1a2030"
_ACCENT = "#5cc4d4"
_ACCENT_DIM = "#1a3a42"
_TEXT_PRI = "#e4e8ef"
_TEXT_SEC = "#7a8599"
_BORDER = "#2a3345"
_FONT = "Segoe UI"

_DEFAULT_W = 440
_DEFAULT_H = 520
_MIN_W = 280
_MIN_H = 180
_TWO_COL_THRESHOLD = 480
_TITLE_H = 34
_FILTER_H = 32
_ROW_H = 28
_PAD_X = 10
_RESIZE_M = 8
_TOPMOST_MS = 3000
_FLASH_MS = 300


class CommandCheatSheet:
    """Always-on-top floating command reference panel."""

    def __init__(
        self,
        root: tk.Misc,
        execute_cb: Callable[[str], None],
        commands_cb: Callable[[], List[dict]],
        palette_path: Path,
    ):
        self._root = root
        self._execute_cb = execute_cb
        self._commands_cb = commands_cb
        self._palette_path = palette_path

        self._win: Optional[tk.Toplevel] = None
        self._visible = False
        self._pinned: set = set()
        self._all: List[dict] = []
        self._rows: List[dict] = []  # {phrase, pack, frame, pinbtn, label}
        self._two_col = False
        self._topmost_id = None
        self._flash_id = None
        self._filter_var: Optional[tk.StringVar] = None
        self._inner: Optional[tk.Frame] = None
        self._canvas: Optional[tk.Canvas] = None
        self._canvas_win_id = None
        self._resize_data: dict = {}
        self._drag_data: dict = {}
        self._opacity: float = 0.85
        self._opacity_var: Optional[tk.IntVar] = None
        self._geom = {"x": None, "y": None, "w": _DEFAULT_W, "h": _DEFAULT_H}

        self._load_palette()  # sets self._opacity from saved data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self):
        if self._visible and self._win is not None:
            try:
                self._win.deiconify()
                self._win.lift()
                self._win.attributes("-topmost", True)
                return
            except tk.TclError:
                self._win = None
        self._create_window()
        self._refresh_commands()
        self._visible = True
        self._start_topmost_loop()

    def hide(self):
        self._stop_topmost_loop()
        if self._win is not None:
            try:
                self._win.withdraw()
            except tk.TclError:
                self._win = None
        self._visible = False

    def toggle(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    def destroy(self):
        self._stop_topmost_loop()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None
        self._visible = False

    def refresh(self):
        """Reload command list (call after pack enable/disable changes)."""
        if self._visible and self._win is not None:
            self._refresh_commands()

    # ------------------------------------------------------------------
    # Window creation
    # ------------------------------------------------------------------

    def _create_window(self):
        self._all = self._commands_cb()

        self._win = tk.Toplevel(self._root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", self._opacity)
        self._win.configure(bg=_BORDER)
        self._win.minsize(_MIN_W, _MIN_H)

        w = self._geom["w"]
        h = self._geom["h"]
        if self._geom["x"] is None:
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = sw - w - 40
            y = (sh - h) // 2
        else:
            x, y = self._geom["x"], self._geom["y"]
        self._win.geometry(f"{w}x{h}+{x}+{y}")

        # Outer frame (1px border via BG_BORDER on win background)
        outer = tk.Frame(self._win, bg=_BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        # Title bar
        self._build_title_bar(outer)

        # Filter bar
        self._build_filter_bar(outer)

        # Separator
        tk.Frame(outer, bg=_BORDER, height=1).pack(fill="x")

        # Scrollable command list
        self._build_list(outer)

        # Resize grips (placed over everything, handled by the outer tk.Frame)
        self._install_resize_grips(self._win)

        self._win.bind("<Configure>", self._on_configure)

    def _build_title_bar(self, parent: tk.Frame):
        bar = tk.Frame(parent, bg=_SURFACE, height=_TITLE_H)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        # Drag binding on the bar itself
        bar.bind("<ButtonPress-1>", self._on_drag_start)
        bar.bind("<B1-Motion>", self._on_drag_move)
        bar.bind("<ButtonRelease-1>", self._on_drag_end)

        title_lbl = tk.Label(
            bar, text="Command Reference",
            bg=_SURFACE, fg=_TEXT_SEC,
            font=(_FONT, 9), anchor="w",
            cursor="fleur",
        )
        title_lbl.pack(side="left", padx=_PAD_X, pady=4, fill="x", expand=True)
        title_lbl.bind("<ButtonPress-1>", self._on_drag_start)
        title_lbl.bind("<B1-Motion>", self._on_drag_move)
        title_lbl.bind("<ButtonRelease-1>", self._on_drag_end)

        close_btn = tk.Label(
            bar, text="  x  ",
            bg=_SURFACE, fg=_TEXT_SEC,
            font=(_FONT, 9), cursor="hand2",
        )
        close_btn.pack(side="right", padx=4)
        close_btn.bind("<Button-1>", lambda _e: self.hide())
        close_btn.bind("<Enter>", lambda _e: close_btn.configure(fg="#e06060"))
        close_btn.bind("<Leave>", lambda _e: close_btn.configure(fg=_TEXT_SEC))

        # Opacity slider
        opacity_lbl = tk.Label(
            bar, text="opacity",
            bg=_SURFACE, fg=_TEXT_SEC,
            font=(_FONT, 8),
        )
        opacity_lbl.pack(side="right", padx=(0, 2))

        self._opacity_var = tk.IntVar(value=int(self._opacity * 100))
        opacity_slider = tk.Scale(
            bar,
            variable=self._opacity_var,
            from_=20, to=100,
            orient="horizontal",
            length=70,
            showvalue=False,
            bg=_SURFACE, fg=_TEXT_SEC,
            troughcolor=_BG,
            highlightthickness=0,
            bd=0,
            sliderlength=12,
            width=8,
        )
        opacity_slider.pack(side="right", padx=(0, 4))
        opacity_slider.bind("<Motion>",
            lambda _e: self._win.attributes("-alpha", self._opacity_var.get() / 100))
        opacity_slider.bind("<ButtonRelease-1>",
            lambda _e: self._on_opacity_release())

    def _on_opacity_release(self):
        """Save opacity after slider interaction ends."""
        self._opacity = self._opacity_var.get() / 100
        self._save_palette()

    def _build_filter_bar(self, parent: tk.Frame):
        bar = tk.Frame(parent, bg=_BG, height=_FILTER_H)
        bar.pack(fill="x", padx=_PAD_X, pady=(6, 4))
        bar.pack_propagate(False)

        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", self._on_filter_change)

        entry = tk.Entry(
            bar,
            textvariable=self._filter_var,
            bg=_SURFACE, fg=_TEXT_PRI,
            insertbackground=_ACCENT,
            relief="flat",
            font=(_FONT, 9),
            bd=0,
        )
        entry.pack(fill="both", expand=True, ipady=5, padx=1, pady=1)

        # Placeholder logic
        def _show_placeholder(e=None):
            if not self._filter_var.get():
                entry.configure(fg=_TEXT_SEC)
                entry.delete(0, "end")
                entry.insert(0, "Filter commands...")

        def _clear_placeholder(e=None):
            if entry.get() == "Filter commands...":
                entry.delete(0, "end")
                entry.configure(fg=_TEXT_PRI)

        entry.bind("<FocusIn>", _clear_placeholder)
        entry.bind("<FocusOut>", _show_placeholder)
        _show_placeholder()

        # Border frame around the entry
        entry_border = tk.Frame(bar, bg=_BORDER)
        entry_border.place(x=0, y=0, relwidth=1, relheight=1)
        entry.lift()

    def _build_list(self, parent: tk.Frame):
        container = tk.Frame(parent, bg=_SURFACE)
        container.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(container, bg=_SURFACE, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=self._canvas.yview)
        self._canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)

        self._inner = tk.Frame(self._canvas, bg=_SURFACE)
        self._canvas_win_id = self._canvas.create_window(
            (0, 0), window=self._inner, anchor="nw"
        )
        self._inner.bind(
            "<Configure>",
            lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        )
        self._canvas.bind(
            "<Configure>",
            lambda e: self._canvas.itemconfig(self._canvas_win_id, width=e.width)
        )

        # Mouse wheel scrolling
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._inner.bind("<MouseWheel>", self._on_mousewheel)

    # ------------------------------------------------------------------
    # Command list rendering
    # ------------------------------------------------------------------

    def _refresh_commands(self):
        self._all = self._commands_cb()
        self._apply_filter()

    def _on_filter_change(self, *_args):
        self._apply_filter()

    def _apply_filter(self):
        raw = (self._filter_var.get() if self._filter_var else "").strip().lower()
        if raw == "filter commands...":
            raw = ""

        if raw:
            filtered = [
                c for c in self._all
                if raw in c["phrase"]
                or any(raw in a for a in c.get("aliases", []))
            ]
        else:
            filtered = list(self._all)

        # Pinned first
        pinned = [c for c in filtered if c["phrase"] in self._pinned]
        unpinned = [c for c in filtered if c["phrase"] not in self._pinned]
        ordered = pinned + unpinned

        MAX_ROWS = 80
        self._total_count = len(ordered)
        self._render_rows(ordered[:MAX_ROWS])

    def _render_rows(self, commands: List[dict]):
        if self._inner is None:
            return

        # Destroy existing rows
        for child in self._inner.winfo_children():
            child.destroy()
        self._rows = []

        w = self._win.winfo_width() if self._win else _DEFAULT_W
        two_col = w >= _TWO_COL_THRESHOLD
        self._two_col = two_col

        if two_col:
            self._inner.columnconfigure(0, weight=1, uniform="col")
            self._inner.columnconfigure(1, weight=1, uniform="col")
            for i, cmd in enumerate(commands):
                row, col = divmod(i, 2)
                cell = self._make_row(self._inner, cmd)
                cell.grid(row=row, column=col, sticky="ew", padx=1, pady=1)
        else:
            self._inner.columnconfigure(0, weight=1)
            for i, cmd in enumerate(commands):
                cell = self._make_row(self._inner, cmd)
                cell.grid(row=i, column=0, sticky="ew", padx=0, pady=1)

    def _make_row(self, parent: tk.Frame, cmd: dict) -> tk.Frame:
        phrase = cmd["phrase"]
        pack = cmd.get("pack", "")
        pinned = phrase in self._pinned

        cell = tk.Frame(parent, bg=_SURFACE, height=_ROW_H, cursor="hand2")
        cell.pack_propagate(False)
        cell.grid_propagate(False)

        # Pin indicator
        pin_text = "*" if pinned else " "
        pin_lbl = tk.Label(
            cell, text=pin_text,
            bg=_SURFACE, fg=_ACCENT if pinned else _TEXT_SEC,
            font=(_FONT, 9, "bold"), width=2, cursor="hand2",
        )
        pin_lbl.place(relx=0, rely=0.5, x=4, anchor="w")

        # Command phrase
        display = phrase.title()
        phrase_lbl = tk.Label(
            cell, text=display,
            bg=_SURFACE, fg=_TEXT_PRI,
            font=(_FONT, 9), anchor="w",
            cursor="hand2",
        )
        phrase_lbl.place(relx=0, rely=0.5, x=22, anchor="w")

        # Pack label (right-aligned)
        if pack and pack != "core":
            pack_lbl = tk.Label(
                cell, text=pack,
                bg=_SURFACE, fg=_TEXT_SEC,
                font=(_FONT, 8), anchor="e",
                cursor="hand2",
            )
            pack_lbl.place(relx=1, rely=0.5, x=-8, anchor="e")

        row_info = {
            "phrase": phrase,
            "cell": cell,
            "pin_lbl": pin_lbl,
            "phrase_lbl": phrase_lbl,
        }
        self._rows.append(row_info)

        # Click on pin indicator toggles pin
        def _toggle_pin(e, p=phrase):
            self._toggle_pin(p)

        pin_lbl.bind("<Button-1>", _toggle_pin)

        # Click on the rest of the row executes command
        def _click(e, p=phrase, r=row_info):
            self._flash_and_execute(r, p)

        for widget in (cell, phrase_lbl):
            widget.bind("<Button-1>", _click)
            widget.bind("<Enter>", lambda e, c=cell: c.configure(bg=_ELEVATED))
            widget.bind("<Leave>", lambda e, c=cell, r=row_info: self._restore_row_bg(c, r))

        return cell

    def _restore_row_bg(self, cell: tk.Frame, row_info: dict):
        # Don't restore if we're mid-flash
        if self._flash_id is not None:
            return
        cell.configure(bg=_SURFACE)
        for child in cell.winfo_children():
            try:
                child.configure(bg=_SURFACE)
            except tk.TclError:
                pass

    def _flash_and_execute(self, row_info: dict, phrase: str):
        cell = row_info["cell"]

        def _set_bg(color):
            try:
                cell.configure(bg=color)
                for child in cell.winfo_children():
                    try:
                        child.configure(bg=color)
                    except tk.TclError:
                        pass
            except tk.TclError:
                pass

        _set_bg(_ACCENT_DIM)
        if self._flash_id is not None:
            try:
                self._root.after_cancel(self._flash_id)
            except Exception:
                pass

        self._flash_id = self._root.after(_FLASH_MS, lambda: (
            _set_bg(_SURFACE),
            setattr(self, "_flash_id", None),
        ))

        try:
            self._execute_cb(phrase)
        except Exception as e:
            print(f"[CHEATSHEET] Execute failed for '{phrase}': {e}")

    def _toggle_pin(self, phrase: str):
        if phrase in self._pinned:
            self._pinned.discard(phrase)
        else:
            self._pinned.add(phrase)
        self._save_palette()
        self._apply_filter()

    # ------------------------------------------------------------------
    # Resize grips
    # ------------------------------------------------------------------

    def _install_resize_grips(self, win: tk.Toplevel):
        m = _RESIZE_M

        grips = [
            ("n",  dict(relx=0, rely=0,   relwidth=1,    height=m, width=0), "size_ns",    ("n",)),
            ("s",  dict(relx=0, rely=1,   relwidth=1,    height=m, width=0, y=-m), "size_ns", ("s",)),
            ("w",  dict(relx=0, rely=0,   relheight=1,   width=m,  height=0), "size_we",   ("w",)),
            ("e",  dict(relx=1, rely=0,   relheight=1,   width=m,  height=0, x=-m), "size_we", ("e",)),
            ("nw", dict(relx=0, rely=0,   width=m*2,     height=m*2), "size_nw_se",        ("n", "w")),
            ("ne", dict(relx=1, rely=0,   width=m*2,     height=m*2, x=-m*2), "size_ne_sw", ("n", "e")),
            ("sw", dict(relx=0, rely=1,   width=m*2,     height=m*2, y=-m*2), "size_ne_sw", ("s", "w")),
            ("se", dict(relx=1, rely=1,   width=m*2,     height=m*2, x=-m*2, y=-m*2), "size_nw_se", ("s", "e")),
        ]

        for name, place_kw, cursor, edges in grips:
            grip = tk.Frame(win, bg=_BG, cursor=cursor)
            grip.place(**place_kw)
            grip.lift()

            def _press(e, ed=edges):
                self._resize_data = {
                    "edges": ed,
                    "x0": e.x_root,
                    "y0": e.y_root,
                    "wx": win.winfo_x(),
                    "wy": win.winfo_y(),
                    "ww": win.winfo_width(),
                    "wh": win.winfo_height(),
                }

            def _drag(e):
                rd = self._resize_data
                if not rd:
                    return
                dx = e.x_root - rd["x0"]
                dy = e.y_root - rd["y0"]
                x, y = rd["wx"], rd["wy"]
                w, h = rd["ww"], rd["wh"]

                if "e" in rd["edges"]:
                    w = max(_MIN_W, w + dx)
                if "s" in rd["edges"]:
                    h = max(_MIN_H, h + dy)
                if "w" in rd["edges"]:
                    new_w = max(_MIN_W, w - dx)
                    x = x + (w - new_w)
                    w = new_w
                if "n" in rd["edges"]:
                    new_h = max(_MIN_H, h - dy)
                    y = y + (h - new_h)
                    h = new_h

                win.geometry(f"{w}x{h}+{x}+{y}")

            def _release(e):
                self._resize_data = {}
                self._save_palette()
                two_col = win.winfo_width() >= _TWO_COL_THRESHOLD
                if two_col != self._two_col:
                    self._apply_filter()

            grip.bind("<ButtonPress-1>", _press)
            grip.bind("<B1-Motion>", _drag)
            grip.bind("<ButtonRelease-1>", _release)

    # ------------------------------------------------------------------
    # Title bar drag
    # ------------------------------------------------------------------

    def _on_drag_start(self, event):
        self._drag_data = {
            "x": event.x_root,
            "y": event.y_root,
            "wx": self._win.winfo_x(),
            "wy": self._win.winfo_y(),
        }

    def _on_drag_move(self, event):
        dd = self._drag_data
        if not dd:
            return
        dx = event.x_root - dd["x"]
        dy = event.y_root - dd["y"]
        self._win.geometry(f"+{dd['wx'] + dx}+{dd['wy'] + dy}")

    def _on_drag_end(self, event):
        self._drag_data = {}
        self._save_palette()

    # ------------------------------------------------------------------
    # Window events
    # ------------------------------------------------------------------

    def _on_configure(self, event):
        if event.widget is not self._win:
            return
        w = self._win.winfo_width()
        two_col = w >= _TWO_COL_THRESHOLD
        if two_col != self._two_col:
            self._apply_filter()

    def _on_mousewheel(self, event):
        if self._canvas:
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ------------------------------------------------------------------
    # Topmost loop
    # ------------------------------------------------------------------

    def _start_topmost_loop(self):
        self._stop_topmost_loop()
        self._schedule_topmost()

    def _schedule_topmost(self):
        if not self._visible or self._win is None:
            return
        try:
            self._win.attributes("-topmost", True)
            self._topmost_id = self._root.after(_TOPMOST_MS, self._schedule_topmost)
        except tk.TclError:
            self._win = None
            self._visible = False

    def _stop_topmost_loop(self):
        if self._topmost_id is not None:
            try:
                self._root.after_cancel(self._topmost_id)
            except Exception:
                pass
            self._topmost_id = None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_palette(self):
        try:
            if self._palette_path.exists():
                data = json.loads(self._palette_path.read_text(encoding="utf-8"))
                self._pinned = set(data.get("pinned", []))
                geom = data.get("geometry", {})
                self._opacity = float(data.get("opacity", 0.85))
                self._geom["x"] = geom.get("x")
                self._geom["y"] = geom.get("y")
                self._geom["w"] = geom.get("w", _DEFAULT_W)
                self._geom["h"] = geom.get("h", _DEFAULT_H)
        except Exception:
            pass

    def _save_palette(self):
        if self._win is None:
            return
        try:
            data = {
                "pinned": sorted(self._pinned),
                "opacity": round(self._opacity, 2),
                "geometry": {
                    "x": self._win.winfo_x(),
                    "y": self._win.winfo_y(),
                    "w": self._win.winfo_width(),
                    "h": self._win.winfo_height(),
                },
            }
            self._palette_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as e:
            print(f"[CHEATSHEET] Could not save palette: {e}")
