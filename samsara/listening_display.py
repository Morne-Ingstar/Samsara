"""One persisted choice, frozen per app lifetime; no startup config writes."""
from dataclasses import dataclass
import threading

PRESELECTED = "full"  # Owner decision: decide.display_preselected.
VALUES = ("compact", "full")
LABEL = "Listening display"
QUESTION = "Which listening display would you like?"
NAMES = {"compact": "Compact", "full": "Full"}
DESCRIPTIONS = {
    "compact": "A more minimal status indicator: recording on or off, and the recording mode.",
    "full": (
        "Shows your mode and recording status, live words during supported hold recordings, and a brief result after text is sent. "
        "Hold and tap normally send when you finish; you can pause a hold to keep its draft visible, correct a word or clear it before choosing Insert text. "
        "In hands-free dictation, each thought stays in the box so you can correct it, clear it or keep adding until you say your paste word or choose Insert text."
    ),
}
RESTART_NOTICE = "Restart required to apply."  # Existing Settings wording.
_runtime_lock = threading.RLock()


@dataclass(frozen=True)
class DisplayChoice:
    value: str
    show_idle: bool

    @property
    def full(self):
        return self.value == "full"


def display_choice(config, *, setup=False):
    """Read the choice or derive the legacy state without modifying config.

    Compact includes legacy recording receipts with the idle marker hidden.
    The existing internal show_idle_mark preference carries that quiet state
    across the next Settings save; it is not a third display or UI switch.
    """
    ui = config.get("ui", {}) or {}
    live = ui.get("live_surface", {}) or {}
    value = ui.get("listening_display")
    if value in VALUES:
        return DisplayChoice(value, bool(live.get("show_idle_mark", True)))
    if setup:
        return DisplayChoice(PRESELECTED, True)
    if live.get("enabled", False):
        return DisplayChoice("full", bool(live.get("show_idle_mark", True)))
    return DisplayChoice("compact", bool(config.get("listening_indicator_enabled", False)))


def run_display(app):
    """Freeze before construction/hotkey registration; never reread on Apply."""
    with _runtime_lock:
        choice = getattr(app, "_listening_display_choice", None)
        if not isinstance(choice, DisplayChoice):
            choice = display_choice(getattr(app, "config", {}) or {})
            app._listening_display_choice = choice
        return choice


def build_display(app, compact_factory, full_factory=None, adapter_factory=None):
    """Build exactly one implementation on the caller's Qt thread."""
    choice = run_display(app)
    if choice.full:
        if full_factory is None or adapter_factory is None:
            from samsara.live_surface.controller import LiveSurfaceController, LiveSurfaceIndicatorAdapter
            full_factory = full_factory or LiveSurfaceController
            adapter_factory = adapter_factory or LiveSurfaceIndicatorAdapter
        app.live_surface = full_factory(app)
        app.live_surface.start()
        app.listening_indicator = adapter_factory(app.live_surface)
    else:
        app.listening_indicator = compact_factory()
    from samsara.speech_readiness import show
    show(app)  # 918: the shell can precede the async model by minutes.
    return choice.full


def display_visible(app):
    return getattr(app, "_listening_display_visible", run_display(app).show_idle)


def require_full_surface(app):
    controller = getattr(app, "live_surface", None)
    if controller is None:
        raise RuntimeError("Full listening display has not been constructed yet")
    return controller


def toggle_display_visibility(app):
    """Tray visibility is session-local; never rewrite either legacy key."""
    app._listening_display_visible = not display_visible(app)
    app.apply_listening_indicator_settings()


def choice_updates(config, value=None, *, chosen=False):
    """Save only at an explicit Settings/setup save, preserving legacy keys."""
    inherited = display_choice(config)
    value = value if value in VALUES else inherited.value
    ui = dict(config.get("ui", {}) or {})
    live = dict(ui.get("live_surface", {}) or {})
    ui["listening_display"] = value
    if chosen or value == "compact":
        live["show_idle_mark"] = True if chosen else inherited.show_idle
    ui["live_surface"] = live
    return {"ui": ui}


def request_restart_once(app, notify):
    """One notice per pending runtime choice; changing back clears it."""
    if display_choice(app.config) == run_display(app):
        app._listening_display_restart_notified = False
        return False
    if getattr(app, "_listening_display_restart_notified", False):
        return False
    app._listening_display_restart_notified = True
    notify(RESTART_NOTICE)
    return True
