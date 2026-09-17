"""Queue 187: named Spotify library rows round-trip through Settings."""

from tests.test_settings import _StubApp


def _window_and_save(config, qapp):
    from samsara.ui.settings_qt import _SettingsWindow

    app = _StubApp()
    app.config = config
    window = _SettingsWindow(app)
    save = next(fn for fn in window._save_fns if "_build_music_tab" in fn.__qualname__)
    return window, save


def test_music_library_migrates_adds_deletes_and_round_trips(qapp):
    config = {'music_library': {'Morning Coffee': 'spotify:playlist:coffee'}}
    window, save = _window_and_save(config, qapp)
    try:
        table = window._widgets['music_library_table']
        assert table.rowCount() == 1
        assert table.rowHeight(0) >= 44
        assert table.cellWidget(0, 1).text() == 'Playlist'

        window._widgets['music_library_name'].setText('Blue Train')
        window._widgets['music_library_link'].setText(
            'https://open.spotify.com/album:wrong'
        )
        window._add_music_library_entry()
        assert 'not a Spotify' in window._widgets['music_library_error'].text()

        window._widgets['music_library_link'].setText(
            'https://open.spotify.com/album/blue123?si=token'
        )
        window._add_music_library_entry()
        saved = save({})['music_library']
        assert saved == {
            'Morning Coffee': {'uri': 'spotify:playlist:coffee', 'type': 'playlist'},
            'Blue Train': {'uri': 'spotify:album:blue123', 'type': 'album'},
        }

        window._delete_music_library_entry('Morning Coffee')
        assert 'Morning Coffee' not in save({})['music_library']
    finally:
        window.deleteLater()

    reloaded = {'music_library': saved}
    second_window, _save = _window_and_save(reloaded, qapp)
    try:
        assert second_window._widgets['music_library_table'].rowCount() == 2
        assert second_window._music_library == saved
    finally:
        second_window.deleteLater()
