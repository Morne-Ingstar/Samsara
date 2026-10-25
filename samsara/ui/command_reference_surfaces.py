"""Compact reference surfaces and single-row disclosure (864)."""
from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtWidgets import QAbstractItemView

from samsara.ui import theme


def row_fill(row, selected=False, hovered=False):
    return (theme.DATA_ROW_SELECTED if selected else theme.DATA_ROW_HOVER if hovered
            else theme.DATA_ROW_ALT if row % 2 else theme.DATA_SURFACE)


class ReferenceDisclosure(QObject):
    """Keep details inside the selected list row, with inert navigation."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.placing = False
        window._list.installEventFilter(self)

    def eventFilter(self, watched, event):
        win = self.window
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Left, Qt.Key.Key_Escape):
                if win._detail_card.isVisible():
                    win._detail_card.dismiss(True)
                    return True
            if key in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = win._list.currentItem()
                if item is not None:
                    win._on_item_clicked(item)
                return True
            if key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End):
                # Let native list navigation choose the row before disclosing it.
                win._detail_card.dismiss()
                from PySide6.QtWidgets import QListWidget
                QListWidget.keyPressEvent(watched, event)
                item = watched.currentItem()
                if item is not None:
                    win._on_item_clicked(item)
                return True
        elif event.type() == QEvent.Type.Resize:
            win._detail_card.dismiss()
        return False

    def place(self, card, height):
        view = self.window._list
        item = view.currentItem()
        if item is None or item.data(Qt.ItemDataRole.UserRole) != card.phrase:
            return False
        self.placing = True
        try:
            delegate = view.itemDelegate()
            delegate.expanded_phrase = card.phrase
            header = delegate.header_height()
            height = min(height, max(1, view.viewport().height() - header))
            delegate.detail_height = height
            view.doItemsLayout()
            view.scrollToItem(item, QAbstractItemView.ScrollHint.EnsureVisible)
            rect = view.visualItemRect(item)
            point = view.viewport().mapTo(card.parentWidget(), rect.topLeft())
            card.setGeometry(QRect(point.x(), point.y() + header, rect.width(), height))
        finally:
            self.placing = False
        return True

    def collapse(self):
        view = self.window._list
        delegate = view.itemDelegate()
        if delegate.expanded_phrase is not None:
            self.placing = True
            try:
                delegate.expanded_phrase = None
                delegate.detail_height = 0
                view.doItemsLayout()
                view.viewport().update()
            finally:
                self.placing = False


def reference_qss():
    return f"""
QMainWindow {{ background: {theme.BG1}; }}
QWidget {{ color: {theme.TEXT_PRIMARY}; font-family: {theme.FONT_FAMILY}; font-size: {theme.TYPE_BODY}px; }}
QListWidget {{
    background: {theme.DATA_SURFACE};
    border: none;
    outline: none;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
}}
QListWidget::item {{ padding: 0; }}
QListWidget::item:hover {{ background: {theme.DATA_ROW_HOVER}; }}
QListWidget::item:selected {{ background: {theme.DATA_ROW_SELECTED}; color: {theme.TEXT_PRIMARY}; }}
QLineEdit {{
    background: {theme.BG2};
    border: 1px solid {theme.BORDER};
    border-radius: 6px;
    color: {theme.TEXT_PRIMARY};
    font-size: {theme.TYPE_BODY}px;
    padding: 6px 10px;
}}
QLineEdit:focus {{ border-color: {theme.ACCENT}; }}
{theme.SCROLLBAR_QSS}
QSlider::groove:horizontal {{
    height: 4px;
    background: {theme.BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {theme.ACCENT};
    border: 2px solid {theme.BG1};
    width: 14px;
    height: 14px;
    margin: -7px 0;
    border-radius: 8px;
}}
QSlider::sub-page:horizontal {{ background: {theme.ACCENT}; border-radius: 1px; }}
QMenu {{ background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; border: 1px solid {theme.BORDER}; }}
QMenu::item:selected {{ background: {theme.DATA_ROW_SELECTED}; color: {theme.TEXT_PRIMARY}; }}
QComboBox {{
    background: {theme.BG2};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    border-radius: 4px;
    padding: 0 8px;
    min-height: 0;
    font-size: {theme.TYPE_BODY}px;
    min-width: 120px;
}}
QComboBox:hover {{ border-color: {theme.ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{
    image: url({theme.ARROW_PATH});
    width: 10px;
    height: 6px;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {theme.BG1};
    color: {theme.TEXT_PRIMARY};
    border: 1px solid {theme.BORDER};
    selection-background-color: {theme.DATA_ROW_SELECTED};
    selection-color: {theme.TEXT_PRIMARY};
    outline: none;
}}
"""
