"""Queue 314 Phase-1 visual candidates; not wired into production yet.

The module deliberately contains all candidate scale and QSS decisions in one
place.  Phase 2 will select one treatment and connect its stable tokens through
``theme.py``; importing this module alone has no effect on a live window.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

# Six reading sizes, four weights, and an eight-step 8px spacing grid.
TYPE_SCALE = {"caption": 14, "body": 16, "label": 18, "heading": 20, "title": 28, "display": 36}
WEIGHT_SCALE = {"regular": 400, "medium": 500, "semibold": 600, "bold": 700}
SPACE_SCALE = (0, 8, 16, 24, 32, 40, 48, 64)
TAP_TARGET = 44


@dataclass(frozen=True)
class Treatment:
    key: str
    cards: bool = False
    header: bool = False


A = Treatment("A")
B = Treatment("B", cards=True)
C = Treatment("C", cards=True, header=True)
TREATMENTS = {item.key: item for item in (A, B, C)}


def _theme():
    """Import lazily so theme.py can own and re-export this scale."""
    from samsara.ui import theme
    return theme


def stylesheet(treatment: Treatment) -> str:
    """Return additive QSS for one visual candidate using theme tokens only."""
    theme = _theme()
    base = f"""
        QWidget {{ font-family: {theme.FONT_FAMILY}; font-size: {TYPE_SCALE['body']}px; }}
        QLabel[role='title'] {{ font-size: {TYPE_SCALE['title']}px; font-weight: {WEIGHT_SCALE['semibold']}; }}
        QLabel[role='label'] {{ font-size: {TYPE_SCALE['label']}px; font-weight: {WEIGHT_SCALE['semibold']}; }}
        QLabel[role='description'] {{ font-size: {TYPE_SCALE['body']}px; line-height: 1.5; color: {theme.TEXT_SECONDARY}; }}
        QPushButton, QLineEdit, QComboBox, QAbstractSpinBox {{ min-height: {TAP_TARGET}px; font-weight: {WEIGHT_SCALE['medium']}; }}
        QCheckBox {{ min-height: {TAP_TARGET}px; spacing: {SPACE_SCALE[2]}px; }}
        QCheckBox::indicator {{ width: 24px; height: 24px; }}
        QPushButton {{ border-radius: 8px; padding: 0 {SPACE_SCALE[2]}px; }}
    """
    if not treatment.cards:
        return base
    cards = f"""
        QFrame[role='row'] {{
            background: {theme.BG1}; border: 1px solid {theme.BORDER_FAINT};
            border-radius: 12px; margin: {SPACE_SCALE[1]}px 0; padding: {SPACE_SCALE[1]}px;
        }}
        QWidget[designCardContent='true'] {{ background: transparent; }}
        QFrame[role='row']:hover {{ border-color: {theme.ACCENT}; }}
        QFrame#helpPanel {{ border-radius: 12px; padding: {SPACE_SCALE[1]}px; }}
    """
    if not treatment.header:
        return base + cards
    return base + cards + f"""
        QFrame#designCandidateHeader {{ background: {theme.BG1}; border-bottom: 2px solid {theme.ACCENT}; }}
        QLabel#designCandidateGlyph {{ color: {theme.ACCENT}; font-size: {TYPE_SCALE['heading']}px; font-weight: {WEIGHT_SCALE['bold']}; }}
    """


def _button_qss(primary: bool) -> str:
    """One unmistakable button language for the Phase-1 comparison."""
    theme = _theme()
    if primary:
        return f"""
            QPushButton {{ background: {theme.ACCENT}; color: {theme.TEXT_ON_ACCENT};
                border: 1px solid {theme.ACCENT}; border-radius: 8px;
                min-height: {TAP_TARGET}px; padding: 0 {SPACE_SCALE[2]}px;
                font-weight: {WEIGHT_SCALE['semibold']}; }}
            QPushButton:hover {{ background: {theme.ACCENT_HOVER}; border-color: {theme.ACCENT_HOVER}; }}
            QPushButton:pressed {{ background: {theme.ACCENT_PRESSED}; border-color: {theme.ACCENT_PRESSED}; }}
            QPushButton:disabled {{ background: {theme.ACCENT_DISABLED}; color: {theme.TEXT_DISABLED};
                border-color: {theme.ACCENT_DISABLED}; }}
        """
    return f"""
        QPushButton {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};
            border: 1px solid {theme.BORDER}; border-radius: 8px;
            min-height: {TAP_TARGET}px; padding: 0 {SPACE_SCALE[2]}px;
            font-weight: {WEIGHT_SCALE['medium']}; }}
        QPushButton:hover {{ background: {theme.HOVER_WASH}; border-color: {theme.ACCENT}; }}
        QPushButton:pressed {{ background: {theme.PRESS_WASH}; border-color: {theme.ACCENT}; }}
        QPushButton:disabled {{ color: {theme.TEXT_DISABLED}; border-color: {theme.BORDER_FAINT}; }}
    """


def apply(widget: QWidget, candidate: str) -> None:
    """Apply an additive candidate stylesheet to this real, isolated window."""
    theme = _theme()
    treatment = TREATMENTS[candidate]
    if treatment.cards:
        for row in widget.findChildren(QFrame):
            if row.property("role") == "row":
                for content in (getattr(row, "text", None), getattr(row, "value", None)):
                    if content is not None:
                        content.setProperty("designCardContent", True)
    widget.setStyleSheet(widget.styleSheet() + "\n" + stylesheet(treatment))
    # Existing guide sheets deliberately make Skip a low-emphasis ghost link.
    # For this pass, use a conventional bordered secondary button instead.
    from PySide6.QtWidgets import QPushButton  # local: no import-side effect
    for button in widget.findChildren(QPushButton):
        if button.objectName() == "helpButton":
            continue
        inline = button.styleSheet()
        is_primary = (
            button.objectName() in {"apply", "primary"}
            or button.property("class") == "primary"
            or f"background:{theme.ACCENT}" in inline.replace(" ", "")
        )
        button.setStyleSheet(_button_qss(is_primary))
    if treatment.header:
        _add_header_mark(widget)


def _add_header_mark(widget: QWidget) -> None:
    """Add one decorative, non-focusable mark without changing source copy."""
    if widget.findChild(QWidget, "designCandidateHeader"):
        return
    root = widget.layout()
    if not isinstance(root, QVBoxLayout):
        return
    header = QFrame(widget)
    header.setObjectName("designCandidateHeader")
    header.setFixedHeight(TAP_TARGET)
    glyph = QLabel("✦", header)
    glyph.setObjectName("designCandidateGlyph")
    glyph.setAccessibleName("Samsara guide mark")
    glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
    layout = QVBoxLayout(header)
    layout.setContentsMargins(SPACE_SCALE[2], 0, SPACE_SCALE[2], 0)
    layout.addWidget(glyph)
    root.insertWidget(0, header)
