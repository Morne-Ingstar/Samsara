"""First-run decision pages, kept outside the no-growth wizard host (373)."""
from copy import deepcopy
import ctypes
import json
import os
import weakref
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, QObject, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QFrame,
    QHBoxLayout, QLabel, QLayout, QPushButton, QScrollArea, QRadioButton, QSizePolicy, QStyle, QStyleOptionButton, QVBoxLayout, QWidget,
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


def mark_setup_version_seen():
    """Introduce this release on successful setup, retaining Home's update rule."""
    from samsara.ui import home_qt
    settings = home_qt.QSettings(home_qt.SETTINGS_ORG, home_qt.SETTINGS_APP)
    settings.setValue(home_qt.WHATS_NEW_SEEN_KEY, home_qt.__version__)
    settings.sync()


class SetupSegmentedChoice(SegmentedChoice):
    def __init__(self, combo):
        super().__init__(combo)
        for button in self.buttons:
            # Reserve separate text and tick lanes inside the painted part.
            button.setFixedWidth(button.fontMetrics().horizontalAdvance(button.text()) + theme.SPACE_6)
            button.installEventFilter(self)
        self.fit_labels()

    def fit_labels(self):
        # 935: size the painted text after theme/font changes, including 200%.
        for button in self.buttons:
            button.setFixedSize(button.fontMetrics().horizontalAdvance(button.text()) + theme.SPACE_6,
                                max(theme.HIT_TARGET_MIN, button.fontMetrics().height() + theme.SPACE_2))
        self.setFixedSize(sum(button.width() for button in self.buttons) + 2,
                          max(button.height() for button in self.buttons) + 2)

    @staticmethod
    def paint_rects(button):
        inset = theme.SPACE_1 / 2
        box = QRectF(button.rect()).adjusted(inset, inset, -inset, -inset)
        tick = QRectF(box.right() - theme.SPACE_3, box.center().y() - theme.SPACE_1 / 2,
                      theme.SPACE_1, theme.SPACE_1)
        text = box.adjusted(theme.SPACE_1, 0, -theme.SPACE_3 - theme.SPACE_1, 0)
        return box, tick, text

    def eventFilter(self, widget, event):
        if widget in self.buttons and event.type() == QEvent.Type.FontChange:
            self.fit_labels()
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
    from samsara.wake_setup import normalize_wake_phrase
    normalize_wake_phrase(config)
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
    # 1006: Qt exposes checkable push buttons as checkboxes. Expansion is UI
    # state, while the accessible action remains a named button.
    _expanded = False

    def isChecked(self):
        return self._expanded

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class SetupCheckBox(QCheckBox):
    """Native checkbox semantics with a visible indicator in either theme."""

    def paintEvent(self, event):
        super().paintEvent(event)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        bounds = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, self)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(bounds).adjusted(1, 1, -1, -1)
        painter.fillRect(bounds, theme.qcolor(theme.BG0))
        painter.setPen(QPen(theme.qcolor(theme.TEXT_PRIMARY), 2))
        painter.setBrush(theme.qcolor(theme.ACCENT if self.isChecked() else theme.BG1))
        painter.drawRoundedRect(box, 3, 3)
        if self.isChecked():
            path = QPainterPath()
            path.moveTo(box.left() + box.width() * .2, box.center().y())
            path.lineTo(box.left() + box.width() * .43, box.bottom() - box.height() * .23)
            path.lineTo(box.right() - box.width() * .15, box.top() + box.height() * .23)
            painter.setPen(QPen(theme.qcolor(theme.TEXT_ON_ACCENT), 2))
            painter.drawPath(path)
        painter.end()


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
        if watched is getattr(self, 'phrase', None) and event.type() == QEvent.Type.FontChange:
            self.fit_phrase()
        if watched.property('setupChoiceQuestion'):
            if event.type() == QEvent.Type.FontChange:
                watched.setMaximumWidth(watched.fontMetrics().horizontalAdvance(watched.text()) + 2)
            elif event.type() == QEvent.Type.Resize:
                # The help target must not cap a wrapped heading at 44px.
                self.fit_question(watched)
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
            if watched is self.owner._tutorial_btn:
                watched.click()  # explicit Learn still works with Enter as a secondary action
                return True
            if getattr(watched, '_capturing', False) or watched.property('walkthrough_detail'):
                return False
            if self.owner._next_btn.isEnabled():
                self.owner._go_next()
            return True
        return False

    @staticmethod
    def fit_question(question):
        # 1006: QLabel.heightForWidth includes its old minimum, retaining a
        # narrow construction width even after the question has room to fit.
        question.setMinimumHeight(question.fontMetrics().boundingRect(
            QRect(0, 0, question.width(), 100000), Qt.TextFlag.TextWordWrap, question.text()).height())
        question.updateGeometry()

    def fit_progress(self):
        if self.closed:
            return
        self.owner._step_indicator._size_items()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.readiness_timer.stop()
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
        help_button.setAccessibleName(f'Help: {title}')
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
        button.setAccessibleName(f'Help: {title}')
        button.setAccessibleDescription(title)
        button.setProperty('walkthrough_detail', True)
        button.setObjectName('helpButton')
        button.setStyleSheet(f'QPushButton{{background:transparent;border:none;padding:0;color:{theme.TEXT_SECONDARY};}}'
                            f'QPushButton:focus{{border:1px solid {theme.ACCENT};border-radius:22px;}}')
        button.setFixedSize(44, 44)
        expanded = label('')
        expanded.hide()
        column.addWidget(expanded)
        row.addWidget(button)
        def toggle_detail():
            button._expanded = not button._expanded
            expanded.setText(text())
            expanded.setVisible(button._expanded)
            button.toggled.emit(button._expanded)
        button.clicked.connect(toggle_detail)
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
        widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        layout.addWidget(widget, alignment=Qt.AlignmentFlag.AlignLeft)
        return widget

    def recognition(self):
        w, layout = page(self.owner)
        self.reason = label('')
        layout.addWidget(self.reason)
        self.readiness = label('')
        layout.addWidget(self.readiness)
        self.readiness_timer = QTimer(self)
        self.readiness_timer.setInterval(500)
        self.readiness_timer.timeout.connect(self.refresh_readiness)
        self.readiness_timer.start()
        self.refresh_readiness()
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
        self.owner._model_group.buttonClicked.connect(lambda _: self.prepare_model_download())
        language = (self.owner._existing_config.get('language') if 'language' in self.owner._existing_config
                    else recommend_language(QLocale.system().name(), [code for _, code in LANGUAGES]))
        self.language = self.combo(layout, COPY['recognition.language'], LANGUAGES, language)
        layout.addStretch()
        return w

    def refresh_readiness(self):
        app = self.owner._samsara_app
        downloads = getattr(app, '_setup_model_download', None)
        preparing = downloads is not None and not getattr(app, 'model_loaded', False)
        self.owner._download_status.setVisible(preparing)
        self.owner._download_retry.setVisible(
            preparing and downloads.snapshot(self.owner._config['model_size']) == 'failed')
        self.readiness.setVisible(not preparing)
        if preparing:
            from samsara.setup_model_download import progress_text
            model = self.owner._config['model_size']
            text = progress_text(model, downloads.details(model))
            self.owner._download_status.setText(text)
            self.owner._download_status.show()
        elif app is None or not hasattr(app, 'model_loaded'):
            text = ('The speech model downloads, if needed, and loads after setup. '
                    'Wait for Samsara to finish getting ready before talking. '
                    'If a download fails, reconnect and restart Samsara to retry.')
        elif not app.model_loaded:
            from samsara.speech_readiness import status
            suffix = ('. Finish setup, then restart Samsara to try again.'
                      if getattr(app, '_speech_model_phase', '') == 'failed'
                      else '. You can finish setup while it gets ready.')
            text = status(app) + suffix
        else:
            text = 'The current speech model is loaded. A changed model applies after startup.'
        self.readiness.setText(text)  # PLACEHOLDER COPY (935)

    def retry_model_download(self):
        downloads = self.owner._samsara_app._setup_model_download
        downloads.request(self.owner._config['model_size'], retry=True)
        self.refresh_readiness()

    def prepare_model_download(self):
        downloads = getattr(self.owner._samsara_app, '_setup_model_download', None)
        if downloads is not None and not getattr(self.owner._samsara_app, 'model_loaded', False):
            selected = self.selected(self.owner._model_group)
            if selected is not None:
                self.owner._config['model_size'] = selected
                downloads.request(selected)
            self.refresh_readiness()

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
        self.prepare_model_download()

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
        saved = config_defaults.cfg_get(self.owner._config, 'wake_word_config.phrase')
        from samsara.ui.wake_setup_qt import configure_phrase_choice
        self.wake_fallback_note = configure_phrase_choice(
            self.phrase, saved, layout, fallback_pending=bool(
                self.owner._config['wake_word_config'].get('phrase_fallback_pending')))
        self.phrase.ensurePolished()
        self.fit_phrase()
        self.phrase.installEventFilter(self)
        phrase_layout.addWidget(self.phrase)
        self.wake._row.layout().addWidget(phrase_row)
        self.wake.buttonToggled.connect(lambda *_: phrase_row.setVisible(bool(self.selected(self.wake))))
        phrase_row.setVisible(bool(self.selected(self.wake)))
        self.wake_calibrate = QPushButton('Calibrate wake word')  # PLACEHOLDER COPY (919)
        theme.make_secondary(self.wake_calibrate)
        self.wake_calibrate.clicked.connect(self.owner._calibrate_wake)
        layout.addWidget(self.wake_calibrate, alignment=Qt.AlignmentFlag.AlignLeft)
        def wake_visibility(*_):
            enabled = bool(self.selected(self.wake))
            self.wake_calibrate.setVisible(enabled)
        self.wake.buttonToggled.connect(wake_visibility)
        wake_visibility()
        self.wake.buttonClicked.connect(
            lambda button: self.owner._calibrate_wake() if button.property('_value') else None)
        layout.addWidget(label(COPY['talking.tutorial']))
        layout.addStretch()
        return w

    def fit_phrase(self):
        width = max(self.phrase.fontMetrics().horizontalAdvance(self.phrase.itemText(i))
                    for i in range(self.phrase.count()))
        self.phrase.setFixedWidth(width + theme.SPACE_4 + theme.HIT_TARGET_MIN)

    @staticmethod
    def wake_phrases():
        from samsara.wake_setup import modelled_phrases
        return modelled_phrases()

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
            put(config, 'wake_word_config.phrase', self.phrase.currentData())
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
        layout.addWidget(label('Start Samsara when you are ready. Learning and extra settings are optional.'))  # PLACEHOLDER COPY (935)
        card = QFrame()
        theme.style_card(card)
        rows = QVBoxLayout(card)
        for _ in range(11):
            item = label('')
            self.owner._summary_labels.append(item)
            rows.addWidget(item)
        layout.addWidget(card)
        self.launch = SetupCheckBox(COPY['complete.startup'])
        self.launch.setAccessibleName(self.launch.text())
        self.launch.setChecked(self.owner._config['launch_at_login'])
        self.launch.installEventFilter(self)
        layout.addWidget(self.launch)
        self.include_beta_updates = SetupCheckBox('Include beta updates')
        self.include_beta_updates.setObjectName('includeBetaUpdatesCheckbox')
        self.include_beta_updates.setAccessibleName(self.include_beta_updates.text())
        self.include_beta_updates.setAccessibleDescription('Beta updates are experimental and less tested. This does not enable automatic checks.')
        self.include_beta_updates.setChecked(config_defaults.cfg_get(self.owner._config, 'updates.include_prerelease') is True)
        self.include_beta_updates.installEventFilter(self)
        layout.addWidget(self.include_beta_updates)
        # PLACEHOLDER COPY (935): one optional group, outside the five-step route.
        more = QPushButton('Optional settings and guides')
        more.setCheckable(True)
        theme.make_secondary(more)
        layout.addWidget(more, alignment=Qt.AlignmentFlag.AlignLeft)
        extras = QWidget()
        links = QVBoxLayout(extras)
        links.setContentsMargins(0, 0, 0, 0)
        for name, text in (
                ('Components', 'Add-ons'),
                ('Music', 'Music while recording'),
                ('Voice', 'Voice feedback'),
                ('Listening display', 'Listening display'),
                ('Position', 'Display position')):
            button = QPushButton(text)
            theme.make_secondary(button)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.clicked.connect(lambda checked=False, name=name: self.owner._show_optional(name))
            links.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        links.addWidget(label('Later: add-ons are in Settings > Components; music and voice in '
                              'Settings > Audio; display and position in Settings > General.'))
        for kind, target, text in (
                ('settings', 'General', 'Start and open Settings'),
                ('guide', 'open_mic_setup_guide', 'Start and open Mic Setup Guide'),
                ('guide', 'open_ava_guide', 'Start and open Ava Setup Guide')):
            button = QPushButton(text)
            theme.make_secondary(button)
            button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            button.setEnabled(self.owner._samsara_app is not None)
            button.clicked.connect(lambda checked=False, kind=kind, target=target:
                                   self.finish_to_destination(kind, target))
            links.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(extras)
        extras.hide()
        more.toggled.connect(extras.setVisible)
        self.more_options = more
        layout.addStretch()
        return w

    def summary(self):
        from samsara.ui.quick_reference_qt import _pretty_button, _pretty_key_combo
        cfg = self.owner._config
        lines = [f'Microphone: {self.owner._mic_combo.currentText()}',
            f'Recognition: {str(cfg["model_size"]).title()}', f'Language: {dict((code, name) for name, code in LANGUAGES).get(cfg["language"], cfg["language"])}',
            f'Recording: {COPY.get("talking." + cfg["mode"], cfg["mode"])} · {_pretty_key_combo(cfg["hotkey"])}',
            f'Hands-free: {"On" if config_defaults.cfg_get(cfg, "command_mode.enabled") else "Off"} · {_pretty_button(config_defaults.cfg_get(cfg, "command_mode.button")).replace(" (default)", "")}',
            ('Wake word: ' + (config_defaults.cfg_get(cfg, 'wake_word_config.phrase')
                             if cfg['wake_word_enabled'] else COPY['talking.off']))]
        visited = self.owner._visited_steps
        if 'Music' in visited:
            lines.append(f'Music: {dict((value, title) for value, title, _ in music_choices())[config_defaults.cfg_get(cfg, "ducking.recording_mode")]}')
        if 'Voice' in visited:
            lines.append(f'Voice feedback: {FEEDBACK_LEVEL_CHOICES[config_defaults.cfg_get(cfg, "tts.feedback_level")][0]}')
            if config_defaults.cfg_get(cfg, 'tts.feedback_level') != 'silent':
                lines.append(f'Voice: {COPY["voice.local"] if config_defaults.cfg_get(cfg, "tts.engine") == "winrt" else COPY["voice.network"]}')
        if 'Listening display' in visited:
            lines.append(f'Listening display: {self.owner._listening_display_page.group.checkedButton().text()}')
        lines.append('Other defaults can be changed in Settings.')  # PLACEHOLDER COPY (1006)
        for widget in self.owner._summary_labels:
            widget.clear()
            widget.hide()
        for widget, text in zip(self.owner._summary_labels, lines):
            widget.setText(text)
            widget.show()

    def finish_to_destination(self, kind, target):
        app = self.owner._samsara_app
        if app is None:
            return
        self.owner._finish()
        if self.owner.result is None:
            return
        # The app is only half constructed during first run. Reuse its existing
        # shell-ready barrier before calling an existing Settings/guide surface.
        timer = QTimer(QApplication.instance())
        timer.setInterval(250)
        app._setup_destination_timer = timer
        def open_when_ready():
            barrier = getattr(app, '_startup_shell_ready', None)
            if barrier is not None and not barrier.is_set():
                return
            timer.stop()
            timer.deleteLater()
            app._setup_destination_timer = None
            if kind == 'settings':
                from samsara.ui.settings_qt import SettingsQt
                if getattr(app, '_settings_qt', None) is None:
                    app._settings_qt = SettingsQt(app)
                app._settings_qt.show_page(target)
            else:
                getattr(app, target)()
        timer.timeout.connect(open_when_ready)
        timer.start()

    def finish(self):
        # Skipping Voice must never turn an unconsented network engine on.
        if (config_defaults.cfg_get(self.owner._config, 'tts.engine') == 'edge'
                and not config_defaults.cfg_get(self.owner._config, network_voice_notice.ACCEPTANCE_KEY)):
            put(self.owner._config, 'tts.engine', 'winrt')
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
        self.fit_phrase()
        self.owner._listening_display_page.prepare_question_rows(self.row_separator)
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
            if name in strip.steps:
                strip.steps[strip.steps.index(name)] = COPY[copy_key]
        strip.steps[strip.steps.index('Complete')] = 'Ready'  # PLACEHOLDER COPY (935)
        strip._refresh()
        self.fit_progress()
        strip.installEventFilter(self)

    def scroll_existing_pages(self):
        for name, widget in list(self.owner._page_by_name.items()):
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
            self.owner._page_by_name[name] = scroll
        from samsara.ui.first_run_wizard_qt import _STEPS
        self.owner._pages = [self.owner._page_by_name[name] for name, _ in _STEPS]
        for widget in self.owner._page_by_name.values():
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
        signature = (self.owner.width(), tuple(
            (widget.widget().layout().minimumSize().height(),
             widget.widget().layout().totalHeightForWidth(self.owner._stack.width()))
            for widget in self.owner._page_by_name.values()))
        if signature != getattr(self, '_fit_signature', None):
            self._fit_signature = signature
            self.owner._fit_available_screen()

    def fit_content(self, width, height):
        """1006: reserve the tallest page, retaining 519's expansion and fit."""
        owner = self.owner
        if owner._current_page is None:
            return min(760, height)
        root = owner.centralWidget().layout()
        header, title, nav = (root.itemAt(i).widget() for i in (0, 4, 7))
        from samsara.ui.listening_display_page import STEP_LABEL_GAP
        root.itemAt(2).spacerItem().changeSize(0, STEP_LABEL_GAP, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        header.setFixedHeight(64)
        title.setMinimumHeight(60)
        title.layout().setContentsMargins(28, 14, 28, 8)
        nav.setFixedHeight(theme.HIT_TARGET_MIN + 24)
        nav.layout().setContentsMargins(28, 12, 28, 12)

        # Populate Ready before its first visit, and measure hidden pages at
        # their eventual viewport width rather than their construction width.
        self.summary()
        def required(compact=False):
            tallest = 0
            for name, page in owner._page_by_name.items():
                content = page.widget()
                content.ensurePolished()
                layout = content.layout()
                margins = content._setup_margins
                layout.setContentsMargins(margins.left(), margins.top(), margins.right(),
                                          min(4, margins.bottom()) if compact else margins.bottom())
                content.resize(width, max(layout.minimumSize().height(), layout.totalHeightForWidth(width)))
                for child in [content] + content.findChildren(QWidget):
                    child.ensurePolished()
                    if child.layout() is not None:
                        child.layout().setGeometry(child.rect())
                for question in content.findChildren(QLabel):
                    if question.property('setupChoiceQuestion'):
                        self.fit_question(question)
                for nested in reversed(content.findChildren(QLayout)):
                    nested.invalidate()
                    nested.activate()
                layout.invalidate()
                layout.activate()
                page_height = max(layout.minimumSize().height(), layout.totalHeightForWidth(width))
                if name == 'Complete':
                    # Reserve optional summary rows without displaying choices
                    # the user has not made, or growing on the return to Ready.
                    page_height += sum(item.fontMetrics().height() + item.parentWidget().layout().spacing()
                                       for item in owner._summary_labels if item.isHidden())
                tallest = max(tallest, page_height)
            return tallest

        def chrome():
            return sum(max(root.itemAt(i).minimumSize().height(), root.itemAt(i).heightForWidth(owner.width()))
                       if root.itemAt(i).hasHeightForWidth() else root.itemAt(i).sizeHint().height()
                       for i in range(root.count()) if i != 5)
        needed = required()
        if needed + chrome() > height:
            # Preserve the stage-title gap while reclaiming header/footer space.
            header.setFixedHeight(max(36, owner._step_lbl.sizeHint().height() + 8))
            title.setMinimumHeight(0)
            title.layout().setContentsMargins(28, 8, 28, 4)
            nav.setFixedHeight(theme.HIT_TARGET_MIN + 8)
            nav.layout().setContentsMargins(28, 4, 28, 4)
            needed = required(compact=True)
        root.invalidate()
        # Hidden children's layout requests settle after their first geometry.
        # Recheck all pages before the first navigation, not only the open one.
        QTimer.singleShot(0, self, self.refit_page)
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
