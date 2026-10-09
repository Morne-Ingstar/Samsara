"""Visual-proof screenshot tool for the tutorial / first-run wizard / mic
setup wizard design-system pass.

Constructs each window directly (bypassing qt_runtime's thread-marshaling,
since this runs standalone outside the app), shows it, waits ~500ms for
layout/paint to settle, and saves a PNG.

Usage:
    F:\\envs\\sami\\python.exe tools\\wizard_screenshots.py

Output: C:\\Users\\Morne\\Documents\\Claude\\ui_proof\\*.png
"""
from __future__ import annotations

import sys
import os
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtCore import Qt

OUT_DIR = Path(r"C:\Users\Morne\Documents\Claude\ui_proof")


class _FakeApp:
    """Minimal stand-in for DictationApp -- just enough attribute surface
    for the three windows to construct and display their first page(s)
    without booting the real app."""
    config: dict = {}

    def get_available_microphones(self) -> list:
        # mic_setup_wizard_qt calls this on its app handle; the harness
        # renders windows only, so an empty device list is correct here.
        return []


def _settle_and_grab(app: QApplication, widget, out_path: Path, ms: int = 500) -> None:
    import time

    widget.show()
    widget.raise_()
    widget.activateWindow()
    # Pump the event loop for ~ms milliseconds so layout/paint settles.
    end = time.monotonic() + (ms / 1000.0)
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    pixmap = widget.grab()
    ok = pixmap.save(str(out_path))
    print(f"{'saved' if ok else 'FAILED to save'}: {out_path}")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)

    # ---- Splash screen: brand-red segmented spinner (replaced the old
    # QProgressBar) -- settle for 500ms so the spinner is mid-rotation with
    # its opacity falloff visible, not caught on its very first frame. -----
    try:
        from samsara.ui.splash_qt import _SplashWidget

        splash = _SplashWidget()
        _settle_and_grab(app, splash, OUT_DIR / "splash_spinner.png")
        splash.close()
    except Exception:
        import traceback
        print("FAILED: splash window")
        traceback.print_exc()

    # ---- Tutorial window: step 1 (welcome -- Back disabled, no prior
    # step), step 2 (dictation -- mid-tutorial, Back now enabled next to
    # Next), step 3 (command -- also captured to show Back survives a
    # second forward step), step 4 (done -- the checklist/guide-cards page
    # whose fixed button geometry used to clip against the theme's font
    # metrics) -----------------------------------------------------------
    try:
        from samsara.ui.tutorial_qt import TutorialWindow

        fake_app = _FakeApp()
        tut = TutorialWindow(fake_app)
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step1.png")
        tut._go_next()  # welcome -> dictation
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step2.png")
        tut._go_next()  # dictation -> command
        # Mid-tutorial step demonstrating the new Back button: enabled,
        # placed next to Next (Back then Next), same secondary styling as
        # "Skip this step" -- see samsara/ui/tutorial_qt.py's _go_back.
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step3_back_button.png")
        tut._go_next()  # command -> ways (passive)
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step4_ways.png")
        tut._go_next()  # ways -> done
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step5_done.png")
        tut.close()
    except Exception:
        import traceback
        print("FAILED: tutorial window")
        traceback.print_exc()

    # ---- First-run wizard: page 1 (welcome), page 2 (use case -- radio
    # cards whose title/desc labels used to show a nested rounded-rect
    # border), page 3 (model -- same card pattern), page 6 (complete --
    # the use-case tip frame had the same bug) --------------------------
    try:
        from samsara.ui.first_run_wizard_qt import _WizardWindow as _FRWindow

        frw = _FRWindow(Path(r"C:\Temp\samsara_ui_proof_fake_config.json"))
        _settle_and_grab(app, frw, OUT_DIR / "first_run_wizard_page1.png")
        frw._step = 1  # welcome -> use case
        frw._show_step()
        _settle_and_grab(app, frw, OUT_DIR / "first_run_wizard_page2_usecase.png")
        frw._step = 3  # -> model
        frw._show_step()
        _settle_and_grab(app, frw, OUT_DIR / "first_run_wizard_page4_model.png")
        frw._step = 5  # -> complete
        frw._show_step()
        _settle_and_grab(app, frw, OUT_DIR / "first_run_wizard_page6_complete.png")
        frw.close()
    except Exception:
        import traceback
        print("FAILED: first-run wizard window")
        traceback.print_exc()

    # ---- Mic setup wizard: page 1 (device selection), page 2 (level --
    # "Calibrate ->" used to clip) and page 3 (wake word -- "Continue ->"
    # used to clip) -----------------------------------------------------
    try:
        from samsara.ui.mic_setup_wizard_qt import _WizardWindow as _MicWindow

        fake_app2 = _FakeApp()
        micw = _MicWindow(fake_app2)
        _settle_and_grab(app, micw, OUT_DIR / "mic_wizard_page1.png")
        micw._go_to(1)  # device -> level
        _settle_and_grab(app, micw, OUT_DIR / "mic_wizard_page2_level.png")
        micw._go_to(2)  # level -> wake word
        _settle_and_grab(app, micw, OUT_DIR / "mic_wizard_page3_wake.png")
        micw.close()
    except Exception:
        import traceback
        print("FAILED: mic setup wizard window")
        traceback.print_exc()

    return 0


