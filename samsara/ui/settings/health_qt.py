"""Health page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
HealthPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from samsara.ui import theme

# Paths below stay relative to settings_qt.py, where this code was written.
from samsara.ui.settings_qt import __file__ as _SETTINGS_QT_FILE
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger


class HealthPage:
    """Methods of the Health settings page (moved from _SettingsWindow)."""

    def _build_health_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        # _fmt_time lives in _refresh_health_log (where it's actually used) --
        # this tab only builds the static command reference + table chrome.

        instant_note = QLabel(
            "This tab is read-only — nothing here goes through Apply && Close. "
            "Health entries are logged automatically by voice command."
        )
        instant_note.setWordWrap(True)
        instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(instant_note)
        layout.addSpacing(8)

        # ---- Section: Voice Command Reference ----
        layout.addWidget(self._section_title("Voice Commands"))
        layout.addSpacing(4)

        commands_info = [
            ("Pain tracking",    [
                ('"pain level 6"', "Log pain 1-10 with timestamp"),
                ('"pain level 4 knees"', "Log pain + body location"),
            ]),
            ("Medication",       [
                ('"took ibuprofen 400mg"', "Log medication + dose"),
                ('"took paracetamol"', "Log medication name only"),
            ]),
            ("Symptoms",         [
                ('"symptom hands are stiff"', "Freeform symptom note"),
                ('"I feel nauseous"', "Also triggers symptom log"),
            ]),
            ("Summaries",        [
                ('"health summary"', "Last 24h pain avg, meds, symptoms"),
                ('"how was my week"', "Spoken 7-day summary."),
                ('"read health log"', "Read today's entries aloud"),
            ]),
            ("Management",       [
                ('"export health log"', "Save to CSV file"),
                ('"undo health log"', "Remove last entry"),
            ]),
        ]

        for group_name, cmds in commands_info:
            group_lbl = QLabel(group_name)
            group_lbl.setStyleSheet(
                f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; font-weight: 600; "
                "margin-top: 6px;"
            )
            layout.addWidget(group_lbl)
            for phrase, desc in cmds:
                row = QHBoxLayout()
                row.setContentsMargins(12, 1, 0, 1)
                p = QLabel(phrase)
                p.setStyleSheet(
                    f"color: {theme.ACCENT}; font-size: {theme.TYPE_MIN}px; "
                    "font-family: 'Consolas', 'Courier New', monospace;"
                )
                p.setMinimumWidth(240)
                d = QLabel(desc)
                d.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
                row.addWidget(p)
                row.addWidget(d, stretch=1)
                layout.addLayout(row)

        layout.addSpacing(20)

        # ---- Section: Today's Log ----
        layout.addWidget(self._section_title("Today's Log"))
        layout.addSpacing(4)

        self._health_count_label = QLabel("Loading...")
        self._health_count_label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(self._health_count_label)
        layout.addSpacing(4)

        self._health_log_table = QTableWidget()
        self._health_log_table.setColumnCount(3)
        self._health_log_table.setHorizontalHeaderLabels(["Time", "Type", "Detail"])
        self._health_log_table.horizontalHeader().setStretchLastSection(True)
        self._health_log_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self._health_log_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self._health_log_table.verticalHeader().setVisible(False)
        self._health_log_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._health_log_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._health_log_table.setMinimumHeight(160)
        self._health_log_table.setMaximumHeight(260)
        layout.addWidget(self._health_log_table)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.setMinimumWidth(100)
        refresh_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        refresh_btn.clicked.connect(self._refresh_health_log)
        # Added directly to the tab's own QVBoxLayout with no sibling
        # sharing the row -- without an explicit alignment, a Minimum
        # (rather than Fixed) horizontal sizePolicy lets a QVBoxLayout
        # stretch this button to the row's full width instead of just its
        # own content width.
        layout.addWidget(refresh_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addSpacing(20)

        # ---- Section: Summary ----
        layout.addWidget(self._section_title("Summary"))
        layout.addSpacing(4)

        self._health_summary_label = QLabel("")
        self._health_summary_label.setWordWrap(True)
        self._health_summary_label.setStyleSheet(
            f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px; line-height: 1.5;"
        )
        layout.addWidget(self._health_summary_label)
        layout.addSpacing(20)

        # ---- Section: Export ----
        layout.addWidget(self._section_title("Export"))
        layout.addSpacing(4)

        export_row = QHBoxLayout()
        export_btn = QPushButton("Export to CSV")
        export_btn.setMinimumWidth(140)
        export_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        export_btn.clicked.connect(self._export_health_csv)
        export_row.addWidget(export_btn)

        open_folder_btn = QPushButton("Open folder")
        open_folder_btn.setMinimumWidth(135)  # sizeHint is 125; a few px of margin
        open_folder_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        open_folder_btn.clicked.connect(self._open_health_folder)
        export_row.addWidget(open_folder_btn)
        export_row.addStretch()
        layout.addLayout(export_row)

        self._health_export_label = QLabel("")
        self._health_export_label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(self._health_export_label)
        layout.addSpacing(20)

        # ---- Section: Medication Dictionary ----
        layout.addWidget(self._section_title("Medication Dictionary"))
        layout.addSpacing(4)

        dict_desc = QLabel(
            "Adds ~100 common medication names to speech recognition so 'took pregabalin' "
            "is heard correctly."
        )
        dict_desc.setWordWrap(True)
        dict_desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(dict_desc)
        layout.addSpacing(6)

        dict_row = QHBoxLayout()
        load_dict_btn = QPushButton("Load Medication Dictionary")
        load_dict_btn.setMinimumWidth(235)  # sizeHint is 224; a few px of margin
        load_dict_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        load_dict_btn.clicked.connect(self._load_medication_dictionary)
        dict_row.addWidget(load_dict_btn)
        dict_row.addStretch()
        layout.addLayout(dict_row)

        self._med_dict_label = QLabel("")
        self._med_dict_label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(self._med_dict_label)

        layout.addStretch()
        scroll.setWidget(container)

        # Populate on build
        self._refresh_health_log()

        return scroll

    def _refresh_health_log(self):
        from datetime import datetime, timezone
        try:
            from samsara import health_store
            entries = health_store.get_today()
        except Exception:
            entries = []

        def _fmt_time(ts):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                local = dt.astimezone()
                h = local.hour % 12 or 12
                ampm = "AM" if local.hour < 12 else "PM"
                return f"{h}:{local.minute:02d} {ampm}"
            except Exception:
                return ts

        # Table
        self._health_log_table.setRowCount(len(entries))
        for i, e in enumerate(entries):
            t = e["type"]
            d = e["data"]
            time_str = _fmt_time(e["timestamp"])
            type_str = t.capitalize()
            if t == "pain":
                detail = f"Level {d.get('level', '?')}"
                if d.get("location"):
                    detail += f" - {d['location']}"
                if d.get("note"):
                    detail += f" ({d['note']})"
            elif t == "medication":
                detail = d.get("name", "unknown")
                if d.get("dose"):
                    detail += f" {d['dose']}"
            elif t == "symptom":
                detail = d.get("text", "")
            else:
                detail = str(d)

            self._health_log_table.setItem(i, 0, QTableWidgetItem(time_str))
            self._health_log_table.setItem(i, 1, QTableWidgetItem(type_str))
            self._health_log_table.setItem(i, 2, QTableWidgetItem(detail))

        n = len(entries)
        self._health_count_label.setText(
            f"{n} entr{'ies' if n != 1 else 'y'} today" if n else "No entries today"
        )

        # Summary
        try:
            from samsara import health_store
            pain_avg = health_store.get_pain_average(hours=24)
            week_avg = health_store.get_pain_average(hours=168)
            today_meds = health_store.get_by_type("medication", hours=24)
            today_symptoms = health_store.get_by_type("symptom", hours=24)
            today_pain = health_store.get_by_type("pain", hours=24)

            parts = []
            if pain_avg is not None:
                levels = [e["data"]["level"] for e in today_pain
                          if "level" in e["data"]]
                lo, hi = min(levels), max(levels)
                parts.append(
                    f"Today's pain: avg {pain_avg}, range {lo}-{hi} "
                    f"({len(levels)} readings)"
                )
            else:
                parts.append("No pain logged today.")

            if today_meds:
                names = {}
                for e in today_meds:
                    name = e["data"].get("name", "unknown")
                    names[name] = names.get(name, 0) + 1
                med_str = ", ".join(
                    f"{n} x{c}" if c > 1 else n for n, c in names.items()
                )
                parts.append(f"Medications: {med_str}")

            if today_symptoms:
                parts.append(f"Symptoms: {len(today_symptoms)} logged")

            if week_avg is not None:
                parts.append(f"Weekly avg pain: {week_avg}")

            self._health_summary_label.setText("\n".join(parts))
        except Exception as ex:
            self._health_summary_label.setText(f"Could not load summary: {ex}")

    def _export_health_csv(self):
        try:
            from samsara import health_store
            path = health_store.export_csv()
            self._health_export_label.setText(f"Exported to {path}")
        except Exception as ex:
            self._health_export_label.setText(f"Export failed: {ex}")

    def _open_health_folder(self):
        import os, subprocess
        from samsara.paths import samsara_home_dir
        folder = str(samsara_home_dir())
        os.makedirs(folder, exist_ok=True)
        try:
            subprocess.Popen(["explorer", folder])
        except Exception as e:
            logger.debug(f"_open_health_folder: {e}")

    def _load_medication_dictionary(self):
        """Load the bundled medication dictionary into the vocabulary."""
        import json
        from pathlib import Path
        try:
            dict_path = Path(_SETTINGS_QT_FILE).parent.parent.parent / "dictionaries" / "medications.json"
            if not dict_path.exists():
                self._med_dict_label.setText("Dictionary file not found.")
                self._med_dict_label.setStyleSheet(f"color: {theme.ERROR}; font-size: {theme.TYPE_MIN}px;")
                return

            with open(dict_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            words = data.get("words", [])
            if not words:
                self._med_dict_label.setText("Dictionary is empty.")
                return

            vt = getattr(self.app, 'voice_training_window', None)
            if vt is None:
                self._med_dict_label.setText("Voice training not available.")
                return

            added = 0
            for word in words:
                w = word.strip()
                if w and w not in vt.custom_vocab:
                    vt.custom_vocab.append(w)
                    added += 1

            if added > 0:
                vt.save_training_data()

            total = len([w for w in words if w.strip() in vt.custom_vocab])
            self._med_dict_label.setText(
                f"Added {added} new terms ({total} total medication words in vocabulary)."
            )
            self._med_dict_label.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_MIN}px;")
        except Exception as ex:
            self._med_dict_label.setText(f"Error: {ex}")
            self._med_dict_label.setStyleSheet(f"color: {theme.ERROR}; font-size: {theme.TYPE_MIN}px;")
