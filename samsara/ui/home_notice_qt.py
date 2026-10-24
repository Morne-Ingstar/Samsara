"""Compact Home version and problem notice widgets."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QSizePolicy, QWidget


def build_version_row(version_label, grid: int) -> QWidget:
    """Place the running version inside the Home window's horizontal bounds."""
    row = QWidget()
    row.setStyleSheet("background: transparent;")
    layout = QHBoxLayout(row)
    layout.setContentsMargins(grid, 0, grid, 0)
    layout.setSpacing(0)
    layout.addStretch(1)
    layout.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignRight)
    return row


def build_problem_notice(grid, card_factory, label_factory, button_factory,
                         action_callback):
    """Build the shared card used for all known Home problem states."""
    card = card_factory()
    card.setObjectName("homeNotice")
    card.setAccessibleName("Problem notice")
    layout = QHBoxLayout(card)
    layout.setContentsMargins(grid, grid, grid, grid)
    layout.setSpacing(grid)
    text = label_factory("", role="note", wrap=True)
    text.setAccessibleName("Problem")
    text.setSizePolicy(QSizePolicy.Policy.Ignored,
                       QSizePolicy.Policy.Preferred)
    layout.addWidget(text, stretch=1)
    button = button_factory("Voice troubleshooting", primary=True)
    button.clicked.connect(action_callback)
    layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignVCenter)
    card.setVisible(False)
    return card, text, button


def refresh_problem_notice(card, text_widget, button, notice,
                           dictation_capability, fallback_action_label):
    """Update the shared notice's visible and accessible state."""
    if notice is None:
        card.setVisible(False)
        text_widget.setText("")
        card.setAccessibleDescription("")
        return
    visible_text = notice.text
    if (notice.capability == dictation_capability and
            notice.text.startswith("Your selected device is unavailable.")):
        visible_text = (
            "Mic unavailable. Choose another in Settings > Audio > "
            "Microphone. Dictation unavailable.")  # PLACEHOLDER COPY (838b)
    text_widget.setText(visible_text)
    card.setAccessibleDescription(f"{notice.text} Evidence: {notice.evidence}.")
    label = notice.action_label or fallback_action_label
    button.setText(label)
    button.setAccessibleName(label)
    card.setVisible(True)
