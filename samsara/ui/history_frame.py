"""Reusable history viewer frame.

A CTkFrame subclass that renders the persistent SQLite history. Used both
embedded in the main hub window and inside the Voice Training window's
History tab. Single source of truth for the history UI -- a fix here lands
everywhere it's mounted.

Refresh model:
  - Initial load fetches PAGE_SIZE rows (all-filter) or FILTERED_FETCH_LIMIT
    rows (status filter), then renders PAGE_SIZE at a time.
  - New dictations are pushed via on_new_entry() (no full rebuild -- prepends).
  - A visibility-gated poll re-checks every POLL_MS. When the host tab is
    hidden, the poll skips the DB hit.
  - Refresh button, search, and filter change all do a full rebuild.

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
        'failed':  '#e25555',
        'empty':   '#888888',
    }

    TYPE_COLORS = {
        'command':   '#1f6aa5',
        'dictation': '#4a8a6a',
        'failed':    '#c25a20',
    }

    TYPE_LABELS = {
        'command':   'CMD',
        'dictation': 'TXT',
        'failed':    'ERR',
    }

    def __init__(self, parent, app, *, is_visible=None, **kwargs):
        super().__init__(parent, **kwargs)
        self.app = app
        self._is_visible = is_visible or (lambda: True)
        self._alive = True

        # Display state
        self._filter = "success"      # default: successes view
        self._query = ""
        self._rows = []
        self._visible_count = 0
        self._loading = False
        self._has_more_in_db = True
        self._top_row_id = None
        self._search_after_id = None
        self._poll_after_id = None
        self._expanded_id = None
        self._card_widgets = {}       # row_id -> card_outer frame
        self._card_content = {}       # row_id -> content frame (for expand)
        self._filter_buttons = {}

        # Session grouping
        self._current_session_id = self._get_current_session_id()
        self._render_session_id = None    # last session rendered, for group breaks
        self._collapsed_sessions = set()  # sessions hidden by user
        self._user_expanded = set()       # sessions explicitly expanded by user
        self._session_cards = {}          # session_id -> [card_outer, ...]
        self._session_headers = {}        # session_id -> (header_frame, chevron_lbl, count_lbl)
        self._session_entry_counts = {}   # session_id -> total entry count from _rows

        # Per-card widget references for inline correction
        self._card_text_labels = {}       # row_id -> text preview CTkLabel
        self._card_top_frames = {}        # row_id -> top CTkFrame (holds the label)
        self._card_text_widgets = {}      # row_id -> tk.Text in expanded body
        self._card_correct_buttons = {}   # row_id -> "Correct/Save" CTkButton
        self._card_cancel_buttons = {}    # row_id -> "Cancel" CTkButton (hidden when idle)

        self._build_ui()
        self._reload(force=True)
        self._schedule_poll()

    def _get_current_session_id(self):
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            return None
        return getattr(history_db, 'session_id', None)

    # ---- Lifecycle -------------------------------------------------------

    def destroy(self):
        self._alive = False
        for attr in ('_search_after_id', '_poll_after_id'):
            after_id = getattr(self, attr, None)
            if after_id is not None:
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
                setattr(self, attr, None)
        super().destroy()

    # ---- Layout ----------------------------------------------------------

    def _build_ui(self):
        ctk.CTkLabel(
            self, text="Dictation History",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(15, 5))
        ctk.CTkLabel(
            self,
            text="Click an entry to copy. Right-click for more options. "
                 "Double-click to expand detail.",
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

        # Filter buttons: Successes -> Failed -> All -> Empty
        filter_frame = ctk.CTkFrame(self, fg_color="transparent")
        filter_frame.pack(fill='x', pady=(0, 8))
        for label, key in (("Successes", "success"), ("Failed", "failed"),
                           ("All", "all"), ("Empty", "empty")):
            btn = ctk.CTkButton(
                filter_frame, text=label, width=80, height=28,
                command=lambda k=key: self._set_filter(k),
            )
            btn.pack(side='left', padx=(0, 6))
            self._filter_buttons[key] = btn
        self._apply_filter_styles()

        # Scrollable card list
        self._list = ctk.CTkScrollableFrame(self, corner_radius=10)
        self._list.pack(fill='both', expand=True, pady=(4, 0))

        # Status label (entry count / loading / copy feedback)
        self._status_label = ctk.CTkLabel(
            self, text="", text_color="gray", anchor='w')
        self._status_label.pack(fill='x', pady=(4, 0))

        # Session stats bar
        self._stats_bar = ctk.CTkLabel(
            self, text="", text_color="gray",
            font=ctk.CTkFont(size=11), anchor='w')
        self._stats_bar.pack(fill='x', pady=(1, 4))

        # Paginate on scroll
        inner_canvas = self._list._parent_canvas
        inner_canvas.bind('<Configure>', lambda _e: self._check_paginate())
        inner_canvas.bind(
            '<MouseWheel>',
            lambda _e: self.after(50, self._check_paginate),
            add='+')

    # ---- Filter + search -------------------------------------------------

    def _is_filtered(self):
        """Returns True only for search queries. Status filter uses SQL and
        is still compatible with push-hooks and pagination."""
        return bool(self._query)

    def _set_filter(self, key):
        if key == self._filter:
            return
        self._filter = key
        self._apply_filter_styles()
        self._reload(force=True)

    def _apply_filter_styles(self):
        for key, btn in self._filter_buttons.items():
            active = key == self._filter
            btn.configure(fg_color=("#1f6aa5", "#1f6aa5") if active else "gray40")

    def _on_search_changed(self):
        if self._search_after_id is not None:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(self.DEBOUNCE_MS, self._run_search)

    def _run_search(self):
        self._search_after_id = None
        new_query = self._search_entry.get().strip()
        if new_query == self._query:
            return
        self._query = new_query
        self._reload(force=True)

    # ---- Data load + render ---------------------------------------------

    def _reload(self, force=False):
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
                    rows = history_db.recent_filtered(
                        status_filter, limit=self.FILTERED_FETCH_LIMIT)
                else:
                    rows = history_db.recent(limit=self.PAGE_SIZE)
            except Exception as e:
                logger.error("History fetch failed: %s", e, exc_info=True)
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

        if bool(self._query):
            self._has_more_in_db = False
        elif self._filter != "all":
            self._has_more_in_db = len(rows) >= self.FILTERED_FETCH_LIMIT
        else:
            self._has_more_in_db = len(rows) >= self.PAGE_SIZE

        if self._expanded_id is not None and not any(
                r['id'] == self._expanded_id for r in rows):
            self._expanded_id = None

        for child in list(self._list.winfo_children()):
            child.destroy()
        self._card_widgets = {}
        self._card_content = {}
        self._card_text_labels = {}
        self._card_top_frames = {}
        self._card_text_widgets = {}
        self._card_correct_buttons = {}
        self._card_cancel_buttons = {}
        self._session_cards = {}
        self._session_headers = {}
        self._render_session_id = None

        # Pre-compute entry counts per session (for collapsed header labels)
        self._session_entry_counts = {}
        for row in rows:
            sid = self._row_session_id(row)
            if sid:
                self._session_entry_counts[sid] = (
                    self._session_entry_counts.get(sid, 0) + 1)

        # When a search query is active, expand all sessions so matches are visible.
        # Otherwise collapse non-current sessions by default, respecting user_expanded.
        if bool(self._query):
            self._collapsed_sessions.clear()
        else:
            current_sid = self._current_session_id or ''
            for row in rows:
                sid = row['session_id'] if 'session_id' in row.keys() else ''
                if sid and sid != current_sid and sid not in self._user_expanded:
                    self._collapsed_sessions.add(sid)
                elif sid in self._user_expanded:
                    self._collapsed_sessions.discard(sid)

        self._render_more()
        self._top_row_id = self._rows[0]['id'] if self._rows else None

        if not rows:
            self._set_status(
                "No matching entries." if (self._query or self._filter != "all")
                else "No history yet. Hold Ctrl+Shift and say something to get started.")
        else:
            self._update_status_count()

        self._refresh_stats_bar()

    def _render_more(self):
        end = min(self._visible_count + self.PAGE_SIZE, len(self._rows))
        for row in self._rows[self._visible_count:end]:
            self._render_card(row)
        self._visible_count = end
        self._update_status_count()

    def _check_paginate(self):
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
        status_filter = self._filter
        self._loading = True
        self._set_status("Loading more...")

        def fetch():
            try:
                if status_filter != "all":
                    page = history_db.recent_filtered(
                        status_filter, limit=self.PAGE_SIZE, offset=offset)
                else:
                    page = history_db.recent(
                        limit=self.PAGE_SIZE, offset=offset)
            except Exception as e:
                logger.error("history page fetch failed: %s", e, exc_info=True)
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
            if self._query:
                self._set_status(f"No matches for '{self._query}'")
            return
        suffix = " (scroll for more)" if (shown < total or self._has_more_in_db) else ""
        if self._query:
            self._set_status(f"{total} results for '{self._query}'{suffix}")
        else:
            self._set_status(f"Showing {shown} entries{suffix}")

    def _set_status(self, text):
        if self._alive:
            try:
                self._status_label.configure(text=text)
            except Exception:
                pass

    def _set_status_timed(self, text, duration_ms=1500):
        self._set_status(text)
        if self._alive:
            try:
                self.after(duration_ms, lambda: self._update_status_count()
                           if self._rows else self._set_status(""))
            except Exception:
                pass

    # ---- Session grouping ------------------------------------------------

    def _row_session_id(self, row):
        try:
            return row['session_id'] or ''
        except (KeyError, IndexError):
            return ''

    def _render_session_header(self, session_id, timestamp_iso, before_target=None):
        is_current = session_id == (self._current_session_id or '')
        is_collapsed = session_id in self._collapsed_sessions
        label_text = self._format_session_label(timestamp_iso, is_current)
        chevron = "v" if not is_collapsed else ">"
        count = self._session_entry_counts.get(session_id, 0)
        count_text = f"  ({count} entries)" if is_collapsed and count else ""

        header = ctk.CTkFrame(
            self._list,
            fg_color="#162030" if is_current else "#202020",
            corner_radius=4,
        )
        if before_target is not None:
            header.pack(fill='x', padx=2, pady=(10, 2), before=before_target)
        else:
            header.pack(fill='x', padx=2, pady=(10, 2))

        chevron_lbl = ctk.CTkLabel(
            header, text=chevron, width=18,
            font=ctk.CTkFont(size=11), text_color="gray60",
        )
        chevron_lbl.pack(side='left', padx=(6, 2), pady=4)
        ctk.CTkLabel(
            header, text=label_text,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#88bbff" if is_current else "gray60",
        ).pack(side='left', pady=4)
        count_lbl = ctk.CTkLabel(
            header, text=count_text,
            font=ctk.CTkFont(size=10), text_color="gray50",
        )
        count_lbl.pack(side='left', pady=4)

        for w in (header, chevron_lbl, count_lbl):
            w.bind('<Button-1>',
                   lambda _e, sid=session_id, cl=chevron_lbl, cnl=count_lbl:
                   self._toggle_session(sid, cl, cnl))

        self._session_headers[session_id] = (header, chevron_lbl, count_lbl)

    @staticmethod
    def _format_session_label(timestamp_iso, is_current):
        prefix = "Current session" if is_current else "Session"
        try:
            dt = datetime.fromisoformat(timestamp_iso)
            today = datetime.now().date()
            diff = (today - dt.date()).days
            if diff == 0:
                when = "Today, " + dt.strftime("%I:%M %p").lstrip('0')
            elif diff == 1:
                when = "Yesterday, " + dt.strftime("%I:%M %p").lstrip('0')
            else:
                when = dt.strftime("%b %d, %I:%M %p").lstrip('0')
        except Exception:
            when = timestamp_iso or "unknown"
        return f"{prefix}: {when}"

    def _toggle_session(self, session_id, chevron_label, count_label=None):
        if session_id in self._collapsed_sessions:
            self._user_expanded.add(session_id)
            self._collapsed_sessions.discard(session_id)
            self._reload(force=True)
        else:
            self._user_expanded.discard(session_id)
            self._collapsed_sessions.add(session_id)
            for card in self._session_cards.get(session_id, []):
                try:
                    card.destroy()
                except Exception:
                    pass
            self._session_cards[session_id] = []
            try:
                chevron_label.configure(text=">")
            except Exception:
                pass
            if count_label is not None:
                count = self._session_entry_counts.get(session_id, 0)
                try:
                    count_label.configure(
                        text=f"  ({count} entries)" if count else "")
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

    def _type_color(self, entry_type):
        return self.TYPE_COLORS.get(entry_type or 'dictation', '#4a8a6a')

    def _type_label_text(self, entry_type):
        return self.TYPE_LABELS.get(entry_type or 'dictation', 'TXT')

    def _confidence_color(self, log_prob):
        if log_prob is None:
            return '#444444'
        if log_prob > -0.5:
            return '#3ad26a'
        if log_prob > -1.0:
            return '#e2c355'
        return '#e25555'

    def _render_card(self, row, prepend=False):
        session_id = self._row_session_id(row)

        before_target = None
        if prepend:
            existing = self._list.winfo_children()
            before_target = existing[0] if existing else None

        # Insert session header when crossing a session boundary
        if session_id != self._render_session_id:
            self._render_session_header(
                session_id, row['timestamp'], before_target=before_target)
            self._render_session_id = session_id
            self._session_cards.setdefault(session_id, [])

        # Skip card body for collapsed sessions (but not while searching)
        if session_id in self._collapsed_sessions and not self._query:
            return

        row_id = row['id']
        try:
            entry_type = row['entry_type'] or 'dictation'
        except (KeyError, IndexError):
            entry_type = 'dictation'
        try:
            log_prob = row['log_prob']
        except (KeyError, IndexError):
            log_prob = None

        type_color = self._type_color(entry_type)
        bg = "#1e1e1e"

        # Card: outer frame + left-border strip + content column
        card_outer = ctk.CTkFrame(self._list, corner_radius=8, fg_color=bg)
        self._card_widgets[row_id] = card_outer
        self._session_cards[session_id].append(card_outer)

        if before_target is not None:
            card_outer.pack(fill='x', padx=4, pady=3, before=before_target)
        else:
            card_outer.pack(fill='x', padx=4, pady=3)

        card_inner = ctk.CTkFrame(card_outer, corner_radius=0, fg_color="transparent")
        card_inner.pack(fill='both', expand=True)

        left_bar = ctk.CTkFrame(card_inner, width=4, corner_radius=0,
                                fg_color=type_color)
        left_bar.pack(side='left', fill='y', padx=(2, 0))
        left_bar.pack_propagate(False)

        content = ctk.CTkFrame(card_inner, fg_color="transparent")
        content.pack(side='left', fill='both', expand=True)
        self._card_content[row_id] = content

        # --- Top row ---
        top = ctk.CTkFrame(content, fg_color="transparent")
        top.pack(fill='x', padx=10, pady=(8, 2))

        # Type badge
        ctk.CTkLabel(
            top, text=self._type_label_text(entry_type),
            font=ctk.CTkFont(size=9, weight="bold"),
            text_color=type_color, width=28, anchor='center',
        ).pack(side='left', padx=(0, 4))

        # Status dot
        sdot = tk.Canvas(top, width=10, height=10,
                         highlightthickness=0, bg=bg)
        sdot.create_oval(1, 1, 9, 9,
                         fill=self._status_color(row['status']), outline='')
        sdot.pack(side='left', padx=(0, 3))

        # Confidence dot
        cdot = tk.Canvas(top, width=8, height=8,
                         highlightthickness=0, bg=bg)
        cdot.create_oval(1, 1, 7, 7,
                         fill=self._confidence_color(log_prob), outline='')
        cdot.pack(side='left', padx=(0, 8))

        preview = self._truncate(row['display_text'] or row['raw_text'])
        if not preview and row['status'] == 'empty':
            preview = "(no speech detected)"
        text_label = ctk.CTkLabel(top, text=preview, anchor='w', justify='left')
        text_label.pack(side='left', fill='x', expand=True)
        self._card_text_labels[row_id] = text_label
        self._card_top_frames[row_id] = top

        if row['duration_ms']:
            ctk.CTkLabel(
                top, text=f"{row['duration_ms'] / 1000:.1f}s",
                text_color="gray", font=ctk.CTkFont(size=11),
            ).pack(side='right', padx=(8, 0))

        # --- Bottom row ---
        meta = ctk.CTkFrame(content, fg_color="transparent")
        meta.pack(fill='x', padx=10, pady=(0, 8))
        ctk.CTkLabel(
            meta, text=self._format_timestamp(row['timestamp']),
            text_color="gray", font=ctk.CTkFont(size=11),
        ).pack(side='left')
        try:
            if row['app_context']:
                ctk.CTkLabel(
                    meta, text=self._truncate(row['app_context'], 60),
                    text_color="gray", font=ctk.CTkFont(size=11),
                ).pack(side='right')
        except (KeyError, IndexError):
            pass

        # --- Event bindings ---
        # single click = copy; double click = expand; right click = context menu
        clickable = [card_outer, card_inner, content, top, meta, text_label,
                     left_bar, sdot, cdot]
        for w in clickable:
            w.bind('<Button-1>',
                   lambda _e, r=row: self._copy_row_quick(r))
            w.bind('<Double-1>',
                   lambda _e, rid=row_id: self._toggle_expand(rid))
            w.bind('<Button-3>',
                   lambda e, r=row: self._show_context_menu(e, r))

        if self._expanded_id == row_id:
            self._build_expanded_body(content, row)

    def _toggle_expand(self, row_id):
        prev = self._expanded_id
        self._expanded_id = None if prev == row_id else row_id
        for rid in {prev, row_id}:
            if rid is None:
                continue
            row = next((r for r in self._rows if r['id'] == rid), None)
            content = self._card_content.get(rid)
            if row is None or content is None:
                continue
            # Clear expanded body refs before destroying widgets
            self._card_text_widgets.pop(rid, None)
            self._card_correct_buttons.pop(rid, None)
            self._card_cancel_buttons.pop(rid, None)
            self._strip_expanded_body(content)
            if rid == self._expanded_id:
                self._build_expanded_body(content, row)

    @staticmethod
    def _strip_expanded_body(content):
        for child in list(content.winfo_children()):
            if getattr(child, '_history_expanded', False):
                child.destroy()

    def _build_expanded_body(self, content, row):
        body = ctk.CTkFrame(content, fg_color="transparent")
        body._history_expanded = True
        body.pack(fill='x', padx=10, pady=(0, 10))

        meta_row = ctk.CTkFrame(body, fg_color="transparent")
        meta_row.pack(fill='x', pady=(0, 6))
        ctk.CTkLabel(
            meta_row, text=f"mode: {row['mode'] or 'hold'}",
            text_color="gray", font=ctk.CTkFont(size=11),
        ).pack(side='left')
        ctk.CTkLabel(
            meta_row, text=f"status: {row['status']}",
            text_color=self._status_color(row['status']),
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side='left', padx=(12, 0))
        try:
            if row['matched_command']:
                ctk.CTkLabel(
                    meta_row, text=f"matched: {row['matched_command']}",
                    text_color="gray", font=ctk.CTkFont(size=11),
                ).pack(side='left', padx=(12, 0))
        except (KeyError, IndexError):
            pass

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
        # Store so _start_inline_correction() can find and enable it
        self._card_text_widgets[row['id']] = text_widget

        actions = ctk.CTkFrame(body, fg_color="transparent")
        actions.pack(fill='x', pady=(8, 0))
        ctk.CTkButton(
            actions, text="Copy", width=80, height=28,
            command=lambda: self._copy_row_quick(row),
        ).pack(side='left')
        ctk.CTkButton(
            actions, text="Delete", width=80, height=28,
            fg_color="#7a2a2a", hover_color="#9a3030",
            command=lambda rid=row['id']: self._delete(rid),
        ).pack(side='left', padx=(8, 0))

        try:
            entry_type = row['entry_type'] or ''
        except (KeyError, IndexError):
            entry_type = ''
        if entry_type == 'command':
            ctk.CTkButton(
                actions, text="Retry", width=80, height=28,
                fg_color="gray40",
                command=lambda r=row: self._retry_row(r),
            ).pack(side='left', padx=(8, 0))

        correct_btn = ctk.CTkButton(
            actions, text="Correct", width=80, height=28,
            fg_color="gray40",
            command=lambda r=row: self._start_inline_correction(r),
        )
        correct_btn.pack(side='left', padx=(8, 0))
        self._card_correct_buttons[row['id']] = correct_btn

        # Cancel button: created now but not packed. Shown only during edit mode.
        cancel_btn = ctk.CTkButton(
            actions, text="Cancel", width=80, height=28,
            fg_color="#7a4a2a", hover_color="#9a5a30",
        )
        self._card_cancel_buttons[row['id']] = cancel_btn

    # ---- Actions ---------------------------------------------------------

    def _copy_row_quick(self, row):
        text = row['display_text'] or row['raw_text'] or ""
        try:
            import pyperclip
            pyperclip.copy(text)
            self._set_status_timed("Copied to clipboard.")
        except Exception as e:
            logger.error("Copy failed: %s", e)
            self._set_status(f"Copy failed: {e}")

    def _show_context_menu(self, event, row):
        try:
            entry_type = row['entry_type'] or ''
        except (KeyError, IndexError):
            entry_type = ''
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Copy", command=lambda: self._copy_row_quick(row))
        if entry_type == 'command':
            menu.add_command(label="Retry", command=lambda: self._retry_row(row))
        else:
            menu.add_command(label="Retry", state='disabled')
        menu.add_command(label="Correct...",
                         command=lambda: self._start_inline_correction(row))
        menu.add_separator()
        menu.add_command(label="Delete", command=lambda: self._delete(row['id']))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _retry_row(self, row):
        text = (row['raw_text'] or row['display_text'] or "").strip()
        if not text:
            self._set_status("Retry: no text to re-submit.")
            return
        try:
            executor = getattr(self.app, 'command_executor', None)
            if executor is None:
                self._set_status("Retry: command executor not available.")
                return
            _result, was_command = executor.process_text(text, self.app)
            msg = "Retry: command executed." if was_command else "Retry: no command matched."
            self._set_status_timed(msg, 2000)
        except Exception as e:
            logger.error("Retry failed: %s", e)
            self._set_status(f"Retry failed: {e}")

    # ---- Inline correction -----------------------------------------------

    _EDIT_BG = '#3a3a3a'   # text widget background while editable
    _READ_BG = '#2b2b2b'   # text widget background when read-only

    def _start_inline_correction(self, row, _retry=False):
        """Enter inline edit mode on the expanded body's tk.Text widget.

        If the card is not yet expanded, expand it first then schedule a
        second call so the expanded body has time to be created.
        """
        row_id = row['id']
        text_widget = self._card_text_widgets.get(row_id)

        if text_widget is None:
            if _retry:
                logger.warning("No text widget for row %s after expand", row_id)
                return
            # Card not expanded — expand it, then retry after Tk renders it
            if self._expanded_id != row_id:
                self._toggle_expand(row_id)
            self.after(80, lambda: self._start_inline_correction(row, _retry=True))
            return

        original = (row['display_text'] or row['raw_text'] or "").strip()

        # Enable editing: make writable and visually distinct
        try:
            text_widget.configure(state='normal', bg=self._EDIT_BG)
            text_widget.focus_set()
            text_widget.tag_add('sel', '1.0', 'end-1c')
            text_widget.mark_set('insert', 'end-1c')
        except Exception as e:
            logger.error("Could not enable text widget for editing: %s", e)
            return

        # Swap "Correct" → "Save" and wire up "Cancel"
        correct_btn = self._card_correct_buttons.get(row_id)
        if correct_btn:
            correct_btn.configure(
                text="Save",
                command=lambda: self._confirm_correction(row, text_widget, original))

        cancel_btn = self._card_cancel_buttons.get(row_id)
        if cancel_btn:
            cancel_btn.configure(
                command=lambda: self._cancel_correction(row, text_widget, original))
            cancel_btn.pack(side='left', padx=(8, 0))

        # Keyboard shortcuts on the text widget
        def _on_return(event):
            self._confirm_correction(row, text_widget, original)
            return 'break'   # prevent newline insertion

        text_widget.bind('<Return>', _on_return)
        text_widget.bind('<Escape>',
                         lambda _e: self._cancel_correction(row, text_widget, original))

    def _confirm_correction(self, row, text_widget, original):
        """Read the edited text, restore the widget to read-only, and save."""
        row_id = row['id']
        try:
            corrected = text_widget.get('1.0', 'end-1c').strip()
        except Exception:
            corrected = original

        self._exit_edit_mode(row_id, text_widget, corrected or original)

        if not corrected:
            return

        # Update the header preview label to show corrected text
        text_label = self._card_text_labels.get(row_id)
        if text_label:
            try:
                text_label.configure(text=self._truncate(corrected))
            except Exception:
                pass

        # Copy to clipboard
        try:
            import pyperclip
            pyperclip.copy(corrected)
        except Exception:
            pass

        # Offer to save mapping to corrections dictionary
        if corrected.strip().lower() != original.strip().lower() and original:
            try:
                if messagebox.askyesno(
                        "Add to dictionary?",
                        f"Save correction to dictionary?\n\n"
                        f"Heard:  {original}\n"
                        f"Should: {corrected}"):
                    self._add_to_corrections(original, corrected)
            except Exception:
                pass

        self._set_status_timed("Correction applied. Text copied to clipboard.")

    def _cancel_correction(self, row, text_widget, original):
        """Restore the original text and exit edit mode without saving."""
        row_id = row['id']
        self._exit_edit_mode(row_id, text_widget, original)

    def _exit_edit_mode(self, row_id, text_widget, display_text):
        """Shared cleanup: restore read-only state, hide Cancel, restore button."""
        try:
            text_widget.configure(state='normal')
            text_widget.delete('1.0', 'end')
            text_widget.insert('1.0', display_text)
            text_widget.configure(state='disabled', bg=self._READ_BG)
            text_widget.unbind('<Return>')
            text_widget.unbind('<Escape>')
        except Exception:
            pass

        correct_btn = self._card_correct_buttons.get(row_id)
        if correct_btn:
            row = next((r for r in self._rows if r['id'] == row_id), None)
            if row is not None:
                correct_btn.configure(
                    text="Correct",
                    command=lambda r=row: self._start_inline_correction(r))

        cancel_btn = self._card_cancel_buttons.get(row_id)
        if cancel_btn:
            try:
                cancel_btn.pack_forget()
            except Exception:
                pass

    def _add_to_corrections(self, original, corrected):
        try:
            from samsara import phonetic_wash as pw
            cur = pw.get_user_corrections()
            cur[original.strip().lower()] = corrected.strip()
            pw.set_user_corrections(cur)
            self._set_status_timed("Correction saved to dictionary.")
        except Exception as e:
            logger.warning("Could not save correction to dictionary: %s", e)

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
                logger.error("History delete failed: %s", e, exc_info=True)
                ok = False
            self._after_safe(lambda: self._on_deleted(row_id, ok))

        threading.Thread(target=do_delete, daemon=True).start()

    def _on_deleted(self, row_id, ok):
        if not ok:
            self._set_status("Delete failed -- check log.")
            return
        card = self._card_widgets.pop(row_id, None)
        self._card_content.pop(row_id, None)
        self._card_text_labels.pop(row_id, None)
        self._card_top_frames.pop(row_id, None)
        self._card_text_widgets.pop(row_id, None)
        self._card_correct_buttons.pop(row_id, None)
        self._card_cancel_buttons.pop(row_id, None)
        if card is not None:
            try:
                card.destroy()
            except Exception:
                pass
        prev_count = len(self._rows)
        self._rows = [r for r in self._rows if r['id'] != row_id]
        if len(self._rows) < prev_count:
            self._visible_count = max(0, min(self._visible_count, len(self._rows)))
        if self._expanded_id == row_id:
            self._expanded_id = None
        self._top_row_id = self._rows[0]['id'] if self._rows else None
        self._update_status_count()

    # ---- Session stats bar -----------------------------------------------

    def _refresh_stats_bar(self):
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None or not self._alive:
            return
        session_id = getattr(history_db, 'session_id', None)
        if not session_id:
            return

        def fetch():
            try:
                stats = history_db.get_session_stats(session_id)
            except Exception:
                stats = {}
            self._after_safe(lambda: self._apply_stats(stats))

        threading.Thread(target=fetch, daemon=True).start()

    def _apply_stats(self, stats):
        if not self._alive:
            return
        successes = stats.get('successes') or 0
        failures = stats.get('failures') or 0
        session_start = stats.get('session_start')
        total = successes + failures
        rate = f"{successes / total * 100:.0f}%" if total > 0 else "n/a"
        start_str = ""
        if session_start:
            try:
                dt = datetime.fromisoformat(session_start)
                start_str = "  ·  Session started " + dt.strftime("%I:%M %p").lstrip('0')
            except Exception:
                pass
        text = (f"{successes} commands this session"
                f"  ·  {failures} failed"
                f"  ·  {rate} success rate"
                f"{start_str}")
        try:
            self._stats_bar.configure(text=text)
        except Exception:
            pass

    # ---- Push-based new-entry hook + visibility-gated poll --------------

    def refresh(self):
        if self._alive:
            self._reload(force=True)

    def on_new_entry(self):
        """Push hook: a new dictation just landed in the DB. No-ops during search."""
        if not self._alive or self._loading or self._is_filtered():
            return
        self._fetch_newer_async()

    def _fetch_newer_async(self):
        history_db = getattr(self.app, 'history_db', None)
        if history_db is None:
            return
        top_id = self._top_row_id
        status_filter = self._filter

        def fetch():
            try:
                if status_filter != "all":
                    latest = history_db.recent_filtered(
                        status_filter, limit=self.PAGE_SIZE)
                else:
                    latest = history_db.recent(limit=self.PAGE_SIZE)
            except Exception as e:
                logger.error("history newer fetch failed: %s", e, exc_info=True)
                return
            new_rows = (list(latest) if top_id is None
                        else [r for r in latest if r['id'] > top_id])
            if not new_rows:
                return
            self._after_safe(lambda: self._prepend_rows(new_rows))

        threading.Thread(target=fetch, daemon=True).start()

    def _prepend_rows(self, rows):
        if not self._alive or not rows:
            return
        existing_ids = {r['id'] for r in self._rows}
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
            self._refresh_stats_bar()

    def _schedule_poll(self):
        if not self._alive:
            return
        try:
            self._poll_after_id = self.after(self.POLL_MS, self._poll_tick)
        except Exception:
            self._poll_after_id = None

    def _poll_tick(self):
        if not self._alive:
            return
        try:
            if self._is_visible() and not self._loading and not self._is_filtered():
                self._fetch_newer_async()
        except Exception as e:
            logger.error("history poll error: %s", e)
        self._schedule_poll()
