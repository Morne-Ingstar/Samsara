"""Read-only PySide6 status overlay — active reminders and alarm state.

Public API:
    get_overlay() -> StatusOverlay   # module-level singleton
    StatusOverlay.show(notification_manager, alarm_manager)
    StatusOverlay.hide()
    StatusOverlay.toggle(notification_manager, alarm_manager)

Auto-refreshes every 2 seconds so alarm nagging state stays current.
"""

from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel,
    QMainWindow, QPushButton, QScrollArea,
    QVBoxLayout, QWidget,
)

from samsara.ui import qt_runtime

from samsara.log import get_logger

logger = get_logger(__name__)

# ── Palette (matches task_overlay.py dark theme) ──────────────────────────────

_BG       = "#0A0A0B"
_SURFACE  = "#111114"
_BORDER   = "#2a2a32"
_ACCENT   = "#5EEAD4"
_WARN     = "#f59e0b"
_WARN_BG  = "#1c1400"
_TEXT_PRI = "#E8E8EA"
_TEXT_MUT = "#55555C"

_SS = f"""
QMainWindow, QWidget {{
    background: {_BG};
    color: {_TEXT_PRI};
    font-family: 'Segoe UI', sans-serif;
    font-size: 13px;
}}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: {_BG}; width: 5px; border: none;
}}
QScrollBar::handle:vertical {{
    background: {_BORDER}; border-radius: 2px; min-height: 20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""

# ── Schedule formatting helpers ───────────────────────────────────────────────

def _display_time(hhmm: str) -> str:
    try:
        dt = datetime.strptime(hhmm, "%H:%M")
        h = dt.hour % 12 or 12
        period = "AM" if dt.hour < 12 else "PM"
        if dt.minute:
            return f"{h}:{dt.minute:02d} {period}"
        return f"{h} {period}"
    except ValueError:
        return hhmm


def _schedule_label(schedule: dict) -> str:
    stype = schedule.get("type", "")
    if stype == "interval":
        mins = schedule.get("minutes", 0)
        if mins >= 60 and mins % 60 == 0:
            h = mins // 60
            return f"every {h} hr" if h == 1 else f"every {h} hrs"
        return f"every {mins} min"
    if stype == "times":
        times = schedule.get("times", [])
        return "at " + ", ".join(_display_time(t) for t in times)
    if stype == "once":
        at = schedule.get("at", "")
        try:
            dt = datetime.fromisoformat(at)
            h = dt.hour % 12 or 12
            period = "AM" if dt.hour < 12 else "PM"
            suffix = f":{dt.minute:02d}" if dt.minute else ""
            return f"once at {h}{suffix} {period}"
        except (ValueError, TypeError):
            return "once"
    return ""


# ── Window ────────────────────────────────────────────────────────────────────

class _StatusWindow(QMainWindow):
    _refresh_sig = Signal()

    def __init__(self, notification_manager, alarm_manager):
        super().__init__()
        self._nm = notification_manager
        self._am = alarm_manager

        self.setWindowTitle("Reminders & Alarms")
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setStyleSheet(_SS)
        self.resize(320, 440)
        self.setMinimumSize(260, 180)

        self._scroll = None
        self._setup_chrome()
        self._render()

        self._refresh_sig.connect(self._render)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._render)
        self._timer.start(2000)

    def _setup_chrome(self):
        """Build the fixed chrome (header, nag banner, scroll area, footer)."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(8)

        # Header row
        hdr_row = QHBoxLayout()
        hdr = QLabel("Reminders & Alarms")
        hdr.setFont(QFont("Segoe UI", 14, QFont.Bold))
        hdr.setStyleSheet(f"color: {_ACCENT}; background: transparent;")
        hdr_row.addWidget(hdr, stretch=1)

        close_btn = QPushButton("x")
        close_btn.setFixedSize(22, 22)
        close_btn.setFocusPolicy(Qt.NoFocus)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        close_btn.setStyleSheet(
            f"background: transparent; border: none; color: {_TEXT_MUT};"
            f" font-size: 14px; font-weight: bold; border-radius: 3px;"
        )
        hdr_row.addWidget(close_btn)
        root.addLayout(hdr_row)

        # Divider
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        div.setStyleSheet(f"color: {_BORDER};")
        root.addWidget(div)

        # Nagging alarm banner — shown only when an alarm is sounding
        self._nag_banner = QLabel()
        self._nag_banner.setWordWrap(True)
        self._nag_banner.setStyleSheet(
            f"background: {_WARN_BG}; color: {_WARN};"
            f" border: 1px solid {_WARN}; border-radius: 5px;"
            f" padding: 6px 10px; font-weight: bold; background: {_WARN_BG};"
        )
        self._nag_banner.hide()
        root.addWidget(self._nag_banner)

        # Scrollable content area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet("background: transparent;")
        root.addWidget(self._scroll, stretch=1)

        # Footer
        self._footer = QLabel()
        self._footer.setStyleSheet(
            f"color: {_TEXT_MUT}; font-size: 10px;"
            f" padding-top: 4px; border-top: 1px solid {_BORDER};"
            f" background: transparent;"
        )
        root.addWidget(self._footer)

    @Slot()
    def _render(self):
        # Nagging banner
        nagging = self._am is not None and self._am.is_nagging()
        if nagging:
            alarm = self._am.get_nagging_alarm()
            name = alarm.get("name", "Alarm") if alarm else "Alarm"
            self._nag_banner.setText(
                f"ALARM ACTIVE: {name}"
                f" say ‘complete alarm’ or ‘dismiss alarm’"
            )
            self._nag_banner.show()
        else:
            self._nag_banner.hide()

        # Build fresh content widget — QScrollArea takes ownership of the old one
        content = QWidget()
        content.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)

        # ── Reminders ─────────────────────────────────────────────────────
        self._section(layout, "Reminders")

        reminders = []
        if self._nm is not None:
            try:
                reminders = [
                    r for r in self._nm.get_all_reminders()
                    if r.get("enabled", True)
                ]
            except Exception as e:
                logger.debug(f"_render: {e}")

        if reminders:
            for r in reminders:
                self._item_row(
                    layout,
                    name=r.get("name", "Unnamed"),
                    detail=_schedule_label(r.get("schedule", {})),
                )
        else:
            self._empty(layout, "No active reminders")

        self._gap(layout)

        # ── Alarms ────────────────────────────────────────────────────────
        self._section(layout, "Alarms")

        alarms = []
        if self._am is not None:
            try:
                alarms = list(self._am.items)
            except Exception as e:
                logger.debug(f"_render: {e}")

        if alarms:
            for alarm in alarms:
                alarm_id = alarm.get("id", alarm.get("name", ""))
                name = alarm.get("name", alarm_id)
                interval = alarm.get("interval_minutes", 0)
                enabled = alarm.get("enabled", False)

                streak = 0
                if self._am is not None:
                    try:
                        stats = self._am.get_stats(alarm_id)
                        streak = stats.get("current_streak", 0)
                    except Exception as e:
                        logger.debug(f"_render: {e}")

                state = f"streak {streak}" if enabled else "disabled"
                detail = f"{interval} min  |  {state}"
                self._item_row(
                    layout, name=name, detail=detail,
                    dim=not enabled,
                )
        else:
            self._empty(layout, "No alarms configured")

        layout.addStretch()
        self._scroll.setWidget(content)

        self._footer.setText(f"Updated {datetime.now().strftime('%H:%M:%S')}")

    # ── Layout helpers ────────────────────────────────────────────────────────

    def _section(self, layout: QVBoxLayout, text: str):
        lbl = QLabel(text.upper())
        lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        lbl.setStyleSheet(
            f"color: {_TEXT_MUT}; letter-spacing: 1px;"
            f" padding: 6px 0 2px 0; background: transparent;"
        )
        layout.addWidget(lbl)

    def _item_row(self, layout: QVBoxLayout, name: str, detail: str, dim: bool = False):
        row = QWidget()
        row.setStyleSheet(f"background: {_SURFACE}; border-radius: 4px;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(10, 6, 10, 6)
        hl.setSpacing(8)

        name_color = _TEXT_MUT if dim else _TEXT_PRI
        name_lbl = QLabel(name)
        name_lbl.setStyleSheet(f"color: {name_color}; background: transparent;")
        hl.addWidget(name_lbl, stretch=1)

        detail_lbl = QLabel(detail)
        detail_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        detail_lbl.setStyleSheet(
            f"color: {_TEXT_MUT}; font-size: 11px; background: transparent;"
        )
        hl.addWidget(detail_lbl)

        layout.addWidget(row)

    def _empty(self, layout: QVBoxLayout, text: str):
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {_TEXT_MUT}; font-style: italic;"
            f" padding: 2px 0; background: transparent;"
        )
        layout.addWidget(lbl)

    def _gap(self, layout: QVBoxLayout):
        spacer = QWidget()
        spacer.setFixedHeight(8)
        spacer.setStyleSheet("background: transparent;")
        layout.addWidget(spacer)

    def closeEvent(self, e):
        e.ignore()
        self.hide()


