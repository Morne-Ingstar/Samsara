"""Native-DPI, isolated regressions for the five repaired windows."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('surface', ('mic', 'wake', 'wake_narrow', 'workflow', 'update', 'profiles'))
@pytest.mark.parametrize('palette,scale', [('dark', 100), ('dark', 150), ('light', 100), ('light', 150)])
def test_window_text_fits(surface, palette, scale, tmp_path):
    env = os.environ.copy()
    for key in ('HOME', 'USERPROFILE', 'SAMSARA_HOME_DIR', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP', 'TMPDIR'):
        folder = tmp_path / key
        folder.mkdir()
        env[key] = str(folder)
    env.update(QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=str(scale / 100),
               PYTHONDONTWRITEBYTECODE='1')
    # The caller supplies the system font folder for these layout measurements.
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), surface, palette],
                            env=env, cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def _measure(surface, palette):
    from types import SimpleNamespace
    from unittest.mock import patch
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QLineEdit, QScrollArea
    from samsara.runtime import thread_registry
    from samsara.ui import theme
    thread_registry.spawn = lambda *a, **k: None
    theme.set_theme(palette, refresh=False)
    if surface == 'mic':
        from samsara.ui.mic_setup_wizard_qt import _WizardWindow
        from tests.test_mic_setup_wizard_qt import _WizardApp
        with patch.object(_WizardWindow, '_ensure_audio_running', lambda self: None):
            window = _WizardWindow(_WizardApp())
        for step in range(4):
            window._go_to(step)
            _settle(window)
            _check_text(window)
        return
    if surface in ('wake', 'wake_narrow'):
        from samsara.ui.settings_qt import _SettingsWindow
        from samsara.ui.first_run_qt import ComponentsPage
        from samsara import autostart
        from tests.test_settings_pages import _StubApp, _schema_defaults
        with patch.object(ComponentsPage, 'refresh_async', lambda self: None), \
             patch.object(autostart, 'sync_run_entry', lambda *a, **k: None):
            window = _SettingsWindow(_StubApp(_schema_defaults()))
        window.resize(780 if surface == 'wake_narrow' else 1160, 920)
        page = window._pages['Dictation']
        window._stack.setCurrentWidget(page)
        page.advanced.reveal()
        _settle(window)
        root = window._rows_by_widget['wake_profiles']
        assert page.widget().width() <= page.viewport().width()
        assert page.horizontalScrollBar().maximum() == 0
        _check_text(root)
        assert sum(label.text() == 'Wake profiles' and label.isVisibleTo(page)
                   for label in page.findChildren(QLabel)) == 1
        for label in root.findChildren(QLabel):
            if label.isVisibleTo(root) and label.text() in ('Profile', 'Phrase', 'Target app (.exe)', 'Send word', 'On / Off'):
                assert label.width() >= label.fontMetrics().horizontalAdvance(label.text()), label.text()
        for control in window._widgets['wake_profile_controls']:
            for key in ('phrase', 'target_process', 'send_word'):
                field = control[key]
                assert field.width() >= field.fontMetrics().horizontalAdvance(field.text()) + 24, key
                assert field.height() == 44
                assert field.parentWidget().rect().contains(field.geometry())
        # Exercise reflow in both directions, including a theme refresh.
        window.resize(1160 if surface == 'wake_narrow' else 780, 920)
        window.apply_theme()
        _settle(window)
        assert page.widget().width() <= page.viewport().width()
        _check_text(root)
        return
    if surface == 'workflow':
        from samsara.ui.workflow_capture_qt import _ReviewWindow
        window = _ReviewWindow()
    elif surface == 'update':
        from samsara.ui.update_qt import _UpdateDialog
        window = _UpdateDialog(SimpleNamespace(config={}), initial_release=SimpleNamespace(version='0.24.0', asset_size=150000000))
    else:
        from samsara.ui.profile_manager_qt import _ProfileManagerWindow
        from samsara.profiles import ProfileManager
        window = _ProfileManagerWindow(ProfileManager(os.environ['SAMSARA_HOME_DIR']))
    _settle(window)
    _check_text(window)
    if surface in ('workflow', 'update'):
        assert window.findChildren(QLabel)[0].font().family() == 'Segoe UI'
        assert window.grab().toImage().pixelColor(0, 0).name() == theme.qcolor(theme.BG0).name()
        for button in window.findChildren(QPushButton):
            assert button.property('class') in ('secondary', 'primary'), button.text()
    if surface == 'profiles':
        scroll = window.findChild(QScrollArea)
        assert scroll.horizontalScrollBar().maximum() == 0


def _settle(window):
    from PySide6.QtWidgets import QApplication
    window.show()
    for _ in range(20):
        QApplication.processEvents()


def _check_text(root):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTextDocument
    from PySide6.QtWidgets import QLabel, QPushButton
    widgets = [w for w in root.findChildren(QLabel) + root.findChildren(QPushButton)
               if w.isVisibleTo(root) and w.text()]
    for widget in widgets:
        text = widget.text()
        assert widget.parentWidget().rect().contains(widget.geometry()), text
        if isinstance(widget, QPushButton):
            assert widget.width() >= widget.sizeHint().width(), text
        else:
            margins = widget.contentsMargins()
            width = widget.width() - margins.left() - margins.right()
            height = widget.height() - margins.top() - margins.bottom()
            if widget.wordWrap():
                doc = QTextDocument()
                doc.setDocumentMargin(0)
                doc.setDefaultFont(widget.font())
                doc.setHtml(text) if '<b>' in text else doc.setPlainText(text)
                doc.setTextWidth(width)
                assert height + 1 >= doc.size().height(), (text, height, doc.size().height())
            else:
                assert width + 1 >= widget.fontMetrics().horizontalAdvance(text), text
                assert height + 1 >= widget.fontMetrics().height(), text
        for other in widgets:
            if other is not widget and other.parentWidget() is widget.parentWidget():
                assert not widget.geometry().intersects(other.geometry()), (text, other.text())


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import threading
    from samsara.ui import qt_runtime
    errors = []
    def run():
        try:
            _measure(*sys.argv[1:])
        except BaseException:
            import traceback
            errors.append(traceback.format_exc())
        finally:
            qt_runtime._app.quit()
    def enqueue():
        qt_runtime._ready.wait(10)
        qt_runtime.post(run)
    sender = threading.Thread(target=enqueue)
    sender.start()
    qt_runtime._run_loop()
    sender.join()
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
