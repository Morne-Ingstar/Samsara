"""Ava setup: optional PC check, local/cloud choice, setup and a real test.

No audio required — background threads handle HTTP checks and subprocess pulls.

Public API (same wrapper pattern as MicSetupWizardQt):
    AvaGuideQt(app).show()
"""

import weakref
from copy import deepcopy
from types import SimpleNamespace

import json
import re
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from PySide6.QtCore import QEvent, QUrl, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFrame, QHBoxLayout,
    QLabel, QPushButton, QRadioButton, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget, QScrollArea,
)

from samsara.constants import DEFAULT_WAKE_PHRASE
from samsara import ai_preferences, hardware_check
from samsara.runtime import thread_registry
from samsara.ui import ava_consent_qt, qt_runtime

from samsara.log import get_logger
from samsara.ui import theme
from samsara.ui.widgets.step_indicator import StepIndicator

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Colours — identical to mic_setup_wizard_qt.py
# ---------------------------------------------------------------------------


# Colour comes from samsara.ui.theme, never from a literal here: this
# module used to keep its own copy of the dark palette, which is exactly
# how a second palette leaves one window unreadable (queue 129).

def _primary_ss():
    return (
        f"QPushButton{{background:{theme.ACCENT};color:{theme.BG0};"
        f"border:none;border-radius:4px;font-weight:bold;padding:6px 18px;}}"
        f"QPushButton:hover{{background:{theme.ACCENT_HOVER};color:{theme.BG0};}}"
        f"QPushButton:disabled{{background:{theme.BG2};color:{theme.TEXT_DISABLED};"
        f"border:1px solid {theme.BORDER};}}"
    )


def _ss() -> str:
    """The window's stylesheet, built on demand. Never a module
    constant: an f-string evaluated at import time freezes the
    palette that happened to be live then (queue 129)."""
    return f"""
    QDialog, QWidget {{
        color: {theme.TEXT_PRIMARY};
        font-family: 'Segoe UI', sans-serif;
        font-size: {theme.TYPE_BODY}px;
    }}
    QDialog, QScrollArea, QStackedWidget, QWidget#avaPage {{ background: {theme.BG0}; }}
    QPushButton {{
        background: {theme.BG2};
        color: {theme.TEXT_PRIMARY};
        border: 1px solid {theme.BORDER};
        padding: 6px 18px;
        border-radius: 4px;
        min-width: 80px;
    }}
    QPushButton:hover {{
        background: {theme.ACCENT};
        color: {theme.BG0};
        border-color: {theme.ACCENT};
    }}
    QPushButton:disabled {{
        background: {theme.BG2};
        color: {theme.TEXT_DISABLED};
        border-color: {theme.BORDER};
    }}
    QPushButton#primary {{
        background: {theme.ACCENT};
        color: {theme.BG0};
        border-color: {theme.ACCENT};
        font-weight: bold;
    }}
    QPushButton#primary:hover {{ background: {theme.ACCENT_HOVER}; }}
    QPushButton#ghost {{
        background: transparent;
        color: {theme.TEXT_SECONDARY};
        border: none;
    }}
    QPushButton#ghost:hover {{ color: {theme.TEXT_PRIMARY}; background: transparent; }}
    QComboBox {{
        background: {theme.BG1};
        border: 1px solid {theme.BORDER};
        color: {theme.TEXT_PRIMARY};
        padding: 5px 10px;
        border-radius: 4px;
    }}
    QComboBox::drop-down {{ border: none; width: 20px; }}
    QComboBox QAbstractItemView {{
        background: {theme.BG1};
        color: {theme.TEXT_PRIMARY};
        selection-background-color: {theme.ACCENT};
        selection-color: {theme.BG0};
        border: 1px solid {theme.BORDER};
    }}
    QLabel {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: {theme.BG0}; width: 12px; }}
    QScrollBar::handle:vertical {{ background: {theme.BORDER}; min-height: 24px; border-radius: 4px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
"""

# ---------------------------------------------------------------------------
# Ollama model recommendations
# ---------------------------------------------------------------------------

_MODELS = [
    ("qwen2.5:3b",  "~2 GB",    "Fastest — best for quick command help"),
    ("llama3.2",    "~2 GB",    "Balanced quality and speed"),
    ("qwen2.5:7b",  "~4.7 GB",  "Smarter — handles complex questions"),
    ("mistral",     "~4.1 GB",  "Reliable all-rounder, good reasoning"),
]

_OLLAMA_URL = "http://localhost:11434"
_OLLAMA_DOWNLOAD = "https://ollama.com/download"
_OLLAMA_RECHECK_INTERVAL_MS = 5_000
_OLLAMA_RECHECK_LIMIT = 120  # ten minutes at one check every five seconds
# PLACEHOLDER COPY (462)
_AVA_CLOUD_GUIDE_NOTE = ('Ava is set to the cloud in Settings > Ava. '
                        'This guide sets up Ava on this PC.')

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
)

_AVA_KEY_OPTIONS = [
    ("Right Alt (default)", "right_alt"),
    ("Right Ctrl",          "rctrl"),
    ("F13",                 "f13"),
]


