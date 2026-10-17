"""First-run decision pages, kept outside the no-growth wizard host (373)."""
from copy import deepcopy
import ctypes
import json
import os
import weakref
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea, QRadioButton, QSizePolicy, QVBoxLayout, QWidget,
)

from samsara import autostart, config_defaults, session_modes
from samsara.config_schema import FEEDBACK_LEVEL_CHOICES, apply_feedback_level
from samsara.languages import LANGUAGES
from samsara.log import get_logger
from samsara.onboarding.first_run_recommend import (
    CHOICE_DETAILS, COPY, MODEL_REASONS, recommend_language, recommend_model,
)
from samsara.onboarding.mode_details import detail_from_config
from samsara.ui import network_voice_notice, theme
from samsara.ui.settings.calm_controls import SegmentedChoice
from samsara.ui.listening_display_page import ListeningDisplayPage

logger = get_logger(__name__)


class SetupSegmentedChoice(SegmentedChoice):
    def __init__(self, combo):
        super().__init__(combo)
        for button in self.buttons:
            # Reserve separate text and tick lanes inside the painted part.
            button.setFixedWidth(button.fontMetrics().horizontalAdvance(button.text()) + theme.SPACE_6)
            button.installEventFilter(self)
        self.setFixedWidth(sum(button.width() for button in self.buttons) + 2)

    @staticmethod
    def paint_rects(button):
        inset = theme.SPACE_1 / 2
        box = QRectF(button.rect()).adjusted(inset, inset, -inset, -inset)
        tick = QRectF(box.right() - theme.SPACE_3, box.center().y() - theme.SPACE_1 / 2,
                      theme.SPACE_1, theme.SPACE_1)
        text = box.adjusted(theme.SPACE_1, 0, -theme.SPACE_3 - theme.SPACE_1, 0)
        return box, tick, text

    def eventFilter(self, widget, event):
        if widget in self.buttons and event.type() == QEvent.Type.Paint:
            painter = QPainter(widget)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                box, tick, text = self.paint_rects(widget)
                fill = theme.BG2 if widget.isChecked() else (theme.HOVER_WASH if widget.underMouse() else theme.BG1)
                ink = theme.TEXT_PRIMARY if widget.isEnabled() else theme.TEXT_DISABLED
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.qcolor(fill))
                painter.drawRoundedRect(box, theme.CONTROL_RADIUS, theme.CONTROL_RADIUS)
                painter.setPen(theme.qcolor(ink))
                painter.setFont(widget.font())
                painter.drawText(text, Qt.AlignmentFlag.AlignCenter, widget.text())
                if widget.isChecked():
                    path = QPainterPath()
                    path.moveTo(tick.left(), tick.center().y())
                    path.lineTo(tick.left() + tick.width() / 3, tick.bottom())
                    path.lineTo(tick.right(), tick.top())
                    painter.setPen(QPen(theme.qcolor(ink), 2))
                    painter.drawPath(path)
                if widget.hasFocus():
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2))
                    painter.drawRoundedRect(box, theme.CONTROL_RADIUS, theme.CONTROL_RADIUS)
            finally:
                painter.end()
            return True
        return super().eventFilter(widget, event)

    def refresh(self, *args):
        super().refresh(*args)
        if self.group.checkedButton() is None:
            first = next((button for button in self.buttons if button.isEnabled()), None)
            if first is not None:
                first.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                self.setFocusProxy(first)


class SetupListeningDisplayPage(ListeningDisplayPage):
    def prepare_question_rows(self, separator):
        """Decorate setup rows without coupling navigation to layout positions."""
        layout = self.widget().layout()
        cards = [button.parentWidget() for button in self.group.buttons()]
        for card, button in zip(cards, self.group.buttons()):
            row = card.layout()
            for index in range(row.count()):
                text = row.itemAt(index).layout()
                if text is not None and text.indexOf(button) >= 0:
                    text.setSpacing(theme.SPACE_1)
        for card in cards[1:]:
            separator(layout, layout.indexOf(card))


def setup_page(owner):
    owner._listening_display_page = SetupListeningDisplayPage(owner._config, owner._stack)
    return owner._listening_display_page


