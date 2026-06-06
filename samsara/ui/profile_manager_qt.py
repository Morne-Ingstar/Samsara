"""
PySide6 profile manager for Samsara.

Runs on its own daemon thread — same pattern as settings_qt.py / history_qt.py.
Can be opened from the Qt settings General tab or any other caller.
"""

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QScrollArea, QLabel, QComboBox, QPushButton,
    QDialog, QFormLayout, QLineEdit, QFrame, QMessageBox, QFileDialog,
)

from samsara.ui import qt_runtime


_STYLESHEET = """
QMainWindow, QWidget {
    background-color: #0A0A0B;
    color: #E8E8EA;
    font-family: 'Segoe UI', system-ui, sans-serif;
    font-size: 13px;
}
QScrollArea { border: none; background: transparent; }
QLabel { color: #E8E8EA; }
QComboBox {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 7px 10px;
    color: #E8E8EA;
    min-width: 220px;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background-color: #16161A;
    color: #E8E8EA;
    selection-background-color: rgba(94,234,212,0.2);
    border: 1px solid rgba(255,255,255,0.14);
}
QLineEdit {
    background-color: #16161A;
    border: 1px solid rgba(255,255,255,0.14);
    border-radius: 6px;
    padding: 8px 12px;
    color: #E8E8EA;
}
QLineEdit:focus { border-color: rgba(94,234,212,0.5); }
QPushButton {
    background-color: #5EEAD4;
    color: #0A0A0B;
    border: none;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 600;
    font-size: 13px;
}
QPushButton:hover { background-color: #4DD8C2; }
QPushButton[class="secondary"] {
    background-color: transparent;
    color: #8A8A92;
    border: 1px solid rgba(255,255,255,0.14);
}
QPushButton[class="secondary"]:hover {
    background-color: rgba(255,255,255,0.05);
    color: #E8E8EA;
}
QPushButton[class="danger"] {
    background-color: rgba(180,40,40,0.18);
    color: #FF8888;
    border: 1px solid rgba(180,40,40,0.35);
}
QPushButton[class="danger"]:hover { background-color: rgba(180,40,40,0.28); }
QDialog { background-color: #0A0A0B; }
"""


