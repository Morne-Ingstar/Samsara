"""Task 348: local Spotify export parsing, merging, and entry points."""

import json
from pathlib import Path
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from samsara.music_import import (
    ImportedPlaylist,
    ImportResult,
    MusicImportError,
    import_spotify_export,
    merge_imported_playlists,
)


def _playlist(name, uri, items=(), **fields):
    return {"name": name, "uri": uri, "items": list(items), **fields}


def _write_export_zip(path: Path, *playlist_files, other_files=None):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in playlist_files:
            archive.writestr(
                f"MyData/Playlist{filename}.json",
                json.dumps(data, ensure_ascii=False),
            )
        for filename, raw in (other_files or {}).items():
            archive.writestr(filename, raw)
    return path


def test_reads_playlist_json_only_and_merges_duplicate_uris(tmp_path):
    archive = _write_export_zip(
        tmp_path / "spotify-export.zip",
        ("1", [_playlist("Morning Mix", "spotify:playlist:morning1", [{"track": "a"}])]),
        ("2", {"playlists": [
            _playlist("Renamed duplicate", "spotify:playlist:morning1", [{"track": "a"}, {"track": "b"}]),
            _playlist("Late Night", "spotify:playlist:night2", [{"track": "c"}]),
        ]}),
        other_files={"MyData/StreamingHistory1.json": b"not JSON; must not be read"},
    )

    result = import_spotify_export(archive)

    assert result.playlists == (
        ImportedPlaylist("Morning Mix", "spotify:playlist:morning1", 2),
        ImportedPlaylist("Late Night", "spotify:playlist:night2", 1),
    )


def test_folder_export_skips_followed_unowned_and_missing_uri(tmp_path):
    export_folder = tmp_path / "extracted"
    export_folder.mkdir()
    (export_folder / "Playlist1.json").write_text(json.dumps({"playlists": [
        _playlist("Owned", "spotify:playlist:owned1", [{}, {}]),
        _playlist("Followed", "spotify:playlist:followed2", isFollowed=True),
        _playlist("Shared elsewhere", "spotify:playlist:shared3", collaborative=True, isOwned=False),
        _playlist("Shared and owned", "spotify:playlist:shared4", collaborative=True, isOwned=True),
        {"name": "Missing URI", "items": []},
        _playlist("Bad URI", "https://open.spotify.com/playlist/bad5"),
    ]}), encoding="utf-8")
    (export_folder / "StreamingHistory1.json").write_text("invalid ignored data", encoding="utf-8")

    result = import_spotify_export(export_folder)

    assert [item.name for item in result.playlists] == ["Owned", "Shared and owned"]
    assert result.skipped_not_owned == 2
    assert result.skipped_without_uri == 1
    assert result.skipped_invalid == 1


def test_merge_keeps_spoken_name_and_resolves_name_collision():
    existing = {
        "My spoken name": {"uri": "spotify:playlist:already1", "type": "playlist"},
        "Daily": {"uri": "spotify:playlist:daily-old", "type": "playlist"},
    }
    imported = ImportResult((
        ImportedPlaylist("Export name", "spotify:playlist:already1", 10),
        ImportedPlaylist("Daily", "spotify:playlist:daily-new", 4),
    ))

    merged = merge_imported_playlists(existing, imported)

    assert merged.summary == "1 playlists added, 1 already there"
    assert merged.library["My spoken name"]["uri"] == "spotify:playlist:already1"
    assert merged.library["Daily"]["uri"] == "spotify:playlist:daily-old"
    assert merged.library["Daily (2)"] == {
        "uri": "spotify:playlist:daily-new", "type": "playlist"
    }


def test_bad_zip_has_a_plain_error(tmp_path):
    bad_zip = tmp_path / "broken.zip"
    bad_zip.write_bytes(b"this is not a ZIP")

    with pytest.raises(MusicImportError, match="Choose a Spotify data ZIP"):
        import_spotify_export(bad_zip)


def test_zip_without_playlist_json_has_a_plain_error(tmp_path):
    archive = _write_export_zip(
        tmp_path / "no-playlists.zip",
        other_files={"MyData/StreamingHistory1.json": b"[]"},
    )

    with pytest.raises(MusicImportError, match="No Playlist JSON file"):
        import_spotify_export(archive)


