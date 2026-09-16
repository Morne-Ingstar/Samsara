"""The Snippets page (queue 121) -- named text, managed without a mouse.

The store is samsara/snippets.py; this is the place to see what is in it and
change it. It follows samsara/ui/memos_qt.py's shape deliberately rather than
inventing a second convention for a hub list page.

Operable with no mouse, by design, not by accident:
  * Every control is a real focusable widget in a deliberate tab order, and
    every one carries its shortcut in its own visible label -- never a
    shortcut a user had to be told somewhere else.
  * The list is a QListWidget, so Up/Down/Home/End work as they do
    everywhere, and the actions apply to the selected row.
  * Ctrl+F reaches search from anywhere on the page; Ctrl+N starts a new
    snippet; Enter in the name box saves.
  * Delete confirms INLINE, with two ordinary focusable buttons -- never a
    modal dialog, which blocks the keyboard path and the automation path.
  * Type sizes are theme tokens only (queue 83's floor); nothing here is
    smaller than the body size the rest of the hub reads at.

The editor is deliberately one name field and one text box. Placeholders,
variables, per-app snippets and rich text are out of scope (queue 121) --
plain text, typed verbatim.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from samsara import snippets
from samsara.log import get_logger
from samsara.ui import theme

logger = get_logger(__name__)

#: The nav label. Defined HERE rather than in home_qt so the page owns its
#: own name, the way the store owns its own paths.
SNIPPETS = "Snippets"

TITLE = SNIPPETS
SEARCH_LABEL = "Search snippets (Ctrl+F)"
SEARCH_PLACEHOLDER = "Search a name or the text..."
NAME_LABEL = "Name (say \"insert\" + this)"
NAME_PLACEHOLDER = "sign off"
TEXT_LABEL = "Text to type"
TEXT_PLACEHOLDER = "Kind regards,\nMorne"
NEW = "New (Ctrl+N)"
SAVE = "Save (Enter)"
CANCEL_EDIT = "Cancel (Esc)"
DELETE = "Delete (Del)"
CONFIRM_DELETE = "Delete for good?"
CANCEL = "Cancel"
NO_SNIPPETS = ("No snippets yet. Dictate something, then say "
               "\"save snippet\" and a name -- or press Ctrl+N.")
NO_MATCHES = "No snippet matches that search."
SAVED = "Saved."
DELETED = "Deleted."
MIN_TARGET = 44
GRID = 12
#: Three list rows (a name line plus a preview line each) at the 900x650
#: default, with the editor open. Measured, not guessed -- see ui_proof/121.
_LIST_MIN_H = 150
#: The editor is bounded so it cannot starve the list. A longer snippet
#: scrolls inside the box; the PAGE still never scrolls.
_TEXT_MIN_H = MIN_TARGET + GRID * 2
_TEXT_MAX_H = MIN_TARGET * 3
MIDDLE_DOT = chr(0xB7)
EM_DASH = chr(0x2014)


def _preview(text: str, limit: int = 80) -> str:
    """One line of a snippet's text for the list row."""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + chr(0x2026)


