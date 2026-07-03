"""Visual-proof screenshot tool for the tutorial / first-run wizard / mic
setup wizard design-system pass.

Constructs each window directly (bypassing qt_runtime's thread-marshaling,
since this runs standalone outside the app), shows it, waits ~500ms for
layout/paint to settle, and saves a PNG.

Usage:
    F:\\envs\\sami\\python.exe tools\\wizard_screenshots.py

Output: C:\\Temp\\samsara_ui_proof\\*.png
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from PySide6.QtWidgets import QApplication

OUT_DIR = Path(r"C:\Temp\samsara_ui_proof")


class _FakeApp:
    """Minimal stand-in for DictationApp -- just enough attribute surface
    for the three windows to construct and display their first page(s)
    without booting the real app."""
    config: dict = {}


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

    # ---- Tutorial window: step 1 (welcome), step 2 (dictation), step 4
    # (done -- the checklist/guide-cards page whose fixed button geometry
    # used to clip against the theme's font metrics) -------------------------
    try:
        from samsara.ui.tutorial_qt import TutorialWindow

        fake_app = _FakeApp()
        tut = TutorialWindow(fake_app)
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step1.png")
        tut._go_next()  # welcome -> dictation
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step2.png")
        tut._go_next()  # dictation -> command
        tut._go_next()  # command -> done
        _settle_and_grab(app, tut, OUT_DIR / "tutorial_step4_done.png")
        tut.close()
    except Exception:
        import traceback
        print("FAILED: tutorial window")
        traceback.print_exc()

    # ---- First-run wizard: page 1 (welcome) --------------------------------
    try:
        from samsara.ui.first_run_wizard_qt import _WizardWindow as _FRWindow

        frw = _FRWindow(Path(r"C:\Temp\samsara_ui_proof_fake_config.json"))
        _settle_and_grab(app, frw, OUT_DIR / "first_run_wizard_page1.png")
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


if __name__ == "__main__":
    sys.exit(main())
