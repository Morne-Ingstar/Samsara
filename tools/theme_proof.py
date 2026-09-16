"""Render every user-visible surface in both palettes, and check the render.

Queue 129. A light theme is only real if every surface takes it, so this
builds each surface twice -- once per palette -- grabs a PNG, and asserts the
two things a palette switch is most likely to break:

  * the surface actually changed polarity (a window whose stylesheet was
    built at import time renders identically and is caught here);
  * nothing scrolls or clips that did not scroll or clip before. A light
    palette does not change string lengths, but it does change a theme's
    metrics through the stylesheet, and the whole point of the check is that
    nobody has to remember to look.

Usage:
    F:\\envs\\sami\\python.exe tools\\theme_proof.py

Output: ui_proof/129/<surface>_<theme>.png plus proof.json with the table.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PySide6.QtWidgets import QAbstractScrollArea, QApplication  # noqa: E402

from samsara.ui import theme  # noqa: E402
from tests._theme_surfaces import SURFACES  # noqa: E402

OUT_DIR = REPO_ROOT / "ui_proof" / "129"
WINDOW_SIZE = (960, 700)


def _pump(app, rounds: int = 6) -> None:
    for _ in range(rounds):
        app.processEvents()


def _luminance(pixel: int) -> float:
    return (0.2126 * ((pixel >> 16) & 0xFF)
            + 0.7152 * ((pixel >> 8) & 0xFF)
            + 0.0722 * (pixel & 0xFF))


def _dominant(image) -> int:
    from collections import Counter
    counts = Counter()
    for y in range(0, image.height(), 4):
        for x in range(0, image.width(), 4):
            counts[image.pixel(x, y)] += 1
    return counts.most_common(1)[0][0]


def _scroll_overflow(widget) -> int:
    """How far past the window any scroll area extends. A non-zero value is
    not automatically wrong -- a long list scrolls by design -- but a value
    that CHANGES between the two themes is a layout the palette broke."""
    worst = 0
    for area in widget.findChildren(QAbstractScrollArea):
        bar = area.verticalScrollBar()
        if bar is not None:
            worst = max(worst, bar.maximum())
    return worst


def _clipped(widget) -> list[str]:
    """Visible children whose laid-out size is smaller than what they asked
    for -- the shape a clipped label or a squeezed button takes."""
    out = []
    for child in widget.findChildren(object):
        if not hasattr(child, "sizeHint") or not hasattr(child, "isVisible"):
            continue
        try:
            if not child.isVisible():
                continue
            hint = child.sizeHint()
            size = child.size()
        except (AttributeError, RuntimeError):
            continue
        if not hint.isValid():
            continue
        if size.height() + 1 < hint.height() and hint.height() - size.height() > 2:
            name = child.objectName() or type(child).__name__
            text = getattr(child, "text", lambda: "")()
            out.append(f"{name}({text[:28]}) {size.height()} < {hint.height()}")
    return out


def render(app, name: str, build, palette: str) -> dict:
    theme.set_theme(palette, refresh=False)
    widget = build()
    widget.resize(*WINDOW_SIZE)
    widget.show()
    _pump(app)
    pixmap = widget.grab()
    path = OUT_DIR / f"{name}_{palette}.png"
    saved = pixmap.save(str(path))
    image = pixmap.toImage()
    row = {
        "surface": name,
        "theme": palette,
        "png": path.name if saved else None,
        "size": [pixmap.width(), pixmap.height()],
        "dominant_luminance": round(_luminance(_dominant(image)), 1),
        "scroll_overflow_px": _scroll_overflow(widget),
        "clipped": _clipped(widget)[:6],
    }
    widget.hide()
    widget.deleteLater()
    _pump(app, 3)
    return row


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication(sys.argv)

    rows, failures = [], []
    for name, build in SURFACES.items():
        pair = {}
        for palette in ("dark", "light"):
            try:
                row = render(app, name, build, palette)
            except Exception as exc:
                failures.append(f"{name}/{palette}: {type(exc).__name__}: {exc}")
                traceback.print_exc()
                continue
            rows.append(row)
            pair[palette] = row
        if len(pair) == 2:
            dark, light = pair["dark"], pair["light"]
            if light["dominant_luminance"] <= dark["dominant_luminance"] + 40:
                failures.append(
                    f"{name}: light surface is not clearly lighter "
                    f"({light['dominant_luminance']} vs {dark['dominant_luminance']})")
            if light["scroll_overflow_px"] != dark["scroll_overflow_px"]:
                failures.append(
                    f"{name}: scroll overflow changed with the theme "
                    f"({dark['scroll_overflow_px']} -> {light['scroll_overflow_px']} px)")
            if len(light["clipped"]) != len(dark["clipped"]):
                failures.append(
                    f"{name}: clipped widgets changed with the theme "
                    f"({len(dark['clipped'])} -> {len(light['clipped'])})")

    theme.set_theme("dark", refresh=False)
    (OUT_DIR / "proof.json").write_text(
        json.dumps({"rows": rows, "failures": failures}, indent=2),
        encoding="utf-8")

    width = max(len(r["surface"]) for r in rows) if rows else 10
    print(f"\n{'surface'.ljust(width)}  theme  luminance  scroll  clipped  png")
    for row in rows:
        print(f"{row['surface'].ljust(width)}  {row['theme']:<5}  "
              f"{row['dominant_luminance']:>9}  {row['scroll_overflow_px']:>6}  "
              f"{len(row['clipped']):>7}  {row['png']}")
    print(f"\n{len(rows)} render(s) into {OUT_DIR}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for failure in failures:
            print("  -", failure)
        return 1
    print("\nno failures: every surface changed polarity, and neither scrolling "
          "nor clipping moved with the theme")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
