"""
Interactive hands-on tutorial for Samsara.

Architecture mirrors first_run_wizard_qt.py exactly:
  - Shared samsara.ui.theme stylesheet (BG0/BG1/BG2 surfaces, cyan accent, Segoe UI)
  - _WizardWindow-style class: header bar with step dots, page title,
    page stack, Back/Next nav
  - Same _padded() helper and _STEPS pattern
  - Qt-thread safety: TutorialWindow MUST be created on the Qt thread.
    Use app.show_tutorial() (calls _schedule_ui) — never construct directly
    from an audio/worker thread.

Steps:
  0  Welcome     — passive: "takes two minutes" + Let's go
  1  Dictation   — speak → text lands in box → green check
  2  Command     — say any command → green check
  3  Ways        — passive overview of the user's current capture controls
  4  Display     — try the listening display and choose its position
  5  Try         — live voice, pace and feedback trials; each card saves on Keep
  6  Done        — checklist recap, all items ticked; points to Ava and
                   the click-by-number overlay as things to try once set up

Every "try saying ..." example and every command named in the text is read
from the command catalog at runtime (samsara.command_catalog, built from the
live registry): examples prefer read-only commands, never destructive ones,
so an example can never name a command that does not exist. With no catalog
the text says so and links to Help.

Interaction detection hooks (registered on app, removed on window close):
  app._tutorial_hooks['dictation']  one-shot cb(text) after dictation
  app._tutorial_hooks['command']    one-shot cb(cmd_name) after any command
"""
from __future__ import annotations

from samsara.ui.tutorial_try_qt import _TryPage

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QLabel, QLayout, QMainWindow,
    QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from samsara import command_catalog, tutorial_capture
from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara.log import get_logger
from samsara.onboarding.guidance_facts import facts_from_config
from samsara.ui.tutorial_display_qt import tutorial_page, TITLE as PLACEMENT_TITLE
from samsara.onboarding.mode_details import detail_from_config
from samsara.support_feedback import open_support_tab
from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator

logger = get_logger(__name__)

# How long to wait before showing the "need a hand?" hint and skip link
_HINT_DELAY_MS = 20_000

# Catalog commands the done page points at, by canonical id (ids, not
# phrases: the phrase shown is whatever the catalog says today, and a
# vanished id simply drops the sentence).
_HELP_COMMAND_ID = "core_utils.what_can_i_say"
_NUMBERS_COMMAND_ID = "show_numbers.show_numbers"
#: The link target the degraded text uses; linkActivated opens Help & Support.
HELP_LINK = "samsara:help"
EXAMPLES_UNAVAILABLE = (
    f'Command examples are unavailable right now. See <a href="{HELP_LINK}">Help</a> '
    "for the command list."
)


# ---------------------------------------------------------------------------
# Catalog-driven text (pure helpers, no widgets)
# ---------------------------------------------------------------------------

def _tutorial_catalog(app):
    """Catalog records from the app's live registry, else commands_catalog.json;
    None when neither is available."""
    rows = None
    matcher = getattr(getattr(app, 'command_executor', None), '_matcher', None)
    if matcher is not None:
        try:
            rows = matcher.list_commands()
        except Exception as e:
            logger.debug(f"_tutorial_catalog: {e}")
    try:
        return command_catalog.guidance_catalog(rows)
    except Exception as e:
        logger.debug(f"_tutorial_catalog: {e}")
        return None


def _enabled_packs(config: dict):
    try:
        from samsara.command_packs import get_enabled_packs  # noqa: PLC0415
        return set(get_enabled_packs(config or {}))
    except Exception:
        return None


def _command_examples(records, config: dict, count: int = 3):
    """Canonical phrases of up to `count` example commands (read first, never
    destructive, enabled packs only), or None when there is no catalog."""
    if not records:
        return None
    picked = command_catalog.pick_examples(records, count, enabled_packs=_enabled_packs(config))
    return [command_catalog.canonical_phrase(r) for r in picked] or None


def _catalog_phrase(records, canonical_id: str):
    for record in records or []:
        if record.get("canonical_id") == canonical_id:
            return command_catalog.canonical_phrase(record)
    return None


def _command_instruction(examples, hotkey: str, wake: str, wake_enabled: bool,
                         opens_session: bool = False) -> str:
    if not examples:
        return "Say a voice command. " + EXAMPLES_UNAVAILABLE
    first, rest = examples[0], examples[1:]
    bullets = [f'  • Hold  {hotkey.upper()}  and say  "{first}"']
    if wake_enabled:
        # OWNER COPY: distinguish session-opening wake from one-command wake.
        bullets.append(
            f'  • Say "{wake}", then say "{first}" after the session opens'
            if opens_session else f'  • Say  "{wake}, {first}"  (wake word mode)'
        )
    if rest:
        bullets.append("  • Or any command you know — " + ", ".join(f"'{p}'" for p in rest) + ", etc.")
    return "Say a voice command. Try one of:\n" + "\n".join(bullets)


def _command_hint(examples) -> str:
    if not examples:
        return EXAMPLES_UNAVAILABLE
    quoted = [f"'{p}'" for p in examples]
    joined = quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + ", or " + quoted[-1]
    return f"Try saying {joined}."


def _more_text(records) -> str:
    phrase = _catalog_phrase(records, _HELP_COMMAND_ID)
    if phrase:
        # OWNER COPY: command voice help requires a capture route.
        return (f'Say "{phrase}" while a command route is listening, or open '
                "the Command Reference from the tray menu.")
    return "Open the Command Reference from the tray menu for the full command list."