def _sec(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    b.setProperty("class", "secondary")
    b.style().unpolish(b)
    b.style().polish(b)
    if width:
        b.setFixedWidth(width)
    return b


def _danger(label: str, width: int = 0) -> QPushButton:
    b = QPushButton(label)
    b.setProperty("class", "danger")
    b.style().unpolish(b)
    b.style().polish(b)
    if width:
        b.setFixedWidth(width)
    return b


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ProfileManagerQt:
    """Wrapper around _ProfileManagerWindow using the shared qt_runtime."""

    def __init__(self, pm, on_profiles_changed=None):
        self._pm = pm
        self._cb = on_profiles_changed
        self._window = None
        self._init_posted = False

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _ProfileManagerWindow(self._pm, self._cb)
        self._window.show()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class _ProfileManagerWindow(QMainWindow):
    def __init__(self, pm, on_profiles_changed=None):
        super().__init__()
        self._pm = pm
        self._cb = on_profiles_changed

        self.setWindowTitle("Profile Manager")
        self.resize(600, 680)
        self.setMinimumSize(480, 480)
        self.setStyleSheet(_STYLESHEET)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.setCentralWidget(scroll)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        active = self._pm.get_active_profile_names()

        # ---- Voice Training Profiles ----------------------------------------
        layout.addWidget(self._section_title("Voice Training Profiles"))
        layout.addWidget(self._desc(
            "Manage vocabulary, corrections, and prompt profiles"
        ))
        self._dict_active_lbl = QLabel(
            f"Active: {active.get('dictionary') or '(unsaved)'}"
        )
        self._dict_active_lbl.setStyleSheet("color: #5EEAD4; font-size: 12px;")

        dict_profiles = self._pm.list_dictionary_profiles()
        self._dict_combo = self._make_combo(dict_profiles)

        dict_frame = self._make_profile_section(
            active_lbl   = self._dict_active_lbl,
            combo        = self._dict_combo,
            ptype        = 'dictionary',
        )
        layout.addWidget(dict_frame)

        # ---- Command Profiles -----------------------------------------------
        layout.addWidget(self._section_title("Command Profiles"))
        layout.addWidget(self._desc("Manage voice command profiles"))
        self._cmd_active_lbl = QLabel(
            f"Active: {active.get('commands') or '(unsaved)'}"
        )
        self._cmd_active_lbl.setStyleSheet("color: #5EEAD4; font-size: 12px;")

        cmd_profiles = self._pm.list_command_profiles()
        self._cmd_combo = self._make_combo(cmd_profiles)

        cmd_frame = self._make_profile_section(
            active_lbl   = self._cmd_active_lbl,
            combo        = self._cmd_combo,
            ptype        = 'commands',
        )
        layout.addWidget(cmd_frame)

        # ---- Tips -----------------------------------------------------------
        layout.addWidget(self._section_title("Profile Tips"))
        tips = QLabel(
            "Load (Replace): clears current data and loads the selected profile.\n"
            "Load (Merge): adds profile data to your existing data (no duplicates).\n"
            "Save Current: saves your active vocabulary/commands as a new profile.\n"
            "Profiles are stored in the profiles/ folder and can be shared."
        )
        tips.setWordWrap(True)
        tips.setStyleSheet("color: #8A8A92; font-size: 12px; line-height: 1.5;")
        layout.addWidget(tips)

        layout.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

        scroll.setWidget(container)

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _section_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #5EEAD4; font-size: 16px; font-weight: bold;")
        return lbl

    def _desc(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #8A8A92; font-size: 12px;")
        return lbl

    def _make_combo(self, profiles: list) -> QComboBox:
        combo = QComboBox()
        if profiles:
            combo.addItems(profiles)
        else:
            combo.addItem("(no profiles saved)")
            combo.setEnabled(False)
        return combo

    def _make_profile_section(
        self, active_lbl: QLabel, combo: QComboBox, ptype: str
    ) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame{background:#111114;border-radius:8px;"
            "border:1px solid rgba(255,255,255,0.06);}"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        layout.addWidget(active_lbl)

        # Selector row
        sel_row = QHBoxLayout()
        sel_row.setSpacing(8)
        lbl = QLabel("Profile:")
        lbl.setStyleSheet("color: #8A8A92; font-size: 12px;")
        sel_row.addWidget(lbl)
        sel_row.addWidget(combo, stretch=1)
        layout.addLayout(sel_row)

        # Button row 1: Load Replace | Load Merge | Save Current
        row1 = QHBoxLayout()
        row1.setSpacing(6)

        load_rep = QPushButton("Load (Replace)")
        load_rep.clicked.connect(lambda: self._load(ptype, merge=False))
        row1.addWidget(load_rep)

        load_merge = _sec("Load (Merge)")
        load_merge.clicked.connect(lambda: self._load(ptype, merge=True))
        row1.addWidget(load_merge)

        save_btn = _sec("Save Current…")
        save_btn.clicked.connect(lambda: self._save(ptype))
        row1.addWidget(save_btn)

        row1.addStretch()
        layout.addLayout(row1)

        # Button row 2: Import | Export | Delete
        row2 = QHBoxLayout()
        row2.setSpacing(6)

        imp_btn = _sec("Import…")
        imp_btn.clicked.connect(lambda: self._import(ptype))
        row2.addWidget(imp_btn)

        exp_btn = _sec("Export…")
        exp_btn.clicked.connect(lambda: self._export(ptype))
        row2.addWidget(exp_btn)

        del_btn = _danger("Delete")
        del_btn.clicked.connect(lambda: self._delete(ptype))
        row2.addWidget(del_btn)

        row2.addStretch()
        layout.addLayout(row2)

        return frame

    # ------------------------------------------------------------------
    # Combo helpers
    # ------------------------------------------------------------------

    def _combo_for(self, ptype: str) -> QComboBox:
        return self._dict_combo if ptype == 'dictionary' else self._cmd_combo

    def closeEvent(self, e):
        e.ignore()
        self.hide()

    def _selected(self, ptype: str) -> str:
        val = self._combo_for(ptype).currentText()
        return "" if val == "(no profiles saved)" else val

    def _refresh(self):
        active = self._pm.get_active_profile_names()

        dict_profiles = self._pm.list_dictionary_profiles()
        self._refresh_combo(self._dict_combo, dict_profiles)
        self._dict_active_lbl.setText(
            f"Active: {active.get('dictionary') or '(unsaved)'}"
        )

        cmd_profiles = self._pm.list_command_profiles()
        self._refresh_combo(self._cmd_combo, cmd_profiles)
        self._cmd_active_lbl.setText(
            f"Active: {active.get('commands') or '(unsaved)'}"
        )

    @staticmethod
    def _refresh_combo(combo: QComboBox, profiles: list):
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        if profiles:
            combo.addItems(profiles)
            combo.setEnabled(True)
            combo.setCurrentText(current if current in profiles else profiles[0])
        else:
            combo.addItem("(no profiles saved)")
            combo.setEnabled(False)
        combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _load(self, ptype: str, merge: bool):
        name = self._selected(ptype)
        if not name:
            QMessageBox.warning(self, "No Profile", "Select a profile to load.")
            return
        action = "merge with" if merge else "replace"
        reply = QMessageBox.question(
            self, "Confirm Load",
            f"This will {action} your current "
            f"{'vocabulary and corrections' if ptype == 'dictionary' else 'commands'}.\n\nContinue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if ptype == 'dictionary':
            ok, msg = self._pm.load_dictionary_profile(name, merge=merge)
            if ok:
                self._pm.set_active_profile_names(dictionary=name)
        else:
            ok, msg = self._pm.load_command_profile(name, merge=merge)
            if ok:
                self._pm.set_active_profile_names(commands=name)

        if ok:
            self._refresh()
            if self._cb:
                try:
                    self._cb()
                except Exception as exc:
                    print(f"[PROFILES] on_profiles_changed error: {exc}")

        QMessageBox.information(self, "Load Profile", msg)

    def _save(self, ptype: str):
        dlg = _SaveProfileDialog(self, ptype)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        name, description, author = dlg.result

        if ptype == 'dictionary':
            existing = self._pm.list_dictionary_profiles()
        else:
            existing = self._pm.list_command_profiles()

        if name in existing:
            reply = QMessageBox.question(
                self, "Overwrite?",
                f"Profile '{name}' already exists. Overwrite?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
            overwrite = True
        else:
            overwrite = False

        if ptype == 'dictionary':
            ok, msg = self._pm.save_dictionary_profile(
                name, description, author, overwrite=overwrite)
            if ok:
                self._pm.set_active_profile_names(dictionary=name)
        else:
            ok, msg = self._pm.save_command_profile(
                name, description, author, overwrite=overwrite)
            if ok:
                self._pm.set_active_profile_names(commands=name)

        if ok:
            self._refresh()
        QMessageBox.information(self, "Save Profile", msg)

    def _import(self, ptype: str):
        path, _ = QFileDialog.getOpenFileName(
            self,
            f"Import {ptype.title()} Profile",
            "",
            "JSON files (*.json);;All files (*.*)",
        )
        if not path:
            return
        if ptype == 'dictionary':
            ok, msg = self._pm.import_dictionary_profile(path)
        else:
            ok, msg = self._pm.import_command_profile(path)
        if ok:
            self._refresh()
        QMessageBox.information(self, "Import Profile", msg)

    def _export(self, ptype: str):
        name = self._selected(ptype)
        if not name:
            QMessageBox.warning(self, "No Profile", "Select a profile to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export {ptype.title()} Profile",
            f"{name}.json",
            "JSON files (*.json)",
        )
        if not path:
            return
        if ptype == 'dictionary':
            ok, msg = self._pm.export_dictionary_profile(name, path)
        else:
            ok, msg = self._pm.export_command_profile(name, path)
        QMessageBox.information(self, "Export Profile", msg)

    def _delete(self, ptype: str):
        name = self._selected(ptype)
        if not name:
            QMessageBox.warning(self, "No Profile", "Select a profile to delete.")
            return
        reply = QMessageBox.question(
            self, "Confirm Delete",
            f"Permanently delete profile '{name}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if ptype == 'dictionary':
            ok, msg = self._pm.delete_dictionary_profile(name)
        else:
            ok, msg = self._pm.delete_command_profile(name)
        if ok:
            self._refresh()
        QMessageBox.information(self, "Delete Profile", msg)


# ---------------------------------------------------------------------------
# Save-profile dialog
# ---------------------------------------------------------------------------

class _SaveProfileDialog(QDialog):
    def __init__(self, parent, ptype: str):
        super().__init__(parent)
        self.setWindowTitle(f"Save {ptype.title()} Profile")
        self.setMinimumWidth(380)
        self.setStyleSheet(parent.styleSheet())
        self.result = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._name = QLineEdit()
        self._name.setPlaceholderText("Required")
        form.addRow("Profile name *:", self._name)

        self._desc = QLineEdit()
        self._desc.setPlaceholderText("Optional")
        form.addRow("Description:", self._desc)

        self._author = QLineEdit()
        self._author.setPlaceholderText("Optional")
        form.addRow("Author:", self._author)

        layout.addLayout(form)
        layout.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel = QPushButton("Cancel")
        cancel.setFixedWidth(80)
        cancel.setStyleSheet(
            "QPushButton{background:transparent;color:#8A8A92;"
            "border:1px solid rgba(255,255,255,0.14);border-radius:6px;padding:7px 14px;}"
        )
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)

        save = QPushButton("Save")
        save.setFixedWidth(80)
        save.clicked.connect(self._on_save)
        btn_row.addWidget(save)

        layout.addLayout(btn_row)
        self._name.setFocus()

    def _on_save(self):
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Required", "Please enter a profile name.")
            return
        # Strip invalid filename characters
        for ch in '<>:"/\\|?*':
            name = name.replace(ch, "")
        if not name:
            QMessageBox.warning(self, "Invalid Name", "Name contains only invalid characters.")
            return
        self.result = (name, self._desc.text().strip(), self._author.text().strip())
        self.accept()
