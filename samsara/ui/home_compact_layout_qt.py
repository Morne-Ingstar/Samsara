"""1014: spend short-window space on content rather than section furniture."""
from PySide6.QtWidgets import QBoxLayout


def apply_compact_layout(home, width):
    compact = home.height() < 500 and home._cards_cols == 3
    if getattr(home, '_compact_layout', None) == compact:
        return
    home._compact_layout = compact
    if not hasattr(home, '_roomy_stat_size'):
        home._roomy_stat_size = home._stat_w, home._stat_h
        home._roomy_stat_spacing = home._words_card.layout().spacing()
        home._roomy_stat_margins = home._words_card.layout().contentsMargins()
    home._destination_heading.setVisible(not compact)
    home._notice_card.layout().setContentsMargins(16, 8 if compact else 16, 16, 8 if compact else 16)
    for card in (home._words_card, home._infinity_card):
        layout = card.layout()
        layout.setDirection(QBoxLayout.Direction.LeftToRight if compact else QBoxLayout.Direction.TopToBottom)
        if compact:
            layout.setContentsMargins(12, 8, 12, 8)
        else:
            layout.setContentsMargins(home._roomy_stat_margins)
        layout.setSpacing(8 if compact else home._roomy_stat_spacing)
    home._stat_w, home._stat_h = (210, 56) if compact else home._roomy_stat_size
    home._stat_size = None
    home._relayout_identity(width)
