"""997c: real themed hub layouts, with disposable data and no app bootstrap."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QAbstractButton, QScrollArea, QWidget

from samsara.ui import dictionary_panel_qt as dictionary, main_window_qt as hub, theme


def build_window(monkeypatch, home):
    """Also used by the isolated full-window proof renderer."""
    monkeypatch.setenv('SAMSARA_HOME_DIR', str(home))
    class Settings:
        def __init__(self, *args):
            self.values = {}
        def value(self, key, default=None, type=None):
            return self.values.get(key, default)
        def setValue(self, key, value):
            self.values[key] = value

    # No registry preferences or owner's dictionary are consulted.
    from samsara.ui import home_qt
    monkeypatch.setattr(dictionary, 'QSettings', Settings)
    monkeypatch.setattr(home_qt, 'QSettings', Settings)
    store = SimpleNamespace(custom_vocab=['Example term ' + str(i) for i in range(40)],
                            corrections_dict={'exampl': 'example'},
                            get_builtin_words=lambda: [])
    monkeypatch.setattr(dictionary, 'get_dictionary_store', lambda app: store)
    monkeypatch.setattr(hub._MainWindow, '_geometry_screens',
                        lambda self: ([(0, 0, 2560, 1440)], (0, 0, 2560, 1440)))
    monkeypatch.setattr(hub._MainWindow, '_frame_margins', lambda self: (0, 0, 0, 0))
    app = SimpleNamespace(config={'hotkey': 'ctrl+shift+space', 'command_hotkey': 'ctrl+space'},
                          available_mics=[], history=[], saved=[])
    app.update_config = lambda changes: app.saved.append(dict(changes))
    app.update_config_and_save = lambda changes: True
    app.open_settings = lambda: None
    theme.set_theme('dark', refresh=False)
    window = hub._MainWindow(app)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    window.apply_theme()
    return window


@pytest.fixture
def window(qapp, monkeypatch, tmp_path, qt_widget_scope):
    widget = build_window(monkeypatch, tmp_path)
    with qt_widget_scope(widget):
        widget.show()
        yield widget


def settle(qapp):
    for _ in range(12):
        qapp.processEvents()


def assert_layouts_do_not_overlap(root):
    """Compare siblings assigned by each layout, never ancestor/child rects."""
    def check(layout):
        rects = []
        for i in range(layout.count()):
            item = layout.itemAt(i)
            child, nested = item.widget(), item.layout()
            if nested:
                check(nested)
            if child and not child.isVisibleTo(root):
                continue
            if item.spacerItem():
                continue
            rect = item.geometry()
            if rect.width() > 0 and rect.height() > 0:
                for prior in rects:
                    intersection = rect.intersected(prior)
                    assert intersection.isEmpty(), (type(root).__name__, rect, prior)
                rects.append(rect)
    for widget in [root, *root.findChildren(QWidget)]:
        if widget.isVisibleTo(root) and widget.layout():
            # Stacked layouts intentionally occupy the same rectangle.
            if widget.layout().inherits('QStackedLayout'):
                continue
            check(widget.layout())


def assert_buttons_reachable(root, qapp):
    for button in root.findChildren(QAbstractButton):
        if not button.isVisibleTo(root) or button.width() <= 0:
            continue
        ancestor = button.parentWidget()
        while ancestor is not None and ancestor is not root:
            if isinstance(ancestor, QScrollArea):
                ancestor.ensureWidgetVisible(button, 0, 0)
                settle(qapp)
            ancestor = ancestor.parentWidget()
        ancestor = button.parentWidget()
        while ancestor is not None:
            rect = QRect(button.mapTo(ancestor, QPoint()), button.size())
            assert ancestor.rect().contains(rect), (button.text(), type(ancestor).__name__, rect, ancestor.rect())
            if ancestor is root:
                break
            ancestor = ancestor.parentWidget()


@pytest.mark.parametrize('size', [(700, 500), (799, 537), (1920, 1080)])
@pytest.mark.parametrize('page', ['Home', 'Commands', 'History', 'Memos', 'Snippets', 'Dictionary', 'Voice help'])
def test_every_page_fits_or_scrolls(window, qapp, size, page):
    panel = window._activate(page)
    window.resize(*size)
    settle(qapp)
    assert (window.width(), window.height()) == size
    assert_layouts_do_not_overlap(panel)
    assert_buttons_reachable(panel, qapp)
    assert_buttons_reachable(window.findChild(QWidget, 'hubSidebar'), qapp)


@pytest.mark.parametrize('tab', [0, 1, 2])
@pytest.mark.parametrize('size', [(700, 500), (799, 537), (1920, 1080)])
def test_dictionary_tabs_and_disclosures(window, qapp, tab, size):
    panel = window._activate('Dictionary')
    window.resize(*size)
    panel._tabs.setCurrentIndex(tab)
    if tab == 0:
        panel._voice_expander.setChecked(True)
        panel._builtin_expander.setChecked(True)
    settle(qapp)
    assert_layouts_do_not_overlap(panel)
    assert_buttons_reachable(panel, qapp)


def test_snippet_edit_and_delete_confirmation_remain_reachable(window, qapp, tmp_path):
    from samsara import snippets
    snippets.add_snippet('sample closing', 'A sample paragraph.', home=tmp_path)
    panel = window._activate('Snippets')
    window.resize(700, 500)
    panel._list.setCurrentRow(0)
    panel.request_delete()
    settle(qapp)
    assert_layouts_do_not_overlap(panel)
    assert_buttons_reachable(panel, qapp)
    panel.cancel_delete()
    panel._name.setText('sample closing')
    panel._text.setPlainText('Updated paragraph.')
    assert panel.save_edit()
    assert snippets.load_snippets(tmp_path)[0]['text'] == 'Updated paragraph.'


@pytest.mark.parametrize('work,expected', [
    ((0, 0, 1366, 768), (956, 538)),
    ((0, 0, 1920, 1080), (1344, 756)),
    ((0, 0, 2560, 1440), (1600, 1000)),
])
def test_reset_and_unsaved_startup_use_work_area(window, monkeypatch, work, expected):
    monkeypatch.setattr(window, '_geometry_screens', lambda: ([work], work))
    window._app.config = {}
    window._restore_geometry()
    startup = window.geometry().getRect()
    assert startup[2:] == expected
    assert startup[:2] == ((work[2] - expected[0]) // 2, (work[3] - expected[1]) // 2)
    window.setGeometry(20, 20, 799, 537)
    window._reset_btn.click()
    assert window.geometry().getRect() == startup


def test_visible_grip_has_large_hit_area(window, qapp):
    window.resize(799, 537)
    settle(qapp)
    grip = window._resize_grip
    assert grip.isVisibleTo(window)
    assert grip.width() >= 24 and grip.height() >= 24
    assert grip.cursor().shape() == Qt.CursorShape.SizeFDiagCursor
    assert grip.mapTo(window, grip.rect().bottomRight()).x() >= window.width() - 20


@pytest.mark.parametrize('screen', [(0, 0, 600, 420), (-1600, -200, 1366, 728),
                                 (1920, 0, 2560, 1400), (0, 0, 7680, 4320)])
def test_responsive_default_includes_native_frame_and_taskbar(screen):
    from samsara.ui.window_geometry import contains, default_geometry, outer_rect
    margins = (8, 32, 8, 8)
    rect = default_geometry(screen, margins=margins)
    assert contains(screen, outer_rect(rect, margins))
    assert rect[2] <= 1600 and rect[3] <= 1000


def test_responsive_reset_selects_current_monitor():
    from samsara.ui.window_geometry import default_geometry, reset_geometry
    primary = (0, 0, 1920, 1040)
    secondary = (1920, -100, 1366, 728)
    assert reset_geometry((1950, 0, 900, 650), [primary, secondary], primary) == default_geometry(secondary)
