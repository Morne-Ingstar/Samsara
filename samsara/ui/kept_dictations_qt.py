"""Settings controls and the private History > Kept reviewer."""
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QListWidget, QListWidgetItem, QPlainTextEdit, QCheckBox,
    QSpinBox, QSizePolicy, QDialog, QFileDialog)

from samsara.kept_dictations import store_for
from samsara.ui import theme


def _button(text, callback):
    button = QPushButton(text)
    theme.hub_control(button)
    button.clicked.connect(callback)
    return button


def add_settings(owner, page, name):
    from samsara.ui.settings.presentation import SettingRow
    from samsara.ui.settings.calm_controls import switch
    from samsara.ui.settings.shared_qt import _HotkeyButton
    if name not in ('Dictation', 'Shortcuts'):
        return
    cfg = owner.app.config.get('kept_dictations') or {}
    controls = []
    if name == 'Dictation':
        enabled = switch(QCheckBox())
        enabled.setChecked(cfg.get('enabled', False))
        enabled.setAccessibleName('Keep my dictations for review')
        maximum = QSpinBox()
        maximum.setRange(1, 500)
        maximum.setValue(cfg.get('maximum', 50))
        maximum.setAccessibleName('Dictations to keep')
        maximum.setFixedWidth(100)
        maximum.setFixedHeight(32)
        controls = [('keep_dictations', 'Keep my dictations for review',
                     'Keep audio and text in History > Kept. Off until you choose it.',
                     'kept_dictations.enabled', [enabled]),
                    ('kept_dictations_maximum', 'Dictations to keep',
                     'The oldest kept take is removed when this limit is reached.',
                     'kept_dictations.maximum', [maximum])]
        def save(acc):
            value = dict(owner.app.config.get('kept_dictations') or {})
            value.update(acc.get('kept_dictations') or {})
            value.update(enabled=enabled.isChecked(), maximum=maximum.value())
            return {'kept_dictations': value}
        owner._source_saves['general'].append(save)
    else:
        hotkey = _HotkeyButton(owner.app.config.get('keep_dictation_hotkey', ''))
        hotkey.setFixedHeight(32)
        hotkey.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        hotkey.setAccessibleName('Keep this dictation')
        def clear_hotkey():
            hotkey._combo = ''
            hotkey.setText(hotkey._idle_text(''))
        clear = _button('Clear', clear_hotkey)
        clear.setFixedHeight(32)
        controls = [('keep_dictation_hotkey', 'Keep this dictation',
            'Save the current or last take, even with keeping off. With no recent take, keep the next one. Say “keep that” or “save that dictation” after speaking.',
            'keep_dictation_hotkey', [hotkey, clear])]
        def save(acc):
            return {'keep_dictation_hotkey': hotkey.combo}
        owner._source_saves['modes'].append(save)
    for key, title, description, config, widgets in controls:
        # PLACEHOLDER COPY (1161)
        row = SettingRow(title, description, widgets)
        row.config_keys = (config,)
        row.widget_keys = (key,)
        row.page_name = name
        row.disclosure = None
        row.full_help = description
        owner._widgets[key] = widgets[0]
        owner._rows_by_widget[key] = row
        owner._destination_rows.append(row)
        page.add_row(row)


