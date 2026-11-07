"""Small, keyboard-reachable trial steps inside the existing tutorial."""
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSlider, QWidget, QVBoxLayout, QSizePolicy

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


class PaceSlider(QWidget):
    """The same seconds control in Settings and the Your pace trial."""
    currentIndexChanged = Signal()

    def __init__(self, config):
        super().__init__()
        from samsara.speech_pace import pause_seconds, STOPS, with_pause, profile
        self._starting_seconds = pause_seconds(config)
        self._starting_profile = profile(with_pause(config, self._starting_seconds))
        self.changed = False
        self.setAccessibleName('How long you can pause')  # PLACEHOLDER COPY (1040)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(390)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.readout = label('')
        layout.addWidget(self.readout)
        self.slider = TrySlider(Qt.Orientation.Horizontal)
        self.slider.setAccessibleName(self.accessibleName())
        self.slider.setRange(50, 800)
        self.slider.setSingleStep(5)
        self.slider.setPageStep(50)
        self.slider.setFixedHeight(theme.HIT_TARGET_MIN)
        self.slider.setStyleSheet(stylesheet())
        self.slider.setValue(round(pause_seconds(config) * 100))
        layout.addWidget(self.slider)
        marks = self.marks = QWidget()
        # 1040 FINISH IT 2: inherit the host card surface, including stop labels.
        marks.setStyleSheet('QWidget { background: transparent; }')
        marks.setFixedHeight(24)
        self.stop_labels = []
        for name, seconds in STOPS.items():
            mark = QLabel(name.capitalize(), marks)
            mark.setProperty('role', 'description')
            mark.adjustSize()
            x = round(8 + (seconds - .5) / 7.5 * (390 - 16))
            mark.move(max(0, min(390 - mark.width(), x - mark.width() // 2)), 0)
            self.stop_labels.append(mark)
        layout.addWidget(marks)
        self.setFocusProxy(self.slider)
        self.slider.valueChanged.connect(self._changed)
        self._changed()

    def seconds(self):
        return self.slider.value() / 100

    def setSeconds(self, seconds):
        self.changed = True
        tick = round(float(seconds) * 100)
        if tick == self.slider.value():
            self._changed()
        else:
            self.slider.setValue(tick)

    def currentData(self):
        from samsara.speech_pace import STOPS
        if not self.changed and self.seconds() == self._starting_seconds and self._starting_profile == 'custom':
            return 'custom'
        return next((name for name, stop in STOPS.items() if self.seconds() == stop), 'custom')

    def currentText(self):
        return f'{self.seconds():g} s · {self.currentData().capitalize()}'

    def _changed(self, *_args):
        if _args:
            self.changed = True
        self.readout.setText(self.currentText())
        self.slider.setAccessibleDescription(self.currentText() + '. Standard 2 s; Relaxed 5 s; Unhurried 8 s.')
        self.currentIndexChanged.emit()

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self, self._layout_stops)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_stops()

    def _layout_stops(self):
        from samsara.speech_pace import STOPS
        if not hasattr(self, 'stop_labels'):
            return
        for mark, seconds in zip(self.stop_labels, STOPS.values()):
            mark.ensurePolished()
            mark.adjustSize()
            x = round(8 + (seconds - .5) / 7.5 * (self.marks.width() - 16))
            mark.move(max(0, min(self.marks.width() - mark.width(), x - mark.width() // 2)), 0)
        self.marks.setFixedHeight(max(mark.sizeHint().height() for mark in self.stop_labels) + 2)


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
            control.setStyleSheet(control.styleSheet() + f'QPushButton:checked{{border-color:{theme.ACCENT};background:{theme.BG1};color:{theme.ACCENT};}}'
                f'QPushButton:checked:focus{{border-color:{theme.TEXT_PRIMARY};}}')
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


class PaceTrialBody(QWidget):
    """Keep reading text beside the controls when there is room to wrap it."""
    def __init__(self, instruction, sentence, state, description, pace):
        super().__init__()
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(theme.SPACE_2)
        self._columns = None
        self._intro, self._controls = QWidget(), QWidget()
        for body, widgets in ((self._intro, (instruction, sentence)),
                              (self._controls, (state, description, pace))):
            body.setStyleSheet('QWidget { background: transparent; }')
            column = QVBoxLayout(body)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(theme.SPACE_2)
            for widget in widgets:
                column.addWidget(widget)
            column.addStretch()
        self._arrange(False)

    def _arrange(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        self._grid.addWidget(self._intro, 0, 0)
        self._grid.addWidget(self._controls, 0 if columns else 1, 1 if columns else 0)
        self._grid.setColumnStretch(0, 1)
        self._grid.invalidate()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange(self.width() >= 700)


def build_steps(page, layout):
    cfg, window = page.app.config.get('tts', {}) or {}, page.window
    page.position = label('1 of 3')
    position_row = QHBoxLayout()
    position_row.addWidget(page.position)
    position_row.addWidget(label('Nothing is saved until you press Keep this.'), stretch=1)
    layout.addLayout(position_row)
    page.level = PlainChoices('How much Samsara talks', [('Silent', 'silent'), ('Brief', 'brief'), ('Full', 'full')], cfg.get('feedback_level', 'brief'))
    page.voice = choices(next(row[0] for row in BASE if row[2] == 'tts_engine'), [('Local', 'winrt'), ('Network', 'edge')], cfg.get('engine', 'winrt'))
    page.pace = PaceSlider(page.app.config)
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
            # 1179: reuse the available width before requiring vertical scrolling.
            for widget in (instruction, sentence, state):
                frame.layout().removeWidget(widget)
            frame.layout().addWidget(PaceTrialBody(instruction, sentence, state,
                                                  page.pace_description, page.pace))
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
