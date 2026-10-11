"""Record managers and integrations opened independently of Settings."""
from PySide6.QtWidgets import QLabel, QPushButton, QWidget, QHBoxLayout
from PySide6.QtCore import Qt
from .presentation import Page, Disclosure, SettingRow
from .calm_controls import switch
from .calm_layout import RowLayout
from samsara.ui import theme


def _tool_row(title, description, controls):
    row = SettingRow(title, description, controls)
    row.detail.setVisible(bool(description))
    row._calm_layout = RowLayout(row)
    return row

TOOLS = ('Command library', 'Alarms', 'Health', 'Music', 'Components', 'Integrations')


def build(owner):
    tools = {}
    commands = owner._source_pages['commands']
    # The source factory gives the integration a real container, so its
    # credentials, result status and callbacks move together.
    todoist = commands.findChild(QWidget, 'todoistSection')
    integration = Page('Integrations', 'Connect optional services with explicit permission.')
    todoist.setParent(integration.container)
    integration.base.addWidget(todoist)
    integration.advanced.hide()
    token_group = todoist.findChild(QWidget, 'todoistCredentials')
    enabled = owner._widgets['todoist_enabled']
    layout = todoist.layout()
    layout.setContentsMargins(0, 0, 0, 0)
    note = next(label for label in todoist.findChildren(QLabel) if label.text().startswith('When on, tasks'))
    title = enabled.text()
    enabled.setText('')
    enabled_row = _tool_row(title, note.text(), [switch(enabled)])
    layout.insertWidget(layout.indexOf(note), enabled_row)
    note.hide()
    token_group.setProperty('settingsToolRow', True)
    credentials = token_group.layout()
    credentials.setContentsMargins(0, 0, 0, 0)
    token_layout = credentials.takeAt(0).layout()
    controls = [token_layout.takeAt(0).widget() for _ in range(token_layout.count())]
    token_row = _tool_row(owner._widgets['todoist_token'].placeholderText(), '', controls)
    credentials.insertWidget(0, token_row)
    test = next(button for button in token_group.findChildren(QPushButton) if button.text() == 'Test connection')
    credentials.setAlignment(test, Qt.AlignmentFlag.AlignRight)
    token_group.setVisible(enabled.isChecked())
    enabled.toggled.connect(token_group.setVisible)
    tools['Integrations'] = integration
    packs = owner._widgets['_pack_scroll']
    pack_parent = packs.parentWidget().layout()
    pack_parent.removeWidget(packs)
    disclosure = Disclosure('Command packs')
    disclosure.rows.addWidget(packs)
    pack_parent.insertWidget(0, disclosure)
    for label in commands.findChildren(QLabel):
        if label.text() == 'Command Packs' or label.text().startswith('Enable the packs you use.'):
            label.hide()
    tools['Command library'] = commands
    # Ava's effective master is the single editor for the AI pack.
    ai_pack = owner._widgets['_pack_checkboxes'].get('ai')
    if ai_pack is not None:
        ai_pack.parentWidget().hide()
    alarms = owner.make_page('Alarms', 'Record changes apply immediately; preferences use Apply.', [
        ['Alarm reminders', 'Enable alarm reminders.', 'alarms_enabled', 'alarms.enabled'],
    ], [
        ['Repeat interval', 'Replay the alarm until completed or dismissed.', 'alarms_nag', 'alarms.nag_interval_seconds'],
    ])
    table = owner._widgets['alarms_table']
    alarms.base.addWidget(table)
    toolbar = QWidget()
    actions = QHBoxLayout(toolbar)
    for text in ('Add Alarm', 'Edit', 'Toggle', 'Test', 'Delete'):
        actions.addWidget(owner._source_button(text, 'alarms'))
    alarms.base.addWidget(toolbar)
    alarms.advanced.rows.addWidget(owner._source_button('Reset Stats', 'alarms'))
    shortcuts = QPushButton('Alarm shortcuts…')
    shortcuts.clicked.connect(lambda: owner.open_preferences('Shortcuts'))
    alarms.advanced.rows.addWidget(shortcuts)
    tools['Alarms'] = alarms
    tools['Health'] = owner._source_pages['health']
    tools['Music'] = owner._source_pages['music']
    for name in ('Command library', 'Health', 'Music'):
        heading = QLabel(name)
        heading.setProperty('role', 'title')
        tools[name].widget().layout().insertWidget(0, heading)
    components = owner.make_page('Components', 'Install and manage optional capabilities.', [
        ['Gesture lane', 'Use the optional camera gesture capability.', 'adv_gesture_enabled', 'gesture.enabled'],
    ], [])
    components.base.addWidget(owner._components_page.widget)
    components.advanced.hide()
    tools['Components'] = components
    return tools