def _pointer_text(records) -> str:
    phrase = _catalog_phrase(records, _NUMBERS_COMMAND_ID)
    # OWNER COPY: provider and numbered targets vary with readiness and scope.
    if phrase:
        return (f'Ava availability follows Settings > Ava. "{phrase}" labels exposed '
                "controls when Show Numbers is available.")
    return "Ava availability follows Settings > Ava."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _dictation_instruction(hotkey: str, mode: str,
                           continuous_commit_trigger: str = "silence") -> str:
    """Audit row 335: the wording must follow the configured `mode`."""
    key = (hotkey or 'ctrl+shift').upper()
    if mode == 'toggle':
        return (f"Press  {key}  once to start, say anything — 'hello world' works fine — "
                f"then press it again to transcribe. Text will appear in the box below.")
    if mode == 'continuous':
        if continuous_commit_trigger == 'key':
            # OWNER COPY: name the configured manual commit key.
            return (f"Press {key} to start continuous dictation. "
                    "Use the configured commit key to deliver text; press the record key again to stop.")
        return (f"Press  {key}  to start continuous dictation and say anything — 'hello world' "
                f"works fine. Each pause is transcribed on its own; press it again to stop. "
                f"Text will appear in the box below.")
    return (f"Hold  {key}  and say anything — 'hello world' works fine. "
            f"Release to transcribe. Text will appear in the box below.")


def _command_count(app):
    """Audit row 403: never a literal. The live registry when there is an app;
    otherwise the same loader tools/dump_command_metadata.py uses (no
    dictation.py import). None when neither is available."""
    matcher = getattr(getattr(app, 'command_executor', None), '_matcher', None)
    if matcher is not None:
        try:
            return len(matcher.list_commands())
        except Exception:
            pass
    try:
        from tools.dump_command_metadata import build_dump  # noqa: PLC0415
        return int(build_dump()['summary']['commands'])
    except Exception:
        return None


def _commands_banner_text(count) -> str:
    how_many = f"There are {count} of them" if count else "There are hundreds of them"
    return (f"Commands do things. {how_many} — scroll, open apps, "
            "manage windows, type shortcuts, and more.")


def _hands_free_text(config: dict, *, recovery_only: bool = False) -> str:
    """Audit row 165: the session, its commit/undo words, undo and cancel keys."""
    from samsara import session_modes  # noqa: PLC0415
    from samsara.ui.quick_reference_qt import _HOTKEY_FALLBACKS, _pretty_button, _pretty_key_combo  # noqa: PLC0415
    cm = config.get('command_mode') or {}
    button = _pretty_button(cm.get('button', 'rctrl')).replace(' (default)', '')
    undo = _pretty_key_combo(config.get('undo_hotkey', _HOTKEY_FALLBACKS['undo_hotkey']))
    cancel = _pretty_key_combo(config.get('cancel_hotkey', _HOTKEY_FALLBACKS['cancel_hotkey']))
    if recovery_only:
        # PLACEHOLDER COPY (378)
        spoken_undo = (f'Say "{session_modes.SCRATCH_THAT_PHRASE}" to undo a thought. '
                       if cm.get('enabled', False) else '')
        return (f"Hands-free undo: {spoken_undo}Undo the last dictation with {undo}; "
                f"cancel a recording with {cancel}.")
    state = '' if cm.get('enabled', False) else ' (turn it on in Settings -> Modes)'
    action = "tap" if cm.get('mode', 'hold') == 'toggle' else "hold"
    return (f"Hands-free{state}: {action} {button} to start a session, say "
            f"\"{_paste_word(config)}\" to paste a thought and "
            f"\"{session_modes.SCRATCH_THAT_PHRASE}\" to undo it. Undo the last dictation with {undo}; "
            f"cancel a recording with {cancel}.")


def _paste_word(config: dict) -> str:
    """Valid/missing values use guidance facts; rejected overrides keep runtime word."""
    from samsara import session_modes
    cm = config.get('command_mode') or {}
    if 'dictate_commit_word' in cm:
        candidate = session_modes.normalize_utterance(cm['dictate_commit_word'] or '')
        if len(candidate.split()) != 1:
            # set_commit_phrase refuses this override and keeps the installed word.
            return session_modes.DICTATE_COMMIT_PHRASE
    return facts_from_config(config).session_end.commit_word


def _mode_detail(mode_id: str, app, records) -> str:
    from samsara.ui.quick_reference_qt import _resolve_session_phrases
    cfg = getattr(app, 'config', None) or {}
    session = _resolve_session_phrases(app)
    phrase = _catalog_phrase(records, _HELP_COMMAND_ID)
    configured_exits = (cfg.get('command_mode') or {}).get('abort_phrases') or []
    if isinstance(configured_exits, str):
        configured_exits = [configured_exits]
    exits = [p.strip() for p in configured_exits if isinstance(p, str) and p.strip()]
    return detail_from_config(
        mode_id, cfg, paste_word=_paste_word(cfg), help_phrase=phrase,
        exit_phrase=(exits or session['sleep']['words'])[0],
    )


def _ways_rows(app) -> list[dict]:
    """Use Quick Reference's resolved controls, phrases and feature states."""
    from samsara.ui.home_qt import wake_phrase
    from samsara.ui.quick_reference_qt import (
        _DICTATE_MODE_LABELS, _resolve_hotkeys, _resolve_session_phrases,
    )

    controls = _resolve_hotkeys(app)
    session = _resolve_session_phrases(app)
    # These are the shared resolver's ordered rows, not a second key table.
    main, continuous, wake, commands, _cancel, hands_free = controls[:6]
    mode = next((key for key, label in _DICTATE_MODE_LABELS.items()
                 if label == main['label']), None)
    hf = session['hands_free_toggle']
    # PLACEHOLDER COPY (378): owner supplies final wording.
    main_name, main_start = {
        'hold': ('Hold to talk', f"Hold {main['value']}; release to finish."),
        'toggle': ('Tap to talk', f"Tap {main['value']} to start; tap again to finish."),
        'continuous': ('Continuous on the main key', f"Tap {main['value']} to start or stop."),
    }.get(mode, (main['label'], main['value']))
    return [
        dict(key='main', name=main_name, start=main_start,
             purpose='Speak to turn your words into text.', enabled=main['enabled']),
        dict(key='continuous', name='Continuous',
             start=f"Tap {continuous['value']} to start or stop.",
             purpose='Keep dictating without holding a key.', enabled=continuous['enabled']),
        dict(key='commands', name='Commands only', start=f"Hold {commands['value']} while speaking.",
             purpose='Speak commands to control your computer.', enabled=commands['enabled']),
        dict(key='wake', name='Wake word',
             start=f'Say "{wake_phrase(getattr(app, "config", None) or {})}"; tap {wake["value"]} to toggle the listener.',
             purpose='Start speaking by saying your wake phrase.', enabled=session['wake']['enabled']),
        dict(key='hands_free', name='Hands-free',
             start=(f'{"Tap" if hf["mode"] == "toggle" else "Hold"} {hands_free["value"]}; '
                    f'say "{_paste_word(getattr(app, "config", None) or {})}" to paste a thought.'),
             purpose='Dictate thoughts and speak commands in a voice session.', enabled=hands_free['enabled']),
    ]


