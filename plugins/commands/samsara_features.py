"""Approved mid-task Samsara routes (1160); effects stay in existing actions."""
from samsara.plugin_commands import command


def _notice(app, text):
    show = getattr(app, '_show_outcome_chip', None)
    if callable(show):
        show(text, 'info')


def _post(action):
    from samsara.ui import qt_runtime
    qt_runtime.post(action)
    return True


def _current_page(app, name):
    """Only a visible current page is a selected-item target."""
    window = getattr(getattr(app, 'main_window', None), '_window', None)
    if window is not None and window.isVisible() and window.isActiveWindow():
        page = window._panel_cache.get(name)
        if page is not None and window._stack.currentWidget() is page:
            return page
    if name == 'History':
        window = getattr(getattr(app, '_history_qt', None), '_window', None)
        if window is not None and window.isVisible() and window.isActiveWindow():
            return window._view
    if name == 'Commands':
        window = getattr(getattr(app, 'cheat_sheet', None), '_window', None)
        if window is not None and window.isVisible() and window.isActiveWindow():
            return window
    return None


def _hub(app, name, action):
    """Build a cold hub before selecting; keep an already selected page intact."""
    def request():
        page = _current_page(app, name)
        if page is not None:
            action(page)
            return
        app.show_main_window()

        def select():
            from samsara.ui.window_activation import surface_requested
            window = getattr(getattr(app, 'main_window', None), '_window', None)
            if window is None:
                _notice(app, 'Samsara window is not ready.')
                return
            page = window._activate(name)
            surface_requested(window)
            if page is not None:
                action(page)
        _post(select)
    return _post(request)


def _selected(app, name, action):
    def run():
        page = _current_page(app, name)
        if page is None:
            _notice(app, 'Open ' + name + ' and select an item first.')
        else:
            action(page)
    return _post(run)


