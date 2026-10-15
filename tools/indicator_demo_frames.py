"""Offscreen evidence only: PNGs, contact sheets and two 12-second GIFs.

Run with the app Python: tools/indicator_demo_frames.py --output <report pics>.
Never reads a live profile; owns no QApplication or product process.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--partb", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_QPA_FONTDIR"] = "C:/Windows/Fonts"
    os.environ["SAMSARA_HOME_DIR"] = str(output.parent / "isolated_home")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QPoint, QCoreApplication, QEvent
    from PySide6.QtGui import QImage, QPainter, QFontInfo
    from samsara.ui import qt_runtime, theme
    from samsara.ui.widgets.indicator_demo import IndicatorDemo, POSITIONS
    from samsara.ui.widgets.indicator_place import IndicatorPlacePicker
    done, errors, manifest = threading.Event(), [], []

    def snapshot(widget, name, scale):
        image = QImage(round(widget.width()*scale), round(widget.height()*scale), QImage.Format.Format_ARGB32)
        image.fill(theme.qcolor(theme.BG0))
        painter = QPainter(image)
        painter.scale(scale, scale)
        widget.render(painter, QPoint())
        painter.end()
        path = output / (name + ".png")
        if not image.save(str(path)):
            raise RuntimeError(path)
        manifest.append(path.name)
        return path

    def render():
        try:
            if args.partb:
                render_partb(snapshot, output)
                return
            measurements = []
            variants = (("full", "dark", 1.0), ("compact", "dark", 1.0),
                        ("full", "light", 1.0), ("full", "dark", 1.5))
            for display, palette, scale in variants:
                theme.set_theme(palette, refresh=False)
                primary = (display, palette, scale) == variants[0]
                for reduced in (False, True):
                    widget = IndicatorDemo(reduced_motion=reduced, display=display)
                    widget.resize(640, 390 if reduced else 340)
                    widget.show()
                    if widget.timer:
                        widget.timer.stop()
                    for beat in (range(5) if primary else (1,)):
                        widget.set_frame(beat, 1.0)
                        widget.layout().activate()
                        assert widget.desktop.work_rect().contains(widget.desktop.indicator_rect())
                        required = widget.caption.heightForWidth(widget.caption.width())
                        assert required <= max(44, widget.caption.fontMetrics().lineSpacing()*2)
                        measurements.append(dict(display=display, palette=palette, scale=scale,
                            beat=beat+1, reduced=reduced, font=QFontInfo(widget.font()).family(),
                            caption_required=required, caption_available=widget.caption.height(),
                            contained=True))
                        snapshot(widget, f"{display.title()}_{palette}_{int(scale*100)}_{'still' if reduced else 'beat'}_{beat+1}", scale)
                    widget.close()
                    assert widget.timer is None or not widget.timer.isActive()
                    widget.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                picker = IndicatorPlacePicker(display=display)
                picker.resize(640, 340)
                picker.show()
                for position in (POSITIONS if primary else ("bottom-left",)):
                    picker.set_value(position)
                    snapshot(picker, f"Picker_{display.title()}_{palette}_{int(scale*100)}_{position}", scale)
                picker.close()
                picker.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            for palette in ("dark", "light"):
                theme.set_theme(palette, refresh=False)
                widget = IndicatorDemo(reduced_motion=False, display="full")
                widget.resize(640, 340)
                widget.show()
                widget.timer.stop()
                frames = []
                for milliseconds in range(0, 12000, 500):
                    widget.seek(milliseconds)
                    path = snapshot(widget, f"gif_{palette}_{milliseconds:05}", 1.0)
                    with Image.open(path) as frame:
                        frames.append(frame.convert("RGB"))
                frames[0].save(output / f"indicator_demo_{palette}.gif", save_all=True,
                              append_images=frames[1:], duration=500, loop=0)
                widget.close()
                widget.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            (output / "measurements.json").write_text(json.dumps(measurements, indent=2), encoding="utf-8")
            # Every PNG gets a labelled, original-size tile for visual inspection.
            for start in range(0, len(manifest), 12):
                batch = manifest[start:start+12]
                sheet = Image.new("RGB", (1920, 4*425), "#777777")
                draw = ImageDraw.Draw(sheet)
                for index, name in enumerate(batch):
                    with Image.open(output / name) as tile:
                        tile.thumbnail((640, 390))
                        x, y = index % 3 * 640, index // 3 * 425
                        sheet.paste(tile.convert("RGB"), (x, y+30))
                        draw.text((x+8, y+8), name, fill="white")
                sheet.save(output / f"sheet_{start//12:02}.jpg")
            (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        except BaseException as error:
            import traceback
            (output / 'render_error.txt').write_text(traceback.format_exc(), encoding='utf-8')
            errors.append(error)
        finally:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            done.set()
            qt_runtime._app.quit()

    # Evidence runs the central runtime on the process main thread. A threaded
    # QApplication can crash at interpreter teardown even with no live timers.
    # The runtime still owns application construction and the event loop.
    def enqueue():
        while not qt_runtime.is_alive():
            done.wait(0.01)
        qt_runtime.post(render)
    sender = threading.Thread(target=enqueue)
    sender.start()
    qt_runtime._run_loop()
    sender.join()
    if errors:
        raise errors[0]
    print(f"Rendered {len(manifest)} PNGs, contact sheets and dark/light GIFs to {output}")


def render_partb(snapshot, output):
    from types import SimpleNamespace
    from PIL import Image, ImageDraw
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication, QWidget
    from samsara.ui import theme, first_run_wizard_qt as wizard, tutorial_qt
    app = QApplication.instance()
    wizard.thread_registry.spawn = lambda *a, **k: None
    # Native DPI evidence uses a representative desktop rather than Qt's tiny
    # synthetic 800x800 screen (which shrinks to 533x533 DIP at 150%).
    if os.environ.get('INDICATOR_PROOF_AVAILABLE'):
        from PySide6.QtCore import QRect
        width, height = map(int, os.environ['INDICATOR_PROOF_AVAILABLE'].split(','))
        real_screen = app.primaryScreen()
        class ProofScreen:
            def availableGeometry(self):
                return QRect(0, 0, width, height)
            def __getattr__(self, name):
                return getattr(real_screen, name)
        screen = ProofScreen()
        QApplication.primaryScreen = staticmethod(lambda: screen)
    original_steps = tutorial_qt.TutorialWindow._build_step_list
    original_builder = tutorial_qt.TutorialWindow._build_page
    measurements, paths = [], []

    def capture(window, page, name, scale):
        print(f'Processing {name}', flush=True)
        app.processEvents()
        if hasattr(page, 'demo'):
            if page.demo.timer:
                page.demo.timer.stop()
            page.demo.set_frame(1)
            for beat in range(5):
                page.demo.set_frame(beat)
                assert page.demo.desktop.work_rect().contains(page.demo.desktop.indicator_rect())
            page.demo.set_frame(1)
            for label in (page.demo.caption, page.reason, page.picker.hint):
                assert label.heightForWidth(label.width()) <= max(44, label.fontMetrics().lineSpacing()*2)
            assert page.minimumSizeHint().height() <= page.height(), (name, page.minimumSizeHint().height(), page.height())
        scroll_max = 0
        if hasattr(window, '_display_placement_page') and page is window._display_placement_page:
            scroll = window._pages[window._step]
            scroll_max = scroll.verticalScrollBar().maximum()
            assert scroll_max == 0, (name, scroll_max)
        measurements.append(dict(name=name, window_height=window.height(), page_height=page.height(),
                                 minimum=page.minimumSizeHint().height(), scale=scale,
                                 device_pixel_ratio=window.devicePixelRatioF(), scroll_max=scroll_max))
        paths.append(snapshot(window, name, scale))

    def dispose(window):
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

    for palette in ('dark', 'light'):
        theme.set_theme(palette, refresh=False)
        for scale in (1.0, 1.5):
            for display in ('full', 'compact'):
                window = wizard._WizardWindow(output / 'never_created_profile.json')
                window._listening_display_page.group.button(('compact', 'full').index(display)).setChecked(True)
                window.show()
                choice_index = next(i for i, step in enumerate(wizard._STEPS) if step[0] == 'Listening display')
                window._step = choice_index
                window._show_step()
                capture(window, window._listening_display_page, f'Setup_choice_{display}_{palette}_{int(scale*100)}', scale)
                window._step = choice_index + 1
                window._show_step()
                for spot in ('bottom-left', 'top-right'):
                    window._display_placement_page.picker.choose(spot)
                    capture(window, window._display_placement_page, f'Setup_place_{display}_{palette}_{int(scale*100)}_{spot}', scale)
                dispose(window)
                for with_try in (False, True):
                    def steps(self):
                        original_steps(self)
                        self._steps = [step for step in self._steps if step[0] != 'try']
                        if with_try:
                            self._steps.insert(-1, ('try', 'Try your settings'))
                    tutorial_qt.TutorialWindow._build_step_list = steps
                    tutorial_qt.TutorialWindow._build_page = lambda self, key: QWidget() if key == 'try' else original_builder(self, key)
                    host = SimpleNamespace(config={'ui': {'listening_display': display}})
                    host.update_config_and_save = host.config.update
                    host.apply_listening_indicator_settings = lambda: None
                    window = tutorial_qt.TutorialWindow(host)
                    window._step = next(i for i, step in enumerate(window._steps) if step[0] == 'display')
                    window.show()
                    window._show_step()
                    for spot in ('bottom-left', 'top-right'):
                        window._display_page.picker.choose(spot)
                        capture(window, window._display_page,
                                f'Tutorial_{display}_{palette}_{int(scale*100)}_{len(window._steps)}steps_{spot}', scale)
                    assert window.height() <= window._default_window_height
                    dispose(window)
    for start in range(0, len(paths), 8):
        sheet = Image.new('RGB', (1600, 4*670), '#777777')
        draw = ImageDraw.Draw(sheet)
        for index, path in enumerate(paths[start:start+8]):
            with Image.open(path) as tile:
                tile.thumbnail((800, 630))
                x, y = index % 2 * 800, index // 2 * 670
                sheet.paste(tile.convert('RGB'), (x, y+30))
                draw.text((x+8, y+8), path.stem, fill='white')
        sheet.save(output/f'sheet_{start//8:02}.jpg')
    (output/'measurements.json').write_text(json.dumps(measurements, indent=2), encoding='utf-8')
    (output/'manifest.json').write_text(json.dumps([p.name for p in paths], indent=2), encoding='utf-8')


if __name__ == "__main__":
    main()