def show_tutorial(app) -> None:
    """Create and display the tutorial window on the Qt thread.

    Call this from dictation.py via _schedule_ui so the window is
    always constructed on the correct thread.
    """
    win = TutorialWindow(app)
    win.show()
    qt_app = QApplication.instance()
    if qt_app:
        screen = qt_app.primaryScreen().availableGeometry()
        win.move(
            screen.center().x() - win.width() // 2,
            screen.center().y() - win.height() // 2,
        )


# ---------------------------------------------------------------------------
# Tutorial window
# ---------------------------------------------------------------------------

class _ModeDetailButton(QPushButton):
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.click()
            event.accept()
            return
        super().keyPressEvent(event)


class TutorialWindow(QMainWindow):
    """Interactive post-wizard tutorial.

    Created on Qt thread. Registers lightweight one-shot hooks on `app`
    for dictation and command detection; removes them on close.
    """

    # Signal for cross-thread UI updates (audio/worker → Qt thread)
    _step_signal = Signal(str, str)   # (event_name, payload)

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._step = 0
        self._completed: set[int] = set()
        self._skipped_steps: set[int] = set()
        self._hint_timer: QTimer | None = None

        self._step_signal.connect(self._on_step_signal)

        self._build_step_list()

        self.setWindowTitle("Samsara Tutorial")
        # Keep ordinary steps compact. The done page expands to its content
        # when the available screen height permits; otherwise its scroll area
        # keeps the text and guide actions at their natural sizes.
        self._default_window_height = 722
        self.setMinimumSize(620, 480)
        self.resize(620, self._default_window_height)
        self.setStyleSheet(theme.build_stylesheet())

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---- Header bar: step counter + dots --------------------------------
        header = QWidget()
        header.setStyleSheet(f"background:{theme.BG0};")
        header.setFixedHeight(theme.SPACE_3)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(28, 0, 28, 0)

        self._step_lbl = QLabel()
        self._step_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        hl.addWidget(self._step_lbl)
        hl.addStretch()

        root.addWidget(header)
        self._step_indicator = StepIndicator([key.capitalize() for key, _ in self._steps])
        root.addWidget(self._step_indicator)
        root.addSpacing(12)

        root.addWidget(self._separator())

        # ---- Page title -----------------------------------------------------
        title_bar = QWidget()
        title_bar.setMinimumHeight(48)
        tbl = QVBoxLayout(title_bar)
        tbl.setContentsMargins(28, 10, 28, 4)
        self._title_lbl = QLabel()
        self._title_lbl.setWordWrap(True)
        self._title_lbl.setStyleSheet(
            f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_TITLE}px;font-weight:bold;"
        )
        tbl.addWidget(self._title_lbl)
        root.addWidget(title_bar)

        # ---- Page stack -----------------------------------------------------
        self._stack = QWidget()
        self._stack_layout = QVBoxLayout(self._stack)
        self._stack_layout.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self._stack, stretch=1)

        # Build pages
        self._dictation_box: QTextEdit | None = None
        self._scroll_area:   QScrollArea | None = None
        self._pages = []
        for key, title in list(self._steps):
            try:
                self._pages.append(self._scroll_page(self._build_page(key)))
            except Exception:
                if key != 'try':
                    raise
                logger.exception('Tutorial Try page unavailable; skipping that step')
                self._try_page = None
                step_index = self._steps.index((key, title))
                self._steps.remove((key, title))
                self._step_indicator.steps.remove(key.capitalize())
                self._step_indicator.takeItem(step_index)
        self._current_page: QWidget | None = None

        root.addWidget(self._separator())

        # ---- Nav bar --------------------------------------------------------
        nav = QWidget()
        nav.setMinimumHeight(theme.HIT_TARGET_MIN + 2 * theme.SPACE_1)
        theme.style_footer(nav)
        nav.setStyleSheet(nav.styleSheet() + 'border-top:none;')
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(28, theme.SPACE_1, 28, theme.SPACE_1)
        nl.setSpacing(theme.SPACE_3)

        self._skip_all_btn = QPushButton("Skip tutorial")
        theme.make_secondary(self._skip_all_btn)
        self._skip_all_btn.clicked.connect(self._skip_tutorial)
        nl.addWidget(self._skip_all_btn)

        nl.addStretch()

        self._skip_step_btn = QPushButton("Skip this step")
        theme.make_secondary(self._skip_step_btn)
        self._skip_step_btn.clicked.connect(self._skip_step)
        nl.addWidget(self._skip_step_btn)
        # Measured after addWidget() parents the button into the styled
        # tree -- fontMetrics() on an unparented widget doesn't reflect the
        # cascaded stylesheet font size yet.
        self._skip_step_btn.setMinimumWidth(
            self._button_min_width(self._skip_step_btn, ["Skip this step"])
        )

        # Back sits between "Skip this step" and "Next" -- same secondary
        # styling + repolish pattern as every other secondary button here
        # (theme.make_secondary), and disabled (not hidden) on step 1,
        # matching first_run_wizard_qt.py's _back_btn convention exactly.
        self._back_btn = QPushButton("Back")
        theme.make_secondary(self._back_btn)
        self._back_btn.clicked.connect(self._go_back)
        nl.addWidget(self._back_btn)
        self._back_btn.setMinimumWidth(
            self._button_min_width(self._back_btn, ["Back"])
        )

        self._next_btn = QPushButton("Next")
        theme.make_primary(self._next_btn)
        self._next_btn.clicked.connect(self._go_next)
        nl.addWidget(self._next_btn)
        # Minimum, not fixed -- the label changes per step ("Let's go" /
        # "Next" / "Start using Samsara") and must never clip the widest one.
        self._next_btn.setMinimumWidth(self._button_min_width(self._next_btn, ["Next"]))

        root.addWidget(nav)

        theme.apply_design_scale(self)
        theme.make_secondary(self._skip_all_btn)
        theme.make_secondary(self._skip_step_btn)
        for button in (self._skip_all_btn, self._skip_step_btn, self._back_btn, self._next_btn):
            button.setStyleSheet(button.styleSheet() +
                                f'QPushButton{{min-height:0px;padding:0 {theme.SPACE_2}px;}}')
            button.setFixedHeight(theme.HIT_TARGET_MIN)
        # The shared design pass reapplies button minimums; these compact
        # detail controls retain an exact 44px target including their border.
        for button, _detail in self._way_details.values():
            button.setStyleSheet(button.styleSheet() +
                                'QPushButton{min-height:42px;min-width:42px;'
                                'max-height:42px;max-width:42px;padding:0px;}')
            button.setFixedSize(44, 44)
        self._show_step()

    # ------------------------------------------------------------------
    # Sizing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _separator():
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(28, 0, 28, 0)
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{theme.BORDER_FAINT};border:none;")
        layout.addWidget(line)
        return container

    def showEvent(self, event):
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not getattr(self, '_screen_fit_connected', False):
            handle.screenChanged.connect(self._fit_available_screen)
            self._screen_fit_connected = True
        self._fit_available_screen()

    def _fit_available_screen(self, *_):
        screen = self.screen()
        if screen is None:
            return
        if screen is not getattr(self, '_fit_screen', None):
            previous = getattr(self, '_fit_screen', None)
            if previous is not None and hasattr(previous, 'availableGeometryChanged'):
                previous.availableGeometryChanged.disconnect(self._fit_available_screen)
            if hasattr(screen, 'availableGeometryChanged'):
                screen.availableGeometryChanged.connect(self._fit_available_screen)
            self._fit_screen = screen
        area = screen.availableGeometry()
        handle = self.windowHandle()
        margins = handle.frameMargins() if handle is not None else None
        left, top, right, bottom = ((margins.left(), margins.top(), margins.right(), margins.bottom())
                                    if margins is not None else (0, 0, 0, 0))
        width = max(1, area.width() - left - right)
        height = max(1, area.height() - top - bottom)
        self.setMinimumSize(min(620, width), min(480, height))
        self.setMaximumSize(width, height)
        self.resize(min(self.width(), width), min(self.height(), height))
        frame = self.frameGeometry()
        self.move(max(area.left(), min(frame.left(), area.right() - frame.width() + 1)),
                  max(area.top(), min(frame.top(), area.bottom() - frame.height() + 1)))

    def _scroll_page(self, page):
        if isinstance(page, QScrollArea):
            return page
        scroll = QScrollArea(self._stack)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if page.layout() is not None:
            page.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll.setWidget(page)
        scroll.hide()
        return scroll

    @staticmethod
    def _button_min_width(btn: QPushButton, texts: list[str], h_padding: int = 48) -> int:
        """Minimum width that fits the widest of `texts` in btn's actual
        font, plus theme button padding (24px each side). Used instead of
        setFixedWidth() so buttons never clip text the theme's font metrics
        don't fit in an old, pre-theme pixel count."""
        fm = btn.fontMetrics()
        widest = max((fm.horizontalAdvance(t) for t in texts), default=0)
        return widest + h_padding

    def _fit_done_page_height(self) -> None:
        page = self._current_page
        if not isinstance(page, QScrollArea):
            return
        content = page.widget()
        layout = content.layout() if content is not None else None
        if content is None or layout is None:
            return
        layout.activate()
        if self._steps[self._step][0] in ('ways', 'try', 'done'):
            if (self._steps[self._step][0] == 'try' or
                    (self._steps[self._step][0] == 'done' and self.maximumWidth() >= 1000)):
                # Widen Done where two cards fit comfortably; smaller screens
                # retain the user's pre-Try width and can scroll internally.
                self.resize(min(self.maximumWidth(), max(self.width(), 1000)), self.height())
                self._fit_available_screen()
                content.resize(page.viewport().width(), content.height())
                layout.activate()
            natural_height = max(content.minimumSizeHint().height(), layout.minimumSize().height())
            if self._steps[self._step][0] == 'try' and layout.hasHeightForWidth():
                natural_height = max(natural_height, layout.heightForWidth(page.viewport().width()))
            chrome = self.height() - page.viewport().height()
            self.resize(self.width(), min(self.maximumHeight(),
                        max(self._default_window_height, chrome + natural_height)))
            self._fit_available_screen()
            return
        natural_height = max(
            content.height(), content.minimumSizeHint().height(),
            layout.sizeHint().height(),
        )
        extra = max(0, natural_height - page.viewport().height())
        if extra:
            self.resize(self.width(), min(self.maximumHeight(), self.height() + extra))

    # ------------------------------------------------------------------
    # Step configuration
    # ------------------------------------------------------------------

    def _build_step_list(self):
        self._steps: list[tuple[str, str]] = [
            ("welcome",   "Welcome to Samsara"),
            ("dictation", "Talk → It Types"),
            ("command",   "Say a Command"),
            # PLACEHOLDER COPY (378)
            ("ways",      "Ways to talk to Samsara"),
            ("display",   PLACEMENT_TITLE),  # PLACEHOLDER COPY (490)
            ("try",       "Try your settings"),
            ("done",      "Tutorial complete"),
        ]

    # ------------------------------------------------------------------
    # Page builders
    # ------------------------------------------------------------------

    def _padded(self) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 16, 28, 16)
        lay.setSpacing(12)
        return w, lay

    def _link_help(self, label) -> None:
        """Degraded text carries a Help link: open Settings on Help & Support."""
        if label is None or HELP_LINK not in label.text():
            return
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.linkActivated.connect(lambda _href: open_support_tab(self._app))

    def _instruction_box(self, text: str) -> QFrame:
        """Tinted instruction card used on interactive steps."""
        frame = QFrame()
        theme.style_instruction_box(frame)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(16, 12, 16, 12)
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
        fl.addWidget(lbl)
        return frame

    def _success_banner(self, text: str) -> QFrame:
        frame = QFrame()
        theme.style_success_banner(frame)
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(14, 10, 14, 10)
        fl.setSpacing(10)
        check = QLabel("✓")
        check.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_DISPLAY}px;font-weight:bold;")
        fl.addWidget(check, alignment=Qt.AlignmentFlag.AlignVCenter)
        msg = QLabel(text)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
        fl.addWidget(msg, stretch=1)
        return frame

    def _build_page(self, key: str) -> QWidget:
        builders = {
            "welcome":   self._build_welcome,
            "dictation": self._build_dictation,
            "command":   self._build_command,
            "ways":      self._build_ways,
            "display":   lambda: tutorial_page(self),
            "try":       lambda: _TryPage(self),
            "done":      self._build_done,
        }
        return builders[key]()

    def _build_ways(self) -> QWidget:
        content, lay = self._padded()
        lay.setContentsMargins(28, 0, 28, 0)
        lay.setSpacing(theme.SPACE_SCALE[1])
        self._way_details = {}
        cfg = getattr(self._app, 'config', None) or {}
        mode_ids = {'main': {'hold': 'hold', 'toggle': 'tap', 'continuous': 'continuous'}.get(cfg.get('mode', 'hold'), 'hold'),
                    'continuous': 'continuous', 'commands': 'commands_only',
                    'wake': 'wake_word', 'hands_free': 'hands_free'}
        for row in _ways_rows(self._app):
            frame = self._instruction_box('')
            frame.setObjectName(f'tutorialWay_{row["key"]}')
            labels = frame.layout()
            labels.setContentsMargins(12, 12, 12, 12)
            labels.setSpacing(0)
            title = labels.itemAt(0).widget()
            title.setText(row['name'])
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;")
            labels.removeWidget(title)
            card_row = QHBoxLayout()
            card_row.setSpacing(12)
            text_column = QVBoxLayout()
            text_column.setSpacing(4)
            text_column.addWidget(title)
            card_row.addLayout(text_column, stretch=1)
            button = _ModeDetailButton('?')
            button.setObjectName(f'tutorialWayHelp_{row["key"]}')
            button.setAccessibleName(f'More about {row["name"]}')
            button.setCheckable(True)
            button.setMinimumSize(44, 44)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            theme.make_secondary(button)
            button.setFixedSize(44, 44)
            card_row.addWidget(button, alignment=Qt.AlignmentFlag.AlignVCenter)
            labels.addLayout(card_row)
            # PLACEHOLDER COPY (378)
            off = '\nOff — turn it on in Settings -> Dictation.' if not row['enabled'] else ''
            detail = QLabel(row['start'] + '\n' + row['purpose'] + off)
            detail.setTextFormat(Qt.TextFormat.PlainText)
            detail.setWordWrap(True)
            detail.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
            text_column.addWidget(detail)
            expanded = QLabel(_mode_detail(mode_ids[row['key']], self._app, self._catalog))
            expanded.setObjectName(f'tutorialWayDetail_{row["key"]}')
            expanded.setTextFormat(Qt.TextFormat.PlainText)
            expanded.setWordWrap(True)
            expanded.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
            expanded.hide()
            text_column.addWidget(expanded)
            self._way_details[row['key']] = (button, expanded)
            button.clicked.connect(lambda checked=False, key=row['key']: self._toggle_way_detail(key, checked))
            lay.addWidget(frame)
        lay.addStretch()
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        scroll = QScrollArea()
        scroll.setObjectName('tutorialWaysScrollArea')
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def _toggle_way_detail(self, key, opened):
        for row_key, (button, detail) in self._way_details.items():
            active = row_key == key and opened
            button.setChecked(active)
            detail.setVisible(active)
        QTimer.singleShot(0, self, self._fit_done_page_height)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self._steps[self._step][0] == 'ways':
            self._toggle_way_detail(None, False)
            event.accept()
            return
        super().keyPressEvent(event)

    def _build_welcome(self) -> QWidget:
        w, lay = self._padded()

        headline = QLabel(
            "Samsara lets you control your computer and type with your voice."
        )
        headline.setWordWrap(True)
        headline.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_HEADING}px;")
        lay.addWidget(headline)

        sub = QLabel(
            "Let's try each thing once — it takes about two minutes. "
            "Every step is skippable."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(sub)
        lay.addSpacing(8)

        things_frame = QFrame()
        theme.style_card(things_frame)
        tf = QVBoxLayout(things_frame)
        tf.setContentsMargins(20, 16, 20, 16)
        tf.setSpacing(10)
        for icon, label in [
            ("🎙", "Talk → text appears wherever you're typing"),
            ("⚡", "Say a command → it happens"),
        ]:
            row = QHBoxLayout()
            i_lbl = QLabel(icon)
            i_lbl.setMinimumWidth(28)
            i_lbl.setStyleSheet(f"font-size:{theme.TYPE_HEADING}px;")
            row.addWidget(i_lbl)
            t_lbl = QLabel(label)
            t_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;")
            row.addWidget(t_lbl)
            row.addStretch()
            tf.addLayout(row)
        lay.addWidget(things_frame)
        lay.addStretch()
        return w

    def _build_dictation(self) -> QWidget:
        w, lay = self._padded()

        cfg = self._app.config if hasattr(self._app, 'config') else {}
        hotkey = cfg.get('hotkey', 'ctrl+shift')
        instruction = self._instruction_box(
            _dictation_instruction(hotkey, cfg.get('mode', 'hold'),
                                   cfg.get('continuous_commit_trigger', 'silence'))
        )
        theme.style_card(instruction)
        instruction.setStyleSheet(
            f'QFrame[class="card"]{{border-radius:{theme.CONTROL_RADIUS}px;}}')
        lay.addWidget(instruction)

        self._dictation_box = QTextEdit()
        self._dictation_box.setPlaceholderText("Your words will appear here…")
        self._dictation_box.document().setDocumentMargin(0)
        self._dictation_box.setReadOnly(False)
        self._dictation_box.setFixedHeight(80)
        self._dictation_box.setAccessibleName("Dictation result box")
        self._dictation_box.setStyleSheet(
            f"QTextEdit{{background:{theme.BG1};border:1px solid {theme.BORDER};"
            f"border-radius:{theme.CONTROL_RADIUS}px;padding:{theme.SPACE_2}px;}}")
        self._dictation_box.textChanged.connect(self._dictation_text_changed)
        lay.addWidget(self._dictation_box)

        # Success banner (hidden until success)
        self._dict_success = self._success_banner(
            # OWNER COPY: qualify editable targets and blocked windows.
            "Dictation practice complete. Text goes to the active editable target."
        )
        self._dict_success.setVisible(False)
        lay.addWidget(self._dict_success)

        # Hint label (shown after _HINT_DELAY_MS with no success)
        self._dict_hint = QLabel(
            f"Hotkey not working? Make sure Samsara is running and {hotkey.upper()} "
            f"is your configured record key."
        )
        self._dict_hint.setWordWrap(True)
        self._dict_hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        self._dict_hint.setVisible(False)
        lay.addWidget(self._dict_hint)

        lay.addStretch()
        return w

    def _build_command(self) -> QWidget:
        w, lay = self._padded()

        cfg = self._app.config if hasattr(self._app, 'config') else {}
        cmd_hotkey = cfg.get('command_hotkey', 'ctrl+alt+c')
        wake = cfg.get('wake_word_config', {}).get('phrase', DEFAULT_WAKE_PHRASE)

        self._catalog = _tutorial_catalog(self._app)
        self._examples = _command_examples(self._catalog, cfg)
        box = self._instruction_box(_command_instruction(
            self._examples, cmd_hotkey, wake, bool(cfg.get('wake_word_enabled', False)),
            bool((cfg.get('wake_word_config') or {}).get('opens_session', False))))
        theme.style_card(box)
        box.setStyleSheet(f'QFrame[class="card"]{{border-radius:{theme.CONTROL_RADIUS}px;}}')
        self._link_help(box.findChild(QLabel))
        lay.addWidget(box)

        # Scrollable demonstration area
        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll_area.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        demo = QFrame()
        theme.style_card(demo)
        demo.setStyleSheet(f'QFrame[class="card"]{{border-radius:{theme.CONTROL_RADIUS}px;}}')
        demo.setFixedHeight(120)
        demo_layout = QVBoxLayout(demo)
        demo_layout.setContentsMargins(theme.SPACE_2, theme.SPACE_3, theme.SPACE_2, theme.SPACE_3)
        demo_layout.addWidget(self._scroll_area)
        inner = QWidget()
        inner.setStyleSheet(f"background:{theme.BG1};")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 0, 0)
        inner_lay.setSpacing(4)
        for i in range(1, 15):
            lbl = QLabel(f"Line {i} — more of this area continues below")
            lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            inner_lay.addWidget(lbl)
        self._scroll_area.setWidget(inner)
        lay.addWidget(demo)

        self._cmd_success = self._success_banner(_commands_banner_text(_command_count(self._app)))
        self._cmd_success.setVisible(False)
        lay.addWidget(self._cmd_success)

        self._cmd_hint = QLabel(_command_hint(self._examples))
        self._link_help(self._cmd_hint)
        self._cmd_hint.setWordWrap(True)
        self._cmd_hint.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        self._cmd_hint.setVisible(False)
        lay.addWidget(self._cmd_hint)

        lay.addStretch()
        return w

    def _build_done(self) -> QWidget:
        content, lay = self._padded()
        lay.setContentsMargins(28, 8, 28, 8)
        lay.setSpacing(6)

        checklist_items = [
            (1, "✓  Dictated text — talk, it types"),
            (2, "✓  Ran a command — voice controls your computer"),
        ]

        card = QFrame()
        theme.style_card(card)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 8, 14, 8)
        cl.setSpacing(4)

        self._done_checks = {}
        for step_idx, text in checklist_items:
            row = QHBoxLayout()
            icon_lbl = QLabel()
            icon_lbl.setObjectName(f"tutorialDoneCheck{step_idx}")
            completed = step_idx in self._completed
            icon_lbl.setText("✓" if completed else "—")
            icon_lbl.setStyleSheet(
                f"color:{theme.ACCENT if completed else theme.TEXT_DISABLED};"
                f"font-size:{theme.TYPE_HEADING}px;font-weight:bold;"
            )
            icon_lbl.setMinimumWidth(24)
            icon_lbl.setMinimumHeight(24)
            row.addWidget(icon_lbl)
            text_lbl = QLabel(text[2:] if text.startswith("✓  ") else text)
            text_lbl.setWordWrap(True)
            text_lbl.setStyleSheet(
                f"color:{theme.TEXT_PRIMARY if completed else theme.TEXT_DISABLED};"
                f"font-size:{theme.TYPE_BODY}px;"
            )
            text_lbl.setMinimumHeight(24)
            row.addWidget(text_lbl, stretch=1, alignment=Qt.AlignmentFlag.AlignVCenter)
            cl.addLayout(row)
            self._done_checks[step_idx] = (icon_lbl, text_lbl)

        recap = QHBoxLayout()
        recap.setSpacing(12)
        recap.addWidget(card, stretch=1)
        self._try_summary = QLabel('')
        self._try_summary.setTextFormat(Qt.TextFormat.PlainText)
        self._try_summary.setWordWrap(True)
        self._try_summary.setStyleSheet(f'color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;')
        recap.addWidget(self._try_summary, stretch=1)
        lay.addLayout(recap)

        records = getattr(self, '_catalog', None)
        if records is None:
            records = _tutorial_catalog(self._app)
        more_lbl = QLabel(_more_text(records))
        more_lbl.setWordWrap(True)
        more_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(more_lbl)

        pointer_lbl = QLabel(_pointer_text(records))
        pointer_lbl.setWordWrap(True)
        pointer_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(pointer_lbl)
        hf_lbl = QLabel(_hands_free_text(
            self._app.config if hasattr(self._app, 'config') else {}, recovery_only=True,
        ))
        hf_lbl.setWordWrap(True)
        hf_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(hf_lbl)

        # --- Advanced guides ---
        next_lbl = QLabel("Go deeper when you're ready:")
        next_lbl.setWordWrap(True)
        next_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;")
        # Shares the header row: the window is fixed-height and a row of its
        # own squeezes the checklist card above.
        next_row = QHBoxLayout()
        next_row.addWidget(next_lbl, stretch=1)
        self._support_btn = QPushButton("Something wrong? Tell me")
        self._support_btn.setObjectName("tutorialSupportButton")
        theme.make_secondary(self._support_btn)
        self._support_btn.clicked.connect(lambda checked=False: open_support_tab(self._app))
        next_row.addWidget(self._support_btn)
        lay.addLayout(next_row)
        self._support_btn.setMinimumWidth(
            self._button_min_width(self._support_btn, ["Something wrong? Tell me"])
        )
        lay.addSpacing(4)

        _GUIDES = [
            ("Mic Setup Guide",
             "Fine-tune your microphone for the best accuracy.",
             "open_mic_setup_guide"),
            ("Ava Setup Guide",
             "Install Ollama and set up your on-device AI assistant.",
             "open_ava_guide"),
        ]

        guides = QHBoxLayout()
        guides.setSpacing(12)
        lay.addLayout(guides)
        for title, desc, method in _GUIDES:
            guide_card = QFrame()
            theme.style_guide_card(guide_card)
            guide_card.setObjectName(method)
            gc_lay = QHBoxLayout(guide_card)
            gc_lay.setContentsMargins(14, 10, 12, 10)
            gc_lay.setSpacing(10)

            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            title_lbl = QLabel(title)
            title_lbl.setWordWrap(True)
            title_lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;")
            desc_lbl = QLabel(desc)
            desc_lbl.setWordWrap(True)
            desc_lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
            text_col.addWidget(title_lbl)
            text_col.addWidget(desc_lbl)
            gc_lay.addLayout(text_col, stretch=1)

            open_btn = QPushButton("Open →")
            theme.make_secondary(open_btn)
            open_btn.setMinimumHeight(theme.HIT_TARGET_MIN)
            _m = method  # capture for lambda
            open_btn.clicked.connect(
                lambda checked=False, m=_m: self._open_guide(m)
            )
            gc_lay.addWidget(open_btn, alignment=Qt.AlignmentFlag.AlignVCenter)
            # Measured after addWidget() parents the button -- see nav bar
            # buttons above for why this ordering matters.
            open_btn.setMinimumWidth(self._button_min_width(open_btn, ["Open →"]))

            guides.addWidget(guide_card, stretch=1)

        lay.addStretch()
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        scroll = QScrollArea()
        scroll.setObjectName("tutorialDoneScrollArea")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidgetResizable(True)
        content.setObjectName("tutorialDoneContent")
        scroll.setWidget(content)
        return scroll

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _show_step(self):
        # Cancel any running hint timer from the previous step
        self._cancel_hint_timer()
        self._remove_all_hooks()

        # Restore the user's pre-Try width and keep all frame edges inside the work area.
        key = self._steps[self._step][0]
        previous_try = self._current_page is not None and self._current_page is getattr(self, '_try_page', None)
        if key == 'try' and not previous_try:
            self._try_previous_width = self.width()
        if previous_try and key != 'try':
            self.resize(getattr(self, '_try_previous_width', 620), self.height())
            self._fit_available_screen()
        if key == 'try' or previous_try:
            self._next_btn.setEnabled(True)
            self._next_btn.setFocus()
        # Swap page
        page = self._pages[self._step]
        if self._current_page is not None:
            self._stack_layout.removeWidget(self._current_page)
            self._current_page.hide()
        self._stack_layout.addWidget(page)
        page.show()
        self._current_page = page

        key, title = self._steps[self._step]
        self._step_lbl.setText(f"Step {self._step + 1} of {len(self._steps)}")
        self._title_lbl.setText(title)

        self._step_indicator.set_progress(self._step, self._skipped_steps)

        is_last    = self._step == len(self._steps) - 1
        is_welcome = key == "welcome"
        is_done    = key == "done"
        is_interactive = key in ("dictation", "command")

        # Nav button states
        self._back_btn.setEnabled(self._step > 0)
        self._skip_step_btn.setVisible(is_interactive)
        self._next_btn.setVisible(True)

        if is_done:
            self._refresh_done_checks()
            self._next_btn.setText("Start using Samsara")
            self._next_btn.setEnabled(True)
            self._skip_all_btn.setVisible(False)
        elif is_welcome:
            self._next_btn.setText("Let's go")
            self._next_btn.setEnabled(True)
        else:
            already_done = self._step in self._completed
            self._next_btn.setText("Next")
            self._next_btn.setEnabled(already_done or not is_interactive)

        # Register hook for interactive steps
        self._next_btn.setMinimumWidth(self._button_min_width(self._next_btn, [self._next_btn.text()]))
        if is_interactive and (key == "dictation" or self._step not in self._completed):
            self._install_hook(key)
            self._start_hint_timer(key)

        # Focus the dictation box so the hotkey routes text to it
        if key == "dictation" and self._dictation_box:
            QTimer.singleShot(100, self, lambda: self._dictation_box.setFocus())
        if isinstance(page, QScrollArea):
            if key != 'done':
                self.resize(self.width(), min(self._default_window_height, self.maximumHeight()))
            if key in ('ways', 'try', 'done'):
                QTimer.singleShot(0, self, self._fit_done_page_height)
        else:
            self.resize(self.width(), min(self._default_window_height, self.maximumHeight()))

    def _open_guide(self, method):
        from samsara.ui.tutorial_guides import open_guide
        open_guide(self, method)

    def _refresh_done_checks(self):
        self._try_summary.setText('\n'.join(getattr(getattr(self, '_try_page', None), 'kept', {}).values()))
        self._try_summary.setVisible(bool(self._try_summary.text()))
        for step_idx, (icon, label) in self._done_checks.items():
            completed = step_idx in self._completed
            icon.setText("✓" if completed else "—")
            icon.setAccessibleName("Completed" if completed else "Skipped")
            icon.setStyleSheet(
                f"color:{theme.ACCENT if completed else theme.TEXT_DISABLED};"
                f"font-size:{theme.TYPE_HEADING}px;font-weight:bold;"
            )
            label.setStyleSheet(
                f"color:{theme.TEXT_PRIMARY if completed else theme.TEXT_DISABLED};"
                f"font-size:{theme.TYPE_BODY}px;"
            )

    def _go_next(self):
        key = self._steps[self._step][0]
        if key == 'try' and getattr(self, '_try_page', None) is not None and self._try_page.advance(1):
            return
        if key == "done":
            self._finish()
            return
        self._skipped_steps.discard(self._step)
        self._step += 1
        self._show_step()

    def _go_back(self):
        if (self._steps[self._step][0] == 'try' and getattr(self, '_try_page', None) is not None
                and self._try_page.advance(-1)):
            return
        # Reuses _show_step() -- the exact same page-setup/dots/title/nav-
        # button routine forward navigation uses, so there is no separate
        # "go backward" page-setup path to keep in sync. _show_step()'s
        # one-time step-enter side effects (hook install, hint timer) are
        # already idempotent on re-entry: hint timer is always cancelled
        # before being restarted, hook installation is a dict overwrite
        # (not an accumulating registration), and completion handlers
        # (_complete_dictation/_complete_command) separately guard on
        # membership in self._completed -- so stepping back to an
        # already-completed interactive step and forward again cannot
        # re-fire a one-time side effect.
        if self._step > 0:
            self._step -= 1
            self._show_step()

    def _skip_step(self):
        """Skip the current interactive step without marking it complete."""
        key = self._steps[self._step][0]
        self._remove_hook(key)
        self._cancel_hint_timer()
        self._skipped_steps.add(self._step)
        self._step += 1
        self._show_step()

    def _skip_tutorial(self):
        """Skip the entire tutorial and close."""
        self._remove_all_hooks()
        self._cancel_hint_timer()
        self._mark_tutorial_complete()
        self.close()

    def _finish(self):
        self._remove_all_hooks()
        self._cancel_hint_timer()
        self._mark_tutorial_complete()
        self.close()

    def _mark_tutorial_complete(self):
        if not hasattr(self._app, 'config'):
            return
        self._app.config['tutorial_complete'] = True
        if hasattr(self._app, 'persist_config'):
            try:
                self._app.persist_config()
            except Exception as e:
                logger.debug(f"_mark_tutorial_complete: {e}")

    # ------------------------------------------------------------------
    # Hint timer
    # ------------------------------------------------------------------

    def _start_hint_timer(self, key: str):
        self._hint_timer = QTimer(self)
        self._hint_timer.setSingleShot(True)
        self._hint_timer.timeout.connect(lambda: self._show_hint(key))
        self._hint_timer.start(_HINT_DELAY_MS)

    def _cancel_hint_timer(self):
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None

    def _show_hint(self, key: str):
        hints = {
            "dictation": getattr(self, '_dict_hint', None),
            "command":   getattr(self, '_cmd_hint',  None),
        }
        hint_widget = hints.get(key)
        if hint_widget:
            hint_widget.setVisible(True)

    # ------------------------------------------------------------------
    # Interaction hooks
    # ------------------------------------------------------------------

    def _install_hook(self, key: str):
        """Register a one-shot callback on the app for step `key`."""
        if not hasattr(self._app, '_tutorial_hooks'):
            self._app._tutorial_hooks = {}

        if key == "dictation":
            def _cb(text: str):
                self._step_signal.emit(registration.event, text or "")
            registration = tutorial_capture.register(self._app, int(self.winId()), _cb)

        elif key == "command":
            def _cb(cmd_name: str):
                self._step_signal.emit("command_done", cmd_name or "")
            self._app._tutorial_hooks['command'] = _cb

    def _remove_hook(self, key: str):
        if key == "dictation":
            tutorial_capture.release(self._app)
        if hasattr(self._app, '_tutorial_hooks'):
            self._app._tutorial_hooks.pop(key, None)

    def _remove_all_hooks(self):
        tutorial_capture.release(self._app)
        if hasattr(self._app, '_tutorial_hooks'):
            for key in ('dictation', 'command'):
                self._app._tutorial_hooks.pop(key, None)

    # ------------------------------------------------------------------
    # Step completion
    # ------------------------------------------------------------------

    def _on_step_signal(self, event: str, payload: str):
        """Handle success signals delivered from worker threads via Signal."""
        if tutorial_capture.accept_event(self._app, event):
            self._complete_dictation(payload)
        elif event == "command_done":
            self._complete_command(payload)

    def _dictation_text_changed(self):
        if self._steps[self._step][0] == 'dictation' and self._dictation_box.toPlainText().strip():
            self._complete_dictation('')

    def _complete_dictation(self, text: str):
        if self._dictation_box and text:
            cursor = self._dictation_box.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText((' ' if self._dictation_box.toPlainText() else '') + text)
        if 1 in self._completed:
            return
        self._completed.add(1)
        self._skipped_steps.discard(1)
        self._cancel_hint_timer()
        if hasattr(self, '_dict_success'):
            self._dict_success.setVisible(True)
        if hasattr(self, '_dict_hint'):
            self._dict_hint.setVisible(False)
        self._next_btn.setEnabled(True)

    def _complete_command(self, cmd_name: str):
        if 2 in self._completed:
            return
        self._completed.add(2)
        self._skipped_steps.discard(2)
        self._cancel_hint_timer()
        # Scroll the demo area a bit to make it visible
        if self._scroll_area:
            sb = self._scroll_area.verticalScrollBar()
            sb.setValue(sb.value() + 60)
        if hasattr(self, '_cmd_success'):
            self._cmd_success.setVisible(True)
        if hasattr(self, '_cmd_hint'):
            self._cmd_hint.setVisible(False)
        self._next_btn.setEnabled(True)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._remove_all_hooks()
        self._cancel_hint_timer()
        event.accept()
