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
    """Stress text at unchanged geometry; native DPI proofs use QPA/work-area fit."""
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
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QFontDatabase, QFontMetrics
    from PySide6.QtWidgets import QFrame, QScrollArea, QStyle, QStyleOptionButton, QWidget

    output = _REPO_ROOT / 'ui_proof' / '373' / ('finish5' if '--finish5' in sys.argv else 'finish4' if '--finish4' in sys.argv else 'finish3' if '--finish3' in sys.argv else '')
    output.mkdir(parents=True, exist_ok=True)
    proof_home = Path(os.environ['SAMSARA_HOME_DIR'])
    proof_home.mkdir(parents=True, exist_ok=True)
    from samsara.ui import theme, first_run_wizard_qt as wizard
    from samsara.ui import first_run_pages_qt as pages, network_voice_notice
    from samsara.onboarding.first_run_recommend import MODEL_REASONS

    app = QApplication.instance() or QApplication(sys.argv)
    print('Native font families:', len(QFontDatabase.families()))
    pictures = []
    measurements = []

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
            for scale in (float(os.environ.get('WALKTHROUGH_PROOF_SCALE', '1')),):
                suffix = f'{palette}_{round(scale * 100)}'
                win = wizard._WizardWindow(proof_home / 'missing.json')
                win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
                default_size = [win.width(), win.height()]
                if any(flag in sys.argv for flag in ('--finish3', '--finish4', '--finish5')):
                    win.resize(min(720, app.primaryScreen().availableGeometry().width()), win.height())
                else:
                    win.resize(1000, 800)
                win._mics = [{'id': 7, 'name': 'Synthetic microphone'}]
                win._populate_mic_combo()
                win._decisions.receive_facts({'cpu_cores': 8, 'ram_gb': 16, 'gpu_usable': False})
                # QPA applies native DPI to fonts, targets and window together.
                for index, (name, _) in enumerate(wizard._STEPS):
                    show_step(win, name, f'{index+1:02}_{name.lower().replace(" ", "_")}_{suffix}')
                    win._collect_step()
                if any(flag in sys.argv for flag in ('--finish3', '--finish4', '--finish5')):
                    strip = win._step_indicator
                    boxes = [strip.geometry_for(i) for i in range(strip.count())]
                    metrics = QFontMetrics(theme.qfont(round(theme.TYPE_MIN * strip.ui_scale)))
                    origin = strip.viewport().mapTo(win, QPoint(0, 0)).y()
                    divider = min(frame.mapTo(win, QPoint(0, 0)).y() for frame in win.findChildren(QFrame)
                                  if frame.frameShape() == QFrame.Shape.HLine
                                  and frame.mapTo(win, QPoint(0, 0)).y() > origin)
                    ratio = win.devicePixelRatioF()
                    radio = win._decisions.mode.buttons()[0]
                    radio_option = QStyleOptionButton()
                    radio_option.initFrom(radio)
                    indicator = radio.style().subElementRect(QStyle.SubElement.SE_RadioButtonIndicator,
                                                             radio_option, radio)
                    circle_gap = min(label.top() - circle.bottom() for circle, label in boxes)
                    divider_gap = min(divider - origin - label.bottom() for _, label in boxes)
                    assert circle_gap >= theme.SPACE_2
                    assert divider_gap >= theme.SPACE_2
                    assert all(strip.viewport().rect().contains(circle.toAlignedRect()) for circle, _ in boxes)
                    assert all(label.width() >= metrics.horizontalAdvance(strip.steps[i])
                               for i, (_, label) in enumerate(boxes) if strip.all_labels or i == win._step)
                    row_measures = []
                    if '--finish5' in sys.argv:
                        for name in ('Talking', 'Music', 'Voice', 'Listening display'):
                            win._step = [n for n, _ in wizard._STEPS].index(name)
                            win._show_step()
                            app.processEvents()
                            separators = [item for item in win._current_page.findChildren(QWidget)
                                          if item.property('walkthrough_row_separator')]
                            body = win._current_page.widget()
                            def row_items(layout):
                                result = []
                                for i in range(layout.count()):
                                    widget = layout.itemAt(i).widget()
                                    if widget is None or not widget.isVisibleTo(win):
                                        continue
                                    if (not widget.property('walkthrough_row_separator') and widget.layout()
                                            and any(child.property('walkthrough_row_separator')
                                                    for child in widget.findChildren(QWidget))):
                                        result.extend(row_items(widget.layout()))
                                    else:
                                        result.append(widget)
                                return result
                            visible_rows = row_items(body.layout())
                            gaps = []
                            for item in separators:
                                position = visible_rows.index(item)
                                before, after = visible_rows[position - 1], visible_rows[position + 1]
                                line = item.findChild(QFrame)
                                above = line.mapTo(body, QPoint()).y() - before.mapTo(body, QPoint()).y() - before.height()
                                below = after.mapTo(body, QPoint()).y() - line.mapTo(body, QPoint()).y() - line.height()
                                gaps.append(dict(above_dip=above, below_dip=below,
                                                 above_pixels=above * ratio, below_pixels=below * ratio))
                            assert not gaps or max(gap[key] for gap in gaps for key in ('above_dip', 'below_dip')) - min(
                                gap[key] for gap in gaps for key in ('above_dip', 'below_dip')) <= 1, (name, gaps)
                            row_measures.append(dict(page=name, gaps=gaps,
                                scroll_max=win._current_page.verticalScrollBar().maximum(),
                                hairlines=[dict(height_dip=item.findChild(QFrame).height(),
                                    top_margin_dip=item.layout().contentsMargins().top(),
                                    bottom_margin_dip=item.layout().contentsMargins().bottom(),
                                    width_dip=item.width(),
                                    origin_dip=[item.findChild(QFrame).mapTo(win, QPoint()).x(),
                                                item.findChild(QFrame).mapTo(win, QPoint()).y()])
                                    for item in separators]))
                    if '--finish4' in sys.argv or '--finish5' in sys.argv:
                        for name, groups in [('Talking', [win._decisions.mode, win._decisions.wake]),
                                             ('Music', [win._decisions.music_group]),
                                             ('Voice', [win._decisions.level, win._decisions.engine])]:
                            win._step = [n for n, _ in wizard._STEPS].index(name)
                            win._show_step()
                            app.processEvents()
                            for group in groups:
                                cluster = group._segments
                                row_measures.append(dict(page=name, question=cluster.accessibleName(),
                                    row_top_margin_dip=group._row.layout().contentsMargins().top(),
                                    within_group_gap_dip=group._row.layout().spacing(),
                                    segments_width_dip=cluster.width(), segments_height_dip=cluster.height(),
                                    selected_description=group._description.text()))
                        show_step(win, 'Talking', 'talking_wake_on_' + suffix)
                        win._decisions.wake.buttons()[0].click()
                        grab(win, 'talking_wake_on_' + suffix + '.png')
                        assert win._decisions.phrase.isVisibleTo(win)
                        if '--finish5' in sys.argv:
                            assert win._current_page.verticalScrollBar().maximum() == 0, suffix
                        show_step(win, 'Voice', 'voice_silent_' + suffix)
                        win._decisions.level.buttons()[0].click()
                        grab(win, 'voice_silent_' + suffix + '.png')
                        win._decisions.level.buttons()[1].click()
                        win._step = 8
                        win._show_step()
                        app.processEvents()
                    measurements.append(dict(theme=palette, scale=scale, rows=row_measures,
                        device_ratio=ratio, window_dip=[win.width(), win.height()], default_window_dip=default_size,
                        window_pixels=[win.grab().width(), win.grab().height()],
                        default_window_pixels=[round(value * ratio) for value in default_size],
                        available_dip=[app.primaryScreen().availableGeometry().width(), app.primaryScreen().availableGeometry().height()],
                        circle_label_gap_dip=circle_gap, circle_label_gap_pixels=circle_gap * ratio,
                        label_divider_gap_dip=divider_gap, label_divider_gap_pixels=divider_gap * ratio,
                        circle_diameter_pixels=boxes[0][0].width() * ratio,
                        number_font_pixels=theme.TYPE_MIN * ratio, welcome_number_pixels=theme.TYPE_BODY * ratio,
                        radio_indicator_content_pixels=16 * ratio,
                        radio_indicator_outer_pixels=indicator.width() * ratio,
                        horizontal_scroll_max=strip.horizontalScrollBar().maximum(),
                        all_labels=strip.all_labels, circles=strip.count()))
                    win.close()
                    continue
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
    (output / f'measurements_{os.environ.get("WALKTHROUGH_PROOF_SCALE", "1")}.json').write_text(json.dumps(measurements, indent=2), encoding='utf-8')
    (output / f'pictures_{os.environ.get("WALKTHROUGH_PROOF_SCALE", "1")}.json').write_text(json.dumps(pictures, indent=2), encoding='utf-8')
    return 0


