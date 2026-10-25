"""861 Home footer: measured line breaks, one set of accessible controls."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from samsara.ui import theme


from samsara.ui.history_view import _FlowLayout


class InsetFlowLayout(_FlowLayout):
    """Home's tray needs real insets; the shared flow intentionally ignores margins."""

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        inner = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        return super()._do_layout(inner, test_only) + margins.top() + margins.bottom()

    def minimumSize(self):
        margins = self.contentsMargins()
        return super().minimumSize() + QSize(margins.left() + margins.right(),
                                             margins.top() + margins.bottom())


class _InkLink(QPushButton):
    def sizeHint(self):
        from samsara.ui.home_qt import _px
        self.ensurePolished()
        return QSize(max(_px(44), super().sizeHint().width(),
                         QFontMetrics(self.font()).horizontalAdvance(self.text()) + 4), _px(44))

    def minimumSizeHint(self):
        return self.sizeHint()


class HomeFooter(QWidget):
    """Creed plus complete, content-sized links; no right-pushing stretch."""

    def __init__(self, home):
        super().__init__(home)
        from samsara.ui.home_qt import CREED, IDENTITY_LINKS, KOFI_LINK, KOFI_URL, _label, _link, _label_css, _px
        self.setObjectName("homeFooter")
        self.setStyleSheet("background: transparent;")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.creed = _label(CREED, role="creed", color=theme.TEXT_SECONDARY,
                            weight=theme.FONT_WEIGHT_CREED, family=theme.FONT_FAMILY_DISPLAY, wrap=True)
        self.creed.setParent(self)
        self.creed.setStyleSheet(_label_css("creed", theme.TEXT_SECONDARY, theme.FONT_WEIGHT_CREED,
                                           family=theme.FONT_FAMILY_DISPLAY).replace(
                                               f"font-size: {_px(theme.HOME_TYPE_SCALE['creed'])}px",
                                               f"font-size: {_px(16)}px"))
        self.creed.setAccessibleName("Creed")
        self.creed.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.links = QWidget(self)
        self.links.setStyleSheet("background: transparent;")
        self.links.setAccessibleName("Links")
        from samsara.ui.home_qt import FlowLayout, open_url
        self.flow = FlowLayout(self.links, h_spacing=8, v_spacing=8)
        self.flow.setContentsMargins(0, 0, 0, 0)
        home._links = {}
        for text, url in (*IDENTITY_LINKS, (KOFI_LINK, KOFI_URL)):
            prototype = _link(text, url)
            button = _InkLink(text, self.links)
            button.setStyleSheet(prototype.styleSheet())
            button.setFont(prototype.font())
            button.setAccessibleName(text)
            button.setAccessibleDescription(url)
            button.setProperty("typeRole", prototype.property("typeRole"))
            button.setProperty("homeLink", True)
            button.setCursor(prototype.cursor())
            button.setFocusPolicy(prototype.focusPolicy())
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.setFlat(True)
            button.clicked.connect(lambda checked=False, u=url: open_url(u))
            prototype.deleteLater()
            self.flow.addWidget(button)
            home._links[text] = button
        home._creed = self.creed
        home._creed_box = self
        home._tagline_row = self
        home._links_row = home._support_row = self.links

    def _measure(self, width):
        from samsara.ui.home_qt import _px
        self.creed.ensurePolished()
        for i in range(self.flow.count()):
            self.flow.itemAt(i).widget().ensurePolished()
        creed_w = QFontMetrics(self.creed.font()).horizontalAdvance(self.creed.text()) + 4
        links_w = sum(self.flow.itemAt(i).sizeHint().width() for i in range(self.flow.count())) + 8 * (self.flow.count() - 1)
        beside = creed_w + 16 + links_w <= width
        label_w = min(width, creed_w)
        creed_h = max(_px(22), self.creed.heightForWidth(max(1, label_w)))
        link_w = min(width, links_w)
        link_h = self.flow.heightForWidth(link_w)
        height = max(creed_h, link_h) if beside else creed_h + 8 + link_h
        return beside, label_w, creed_h, link_w, link_h, height

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._measure(max(1, width))[-1]

    def sizeHint(self):
        return QSize(760, self.heightForWidth(self.width() or 760))

    def minimumSizeHint(self):
        return QSize(0, self.heightForWidth(self.width() or 760))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.arrange()

    def arrange(self):
        beside, cw, ch, lw, lh, height = self._measure(self.width())
        self.creed.setGeometry(0, (height - ch) // 2 if beside else 0, cw, ch)
        self.links.setGeometry(cw + 16 if beside else 0,
                               (height - lh) // 2 if beside else ch + 8, lw, lh)
        self.flow.setGeometry(QRect(0, 0, lw, lh))