@command('show onscreen messages', aliases=['show on screen messages'], pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def review_messages(app, remainder=''):
    """Opens the existing review of on-screen messages and drafts."""
    from samsara.ui.passive_review import open_review
    return _post(lambda: open_review(app))


def _mute(app, muted):
    def apply():
        from contextlib import nullcontext
        with getattr(app, '_config_lock', None) or nullcontext():
            app.config.setdefault('accessibility', {})['ava_mute_audio'] = muted
            app.save_config()
        _notice(app, 'Ava voice muted.' if muted else 'Ava voice unmuted.')
    return _post(apply)


@command('mute Ava voice', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def mute_ava(app, remainder=''):
    """Mutes Ava's spoken replies while preserving captions and command feedback."""
    return _mute(app, True)


@command('unmute Ava voice', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def unmute_ava(app, remainder=''):
    """Restores Ava's spoken replies using the configured speech engine."""
    return _mute(app, False)


@command('use continuous dictation', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def continuous_mode(app, remainder=''):
    """Selects Continuous using the same capture-mode action as the tray."""
    return _post(lambda: app.switch_mode_from_tray('continuous'))


@command('use hold to talk', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def hold_mode(app, remainder=''):
    """Selects Hold to Talk using the same capture-mode action as the tray."""
    return _post(lambda: app.switch_mode_from_tray('hold'))


def _search_parser(prefix):
    """Consume a complete lead-in plus nonempty query; never match within prose."""
    from samsara.command_registry import view_tokens, argument_text
    expected = [t[0] for t in view_tokens(prefix)]

    def parse(text):
        tokens = view_tokens(text)
        if len(tokens) <= len(expected) or [t[0] for t in tokens[:len(expected)]] != expected:
            return None
        query = argument_text(text, tokens[len(expected) - 1][2]).strip()
        return query if query else None
    return parse


@command('search samsara history for', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True,
         param_schema={'remainder': {'type': 'str', 'required': True}})
def search_history(app, remainder=''):
    """Opens History and searches all saved history for your words."""
    if not remainder.strip():
        return False
    return _hub(app, 'History', lambda page: page._search.setText(remainder))


search_history.match_utterance = _search_parser('search samsara history for')
search_history.query_argument = True


@command('search samsara commands for', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True,
         param_schema={'remainder': {'type': 'str', 'required': True}})
def search_commands(app, remainder=''):
    """Opens Commands and filters its catalogue by your words."""
    if not remainder.strip():
        return False
    return _hub(app, 'Commands', lambda page: page.set_view(None, ('All', remainder)))


search_commands.match_utterance = _search_parser('search samsara commands for')
search_commands.query_argument = True


@command('search saved snippets for', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True,
         param_schema={'remainder': {'type': 'str', 'required': True}})
def search_snippets(app, remainder=''):
    """Opens Snippets and filters the saved snippets by your words."""
    if not remainder.strip():
        return False
    return _hub(app, 'Snippets', lambda page: page._search.setText(remainder))


search_snippets.match_utterance = _search_parser('search saved snippets for')
search_snippets.query_argument = True


def _latest_history(page):
    from PySide6.QtCore import Qt
    # A previous search or command-only filter must not hide the newest entry.
    for widget, value in ((page._search, ''), (page._filter, 'All'), (page._scope, 'All time')):
        widget.blockSignals(True)
        if hasattr(widget, 'setText'):
            widget.setText(value)
        else:
            widget.setCurrentText(value)
        widget.blockSignals(False)
    page._scope.setEnabled(True)
    expected = page._request_generation + 1

    def loaded(_rows, append, generation):
        if generation < expected:
            return
        page._rows_ready.disconnect(loaded)
        if append or generation != expected or page._request_generation != expected:
            return
        for index in range(page._list.count()):
            item = page._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole):
                page._list.setCurrentItem(item)
                page._list.setFocus()
                break
    page._rows_ready.connect(loaded)
    page._reload()


@command('show latest history entry', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def latest_history_entry(app, remainder=''):
    """Opens History and selects its newest entry after the refresh completes."""
    return _hub(app, 'History', _latest_history)


@command('load older history entries', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def older_history_entries(app, remainder=''):
    """Loads the next older page in the visible History view."""
    return _selected(app, 'History', lambda page: page._load_older())


@command('copy selected text', aliases=['copy text'], pack='core', risk_class='ui',
         side_effects=['clipboard'], ai_visible=False, whole_utterance=True)
def copy_selected_text(app, remainder=''):
    """Copies the current selection in the focused window or text field."""
    from samsara.handlers import _get_foreground_hwnd
    target = _get_foreground_hwnd()

    def copy():
        from PySide6.QtWidgets import QApplication
        from samsara.handlers import HotkeyHandler
        if target != _get_foreground_hwnd():
            _notice(app, 'Focus changed. Say copy text again.')
            return
        widget = QApplication.focusWidget()
        window = QApplication.activeWindow()
        # Calling the text field's own copy avoids History's row-copy shortcut.
        if (widget is not None and window is not None and window.isActiveWindow()
                and widget.window() is window and callable(getattr(widget, 'copy', None))):
            widget.copy()
        else:
            HotkeyHandler().execute({'type': 'hotkey', 'keys': ['ctrl', 'c']},
                                    app.command_executor._build_context(app))
    return _post(copy)


def _play_latest(page):
    page.reload()
    if not page._records:
        return False
    page._search.clear()
    page._filter_category.setCurrentIndex(0)
    page._apply_filter()
    page._list.setCurrentRow(0)
    return page.play_selected()


@command('play last memo', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def play_last_memo(app, remainder=''):
    """Plays the newest saved memo using the Memos page's player."""
    return _hub(app, 'Memos', _play_latest)


@command('play selected memo', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def play_selected_memo(app, remainder=''):
    """Plays the memo selected on the visible Memos page."""
    return _selected(app, 'Memos', lambda page: page.play_selected())


@command('play memo', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def play_memo(app, remainder=''):
    """Plays the visible selected memo, or the newest memo when none is selected."""
    return _hub(app, 'Memos', lambda page: page.play_selected() if page.selected else _play_latest(page))


def stop_memo(app, remainder=''):
    """Stops memo audio; playing again starts the memo from the beginning."""
    def stop():
        # Playback may continue after leaving Memos; do not open or reload it.
        window = getattr(getattr(app, 'main_window', None), '_window', None)
        page = window._panel_cache.get('Memos') if window is not None else None
        if page is not None:
            page.stop_playback()
    return _post(stop)


@command('select todays memos', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def select_today_memos(app, remainder=''):
    """Selects today's saved memos for review without approving or sending them."""
    return _hub(app, 'Memos', lambda page: page.select_today_batch())


@command('reload samsara commands', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def reload_commands(app, remainder=''):
    """Reloads commands.json after a manual edit using the existing command loader."""
    app.command_executor.reload_commands()
    _notice(app, 'Samsara commands reloaded.')
    return True


@command('show dictionary corrections', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def dictionary_corrections(app, remainder=''):
    """Opens Dictionary on its Corrections tab without changing any entries."""
    return _hub(app, 'Dictionary', lambda page: page._tabs.setCurrentIndex(1))


def _vocabulary(page):
    page._tabs.setCurrentIndex(0)
    page._voice_expander.setChecked(True)


@command('show vocabulary', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def vocabulary_voice_guide(app, remainder=''):
    """Opens Vocabulary with its guide to adding words by voice expanded."""
    return _hub(app, 'Dictionary', _vocabulary)


def _pin(app, page):
    from PySide6.QtCore import Qt
    item = page._list.currentItem()
    phrase = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
    detail = page._detail_card
    if detail.isVisible():
        phrase = detail.phrase
    if not phrase or not any(row['phrase'] == phrase for row in page._all):
        _notice(app, 'Select a command first.')
    elif phrase not in page._pinned:
        page._toggle_pin(phrase)


@command('pin selected command', aliases=['pin command'], pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def pin_reference_command(app, remainder=''):
    """Pins the selected Command Reference command without unpinning an existing pin."""
    return _selected(app, 'Commands', lambda page: _pin(app, page))


@command('recalibrate my microphone', aliases=['recalibrate my mic', 'recalibrate mic',
         'recalibrate microphone', 'microphone recalibrate'], pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def recalibrate_mic(app, remainder=''):
    """Recalibrates the microphone through the existing tray action."""
    return _post(app.recalibrate_mic)


@command('release stuck mouse buttons', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def release_mouse_buttons(app, remainder=''):
    """Releases mouse buttons held by Samsara using the existing recovery action."""
    return _post(app.release_mouse_buttons)


@command('release stuck buttons', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def release_stuck_buttons(app, remainder=''):
    """Releases Samsara's held mouse buttons, command keys and key-macro keys."""
    from samsara.handlers import ReleaseAllHandler
    app.release_mouse_buttons()
    ReleaseAllHandler().execute({'type': 'release_all'}, app.command_executor._build_context(app))
    macros = getattr(app, 'key_macro_manager', None)
    if macros is not None:
        macros.release_all_held()
    return True


@command('cancel file transcription', pack='core', risk_class='ui',
         ai_visible=False, whole_utterance=True)
def cancel_file_transcription(app, remainder=''):
    """Requests cancellation of the current file transcription without delivering partial output."""
    def cancel():
        dialog = getattr(app, '_transcribe_file_qt', None)
        if dialog is not None:
            dialog.cancel()
        else:
            _notice(app, 'No file transcription is open.')
    return _post(cancel)