def render_step_candidates() -> int:
    from samsara.ui import theme
    from samsara.ui.widgets.step_indicator import StepIndicator, _upcoming_ink

    out_dir = OUT_DIR / "325"
    out_dir.mkdir(parents=True, exist_ok=True)
    previous_home = os.environ.get("SAMSARA_HOME_DIR")
    captures = {}
    with tempfile.TemporaryDirectory(prefix="samsara-325-") as temporary_home:
        os.environ["SAMSARA_HOME_DIR"] = temporary_home
        app = QApplication.instance() or QApplication(sys.argv)
        for palette in ("dark", "light"):
            theme.set_theme(palette, refresh=False)
            upcoming = _upcoming_ink().name()
            ratios = {
                "done/BG1": theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG1),
                "current/BG1": theme.contrast_ratio(theme.ACCENT, theme.BG1),
                "upcoming/BG1": theme.contrast_ratio(upcoming, theme.BG1),
                "done/BG2": theme.contrast_ratio(theme.TEXT_PRIMARY, theme.BG2),
                "upcoming/BG2": theme.contrast_ratio(upcoming, theme.BG2),
                "pill-ink/accent": theme.contrast_ratio(theme.TEXT_ON_ACCENT, theme.ACCENT),
            }
            print(f"{palette} contrast: " + ", ".join(f"{name}={ratio:.2f}:1" for name, ratio in ratios.items()))
            print(f"{palette} upcoming AA-corrected TEXT_DISABLED={upcoming}")
            for scale in (1.0, 1.5):
                for variant in ("A", "B", "C"):
                    widget = StepIndicator(["Device", "Level", "Wake word", "Done"], variant=variant)
                    widget.set_ui_scale(scale)
                    widget.set_current(2)
                    widget.set_done(0)
                    widget.set_done(1)
                    widget.resize(round(theme.HIT_TARGET_MIN * 16 * scale), widget.height())
                    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
                    widget.show()
                    app.processEvents()
                    capture = widget.grab().toImage()
                    filename = f"{variant}_{palette}_{str(scale).replace('.', '_')}x.png"
                    path = out_dir / filename
                    if not capture.save(str(path)):
                        raise RuntimeError(f"Could not save {path}")
                    captures[(palette, scale, variant)] = capture
                    print(f"saved: {path}")
                    widget.close()
        tile_width = max(image.width() for image in captures.values())
        tile_height = max(image.height() for image in captures.values()) + theme.TYPE_TITLE * 2
        sheet = QImage(tile_width * 3, tile_height * 4, QImage.Format.Format_ARGB32_Premultiplied)
        sheet.fill(QColor(theme.BG0))
        painter = QPainter(sheet)
        painter.setPen(QColor(theme.TEXT_PRIMARY))
        painter.setFont(theme.qfont(theme.TYPE_BODY))
        for row, (palette, scale) in enumerate((
            ("dark", 1.0), ("dark", 1.5), ("light", 1.0), ("light", 1.5)
        )):
            for column, variant in enumerate(("A", "B", "C")):
                image = captures[(palette, scale, variant)]
                left = column * tile_width
                top = row * tile_height
                painter.drawText(left + theme.TYPE_MIN, top + theme.TYPE_TITLE, f"{variant} · {palette} · {scale:g}x")
                painter.drawImage(left, top + theme.TYPE_TITLE * 2, image)
        painter.end()
        sheet_path = out_dir / "candidates.png"
        if not sheet.save(str(sheet_path)):
            raise RuntimeError(f"Could not save {sheet_path}")
        print(f"saved: {sheet_path}")
    if previous_home is None:
        os.environ.pop("SAMSARA_HOME_DIR", None)
    else:
        os.environ["SAMSARA_HOME_DIR"] = previous_home
    return 0



def scale_walkthrough_text(window, scale):
    """Enlarge real fonts for proof/layout checks, retaining the window size."""
    import re
    from PySide6.QtGui import QFontInfo
    from PySide6.QtWidgets import QWidget, QPushButton
    for widget in [window, *window.findChildren(QWidget)]:
        if widget.styleSheet():
            widget.setStyleSheet(re.sub(r'(font-size\s*:\s*)([\d.]+)(px)',
                lambda m: f'{m[1]}{round(float(m[2]) * scale)}{m[3]}', widget.styleSheet()))
        font = widget.font()
        font.setPixelSize(max(1, round(QFontInfo(font).pixelSize() * scale)))
        # QSS font sizes take precedence; this also covers controls without QSS.
        widget.setFont(font)
        if isinstance(widget, QPushButton) and widget.minimumWidth() == widget.maximumWidth() and widget.text() != '?':
            widget.setFixedWidth(max(widget.width(), widget.fontMetrics().horizontalAdvance(widget.text()) + 48))
    if hasattr(window, '_step_indicator'):
        window._step_indicator.set_ui_scale(scale)
    window.setProperty('proof_text_scale', scale)


