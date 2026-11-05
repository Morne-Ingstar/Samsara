"""Small offline explanation cards inside existing Qt hosts; no app bootstrap."""
import weakref

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from samsara.help_terms import HelpTerm, TERMS, explanation, page_explanation
from samsara.ui import theme


def open_web_guide(url):
    """Only called on explicit activation; caller supplies a packaged URL."""
    return bool(QDesktopServices.openUrl(QUrl(url)))


class HelpButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)


def _style_button(button):
    theme.make_secondary(button)
    button.setStyleSheet(button.styleSheet() +
        f'QPushButton{{min-height:{theme.HIT_TARGET_MIN - 2}px;max-height:{theme.HIT_TARGET_MIN - 2}px;'
        'padding:0 12px;text-decoration:none;}')
    button.setFixedHeight(theme.HIT_TARGET_MIN)
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)


def term_button(term_id, *, compact=False):
    term = explanation(term_id)
    button = HelpButton('?' if compact else term.title + ' ?')
    # PLACEHOLDER COPY (932)
    button.setAccessibleName('Explain ' + term.title.lower())
    button.setProperty('contextHelpControl', True)
    button.setCheckable(True)
    _style_button(button)
    if compact:
        button.setFixedWidth(theme.HIT_TARGET_MIN)
    return button


def is_help_interaction(widget):
    """Keep Reference command details when a glossary control is activated."""
    while isinstance(widget, QWidget):
        if widget.property('contextHelpControl'):
            return True
        widget = widget.parentWidget()
    return False


class ExplanationCard(QFrame):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setObjectName('contextHelpCard')
        self.setProperty('contextHelpControl', True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        self.heading = QLabel()
        self.body = QLabel()
        for label in (self.heading, self.body):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            layout.addWidget(label)
        # PLACEHOLDER COPY (932): actions on separate lines also fit narrow hosts.
        self.close_button = HelpButton('Close explanation')
        self.close_button.setAccessibleName('Close explanation')
        self.close_button.clicked.connect(lambda: controller.dismiss(True))
        self.guide_button = HelpButton('Open web guide')
        self.guide_button.clicked.connect(self.open_guide)
        for button in (self.close_button, self.guide_button):
            _style_button(button)
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.status)
        self.hide()

    def set_term(self, term):
        self.term = term
        for button in (self.close_button, self.guide_button):
            _style_button(button)
        self.setStyleSheet(
            f'QFrame#contextHelpCard{{background:{theme.BG1};border:1px solid {theme.ACCENT};border-radius:8px;}}'
            f'QLabel{{background:transparent;border:none;color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;}}')
        self.heading.setStyleSheet(f'font-size:{theme.TYPE_EMPHASIS}px;font-weight:600;color:{theme.ACCENT};')
        self.status.setStyleSheet(f'font-size:{theme.TYPE_MIN}px;color:{theme.TEXT_SECONDARY};')
        self.heading.setText(term.title)
        self.heading.setVisible(bool(term.title))
        self.body.setText(term.body)
        self.setAccessibleName(term.title)
        self.setAccessibleDescription(term.body)
        self.guide_button.setVisible(bool(term.guide))
        self.guide_button.setAccessibleName('Open web guide for ' + term.title)
        self.guide_button.setToolTip('Opens your browser; Internet required')
        self.status.setText('Opens your browser · Internet required' if term.guide else '')
        self.status.setVisible(bool(term.guide))

    def text(self):
        """The tutorial's existing detail inspection seam."""
        return self.body.text()

    def open_guide(self):
        if not self.isVisible() or not self.term.guide:
            return
        try:
            opened = self.controller.browser(self.term.guide)
        except Exception:
            opened = False
        # PLACEHOLDER COPY (932). Browser acceptance is only a handoff.
        self.status.setText('Opened in your browser. This explanation stays here.' if opened else
            'Could not open the web guide. You can keep using this explanation.')
        self.status.setAccessibleDescription(self.status.text())
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(self.status, self.status.text()))


