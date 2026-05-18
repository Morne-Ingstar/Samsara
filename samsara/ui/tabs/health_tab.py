"""Health tracking tab for the Samsara Settings window.

Read-only display: voice command reference, today's log, summary stats,
and CSV export. No config to save.
"""

import os
import subprocess
import threading
import tkinter as tk
from datetime import datetime, timezone

import customtkinter as ctk

from samsara import health_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_time(ts: str) -> str:
    """ISO UTC timestamp -> localised '2:30 PM'."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%I:%M %p").lstrip("0")
    except Exception:
        return ts


def _entry_label(entry: dict) -> tuple:
    """Return (type_label, detail_text) for a health entry."""
    etype = entry.get("type", "")
    data  = entry.get("data", {})
    if etype == "pain":
        level    = data.get("level", "?")
        location = data.get("location", "")
        detail   = f"Pain Level {level}" + (f"  |  {location}" if location else "")
        return "Pain", detail
    if etype == "medication":
        name = data.get("name", "unknown")
        dose = data.get("dose", "")
        detail = name + (f" {dose}" if dose else "")
        return "Medication", detail
    if etype == "symptom":
        return "Symptom", data.get("text", "")
    return etype.title(), str(data)


# ---------------------------------------------------------------------------
# Tab class
# ---------------------------------------------------------------------------

TEAL    = "#00CED1"
MUTED   = "#888888"
SUCCESS = "#3ad26a"


class HealthTab:
    """Health tracking tab: voice command reference, today's log, stats, export."""

    # Voice command reference data
    _COMMANDS = [
        ("pain level 6",                   "Log pain level (1-10)"),
        ("pain level 4 knees",             "Log pain with location"),
        ("took ibuprofen 400mg",           "Log medication with dose"),
        ("took paracetamol",               "Log medication (no dose)"),
        ("symptom my hands are stiff",     "Log a symptom in your own words"),
        ("health summary",                 "Hear today's pain average + meds"),
        ("how was my week",                "Hear a 7-day summary"),
        ("read health log",                "Read today's entries aloud"),
        ("export health log",              "Save CSV to ~/.samsara/"),
        ("undo health log",                "Remove the last entry"),
    ]

    def __init__(self, parent_frame, app, settings_window):
        self.parent = parent_frame
        self.app    = app
        self.sw     = settings_window
        self._built = False

        # Widget refs populated during build
        self._log_frame      = None
        self._count_label    = None
        self._stats_frame    = None
        self._export_path_lbl = None

    # ------------------------------------------------------------------
    # Build (generator for staged loading)
    # ------------------------------------------------------------------

    def build(self):
        scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        scroll.pack(fill='both', expand=True)

        # ---- Section 1: Voice Command Reference -----------------------
        self._build_commands_section(scroll)
        self._built = True
        yield

        # ---- Section 2: Today's Log -----------------------------------
        self._build_log_section(scroll)
        yield

        # ---- Section 3: Summary Stats ---------------------------------
        self._build_stats_section(scroll)
        yield

        # ---- Section 4: Export ----------------------------------------
        self._build_export_section(scroll)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_commands_section(self, parent):
        ctk.CTkLabel(
            parent, text="Voice Commands",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(15, 8))

        frame = ctk.CTkFrame(parent, corner_radius=10)
        frame.pack(fill='x', pady=(0, 20))

        ctk.CTkLabel(
            frame,
            text="Say these to Jarvis at any time — no settings needed.",
            text_color=MUTED,
            font=ctk.CTkFont(size=11),
        ).pack(anchor='w', padx=15, pady=(12, 8))

        for cmd, desc in self._COMMANDS:
            row = ctk.CTkFrame(frame, fg_color="transparent")
            row.pack(fill='x', padx=15, pady=1)

            ctk.CTkLabel(
                row,
                text=f'"{cmd}"',
                text_color=TEAL,
                font=ctk.CTkFont(size=12, weight="bold"),
                width=280,
                anchor='w',
            ).pack(side='left')

            ctk.CTkLabel(
                row,
                text=desc,
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
                anchor='w',
            ).pack(side='left', padx=(8, 0))

        ctk.CTkFrame(frame, height=10, fg_color="transparent").pack()

    def _build_log_section(self, parent):
        header_row = ctk.CTkFrame(parent, fg_color="transparent")
        header_row.pack(fill='x', pady=(0, 8))

        ctk.CTkLabel(
            header_row,
            text="Today's Log",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(side='left')

        self._count_label = ctk.CTkLabel(
            header_row,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
        )
        self._count_label.pack(side='left', padx=(12, 0))

        ctk.CTkButton(
            header_row,
            text="Refresh",
            width=80,
            height=28,
            command=self._refresh_log,
        ).pack(side='right')

        outer = ctk.CTkFrame(parent, corner_radius=10)
        outer.pack(fill='x', pady=(0, 20))

        self._log_frame = ctk.CTkScrollableFrame(
            outer, fg_color="transparent", height=160)
        self._log_frame.pack(fill='both', expand=True, padx=4, pady=4)

        self._populate_log()

    def _build_stats_section(self, parent):
        ctk.CTkLabel(
            parent,
            text="Summary Stats",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(0, 8))

        self._stats_frame = ctk.CTkFrame(parent, corner_radius=10)
        self._stats_frame.pack(fill='x', pady=(0, 20))

        self._populate_stats()

    def _build_export_section(self, parent):
        ctk.CTkLabel(
            parent,
            text="Export",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor='w', pady=(0, 8))

        frame = ctk.CTkFrame(parent, corner_radius=10)
        frame.pack(fill='x', pady=(0, 20))

        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill='x', padx=15, pady=(15, 8))

        ctk.CTkButton(
            btn_row,
            text="Export to CSV",
            width=130,
            command=self._do_export,
        ).pack(side='left', padx=(0, 10))

        ctk.CTkButton(
            btn_row,
            text="Open Folder",
            width=110,
            fg_color="transparent",
            border_width=1,
            command=self._open_folder,
        ).pack(side='left')

        self._export_path_lbl = ctk.CTkLabel(
            frame,
            text="",
            text_color=MUTED,
            font=ctk.CTkFont(size=11),
            wraplength=460,
            anchor='w',
        )
        self._export_path_lbl.pack(anchor='w', padx=15, pady=(0, 15))

    # ------------------------------------------------------------------
    # Data helpers
    # ------------------------------------------------------------------

    def _populate_log(self):
        if self._log_frame is None:
            return
        for w in self._log_frame.winfo_children():
            w.destroy()

        entries = health_store.get_today()
        entries_sorted = sorted(entries, key=lambda e: e.get("timestamp", ""))

        if not entries_sorted:
            ctk.CTkLabel(
                self._log_frame,
                text="No entries today.",
                text_color=MUTED,
                font=ctk.CTkFont(size=12),
            ).pack(anchor='w', padx=8, pady=8)
        else:
            for entry in entries_sorted:
                self._add_log_row(entry)

        if self._count_label:
            n = len(entries_sorted)
            self._count_label.configure(
                text=f"{n} entr{'y' if n == 1 else 'ies'} today")

    def _add_log_row(self, entry: dict):
        row = ctk.CTkFrame(self._log_frame, fg_color="transparent")
        row.pack(fill='x', pady=1)

        time_str = _fmt_time(entry.get("timestamp", ""))
        type_label, detail = _entry_label(entry)

        type_colors = {"Pain": "#f87171", "Medication": TEAL, "Symptom": "#fbbf24"}
        tcolor = type_colors.get(type_label, MUTED)

        ctk.CTkLabel(
            row,
            text=time_str,
            text_color=MUTED,
            font=ctk.CTkFont(size=12),
            width=70,
            anchor='w',
        ).pack(side='left')

        ctk.CTkLabel(
            row,
            text=f"|  {type_label}",
            text_color=tcolor,
            font=ctk.CTkFont(size=12, weight="bold"),
            width=110,
            anchor='w',
        ).pack(side='left')

        ctk.CTkLabel(
            row,
            text=f"|  {detail}",
            text_color="#c0c8d4",
            font=ctk.CTkFont(size=12),
            anchor='w',
        ).pack(side='left', fill='x', expand=True)

    def _populate_stats(self):
        if self._stats_frame is None:
            return
        for w in self._stats_frame.winfo_children():
            w.destroy()

        inner = ctk.CTkFrame(self._stats_frame, fg_color="transparent")
        inner.pack(fill='x', padx=15, pady=(12, 15))

        # Today's pain average
        pain_avg = health_store.get_pain_average(hours=24)
        pain_text = (f"Avg pain today:  {pain_avg}/10"
                     if pain_avg is not None else "No pain logged today")
        pain_color = "#f87171" if pain_avg and pain_avg >= 6 else (
            "#fbbf24" if pain_avg and pain_avg >= 4 else SUCCESS)
        ctk.CTkLabel(
            inner,
            text=pain_text,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=pain_color if pain_avg is not None else MUTED,
            anchor='w',
        ).pack(anchor='w', pady=(0, 6))

        # Medications today
        meds = health_store.get_by_type("medication", hours=24)
        if meds:
            names = [e["data"].get("name", "unknown") for e in meds]
            med_text = f"Medications today:  {len(meds)}  ({', '.join(names)})"
        else:
            med_text = "No medications logged today"
        ctk.CTkLabel(
            inner,
            text=med_text,
            font=ctk.CTkFont(size=12),
            text_color=TEAL if meds else MUTED,
            anchor='w',
        ).pack(anchor='w', pady=(0, 6))

        # Symptoms today
        symptoms = health_store.get_by_type("symptom", hours=24)
        symp_text = (f"Symptoms today:  {len(symptoms)}" if symptoms
                     else "No symptoms logged today")
        ctk.CTkLabel(
            inner,
            text=symp_text,
            font=ctk.CTkFont(size=12),
            text_color="#fbbf24" if symptoms else MUTED,
            anchor='w',
        ).pack(anchor='w', pady=(0, 6))

        # Weekly summary
        week_avg = health_store.get_pain_average(hours=168)
        week_meds = health_store.get_by_type("medication", hours=168)
        if week_avg is not None or week_meds:
            parts = []
            if week_avg is not None:
                parts.append(f"avg pain {week_avg}/10")
            if week_meds:
                parts.append(f"{len(week_meds)} medication{'s' if len(week_meds) != 1 else ''}")
            ctk.CTkLabel(
                inner,
                text="This week:  " + ",  ".join(parts),
                font=ctk.CTkFont(size=11),
                text_color=MUTED,
                anchor='w',
            ).pack(anchor='w')

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _refresh_log(self):
        self._populate_log()
        self._populate_stats()

    def _do_export(self):
        def _run():
            try:
                path = health_store.export_csv()
                if self._export_path_lbl:
                    self._export_path_lbl.configure(
                        text=f"Exported to: {path}", text_color=SUCCESS)
            except Exception as e:
                if self._export_path_lbl:
                    self._export_path_lbl.configure(
                        text=f"Export failed: {e}", text_color="#f87171")

        threading.Thread(target=_run, daemon=True).start()

    def _open_folder(self):
        folder = os.path.join(os.path.expanduser("~"), ".samsara")
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            if self._export_path_lbl:
                self._export_path_lbl.configure(
                    text=f"Could not open folder: {e}", text_color="#f87171")

    # ------------------------------------------------------------------
    # No-op save (read-only tab)
    # ------------------------------------------------------------------

    def save(self):
        pass