def _ping_ollama(timeout: int = 3, host: str = _OLLAMA_URL) -> tuple[bool, list[str]]:
    """Return (is_running, list_of_installed_model_names)."""
    try:
        req = urllib.request.Request(f"{host.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            models = [m["name"] for m in data.get("models", [])]
            return True, models
    except Exception:
        return False, []


def _clean_ollama_output(text: str) -> str:
    """Keep model-pull progress readable without leaking terminal controls."""
    text = _ANSI_RE.sub("", text).replace("\r", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Public wrapper
# ---------------------------------------------------------------------------

class AvaGuideQt:
    """Drop-in Qt wizard — same open/close pattern as MicSetupWizardQt."""

    def __init__(self, app):
        self.app = app
        self._window: "_WizardWindow | None" = None
        self._init_posted = False

    @property
    def window(self):
        return self._window

    def show(self):
        if self._window is not None:
            qt_runtime.post(self._window.show)
            qt_runtime.post(self._window.raise_)
            qt_runtime.post(self._window.activateWindow)
        elif not self._init_posted:
            self._init_posted = True
            qt_runtime.post(self._init_window)

    def close(self):
        if self._window is not None:
            qt_runtime.post(self._window.close)

    def _init_window(self):
        """Runs on the Qt thread."""
        self._window = _WizardWindow(self.app)
        self._window.destroyed.connect(self._on_destroyed)
        self._window.show()

    def _on_destroyed(self):
        self._window = None
        self._init_posted = False


# ---------------------------------------------------------------------------
# Wizard window
# ---------------------------------------------------------------------------


from PySide6.QtWidgets import QCheckBox, QLineEdit, QGridLayout, QSpinBox, QDoubleSpinBox
from samsara import cloud_llm
from samsara.ui.settings.presentation import style as presentation_style, CleanComboBox
from samsara import ai_models, ai_transport, openrouter_catalog
from samsara.paths import samsara_home_dir
import time
from urllib.parse import urlencode


def _model_cache_path(provider):
    if provider not in ai_models.SNAPSHOT:
        raise ValueError('unsupported provider')
    return samsara_home_dir() / ('ai_models_' + provider + '.json')


def _merge_models(earlier, later):
    """Refresh sparse endpoint rows without losing bundled friendly metadata."""
    rows = {row['id']: dict(row) for row in earlier}
    for row in later:
        prior = rows.get(row['id'], {})
        merged = dict(prior)
        for key, value in row.items():
            if value is None or value == [] or value == {'prompt': None, 'completion': None}:
                if key not in merged:
                    merged[key] = value
            elif key == 'name' and value == row['id'] and prior.get('name'):
                continue
            else:
                merged[key] = value
        rows[row['id']] = merged
    return tuple(rows.values())[:openrouter_catalog.MAX_ENTRIES]


def _load_models(provider, *, refresh=False, api_key=''):
    """Offline first; only explicit consent/key-check code may request a refresh.

    Reuse the Settings catalogue parser and atomic metadata-only cache writer.
    Keys, raw responses and error bodies never enter the cache or the UI.
    """
    path = _model_cache_path(provider)
    now = time.time()
    bundled = []
    for row in ai_models.SNAPSHOT[provider]['models']:
        bundled.append({'id': row['id'], 'name': row['display_name'],
                        'context_length': row['context'],
                        'pricing': row.get('pricing', {}),
                        'supported_parameters': (['tools'] if row['tools'] else []) +
                                                (['response_format'] if row['json'] else [])})
    cached = openrouter_catalog.read_cache(path, now)
    models = _merge_models(openrouter_catalog.parse_models({'data': bundled}), cached.models)
    fallback = openrouter_catalog.Catalogue(models, cached.fetched_at, cached.attempted_at, cached.reason)
    if not refresh or not api_key.strip():
        return fallback
    headers = ({'x-api-key': api_key, 'anthropic-version': '2023-06-01'} if provider == 'anthropic'
               else {'Authorization': 'Bearer ' + api_key})
    endpoint = cloud_llm.BUILTIN_PROVIDERS[provider]['base_url'] + '/models'
    url, response, incoming = endpoint, None, ()
    try:
        for _page in range(20):
            response = ai_transport.get_cloud(url, timeout=8, headers=headers)
            response.raise_for_status()
            if len(response.content) > openrouter_catalog.MAX_BYTES:
                raise ValueError('oversize catalogue')
            payload = response.json()
            # Anthropic supplies display_name; OpenAI/DeepSeek may supply ids only.
            if isinstance(payload, dict) and isinstance(payload.get('data'), list):
                names = {row['id']: row['name'] for row in models}
                payload = dict(payload, data=[dict(row, name=row.get('display_name') or row.get('name') or
                    names.get(row.get('id')) or row.get('id', '').replace('/', ' / ').replace('-', ' '))
                    if isinstance(row, dict) and isinstance(row.get('id'), str) else row
                    for row in payload['data']])
            incoming = _merge_models(incoming, openrouter_catalog.parse_models(payload))
            response.close()
            response = None
            if provider != 'anthropic' or not payload.get('has_more'):
                break
            cursor = payload.get('last_id')
            if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
                raise ValueError('invalid cursor')
            url = endpoint + '?' + urlencode({'after_id': cursor, 'limit': 100})
        else:
            raise ValueError('catalogue page limit')
        if not incoming:
            raise ValueError('empty catalogue')
        result = openrouter_catalog.Catalogue(_merge_models(models, incoming), now, now)
    except (ValueError, TypeError, RecursionError):
        return openrouter_catalog.Catalogue(models, cached.fetched_at, now, 'invalid')
    except Exception:
        return openrouter_catalog.Catalogue(models, cached.fetched_at, now, 'offline')
    finally:
        if response is not None:
            response.close()
    try:
        openrouter_catalog._write_cache(path, result)
    except (OSError, ValueError):
        return openrouter_catalog.Catalogue(result.models, now, now, 'cache')
    return result


def _make_provider_picker(entry, provider, active, catalogue, refresh_action, custom_action, choose_action):
    # Reuse 568's widget, keyboard activation, search/filter signals and selection.
    # Lazy import avoids the Settings module's initialization cycle.
    from samsara.ui.settings.ava_cloud_qt import _OpenRouterPicker
    from PySide6.QtWidgets import QListWidgetItem

    class ProviderPicker(_OpenRouterPicker):
        def __init__(self):
            self.provider = provider
            super().__init__(entry, active)
            self.setAccessibleName('Cloud model picker')
            self.search.setAccessibleName('Search models by name or id')
            self.models.setAccessibleName('Models with names and ids')
            self.status.setAccessibleName('Model list status')
            self.refresh.setText('Check key and refresh')
            self.refresh.setAccessibleName(self.refresh.text())
            self.receive(catalogue)

        @staticmethod
        def row_text(model):
            text = model['name'] + '\n' + model['id']
            if model['context_length']:
                text += f"\nContext: {model['context_length']:,}"
            return text

        def populate(self, *_args):
            selected = self.models.currentItem()
            selected_id = selected.data(Qt.ItemDataRole.UserRole) if selected else self.entry.text().strip()
            self.models.clear()
            default = ai_models.SNAPSHOT[self.provider]['default']
            for model in self.catalogue.models:
                if not openrouter_catalog.matches(model, self.search.text(), self.free.isChecked(), self.capabilities.isChecked()):
                    continue
                text = self.row_text(model)
                if model['id'] == default:
                    text = text.replace('\n', ' · Recommended\n', 1)
                item = QListWidgetItem(text, self.models)
                item.setData(Qt.ItemDataRole.UserRole, model['id'])
                item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
                if model['id'] == selected_id:
                    self.models.setCurrentItem(item)
            item = QListWidgetItem('Custom model id', self.models)
            item.setData(Qt.ItemDataRole.UserRole, None)
            if self.models.currentRow() < 0:
                self.models.setCurrentRow(0)

        def select_model(self):
            item = self.models.currentItem()
            if self.active() and item is not None:
                if item.data(Qt.ItemDataRole.UserRole) is None:
                    custom_action()
                else:
                    choose_action()
                    super().select_model()

        def load(self, *, refresh=False):
            if refresh and self.active():
                refresh_action()

        def receive(self, result):
            self.catalogue = result
            # Never imply unsupported/paid when metadata is merely absent.
            free_known = any(any(v is not None for v in m['pricing'].values()) or m['id'].endswith(':free') for m in result.models)
            tools_known = any(m['supported_parameters'] for m in result.models)
            for control, label, known in ((self.free, self.free_label, free_known),
                                           (self.capabilities, self.capabilities_label, tools_known)):
                if not known:
                    control.setChecked(False)
                control.setVisible(known)
                label.setVisible(known)
            self.populate()
            note = ('Saved model list.' if result.fetched_at else 'Bundled model list. No connection needed.')
            if result.reason in ('offline', 'invalid'):
                note += ' Could not refresh. You can still choose a model.'
            elif result.reason == 'cache':
                note += ' The updated list could not be saved on this PC.'
            self.status.setText(note)

    return ProviderPicker()

# Stand-in copy throughout; owner review inventory is in report 630/ASK.json.
COPY = {
    'intro': 'Choose where Ava thinks, set it up, then test the connection.',
    'intro_note': 'Ava is optional. You can leave this guide and come back later.',
    'local_desc': 'Uses a model on your computer. Your requests stay here.',
    'cloud_desc': 'Uses your account with an AI provider. Needs an internet connection.',
    'details': 'Enter what you know, or let Samsara fill in these same fields.',
    'permission': 'Check my graphics card, memory and processor. Nothing leaves this computer.',
    'check_failed': 'The PC check could not finish. Your entries are still here. Enter what you know, or continue without a recommendation.',
    'unknown': 'There is not enough information to recommend a choice. Both choices are available.',
    'manual_cpu': 'Your processor has not been checked. The speed test will help you decide.',
    'choose': 'This is a suggestion. You can use either choice and change it later.',
    'local_setup': 'Ollama runs the model on your PC. Check it, then choose a model.',
    'ollama_missing': 'Cannot connect to Ollama. If it is missing, download and install it. If it is installed, open it and check again.',
    'model_missing': 'This model is not on your PC yet. Download it to continue.',
    'download_failed': 'The download did not finish. Check your connection and try again.',
    'cloud_setup': 'Use your own provider account. Your key stays hidden in this form.',
    'key_good': 'The key check passed. A test reply is still needed.',
    'bad_key': 'The provider rejected this key. Replace it, then check again.',
    'no_credit': 'The account balance or key spending limit is exhausted. Check it with your provider, then retry.',
    'network': 'The connection failed. Check your internet connection, then retry.',
    'model_unavailable': 'This model is unavailable. Choose another model, then retry.',
    'provider_error': 'The provider could not complete the request. Try again shortly, or choose another provider.',
    'rate_limited': 'The provider is limiting requests. Wait a moment, then retry.',
    'blocked': 'The provider blocked this request. Check your account restrictions or choose another provider.',
    'local_test': 'Run a short speed test, usually about 10 seconds. It checks this model on this PC.',
    'cloud_test': 'Send one short setup message to your selected model. No speech or remembered facts are included.',
    'local_timeout': 'No speed result yet. The model may still be loading. Wait a moment, then retry.',
    'cloud_success': 'The model answered the setup message. Your connection works.',
    'local_success': 'The model replied comfortably in this test. Longer replies can take more time.',
    'local_slow': 'The model replied, but slowly. You can keep this choice or try the cloud.',
    'ready': 'The connection test passed. Save this choice to use Ava.',
    'ready_note': 'You can change this choice in Settings > Ava.',
    'consent_common': 'Ava can run commands on your computer, including opening apps, typing and changing settings. She can make mistakes. Some actions cannot be undone.',
    'consent_local': 'This choice sends Ava requests to a model on this PC. Your requests stay on this computer.',
    'consent_cloud': 'Your requests go to the provider you choose, using your account and key. If assistant memory is enabled, saved facts and recent summaries also go with requests. Samsara is free.',
    'consent_memory': 'You can read or erase Ava’s local memory file and turn Ava off in Settings.',
    'save_failed': 'The choice could not be saved. Try again before closing the guide.',
    'changed': 'Settings changed. Review your choice and test again.',
}


def _text(value, role='description'):
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setProperty('role', role)
    label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return label


class _WizardWindow(QDialog):
    """630 design candidate. Reuses 617 hardware, downloads and local measurement.

    Stages settings until a real test succeeds and the user chooses Use Ava.
    Preview fixtures live outside production; there are no simulated successes here.
    """
    _result = Signal(int, str, object)
    _pull_line_sig = Signal(str)
    _pull_done_sig = Signal(bool)
    _STEP_INTRO, _STEP_CHECK, _STEP_CHOOSE, _STEP_CONSENT = range(4)
    _STEP_OLLAMA, _STEP_CLOUD, _STEP_TEST, _STEP_DONE = range(4, 8)
    _STEP_MODEL = _STEP_OLLAMA

    def __init__(self, app):
        super().__init__()
        self._app = app
        self._opened_config = deepcopy(app.config)
        self._route = 'local'
        self._generation = 0
        self._closed = False
        self._busy = False
        self._facts = None
        self._filling = False
        self._accepted = None
        self._tested = None
        self._ollama_running = False
        self._installed_models = []
        self._selected_model_name = (app.config.get('ollama') or {}).get('model') or 'qwen2.5:3b'
        self._pulling = False
        self._pull_proc = None
        self._ollama_recheck_attempts = 0
        self._ollama_recheck_timer = QTimer(self)
        self._ollama_recheck_timer.setInterval(_OLLAMA_RECHECK_INTERVAL_MS)
        self._ollama_recheck_timer.timeout.connect(self._auto_recheck_ollama)
        self._result.connect(self._receive)
        self._pull_line_sig.connect(self._on_pull_line)
        self._pull_done_sig.connect(self._on_pull_done)
        self.setWindowTitle('Set up Ava')
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setStyleSheet(presentation_style() + f'''
          QRadioButton {{ background: {theme.BG1}; padding: 16px; border: 1px solid {theme.BORDER}; border-radius: 8px; spacing: 12px; }}
          QRadioButton:checked {{ border: 2px solid {theme.ACCENT}; background: {theme.BG2}; }}
          QRadioButton::indicator {{ width: 22px; height: 22px; border: 2px solid {theme.TEXT_SECONDARY}; border-radius: 12px; background: {theme.BG0}; }}
          QRadioButton::indicator:checked {{ background: {theme.ACCENT}; border: 3px solid {theme.BG1}; }}
          QRadioButton:focus {{ border: 2px solid {theme.ACCENT}; }}
          QLineEdit:focus {{ border: 2px solid {theme.ACCENT}; }}
          QDoubleSpinBox {{ background:{theme.BG2}; border:1px solid {theme.BORDER}; border-radius:6px; padding:0 12px; }}
          QScrollArea {{ border: none; }}
          QFrame#pcCheck {{ background: {theme.BG1}; border: 1px solid {theme.BORDER}; border-radius: 8px; }}
        ''' + theme.SCROLLBAR_QSS)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(158)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(18, 24, 12, 24)
        side.addWidget(_text('Samsara', 'label'))
        side.addSpacing(24)
        side.addWidget(_text('SET UP AVA', 'description'))
        self._steps = []
        for name in ('Welcome', 'Your PC', 'Choose', 'Set up', 'Test'):
            item = _text(name)
            item.setMinimumHeight(44)
            side.addWidget(item)
            self._steps.append(item)
        side.addStretch()
        body.addWidget(sidebar)
        self._stack = QStackedWidget()
        self._pages = []
        self._build_intro()
        self._build_details()
        self._build_choose()
        self._build_consent()
        self._build_local()
        self._build_cloud()
        self._build_test()
        self._build_ready()
        body.addWidget(self._stack, 1)
        root.addLayout(body, 1)
        footer = QFrame()
        footer.setObjectName('footer')
        nav = QHBoxLayout(footer)
        self._nav_layout = nav
        nav.setContentsMargins(24, 12, 24, 12)
        self._back_btn = self._button('Back', self._go_back)
        self._skip_btn = self._button('Set up later', self.close)
        self._next_btn = self._button('Get started', self._go_next)
        theme.make_primary(self._next_btn)
        nav.addWidget(self._back_btn)
        nav.addStretch()
        nav.addWidget(self._skip_btn)
        nav.addWidget(self._next_btn)
        root.addWidget(footer)
        available = self.screen().availableGeometry()
        self.resize(min(960, available.width()-40), min(660, available.height()-60))
        self.setMinimumSize(720, 420)
        theme.apply_design_scale(self)
        self._current_step = self._STEP_INTRO
        self._go_to(self._STEP_INTRO)

    def _button(self, text, action):
        button = QPushButton(text)
        button.setAccessibleName(text)
        button.setMinimumHeight(theme.HIT_TARGET_MIN)
        button.setAutoDefault(False)
        button.clicked.connect(action)
        return button

    def _page(self, title, description):
        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(28, 24, 28, 20)
        column.setSpacing(12)
        column.addWidget(_text(title, 'title'))
        if description:
            column.addWidget(_text(description))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self._stack.addWidget(scroll)
        self._pages.append(page)
        return column

    def _row(self, column, name, *controls):
        row = QHBoxLayout()
        label = _text(name, 'label')
        label.setFixedWidth(120)
        row.addWidget(label)
        for control in controls:
            control.setMinimumHeight(theme.HIT_TARGET_MIN)
            row.addWidget(control, 1 if not isinstance(control, QPushButton) else 0)
        label.setBuddy(controls[0])
        controls[0].setAccessibleName(name)
        column.addLayout(row)

    def _status(self, text=''):
        result = _text(text)
        result.setStyleSheet(f'color:{theme.ACCENT}; padding:8px 0;')
        return result

    def _build_intro(self):
        col = self._page('Ava, set up your way', COPY['intro'])
        col.addSpacing(16)
        for heading, detail in [('1   Choose where Ava thinks', 'On this PC or in the cloud.'),
                                ('2   Set up your choice', 'We will walk through what it needs.'),
                                ('3   Test the connection', 'Make sure the selected model can reply.')]:
            col.addWidget(_text(heading, 'label'))
            col.addWidget(_text(detail))
            col.addSpacing(8)
        col.addStretch()
        col.addWidget(_text(COPY['intro_note']))

    def _build_details(self):
        col = self._page('Tell us about your PC', COPY['details'])
        self._pc_check = QFrame()
        self._pc_check.setObjectName('pcCheck')
        self._pc_check.setAccessibleName('PC check')
        check = QVBoxLayout(self._pc_check)
        check.setContentsMargins(16, 12, 16, 12)
        check.setSpacing(8)
        check.addWidget(_text('PC check', 'label'))
        self._check_explanation = _text(COPY['permission'])
        check.addWidget(self._check_explanation)
        self._check_btn = self._button('Run the PC check', self._check_pc)
        self._check_btn.setAccessibleDescription(COPY['permission'])
        check.addWidget(self._check_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._hardware_status = self._status()
        self._hardware_status.hide()
        check.addWidget(self._hardware_status)
        col.addWidget(self._pc_check)
        fields = QGridLayout()
        self._gpu = QLineEdit()
        self._gpu.setPlaceholderText('For example, NVIDIA RTX 3060')
        self._vram = QDoubleSpinBox()
        self._ram = QDoubleSpinBox()
        for spin in (self._vram, self._ram):
            spin.setRange(0, 1024)
            spin.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
            spin.setDecimals(1)
            spin.setSuffix(' GB')
            spin.setSpecialValueText('Not sure')
        self._cpu = QLineEdit()
        self._cpu.setPlaceholderText('Optional')
        self._cores = QSpinBox()
        self._cores.setRange(0, 512)
        self._cores.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self._cores.setSpecialValueText('Not sure')
        for i, (label, control) in enumerate([('Graphics card', self._gpu), ('Graphics memory', self._vram),
                                             ('System memory', self._ram), ('Processor', self._cpu), ('Processor cores', self._cores)]):
            group = QVBoxLayout()
            caption = _text(label, 'label')
            caption.setBuddy(control)
            control.setAccessibleName(label)
            control.setMinimumHeight(44)
            group.addWidget(caption)
            group.addWidget(control)
            fields.addLayout(group, i//3, i%3)
            signal = control.textEdited if isinstance(control, QLineEdit) else control.valueChanged
            signal.connect(self._manual_changed)
        col.addLayout(fields)
        col.addStretch()

    def _build_choose(self):
        col = self._page('Where should Ava think?', COPY['choose'])
        self._hardware_verdict = self._status(COPY['unknown'])
        col.addWidget(self._hardware_verdict)
        self._local_choice = QRadioButton('On this PC')
        self._cloud_choice = QRadioButton('In the cloud')
        saved_route = (self._app.config.get('ava') or {}).get('provider_policy')
        self._local_choice.setChecked(saved_route == 'local')
        self._cloud_choice.setChecked(saved_route == 'cloud')
        for choice, desc in [(self._local_choice, COPY['local_desc']), (self._cloud_choice, COPY['cloud_desc'])]:
            choice.setMinimumHeight(58)
            choice.setAccessibleName(choice.text())
            choice.toggled.connect(lambda _: self._nav())
            col.addWidget(choice)
            col.addWidget(_text(desc))
        col.addStretch()

    def _build_consent(self):
        col = self._page('Before you turn Ava on', COPY['consent_common'])
        self._consent_selection = _text('', 'label')
        col.addWidget(self._consent_selection)
        self._consent_route = _text('')
        col.addWidget(self._consent_route)
        col.addWidget(_text(COPY['consent_memory']))
        self._consent_check = QCheckBox('I understand and agree')
        self._consent_check.setMinimumHeight(44)
        self._consent_check.toggled.connect(lambda _: self._nav())
        col.addWidget(self._consent_check)
        col.addStretch()

    def _build_local(self):
        col = self._page('Set up Ava on this PC', COPY['local_setup'])
        self._ollama_status_lbl = self._status('Ollama has not been checked yet.')
        col.addWidget(self._ollama_status_lbl)
        row = QHBoxLayout()
        self._ollama_check_btn = self._button('Check Ollama', self._check_ollama)
        self._download_ollama_btn = self._button('Download Ollama', self._open_ollama_download)
        row.addWidget(self._ollama_check_btn)
        row.addWidget(self._download_ollama_btn)
        row.addStretch()
        col.addLayout(row)
        self._model_combo = CleanComboBox()
        for model in dict.fromkeys([self._selected_model_name, 'qwen2.5:3b', 'qwen2.5:7b', 'llama3.2', 'mistral']):
            self._model_combo.addItem(model)
        self._model_combo.currentTextChanged.connect(self._model_changed)
        self._row(col, 'Model', self._model_combo)
        self._model_status_lbl = self._status(COPY['model_missing'])
        col.addWidget(self._model_status_lbl)
        self._pull_btn = self._button('Download model', self._pull_model)
        self._pull_btn.setEnabled(False)
        col.addWidget(self._pull_btn, 0, Qt.AlignmentFlag.AlignLeft)
        self._pull_progress = _text('')
        col.addWidget(self._pull_progress)
        col.addStretch()
        # 617 download mechanics use these nonvisual status seams.
        self._model_status_dot = QLabel()

    def _build_cloud(self):
        col = self._page('Set up Ava in the cloud', COPY['cloud_setup'])
        self._provider = CleanComboBox()
        for code, label in [('deepseek', 'DeepSeek'), ('openai', 'OpenAI'), ('anthropic', 'Anthropic'), ('openrouter', 'OpenRouter')]:
            self._provider.addItem(label, code)
        saved = self._app.config.get('cloud_llm', {}) or {}
        self._provider.setCurrentIndex(max(0, self._provider.findData(saved.get('provider', 'deepseek'))))
        self._row(col, 'Provider', self._provider)
        self._api_key = QLineEdit(saved.get('api_key', ''))
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key = self._button('Show', self._toggle_key)
        self._row(col, 'API key', self._api_key, self._show_key)
        # Keep one value source for the inherited picker and the existing save path.
        # The line edit is visible only after choosing the explicit Custom option.
        self._cloud_model = QLineEdit(saved.get('model') or ai_models.SNAPSHOT[self._provider.currentData()]['default'])
        self._cloud_models = CleanComboBox()
        self._row(col, 'Model', self._cloud_models)
        self._model_id = _text('', 'description')
        self._model_id.setTextFormat(Qt.TextFormat.PlainText)
        col.addWidget(self._model_id)
        self._cloud_model.setAccessibleName('Custom model id')
        self._cloud_model.setPlaceholderText('Enter a custom model id')
        self._cloud_model.setMinimumHeight(theme.HIT_TARGET_MIN)
        col.addWidget(self._cloud_model)
        self._custom_mode = False
        self._cloud_catalogue = _load_models(self._provider.currentData())
        self._fill_model_choices()
        actions = QHBoxLayout()
        self._key_btn = self._button('Check key', self._check_key)
        self._picker_btn = self._button('Choose model', self._open_picker)
        actions.addWidget(self._key_btn)
        actions.addWidget(self._picker_btn)
        actions.addStretch()
        col.addLayout(actions)
        self._cloud_status = self._status('Your key and model will be tested before you finish.')
        col.addWidget(self._cloud_status)
        col.addStretch()
        self._provider.currentIndexChanged.connect(self._provider_changed)
        self._api_key.textChanged.connect(self._cloud_changed)
        self._cloud_model.textChanged.connect(self._cloud_changed)
        self._cloud_model.textChanged.connect(self._sync_model_choice)
        self._cloud_models.currentIndexChanged.connect(self._choose_cloud_model)
        self._picker = None

    def _fill_model_choices(self):
        self._cloud_models.blockSignals(True)
        self._cloud_models.clear()
        default = ai_models.SNAPSHOT[self._provider.currentData()]['default']
        for model in self._cloud_catalogue.models:
            label = model['name'] + (' · Recommended' if model['id'] == default else '')
            self._cloud_models.addItem(label, model['id'])
            index = self._cloud_models.count() - 1
            self._cloud_models.setItemData(index, model['id'], Qt.ItemDataRole.ToolTipRole)
        self._cloud_models.addItem('Custom model id', None)
        self._cloud_models.blockSignals(False)
        self._sync_model_choice()

    def _sync_model_choice(self, *_args):
        model_id = self._cloud_model.text().strip()
        index = self._cloud_models.findData(model_id)
        custom = self._custom_mode or index < 0
        self._cloud_models.blockSignals(True)
        self._cloud_models.setCurrentIndex(self._cloud_models.count() - 1 if custom else index)
        self._cloud_models.blockSignals(False)
        self._cloud_model.setVisible(custom)
        self._model_id.setVisible(not custom)
        self._model_id.setText(model_id)

    def _choose_cloud_model(self, *_args):
        model_id = self._cloud_models.currentData()
        self._custom_mode = model_id is None
        self._cloud_model.setText(model_id or '')
        self._sync_model_choice()
        if self._custom_mode:
            self._cloud_model.setFocus()

    def _custom_model(self):
        self._picker.hide()
        self._cloud_models.setCurrentIndex(self._cloud_models.count() - 1)

    def _picker_model_chosen(self):
        self._custom_mode = False
        self._sync_model_choice()

    def _picker_refresh(self):
        self._picker.hide()
        self._check_key()

    def _build_test(self):
        col = self._page('Test your connection', '')
        self._test_route_label = _text('', 'label')
        self._test_description = _text('')
        self._test_status = self._status('')
        col.addWidget(self._test_route_label)
        col.addWidget(self._test_description)
        col.addWidget(self._test_status)
        self._test_btn = self._button('Run test', self._test_route)
        col.addWidget(self._test_btn, 0, Qt.AlignmentFlag.AlignLeft)
        col.addStretch()
        col.addWidget(_text('A successful test checks the connection, not the quality of every answer.'))

    def _build_ready(self):
        col = self._page('Your connection is ready', COPY['ready'])
        self._ready_summary = self._status()
        col.addWidget(self._ready_summary)
        col.addWidget(_text('Once saved, hold the Ava key to speak to Ava.', 'label'))
        self._key_summary = _text('')
        col.addWidget(self._key_summary)
        col.addWidget(_text(COPY['ready_note']))
        col.addStretch()
        self._save_status = self._status()
        col.addWidget(self._save_status)

    def _manual_changed(self, *_args):
        if not self._filling:
            self._facts = None
            self._hardware_status.setText('')
            self._hardware_status.hide()

    def _check_pc(self):
        if self._busy:
            return
        # Pressing this explicitly labelled button is consent for this check only.
        self._hardware_status.show()
        self._hardware_status.setText('Checking this PC…')
        self._work('hardware', lambda: hardware_check.check_hardware(consent=True))

    def _fill_hardware(self, facts):
        self._facts = facts
        self._filling = True
        gpu = max(facts.gpus, key=lambda g: ('nvidia' in g.name.lower(), g.dedicated_bytes), default=None)
        self._gpu.setText(gpu.name if gpu else '')
        self._vram.setValue(gpu.dedicated_bytes / hardware_check.GIB if gpu else 0)
        self._ram.setValue(facts.ram_bytes / hardware_check.GIB)
        self._cpu.setText(facts.processor)
        self._cores.setValue(facts.cores)
        self._filling = False
        graphics = (f'{gpu.name} (shared memory)' if gpu and gpu.dedicated_bytes == 0 else
                    f'{gpu.name}, {gpu.dedicated_bytes / hardware_check.GIB:g} GB graphics memory' if gpu else
                    'Graphics card not found')
        processor = facts.processor or 'Processor not identified'
        self._hardware_status.setText(
            f'{graphics}\n{facts.ram_bytes / hardware_check.GIB:g} GB system memory; {processor} ({facts.cores} cores).')
        self._hardware_status.show()
        self._check_btn.setText('Run again')
        self._check_btn.setAccessibleName('Run again')

    def _recommend(self):
        facts = self._facts
        if facts is None and self._gpu.text().strip() and self._ram.value() and self._vram.value():
            facts = hardware_check.Hardware((hardware_check.GPU(self._gpu.text(), int(self._vram.value()*hardware_check.GIB)),),
                       int(self._ram.value()*hardware_check.GIB), self._cpu.text(), self._cores.value(), None)
        if facts is None:
            self._hardware_verdict.setText(COPY['unknown'])
            return
        verdict = hardware_check.recommend(facts)
        self._hardware_verdict.setText(verdict.reason + (' ' + COPY['manual_cpu'] if self._facts is None else ''))
        (self._local_choice if verdict.route == 'local' else self._cloud_choice).setChecked(True)
        self._model_combo.setCurrentText(verdict.model)

    def _go_to(self, step):
        self._current_step = step
        self._stack.setCurrentIndex(step)
        self._stack.currentWidget().verticalScrollBar().setValue(0)
        position = [0, 1, 2, 3, 3, 3, 4, 4][step]
        for i, label in enumerate(self._steps):
            label.setText(('✓  ' if i < position else '●  ' if i == position else '    ') + ('Welcome', 'Your PC', 'Choose', 'Set up', 'Test')[i])
            label.setStyleSheet(f'color:{theme.TEXT_PRIMARY if i == position else theme.TEXT_SECONDARY};font-weight:{600 if i == position else 400};')
        if step == self._STEP_CONSENT:
            self._consent_selection.setText(self._selection_text())
            self._consent_route.setText(COPY['consent_cloud' if self._route == 'cloud' else 'consent_local'])
        if step == self._STEP_TEST:
            self._test_route_label.setText(self._selection_text())
            self._test_description.setText(COPY[self._route + '_test'])
        if step == self._STEP_DONE:
            self._ready_summary.setText(self._selection_text())
            key = self._app.config.get('ava_mode_key', 'right_alt')
            self._key_summary.setText('Ava key: ' + {'right_alt': 'Right Alt', 'rctrl': 'Right Ctrl'}.get(key, str(key)))
        self._nav()

    def _nav(self):
        if not hasattr(self, '_next_btn'):
            return
        step = self._current_step
        self._back_btn.setVisible(step != self._STEP_INTRO)
        self._back_btn.setEnabled(not self._busy)
        labels = ['Get started', 'Continue', 'Continue', 'Continue', 'Continue', 'Continue', 'Continue', 'Use Ava']
        self._next_btn.setText(labels[step])
        ready = True
        if step == self._STEP_CHOOSE:
            ready = self._local_choice.isChecked() or self._cloud_choice.isChecked()
        elif step == self._STEP_CONSENT:
            ready = self._consent_check.isChecked()
        elif step == self._STEP_OLLAMA:
            ready = self._ollama_running and self._selected_model_name in self._installed_models
        elif step == self._STEP_CLOUD:
            ready = bool(self._api_key.text().strip() and self._cloud_model.text().strip())
        elif step in (self._STEP_TEST, self._STEP_DONE):
            ready = self._tested is not None and self._tested == self._signature()
        self._next_btn.setEnabled(ready and not self._busy)
        self._check_btn.setEnabled(not self._busy)
        self._key_btn.setEnabled(bool(self._api_key.text().strip()) and not self._busy)
        self._test_btn.setEnabled(not self._busy)

    def _go_next(self):
        if not self._next_btn.isEnabled():
            return
        step = self._current_step
        if step == self._STEP_CHECK:
            self._recommend()
        if step == self._STEP_CHOOSE:
            self._route = 'cloud' if self._cloud_choice.isChecked() else 'local'
            self._accepted = None
            self._consent_check.setChecked(False)
            self._tested = None
        if step == self._STEP_CHOOSE:
            self._go_to(self._STEP_CLOUD if self._route == 'cloud' else self._STEP_OLLAMA)
        elif step == self._STEP_CONSENT:
            self._accepted = (self._route, self._provider.currentData())
            self._go_to(getattr(self, '_consent_return', self._STEP_TEST))
        elif step in (self._STEP_OLLAMA, self._STEP_CLOUD):
            self._consent_return = self._STEP_TEST
            if self._ensure_consent():
                self._go_to(self._STEP_TEST)
        elif step == self._STEP_DONE:
            self._finish()
        else:
            self._go_to(step+1)

    def _go_back(self):
        step = self._current_step
        target = {self._STEP_CONSENT: self._STEP_CLOUD if self._route == 'cloud' else self._STEP_OLLAMA, self._STEP_OLLAMA: self._STEP_CHOOSE, self._STEP_CLOUD: self._STEP_CHOOSE,
                  self._STEP_TEST: self._STEP_CLOUD if self._route == 'cloud' else self._STEP_OLLAMA}.get(step, max(0, step-1))
        self._go_to(target)

    def _signature(self):
        return (self._route, self._provider.currentData(), self._api_key.text(), self._cloud_model.text(), self._selected_model_name)

    def _selection_text(self):
        return ('On this PC · ' + self._selected_model_name if self._route == 'local' else
                'In the cloud · ' + self._provider.currentText() + ' · ' + self._cloud_model.text())

    def _model_changed(self, name):
        self._selected_model_name = name
        self._tested = None
        self._refresh_model_status()

    def _refresh_model_status(self):
        installed = self._ollama_running and self._selected_model_name in self._installed_models
        self._model_status_lbl.setText('Model installed. Ready for a speed test.' if installed else COPY['model_missing'])
        self._pull_btn.setVisible(not installed)
        self._pull_btn.setEnabled(self._ollama_running and not self._pulling)
        self._nav()

    def _check_ollama(self):
        if self._busy:
            return
        self._ollama_status_lbl.setText('Checking Ollama…')
        self._work('ollama', lambda: _ping_ollama(host=(self._app.config.get('ollama') or {}).get('host') or _OLLAMA_URL))

    def _on_ollama_status(self, running, models):
        self._ollama_running = running
        self._installed_models = models if running else []
        self._ollama_status_lbl.setText('Ollama is running. Choose a model below.' if running else COPY['ollama_missing'])
        self._download_ollama_btn.setVisible(not running)
        if running:
            self._ollama_recheck_timer.stop()
            for model in models:
                if self._model_combo.findText(model) < 0:
                    self._model_combo.addItem(model)
        self._refresh_model_status()

    def _on_pull_line(self, line):
        if not self._closed:
            self._pull_progress.setText(line)

    def _on_pull_done(self, success):
        if self._closed:
            return
        self._pulling = False
        self._pull_btn.setText('Download model')
        self._pull_progress.setText('Download complete. Checking the model…' if success else COPY['download_failed'])
        if success:
            self._check_ollama()
        self._refresh_model_status()

    def _toggle_key(self):
        visible = self._api_key.echoMode() == QLineEdit.EchoMode.Password
        self._api_key.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self._show_key.setText('Hide' if visible else 'Show')

    def _provider_changed(self, *_args):
        self._api_key.clear()
        self._custom_mode = False
        self._cloud_catalogue = _load_models(self._provider.currentData())
        self._cloud_model.setText(ai_models.SNAPSHOT[self._provider.currentData()]['default'])
        self._fill_model_choices()
        if self._picker:
            self._picker.hide()
            self._picker.deleteLater()
            self._picker = None
        # Consent names the selected destination; changing it requires review again.
        self._accepted = None
        self._cloud_changed()

    def _cloud_changed(self, *_args):
        self._tested = None
        self._cloud_status.setText('Review permission for this provider before testing.' if self._accepted is None else 'Test this key and model before you finish.')
        self._nav()

    def _open_picker(self):
        if self._picker is None:
            provider = self._provider.currentData()
            self._picker = QDialog(self)
            self._picker.setWindowTitle('Choose a ' + self._provider.currentText() + ' model')
            layout = QVBoxLayout(self._picker)
            self._catalogue = _make_provider_picker(self._cloud_model, provider,
                lambda: self._provider.currentData() == provider and not self._closed,
                self._cloud_catalogue, self._picker_refresh, self._custom_model, self._picker_model_chosen)
            self._catalogue.choose.clicked.connect(self._picker.hide)
            self._catalogue.models.itemActivated.connect(lambda _item: self._picker.hide())
            self._catalogue.search.setMinimumHeight(theme.HIT_TARGET_MIN)
            self._catalogue.models.setMinimumHeight(120)
            layout.addWidget(self._catalogue)
            layout.addWidget(self._button('Done', self._picker.hide))
            self._picker.resize(680, 520)
        self._picker.show()
        self._catalogue.receive(self._cloud_catalogue)

    def _draft(self):
        if self._accepted != (self._route, self._provider.currentData()):
            return None
        cfg = deepcopy(self._app.config)
        cfg.setdefault('ollama', {}).update(model=self._selected_model_name)
        cfg.setdefault('ava_command_session', {}).update(model=self._selected_model_name)
        cfg.setdefault('cloud_llm', {}).update(provider=self._provider.currentData(), api_key=self._api_key.text().strip(), model=self._cloud_model.text().strip())
        record = ava_consent_qt.accepted_consent(cfg, cloud_enabled=self._route == 'cloud')
        return ava_consent_qt.apply_accepted_ava(self._app, record, policy=self._route, config=cfg, persist=False)

    def _ensure_consent(self):
        if self._accepted != (self._route, self._provider.currentData()):
            self._consent_check.setChecked(False)
            self._go_to(self._STEP_CONSENT)
            return False
        return True

    def _check_key(self):
        self._consent_return = self._STEP_CLOUD
        if self._busy or not self._api_key.text().strip() or not self._ensure_consent():
            return
        cfg = self._draft()
        self._cloud_status.setText('Checking key…')
        def run():
            app = SimpleNamespace(config=cfg)
            if cfg['cloud_llm']['provider'] == 'openrouter':
                result = cloud_llm.check_openrouter(app)
                failure = None if result.failure is None else result.failure.kind
            else:
                ok, info = cloud_llm.check_available(app)
                reasons = {'The key was rejected.': 'bad_key', 'The account is out of balance.': 'out_of_credit',
                           'Could not be reached.': 'unreachable', 'Did not answer in time.': 'timeout',
                           'The model or provider route is unavailable.': 'model_unavailable'}
                failure = None if ok else reasons.get(info, 'provider_error')
            if failure is not None:
                return failure
            return _load_models(cfg['cloud_llm']['provider'], refresh=True, api_key=cfg['cloud_llm']['api_key'])
        self._work('key', run)

    def _test_route(self):
        self._consent_return = self._STEP_TEST
        if self._busy or not self._ensure_consent():
            return
        self._tested = None
        cfg = self._draft()
        self._trial_config = cfg
        self._test_status.setText('Waiting for the selected model…')
        def run():
            if self._route == 'local':
                return hardware_check.measure_local(cfg)
            cfg['cloud_llm'].update(timeout_seconds=10, max_tokens=32)
            reply = cloud_llm.send('Reply only with the word ready.', 'Setup test.', SimpleNamespace(config=cfg), timeout=10)
            if isinstance(reply, cloud_llm.CloudError):
                return reply.failure.kind
            return True if isinstance(reply, str) and reply.strip() and not reply.startswith('Error:') else 'provider_error'
        self._work('test', run)

    def _work(self, kind, action):
        self._generation += 1
        token = self._generation
        self._job_signature = self._signature()
        self._job_config = deepcopy(self._app.config)
        self._busy = True
        self._pages[self._current_step].setEnabled(False)
        self._nav()
        def run():
            try:
                result = action()
            except Exception:
                logger.exception('[AVA-GUIDE] %s job failed', kind)
                result = 'exception'
            self._result.emit(token, kind, result)
        thread_registry.spawn('ava-guide-' + kind, run, daemon=True)

    def _receive(self, token, kind, value):
        if token != self._generation or self._closed:
            return
        self._busy = False
        for page in self._pages:
            page.setEnabled(True)
        if self._job_signature != self._signature() or self._job_config != self._app.config:
            self._test_status.setText(COPY['changed'])
            self._tested = None
            self._nav()
            return
        errors = {'bad_key': 'bad_key', 'out_of_credit': 'no_credit', 'unreachable': 'network',
                  'timeout': 'network', 'model_unavailable': 'model_unavailable', 'provider_down': 'provider_error',
                  'rate_limited': 'rate_limited', 'blocked': 'blocked', 'error': 'provider_error'}
        if kind == 'hardware':
            if isinstance(value, hardware_check.Hardware):
                self._fill_hardware(value)
            else:
                self._hardware_status.setText(COPY['check_failed'])
        elif kind == 'ollama':
            self._on_ollama_status(*(value if isinstance(value, tuple) else (False, [])))
        elif kind == 'key':
            if isinstance(value, openrouter_catalog.Catalogue):
                self._cloud_catalogue = value
                self._fill_model_choices()
                if self._picker:
                    self._catalogue.receive(value)
                value = None
            self._cloud_status.setText(COPY['key_good'] if value is None else COPY[errors.get(value, 'provider_error')])
        elif kind == 'test':
            if value is True or type(value) in (float, int):
                self._tested = self._signature()
                message = COPY['cloud_success'] if value is True else COPY['local_success' if value >= 10 else 'local_slow'] + f' ({value:.1f} tokens/second.)'
            else:
                message = COPY['local_timeout'] if self._route == 'local' else COPY[errors.get(value, 'provider_error')]
            self._test_status.setText(message)
            self._test_btn.setText('Run again' if self._tested else 'Retry test')
        self._nav()

    def _finish(self):
        if self._tested != self._signature() or self._app.config != self._job_config:
            self._tested = None
            self._save_status.setText(COPY['changed'])
            self._nav()
            return
        try:
            cfg = self._draft()
            ava_consent_qt.apply_accepted_ava(self._app, cfg['ava']['consent'], policy=self._route, config=cfg)
        except Exception:
            self._save_status.setText(COPY['save_failed'])
            return
        self.close()

    def resizeEvent(self, event):
        if hasattr(self, '_nav_layout'):
            compact = self.height() < 540
            for page in self._pages:
                page.layout().setContentsMargins(24 if compact else 28, 16 if compact else 24,
                                                24 if compact else 28, 12 if compact else 20)
                page.layout().setSpacing(6 if compact else 12)
            self._nav_layout.setContentsMargins(24, 6 if compact else 12, 24, 6 if compact else 12)
        super().resizeEvent(event)

    def showEvent(self, event):
        self._closed = False
        super().showEvent(event)

    def reject(self):
        self.close()

    def closeEvent(self, event):
        self._closed = True
        self._generation += 1
        self._busy = False
        self._tested = None
        self._accepted = None
        self._consent_check.setChecked(False)
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_key.setText('Show')
        self._ollama_recheck_timer.stop()
        for page in self._pages:
            page.setEnabled(True)
        if self._picker:
            self._picker.hide()
        self.hide()
        event.ignore()


    def _open_ollama_download(self):
        QDesktopServices.openUrl(QUrl(_OLLAMA_DOWNLOAD))
        self._ollama_recheck_attempts = 0
        self._ollama_recheck_timer.start()

    def _auto_recheck_ollama(self):
        if self._ollama_recheck_attempts >= _OLLAMA_RECHECK_LIMIT:
            self._ollama_recheck_timer.stop()
            return
        self._ollama_recheck_attempts += 1
        self._check_ollama()
        if self._ollama_recheck_attempts >= _OLLAMA_RECHECK_LIMIT:
            self._ollama_recheck_timer.stop()

    def _pull_model(self):
        if self._pulling:
            return
        model = self._selected_model_name
        self._pulling = True
        self._pull_btn.setEnabled(False)
        self._pull_btn.setText("Pulling...")
        self._pull_progress.setVisible(True)
        self._pull_progress.setText("Starting download...")
        self._model_status_dot.setStyleSheet(f"color:{theme.ACCENT};font-size:{theme.TYPE_BODY}px;")
        self._model_status_lbl.setText(f"Downloading {model}...")

        def _run():
            success = False
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", model],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    ),
                )
                self._pull_proc = proc
                for raw_line in proc.stdout:
                    line = _clean_ollama_output(raw_line)
                    if line:
                        self._pull_line_sig.emit(line)
                proc.wait()
                success = proc.returncode == 0
            except FileNotFoundError:
                logger.exception("Ava model pull failed: ollama was not found on PATH")
                self._pull_line_sig.emit(
                    "Couldn't download the model: Ollama was not found on PATH."
                )
            except Exception:
                logger.exception("Ava model pull failed")
                self._pull_line_sig.emit(
                    "Couldn't download the model: Ollama could not start the download."
                )
            finally:
                self._pull_proc = None
                self._pulling = False
            self._pull_done_sig.emit(success)

        thread_registry.spawn("ava-pull", _run, daemon=True)

def _ava_intro_text(config: dict) -> str:
    """Describe authoritative Ava policy without guessing provider readiness."""
    state = ai_preferences.effective_state(config, "ava", ready=None)
    # OWNER COPY: replace this state-derived provider explanation.
    return f"Ava is optional. {state.reason} Check Settings > Ava for the current choice."


def _confirmation_text() -> str:
    """Describe model-proposed actions without promising every UI action asks."""
    from samsara.session_modes import SCRATCH_THAT_PHRASE  # noqa: PLC0415
    # OWNER COPY: explain the exact model-proposed action policy.
    return ("Ava may ask before a model-proposed action. Review the action, then say "
            "<b>\"yes\"</b> to continue or <b>\"ava cancel\"</b> / "
            f"<b>\"{SCRATCH_THAT_PHRASE}\"</b> to drop it.")


def _keep_warm_note(config: dict) -> str:
    """Describe the warm request without claiming Ollama memory residency."""
    keep_warm = bool((config.get("ava_command_session") or {}).get("keep_warm", True))
    # OWNER COPY: explain request and server retention separately.
    if keep_warm:
        return ("Keep model warm is on in Settings > Ava > Advanced. Samsara requests warming; "
                "Ollama controls how long the model stays loaded.")
    return ("Keep model warm is off in Settings > Ava > Advanced. "
            "Ollama controls how long the model stays loaded.")


def _usage_lines(config: dict, wake: str) -> list:
    """Show supported controls without promising a legacy provider switch."""
    from samsara import session_modes  # noqa: PLC0415
    ava_mode = next((p for p, m in session_modes._WHOLE_UTTERANCE_SWITCHES.items()
                     if m is session_modes.SessionMode.AVA), "ava mode")
    key = config.get("ava_mode_key", "right_alt")
    key_label = next((label for label, value in _AVA_KEY_OPTIONS if value == key), str(key))
    # OWNER COPY: qualify these controls against live Ava and Wake readiness.
    return [
        f"Ava key: {key_label}. Its availability follows Settings > Ava.",
        f'If Ava is enabled, say "{ava_mode}" inside a hands-free session to switch lanes.',
        "Use an active command route for Ava questions and cancellation.",
    ]


def _label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
    return lbl


def _body(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setWordWrap(True)
    lbl.setTextFormat(Qt.TextFormat.RichText)
    lbl.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;line-height:1.5;")
    return lbl


def _small_bold(text: str, color: str | None = None) -> QLabel:
    # None, not the token itself: a default argument is evaluated when the
    # def runs, so a token there keeps the palette that was live at import
    # (queue 129).
    if color is None:
        color = theme.TEXT_PRIMARY
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{color};font-size:{theme.TYPE_MIN}px;font-weight:bold;letter-spacing:0.5px;"
    )
    return lbl