class SnippetsPage(QWidget):
    """The hub's Snippets page. `home` overrides the store root for tests."""

    def __init__(self, app, parent=None, home=None):
        super().__init__(parent)
        self._app = app
        self._home = home
        self._records: list = []
        self._filtered: list = []
        self._editing_id: Optional[str] = None
        self._build()
        self.reload()

    # ---- Construction ----------------------------------------------------

    def _build(self):
        self.setStyleSheet(
            f"QWidget {{ background: {theme.BG1}; }}"
            f"QLineEdit, QPlainTextEdit {{ background: {theme.BG2}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; padding: 8px 10px;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QLineEdit:focus, QPlainTextEdit:focus {{ border: 2px solid {theme.ACCENT}; }}"
            f"QListWidget {{ background: {theme.BG0}; color: {theme.TEXT_PRIMARY};"
            f" border: 1px solid {theme.BORDER}; border-radius: 8px;"
            f" font-size: {theme.TYPE_BODY}px; }}"
            f"QListWidget::item {{ padding: 10px 8px; }}"
            f"QListWidget::item:selected {{ background: {theme.BG2};"
            f" color: {theme.TEXT_PRIMARY}; }}"
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(GRID, GRID, GRID, GRID)
        lay.setSpacing(GRID)

        # Search row. The label carries the shortcut.
        search_row = QHBoxLayout()
        search_row.setSpacing(GRID)
        search_label = self._label(SEARCH_LABEL)
        search_row.addWidget(search_label)
        self._search = QLineEdit()
        self._search.setPlaceholderText(SEARCH_PLACEHOLDER)
        self._search.setAccessibleName(SEARCH_LABEL)
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumHeight(MIN_TARGET)
        self._search.textChanged.connect(self._apply_filter)
        search_label.setBuddy(self._search)
        search_row.addWidget(self._search, stretch=1)
        self._new_btn = self._button(NEW, self.start_new)
        search_row.addWidget(self._new_btn)
        lay.addLayout(search_row)

        self._list = QListWidget()
        self._list.setAccessibleName("Snippets")
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setWordWrap(True)
        # Rows wrap; they never scroll sideways. A horizontal scrollbar in a
        # list of sentences is a way to hide half of one.
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The list is the page: it takes the slack, and it is the only thing
        # here allowed to scroll. Three rows fit at the 900x650 default even
        # with the editor open, so the page never has to.
        self._list.setMinimumHeight(_LIST_MIN_H)
        self._list.currentRowChanged.connect(self._on_selection)
        self._list.itemActivated.connect(lambda _item: self.start_edit())
        lay.addWidget(self._list, stretch=1)

        # The editor. One name, one text box: that is the whole record.
        name_row = QHBoxLayout()
        name_row.setSpacing(GRID)
        name_label = self._label(NAME_LABEL)
        name_row.addWidget(name_label)
        self._name = QLineEdit()
        self._name.setPlaceholderText(NAME_PLACEHOLDER)
        self._name.setAccessibleName(NAME_LABEL)
        self._name.setMinimumHeight(MIN_TARGET)
        self._name.returnPressed.connect(self.save_edit)
        name_label.setBuddy(self._name)
        name_row.addWidget(self._name, stretch=1)
        lay.addLayout(name_row)

        text_label = self._label(TEXT_LABEL)
        lay.addWidget(text_label)
        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(TEXT_PLACEHOLDER)
        self._text.setAccessibleName(TEXT_LABEL)
        # Bounded, so the editor cannot starve the list at 900x650. Longer
        # snippets scroll inside this box, which is what a text box is for.
        self._text.setMinimumHeight(_TEXT_MIN_H)
        self._text.setMaximumHeight(_TEXT_MAX_H)
        text_label.setBuddy(self._text)
        lay.addWidget(self._text)

        # Actions. Every one is a button with its key in the visible label.
        actions = QHBoxLayout()
        actions.setSpacing(GRID)
        self._save_btn = self._button(SAVE, self.save_edit)
        self._cancel_edit_btn = self._button(CANCEL_EDIT, self.cancel_edit)
        self._delete_btn = self._button(DELETE, self.request_delete)
        self._confirm_btn = self._button(CONFIRM_DELETE, self.confirm_delete, danger=True)
        self._cancel_btn = self._button(CANCEL, self.cancel_delete)
        self._confirm_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        for button in (self._save_btn, self._cancel_edit_btn, self._delete_btn,
                       self._confirm_btn, self._cancel_btn):
            actions.addWidget(button)
        actions.addStretch(1)
        lay.addLayout(actions)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Snippet status")
        self._status.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        lay.addWidget(self._status)

        chain = [self._search, self._new_btn, self._list, self._name, self._text,
                 self._save_btn, self._cancel_edit_btn, self._delete_btn,
                 self._confirm_btn, self._cancel_btn]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)
        self._tab_chain = chain

        # Shortcuts a user can reach from anywhere on the page. Each one is
        # also written on the control it drives, so nothing here is secret.
        self._shortcuts = [
            self._shortcut("Ctrl+F", lambda: (self._search.setFocus(),
                                              self._search.selectAll())),
            self._shortcut("Ctrl+N", self.start_new),
            self._shortcut("Del", self.request_delete),
            self._shortcut("Esc", self.cancel_edit),
        ]

    def _label(self, text) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(
            f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;"
            " background: transparent;")
        return label

    def _button(self, text, slot, danger=False) -> QPushButton:
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.setMinimumHeight(MIN_TARGET)
        button.setMinimumWidth(MIN_TARGET)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        colour = theme.ERROR if danger else theme.TEXT_PRIMARY
        button.setStyleSheet(
            f"QPushButton {{ background: {theme.BG2}; color: {colour};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px;"
            f" padding: 8px 14px; font-size: {theme.TYPE_BODY}px; }}"
            f"QPushButton:hover {{ border-color: {theme.ACCENT}; }}"
            f"QPushButton:focus {{ border: 2px solid {theme.ACCENT}; }}"
            f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED};"
            f" border-color: {theme.BORDER_FAINT}; }}"
        )
        button.clicked.connect(slot)
        return button

    def _shortcut(self, sequence, slot) -> QShortcut:
        shortcut = QShortcut(QKeySequence(sequence), self)
        shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        shortcut.activated.connect(slot)
        return shortcut

    # ---- Data ------------------------------------------------------------

    def reload(self):
        """Re-read the store. Cheap enough to call whenever the page opens."""
        try:
            self._records = snippets.load_snippets(self._home)
        except Exception as exc:
            logger.warning("[SNIPPET] could not read the store: %s", exc)
            self._records = []
        self._apply_filter()

    def _apply_filter(self):
        needle = self._search.text() if hasattr(self, "_search") else ""
        self._filtered = snippets.search_snippets(self._records, needle)
        self._list.clear()
        for record in self._filtered:
            used = int(record.get("use_count") or 0)
            suffix = f"  {MIDDLE_DOT} used {used}" if used else ""
            item = QListWidgetItem(
                f"{record.get('name', '')}{suffix}\n{_preview(record.get('text', ''))}")
            item.setData(Qt.ItemDataRole.UserRole, record.get("id"))
            item.setToolTip(record.get("text", ""))
            self._list.addItem(item)
        if not self._records:
            self._status.setText(NO_SNIPPETS)
        elif not self._filtered:
            self._status.setText(NO_MATCHES)
        else:
            self._status.setText(
                f"{len(self._filtered)} of {len(self._records)} snippet"
                f"{'s' if len(self._records) != 1 else ''}")
        self._cancel_delete()
        self._refresh_actions()

    def selected(self) -> Optional[dict]:
        row = self._list.currentRow()
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        return None

    def _on_selection(self, _row=None):
        record = self.selected()
        self._cancel_delete()
        if record is not None:
            self._editing_id = record.get("id")
            self._name.setText(record.get("name", ""))
            self._text.setPlainText(record.get("text", ""))
        self._refresh_actions()

    def _refresh_actions(self):
        has_selection = self.selected() is not None
        self._delete_btn.setEnabled(has_selection and not self._confirm_btn.isVisible())
        self._save_btn.setEnabled(True)

    # ---- Editing ---------------------------------------------------------

    def start_new(self):
        """Clear the editor for a new snippet and put the cursor in the name."""
        self._editing_id = None
        self._list.setCurrentRow(-1)
        self._name.clear()
        self._text.clear()
        self._cancel_delete()
        self._status.setText("New snippet: name it, type the text, press Enter.")
        self._name.setFocus()
        self._refresh_actions()

    def start_edit(self):
        """Open the selected snippet in the editor (Enter on the list)."""
        if self.selected() is not None:
            self._name.setFocus()
            self._name.selectAll()

    def cancel_edit(self):
        """Esc: drop unsaved edits and go back to what is stored."""
        if self._confirm_btn.isVisible():
            self._cancel_delete()
            return
        self._on_selection()
        self._status.setText("")

    def save_edit(self):
        """Create or update, reporting a refusal in words rather than
        failing silently."""
        name = self._name.text().strip()
        text = self._text.toPlainText()
        try:
            if self._editing_id:
                snippets.update_snippet(self._editing_id, name=name, text=text,
                                        home=self._home, app=self._app)
            else:
                record = snippets.add_snippet(name, text, home=self._home, app=self._app)
                self._editing_id = record["id"]
        except snippets.SnippetError as exc:
            self._status.setText(str(exc))
            self._name.setFocus()
            return False
        except Exception as exc:
            logger.warning("[SNIPPET] save failed: %s", exc)
            self._status.setText(f"Could not save: {exc}")
            return False
        self.reload()
        self._select_id(self._editing_id)
        self._status.setText(f'{SAVED} Say "insert {name}" to use it.')
        return True

    def _select_id(self, snippet_id):
        for row in range(self._list.count()):
            if self._list.item(row).data(Qt.ItemDataRole.UserRole) == snippet_id:
                self._list.setCurrentRow(row)
                return

    # ---- Delete (inline confirm, never a modal) --------------------------

    def request_delete(self):
        if self.selected() is None:
            return
        self._delete_btn.setVisible(False)
        self._confirm_btn.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._status.setText(f'Delete "{self.selected().get("name", "")}"? This cannot be undone.')
        self._confirm_btn.setFocus()

    def confirm_delete(self):
        record = self.selected()
        if record is None:
            self._cancel_delete()
            return False
        try:
            snippets.delete_snippet(record.get("id"), home=self._home)
        except Exception as exc:
            logger.warning("[SNIPPET] delete failed: %s", exc)
            self._status.setText(f"Could not delete: {exc}")
            return False
        self._editing_id = None
        self._name.clear()
        self._text.clear()
        self.reload()
        self._status.setText(DELETED)
        self._list.setFocus()
        return True

    def cancel_delete(self):
        self._cancel_delete()
        self._status.setText("")
        self._delete_btn.setFocus()

    def _cancel_delete(self):
        self._delete_btn.setVisible(True)
        self._confirm_btn.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._delete_btn.setEnabled(self.selected() is not None)


def overflowing_widgets(root: QWidget) -> list:
    """Visible labels and buttons whose preferred size does not fit the rect
    the layout gave them -- the scaling gate, the same shape home_qt uses."""
    bad = []
    for widget in root.findChildren(QWidget):
        if not widget.isVisibleTo(root) or widget.width() <= 0 or widget.height() <= 0:
            continue
        if not isinstance(widget, (QLabel, QPushButton)):
            continue
        name = widget.accessibleName() or widget.objectName() or type(widget).__name__
        if isinstance(widget, QLabel) and widget.wordWrap():
            need = widget.heightForWidth(widget.width())
            if need > widget.height() + 1:
                bad.append(f"{name}: needs h={need} has h={widget.height()}")
            continue
        hint = widget.sizeHint()
        if hint.width() > widget.width() + 1 or hint.height() > widget.height() + 1:
            bad.append(f"{name}: needs {hint.width()}x{hint.height()} "
                       f"has {widget.width()}x{widget.height()}")
    return bad