class KeptView(QWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.store = store
        self.player = None
        self.audio_output = None
        root = QVBoxLayout(self)
        root.addWidget(theme.hub_header('Kept', 'Play a take and compare the recognition with the text sent.', self))
        self.entries = QListWidget()
        self.entries.setAccessibleName('Kept dictations')
        self.entries.setMaximumHeight(190)
        self.entries.currentItemChanged.connect(self._selected)
        root.addWidget(self.entries)
        self.metadata = QLabel()
        self.metadata.setTextFormat(Qt.TextFormat.PlainText)
        self.metadata.setWordWrap(True)
        root.addWidget(self.metadata)
        texts = QHBoxLayout()
        for label, attribute in [('Raw transcript', 'raw'), ('Typed text', 'typed')]:
            column = QVBoxLayout()
            column.addWidget(QLabel(label))
            edit = QPlainTextEdit()
            edit.setReadOnly(True)
            edit.setAccessibleName(label)
            setattr(self, attribute, edit)
            column.addWidget(edit)
            texts.addLayout(column)
        root.addLayout(texts, 1)
        buttons = QHBoxLayout()
        self.play = _button('Play audio', self._play)
        self.stop = _button('Stop', self._stop)
        self.export_button = _button('Export take…', self._export)
        self.delete_button = _button('Delete take', self._delete)
        self.clear_button = _button('Delete all', self._clear)
        for button in (self.play, self.stop, self.export_button, self.delete_button, self.clear_button):
            buttons.addWidget(button)
        buttons.addStretch()
        root.addLayout(buttons)
        self.status = QLabel()
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(lambda: self.refresh() if self.isVisible() else None)
        self.timer.start()
        self._fingerprint = None
        self.refresh()

    def selected(self):
        item = self.entries.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def refresh(self):
        try:
            rows = self.store.list()
            fingerprint = repr(rows)
            if fingerprint == self._fingerprint:
                return
            self._fingerprint = fingerprint
            selected = self.selected()
            self.entries.clear()
            for row in rows:
                item = QListWidgetItem(f"{row['timestamp'][:19].replace('T', ' ')} · {row['path']} · {row['duration_s']:.1f} s")
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.entries.addItem(item)
                if selected and selected['id'] == row['id']:
                    self.entries.setCurrentItem(item)
            if self.entries.currentRow() < 0 and rows:
                self.entries.setCurrentRow(0)
            self._selected()
            self.status.setText(f'{len(rows)} kept' if rows else 'No kept dictations. Use your keep hotkey or enable keeping in Settings > Dictation.')
        except Exception:
            self.status.setText('Could not read kept dictations.')

    def _selected(self, *_):
        self._stop()
        row = self.selected()
        self.raw.setPlainText(row.get('raw_transcript', '') if row else '')
        self.typed.setPlainText(row.get('final_text', '') if row else '')
        self.metadata.setText(f"{row['model']} · {row['device']} · {row['sample_rate']} Hz · {row['status']}" if row else '')
        for button in (self.play, self.export_button, self.delete_button):
            button.setEnabled(row is not None)

    def _stop(self):
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())

    def _play(self):
        row = self.selected()
        if not row:
            return
        try:
            if self.player is None:
                from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
                self.player = QMediaPlayer(self)
                self.audio_output = QAudioOutput(self)
                self.player.setAudioOutput(self.audio_output)
                self.player.errorOccurred.connect(lambda *_: self.status.setText('Audio could not be played. You can export the take.'))
            self.player.setSource(QUrl.fromLocalFile(str(self.store.audio_path(row['id']))))
            self.player.play()
        except Exception:
            self.status.setText('Audio could not be played. You can export the take.')

    def _export(self):
        row = self.selected()
        if row:
            folder = QFileDialog.getExistingDirectory(self, 'Export kept dictation')
            if folder:
                try:
                    self.store.export(row['id'], folder)
                    self.status.setText('Exported audio and text to a new folder.')
                except Exception:
                    self.status.setText('Could not export the take.')

    def _delete(self):
        row = self.selected()
        if row:
            self._stop()
            try:
                self.store.delete(row['id'])
                self.refresh()
            except Exception:
                self.status.setText('Could not delete the take.')

    def _clear(self):
        self._stop()
        try:
            self.store.clear()
            self.refresh()
        except Exception:
            self.status.setText('Could not delete kept dictations.')

    def hideEvent(self, event):
        self._stop()
        super().hideEvent(event)


def add_history(view, app):
    from samsara.ui.history_view import _make_transparent
    column = view.layout()
    normal = QWidget(view)
    _make_transparent(normal)
    # Transfer the whole layout so nested rows, stretch and spacing stay intact.
    normal.setLayout(column)
    root = QVBoxLayout(view)
    root.setContentsMargins(column.contentsMargins())
    root.setSpacing(column.spacing())
    column.setContentsMargins(0, 0, 0, 0)
    view.kept_panel = KeptView(store_for(app), view)
    view.kept_panel.hide()
    buttons = QHBoxLayout()
    def show_history():
        view.kept_panel.hide()
        normal.show()
    history_button = _button('History', show_history)
    def show_kept():
        view.kept_panel.refresh()
        normal.hide()
        view.kept_panel.show()
    view.kept_button = _button('Kept', show_kept)
    buttons.addWidget(history_button)
    buttons.addWidget(view.kept_button)
    buttons.addStretch()
    root.addLayout(buttons)
    # Box layouts ignore hidden pages; QStackedWidget reserves Kept's taller
    # height-for-width even on History, pushing its footer below the host.
    root.addWidget(normal, 1)
    root.addWidget(view.kept_panel, 1)

# PLACEHOLDER COPY (1161): ordinary review labels and status messages chosen by the worker.