def render_try_389() -> int:
    """Synthetic, silent tutorial proof; never starts the owner application."""
    import json
    import re
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch
    from PySide6.QtWidgets import QLabel, QWidget
    from samsara.config_schema import apply_feedback_level
    from samsara.ui import theme, tutorial_qt, network_voice_notice
    from samsara.tts import runtime
    from samsara.tts.engine_base import VoiceInfo
    from samsara.audio_engine.frame import FRAME_SIZE
    from samsara.audio_engine.ring import EMPTY
    import numpy as np

    output = _REPO_ROOT / 'ui_proof' / '389'
    output.mkdir(parents=True, exist_ok=True)
    gui = QApplication.instance() or QApplication(sys.argv)
    scale = float(os.environ.get('TRY_PROOF_SCALE', '1'))
    suffix = str(round(scale * 100))
    pictures, measurements = [], []

    def grab(widget, name):
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        _settle_and_grab(gui, widget, output / f'{name}_{suffix}.png', ms=100)
        pictures.append(str(output / f'{name}_{suffix}.png'))
        if name in ('try_page', 'voice_keep_to_hear', 'feedback_full_keep_to_hear', 'pace_measured'):
            from PySide6.QtCore import QPoint
            card = page.cards['voice'].grab().toImage()
            measurements.append(dict(state=name, scale=scale,
                slider_background={key: card.pixelColor(control.x() + 4, control.y() + 4).name()
                                   for key, control in (('speed', page.speed), ('volume', page.volume))},
                card_background=card.pixelColor(4, page.speed.y() + 4).name(),
                control_heights={key: [choice.height(), page.actions[key].height(), page.keeps[key].height()]
                    for key, choice in (('level', page.level), ('voice', page.voice), ('pace', page.pace))},
                label_x={key: label.mapTo(page.widget(), QPoint()).x() for key, label in page.voice_labels.items()},
                control_x={key: control.mapTo(page.widget(), QPoint()).x() for key, control in
                    (('Voice', page.voice_id), (page.voice.accessibleName(), page.voice),
                     ('Speaking speed', page.speed), ('Volume', page.volume))}))


    def enlarge(widget):
        if scale == 1:
            return
        for child in [widget, *widget.findChildren(QWidget)]:
            child.setStyleSheet(re.sub(r'(font-size\s*:\s*)(\d+(?:\.\d+)?)px',
                lambda match: f'{match[1]}{round(float(match[2])*scale)}px', child.styleSheet()))
        if hasattr(widget, '_step_indicator'):
            widget._step_indicator.set_ui_scale(scale)

    def build_preview(app, cfg):
        app.tts_engine = MagicMock()
        app.tts_engine.list_voices.return_value = [VoiceInfo('voice-a', 'Voice A', 'en', 'unknown'), VoiceInfo('voice-b', 'Voice B', 'en', 'unknown')]
        coordinator = MagicMock(engine=app.tts_engine, is_speaking=False)
        def speak(*args, **kwargs):
            handle = SimpleNamespace(utterance_id='synthetic-preview')
            coordinator._last_spoken_handle = handle
            return handle
        coordinator.speak.side_effect = speak
        app.audio_coordinator = coordinator

    cfg = {'tts': {'feedback_level': 'brief', 'enabled': True, 'engine': 'winrt', 'speed': 1},
           'wake_word_enabled': False, 'command_mode': {'enabled': False},
           'accessibility': {'speech_pace': 'standard'}}
    app = SimpleNamespace(config=cfg, _config_lock=threading.Lock(), recording=False,
                          save_config=lambda: None, play_sound=lambda *_: None,
                          audio_coordinator=None)
    build_preview(app, cfg['tts'])
    app.set_feedback_level = lambda value: apply_feedback_level(app.config, value)
    app.update_config_and_save = lambda changes: app.config.update(changes)
    with patch.object(runtime, 'rebuild_tts_runtime', build_preview), \
         patch.object(network_voice_notice, 'request_acceptance', lambda _: False):
        window = tutorial_qt.TutorialWindow(app)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window._step = next(i for i, (key, _) in enumerate(window._steps) if key == 'try')
        window._show_step()
        window.setMaximumHeight(900)
        enlarge(window)
        page = window._try_page
        grab(window, 'try_page')
        grab(page.widget(), 'try_page_content')
        page.sample('level')
        grab(page.widget(), 'feedback_brief_example')
        page.level.setCurrentIndex(page.level.findData('full'))
        page.sample('level')
        grab(window, 'feedback_full_keep_to_hear')
        page.keep('level')
        page.sample('level')
        grab(page.widget(), 'feedback_full_example')
        page.level.setCurrentIndex(page.level.findData('brief'))
        page.keep('level')
        page.voice_id.setCurrentIndex(page.voice_id.findData('voice-b'))
        page.speed.setValue(125)
        page.volume.setValue(65)
        page.sample('voice')
        grab(page.widget(), 'voice_hear_it')
        with patch.object(network_voice_notice, 'request_acceptance', lambda _: True):
            page.voice.setCurrentIndex(page.voice.findData('edge'))
        grab(page.widget(), 'voice_keep_to_hear')
        page.voice.setCurrentIndex(page.voice.findData('winrt'))
        frames = [SimpleNamespace(pcm=np.full(FRAME_SIZE, value, dtype=np.int16))
                  for value in [5000] + [0]*12 + [5000]]
        reader = SimpleNamespace(read_next=lambda: frames.pop(0) if frames else EMPTY)
        app._ace_engine = SimpleNamespace(_running=True,
            register_consumer=lambda _: reader, unregister_consumer=lambda _: None)
        page.measure()
        page.poll()
        page.finish_measure()
        grab(page.widget(), 'pace_measured')
        page.level.setCurrentIndex(page.level.findData('silent'))
        page.keep('level')
        grab(window, 'voice_hidden_silent')
        page.level.setCurrentIndex(page.level.findData('brief'))
        page.keep('level')
        app.recording = True
        page.poll()
        grab(window, 'busy_samples')
        app.recording = False
        page.poll()
        app.audio_coordinator.engine = None
        page.refresh()
        grab(page.widget(), 'speech_unavailable')
        app.audio_coordinator.engine = object()
        page.refresh()
        dialog = network_voice_notice.NetworkVoiceNotice(window)
        enlarge(dialog)
        # Recompute the notice's fixed label heights after synthetic text scaling.
        margins = dialog.layout().contentsMargins()
        content_width = dialog.width() - margins.left() - margins.right()
        for label in dialog.findChildren(QLabel):
            label.setFixedHeight(label.heightForWidth(content_width) if label.wordWrap() else label.sizeHint().height())
        dialog.adjustSize()
        grab(dialog, 'network_notice')
        notice_clipped = [label.text() for label in dialog.findChildren(QLabel)
                          if label.height() < (label.heightForWidth(label.width()) if label.wordWrap() else label.sizeHint().height())]
        dialog.close()
        page.keep('voice')
        page.keep('pace')
        window._go_next()
        grab(window, 'done_kept')
        window._go_back()
        for _ in range(10):
            gui.processEvents()
        clipped = []
        for label in page.widget().findChildren(QLabel):
            if not label.isVisible() or not label.text():
                continue
            required = label.heightForWidth(label.width()) if label.hasHeightForWidth() else label.fontMetrics().height()
            if label.height() < required:
                clipped.append(dict(text=label.text(), height=label.height(), required=required))
        measurements.append(dict(scale=scale, window=[window.width(), window.height()],
            horizontal_scroll=page.horizontalScrollBar().maximum(),
            vertical_scroll=page.verticalScrollBar().maximum(), clipped_labels=clipped,
            notice_clipped_labels=notice_clipped,
            sample_audio='stubbed; no real output', microphone='synthetic ring; no input stream'))
        # The same producer reaches both supported listening displays.
        from samsara.ui.listening_indicator import ListeningIndicator
        from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
        app._schedule_ui = lambda callback, *args: callback(*args)
        compact = ListeningIndicator()
        compact.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        app.listening_indicator = compact
        page.level.setCurrentIndex(page.level.findData('silent'))
        page.sample('level')
        grab(compact, 'Compact_example')
        compact.hide()
        compact.deleteLater()
        with patch('samsara.live_surface.controller.qt_runtime.post', lambda *_: None):
            controller = LiveSurfaceController(app)
            controller.start()
            controller.widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            app.listening_indicator = LiveSurfaceIndicatorAdapter(controller)
            page.sample('level')
            controller.drain()
            grab(controller.widget, 'Full_example')
            controller.close()
        window.close()
        window.deleteLater()
        gui.processEvents()
    (output / f'pictures_{suffix}.json').write_text(json.dumps(pictures, indent=2), encoding='utf-8')
    (output / f'measurements_{suffix}.json').write_text(json.dumps(measurements, indent=2), encoding='utf-8')
    return int(any(row.get('clipped_labels') or row.get('notice_clipped_labels') or row.get('horizontal_scroll') for row in measurements))


if __name__ == "__main__":
    if "--try-389" in sys.argv:
        sys.exit(render_try_389())
    if "--walkthrough-373" in sys.argv:
        sys.exit(render_walkthrough_373())
    sys.exit(render_step_candidates() if "--step-candidates" in sys.argv else main())
