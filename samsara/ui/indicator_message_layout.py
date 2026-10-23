"""Bounded two-line feedback shared by the listening displays."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QTextLayout, QTextOption
from PySide6.QtWidgets import QWidget


def message_lines(message, font, width):
    """Wrap at words (or within long tokens), eliding only beyond line two."""
    width = max(1, int(width))
    text = str(message).replace('\r\n', '\n').replace('\r', '\n')
    from samsara.ui.result_message_rendering import result_width
    # Chain separators use larger bold dots and nonbreaking spacing in the
    # renderer. Reserve that extra width before choosing either line break.
    metrics = QFontMetrics(font)
    extra = max(0, result_width(text.replace('\n', ' '), font)
                - metrics.horizontalAdvance(text.replace('\n', ' ')))
    wrap_width = max(1, width-extra)
    # QTextLayout uses a line separator rather than a paragraph newline.
    layout = QTextLayout(text.replace('\n', '\u2028'), font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    layout.beginLayout()
    lines, consumed = [], 0
    for _ in range(2):
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(wrap_width)
        start, consumed = line.textStart(), line.textStart() + line.textLength()
        lines.append(text[start:consumed].rstrip('\n '))
    layout.endLayout()
    elided = consumed < len(text)
    if elided:
        # A third explicit line may be short enough to fit if flattened; it
        # is still omitted, so always show the ellipsis on the second line.
        remainder = lines[-1].rstrip() + '…'
        lines[-1] = metrics.elidedText(remainder, Qt.TextElideMode.ElideRight, wrap_width)
    return lines or [''], elided


def message_height(message, font, width, *, padding=12):
    lines, _ = message_lines(message, font, width)
    return len(lines) * message_line_spacing(message, font) + padding


def message_line_spacing(message, font):
    from samsara.result_messages import is_chain_result
    return QFontMetrics(font).lineSpacing() + (4 if is_chain_result(message) else 0)


class MessageHoverWindow(QWidget):
    """Compact's message accepts hover/click without taking keyboard focus."""
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter
        chip = self.owner._active_chip()
        if chip:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            self.owner._paint_chip(painter, QRectF(self.rect()), *chip)
            painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.owner.dismiss_error()
        event.accept()
