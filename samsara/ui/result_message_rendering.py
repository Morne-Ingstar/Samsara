"""Prominent result separators without changing listening-display layout."""
from html import escape
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics, QTextDocument
from samsara.result_messages import CHAIN_CONNECTOR, is_chain_result, is_ava_chain_result
from samsara.ui import theme


def result_html(label, *, ink=None):
    if not is_chain_result(label):
        return escape(str(label))
    colour = ink or theme.TEXT_PRIMARY
    joiner = f' &nbsp;<span style="font-size:125%;font-weight:900;color:{colour}">·</span>&nbsp; '
    return joiner.join(escape(part) for part in str(label).split(CHAIN_CONNECTOR))


def result_document(label, font, ink):
    doc = QTextDocument()
    doc.setDocumentMargin(0)
    doc.setDefaultFont(font)
    doc.setHtml(f'<span style="color:{ink}">{result_html(label, ink=ink)}</span>')
    doc.setTextWidth(-1)
    return doc


def result_width(label, font):
    if is_chain_result(label):
        return int(result_document(label, font, theme.TEXT_ON_ACCENT).idealWidth() + .999)
    return QFontMetrics(font).horizontalAdvance(label)


def paint_result(painter, rect, label, font, ink):
    painter.setPen(ink)
    painter.setFont(font)
    if not is_chain_result(label):
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
        return
    doc = result_document(label, font, ink.name())
    painter.save()
    painter.translate(rect.center().x()-doc.idealWidth()/2, rect.center().y()-doc.size().height()/2)
    doc.drawContents(painter)
    painter.restore()


def set_result_label(widget, label, ink):
    widget.setTextFormat(Qt.TextFormat.RichText if is_chain_result(label) else Qt.TextFormat.PlainText)
    widget.setText(result_html(label, ink=ink) if is_chain_result(label) else label)
