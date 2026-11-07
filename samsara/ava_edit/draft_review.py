"""Catalogue-backed directions and a read-only view of the pending edit (1137)."""
from __future__ import annotations

import weakref

_preview = None


def register_commands(module_name):
    from samsara.plugin_commands import command

    def show_change(app, _remainder):
        return show(app) if not _remainder or is_preview(app, _remainder) else False

    show_change.__module__ = module_name
    # PLACEHOLDER COPY (1176)
    show_change.__doc__ = "Shows the proposed edit without changing your draft."
    command("show the change", pack="core", ai_visible=False, risk_class="ui",
            ai_composable=False, side_effects=["ui"], preconditions=[],
            voice_triggerable=True, param_schema={}, reversible=False,
            preview_template="", whole_utterance=True)(show_change)


def phrases(app):
    """Use the live command catalogue, with its normal packaged fallback."""
    from samsara import command_catalog
    matcher = getattr(getattr(app, "command_executor", None), "_matcher", None)
    try:
        rows = matcher.list_commands() if matcher is not None else None
        records = command_catalog.guidance_catalog(rows) or []
    except Exception:
        records = []
    ids = {"view": "selection_edit.show_the_change", "accept": "selection_edit.apply",
           "reject": "selection_edit.discard"}
    return {action: command_catalog.canonical_phrase(record)
            for action, cid in ids.items() for record in records
            if record["canonical_id"] == cid}


def is_preview(app, text):
    from samsara.ava_edit import intent
    phrase = phrases(app).get("view")
    return bool(phrase and not intent._is_quoted(text)
                and intent._normalize(text) == intent._normalize(phrase))


def status(app, target, count):
    """Mention only buttons that are visibly present, never a configured mode."""
    words = phrases(app)
    # PLACEHOLDER COPY (1137)
    prefix = f"Edit to {target}: {count} {'change' if count == 1 else 'changes'}."
    if not all(key in words for key in ("view", "accept", "reject")):
        return prefix + " Command help unavailable."
    full = getattr(getattr(app, "live_surface", None), "widget", None)
    view = f"Say {words['view']} to view;"
    accept, reject = f"{words['accept']} to accept", f"{words['reject']} to reject"
    if full is not None and full.isVisible():
        clean, clear = full._clean_up, full._clear
        if clean.isVisible() and clean.isEnabled() and clear.isVisible() and clear.isEnabled():
            return (f"{prefix} Say {words['view']} to view.\n"
                    f"Say {words['accept']} to accept, {words['reject']} to reject; "
                    f"click {clean.text()} / {clear.text()}.")
        return f"{prefix} {view}\n{accept}; {reject}."
    return f"{prefix} {view} {accept}; {reject}."


def close():
    global _preview
    window = _preview() if _preview is not None else None
    _preview = None
    if window is not None:
        from samsara.ui import qt_runtime
        def close_window():
            from shiboken6 import isValid
            if isValid(window):
                window.close()
        qt_runtime.post(close_window)


def show(app):
    """Open the live proposal on Qt's thread; never apply, inject or bootstrap."""
    from samsara.ava_edit import session
    from samsara.ui import qt_runtime
    staged = session.pending()
    if staged is None or not qt_runtime.is_alive():
        return False

    def open_preview():
        global _preview
        if session.pending() is not staged:
            return
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout
        from samsara.ui import theme
        class ProposedChangeDialog(QDialog):
            """App-owned dialog admitted by the bounded spoken-button discovery."""
        close()
        window = ProposedChangeDialog()
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # PLACEHOLDER COPY (1137)
        window.setWindowTitle("Proposed change")
        layout = QVBoxLayout(window)
        for title, text in (("Before", staged.proposal.original), ("Proposed", staged.proposal.rewritten)):
            layout.addWidget(QLabel(title))
            body = QTextBrowser()
            body.setPlainText(text)
            body.setOpenExternalLinks(False)
            layout.addWidget(body)
        words = phrases(app)
        if all(key in words for key in ("accept", "reject")):
            layout.addWidget(QLabel(f"Say {words['accept']} to accept or {words['reject']} to reject."))
        buttons = QHBoxLayout()
        for label, action in (("Accept?", session.apply), ("Cancel", session.discard)):
            button = QPushButton(label)
            button.setFixedWidth(button.sizeHint().width() + 20)
            def answer(_checked=False, action=action):
                if session.pending() is staged:
                    action(app)
                window.close()
            button.clicked.connect(answer)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        window.resize(580, 380)
        window.setStyleSheet(theme.build_stylesheet())
        app._ava_edit_preview = window
        _preview = weakref.ref(window)
        window.show()
        window.raise_()

    qt_runtime.post(open_preview)
    return True
