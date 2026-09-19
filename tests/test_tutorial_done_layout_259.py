"""Queue 259: the tutorial's final recap is readable and its guide actions fit."""

from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea

from samsara.ui import theme
from samsara.ui.tutorial_qt import TutorialWindow
from tests._theme_stub_app import StubApp


def test_done_page_keeps_body_text_and_open_targets_unclipped(qapp):
    theme.set_theme("dark", refresh=False)
    tutorial = TutorialWindow(StubApp())
    tutorial._step = next(i for i, (key, _title) in enumerate(tutorial._steps) if key == "done")
    tutorial._show_step()
    tutorial.show()
    qapp.processEvents()
    try:
        page = tutorial._current_page
        assert isinstance(page, QScrollArea)
        content = page.widget()
        assert content is not None

        clipped = []
        for label in content.findChildren(QLabel):
            if not label.text().strip():
                continue
            needed = (label.heightForWidth(label.width()) if label.hasHeightForWidth()
                      else label.fontMetrics().height())
            if label.height() < needed:
                clipped.append((label.text(), label.height(), needed))
        assert not clipped, f"tutorial finish labels clipped to less than one line: {clipped}"

        body_fragments = (
            "Dictated text", "Ran a command", "Say ", "Ava (your on-device",
            "Hands-free", "Fine-tune your microphone", "Install Ollama",
            "Teach Samsara your specific words",
        )
        body_labels = [
            label for label in content.findChildren(QLabel)
            if any(fragment in label.text() for fragment in body_fragments)
        ]
        assert len(body_labels) == len(body_fragments)
        assert all(label.font().pixelSize() == theme.TYPE_BODY for label in body_labels)

        open_buttons = [
            button for button in content.findChildren(QPushButton)
            if button.text() == "Open →"
        ]
        assert len(open_buttons) == 3
        assert all(button.minimumHeight() >= theme.HIT_TARGET_MIN for button in open_buttons)
        assert all(button.height() >= theme.HIT_TARGET_MIN for button in open_buttons)

        assert content.height() >= content.layout().minimumSize().height()
    finally:
        tutorial.close()
        tutorial.deleteLater()
        qapp.processEvents()
        theme.set_theme("dark", refresh=False)