def initial_config(defaults, config_path):
    config = deepcopy(defaults)
    for key in ('wake_word_enabled', 'ducking.recording_mode', 'tts.engine',
                'tts.network_voice_accepted', 'launch_at_login',
                'command_mode.enabled', 'command_mode.mode', 'command_mode.button',
                'command_mode.inactivity_timeout_s'):
        put(config, key, deepcopy(config_defaults.cfg_get({}, key)))
    existing = {}
    try:
        value = json.loads(Path(config_path).read_text(encoding='utf-8'))
        if isinstance(value, dict):
            existing = value
    except FileNotFoundError:
        # Fresh installs tap to start/stop; reruns retain their stored mode.
        put(config, 'command_mode.mode', 'toggle')
    except (OSError, ValueError):
        logger.warning('First-run existing config could not be read', exc_info=True)
    merge(config, existing)
    return config, existing


def merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge(target[key], value)
        else:
            target[key] = deepcopy(value)


def put(config, key, value):
    parts = key.split('.')
    for part in parts[:-1]:
        if not isinstance(config.get(part), dict):
            config[part] = {}
        config = config.setdefault(part, {})
    config[parts[-1]] = value


def music_choices():
    """Settings labels, checked against its row in the focused regression gate."""
    labels = {'duck': 'Duck', 'pause': 'Pause', 'off': 'Leave alone'}
    return [(value, labels[value], CHOICE_DETAILS[value])
            for value in config_defaults.SETTINGS_SCHEMA['ducking.recording_mode']['options']]


def hardware_facts():
    """Read hardware off Qt; use the app's CUDA APIs without loading a model."""
    from samsara.ui.first_run_qt import nvidia_gpu_present
    facts = {'cpu_cores': os.cpu_count(), 'ram_gb': None, 'gpu_usable': None}
    try:
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in (
                    'physical', 'available', 'pagefile', 'available_pagefile',
                    'virtual', 'available_virtual', 'extended')]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            facts['ram_gb'] = memory.physical / (1024 ** 3)
    except (AttributeError, OSError):
        logger.info('First-run RAM fact unavailable')
    try:
        present = nvidia_gpu_present()
        if present is False:
            facts['gpu_usable'] = False
        elif present is True:
            # Same support check as dictation's loader; the only native import
            # stays inside this worker, never the UI constructor or page change.
            import ctranslate2
            facts['gpu_usable'] = (ctranslate2.get_cuda_device_count() > 0
                                   and bool(ctranslate2.get_supported_compute_types('cuda')))
    except Exception:
        logger.info('First-run GPU usability fact unavailable')
    for key, value in facts.items():
        if value is None:
            logger.info('First-run hardware fact absent: %s', key)
    return facts


def cuda_chosen(components_page):
    """The unchanged Components page installs, rather than merely ticks, CUDA."""
    from samsara.ui.first_run_qt import component_installed
    manifest = getattr(components_page, '_manifest', None) or {}
    component = next((c for c in manifest.get('components', []) if c['id'] == 'cuda-pack'), None)
    if component is None:
        component = {'id': 'cuda-pack', 'install_dir': '_internal/ctranslate2'}
    return component_installed(component, components_page._app_root, components_page._downloads)


def label(text):
    widget = QLabel(text)
    widget.setWordWrap(True)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setStyleSheet(f'color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;')
    return widget


def page(owner):
    scroll = QScrollArea(owner._stack)
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body, layout = owner._padded()
    layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    scroll.setWidget(body)
    return scroll, layout


