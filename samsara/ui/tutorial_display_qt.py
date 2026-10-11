"""Listening placement page shared by setup and the tutorial (490)."""
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from samsara import listening_display
from samsara.onboarding.display_placement import current_position, placement_updates, choose_for_app
from samsara.ui import theme
from samsara.ui.widgets.indicator_demo import CAPTIONS, IndicatorDemo
from samsara.ui.widgets.indicator_place import IndicatorPlacePicker

# PLACEHOLDER COPY (490)
TITLE = 'Where it sits'


def placement_monitor(config, app=None):
    """Use the controller's screen identity and keep its connected selection."""
    controller = getattr(app, 'live_surface', None)
    if callable(getattr(controller, '_screens', None)):
        screens = controller._screens()
        preferred = (((config.get('ui') or {}).get('live_surface') or {}).get('placement') or {}).get('preferred_monitor')
        selected = next((screen for screen in screens if screen.id == preferred), None)
        if selected is not None:
            return selected.id
    screens = QGuiApplication.screens()
    primary = QGuiApplication.primaryScreen()
    if primary is None:
        return None
    return primary.name() or f'screen-{screens.index(primary)}'


class DisplayPlacementPage(QWidget):
    def __init__(self, config, parent=None, *, app=None, setup=False):
        super().__init__(parent)
        self.config, self.app, self.setup = config, app, setup
        display = listening_display.display_choice(config, setup=setup).value
        self.setFont(theme.qfont(theme.TYPE_BODY))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 0 if setup else 8, 28, 0 if setup else 8)
        layout.setSpacing(2 if setup else 6)
        self.demo = IndicatorDemo(self, display=display)
        self.demo.layout().setContentsMargins(0, 0, 0, 0)
        self.demo.layout().setSpacing(2 if setup else 6)
        self.demo.caption.setMinimumHeight(0)
        self.demo.desktop.drawing_scale = .65
        self.demo.desktop.setMinimumHeight(110)
        self.demo.desktop.setFixedHeight(110)
        if setup and self.demo.reduced_motion:
            self.demo.desktop.drawing_scale = .6
            self.demo.desktop.setFixedHeight(96)
        layout.addWidget(self.demo)
        self.reason = QLabel(CAPTIONS[0], self)
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)
        self.picker = IndicatorPlacePicker(current_position(config, display), self, display=display)
        self.picker.layout().setContentsMargins(0, 0, 0, 0)
        self.picker.layout().setSpacing(2 if setup else 6)
        self.picker.hint.setMinimumHeight(0)
        self.picker.desktop.setMinimumHeight(140)
        self.picker.desktop.setFixedHeight(140)
        layout.addWidget(self.picker)
        layout.addStretch()
        self.picker.placement_chosen.connect(self._choose)

    def set_display(self, display):
        self.demo.desktop.display = display
        self.demo.setAccessibleName(f'{display.title()} listening display demonstration')
        self.demo.set_frame(0)
        self.picker.display = self.picker.desktop.display = display
        self.picker.set_value(current_position(self.config, display))
        for value, button in self.picker.targets.items():
            button.setGeometry(self.picker.desktop.target_rect(value, 112 if display == 'full' else 92, 44).toRect())
        self.picker.desktop.update()
        for button in self.picker.targets.values():
            button.update()

    def _choose(self, value):
        monitor = placement_monitor(self.config, self.app)
        if self.app is not None:
            choose_for_app(self.app, value, monitor)
        else:
            self.config.update(placement_updates(self.config, value, monitor))


def tutorial_page(owner):
    owner._display_page = DisplayPlacementPage(getattr(owner._app, 'config', {}) or {}, owner._stack, app=owner._app)
    return owner._display_page


def setup_placement_page(owner):
    owner._display_placement_page = DisplayPlacementPage(owner._config, owner._stack, setup=True)
    owner._listening_display_page.group.idToggled.connect(
        lambda index, checked: owner._display_placement_page.set_display(listening_display.VALUES[index]) if checked else None)
    return owner._display_placement_page
