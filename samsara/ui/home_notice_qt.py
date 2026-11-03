"""Compact Home version and problem notice widgets."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QSizePolicy, QWidget


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
                         action_callback, report_callback=None):
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
    button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
    button.clicked.connect(lambda: (dismiss_problem_notice(card), action_callback()))
    actions = QWidget(card)
    actions.setObjectName("homeNoticeActions")
    action_layout = QHBoxLayout(actions)
    action_layout.setContentsMargins(0, 0, 0, 0)
    action_layout.setSpacing(grid)
    action_layout.addWidget(button)
    dismiss = button_factory("Dismiss")  # PLACEHOLDER COPY (1038)
    dismiss.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    dismiss.setAccessibleName("Dismiss problem notice")
    dismiss.clicked.connect(lambda: dismiss_problem_notice(card))
    action_layout.addWidget(dismiss)
    if report_callback is not None:
        report = button_factory("Report")
        report.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        report.setObjectName("reportProblemNotice")
        report.clicked.connect(report_callback)
        action_layout.addWidget(report)
    layout.addWidget(actions, alignment=Qt.AlignmentFlag.AlignVCenter)
    card.setVisible(False)
    return card, text, button


def dismiss_problem_notice(card):
    card._dismissed_notice = getattr(card, '_incoming_notice', None)
    messages = getattr(card, '_messages', None)
    if messages is not None:
        messages.dismiss(next_action=True)
        card._announcer.reset()
    card.hide()


def layout_problem_notice(card, text, button, available, grid):
    """Wrap actions when their full labels cannot share the card's width."""
    actions = button.parentWidget()
    row = actions.layout()
    buttons = [row.itemAt(i).widget() for i in range(row.count())]
    for control in buttons:
        control.ensurePolished()
        row.setAlignment(control, Qt.AlignmentFlag.AlignRight)
    required = sum(control.sizeHint().width() for control in buttons)
    required += row.spacing() * (len(buttons) - 1)
    row.setDirection(QBoxLayout.Direction.TopToBottom if required > available - 2 * grid
                     else QBoxLayout.Direction.LeftToRight)
    row.invalidate()
    stack = available < (text.fontMetrics().horizontalAdvance(text.text())
                         + actions.sizeHint().width() + 3 * grid)
    layout = card.layout()
    layout.setDirection(QBoxLayout.Direction.TopToBottom if stack
                        else QBoxLayout.Direction.LeftToRight)
    layout.setAlignment(actions, Qt.AlignmentFlag.AlignRight if stack
                        else Qt.AlignmentFlag.AlignVCenter)


def refresh_problem_notice(card, text_widget, button, notice,
                           dictation_capability, fallback_action_label):
    """Update the shared notice's visible and accessible state."""
    from samsara.message_lifetime import MessageSlot
    if not hasattr(card, '_messages'):
        from samsara.ui.live_surface_accessibility import LiveSurfaceAnnouncer
        card._announcer = LiveSurfaceAnnouncer(card)
        card._messages = MessageSlot()
        card._incoming_notice = None
        card._dismissed_notice = None
    if notice != card._incoming_notice:
        card._incoming_notice = notice
        card._dismissed_notice = None
        if notice is not None:
            kind = 'accent' if notice.action == 'speech_model_loading' else 'warning'
            card._messages.offer(notice.text, kind, None, notice)
        else:
            card._messages.clear()
    current = card._messages.advance()
    notice = current.payload if current else None
    if notice is None or notice == card._dismissed_notice:
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
    text_widget.setToolTip(notice.text)
    text_widget.setAccessibleName(notice.text)
    card.setAccessibleDescription(f"{notice.text} Evidence: {notice.evidence}.")
    label = notice.action_label or fallback_action_label
    button.setText(label)
    button.setAccessibleName(label)
    card.setVisible(True)
    card._announcer.announce_transition(current, notice.text)
    return notice
