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
        tut._go_next()  # command -> done
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step4_done.png")
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


if __name__ == "__main__":
    sys.exit(render_step_candidates() if "--step-candidates" in sys.argv else main())
