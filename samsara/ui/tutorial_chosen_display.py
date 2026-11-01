"""Passive tutorial demo of the saved listening display and placement."""
from PySide6.QtCore import QRectF
from samsara import listening_display
from samsara.onboarding.display_placement import current_position
from samsara.ui.widgets.indicator_demo import DesktopSketch, IndicatorDemo, POSITIONS


class _ChosenDisplaySketch(DesktopSketch):
    """Illustrate saved placement without any draggable/choosable targets."""

    def indicator_size(self):
        if self.display == 'full':
            return super().indicator_size()
        from samsara.ui.widgets.indicator_demo import compact_stand_in
        from samsara.ui.listening_indicator import ListeningIndicator
        proxy = compact_stand_in()
        proxy._font = lambda: ListeningIndicator._font(proxy)
        return ListeningIndicator._pill_width(proxy, self.state_words()[0], True), 36

    def paint_desktop(self, painter):
        # These examples keep drafts in Samsara; none imply text was inserted.
        beat = self.beat
        try:
            self.beat = 0
            super().paint_desktop(painter)
        finally:
            self.beat = beat

    def indicator_rect(self):
        config = self.parentWidget().config
        width, height = self.indicator_size()
        position = current_position(config, self.display)
        if position in POSITIONS:
            return self.target_rect(position, width, height)
        if self.display == 'compact':
            record = config.get('listening_indicator_custom_position') or {}
            cx, cy = record.get('cx', .5), record.get('cy', .5)
        else:
            from samsara.live_surface.placement import normalize_placement
            placement = ((config.get('ui') or {}).get('live_surface') or {}).get('placement') or {}
            record = (placement.get('monitors') or {}).get(placement.get('preferred_monitor')) or placement.get('seed')
            saved = normalize_placement(record)
            cx, cy = {'top': (saved.t, 0), 'bottom': (saved.t, 1),
                      'left': (0, saved.t), 'right': (1, saved.t)}.get(saved.edge.value, (saved.free_cx, saved.free_cy))
        area = self.work_rect().adjusted(10, 10, -10, -10)
        return QRectF(area.left() + (area.width()-width)*cx,
                      area.top() + (area.height()-height)*cy, width, height)

    def demo_view(self):
        from samsara.live_surface.model import LiveSurfaceModel, Lane, CaptureState, DocumentState
        model = LiveSurfaceModel(clock=lambda: 0)
        model.lane = Lane.HOLD if self.beat < 2 else Lane.HANDS_FREE_DICTATE
        model.capture = CaptureState.RECORDING if self.beat in (0, 3) else CaptureState.HANDS_FREE_LISTENING
        if self.beat in (1, 4):
            model.capture = CaptureState.OFF
            model.text = 'A new thought.'  # PLACEHOLDER COPY (837): illustrative sample.
            model.document = DocumentState.EDITABLE_DRAFT
        return model.view()

    def working(self):
        return False

    def result_message(self):
        return ''

    def _paint_indicator_into(self, painter, rect, dpr):
        header, subline = self.state_words()
        state = 'recording' if self.beat in (0, 3) else 'idle'
        if self.display == 'full':
            beat = self.beat
            try:
                # The shared painter puts a level meter on beat 1 only.
                self.beat = 1 if state == 'recording' else 0
                self._paint_indicator_body(painter, rect, dpr, header, subline, '', state, 0)
            finally:
                self.beat = beat
        else:
            from samsara.ui.widgets.indicator_demo import compact_stand_in
            from samsara.ui.listening_indicator import ListeningIndicator
            proxy = compact_stand_in(listening=state == 'recording', mode_text=header)
            proxy._glyph_mark = lambda: (state, 'asleep', 0, 1.0)
            proxy._font = lambda: ListeningIndicator._font(proxy)
            bg, fg, label, glyph = ListeningIndicator._resolve_colors(proxy)
            ListeningIndicator._paint_pill(proxy, painter, rect, bg, fg, label, glyph)


class _ChosenDisplayDemo(IndicatorDemo):
    # PLACEHOLDER COPY (837)
    captions = (
        'Recording — Samsara is listening to your voice.',
        'Staged — your words are waiting to be pasted.',
        'Hands-free — listening for your next thought or command.',
        'Hands-free recording — speak without holding a key.',
        'Staged — your draft stays available until you paste it.',
    )

    def __init__(self, config, parent):
        self.config = config
        super().__init__(parent, display=listening_display.display_choice(config).value,
                         desktop_type=_ChosenDisplaySketch)
        for index, button in enumerate(self.steps):
            button.setAccessibleName(self.captions[index])

    def caption_for_beat(self, beat):
        return self.captions[beat]