def render_walkthrough_373():
    """Native-font hidden proofs; all audio, engine and network seams stubbed."""
    if sys.platform == 'win32':
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x8003)
    import json
    from unittest.mock import patch
    from PySide6.QtGui import QFontDatabase
    from PySide6.QtWidgets import QScrollArea

    output = _REPO_ROOT / 'ui_proof' / '373'
    output.mkdir(parents=True, exist_ok=True)
    proof_home = Path(os.environ['SAMSARA_HOME_DIR'])
    proof_home.mkdir(parents=True, exist_ok=True)
    from samsara.ui import theme, first_run_wizard_qt as wizard
    from samsara.ui import first_run_pages_qt as pages, network_voice_notice
    from samsara.onboarding.first_run_recommend import MODEL_REASONS

    app = QApplication.instance() or QApplication(sys.argv)
    print('Native font families:', len(QFontDatabase.families()))
    pictures = []

    def grab(widget, filename):
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        widget.show()
        for _ in range(4):
            app.processEvents()
        path = output / filename
        assert widget.grab().save(str(path)), path
        pictures.append(str(path))
        print('saved:', path)

    def show_step(win, name, stem):
        win._step = [n for n, _ in wizard._STEPS].index(name)
        win._show_step()
        grab(win, stem + '.png')
        page = win._current_page
        if isinstance(page, QScrollArea) and page.verticalScrollBar().maximum():
            bar = page.verticalScrollBar()
            bar.setValue(bar.maximum())
            grab(win, stem + '_bottom.png')
            bar.setValue(0)
            # A separate full-content picture keeps the scrolling screen honest.
            assert page.widget().grab().save(str(output / (stem + '_content.png')))
            pictures.append(str(output / (stem + '_content.png')))

    def meter(win):
        win._mic_capture_ready = True
        win._next_btn.setEnabled(True)

    with patch.object(wizard.thread_registry, 'spawn', lambda *a, **k: None), \
         patch.object(wizard._WizardWindow, '_start_meter', meter), \
         patch.object(pages, 'cuda_chosen', lambda _: False), \
         patch.object(pages.autostart, 'sync_run_entry', lambda *_: None), \
         patch.object(network_voice_notice, 'request_acceptance', lambda _: False):
        for palette in ('dark', 'light'):
            theme.set_theme(palette, refresh=False)
            for scale in (1.0, 1.5):
                suffix = f'{palette}_{round(scale * 100)}'
                win = wizard._WizardWindow(proof_home / 'missing.json')
                win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                win.resize(1000, 800)
                win._mics = [{'id': 7, 'name': 'Synthetic microphone'}]
                win._populate_mic_combo()
                win._decisions.receive_facts({'cpu_cores': 8, 'ram_gb': 16, 'gpu_usable': False})
                scale_walkthrough_text(win, scale)
                for index, (name, _) in enumerate(wizard._STEPS):
                    show_step(win, name, f'{index+1:02}_{name.lower().replace(" ", "_")}_{suffix}')
                    win._collect_step()
                for reason in MODEL_REASONS:
                    show_step(win, 'Recognition', f'recognition_{reason}_{suffix}')
                    d = win._decisions
                    d.reason.setText(MODEL_REASONS[reason])
                    size = 'tiny' if reason in ('low_cores', 'low_ram') else 'small' if reason == 'gpu_cuda' else 'base'
                    for button in win._model_group.buttons():
                        button.setChecked(button.property('_value') == size)
                    grab(win, f'recognition_{reason}_{suffix}.png')
                show_step(win, 'Talking', f'talking_wake_off_{suffix}')
                win._decisions.wake.buttons()[1].click()
                grab(win, f'talking_wake_off_{suffix}.png')
                show_step(win, 'Voice', f'voice_brief_local_{suffix}')
                win._decisions.level.buttons()[0].click()
                grab(win, f'voice_silent_{suffix}.png')
                win._decisions.level.buttons()[1].click()
                grab(win, f'voice_brief_local_{suffix}.png')
                win._decisions.engine.buttons()[1].setChecked(True)
                grab(win, f'voice_network_{suffix}.png')
                dialog = network_voice_notice.NetworkVoiceNotice(win)
                dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                scale_walkthrough_text(dialog, scale)
                dialog.resize(900, 480)
                grab(dialog, f'voice_notice_{suffix}.png')
                dialog.close()
                show_step(win, 'Talking', f'talking_hold_detail_{suffix}')
                button, _ = win._decisions.details[0]
                button.click()
                grab(win, f'talking_hold_detail_{suffix}.png')
                win.close()
    (output / 'pictures.json').write_text(json.dumps(pictures, indent=2), encoding='utf-8')
    return 0


if __name__ == "__main__":
    if "--walkthrough-373" in sys.argv:
        sys.exit(render_walkthrough_373())
    sys.exit(render_step_candidates() if "--step-candidates" in sys.argv else main())
