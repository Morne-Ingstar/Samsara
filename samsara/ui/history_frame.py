"""Reusable history viewer frame.

A CTkFrame subclass that renders the persistent SQLite history. Used both
embedded in the main hub window and inside the Voice Training window's
History tab. Single source of truth for the history UI -- a fix here lands
everywhere it's mounted.

Refresh model:
  - Initial load fetches PAGE_SIZE rows. More pages fetch from the DB on
    scroll-near-bottom (offset-paginated).
  - New dictations are pushed via on_new_entry() from the parent (no full
    rebuild -- prepends the new card).
  - A light visibility-gated poll re-checks for new rows every POLL_MS but
    only does a 1-row "is there anything newer?" query, never a full
    rebuild. When the host tab is hidden, the poll skips the DB hit.
  - Refresh button, search query change, and filter change all do a full
    rebuild (rare, user-initiated).

All DB calls run on a worker thread; results are marshalled back to the
Tk main thread via after(0, ...).
"""

import logging
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

logger = logging.getLogger(__name__)


class HistoryFrame(ctk.CTkFrame):
    """Search + filter + paginated card list of persistent dictation history."""

    PAGE_SIZE = 50
    POLL_MS = 5000
    DEBOUNCE_MS = 300
    FILTERED_FETCH_LIMIT = 500

    STATUS_COLORS = {
        'success': '#3ad26a',
        'failed': '#e25555',
        'empty': '#888888',
    }

    def __init__(self, parent, app, *, is_visible=None, **kwargs):
        """
        Args:
            parent: any Tk widget that can host a CTkFrame.
            app: the DictationApp instance. Must expose .history_db.
            is_visible: optional callable returning True when this frame's
                host tab/page is currently shown. The poll skips the DB
                query when this returns False. Defaults to "always visible".
        """
        super().__init__(parent, **kwargs)
        self.app = app
        self._is_visible = is_visible or (lambda: True)
        self._alive = True

        # State
        self._filter = "all"          # all | success | failed | empty
        self._query = ""
        self._rows = []
        self._visible_count = 0
        self._loading = False
        self._has_more_in_db = True   # whether DB has unfetched older rows
        self._top_row_id = None       # id of the newest row currently shown
        self._search_after_id = None
        self._poll_after_id = None
        self._expanded_id = None
        self._card_widgets = {}       # row_id -> card frame
        self._filter_buttons = {}

        self._build_ui()
        self._reload(force=True)
        self._schedule_poll()

    # ---- Lifecycle -------------------------------------------------------

    def destroy(self):
        """Stop polling/debounce timers before tearing down widgets."""
        self._alive = False
        for after_id_attr in ('_search_after_id', '_poll_after_id'):
            after_id = getattr(self, after_id_attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, after_id_attr, None)
        super().destroy()

    # ---- Layout ----------------------------------------------------------

    def _build_ui(self):
        ctk.CTkLabel(
            self, text="Dictation History",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(15, 5))
        ctk.CTkLabel(
            self,
            text="Every dictation, command, and failure is recorded here. "
                 "Click an entry to expand.",
            text_color="gray", wraplength=600,
        ).pack(anchor='w', pady=(0, 12))

        # Search bar
        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.pack(fill='x', pady=(0, 8))
        self._search_entry = ctk.CTkEntry(
            search_frame,
            placeholder_text="Search transcriptions...",
            height=32,
        )
        self._search_entry.pack(side='left', fill='x', expand=True)
        self._search_entry.bind(
            '<KeyRelease>', lambda _e: self._on_search_changed())

        ctk.CTkButton(
            search_frame, text="Refresh", width=80, height=32,
            fg_color="gray40",
            command=lambda: self._reload(force=True),
        ).pack(side='left', padx=(8, 0))

        # Filter buttons
        filter_frame = ctk.CTkFrame(self, fg_color="transparent")
        filter_frame.pack(fill='x', pady=(0, 8))
        for label, key in (("All", "all"), ("Success", "success"),
                           ("Failed", "failed"), ("Empty", "empty")):
            btn = ctk.CTkButton(
                filter_frame, text=label, width=80, height=28,
                command=lambda k=key: self._set_filter(k),
            )
            btn.pack(side='left', padx=(0, 6))
            self._filter_buttons[key] = btn
        self._apply_filter_styles()

        # Scrollable list of cards
        self._list = ctk.CTkScrollableFrame(self, corner_radius=10)
        self._list.pack(fill='both', expand=True, pady=(4, 0))

        # Status label (empty state / "loading more...")
        self._status_label = ctk.CTkLabel(
            self, text="", text_color="gray", anchor='w')
        self._status_label.pack(fill='x', pady=(6, 0))

        # Paginate-on-scroll
        inner_canvas = self._list._parent_canvas
        inner_canvas.bind('<Configure>',
                          lambda _e: self._check_paginate())
        inner_canvas.bind(
            '<MouseWheel>',
            lambda _e: self.after(50, self._check_paginate),
            add='+')

    # ---- Filter + search -------------------------------------------------

    def _is_filtered(self):
        return bool(self._query) or self._filter != "all"

    def _set_filter(self, key):
        if key == self._filter:
            return
        self._filter = key
        self._apply_filter_styles()
        self._reload(force=True)

    def _apply_filter_styles(self):
        for key, btn in self._filter_buttons.items():
            if key == self._filter:
                btn.configure(fg_color=("#1f6aa5", "#1f6aa5"))
            else:
                btn.configure(fg_color="gray40")

    def _on_search_changed(self):
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(
            self.DEBOUNCE_MS, self._run_search)

    def _run_search(self):
        self._search_after_id = None
        new_query = self._search_entry.get().strip()
        if new_query == self._query:
            return
        self._query = new_query
        self._reload(force=True)

    # ---- Data load + render ---------------------------------------------

    def _reload(self, force=False):
        """Full refetch + rebuild. Used for initial load, search/filter
        changes, and the explicit Refresh button -- never for new-entry
        push or scroll pagination."""
        if self._loading and not force:
            return
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            self._set_status("History database not available.")
            return

        self._loading = True
        query = self._query
        status_filter = self._filter

        def fetch():
            try:
                if query:
                    rows = history_db.search(
                        query, limit=self.FILTERED_FETCH_LIMIT)
                elif status_filter != "all":
                    rows = history_db.recent(
                        limit=self.FILTERED_FETCH_LIMIT)
                    rows = [r for r in rows if r['status'] == status_filter]
                else:
                    rows = history_db.recent(limit=self.PAGE_SIZE)
            except Exception as e:
                logger.error(f"History fetch failed: {e}", exc_info=True)
                rows = []
            self._after_safe(lambda: self._apply_rows(rows))

        threading.Thread(target=fetch, daemon=True).start()

    def _after_safe(self, fn):
        if not self._alive:
            return
        try:
            self.after(0, fn)
        except Exception:
            pass

    def _apply_rows(self, rows):
        self._loading = False
        self._rows = list(rows)
        self._visible_count = 0
        # In the un-filtered case we know whether the DB has more rows
        # beyond what we just fetched; in the filtered/search case we
        # already pulled up to FILTERED_FETCH_LIMIT into memory.
        if self._is_filtered():
            self._has_more_in_db = False
        else:
            self._has_more_in_db = len(rows) >= self.PAGE_SIZE

        if self._expanded_id is not None and not any(
                r['id'] == self._expanded_id for r in rows):
            self._expanded_id = None
        for child in list(self._list.winfo_children()):
            child.destroy()
        self._card_widgets = {}
        self._render_more()

        self._top_row_id = self._rows[0]['id'] if self._rows else None

        if not rows:
            self._set_status(
                "No matching entries." if self._is_filtered()
                else "No history yet. Hold Ctrl+Shift and say something to get started.")
        else:
            self._update_status_count()

    def _render_more(self):
        """Render the next slice of in-memory rows into card widgets."""
        end = min(self._visible_count + self.PAGE_SIZE, len(self._rows))
        for row in self._rows[self._visible_count:end]:
            self._render_card(row)
        self._visible_count = end
        self._update_status_count()

    def _check_paginate(self):
        """Scroll-driven pagination.

        Filtered/search results are already fully loaded in memory --
        just render more cards. Un-filtered results paginate via the DB
        (LIMIT/OFFSET) so we never hold all 10k entries at once."""
        if not self._alive or self._loading:
            return
        try:
            _top, bottom = self._list._parent_canvas.yview()
        except Exception:
            return
        if bottom < 0.9:
            return

        if self._visible_count < len(self._rows):
            self._render_more()
            return

        if self._is_filtered() or not self._has_more_in_db:
            return

        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            return
        offset = len(self._rows)
        self._loading = True
        self._set_status("Loading more...")

        def fetch():
            try:
                page = history_db.recent(
                    limit=self.PAGE_SIZE, offset=offset)
            except Exception as e:
                logger.error(f"history page fetch failed: {e}",
                             exc_info=True)
                page = []
            self._after_safe(lambda: self._append_rows(page))

        threading.Thread(target=fetch, daemon=True).start()

    def _append_rows(self, rows):
        self._loading = False
        if not rows:
            self._has_more_in_db = False
            self._update_status_count()
            return
        for row in rows:
            self._rows.append(row)
            self._render_card(row)
            self._visible_count += 1
        if len(rows) < self.PAGE_SIZE:
            self._has_more_in_db = False
        self._update_status_count()

    def _update_status_count(self):
        total = len(self._rows)
        shown = self._visible_count
        if total == 0:
            return
        suffix = ""
        if shown < total or self._has_more_in_db:
            suffix = " (scroll for more)"
        self._set_status(f"Showing {shown} entries{suffix}")

    def _set_status(self, text):
        if self._alive:
            try:
                self._status_label.configure(text=text)
            except Exception:
                pass

    # ---- Card rendering --------------------------------------------------

    @staticmethod
    def _format_timestamp(ts_iso):
        try:
            dt = datetime.fromisoformat(ts_iso)
        except Exception:
            return ts_iso or ""
        delta = datetime.now() - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60} min ago"
        if secs < 86400:
            return dt.strftime("%I:%M %p").lstrip('0')
        return dt.strftime("%b %d, %I:%M %p")

    @staticmethod
    def _truncate(text, length=80):
        if not text:
            return ""
        text = text.replace('\n', ' ').strip()
        return text if len(text) <= length else text[:length - 1] + '...'

    def _status_color(self, status):
        return self.STATUS_COLORS.get(status, '#888888')

    def _render_card(self, row, prepend=False):
        # Capture the current first sibling BEFORE creating the new card
        # so pack(before=...) inserts the new one above it.
        before_target = None
        if prepend:
            existing = self._list.winfo_children()
            if existing:
                before_target = existing[0]

        row_id = row['id']
        card = ctk.CTkFrame(self._list, corner_radius=8)
        self._card_widgets[row_id] = card

        if before_target is not None:
            card.pack(fill='x', padx=4, pady=3, before=before_target)
        else:
            card.pack(fill='x', padx=4, pady=3)

        # Top row: status dot + truncated text + duration
        top = ctk.CTkFrame(card, fg_color="transparent")
        top.pack(fill='x', padx=10, pady=(8, 2))

        dot = tk.Canvas(top, width=12, height=12,
                        highlightthickness=0, bg='#2b2b2b')
        dot.create_oval(2, 2, 11, 11,
                        fill=self._status_color(row['status']),
                        outline='')
        dot.pack(side='left', padx=(0, 8))

        preview = self._truncate(row['display_text'] or row['raw_text'])
        if not preview and row['status'] == 'empty':
            preview = "(no speech detected)"
        text_label = ctk.CTkLabel(top, text=preview, anchor='w', justify='left')
        text_label.pack(side='left', fill='x', expand=True)

        if row['duration_ms']:
            ctk.CTkLabel(
                top,
                text=f"{row['duration_ms']/1000:.1f}s",
                text_color="gray",
                font=ctk.CTkFont(size=11),
            ).pack(side='right', padx=(8, 0))

        # Bottom row: timestamp + app context
        meta = ctk.CTkFrame(card, fg_color="transparent")
        meta.pack(fill='x', padx=10, pady=(0, 8))
        ctk.CTkLabel(
            meta,
            text=self._format_timestamp(row['timestamp']),
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(side='left')
        if row['app_context']:
            ctk.CTkLabel(
                meta,
                text=self._truncate(row['app_context'], 60),
                text_color="gray",
                font=ctk.CTkFont(size=11),
            ).pack(side='right')

        for widget in (card, top, meta, text_label):
            widget.bind('<Button-1>',
                        lambda _e, rid=row_id: self._toggle_expand(rid))

        if self._expanded_id == row_id:
            self._build_expanded_body(card, row)

    def _toggle_expand(self, row_id):
        prev = self._expanded_id
        self._expanded_id = None if prev == row_id else row_id
        for rid in {prev, row_id}:
            if rid is None:
                continue
            row = next((r for r in self._rows if r['id'] == rid), None)
            card = self._card_widgets.get(rid)
            if row is None or card is None:
                continue
            self._strip_expanded_body(card)
            if rid == self._expanded_id:
                self._build_expanded_body(card, row)

    @staticmethod
    def _strip_expanded_body(card):
        for child in list(card.winfo_children()):
            if getattr(child, '_history_expanded', False):
                child.destroy()

    def _build_expanded_body(self, card, row):
        body = ctk.CTkFrame(card, fg_color="transparent")
        body._history_expanded = True
        body.pack(fill='x', padx=10, pady=(0, 10))

        meta_row = ctk.CTkFrame(body, fg_color="transparent")
        meta_row.pack(fill='x', pady=(0, 6))
        ctk.CTkLabel(
            meta_row,
            text=f"mode: {row['mode'] or 'hold'}",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(side='left')
        ctk.CTkLabel(
            meta_row,
            text=f"status: {row['status']}",
            text_color=self._status_color(row['status']),
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side='left', padx=(12, 0))

        full_text = row['display_text'] or row['raw_text'] or "(empty)"
        text_widget = tk.Text(
            body, wrap='word',
            height=min(8, max(2, full_text.count('\n') + 2)),
            bg='#2b2b2b', fg='white', insertbackground='white',
            relief='flat', borderwidth=0, padx=8, pady=6,
        )
        text_widget.insert('1.0', full_text)
        text_widget.configure(state='disabled')
        text_widget.pack(fill='x')

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill='x', pady=(8, 0))
        ctk.CTkButton(
            actions, text="Copy", width=80, height=28,
            command=lambda: self._copy(row),
        ).pack(side='left')
        ctk.CTkButton(
            actions, text="Delete", width=80, height=28,
            fg_color="#7a2a2a", hover_color="#9a3030",
            command=lambda rid=row['id']: self._delete(rid),
        ).pack(side='left', padx=(8, 0))

        if row['status'] == 'failed':
            ctk.CTkButton(
                actions, text="Retry", width=80, height=28,
                fg_color="gray40",
                command=lambda r=row: self._show_retry(r),
            ).pack(side='left', padx=(8, 0))

    # ---- Actions ---------------------------------------------------------

    def _copy(self, row):
        text = row['display_text'] or row['raw_text'] or ""
        try:
            import pyperclip
            pyperclip.copy(text)
            self._set_status("Copied to clipboard.")
        except Exception as e:
            logger.error(f"History copy failed: {e}")
            self._set_status(f"Copy failed: {e}")

    def _delete(self, row_id):
        if not messagebox.askyesno(
                "Delete entry",
                "Delete this history entry? This cannot be undone."):
            return
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            return

        def do_delete():
            try:
                history_db.delete(row_id)
                ok = True
            except Exception as e:
                logger.error(f"History delete failed: {e}", exc_info=True)
                ok = False
            self._after_safe(lambda: self._on_deleted(row_id, ok))

        threading.Thread(target=do_delete, daemon=True).start()

    def _on_deleted(self, row_id, ok):
        if not ok:
            self._set_status("Delete failed -- check log.")
            return
        card = self._card_widgets.pop(row_id, None)
        if card is not None:
            try:
                card.destroy()
            except Exception:
                pass
        prev_count = len(self._rows)
        self._rows = [r for r in self._rows if r['id'] != row_id]
        if len(self._rows) < prev_count:
            self._visible_count = max(
                0, min(self._visible_count, len(self._rows)))
        if self._expanded_id == row_id:
            self._expanded_id = None
        self._top_row_id = self._rows[0]['id'] if self._rows else None
        self._update_status_count()

    def _show_retry(self, row):
        msg = row['display_text'] or row['raw_text'] or "(no detail recorded)"
        messagebox.showinfo(
            "Failed transcription",
            f"This dictation failed.\n\n{msg}\n\nRe-dictate to try again.")

    # ---- Push-based new-entry hook + light visibility-gated poll --------

    def refresh(self):
        """Public hook: full reload (used by the Refresh button and any
        callers that want a hard rebuild). New-entry push should call
        on_new_entry() instead -- it's the cheap path."""
        if self._alive:
            self._reload(force=True)

    def on_new_entry(self):
        """Push hook: a new dictation just landed in the DB. Off-thread
        fetch the rows added since our current top, then prepend cards.

        No-ops when a search query or non-default status filter is active
        (the new entry might not match -- the user can hit Refresh)."""
        if not self._alive or self._loading or self._is_filtered():
            return
        self._fetch_newer_async()

    def _fetch_newer_async(self):
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            return
        top_id = self._top_row_id

        def fetch():
            try:
                # Pull a small window from the top; filter to id > top_id
                latest = history_db.recent(limit=self.PAGE_SIZE)
            except Exception as e:
                logger.error(f"history newer fetch failed: {e}",
                             exc_info=True)
                return
            if top_id is None:
                new_rows = list(latest)
            else:
                new_rows = [r for r in latest if r['id'] > top_id]
            if not new_rows:
                return
            self._after_safe(lambda: self._prepend_rows(new_rows))

        threading.Thread(target=fetch, daemon=True).start()

    def _prepend_rows(self, rows):
        if not self._alive or not rows:
            return
        existing_ids = {r['id'] for r in self._rows}
        # rows are newest-first from the DB; reverse so we pack them in
        # oldest-first and the very newest ends up on top.
        added = 0
        for row in reversed(rows):
            if row['id'] in existing_ids:
                continue
            self._rows.insert(0, row)
            existing_ids.add(row['id'])
            self._render_card(row, prepend=True)
            self._visible_count += 1
            added += 1
        if added and self._rows:
            self._top_row_id = self._rows[0]['id']
        if added:
            self._update_status_count()

    def _schedule_poll(self):
        if not self._alive:
            return
        try:
            self._poll_after_id = self.after(self.POLL_MS, self._poll_tick)
        except Exception:
            self._poll_after_id = None

    def _poll_tick(self):
        """Belt-and-braces backstop: catches new rows that did not arrive
        via on_new_entry (e.g., voice_training's history tab where the
        push hook is not wired). Skips DB hits when the host is hidden,
        loading, or has a filter/search active."""
        if not self._alive:
            return
        try:
            if (self._is_visible()
                    and not self._loading
                    and not self._is_filtered()):
                self._fetch_newer_async()
        except Exception as e:
            logger.error(f"history poll error: {e}")
        self._schedule_poll()