class _DetailsButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class Decisions(QObject):
    facts_ready = Signal(dict)

    def __init__(self, owner):
        super().__init__(owner)
        self._owner_ref = weakref.ref(owner)
        self.closed = False
        self.facts = {}
        self.fact_probe_started = False
        self.mic_restored = False
        self.model_picked = 'model_size' in owner._existing_config
        self.facts_ready.connect(self.receive_facts)
        self.details = []

    @property
    def owner(self):
        return self._owner_ref()

    def load_facts(self):
        try:
            facts = hardware_facts()
        except Exception:
            logger.warning('First-run hardware facts unavailable', exc_info=True)
            facts = {}
        if not self.closed:
            try:
                self.facts_ready.emit(facts)
            except RuntimeError:
                logger.debug('First-run window closed before hardware result')

    def receive_facts(self, facts):
        if self.closed or self.owner.result is not None:
            return
        self.fact_probe_started = True
        self.facts = facts
        self.refresh_recognition()

    def eventFilter(self, watched, event):
        if watched.property('setupChoiceQuestion'):
            if event.type() == QEvent.Type.FontChange:
                watched.setMaximumWidth(watched.fontMetrics().horizontalAdvance(watched.text()) + 2)
            elif event.type() == QEvent.Type.Resize:
                # The help target must not cap a wrapped heading at 44px.
                watched.setMinimumHeight(watched.heightForWidth(watched.width()))
        if event.type() == QEvent.Type.LayoutRequest and watched.property('setupFitContent'):
            QTimer.singleShot(0, self, self.refit_page)
        if self.closed:
            return False
        if watched is self.owner._step_indicator and event.type() == QEvent.Type.Resize:
            QTimer.singleShot(0, self, self.fit_progress)
        if event.type() == QEvent.Type.KeyPress and watched in self.owner._model_group.buttons():
            if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Space):
                self.model_picked = True
        if event.type() == QEvent.Type.KeyPress:
            arrows = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Left, Qt.Key.Key_Right)
            if event.key() in arrows:
                for group in (self.owner._model_group, self.owner._listening_display_page.group):
                    buttons = group.buttons()
                    if watched in buttons:
                        direction = -1 if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Left) else 1
                        candidate = buttons[(buttons.index(watched) + direction) % len(buttons)]
                        candidate.click()
                        candidate.setFocus()
                        return True
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if getattr(watched, '_capturing', False) or watched.property('walkthrough_detail'):
                return False
            if self.owner._next_btn.isEnabled():
                self.owner._go_next()
            return True
        return False

    def fit_progress(self):
        if self.closed:
            return
        self.owner._step_indicator._size_items()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.owner._mic_result.disconnect(self.restore_saved_mic)
        for signal in (self.owner._model_group.buttonClicked, self.wake.buttonToggled,
                       self.level.buttonToggled, self.engine.buttonClicked, self.facts_ready):
            signal.disconnect()
        for button, _ in self.details:
            button.clicked.disconnect()
        for control in self.owner.findChildren(QObject):
            control.blockSignals(True)
            control.removeEventFilter(self)

    def recognition_choices(self, layout, rows, selected, details=None):
        group = QButtonGroup(self)
        for index, (value, title, description) in enumerate(rows):
            row = QHBoxLayout()
            row.setSpacing(theme.SPACE_3)
            column = QVBoxLayout()
            column.setSpacing(theme.SPACE_1)
            heading = QHBoxLayout()
            heading.setSpacing(theme.SPACE_1)
            heading.addWidget(label(title))
            if details:
                self.detail(heading, column, title, lambda value=value: details(value))
            heading.addStretch()
            column.insertLayout(0, heading)
            if description:
                column.insertWidget(1, label(description))
            row.addLayout(column, 1)
            rb = QRadioButton()
            rb.setStyleSheet('QRadioButton{background:transparent;padding:0;spacing:0;}'
                            'QRadioButton::indicator{width:16px;height:16px;border-radius:10px;}')
            rb.setAccessibleName(title)
            rb.setProperty('_value', value)
            rb.setAccessibleDescription(description)
            rb.setFixedSize(theme.HIT_TARGET_MIN, theme.HIT_TARGET_MIN)
            rb.installEventFilter(self)
            group.addButton(rb, index)
            rb.setChecked(value == selected)
            row.addWidget(rb, 0, Qt.AlignmentFlag.AlignTop)
            layout.addLayout(row)
        return group

    @staticmethod
    def row_separator(layout, index=None):
        # Same hairline token and width as Settings presentation.style().
        space = QWidget()
        space.setProperty('walkthrough_row_separator', True)
        column = QVBoxLayout(space)
        column.setContentsMargins(0, theme.SPACE_1 // 2, 0, theme.SPACE_1 // 2)
        column.setSpacing(0)
        line = QFrame(space)
        line.setObjectName('walkthroughRowHairline')
        line.setFixedHeight(1)
        line.setStyleSheet(f'background:{theme.BORDER_FAINT};border:none;')
        column.addWidget(line)
        layout.insertWidget(layout.count() if index is None else index, space)
        return space

    def choices(self, layout, title, rows, selected, details):
        section = QWidget()
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SPACE_1)
        row, heading = QHBoxLayout(), QHBoxLayout()
        heading.setSpacing(theme.SPACE_1)
        question = label(title)
        question.setFont(theme.qfont(theme.TYPE_BODY))
        question.ensurePolished()
        question.setMaximumWidth(question.fontMetrics().horizontalAdvance(title) + 2)
        question.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        question.setProperty('setupChoiceQuestion', True)
        question.installEventFilter(self)
        text_area = QWidget()
        text_column = QVBoxLayout(text_area)
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(theme.SPACE_1)
        text_column.addLayout(heading)
        heading.addWidget(question, 1)
        heading.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.detail(heading, text_column, title, lambda: details(combo.currentData()) or '')
        help_button, expanded = self.details[-1]
        help_button.setAccessibleName(title)
        row.addWidget(text_area, 1)
        combo = QComboBox()
        for value, text, _ in rows:
            combo.addItem(text, value)
        combo.setCurrentIndex(combo.findData(selected))
        segments = SetupSegmentedChoice(combo)
        segments.setAccessibleName(title)
        segments.setStyleSheet(f'QFrame#settingsSegments{{background:{theme.BG1};'
                              f'border:1px solid {theme.BORDER};border-radius:{theme.CONTROL_RADIUS}px;}}')
        group = segments.group
        description = label('')
        description.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
        def refresh(*_):
            chosen = combo.currentIndex()
            help_button.setEnabled(chosen >= 0)
            text = rows[chosen][2] if chosen >= 0 else ''
            description.setText(text)
            description.setVisible(bool(text) and not help_button.isChecked())
            expanded.setText((details(combo.currentData()) or '') if chosen >= 0 else '')
            expanded.setVisible(help_button.isChecked() and bool(expanded.text()))
            for index, button in enumerate(segments.buttons):
                button.setAccessibleDescription(details(rows[index][0]) or rows[index][2])
        for index, button in enumerate(segments.buttons):
            button.setProperty('_value', rows[index][0])
            button.installEventFilter(self)
        group.buttonToggled.connect(lambda button, checked: combo.setCurrentIndex(group.id(button)) if checked else None)
        combo.currentIndexChanged.connect(refresh)
        help_button.toggled.connect(refresh)
        group._row, group._description, group._segments = section, description, segments
        row.addWidget(segments, 0, Qt.AlignmentFlag.AlignVCenter)
        column.insertLayout(0, row)
        text_column.addWidget(description)
        layout.addWidget(section)
        refresh()
        return group

    def detail(self, row, column, title, text):
        # Small visual help beside its label, retaining a 44px keyboard/tap target.
        button = _DetailsButton('?')
        button.setAccessibleName(button.text())
        button.setAccessibleDescription(title)
        button.setCheckable(True)
        button.setProperty('walkthrough_detail', True)
        button.setObjectName('helpButton')
        button.setStyleSheet(f'QPushButton{{background:transparent;border:none;padding:0;color:{theme.TEXT_SECONDARY};}}'
                            f'QPushButton:focus{{border:1px solid {theme.ACCENT};border-radius:22px;}}')
        button.setFixedSize(44, 44)
        expanded = label('')
        expanded.hide()
        column.addWidget(expanded)
        row.addWidget(button)
        button.clicked.connect(lambda opened: (expanded.setText(text()), expanded.setVisible(opened)))
        self.details.append((button, expanded))

    def mode_detail(self, mode):
        from samsara.onboarding.guidance_facts import facts_from_config
        facts = facts_from_config(self.owner._config)
        config = deepcopy(self.owner._config)
        if hasattr(self, 'hands_free'):
            put(config, 'command_mode.button', self.hands_free.combo)
        return detail_from_config(mode, config,
            paste_word=facts.session_end.commit_word, help_phrase='',
            exit_phrase=session_modes.SESSION_SLEEP_PHRASES[0])

    def combo(self, layout, title, items, selected):
        layout.addWidget(label(title))
        widget = QComboBox()
        widget.setAccessibleName(title)
        for name, value in items:
            widget.addItem(name, value)
        index = widget.findData(selected)
        if index < 0:
            widget.addItem(str(selected), selected)
            index = widget.count() - 1
        widget.setCurrentIndex(index)
        widget.installEventFilter(self)
        layout.addWidget(widget)
        return widget

    def recognition(self):
        w, layout = page(self.owner)
        self.reason = label('')
        layout.addWidget(self.reason)
        layout.addWidget(label(COPY['recognition.change']))
        rows = [('tiny', 'Tiny', 'Uses the least memory.'),
                ('base', 'Base', 'Balances accuracy and memory.'),
                ('small', 'Small', 'Uses more memory for better accuracy.')]
        current = self.owner._config['model_size']
        if current not in [value for value, _, _ in rows]:
            current_label = label(f'{COPY["recognition.current"]}: {current}')
            layout.addWidget(current_label)
        self.owner._model_group = self.recognition_choices(layout, rows, self.owner._config['model_size'])
        if current not in [value for value, _, _ in rows]:
            self.owner._model_group.buttonClicked.connect(lambda _: current_label.hide())
        self.owner._model_group.buttonClicked.connect(lambda _: setattr(self, 'model_picked', True))
        language = (self.owner._existing_config.get('language') if 'language' in self.owner._existing_config
                    else recommend_language(QLocale.system().name(), [code for _, code in LANGUAGES]))
        self.language = self.combo(layout, COPY['recognition.language'], LANGUAGES, language)
        layout.addStretch()
        return w

    def refresh_recognition(self):
        if 'model_size' in self.owner._existing_config:
            self.reason.hide()
            return
        facts = dict(self.facts, cuda_chosen=cuda_chosen(self.owner._components_page))
        size, reason = recommend_model(facts)
        self.reason.setText(MODEL_REASONS[reason])
        self.reason.show()
        if not self.model_picked:
            for button in self.owner._model_group.buttons():
                button.setChecked(button.property('_value') == size)
            self.owner._config['model_size'] = size

    def _hold_key(self, layout, action, title):
        # 373 owns this page: setup asks only the record and hands-free keys.
        if action not in ('hotkey', 'command_mode.button'):
            return None
        from samsara.ui.first_run_wizard_qt import _HotkeyBtn
        from samsara.hold_key_rules import HOLD_DEFAULTS, capture_overrides, config_value
        section = QWidget()
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(theme.SPACE_1)
        row, heading = QHBoxLayout(), QHBoxLayout()
        text_area = QWidget()
        text_column = QVBoxLayout(text_area)
        text_column.setContentsMargins(0, 0, 0, 0)
        text_column.setSpacing(theme.SPACE_1)
        text_column.addWidget(label(title))
        heading.addWidget(text_area)
        if action == 'command_mode.button':
            self.detail(heading, column, title, lambda: self.mode_detail('hands_free'))
        heading.addStretch()
        row.addLayout(heading, 1)
        if action == 'command_mode.button' and not config_defaults.cfg_get(self.owner._config, 'command_mode.enabled'):
            note = label(COPY['talking.session_off'])
            self.session_off_note = note
            note.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
            text_column.addWidget(note)
        button = _HotkeyBtn(config_value(self.owner._config, action, HOLD_DEFAULTS[action][1]))
        owner_ref = self._owner_ref
        button.configure_hold(action, lambda: owner_ref()._config,
                              lambda: capture_overrides(owner_ref()._hotkey_btns))
        button.setAccessibleName(title)
        button.installEventFilter(self)
        self.owner._hotkey_btns[action] = button
        button.setMinimumWidth(0)
        button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        column.insertLayout(0, row)
        column.addWidget(button.reason_label())
        layout.addWidget(section)
        return button

    def talking(self):
        w, layout = page(self.owner)
        current_mode = self.owner._config['mode']
        if current_mode not in ('hold', 'toggle'):
            current_mode_label = label(f'{COPY["talking.current"]}: {current_mode}')
            layout.addWidget(current_mode_label)
        self.mode = self.choices(layout, COPY['talking.question'],
            [('hold', COPY['talking.hold'], self.mode_detail('hold').split('. ')[0] + '.'),
             ('toggle', COPY['talking.toggle'], self.mode_detail('tap').split('. ')[0] + '.')], self.owner._config['mode'],
            lambda value: self.mode_detail('hold' if value == 'hold' else 'tap'))
        if current_mode not in ('hold', 'toggle'):
            self.mode.buttonClicked.connect(lambda _: current_mode_label.hide())
        self.row_separator(layout)
        self._hold_key(layout, 'hotkey', COPY['talking.record_key'])
        self.row_separator(layout)
        from samsara.ui.first_run_hands_free_qt import HandsFreeOffer
        self.hands_free_offer = HandsFreeOffer(self.owner._config, w)
        layout.addWidget(self.hands_free_offer)
        self.hands_free = self._hold_key(layout, 'command_mode.button', COPY['talking.hands_free'])
        self.hands_free.bindingChanged.connect(self.hands_free_offer.refresh)
        if hasattr(self, 'session_off_note'):
            self.hands_free_offer.combo.currentIndexChanged.connect(
                lambda _: self.session_off_note.setVisible(not self.hands_free_offer.combo.currentData()))
        self.row_separator(layout)
        self.wake = self.choices(layout, COPY['talking.wake'],
            [(True, COPY['talking.on'], self.mode_detail('wake_word').split('. ')[0] + '.'),
             (False, COPY['talking.off'], '')], self.owner._config['wake_word_enabled'],
            lambda _: self.mode_detail('wake_word'))
        phrase_row = QWidget()
        phrase_layout = QHBoxLayout(phrase_row)
        phrase_layout.setContentsMargins(0, layout.spacing() - self.wake._row.layout().spacing(), 0, 0)
        phrase_layout.addWidget(label(COPY['talking.phrase']))
        phrase_layout.addStretch()
        self.phrase = QComboBox(phrase_row)
        self.phrase.setAccessibleName(COPY['talking.phrase'])
        for phrase in self.wake_phrases():
            self.phrase.addItem(phrase, phrase)
        saved = config_defaults.cfg_get(self.owner._config, 'wake_word_config.phrase')
        if self.phrase.findText(saved) < 0:
            self.phrase.addItem(saved, saved)
        self.phrase.setCurrentText(saved)
        self.phrase.ensurePolished()
        phrase_width = max(self.phrase.fontMetrics().horizontalAdvance(self.phrase.itemText(i))
                           for i in range(self.phrase.count()))
        self.phrase.setFixedWidth(phrase_width + theme.SPACE_4 + theme.HIT_TARGET_MIN)
        self.phrase.installEventFilter(self)
        phrase_layout.addWidget(self.phrase)
        self.wake._row.layout().addWidget(phrase_row)
        self.wake.buttonToggled.connect(lambda *_: phrase_row.setVisible(bool(self.selected(self.wake))))
        phrase_row.setVisible(bool(self.selected(self.wake)))
        layout.addWidget(label(COPY['talking.tutorial']))
        layout.addStretch()
        return w

    @staticmethod
    def wake_phrases():
        from samsara.constants import DEFAULT_WAKE_PHRASE_OPTIONS
        return DEFAULT_WAKE_PHRASE_OPTIONS

    def music(self):
        w, layout = page(self.owner)
        self.music_group = self.choices(layout, COPY['music.question'], music_choices(),
            config_defaults.cfg_get(self.owner._config, 'ducking.recording_mode'), CHOICE_DETAILS.get)
        layout.addStretch()
        return w

    def voice(self):
        w, layout = page(self.owner)
        self.level = self.choices(layout, COPY['voice.question'],
            [(value, *copy) for value, copy in FEEDBACK_LEVEL_CHOICES.items()],
            config_defaults.cfg_get(self.owner._config, 'tts.feedback_level'), CHOICE_DETAILS.get)
        self.engine_area = QWidget()
        engines = QVBoxLayout(self.engine_area)
        engines.setContentsMargins(0, 0, 0, 0)
        engines.setSpacing(layout.spacing())
        self.row_separator(engines)
        self.engine = self.choices(engines, COPY['voice.engine'],
            [('winrt', COPY['voice.local'], CHOICE_DETAILS['winrt']), ('edge', COPY['voice.network'], COPY['voice.natural'])],
            config_defaults.cfg_get(self.owner._config, 'tts.engine'), CHOICE_DETAILS.get)
        self.engine.buttonClicked.connect(self.choose_engine)
        self.level.buttonToggled.connect(lambda *_: self.engine_area.setVisible(self.selected(self.level) != 'silent'))
        self.engine_area.setVisible(self.selected(self.level) != 'silent')
        layout.addWidget(self.engine_area)
        layout.addStretch()
        return w

    def choose_engine(self, button):
        if button.property('_value') == 'edge' and not config_defaults.cfg_get(self.owner._config, network_voice_notice.ACCEPTANCE_KEY):
            if network_voice_notice.request_acceptance(self.owner):
                put(self.owner._config, network_voice_notice.ACCEPTANCE_KEY, True)
            else:
                self.engine.buttons()[0].setChecked(True)

    @staticmethod
    def selected(group):
        button = group.checkedButton()
        return button.property('_value') if button is not None else None

    def collect(self, name):
        config = self.owner._config
        if name == 'Recognition':
            selected = self.selected(self.owner._model_group)
            if selected is not None:
                config['model_size'] = selected
            config['language'] = self.language.currentData()
        elif name == 'Talking':
            # An existing continuous mode is retained until the owner picks hold/tap.
            if self.mode.checkedButton() is not None:
                config['mode'] = self.selected(self.mode)
            for path, button in self.owner._hotkey_btns.items():
                put(config, path, button.combo)
            put(config, 'command_mode.enabled', self.hands_free_offer.combo.currentData())
            config['wake_word_enabled'] = self.selected(self.wake)
            put(config, 'wake_word_config.phrase', self.phrase.currentText().strip())
        elif name == 'Music':
            put(config, 'ducking.recording_mode', self.selected(self.music_group))
        elif name == 'Voice':
            level = self.selected(self.level)
            apply_feedback_level(config, level)
            if level != 'silent':
                engine = self.selected(self.engine)
                if engine == 'edge' and not config_defaults.cfg_get(config, network_voice_notice.ACCEPTANCE_KEY):
                    engine = 'winrt'
                    self.engine.buttons()[0].setChecked(True)
                put(config, 'tts.engine', engine)

    def complete(self):
        w, layout = page(self.owner)
        layout.addWidget(label('Check your choices, then start Samsara.'))
        card = QFrame()
        theme.style_card(card)
        rows = QVBoxLayout(card)
        for _ in range(10):
            item = label('')
            self.owner._summary_labels.append(item)
            rows.addWidget(item)
        layout.addWidget(card)
        self.launch = QCheckBox(COPY['complete.startup'])
        self.launch.setAccessibleName(self.launch.text())
        self.launch.setChecked(self.owner._config['launch_at_login'])
        self.launch.installEventFilter(self)
        layout.addWidget(self.launch)
        self.include_beta_updates = QCheckBox('Include beta updates')
        self.include_beta_updates.setObjectName('includeBetaUpdatesCheckbox')
        self.include_beta_updates.setAccessibleName(self.include_beta_updates.text())
        self.include_beta_updates.setAccessibleDescription('Beta updates are experimental and less tested. This does not enable automatic checks.')
        self.include_beta_updates.setChecked(config_defaults.cfg_get(self.owner._config, 'updates.include_prerelease') is True)
        self.include_beta_updates.installEventFilter(self)
        layout.addWidget(self.include_beta_updates)
        layout.addStretch()
        return w

    def summary(self):
        from samsara.ui.quick_reference_qt import _pretty_button, _pretty_key_combo
        cfg = self.owner._config
        lines = [f'Microphone: {self.owner._mic_combo.currentText()}',
            f'Recognition: {str(cfg["model_size"]).title()}', f'Language: {dict((code, name) for name, code in LANGUAGES).get(cfg["language"], cfg["language"])}',
            f'Recording: {COPY.get("talking." + cfg["mode"], cfg["mode"])} · {_pretty_key_combo(cfg["hotkey"])}',
            f'Hands-free: {"On" if config_defaults.cfg_get(cfg, "command_mode.enabled") else "Off"} · {_pretty_button(config_defaults.cfg_get(cfg, "command_mode.button")).replace(" (default)", "")}',
            f'Wake word: {COPY["talking.on"] if cfg["wake_word_enabled"] else COPY["talking.off"]} · {config_defaults.cfg_get(cfg, "wake_word_config.phrase")}',
            f'Music: {dict((value, title) for value, title, _ in music_choices())[config_defaults.cfg_get(cfg, "ducking.recording_mode")]}',
            f'Voice feedback: {FEEDBACK_LEVEL_CHOICES[config_defaults.cfg_get(cfg, "tts.feedback_level")][0]}',
            f'Voice: {COPY["voice.local"] if config_defaults.cfg_get(cfg, "tts.engine") == "winrt" else COPY["voice.network"]}',
            f'Listening display: {self.owner._listening_display_page.group.checkedButton().text()}']
        for widget, text in zip(self.owner._summary_labels, lines):
            widget.setText(text)

    def finish(self):
        put(self.owner._config, 'updates.include_prerelease', self.include_beta_updates.isChecked())
        requested = self.launch.isChecked()
        if requested or self.owner._existing_config.get('launch_at_login', False):
            try:
                autostart.sync_run_entry(requested)
            except Exception as exc:
                logger.warning('First-run Windows startup entry failed: %s', exc)
                requested = False
        self.owner._config['launch_at_login'] = requested

    def prepare_navigation(self):
        self.owner._listening_display_page.prepare_question_rows(self.row_separator)
        self.owner._mic_result.connect(self.restore_saved_mic)
        for control in (self.owner._next_btn, self.owner._back_btn, self.owner._mic_combo):
            control.setAccessibleName(control.text() if isinstance(control, QAbstractButton) else 'Microphone')
        for item in self.owner.findChildren(QAbstractButton):
            if not item.accessibleName():
                item.setAccessibleName(item.text())
            item.installEventFilter(self)
        for page_widget in self.owner._pages:
            for item in page_widget.findChildren(QAbstractButton):
                if not item.accessibleName():
                    item.setAccessibleName(item.text())
        self.owner._next_btn.installEventFilter(self)
        self.owner._mic_combo.installEventFilter(self)
        # Short progress labels leave the full question in the page heading.
        strip = self.owner._step_indicator
        for name, copy_key in [('Microphone', 'progress.microphone'),
                ('Components', 'progress.components'), ('Recognition', 'progress.recognition'),
                ('Listening display', 'progress.display')]:
            strip.steps[strip.steps.index(name)] = COPY[copy_key]
        strip._refresh()
        self.fit_progress()
        strip.installEventFilter(self)

    def restore_saved_mic(self, message, _color):
        if self.closed or self.mic_restored or not self.owner._mics:
            return
        if message not in ('_load_done_', '_refresh_done_'):
            return
        self.mic_restored = True
        saved = self.owner._existing_config.get('microphone')
        for mic in self.owner._mics:
            if mic['id'] == saved:
                self.owner._mic_combo.setCurrentText(mic['name'])
                break

    def scroll_existing_pages(self):
        for index, widget in enumerate(self.owner._pages):
            for item in widget.findChildren(QLabel):
                item.setWordWrap(True)
            if isinstance(widget, QScrollArea):
                continue
            scroll = QScrollArea(self.owner._stack)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            widget.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
            scroll.setWidget(widget)
            self.owner._pages[index] = scroll
        for widget in self.owner._pages:
            widget.widget()._setup_margins = widget.widget().layout().contentsMargins()
            widget.widget().setProperty("setupFitContent", True)
            widget.widget().installEventFilter(self)
            widget.setStyleSheet(theme._scrollbar_qss())
            from samsara.ui.settings.calm_layout import ScrollEdges
            widget._setup_edges = ScrollEdges(widget)
            widget.hide()

    def refit_page(self):
        page = self.owner._current_page
        if self.closed or page is None or not page.isVisible():
            return
        layout = page.widget().layout()
        signature = (page, self.owner.width(), layout.minimumSize().height(),
                     layout.totalHeightForWidth(self.owner.width()))
        if signature != getattr(self, '_fit_signature', None):
            self._fit_signature = signature
            self.owner._fit_available_screen()

    def fit_content(self, width, height):
        """Use available height before scrolling; retain 502's frame placement."""
        owner = self.owner
        page = owner._current_page
        if page is None:
            return min(760, height)
        root = owner.centralWidget().layout()
        header, title, nav = (root.itemAt(i).widget() for i in (0, 4, 7))
        root.itemAt(2).spacerItem().changeSize(0, 12, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        header.setFixedHeight(64)
        title.setMinimumHeight(60)
        title.layout().setContentsMargins(28, 14, 28, 8)
        nav.setFixedHeight(theme.HIT_TARGET_MIN + 24)
        nav.layout().setContentsMargins(28, 12, 28, 12)
        content = page.widget()
        content.ensurePolished()
        layout = content.layout()
        layout.setContentsMargins(content._setup_margins)
        layout.activate()
        needed = max(layout.minimumSize().height(), layout.totalHeightForWidth(width))
        chrome = lambda: sum(root.itemAt(i).sizeHint().height() for i in range(root.count()) if i != 5)
        if needed + chrome() > height:
            # Minimums: strip gap 2, header 36, title 8/4, footer/page bottom 4.
            root.itemAt(2).spacerItem().changeSize(0, 2, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
            header.setFixedHeight(max(36, owner._step_lbl.sizeHint().height() + 8))
            title.setMinimumHeight(0)
            title.layout().setContentsMargins(28, 8, 28, 4)
            nav.setFixedHeight(theme.HIT_TARGET_MIN + 8)
            nav.layout().setContentsMargins(28, 4, 28, 4)
            margins = content._setup_margins
            layout.setContentsMargins(margins.left(), margins.top(), margins.right(), min(4, margins.bottom()))
            layout.activate()
            needed = max(layout.minimumSize().height(), layout.totalHeightForWidth(width))
        root.invalidate()
        return min(height, max(min(760, height), needed + chrome()))

    def prepare_step(self, name):
        self.owner._next_btn.setAccessibleName(self.owner._next_btn.text())
        metrics = self.owner._next_btn.fontMetrics()
        self.owner._next_btn.setFixedWidth(max(150, metrics.horizontalAdvance('Start Samsara') + 48))
        self.owner._next_btn.setEnabled(name != 'Microphone')
        if name in ('Components', 'Recognition') and not self.fact_probe_started and 'model_size' not in self.owner._existing_config:
            from samsara.runtime import thread_registry
            self.fact_probe_started = True
            thread_registry.spawn('first_run.hardware', self.load_facts, daemon=True)
        if name == 'Recognition':
            self.refresh_recognition()
        QTimer.singleShot(0, self, self.fit_progress)
