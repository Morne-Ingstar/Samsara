"""384: native-font grabs of real Home/hub widgets with isolated app data.

One run regenerates HEAD before pictures, current after pictures and all picks:
    F:\\envs\\sami\\python.exe tools/home_bottom_renders.py
    ... --status S2 --creed C2 --links L2 --support "Contributions welcome."
No dictation import, live profile, registry writes, or visible app windows.
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
REPORT = Path(r"C:\Users\Morne\Documents\Claude\reports\384")
OUT = ROOT / "ui_proof" / "384"
sys.path.insert(0, str(ROOT))


def load_head():
    """Bind the four owned modules from HEAD without altering the worktree."""
    snapshot = REPORT / "artifacts" / "head_source"
    snapshot.mkdir(parents=True, exist_ok=True)
    for name in ("theme", "command_marquee", "home_qt", "main_window_qt"):
        data = subprocess.check_output(
            ["git", "show", f"HEAD:samsara/ui/{name}.py"], cwd=ROOT)
        path = snapshot / f"{name}.py"
        path.write_bytes(data)
        spec = importlib.util.spec_from_file_location(f"samsara.ui.{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        import samsara.ui
        setattr(samsara.ui, name, module)


class MemorySettings:
    def __init__(self, *_):
        self.values = {}

    def value(self, key, default=None, **_):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value


def stub_app():
    return SimpleNamespace(
        config={"mode": "hold", "hotkey": "ctrl+shift", "microphone": 7,
                "wake_word_enabled": True, "wake_word_configured": True,
                "wake_word_config": {"phrase": "jarvis"},
                "ava_declined": True, "ollama": {"enabled": False}},
        available_mics=[{"id": 7, "name": "Analogue 1 + 2 (Focusrite USB Audio)"}],
        recording=False, snoozed=False, wake_word_active=True,
        continuous_active=False, command_mode_active=False,
        ava_command_session_active=False, ava_mode_active=False, toggle_active=False,
        _outcome_ring=collections.deque(maxlen=8),
        history_store=SimpleNamespace(query=lambda **_: [], words_typed_since=lambda _: 0),
        _wake_consumer=SimpleNamespace(running=True), _hands_free_fault=None,
        _wake_start_pending=False, wake_ready_state=lambda: "ready",
    )


def render(args):
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetrics
    from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget
    from samsara.ui import command_marquee as cm, home_qt as home, home_signals, main_window_qt as hub, theme

    qapp = QApplication.instance()
    qapp.setFont(QFont("Segoe UI"))
    home.QSettings = MemorySettings
    home_signals.corrections_waiting = lambda _: 0
    home_signals._user_words_exist = lambda: True
    home_signals.generic_hints = lambda _: []
    home_signals._diag_records = lambda *_, **__: []
    theme.set_theme("dark", refresh=False)
    target = OUT / args.phase
    target.mkdir(parents=True, exist_ok=True)
    evidence = {"platform": qapp.platformName(), "font_families": len(QFontDatabase.families()),
                "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
                "frames": [], "sweep": [], "creed_variants": []}
    fallback = QFont()
    fallback.setFamilies(["Palatino Linotype", "Book Antiqua", "Georgia", "serif"])
    fallback.setPixelSize(theme.TYPE_CREED)
    fallback.setWeight(QFont.Weight.Bold)
    fallback.setCapitalization(QFont.Capitalization.AllUppercase)
    info = QFontInfo(fallback)
    evidence["without_perpetua"] = {"family": info.family(), "weight": info.weight(), "exactMatch": info.exactMatch()}

    def pump():
        for _ in range(30):
            qapp.processEvents()

    def save(widget, path, zoom=1):
        pump()
        pix = widget.grab()
        if zoom != 1:
            pix = pix.scaled(pix.width() * zoom, pix.height() * zoom,
                             Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        assert pix.save(str(path)), path
        print(f"saved {path.relative_to(ROOT)} {pix.width()}x{pix.height()}", flush=True)

    def creed(label, variant, scale):
        weight = 400 if variant == "C0" else (600 if variant == "C3" else 700)
        family = "'Palatino Linotype'" if variant == "C3" else theme.FONT_FAMILY_DISPLAY
        label.setStyleSheet(home._label_css("creed", theme.TEXT_SECONDARY, weight, family, "0.12em"))
        label.ensurePolished()
        font = label.font()
        font.setFamilies(["Palatino Linotype"] if variant == "C3" else ["Perpetua Titling MT", "Palatino Linotype", "Book Antiqua", "Georgia"])
        font.setWeight(QFont.Weight(weight))
        font.setCapitalization(QFont.Capitalization.MixedCase if variant == "C0" else QFont.Capitalization.AllUppercase)
        label.setFont(font)
        if variant == "C2":
            label.setStyleSheet(label.styleSheet().replace(
                f"font-size: {home._px(theme.TYPE_CREED)}px",
                f"font-size: {home._px(theme.TYPE_EMPHASIS)}px"))

    def links(page, variant):
        if variant == "L1":
            return
        for btn in page._links.values():
            css = btn.styleSheet().replace("font-weight: 500", "font-weight: 700")
            css = css.replace(f"font-size: {home._px(home.TYPE['note'])}px",
                              f"font-size: {home._px(theme.TYPE_SECTION_LABEL)}px; letter-spacing: {theme.LETTER_SPACING_SECTION}")
            btn.setStyleSheet(css)
            btn.ensurePolished()
            font = btn.font()
            font.setCapitalization(QFont.Capitalization.AllUppercase)
            btn.setFont(font)

    def page(scale):
        font = QFont("Segoe UI")
        font.setPointSizeF(home.BASE_POINT_SIZE * scale)
        qapp.setFont(font)
        return home.HomePage(stub_app())

    def window(width, height, scale, status="S1"):
        font = QFont("Segoe UI")
        font.setPointSizeF(home.BASE_POINT_SIZE * scale)
        qapp.setFont(font)
        win = hub._MainWindow(stub_app())
        win._poll_timer.stop()
        win._header_mark.stop()
        win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        win.resize(width, height)
        p = win._panel_cache["Home"]
        if args.phase == "after":
            creed(p._creed, args.creed, scale)
            links(p, args.links)
            if args.support:
                p._support_text.setText(args.support)
            if status == "S2":
                old = win._example_strip
                win.statusBar().removeWidget(old)
                old.hide()
                win._example_strip = cm.build_example_strip(stub_app(), show_control=False, rng=random.Random(11))
                win.statusBar().addWidget(win._example_strip, 1)
        strip = win._example_strip
        strip._phrases = ["what can i say", "go back", "scroll down", "show commands"]
        win._refresh_status()
        # The status-bar font is not bound to Home's environment scale; apply
        # the same 100/150% text scaling explicitly to these real status widgets.
        import re
        for widget in win.statusBar().findChildren(QWidget):
            css = widget.styleSheet()
            if scale != 1:
                widget.setStyleSheet(re.sub(r"font-size: (\d+)px", lambda m: f"font-size: {round(int(m[1])*scale)}px", css))
        css = strip.label.styleSheet()
        if "{" not in css:
            css = "QLabel { " + css + " }"
        strip.label.setStyleSheet(css + f" QLabel {{ font-size: {round(theme.TYPE_BODY * scale)}px; }}")
        win.show()
        pump()
        p._scroll.verticalScrollBar().setValue(p._scroll.verticalScrollBar().maximum())
        pump()
        strip._apply(0)
        pump()
        return win, p, strip

    for scale in (1.0, 1.5):
        for width, height in ((900, 650), (1280, 800)):
            win, p, strip = window(width, height, scale, args.status)
            tag = f"{width}x{height}_{round(scale*100)}"
            save(win, target / f"home_{tag}.png")
            pos = p._creed_box.mapTo(win, QPoint(0, 0))
            crop = win.grab().copy(pos.x(), pos.y(), p._creed_box.width(), p._creed_box.height())
            crop = crop.scaled(crop.width()*2, crop.height()*2)
            assert crop.save(str(target / f"identity_{tag}_2x.png"))
            info = QFontInfo(p._creed.font())
            row = {"requested": [width, height], "actual": [win.width(), win.height()], "scale": scale,
                   "creed_family": info.family(), "weight": info.weight(), "exactMatch": info.exactMatch(),
                   "hint": strip.text, "advance": QFontMetrics(strip.label.font()).horizontalAdvance(strip.text),
                   "label_width": strip.label.width(), "contents_width": strip.label.contentsRect().width(),
                   "cached_advance": getattr(strip, "_metrics", QFontMetrics(strip.label.font())).horizontalAdvance(strip.text)}
            evidence["frames"].append(row)
            if width == 1280 and scale == 1:
                # Include the item frames adjoining the explicit separator.
                sep = next(w for w in win.statusBar().findChildren(QFrame) if w.height() == 16)
                pos = sep.mapTo(win.statusBar(), QPoint(0, 0))
                crop = win.statusBar().grab().copy(pos.x()-10, 0, 24, win.statusBar().height())
                crop = crop.scaled(crop.width()*4, crop.height()*4)
                assert crop.save(str(target / "separator_4x.png"))
                if args.phase == "before":
                    original = win.statusBar().styleSheet()
                    win.statusBar().setStyleSheet(original + " QStatusBar::item { border: none; }")
                    pump()
                    diagnostic = win.statusBar().grab().copy(pos.x()-10, 0, 24, win.statusBar().height())
                    diagnostic = diagnostic.scaled(diagnostic.width()*4, diagnostic.height()*4)
                    assert diagnostic.save(str(target / "separator_without_item_frame_4x.png"))
                    win.statusBar().setStyleSheet(original)
            if width == 1280:
                for narrow in range(1280, 159, -20):
                    win.setMinimumWidth(0)
                    win.resize(narrow, height)
                    pump()
                    metrics = QFontMetrics(strip.label.font())
                    measured = metrics.horizontalAdvance(strip.text)
                    available = strip.label.contentsRect().width()
                    evidence["sweep"].append({"scale": scale, "requested_width": narrow, "actual_width": win.width(),
                                              "text": strip.text, "advance": measured, "contents_width": available})
                    if strip.text and measured > available:
                        save(win, target / f"clipped_{round(scale*100)}_{narrow}.png")
                        break
            win.hide()
            win.deleteLater()
            pump()

    if args.phase == "after":
        for scale in (1.0, 1.5):
            win, _, _ = window(1280, 800, scale, "S2")
            save(win.statusBar(), OUT / f"status_S2_{round(scale*100)}.png", 2)
            win.hide()

        def sheet(name, rows):
            panel = QWidget()
            panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            panel.setStyleSheet(f"background: {theme.BG1}; color: {theme.TEXT_PRIMARY}; font-family: 'Segoe UI'; font-size: {theme.TYPE_BODY}px;")
            lay = QVBoxLayout(panel)
            lay.setContentsMargins(24, 16, 24, 16)
            for title, widget in rows:
                lay.addWidget(QLabel(title))
                lay.addWidget(widget)
            panel.resize(1000, panel.sizeHint().height())
            panel.show()
            pump()
            if name == "creed_variants.png":
                evidence["creed_variants_painted"] = [
                    {"row": title, "family": QFontInfo(widget.font()).family(),
                     "weight": QFontInfo(widget.font()).weight(), "pixel_size": widget.font().pixelSize()}
                    for title, widget in rows]
            save(panel, OUT / name)
            panel.hide()

        rows = []
        for scale in (1.0, 1.5):
            for variant in ("C0", "C1", "C2", "C3"):
                p = page(scale)
                creed(p._creed, variant, scale)
                p._creed.setStyleSheet("QLabel { " + p._creed.styleSheet() + " }")
                p._creed.setFixedHeight(round(theme.TYPE_HEADING*scale*2))
                info = QFontInfo(p._creed.font())
                evidence["creed_variants"].append({"variant": variant, "scale": scale, "family": info.family(),
                                                   "requested_weight": 600 if variant == "C3" else (400 if variant == "C0" else 700), "weight": info.weight()})
                rows.append((f"{variant} / {round(scale*100)}%", p._creed))
        sheet("creed_variants.png", rows)
        rows = []
        for variant in ("L1", "L2"):
            for state in ("rest", "hover", "focus"):
                p = page(1)
                links(p, variant)
                if state != "rest":
                    for btn in p._links.values():
                        # Force the actual state rule for all three on one row;
                        # there can only be one real keyboard focus at a time.
                        css = btn.styleSheet()
                        import re
                        rule = re.search(r"QPushButton:" + state + r"\s*\{([^}]+)\}", css).group(1)
                        btn.setStyleSheet(css + f"QPushButton {{ {rule} }}")
                rows.append((f"{variant} / {state} (actual state style)", p._links_row))
        sheet("link_variants.png", rows)
        rows = []
        for scale in (1.0, 1.5):
            for text in (home.SUPPORT_LINE, "Contributions welcome."):
                p = page(scale)
                p._support_text.setText(text)
                rows.append((f"{round(scale*100)}% / {'as shipped' if text == home.SUPPORT_LINE else 'placeholder: owner chooses words'}", p._creed_box))
        sheet("support_line.png", rows)
    (target / "measurements.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert "dictation" not in sys.modules, "renders must never load the live app module"
    print(json.dumps(evidence["frames"], indent=2), flush=True)


def render_388_extras(win, page, out, save, pump, scale, width):
    """388 picture-only alternatives; leave the shipped constants intact."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFont, QImage, QPainter
    from PySide6.QtWidgets import QFrame
    from samsara.ui import splash_qt, theme

    if width == 1280 and scale == 1:
        row = win._lbl_mode.parentWidget().parentWidget()
        layout = row.layout()
        lines = [w for w in row.findChildren(QFrame) if w.width() == 1 and w.height() == 16]
        indices = [layout.indexOf(line) for line in lines]
        for line in lines:
            layout.removeWidget(line)
            line.hide()
        layout.setSpacing(28)
        save(win.statusBar(), "statusbar_no_lines_2x.png", 2)
        layout.setSpacing(14)
        for index, line in zip(indices, lines):
            layout.insertWidget(index, line, 0, Qt.AlignmentFlag.AlignVCenter)
            line.show()
        pump()
        return
    if width != 900 or scale != 1:
        return

    import re
    original = page._creed.text()
    shipped = page._creed_box.window().grab()
    point = page._creed_box.mapTo(page._creed_box.window(), QPoint())
    shipped = shipped.copy(point.x(), point.y(), page._creed_box.width(), page._creed_box.height()).toImage()
    alternate = re.sub(r"\s*\u00b7\s*(?=Accessibility)", " ", original, flags=re.IGNORECASE)
    assert alternate != original, original
    page._creed.setText(alternate)
    save(page._creed_box, "identity_no_hanging_dot_2x.png", 2)
    alt = QImage(str(out / "identity_no_hanging_dot_2x.png"))
    shipped = shipped.scaled(shipped.width()*2, shipped.height()*2)
    pair = QImage(shipped.width()+alt.width()+48, max(shipped.height(), alt.height())+48, QImage.Format.Format_ARGB32)
    pair.fill(theme.qcolor(theme.BG1))
    painter = QPainter(pair)
    painter.setFont(QFont("Segoe UI", 12))
    painter.setPen(theme.qcolor(theme.TEXT_PRIMARY))
    painter.drawText(12, 24, "Keep hanging dot (shipped)")
    painter.drawText(shipped.width()+36, 24, "Drop hanging dot (picture only)")
    painter.drawImage(12, 40, shipped)
    painter.drawImage(shipped.width()+36, 40, alt)
    painter.end()
    assert pair.save(str(out / "identity_hanging_dot_comparison.png"))
    page._creed.setText(original)
    pump()
    save(page._creed_box, "identity_2x.png", 2)

    label = page._support_text
    reduced_motion = splash_qt._system_reduced_motion
    try:
        splash_qt._system_reduced_motion = lambda: False
        label.spin_once()
        assert label._layer is not None
        label._animation.pause()
        frames = []
        for ms in range(0, 701, 100):
            label._animation.setCurrentTime(ms)
            pump()
            frames.append(label.grab().toImage())
        cell_w, cell_h = frames[0].width()+24, max(frames[0].height(), 50)+32
        sheet = QImage(cell_w*2, cell_h*4, QImage.Format.Format_ARGB32)
        sheet.fill(theme.qcolor(theme.BG1))
        painter = QPainter(sheet)
        painter.setFont(QFont("Segoe UI", 10))
        painter.setPen(theme.qcolor(theme.TEXT_SECONDARY))
        for i, frame in enumerate(frames):
            x, y = (i % 2)*cell_w, (i // 2)*cell_h
            painter.drawText(x+12, y+17, f"{i*100} ms")
            painter.drawImage(x+12, y+24, frame)
        painter.end()
        sheet = sheet.scaled(sheet.width()*2, sheet.height()*2)
        assert sheet.save(str(out / "spin_frames.png"))
        label._cancel()
    finally:
        splash_qt._system_reduced_motion = reduced_motion

    page.eligible_diagnostics = lambda: []
    page._due_whats_new = lambda: ("9.9.9", "A clearer Home and a link to the full changelog.")
    page._hint = None
    page._refresh_hint()
    save(page._hint_card, "whats_new_card_2x.png", 2)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--388-finish7":
        helper = Path(r"C:\Users\Morne\Documents\Claude\reports\388\artifacts\claude\finish7\render.py")
        return subprocess.run([sys.executable, str(helper), *sys.argv[2:]], check=False).returncode
    if len(sys.argv) > 1 and sys.argv[1] == "--388-finish6":
        helper = Path(r"C:\Users\Morne\Documents\Claude\reports\388\artifacts\claude\finish6\render.py")
        return subprocess.run([sys.executable, str(helper), *sys.argv[2:]], check=False).returncode
    if len(sys.argv) > 1 and sys.argv[1] == "--388-finish":
        helper = Path(r"C:\Users\Morne\Documents\Claude\reports\388\artifacts\claude\render_finish.py")
        return subprocess.run([sys.executable, str(helper), *sys.argv[2:]], check=False).returncode
    if len(sys.argv) > 1 and sys.argv[1] == "--388":
        helper = Path(r"C:\Users\Morne\Documents\Claude\reports\388\artifacts\night\render_388.py")
        return subprocess.run([sys.executable, str(helper), *sys.argv[2:]], check=False).returncode
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("all", "before", "after"), default="all")
    parser.add_argument("--status", choices=("S1", "S2"), default="S1")
    parser.add_argument("--creed", choices=("C0", "C1", "C2", "C3"), default="C1")
    parser.add_argument("--links", choices=("L1", "L2"), default="L1")
    parser.add_argument("--support", default="")
    args = parser.parse_args()
    if args.phase == "all":
        for phase in ("before", "after"):
            subprocess.run([sys.executable, __file__, "--phase", phase, "--status", args.status,
                            "--creed", args.creed, "--links", args.links, "--support", args.support], check=True)
        return 0
    os.environ["QT_QPA_PLATFORM"] = "windows"
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["SAMSARA_HOME_DIR"] = str(REPORT / "artifacts" / "render_home")
    if args.phase == "before":
        load_head()
    from samsara.ui import qt_runtime
    done, failures = threading.Event(), []

    def work():
        try:
            render(args)
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            done.set()
    qt_runtime.ensure_started()
    qt_runtime.post(work)
    if not done.wait(120):
        failures.append("render timed out")
    for failure in failures:
        print(failure, file=sys.stderr, flush=True)
    qt_runtime.shutdown()
    # Qt lives on its runtime thread. Avoid PySide cross-thread finalization
    # of that QApplication after every successful save/assertion has finished.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
