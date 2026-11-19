"""Explicit recovery of retained, unverified History text (1275)."""
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy

from samsara.ui.history_row_text import recoverable


def install_row(row_widget, body):
    if not recoverable(row_widget.row):
        return
    actions = QHBoxLayout()
    show = QPushButton('Show', row_widget)  # PLACEHOLDER COPY (1275)
    show.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    row_widget.show_button = show

    def toggle():
        revealed = show.text() == 'Show'
        row_widget.full_text = (' '.join((row_widget.row.get('display_text') or
                                row_widget.row.get('raw_text') or '').split()) if revealed
                                else row_widget._concealed_text)
        row_widget.text_label.setText(row_widget.full_text)
        show.setText('Hide' if revealed else 'Show')
        parent = row_widget.parentWidget()
        while parent is not None:
            if hasattr(parent, '_schedule_relayout'):
                parent._schedule_relayout()
                break
            parent = parent.parentWidget()

    row_widget._concealed_text = row_widget.full_text
    show.clicked.connect(toggle)
    actions.addWidget(show)
    # These targets have no reliable delivery acknowledgement. Recovery is
    # offered even when an older writer called the shortcut send "success".
    paste = QPushButton('Paste again', row_widget)
    paste.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    paste.setToolTip('Prepare this entry, then choose a text window and say “paste that again”.')
    row_widget.paste_again_button = paste

    def prepare():
        parent = row_widget.parentWidget()
        while parent is not None:
            callback = getattr(parent, '_on_paste_again', None)
            if callable(callback):
                callback(row_widget.row)
                return
            parent = parent.parentWidget()

    paste.clicked.connect(prepare)
    actions.addWidget(paste)
    actions.addStretch()
    body.addLayout(actions)
    row_widget._recovery_height = max(show.sizeHint().height(), paste.sizeHint().height()) + 4


def conceal_detail(card, row):
    """Selecting an entry is not an explicit request to reveal its words."""
    button = getattr(card, 'show_button', None)
    if button is None:
        button = card.show_button = QPushButton('Show', card)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        card.layout().insertWidget(1, button)
        button.clicked.connect(lambda: reveal_detail(card))
    card._privacy_row = row
    button.setText('Show')
    button.setVisible(recoverable(row))
    if row.get('privacy_reason'):
        from samsara.ui.history_view import list_text
        card.text.setPlainText(list_text(row))
        card.heard_as.clear()
        card.heard_as.hide()
        card.copy_original_button.hide()
        card.fit_text()


def reveal_detail(card):
    row = card._privacy_row
    if not recoverable(row):
        return
    from samsara.ui.history_view import is_corrected, list_text
    revealed = card.show_button.text() == 'Show'
    card.text.setPlainText((row.get('display_text') or row.get('raw_text')) if revealed else list_text(row))
    card.show_button.setText('Hide' if revealed else 'Show')
    corrected = revealed and is_corrected(row)
    card.heard_as.setText('Heard as: ' + str(row.get('raw_text') or '') if corrected else '')
    card.heard_as.setVisible(corrected)
    card.copy_original_button.setVisible(corrected)
    card.fit_text()


def prepare_paste_again(app, row):
    """Arm an explicitly selected entry; existing paste guards remain intact."""
    if not recoverable(row):
        return
    from samsara import paste_again
    paste_again.remember_history(app, {**row, 'privacy_reason': ''})
    paste_again._note(app, 'Entry ready. Choose a text window, then say “paste that again”.')
