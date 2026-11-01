"""Spoken examples and contextual explanations on reference detail cards."""
from PySide6.QtCore import QEvent
from samsara import command_catalog


class ExampleDetailCardMixin:
    """Add spoken help at the reference's detail-card seam (774)."""

    def open(self, command, origin, anchor):
        command = dict(command)
        from samsara.ui.command_cheatsheet_qt import row_tags
        command['row_tags'] = row_tags(command)
        command['aliases'] = command.get('display_aliases', command_catalog.display_aliases(
            command, limit=len(command.get('aliases', ()))))
        examples = command.get('examples', ())
        precondition = command.get('precondition', '')
        if precondition:
            command['description'] = '\n\n'.join(
                part for part in (command.get('description', '').strip(), precondition) if part)
        super().open(command, origin, anchor)
        if examples:
            self.labels[3].setText('Say it like this:\n' + '\n'.join(examples))
        elif precondition:
            self.labels[3].hide()
        description = '\n'.join(label.text() for label in self.labels + [self.metadata] if not label.isHidden())
        self.setAccessibleDescription(description)
        self.run_button.setAccessibleDescription(description)
        self.place(anchor)
        from samsara.ui.context_help_qt import reference_command_help
        reference_command_help(self.window, command)

    def eventFilter(self, watched, event):
        from samsara.ui.context_help_qt import is_help_interaction
        if is_help_interaction(watched):
            return False
        if (event.type() == QEvent.Type.Resize and
                getattr(getattr(self.window, '_context_help', None), 'reflowing', False)):
            return False
        return super().eventFilter(watched, event)

    def _scrolled(self, value):
        if not getattr(getattr(self.window, '_context_help', None), 'reflowing', False):
            super()._scrolled(value)


