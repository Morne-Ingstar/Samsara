"""Music library page for named Spotify playlists, albums, artists, and tracks."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from plugins.commands import music
from samsara.ui import theme
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH
from samsara.music_import import MusicImportError, import_spotify_export, merge_imported_playlists


_TYPE_LABELS = {
    "playlist": "Playlist",
    "album": "Album",
    "artist": "Artist",
    "track": "Track",
}


class MusicPage:
    """Methods of the Music settings page, mixed into _SettingsWindow."""

    def _build_music_tab(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        layout.addWidget(self._section_title("Music library"))
        note = QLabel(
            "Name playlists, albums, artists, or tracks once, then say “playlist Morning”."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(note)

        import_button = QPushButton("Import from your Spotify data export")
        import_button.setObjectName("musicLibrarySpotifyImport")
        import_button.setMinimumHeight(48)
        import_button.setMinimumWidth(320)
        layout.addWidget(import_button)

        # OWNER COPY
        export_help = QLabel(
            "Spotify > Privacy settings > Download your data\n"
            "Wait for the email\n"
            "Pick the ZIP here"
        )
        export_help.setObjectName("musicLibrarySpotifyHelp")
        export_help.setWordWrap(True)
        export_help.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(export_help)

        add_row = QHBoxLayout()
        add_row.setSpacing(10)
        name = QLineEdit()
        name.setObjectName("musicLibraryName")
        name.setMinimumHeight(44)
        name.setPlaceholderText("Name to say, for example Morning Coffee")
        link = QLineEdit()
        link.setObjectName("musicLibraryLink")
        link.setMinimumHeight(44)
        link.setPlaceholderText("Paste a Spotify link or spotify: URI")
        add_button = QPushButton("Add")
        add_button.setObjectName("musicLibraryAdd")
        add_button.setMinimumHeight(44)
        add_button.setMinimumWidth(88)
        add_row.addWidget(name, 1)
        add_row.addWidget(link, 2)
        add_row.addWidget(add_button)
        layout.addLayout(add_row)

        error = QLabel("")
        error.setObjectName("musicLibraryError")
        error.setWordWrap(True)
        error.setStyleSheet(f"color: {theme.WARNING}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(error)

        import_summary = QLabel("")
        import_summary.setObjectName("musicLibrarySpotifyImportSummary")
        import_summary.setWordWrap(True)
        import_summary.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(import_summary)

        table = QTableWidget(0, 4)
        table.setObjectName("musicLibraryTable")
        table.setHorizontalHeaderLabels(["Name", "Type", "Spotify link", ""])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setMinimumHeight(190)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 96)
        table.setColumnWidth(3, 104)
        layout.addWidget(table)

        self._widgets['music_library_name'] = name
        self._widgets['music_library_link'] = link
        self._widgets['music_library_error'] = error
        self._widgets['music_library_spotify_import'] = import_button
        self._widgets['music_library_spotify_help'] = export_help
        self._widgets['music_library_spotify_summary'] = import_summary
        self._widgets['music_library_table'] = table
        self._music_library = dict(music.migrate_music_library(self.app.config))
        self._refresh_music_library_table()
        add_button.clicked.connect(self._add_music_library_entry)
        link.returnPressed.connect(self._add_music_library_entry)
        import_button.clicked.connect(self._open_spotify_import_picker)

        def _save(_acc):
            return {'music_library': dict(self._music_library)}

        self._save_fns.append(_save)
        layout.addStretch()
        scroll.setWidget(container)
        return scroll

    def _refresh_music_library_table(self):
        table = self._widgets['music_library_table']
        table.setRowCount(0)
        for name in sorted(self._music_library, key=str.casefold):
            entry = self._music_library[name]
            row = table.rowCount()
            table.insertRow(row)
            table.setRowHeight(row, 44)
            table.setItem(row, 0, QTableWidgetItem(name))

            chip = QLabel(_TYPE_LABELS.get(entry['type'], entry['type'].title()))
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setMinimumWidth(88)
            chip.setStyleSheet(
                f"background-color: {theme.tint(theme.ACCENT, 0.16)}; "
                f"border: 1px solid {theme.tint(theme.ACCENT, 0.45)}; "
                f"border-radius: 8px; color: {theme.ACCENT}; "
                f"font-size: {theme.TYPE_BODY}px; font-weight: 600; padding: 4px 8px;"
            )
            table.setCellWidget(row, 1, chip)
            table.setItem(row, 2, QTableWidgetItem(entry['uri']))

            delete = QPushButton("Delete")
            delete.setMinimumHeight(44)
            delete.setMinimumWidth(92)
            delete.clicked.connect(lambda _checked=False, entry_name=name:
                                   self._delete_music_library_entry(entry_name))
            table.setCellWidget(row, 3, delete)

    def _add_music_library_entry(self):
        name = self._widgets['music_library_name'].text().strip()
        link = self._widgets['music_library_link'].text().strip()
        error = self._widgets['music_library_error']
        if not name:
            error.setText("Give this Spotify item a name you can say.")
            return
        normalized = music.normalize_spotify_link(link)
        if normalized is None:
            error.setText("That is not a Spotify playlist, album, artist, or track link.")
            return
        self._music_library[name] = normalized
        self._widgets['music_library_name'].clear()
        self._widgets['music_library_link'].clear()
        error.clear()
        self._refresh_music_library_table()

    def _delete_music_library_entry(self, name):
        self._music_library.pop(name, None)
        self._widgets['music_library_error'].clear()
        self._refresh_music_library_table()

    def _choose_spotify_export_path(self):
        dialog = QFileDialog(self, "Select your Spotify data export")
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
        dialog.setNameFilter("Spotify data export (*.zip)")
        dialog.setLabelText(QFileDialog.DialogLabel.Accept, "Import")

        use_folder = QPushButton("Use this folder")
        use_folder.setMinimumHeight(44)
        use_folder.setMinimumWidth(160)
        dialog_layout = dialog.layout()
        if isinstance(dialog_layout, QGridLayout):
            dialog_layout.addWidget(
                use_folder, dialog_layout.rowCount(), 0, 1, max(1, dialog_layout.columnCount())
            )
        else:
            dialog_layout.addWidget(use_folder)

        use_folder.clicked.connect(
            lambda: (
                dialog.setProperty("spotifyExportFolder", dialog.directory().absolutePath()),
                dialog.accept(),
            )
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None

        selected_folder = dialog.property("spotifyExportFolder")
        if selected_folder:
            return selected_folder
        files = dialog.selectedFiles()
        return files[0] if files else None

    def _open_spotify_import_picker(self):
        source = self._choose_spotify_export_path()
        if source:
            self._import_spotify_export(source)

    def _import_spotify_export(self, source):
        error = self._widgets['music_library_error']
        summary = self._widgets['music_library_spotify_summary']
        try:
            imported = import_spotify_export(source)
            merged = merge_imported_playlists(self._music_library, imported)
        except MusicImportError as exc:
            summary.clear()
            error.setText(str(exc))
            return

        self._music_library = merged.library
        self._refresh_music_library_table()
        error.clear()
        summary.setText(merged.summary)
        music._announce(self.app, merged.summary)


def open_spotify_import_picker(app) -> bool:
    """Open the Music page's shared ZIP/folder picker from a command worker."""
    from samsara.ui import qt_runtime

    if not qt_runtime.is_alive():
        return False

    def _open_picker():
        settings = getattr(app, "_settings_qt", None)
        if settings is None:
            from samsara.ui.settings_qt import SettingsQt

            settings = SettingsQt(app)
            app._settings_qt = settings
        settings._show_tool("Music")
        window = settings._tools.get("Music")
        if window is not None:
            window._open_spotify_import_picker()

    qt_runtime.post(_open_picker)
    return True