class HelpController(QObject):
    def __init__(self, host, config, browser=None):
        super().__init__(host)
        self.host, self.config = host, config
        self.browser = browser or open_web_guide
        self.active = None
        self._origin = None
        self._page = None
        self._region = None
        self._installed = False
        self.layout_change = lambda: None
        self.reflowing = False

    def toggle(self, term_id, origin, card, *, page=None, region=None, session_detail=None, term=None):
        same = (self.active is card and self._origin is not None and self._origin() is origin
                and card.term.id == (term.id if term else term_id))
        self.dismiss(False)
        if same:
            origin.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return
        self.active, self._origin, self._page, self._region = card, weakref.ref(origin), page, region
        card.set_term(term or explanation(term_id, self.config(), session_detail=session_detail))
        if origin.property('contextHelpControl') and hasattr(origin, 'setChecked'):
            origin.setChecked(True)
        if region is not None:
            self.layout_change()
            region.show()
        card.show()
        app = QApplication.instance()
        app.installEventFilter(self)
        self._installed = True
        QWidget.setTabOrder(origin, card.close_button)
        last = card.close_button
        if not card.guide_button.isHidden():
            QWidget.setTabOrder(last, card.guide_button)
            last = card.guide_button
        fallback = self.fallback()
        if fallback is not None:
            QWidget.setTabOrder(last, fallback)
        card.close_button.setFocus(Qt.FocusReason.TabFocusReason)
        QAccessible.updateAccessibility(QAccessibleAnnouncementEvent(card, card.body.text()))
        QTimer.singleShot(0, self, self.ensure_visible)

    def fallback(self):
        return getattr(self.host, '_skip_step_btn', None) or getattr(self.host, '_filter', None)

    def ensure_visible(self):
        card = self.active
        if card is None or not card.isVisible():
            return
        parent = card.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                parent.ensureWidgetVisible(card.close_button)
                return
            parent = parent.parentWidget()

    def dismiss(self, restore_focus=False):
        origin = self._origin() if self._origin is not None else None
        if self._installed:
            QApplication.instance().removeEventFilter(self)
            self._installed = False
        card, region = self.active, self._region
        self.active = self._origin = self._page = self._region = None
        if card is not None and isValid(card):
            card.hide()
        if region is not None and isValid(region):
            self.layout_change()
            region.hide()
        if (origin is not None and isValid(origin) and origin.property('contextHelpControl')
                and hasattr(origin, 'setChecked')):
            origin.setChecked(False)
        if restore_focus and self.host.isVisible():
            target = origin if origin is not None and isValid(origin) and origin.isVisible() else self.fallback()
            if target is not None and isValid(target) and target.isVisible():
                target.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def eventFilter(self, watched, event):
        if self.active is None:
            return False
        if event.type() == QEvent.Type.Hide and watched in (self.host, self._page):
            self.dismiss(False)
        elif (event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Escape
              and isinstance(watched, QWidget) and (watched is self.host or self.host.isAncestorOf(watched))):
            self.dismiss(True)
            return True
        return False


