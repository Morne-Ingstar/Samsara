"""814: prepare a hands-free draft before display, then deliver it exactly."""
from __future__ import annotations

import re
import unicodedata

from samsara.cleanup import clean_text
from samsara.smart_corrections import smart_correct


def normalize_search_query(text: str) -> str:
    """Ignore boundary punctuation, retaining punctuation inside a query."""
    text = text.strip()
    while text and (text[0].isspace() or unicodedata.category(text[0]).startswith('P')):
        text = text[1:]
    while text and (text[-1].isspace() or unicodedata.category(text[-1]).startswith('P')):
        text = text[:-1]
    return text.lower()


def edit_is_single_line(control) -> bool | None:
    """Use explicit UIA ARIA metadata or the native Edit multiline style.

    UIA has no general IsMultiline property. An unknown Edit is not proof
    of a single-line target; Chromium prose editors also expose Edit.
    """
    if control is None or control.ControlTypeName != 'EditControl':
        return None
    properties = control.GetPropertyValue(30102)  # UIA_AriaPropertiesPropertyId
    match = re.search(r'(?:^|;)\s*multiline\s*=\s*(true|false)\s*(?:;|$)',
                      str(properties), re.IGNORECASE)
    if match:
        return match.group(1).lower() == 'false'
    if control.ClassName == 'QLineEdit':
        return True
    hwnd = control.NativeWindowHandle
    if hwnd:
        import win32gui
        native_class = win32gui.GetClassName(hwnd).lower()
        if native_class == 'edit' or native_class.startswith('richedit'):
            return not bool(win32gui.GetWindowLong(hwnd, -16) & 0x0004)  # ES_MULTILINE
    return None


def focused_single_line() -> bool | None:
    """One focused-control query; no tree walk, text read, or retained target."""
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            return edit_is_single_line(auto.GetFocusedControl())
    except Exception:
        return None


class SessionTextDelivery:
    """Share preparation across legacy sends and the exact draft send seam.

    Dictionary corrections have already run at the decode boundary. This
    adapter never runs them again. Buffered chunks run cleanup/smart correction
    once before display; finish and clipboard retries use inject_prepared.
    """

    def __init__(self, app, *, single_line_probe=focused_single_line):
        self.app = app
        self.single_line_probe = single_line_probe

    def _prepare(self, text: str, *, draft: bool) -> str:
        app = self.app
        formatted = app._apply_verbatim_if_active(text)
        if formatted is not None:
            return formatted
        formatted = app.process_transcription(text)
        mode = 'verbatim' if getattr(app, '_skip_cleanup', False) else app.config.get('cleanup_mode', 'clean')
        formatted = clean_text(formatted, mode=mode)
        if app.config.get('smart_corrections', {}).get('modes', {}).get('wake', True):
            formatted = smart_correct(formatted, app)
        if not draft and app.config.get('add_trailing_space', False):
            formatted += ' '
        formatted = app._apply_formatting_tokens(formatted)
        if draft and mode != 'verbatim':
            pending = getattr(getattr(app, '_session_mode_manager', None), 'dictate_pending_buffer', '')
            fragment = len((pending + ' ' + text).split()) <= 3
            single_line = fragment or self.single_line_probe() is True
            if single_line:
                # Strip a sentence full stop, preserving ellipses and !/?.
                formatted = re.sub(r'(?<!\.)\.(?=\s*$)', '', formatted)
                # Search/fragment cleanup must not capitalize a lower-case term.
                if text and formatted and text[0].islower() and formatted[0].lower() == text[0]:
                    formatted = formatted[0].lower() + formatted[1:]
        return formatted

    def prepare(self, text: str) -> str:
        return self._prepare(text, draft=True)

    def inject(self, text: str, commit_focus_guard=None):
        return self._deliver(self._prepare(text, draft=False), text, commit_focus_guard)

    def inject_prepared(self, text: str, commit_focus_guard=None):
        return self._deliver(text, text, commit_focus_guard)

    def _deliver(self, formatted, raw, commit_focus_guard):
        from samsara import tutorial_capture
        from samsara.session_modes import InjectionDelivery
        app = self.app
        manager = app._session_mode_manager
        formatted = getattr(manager, '_sentence_prefix', '') + formatted
        clipboard_only = getattr(manager, '_sentence_clipboard', False)
        if clipboard_only:
            import pyperclip
            pyperclip.copy(formatted)
            paste_ok, confirmed = True, False
        else:
            paste_ok, confirmed = app._paste_preserving_clipboard(
                formatted, before_paste=commit_focus_guard,
                return_delivery_confirmation=True,
                **tutorial_capture.kwargs(tutorial_capture.current()))
        if not paste_ok:
            return False
        display = formatted.strip()
        if display:
            app.add_to_history(display, is_command=False)
            app._log_history(raw_text=raw, display_text=display, mode='dictate',
                             status='buffered' if clipboard_only else 'success', entry_type='dictation')
            app._notify_main_window(display)
        return InjectionDelivery(formatted, confirmed=confirmed, undo_unit=getattr(app, '_undo_unit', None))
