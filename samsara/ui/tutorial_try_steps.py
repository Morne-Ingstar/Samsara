"""Small, keyboard-reachable trial steps inside the existing tutorial."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from samsara.ui import theme
from samsara.ui.settings.calm_layout import stylesheet
from samsara.ui.settings.audio_page import BASE


class TryButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class TrySlider(QSlider):
    def keyReleaseEvent(self, event):
        super().keyReleaseEvent(event)
        if not event.isAutoRepeat() and event.key() in (
                Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down,
                Qt.Key.Key_Home, Qt.Key.Key_End, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            self.sliderReleased.emit()


def label(text):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    widget.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
    return widget


def button(text, callback, primary=False):
    widget = TryButton(text)
    widget.setAccessibleName(text)
    (theme.make_primary if primary else theme.make_secondary)(widget)
    widget.setFixedHeight(theme.HIT_TARGET_MIN)
    widget.setStyleSheet(widget.styleSheet() + f'QPushButton{{min-height:{theme.HIT_TARGET_MIN - 2}px;padding:0 {theme.SPACE_2}px;}}')
    widget.clicked.connect(callback)
    return widget


def choices(name, rows, current):
    box = QComboBox()
    box.setFixedHeight(theme.HIT_TARGET_MIN)
    box.setStyleSheet(f'QComboBox{{min-height:{theme.HIT_TARGET_MIN - 2}px;padding:0 {theme.SPACE_2}px;}}')
    box.setAccessibleName(name)
    for text, value in rows:
        box.addItem(text, value)
    box.setCurrentIndex(max(0, box.findData(current)))
    return box


class PlainChoices(QWidget):
    currentIndexChanged = Signal()
    tried = Signal()

    def __init__(self, name, rows, current):
        super().__init__()
        self.setAccessibleName(name)
        self.rows, self.buttons = rows, []
        self.index = max(0, self.findData(current))
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for index, (text, _) in enumerate(rows):
            control = button(text, lambda checked=False, i=index: self.choose(i))
            control.setCheckable(True)
            control.setStyleSheet(control.styleSheet() + f'QPushButton:checked{{border-color:{theme.ACCENT};background:{theme.BG1};color:{theme.ACCENT};}}')
            self.buttons.append(control)
            layout.addWidget(control)
        layout.addStretch()
        self.setCurrentIndex(self.index)

    def choose(self, index):
        self.setCurrentIndex(index)
        self.tried.emit()  # Replaying the same choice must also work.

    def findData(self, value):
        return next((i for i, row in enumerate(self.rows) if row[1] == value), -1)

    def currentData(self):
        return self.rows[self.index][1]

    def currentText(self):
        return self.rows[self.index][0]

    def setCurrentIndex(self, index):
        changed = self.index != index
        self.index = max(0, index)
        for i, control in enumerate(self.buttons):
            control.setChecked(i == self.index)
        if changed:
            self.currentIndexChanged.emit()


def build_steps(page, layout):
    cfg, window = page.app.config.get('tts', {}) or {}, page.window
    page.position = label('1 of 3')
    position_row = QHBoxLayout()
    position_row.addWidget(page.position)
    position_row.addWidget(label('Nothing is saved until you press Keep this.'), stretch=1)
    layout.addLayout(position_row)
    page.level = PlainChoices('How much Samsara talks', [('Silent', 'silent'), ('Brief', 'brief'), ('Full', 'full')], cfg.get('feedback_level', 'brief'))
    page.voice = choices(next(row[0] for row in BASE if row[2] == 'tts_engine'), [('Local', 'winrt'), ('Network', 'edge')], cfg.get('engine', 'winrt'))
    from samsara.speech_pace import profile
    page.pace = PlainChoices('Your pace', [('Standard', 'standard'), ('Relaxed', 'relaxed'), ('Unhurried', 'unhurried')], profile(page.app.config))
    descriptions = (
        'Choose Silent, Brief or Full to try what happens after a command.',
        'Listen, then choose another voice or adjust its speed and volume.',
        'Samsara waits for a pause before it treats you as finished. Read this to find a pause that suits you.',
    )
    for key, title, description in zip(('level', 'voice', 'pace'), ('How much Samsara talks', 'The voice', 'Your pace'), descriptions):
        frame = window._instruction_box(title)
        frame.setAccessibleName(title)
        title_label = frame.layout().itemAt(0).widget()
        title_label.setStyleSheet(f'color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;')
        frame.layout().setContentsMargins(12, 12, 12, 12)
        frame.layout().setSpacing(theme.SPACE_2)
        instruction = label(description)
        frame.layout().addWidget(instruction)
        page.cards[key] = frame
        layout.addWidget(frame)
        if key == 'level':
            frame.layout().addWidget(page.level)
        elif key == 'voice':
            build_voice(page, frame, cfg)
        else:
            sentence = label('“I would like enough time to finish what I am saying.”')
            sentence.setStyleSheet(f'color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_HEADING}px;')
            frame.layout().addWidget(sentence)
            page.pace_instruction, page.pace_sentence = instruction, sentence
        state = label('')
        page.states[key] = state
        frame.layout().addWidget(state)
        row = QHBoxLayout()
        if key != 'level':
            text = 'Hear it' if key == 'voice' else 'Start reading'
            page.actions[key] = button(text, lambda checked=False, k=key: page.sample(k))
            row.addWidget(page.actions[key])
        page.keeps[key] = button('Keep this', lambda checked=False, k=key: page.keep(k), primary=True)
        row.addWidget(page.keeps[key])
        row.addStretch()
        if key == 'pace':
            page.pace_description = label('')
            frame.layout().addWidget(page.pace_description)
            frame.layout().addWidget(page.pace)
        frame.layout().addLayout(row)
    page.voice_skip = label('The voice is skipped while Silent is selected.')
    layout.addWidget(page.voice_skip)
    page.result = label('')
    layout.addWidget(page.result)
    layout.addStretch()


def build_voice(page, frame, cfg):
    page.voice_id = choices('Voice', [], None)
    grid = QGridLayout()
    grid.setHorizontalSpacing(theme.SPACE_3)
    grid.setVerticalSpacing(theme.SPACE_2)
    page.voice_labels = {}
    for line, (name, control) in enumerate((('Voice', page.voice_id), (page.voice.accessibleName(), page.voice))):
        title = label(name)
        title.setBuddy(control)
        page.voice_labels[name] = title
        grid.addWidget(title, 0, line)
        grid.addWidget(control, 1, line, Qt.AlignmentFlag.AlignLeft)
    grid.setColumnStretch(0, 1)
    grid.setColumnStretch(1, 1)
    frame.layout().addLayout(grid)
    for line, (name, title, low, high, value) in enumerate((('speed', 'Speaking speed', 50, 200, cfg.get('speed', 1)*100),
                                                          ('volume', 'Volume', 0, 100, cfg.get('volume', .8)*100)), 2):
        caption, slider = label(title), TrySlider(Qt.Orientation.Horizontal)
        slider.setAccessibleName(title)
        slider.setRange(low, high)
        slider.setValue(round(float(value)))
        slider.setFixedWidth(140)
        slider.setFixedHeight(theme.HIT_TARGET_MIN)
        slider.setStyleSheet(stylesheet())
        value_label = label('')
        format_value = (lambda v: f'{v/100:g}×') if name == 'speed' else (lambda v: f'{v}%')
        slider.valueChanged.connect(lambda v, l=value_label, f=format_value: l.setText(f(v)))
        value_label.setText(format_value(slider.value()))
        caption.setBuddy(slider)
        page.voice_labels[title] = caption
        grid.addWidget(caption, 2, line - 2)
        tuning = QHBoxLayout()
        tuning.setSpacing(theme.SPACE_2)
        tuning.addWidget(slider)
        tuning.addWidget(value_label)
        tuning.addStretch()
        grid.addLayout(tuning, 3, line - 2)
        setattr(page, name, slider)