def test_music_settings_button_imports_and_updates_rows(qapp, tmp_path, monkeypatch):
    from tests.test_settings import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow

    archive = _write_export_zip(
        tmp_path / "settings-export.zip",
        ("1", [_playlist("Blue Train", "spotify:playlist:blue1", [{}, {}])]),
    )
    app = _StubApp()
    app.config = {"music_library": {}}
    coordinator = SimpleNamespace(speak=Mock())
    app.audio_coordinator = coordinator
    window = _SettingsWindow(app, tool="Music")
    monkeypatch.setattr(window, "_choose_spotify_export_path", lambda: str(archive))
    try:
        # 840: the ZIP picker sits directly below the preserved import instructions.
        assert window._widgets["music_library_spotify_import"].text() == "Import Spotify ZIP…"
        assert window._widgets["music_library_spotify_help"].text().splitlines() == [
            "Spotify > Privacy settings > Download your data",
            "Wait for the email",
            "Pick the ZIP here",
        ]

        window._open_spotify_import_picker()

        assert window._widgets["music_library_table"].rowCount() == 1
        assert window._widgets["music_library_spotify_summary"].text() == (
            "1 playlists added, 0 already there"
        )
        assert window._music_library["Blue Train"] == {
            "uri": "spotify:playlist:blue1", "type": "playlist"
        }
        assert window._collect_destination_updates({})["music_library"] == (
            window._music_library
        )
        coordinator.speak.assert_called_once_with(
            "1 playlists added, 0 already there",
            category="agent_response",
            interruptible=False,
        )
    finally:
        window.deleteLater()


def test_shared_picker_accepts_zip_files_and_export_folders(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog, QFileDialog, QPushButton
    from tests.test_settings import _StubApp
    from samsara.ui.settings_qt import _SettingsWindow

    window = _SettingsWindow(_StubApp(), tool="Music")
    zip_path = tmp_path / "spotify-data.zip"
    try:
        monkeypatch.setattr(
            QFileDialog, "exec", lambda _dialog: QDialog.DialogCode.Accepted
        )
        monkeypatch.setattr(
            QFileDialog, "selectedFiles", lambda _dialog: [str(zip_path)]
        )
        assert window._choose_spotify_export_path() == str(zip_path)

        def choose_folder(dialog):
            dialog.setDirectory(str(tmp_path))
            folder_button = next(
                button for button in dialog.findChildren(QPushButton)
                if button.text() == "Use this folder"
            )
            folder_button.click()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(QFileDialog, "exec", choose_folder)
        assert Path(window._choose_spotify_export_path()) == tmp_path
    finally:
        window.deleteLater()


def test_voice_command_opens_shared_picker_and_announces(monkeypatch):
    from plugins.commands import music
    from samsara import plugin_commands
    from samsara.command_registry import DispatchState

    picker = Mock(return_value=True)
    monkeypatch.setattr(
        "samsara.ui.settings.music_qt.open_spotify_import_picker", picker
    )
    coordinator = SimpleNamespace(speak=Mock())
    app = SimpleNamespace(audio_coordinator=coordinator)
    plugin_commands._reinstall_module_commands(music)

    result = music.handle_import_spotify_playlists(app, "")

    assert result.state is DispatchState.COMPLETED
    picker.assert_called_once_with(app)
    coordinator.speak.assert_called_once_with(
        "Choose your Spotify data export.",
        category="agent_response",
        interruptible=False,
    )
    assert plugin_commands._REGISTRY["import my spotify playlists"]["pack"] == "media"


def test_voice_picker_posts_window_work_to_qt_thread(monkeypatch):
    from samsara.ui import qt_runtime
    from samsara.ui.settings.music_qt import open_spotify_import_picker

    window = SimpleNamespace(_open_spotify_import_picker=Mock())
    settings = SimpleNamespace(
        _tools={"Music": window},
        _show_tool=Mock(),
    )
    app = SimpleNamespace(_settings_qt=settings)
    callbacks = []
    monkeypatch.setattr(qt_runtime, "is_alive", lambda: True)
    monkeypatch.setattr(qt_runtime, "post", callbacks.append)

    assert open_spotify_import_picker(app) is True
    assert len(callbacks) == 1
    settings._show_tool.assert_not_called()

    callbacks[0]()

    settings._show_tool.assert_called_once_with("Music")
    window._open_spotify_import_picker.assert_called_once_with()