def tutorial_page_help(window, key, page):
    """One construction seam, independent of core/optional tutorial page routing."""
    if not hasattr(window, '_context_help'):
        window._context_help = HelpController(window, lambda: getattr(window._app, 'config', {}) or {})
    controller = window._context_help
    if key == 'ways':
        for row, term_id in (('wake', 'wake-word'), ('hands_free', 'hands-free')):
            button, previous = window._way_details[row]
            column = previous.parentWidget().layout().itemAt(0).layout().itemAt(0).layout()
            # The detail lives in the existing text column, not a second explanation.
            column.removeWidget(previous)
            previous.setParent(None)
            previous.deleteLater()
            card = ExplanationCard(controller)
            card.set_term(explanation(term_id, controller.config(),
                session_detail=_session_detail(window) if row == 'hands_free' else None))
            column.addWidget(card)
            window._way_details[row] = button, card
            button.setAccessibleName('Explain ' + explanation(term_id).title.lower())
            button.setProperty('contextHelpControl', True)
        return page
    term_ids = {'command': ('command-reference', 'wake-word'),
                'display': ('listening-display',), 'done': ('ava',)}.get(key, ())
    content = page.widget() if isinstance(page, QScrollArea) else page
    layout = content.layout()
    for index, term_id in enumerate(term_ids):
        button, card = term_button(term_id), ExplanationCard(controller)
        layout.insertWidget(index * 2, button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.insertWidget(index * 2 + 1, card)
        button.clicked.connect(lambda _checked=False, t=term_id, b=button, c=card:
            controller.toggle(t, b, c, page=content))
    return page


def _session_detail(window):
    # Existing tutorial formatter resolves accepted runtime paste/exit words.
    from samsara.ui.tutorial_qt import _mode_detail
    return _mode_detail('hands_free', window._app, window._catalog)


def tutorial_way_help(window, key, opened):
    controller = window._context_help
    if key not in ('wake', 'hands_free'):
        controller.dismiss(False)
        return False
    button, card = window._way_details[key]
    for other, (other_button, detail) in window._way_details.items():
        if other != key:
            other_button.setChecked(False)
            detail.hide()
    term_id = 'wake-word' if key == 'wake' else 'hands-free'
    if opened:
        controller.toggle(term_id, button, card, page=card.parentWidget(),
            session_detail=_session_detail(window) if key == 'hands_free' else None)
    else:
        controller.dismiss(True)
    return True


def reference_help(window):
    controller = window._context_help = HelpController(window, lambda: (
        window._config_cb() if window._config_cb is not None else {}) or {})
    inner = window._title_bar.parentWidget()
    region = QScrollArea(inner)
    region.setWidgetResizable(True)
    region.setFrameShape(QFrame.Shape.NoFrame)
    region.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    region.setMaximumHeight(300)
    region.setProperty('contextHelpControl', True)
    card = ExplanationCard(controller)
    region.setWidget(card)
    inner.layout().insertWidget(inner.layout().indexOf(window._live_controls), region)
    region.hide()
    window._help_region, window._help_card = region, card
    controller.layout_change = lambda: _reference_reflow(window)
    title = window._title_bar
    for index, term_id in ((1, 'command-reference'), (3, 'command-packs')):
        button = term_button(term_id, compact=True)
        button.clicked.connect(lambda _checked=False, t=term_id, b=button:
            controller.toggle(t, b, card, page=window, region=region))
        title.layout().insertWidget(index, button)
        if term_id == 'command-packs':
            button.setVisible(title._packs_button.isVisibleTo(title))
        setattr(window, '_help_' + term_id.replace('-', '_'), button)
    ava = window._help_ava = term_button('ava')
    window._detail_card.layout().itemAt(window._detail_card.layout().count() - 1).layout().insertWidget(1, ava)
    ava.clicked.connect(lambda: controller.toggle('ava', ava, card, page=window, region=region))
    ava.hide()
    window._apply_width_floor()
    if not window._embedded:
        install_local_help(window, lambda: ('Commands', window))


def _reference_reflow(window):
    """The existing disclosure normally dismisses on list resize/scroll.

    Pause that policy only for the help area's layout, then place the same
    disclosure at the new list origin. User scrolling keeps its usual policy.
    """
    controller = window._context_help
    if controller.reflowing:
        return
    controller.reflowing = True
    view, disclosure = window._list, window._disclosure
    scroll = view.verticalScrollBar().value()
    view.removeEventFilter(disclosure)
    disclosure.placing = True

    def finish():
        detail = window._detail_card
        if detail.phrase and detail.isVisible():
            detail.place(detail._anchor)
        view.verticalScrollBar().setValue(scroll)
        disclosure.placing = False
        controller.reflowing = False
        view.installEventFilter(disclosure)

    QTimer.singleShot(0, controller, finish)


def reference_command_help(window, command):
    """Ava explanation appears only in details whose current metadata names Ava."""
    import re
    metadata = ' '.join(str(command.get(field, '')) for field in ('description', 'row_tags', 'pack', 'plugin', 'precondition'))
    window._help_ava.setVisible(bool(re.search(r'\bava\b', metadata, re.IGNORECASE)))
    if not window._help_ava.isHidden():
        detail = window._detail_card
        first = detail.enable_button if detail.enable_button.isVisible() else detail.run_button
        QWidget.setTabOrder(first, window._help_ava)
        QWidget.setTabOrder(window._help_ava, detail.close_button)


def open_help(app):
    """Shared recovery entry: select Help directly, including a cold window."""
    from samsara.ui.settings_qt import SettingsQt
    controller = getattr(app, '_settings_qt', None)
    if controller is None:
        controller = app._settings_qt = SettingsQt(app)
    controller.show_page('Help')
    return True


def settings_help(owner, page):
    """Extend Choose guide without changing the Help page's existing layout."""
    controller = HelpController(owner, lambda: owner.app.config)
    origin = owner._widgets['setupGuides']
    menu = origin.menu()
    menu.addSeparator()
    # PLACEHOLDER COPY (1024)
    quick = menu.addAction('Quick Reference')
    quick.setObjectName('helpQuickReference')
    quick.triggered.connect(lambda: getattr(owner.app, 'open_quick_reference', lambda: None)())
    explanations = menu.addMenu('Offline explanations')
    card = ExplanationCard(controller)
    actions = {}
    for term_id in TERMS:
        action = explanations.addAction(explanation(term_id).title)
        action.triggered.connect(lambda _=False, t=term_id:
            controller.toggle(t, origin, card, page=page))
        actions[term_id] = action
    page.column.insertWidget(page.column.indexOf(page.subtitle) + 1, card)
    page._offline_help, page._help_actions, page._help_card = controller, actions, card


class LocalPageHelp(QObject):
    """Window-local F1; keep the page, its values and the focus return target."""
    def __init__(self, host, current_page):
        super().__init__(host)
        self.host, self.current_page = host, current_page
        self.controller = HelpController(host, lambda: {})
        self.card = ExplanationCard(self.controller, host)
        self.shortcut = QShortcut(QKeySequence('F1'), host)
        self.shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.shortcut.setAutoRepeat(False)
        self.shortcut.activated.connect(self.open)

    def open(self, origin=None):
        name, page = self.current_page()
        if page is None:
            return
        # Reference already has a local card and a reflow policy that retains
        # its expanded command, filter, selection and scroll position.
        if hasattr(page, '_help_command_reference'):
            controller, card = page._context_help, page._help_card
            if controller.active is not None:
                card.close_button.setFocus()
                return
            origin = origin or QApplication.focusWidget() or page._filter
            controller.toggle('command-reference', origin, card, page=page, region=page._help_region)
            return
        if self.controller.active is not None:
            self.card.close_button.setFocus()
            return
        origin = origin or QApplication.focusWidget() or self.host
        term = page_explanation(name)
        # Prefer the focused Settings row's existing local explanation.
        ancestor = origin
        while ancestor is not None and ancestor is not self.host:
            if hasattr(ancestor, 'full_help') and hasattr(ancestor, 'title'):
                term = HelpTerm('page', ancestor.title, ancestor.full_help, None)
                break
            ancestor = ancestor.parentWidget()
        # An inline card reserves its own space: page text and title stay intact.
        # Settings replaces pages between visits, so recreate a deleted card.
        if not isValid(self.card):
            self.card = ExplanationCard(self.controller, self.host)
        content = page.widget() if isinstance(page, QScrollArea) else getattr(page, '_content', page)
        layout = content.layout()
        anchor = getattr(page, 'guide_header', None) or getattr(page, 'subtitle', None)
        index = layout.indexOf(anchor) + 1 if anchor is not None else 1
        self.card.setParent(content)
        self.card.setMaximumWidth(560)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.card.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
        layout.insertWidget(index, self.card)
        self.controller.toggle('page', origin, self.card, page=page, term=term)
        QTimer.singleShot(0, self, self.reveal_header)

    def reveal_header(self):
        """Keep the title in view when it and the new card fit together."""
        page = self.controller._page
        if self.controller.active is None or not isinstance(page, QScrollArea):
            return
        page.widget().layout().activate()
        bottom = self.card.mapTo(page.widget(), self.card.rect().bottomLeft()).y()
        if bottom < page.viewport().height():
            page.verticalScrollBar().setValue(0)


def install_local_help(host, current_page):
    if not hasattr(host, '_local_page_help'):
        host._local_page_help = LocalPageHelp(host, current_page)


def install_settings_help(owner, page):
    install_local_help(owner, lambda: (
        getattr(owner._stack.currentWidget(), 'title', None) or owner._tool_name or 'Settings',
        owner._stack.currentWidget()))
    # n08 asks for a disclosure on General; other pages keep their current design.
    if getattr(page, 'title', None) == 'General':
        from html import escape
        link = page.subtitle
        # PLACEHOLDER COPY (1024): explicit disclosure; never opens on page entry.
        link.setText(escape(link.text()) + '  <a href="page-help" style="color:'
            + theme.ACCENT + ';text-decoration:none;font-weight:600">More info</a>')
        link.setOpenExternalLinks(False)
        link.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        link.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        link.linkActivated.connect(lambda _: owner._local_page_help.open(link))
        keyboard = QShortcut(link)
        keyboard.setKeys([QKeySequence('Return'), QKeySequence('Enter')])
        keyboard.setContext(Qt.ShortcutContext.WidgetShortcut)
        keyboard.activated.connect(lambda: owner._local_page_help.open(link))


def install_hub_help(host):
    install_local_help(host, lambda: (
        next((name for name, page in host._panel_cache.items() if page is host._stack.currentWidget()), 'Home'),
        host._stack.currentWidget()))
