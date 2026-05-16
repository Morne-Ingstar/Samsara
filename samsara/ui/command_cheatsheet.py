"""
Samsara Command Cheat Sheet

A floating, always-on-top command reference window. Resizable by dragging
edges/corners, repositionable by dragging the title bar, filterable by phrase.
Commands can be pinned and executed by clicking (300ms teal flash as visual
confirmation). State persists to command_palette.json.

Architecture:
  PinnedFrame  -- always-rendered Frame above the canvas; holds "Most used"
                  and pinned-command rows. Never virtualised.
  CanvasScroller -- Canvas + VirtualListController below PinnedFrame; holds
                   all non-pinned commands. Only visible rows are rendered.
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
_TITLE_H = 34
_FILTER_H = 32
_ROW_H = 28       # structurally enforced: fixed height on every row container
_PAD_X = 10
_RESIZE_M = 8
_TOPMOST_MS = 3000
_FLASH_MS = 300
_VISIBLE_BUFFER = 5  # extra rows rendered above/below the viewport

# TODO: keyboard navigation (Tab/arrows) not implemented


class VirtualListController:
    """Canvas-based virtualised row list.

    Maintains a fixed pool of row widgets. On every scroll event, pool slots
    are repositioned and rebound to the visible slice of the backing data list.
    Widgets are never destroyed or recreated during scroll — only their text
    and bindings change.

    Pool size = viewport_height // ROW_HEIGHT + (VISIBLE_BUFFER * 2) + 2,
    calculated lazily on the first <Configure> event and grown when the
    viewport expands. Never shrinks.
    """

    def __init__(
        self,
        canvas: tk.Canvas,
        execute_cb: Callable[[str], None],
        toggle_pin_cb: Callable[[str], None],
        pinned: set,
        root: tk.Misc,
    ):
        self._canvas = canvas
        self._execute_cb = execute_cb
        self._toggle_pin_cb = toggle_pin_cb
        self._pinned = pinned
        self._root = root

        self._data: List[dict] = []
        self._pool: List[dict] = []   # [{frame, pin_lbl, phrase_lbl, right_lbl, cmd_id}]
        self._win_ids: List[int] = [] # canvas window IDs, parallel to _pool

        self._flash_id = None

        canvas.bind("<Configure>",  self._on_canvas_configure)
        canvas.bind("<MouseWheel>", self._on_mousewheel)    # Windows / macOS
        canvas.bind("<Button-4>",   self._on_scroll_up)    # Linux X11
        canvas.bind("<Button-5>",   self._on_scroll_down)  # Linux X11

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def set_data(self, data: List[dict]):
        """Replace the backing list and re-render from the top."""
        self._data = data
        n = len(data)
        w = max(1, self._canvas.winfo_width())
        self._canvas.configure(scrollregion=(0, 0, w, n * _ROW_H))
        self._canvas.yview_moveto(0)
        self._ensure_pool()
        self._render()

    def refresh_pin_indicators(self):
        """Re-render pool to reflect a pin state change."""
        self._render()

    # ----------------------------------------------------------------
    # Pool management
    # ----------------------------------------------------------------

    def _pool_needed(self) -> int:
        vh = max(1, self._canvas.winfo_height())
        return (vh // _ROW_H) + (_VISIBLE_BUFFER * 2) + 2

    def _ensure_pool(self):
        needed = self._pool_needed()
        while len(self._pool) < needed:
            slot = self._make_slot()
            win_id = self._canvas.create_window(
                (0, -_ROW_H * 2),
                window=slot["frame"],
                anchor="nw",
                width=max(1, self._canvas.winfo_width()),
            )
            self._pool.append(slot)
            self._win_ids.append(win_id)

    def _make_slot(self) -> dict:
        # ROW_H is enforced via pack_propagate(False) + explicit height=
        frame = tk.Frame(self._canvas, bg=_SURFACE, height=_ROW_H, cursor="hand2")
        frame.pack_propagate(False)

        pin_lbl = tk.Label(
            frame, text=" ",
            bg=_SURFACE, fg=_TEXT_SEC,
            font=(_FONT, 9, "bold"), width=2, cursor="hand2",
        )
        pin_lbl.place(relx=0, rely=0.5, x=4, anchor="w")

        phrase_lbl = tk.Label(
            frame, text="",
            bg=_SURFACE, fg=_TEXT_PRI,
            font=(_FONT, 9), anchor="w",
            cursor="hand2",
            wraplength=0,  # no text wrapping — row height stays fixed
        )
        phrase_lbl.place(relx=0, rely=0.5, x=22, anchor="w")

        right_lbl = tk.Label(
            frame, text="",
            bg=_SURFACE, fg=_TEXT_SEC,
            font=(_FONT, 8), anchor="e",
            cursor="hand2",
        )
        right_lbl.place(relx=1, rely=0.5, x=-8, anchor="e")

        slot = {
            "frame": frame,
            "pin_lbl": pin_lbl,
            "phrase_lbl": phrase_lbl,
            "right_lbl": right_lbl,
            "cmd_id": None,
        }

        def _enter(e, f=frame):
            f.configure(bg=_ELEVATED)
            for child in f.winfo_children():
                try:
                    child.configure(bg=_ELEVATED)
                except tk.TclError:
                    pass

        def _leave(e, f=frame, s=slot):
            if self._flash_id is not None:
                return
            f.configure(bg=_SURFACE)
            for child in f.winfo_children():
                try:
                    child.configure(bg=_SURFACE)
                except tk.TclError:
                    pass

        for widget in (frame, phrase_lbl):
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)

        return slot

    def _rebind_slot(self, slot: dict, cmd_id: str):
        """Point click and pin bindings at cmd_id.

        cmd_id is the stable command phrase string, captured at bind time so
        scrolling never causes the wrong command to fire.
        """
        if slot["cmd_id"] == cmd_id:
            return  # already bound — skip overhead

        slot["cmd_id"] = cmd_id
        frame = slot["frame"]
        pin_lbl = slot["pin_lbl"]
        phrase_lbl = slot["phrase_lbl"]

        def _click(e, p=cmd_id, s=slot):
            self._flash_and_execute(s, p)

        def _pin(e, p=cmd_id):
            self._toggle_pin_cb(p)

        frame.bind("<Button-1>", _click)
        phrase_lbl.bind("<Button-1>", _click)
        pin_lbl.bind("<Button-1>", _pin)

    # ----------------------------------------------------------------
    # Rendering
    # ----------------------------------------------------------------

    def _render(self):
        n = len(self._data)
        canvas_w = max(1, self._canvas.winfo_width())
        vh = max(1, self._canvas.winfo_height())
        viewport_rows = max(1, vh // _ROW_H)

        if n == 0:
            for win_id in self._win_ids:
                try:
                    self._canvas.coords(win_id, 0, -_ROW_H * 2)
                except tk.TclError:
                    pass
            return

        yview = self._canvas.yview()
        first_visible = int(yview[0] * n)
        render_start = max(0, first_visible - _VISIBLE_BUFFER)
        render_end = min(n, first_visible + viewport_rows + _VISIBLE_BUFFER)

        pool_idx = 0
        for i in range(render_start, render_end):
            if pool_idx >= len(self._pool):
                break

            cmd = self._data[i]
            phrase = cmd["phrase"]
            pack = cmd.get("pack", "")
            pinned = phrase in self._pinned

            slot = self._pool[pool_idx]
            win_id = self._win_ids[pool_idx]

            # Reset bg so a previously-hovered slot doesn't stay highlighted
            slot["frame"].configure(bg=_SURFACE)
            for child in slot["frame"].winfo_children():
                try:
                    child.configure(bg=_SURFACE)
                except tk.TclError:
                    pass

            slot["pin_lbl"].configure(
                text="*" if pinned else " ",
                fg=_ACCENT if pinned else _TEXT_SEC,
            )
            slot["phrase_lbl"].configure(text=phrase.title())
            slot["right_lbl"].configure(
                text="" if (not pack or pack == "core") else pack
            )

            # Rebind to this command's phrase — captured in closure at bind time
            self._rebind_slot(slot, phrase)

            # Reposition via canvas.coords — do NOT use pack/grid inside canvas
            self._canvas.coords(win_id, 0, i * _ROW_H)
            self._canvas.itemconfig(win_id, width=canvas_w)

            pool_idx += 1

        # Park unused pool slots off-screen
        for j in range(pool_idx, len(self._pool)):
            try:
                self._canvas.coords(self._win_ids[j], 0, -_ROW_H * 2)
            except tk.TclError:
                pass

    # ----------------------------------------------------------------
    # Scroll
    # ----------------------------------------------------------------

    def _scroll_by(self, dy_px: int):
        n = len(self._data)
        if n == 0:
            return
        total_h = n * _ROW_H
        cur_y = self._canvas.yview()[0] * total_h
        new_y = max(0.0, min(float(total_h), cur_y + dy_px))
        self._canvas.yview_moveto(new_y / total_h)
        self._render()

    def on_scrollbar(self, *args):
        """Scrollbar command — routes to canvas.yview then re-renders."""
        self._canvas.yview(*args)
        self._render()

    def _on_mousewheel(self, event):
        # Windows/macOS: delta is ±120 multiples; positive = scroll up
        delta = -1 if event.delta > 0 else 1
        self._scroll_by(delta * _ROW_H)

    def _on_scroll_up(self, event):
        self._scroll_by(-_ROW_H)

    def _on_scroll_down(self, event):
        self._scroll_by(_ROW_H)

    def _on_canvas_configure(self, event):
        canvas_w = event.width
        n = len(self._data)
        self._canvas.configure(scrollregion=(0, 0, canvas_w, n * _ROW_H))
        for win_id in self._win_ids:
            try:
                self._canvas.itemconfig(win_id, width=canvas_w)
            except tk.TclError:
                pass
        self._ensure_pool()
        self._render()

    # ----------------------------------------------------------------
    # Flash + execute
    # ----------------------------------------------------------------

    def _flash_and_execute(self, slot: dict, cmd_id: str):
        frame = slot["frame"]

        def _set_bg(color):
            try:
                frame.configure(bg=color)
                for child in frame.winfo_children():
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
            self._execute_cb(cmd_id)
        except Exception as e:
            print(f"[CHEATSHEET] Execute failed for '{cmd_id}': {e}")


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

        self._topmost_id = None
        self._flash_id = None
        self._filter_var: Optional[tk.StringVar] = None

        self._static_pane: Optional[tk.Frame] = None  # PinnedFrame
        self._vlist: Optional[VirtualListController] = None

        self._resize_data: dict = {}
        self._drag_data: dict = {}
        self._opacity: float = 0.85
        self._opacity_var: Optional[tk.IntVar] = None
        self._geom = {"x": None, "y": None, "w": _DEFAULT_W, "h": _DEFAULT_H}

        self._load_palette()

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

        outer = tk.Frame(self._win, bg=_BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        self._build_title_bar(outer)
        self._build_filter_bar(outer)
        tk.Frame(outer, bg=_BORDER, height=1).pack(fill="x")
        self._build_list(outer)
        self._install_resize_grips(self._win)
        self._win.bind("<Configure>", self._on_configure)

    def _build_title_bar(self, parent: tk.Frame):
        bar = tk.Frame(parent, bg=_SURFACE, height=_TITLE_H)
        bar.pack(fill="x")
        bar.pack_propagate(False)

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

        entry_border = tk.Frame(bar, bg=_BORDER)
        entry_border.place(x=0, y=0, relwidth=1, relheight=1)
        entry.lift()

    def _build_list(self, parent: tk.Frame):
        # PinnedFrame: always-rendered section for "Most used" + pinned rows
        self._static_pane = tk.Frame(parent, bg=_BG)
        self._static_pane.pack(fill="x")

        # CanvasScroller: virtualised list for non-pinned commands
        container = tk.Frame(parent, bg=_SURFACE)
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, bg=_SURFACE, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(container, orient="vertical")
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._vlist = VirtualListController(
            canvas=canvas,
            execute_cb=self._execute_cb,
            toggle_pin_cb=self._toggle_pin,
            pinned=self._pinned,
            root=self._root,
        )
        scrollbar.configure(command=self._vlist.on_scrollbar)

    # ------------------------------------------------------------------
    # PinnedFrame ("Most used" + pinned rows)
    # ------------------------------------------------------------------

    def _rebuild_static_pane(self):
        """Rebuild the always-rendered section. Called on open, pin change,
        and command list change. Does NOT fire on every filter keystroke."""
        if self._static_pane is None:
            return

        for child in self._static_pane.winfo_children():
            child.destroy()

        has_content = False

        # --- Most used section ---
        try:
            from samsara.command_stats import get_top_commands
            top_raw = get_top_commands(8)
            phrase_to_cmd = {c["phrase"]: c for c in self._all}
            top = [(name, cnt) for name, cnt in top_raw
                   if name in phrase_to_cmd and cnt > 0]
        except Exception:
            top = []

        if top:
            tk.Label(
                self._static_pane, text="MOST USED",
                bg=_BG, fg=_TEXT_SEC,
                font=(_FONT, 7, "bold"), anchor="w",
                padx=_PAD_X, pady=3,
            ).pack(fill="x")
            for phrase, cnt in top:
                self._make_static_row(
                    phrase_to_cmd[phrase], count=cnt
                ).pack(fill="x", pady=1)
            has_content = True

        # --- Pinned section ---
        pinned_cmds = [c for c in self._all if c["phrase"] in self._pinned]
        if pinned_cmds:
            if has_content:
                tk.Frame(self._static_pane, bg=_BORDER, height=1).pack(
                    fill="x", padx=_PAD_X, pady=2)
            tk.Label(
                self._static_pane, text="PINNED",
                bg=_BG, fg=_TEXT_SEC,
                font=(_FONT, 7, "bold"), anchor="w",
                padx=_PAD_X, pady=3,
            ).pack(fill="x")
            for cmd in pinned_cmds:
                self._make_static_row(cmd).pack(fill="x", pady=1)
            has_content = True

        if has_content:
            tk.Frame(self._static_pane, bg=_BORDER, height=1).pack(
                fill="x", pady=2)

    def _make_static_row(self, cmd: dict, count: int = None) -> tk.Frame:
        """Build a fully-rendered row for the static pane."""
        phrase = cmd["phrase"]
        pack = cmd.get("pack", "")
        pinned = phrase in self._pinned

        cell = tk.Frame(self._static_pane, bg=_SURFACE, height=_ROW_H, cursor="hand2")
        cell.pack_propagate(False)

        pin_lbl = tk.Label(
            cell, text="*" if pinned else " ",
            bg=_SURFACE, fg=_ACCENT if pinned else _TEXT_SEC,
            font=(_FONT, 9, "bold"), width=2, cursor="hand2",
        )
        pin_lbl.place(relx=0, rely=0.5, x=4, anchor="w")

        phrase_lbl = tk.Label(
            cell, text=phrase.title(),
            bg=_SURFACE, fg=_TEXT_PRI,
            font=(_FONT, 9), anchor="w",
            cursor="hand2",
            wraplength=0,
        )
        phrase_lbl.place(relx=0, rely=0.5, x=22, anchor="w")

        if count is not None:
            tk.Label(
                cell, text=str(count),
                bg=_SURFACE, fg=_TEXT_SEC,
                font=(_FONT, 8), anchor="e",
            ).place(relx=1, rely=0.5, x=-8, anchor="e")
        elif pack and pack != "core":
            tk.Label(
                cell, text=pack,
                bg=_SURFACE, fg=_TEXT_SEC,
                font=(_FONT, 8), anchor="e",
            ).place(relx=1, rely=0.5, x=-8, anchor="e")

        def _enter(e, f=cell):
            f.configure(bg=_ELEVATED)
            for child in f.winfo_children():
                try:
                    child.configure(bg=_ELEVATED)
                except tk.TclError:
                    pass

        def _leave(e, f=cell):
            if self._flash_id is not None:
                return
            f.configure(bg=_SURFACE)
            for child in f.winfo_children():
                try:
                    child.configure(bg=_SURFACE)
                except tk.TclError:
                    pass

        for widget in (cell, phrase_lbl):
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)
            widget.bind("<Button-1>", lambda e, p=phrase, c=cell: self._flash_execute_static(c, p))

        pin_lbl.bind("<Button-1>", lambda e, p=phrase: self._toggle_pin(p))

        return cell

    def _flash_execute_static(self, cell: tk.Frame, phrase: str):
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

    # ------------------------------------------------------------------
    # Command list management
    # ------------------------------------------------------------------

    def _refresh_commands(self):
        self._all = self._commands_cb()
        self._rebuild_static_pane()
        self._apply_filter()

    def _on_filter_change(self, *_args):
        self._apply_filter()

    def _apply_filter(self):
        if self._vlist is None:
            return

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

        # Pinned commands live in PinnedFrame only — never in CanvasScroller
        unpinned = [c for c in filtered if c["phrase"] not in self._pinned]
        self._vlist.set_data(unpinned)

    def _toggle_pin(self, phrase: str):
        if phrase in self._pinned:
            self._pinned.discard(phrase)
        else:
            self._pinned.add(phrase)
        self._save_palette()
        self._rebuild_static_pane()
        self._apply_filter()

    # ------------------------------------------------------------------
    # Resize grips
    # ------------------------------------------------------------------

    def _install_resize_grips(self, win: tk.Toplevel):
        m = _RESIZE_M

        grips = [
            ("n",  dict(relx=0, rely=0,  relwidth=1,  height=m, width=0), "size_ns",    ("n",)),
            ("s",  dict(relx=0, rely=1,  relwidth=1,  height=m, width=0, y=-m), "size_ns", ("s",)),
            ("w",  dict(relx=0, rely=0,  relheight=1, width=m,  height=0), "size_we",   ("w",)),
            ("e",  dict(relx=1, rely=0,  relheight=1, width=m,  height=0, x=-m), "size_we", ("e",)),
            ("nw", dict(relx=0, rely=0,  width=m*2,   height=m*2), "size_nw_se",        ("n", "w")),
            ("ne", dict(relx=1, rely=0,  width=m*2,   height=m*2, x=-m*2), "size_ne_sw", ("n", "e")),
            ("sw", dict(relx=0, rely=1,  width=m*2,   height=m*2, y=-m*2), "size_ne_sw", ("s", "w")),
            ("se", dict(relx=1, rely=1,  width=m*2,   height=m*2, x=-m*2, y=-m*2), "size_nw_se", ("s", "e")),
        ]

        for name, place_kw, cursor, edges in grips:
            grip = tk.Frame(win, bg=_BG, cursor=cursor)
            grip.place(**place_kw)
            grip.lift()

            def _press(e, ed=edges):
                self._resize_data = {
                    "edges": ed,
                    "x0": e.x_root, "y0": e.y_root,
                    "wx": win.winfo_x(), "wy": win.winfo_y(),
                    "ww": win.winfo_width(), "wh": win.winfo_height(),
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
                    x += w - new_w
                    w = new_w
                if "n" in rd["edges"]:
                    new_h = max(_MIN_H, h - dy)
                    y += h - new_h
                    h = new_h
                win.geometry(f"{w}x{h}+{x}+{y}")

            def _release(e):
                self._resize_data = {}
                self._save_palette()

            grip.bind("<ButtonPress-1>", _press)
            grip.bind("<B1-Motion>", _drag)
            grip.bind("<ButtonRelease-1>", _release)

    # ------------------------------------------------------------------
    # Title bar drag
    # ------------------------------------------------------------------

    def _on_drag_start(self, event):
        self._drag_data = {
            "x": event.x_root, "y": event.y_root,
            "wx": self._win.winfo_x(), "wy": self._win.winfo_y(),
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
        # VirtualListController handles canvas resizes internally via its
        # own <Configure> binding. Nothing to do here.

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
