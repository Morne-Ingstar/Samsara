"""Focused accessibility checks for the centralized Qt theme.

These intentionally cover shared tokens and representative real widgets;
legacy windows with private stylesheets are outside the centralized pass.
"""

from PySide6.QtWidgets import QLabel, QPushButton

from samsara.ui import theme


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = []
    for value in rgb:
        channel = value / 255.0
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _rgba_white_over(hex_background: str, alpha: float) -> tuple[int, int, int]:
    background = theme._hex_to_rgb(hex_background)
    return tuple(round(value * (1.0 - alpha) + 255 * alpha) for value in background)


def _contrast_ratio(rgb_a: tuple[int, int, int], rgb_b: tuple[int, int, int]) -> float:
    lighter, darker = sorted(
        (_relative_luminance(rgb_a), _relative_luminance(rgb_b)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


def test_shared_caption_floor_and_secondary_opacity():
    assert theme.FONT_SIZE_CAPTION == 12
    assert theme.TEXT_SECONDARY == "rgba(255,255,255,0.75)"


def test_secondary_text_exceeds_wcag_normal_text_contrast_on_every_surface():
    for background in (theme.BG0, theme.BG1, theme.BG2):
        background_rgb = theme._hex_to_rgb(background)
        secondary_rgb = _rgba_white_over(background, 0.75)
        assert _contrast_ratio(secondary_rgb, background_rgb) >= 4.5


def test_shared_stylesheet_propagates_accessible_tokens():
    stylesheet = theme.build_stylesheet()
    assert f"color: {theme.TEXT_SECONDARY};" in stylesheet
    assert f"font-size: {theme.FONT_SIZE_CAPTION}px;" in stylesheet


def test_representative_caption_and_ghost_button_fit_their_content(qapp):
    caption = QLabel("A readable explanatory caption")
    caption.setWordWrap(True)
    caption.setStyleSheet(
        f"color:{theme.TEXT_SECONDARY};font-size:{theme.FONT_SIZE_CAPTION}px;"
    )
    caption.setFixedWidth(240)

    ghost = QPushButton("Skip this step")
    theme.make_ghost(ghost)
    ghost.ensurePolished()
    caption.ensurePolished()
    qapp.processEvents()

    assert caption.heightForWidth(caption.width()) > 0
    assert caption.sizeHint().height() >= caption.fontMetrics().height()
    assert ghost.sizeHint().height() >= ghost.fontMetrics().height()
    assert ghost.property("class") == "ghost"
