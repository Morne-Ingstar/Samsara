"""Queue 55 evidence: render the streaming preview's label exactly as the app does.

Builds the real samsara.streaming._StreamingWidget (no dictation import), feeds it
the strings set_transcript / set_paused produce, grabs each render to PNG and
reports how Qt interpreted the text (Qt.mightBeRichText) and what the label shows.

    F:\\envs\\sami\\python.exe perf_artifacts\\streaming_render_probe.py [out_dir]
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import Qt  # noqa: E402

from samsara import streaming  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "streaming_render_probe")
os.makedirs(OUT, exist_ok=True)

CASES = [
    ("apostrophe_final_only", ["like the one in it's"], ""),
    ("contractions_final_only", ["don't, we've, it's"], ""),
    ("apostrophe_with_partial", ["like the one in it's"], "don't"),
    ("ampersand_lt_quote", ['salt & pepper < 5 "quoted"'], ""),
    ("curly_apostrophe", ["it\u2019s"], ""),
    ("smart_quotes_emdash", ["\u201chello\u201d \u2014 there"], ""),
    ("bullet_token", ["\u2022 first item"], ""),
    ("accented", ["caf\u00e9 na\u00efve \u00fcber"], ""),
    ("newline_token", ["line one\nline two"], ""),
]


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    overlay = streaming.StreamingOverlayQt()
    overlay._widget = streaming._StreamingWidget(False)
    w = overlay._widget._w
    for name, finals, partial in CASES:
        overlay.set_transcript(finals, partial)
        app.processEvents()
        label = w._label
        sent = label.text()
        fmt = label.textFormat()
        rich = Qt.mightBeRichText(sent) if fmt == Qt.TextFormat.AutoText else fmt == Qt.TextFormat.RichText
        w.resize(streaming.OVERLAY_W, 120)
        path = os.path.join(OUT, f"{name}.png")
        w.grab().save(path)
        print(f"{name:<26} format={fmt.name:<10} rendered_as_rich={rich!s:<5} sent={sent!r}")
    print(f"PNGs in {OUT}")


if __name__ == "__main__":
    main()