# ── Public wrapper ────────────────────────────────────────────────────────────

class StatusOverlay:
    """Thread-safe wrapper around _StatusWindow. Safe to call from any thread."""

    def __init__(self):
        self._window: "_StatusWindow | None" = None
        self._init_posted = False

    def show(self, notification_manager=None, alarm_manager=None):
        if self._window is not None:
            self._window._nm = notification_manager
            self._window._am = alarm_manager
            self._window._refresh_sig.emit()
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(
                lambda: self._init_window(notification_manager, alarm_manager)
            )

    def hide(self):
        if self._window is not None:
            qt_runtime.post(self._window.hide)

    def toggle(self, notification_manager=None, alarm_manager=None):
        if self._window is not None and self._window.isVisible():
            self.hide()
        else:
            self.show(notification_manager, alarm_manager)

    def _init_window(self, notification_manager, alarm_manager):
        """Runs on the Qt thread."""
        try:
            self._window = _StatusWindow(notification_manager, alarm_manager)
            self._window.show()
        except Exception as exc:
            # Leave _window as None and reset _init_posted so a future
            # show()/toggle() call can retry construction. Without this
            # reset, one failed construction would brick the window
            # forever: _window stays None, _init_posted stays True, and
            # neither the "already exists" nor "first-time init" branch of
            # show() would ever fire again -- the window would vanish and
            # never reopen.
            print(f"[STATUS-OVERLAY] Window init failed, will retry on next show(): {exc}")
            self._init_posted = False


# Module-level singleton — imported by both alarm_commands and reminders plugins
_overlay: "StatusOverlay | None" = None


def get_overlay() -> StatusOverlay:
    global _overlay
    if _overlay is None:
        _overlay = StatusOverlay()
    return _overlay
