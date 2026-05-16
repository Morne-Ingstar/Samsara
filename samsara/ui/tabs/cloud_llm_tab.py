"""Cloud LLM settings tab — configure Ava's cloud AI backend."""

import threading
import tkinter as tk

import customtkinter as ctk


_PROVIDERS = [
    ("DeepSeek (default)", "deepseek"),
    ("OpenAI",             "openai"),
    ("Anthropic",          "anthropic"),
]
_PROVIDER_DISPLAY = [p[0] for p in _PROVIDERS]
_DISPLAY_TO_CODE  = {p[0]: p[1] for p in _PROVIDERS}
_CODE_TO_DISPLAY  = {p[1]: p[0] for p in _PROVIDERS}

_DEFAULT_MODELS = {
    "deepseek":  "deepseek-chat",
    "openai":    "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}


class CloudLLMTab:
    """Settings tab for Ava's cloud LLM provider."""

    def __init__(self, parent_frame, app):
        self.parent = parent_frame
        self.app = app
        self._built = False

        self.enabled_var      = None
        self.provider_var     = None
        self.api_key_var      = None
        self.model_var        = None
        self.timeout_var      = None
        self._show_key        = False
        self._key_entry       = None
        self._status_label    = None
        self._model_hint      = None

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self):
        scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        scroll.pack(fill='both', expand=True)

        cfg = self.app.config.get("cloud_llm", {})

        # ── Enable section ─────────────────────────────────────────────
        ctk.CTkLabel(scroll, text="Cloud LLM",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        enable_frame = ctk.CTkFrame(scroll, corner_radius=10)
        enable_frame.pack(fill='x', pady=(0, 20))

        self.enabled_var = tk.BooleanVar(value=bool(cfg.get("enabled", False)))
        ctk.CTkCheckBox(
            enable_frame,
            text="Enable cloud LLM (Ava routes requests to the cloud provider)",
            variable=self.enabled_var,
        ).pack(anchor='w', padx=15, pady=(15, 8))

        ctk.CTkLabel(
            enable_frame,
            text="When enabled, your voice requests are sent to the selected provider.\n"
                 "Requires your own API key. Falls back to local Ollama on error.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
            justify='left',
        ).pack(anchor='w', padx=15, pady=(0, 15))
        yield

        # ── Provider section ───────────────────────────────────────────
        ctk.CTkLabel(scroll, text="Provider",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        provider_frame = ctk.CTkFrame(scroll, corner_radius=10)
        provider_frame.pack(fill='x', pady=(0, 20))

        current_provider = cfg.get("provider", "deepseek")
        self.provider_var = tk.StringVar(
            value=_CODE_TO_DISPLAY.get(current_provider, _PROVIDER_DISPLAY[0])
        )

        prow = ctk.CTkFrame(provider_frame, fg_color="transparent")
        prow.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(prow, text="Provider:", width=90, anchor='w').pack(side='left')
        provider_combo = ctk.CTkComboBox(
            prow,
            variable=self.provider_var,
            values=_PROVIDER_DISPLAY,
            width=220,
            state='readonly',
            command=self._on_provider_changed,
        )
        provider_combo.pack(side='left')

        # API Key
        key_row = ctk.CTkFrame(provider_frame, fg_color="transparent")
        key_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(key_row, text="API Key:", width=90, anchor='w').pack(side='left')

        self.api_key_var = tk.StringVar(value=cfg.get("api_key", ""))
        self._key_entry = ctk.CTkEntry(
            key_row,
            textvariable=self.api_key_var,
            width=300,
            show="*",
            placeholder_text="Paste your API key here",
        )
        self._key_entry.pack(side='left', padx=(0, 8))

        ctk.CTkButton(
            key_row, text="Show", width=60,
            command=self._toggle_key_visibility,
        ).pack(side='left')

        ctk.CTkLabel(
            provider_frame,
            text="Your API key is stored locally in config.json and never uploaded.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(anchor='w', padx=15, pady=(0, 10))

        # Model override
        model_row = ctk.CTkFrame(provider_frame, fg_color="transparent")
        model_row.pack(fill='x', padx=15, pady=(0, 5))
        ctk.CTkLabel(model_row, text="Model:", width=90, anchor='w').pack(side='left')
        self.model_var = tk.StringVar(value=cfg.get("model", ""))
        ctk.CTkEntry(
            model_row,
            textvariable=self.model_var,
            width=220,
            placeholder_text="Leave blank to use provider default",
        ).pack(side='left')

        self._model_hint = ctk.CTkLabel(
            provider_frame,
            text=self._model_hint_text(),
            text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self._model_hint.pack(anchor='w', padx=15, pady=(0, 15))
        self.provider_var.trace_add('write', self._update_model_hint)
        yield

        # ── Connection section ─────────────────────────────────────────
        ctk.CTkLabel(scroll, text="Connection",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(0, 10))

        conn_frame = ctk.CTkFrame(scroll, corner_radius=10)
        conn_frame.pack(fill='x', pady=(0, 20))

        timeout_row = ctk.CTkFrame(conn_frame, fg_color="transparent")
        timeout_row.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(timeout_row, text="Timeout (s):", width=110, anchor='w').pack(side='left')
        self.timeout_var = tk.IntVar(value=int(cfg.get("timeout_seconds", 30)))
        ctk.CTkEntry(timeout_row, textvariable=self.timeout_var, width=70).pack(side='left')

        test_row = ctk.CTkFrame(conn_frame, fg_color="transparent")
        test_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkButton(
            test_row, text="Test Connection", width=150,
            command=self._test_connection,
        ).pack(side='left')
        self._status_label = ctk.CTkLabel(
            test_row, text="", text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self._status_label.pack(side='left', padx=(12, 0))

        self._built = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _on_provider_changed(self, _value=None):
        self._update_model_hint()

    def _update_model_hint(self, *_):
        if self._model_hint is None:
            return
        try:
            self._model_hint.configure(text=self._model_hint_text())
        except Exception:
            pass

    def _model_hint_text(self):
        display = self.provider_var.get() if self.provider_var else _PROVIDER_DISPLAY[0]
        code = _DISPLAY_TO_CODE.get(display, "deepseek")
        default = _DEFAULT_MODELS.get(code, "")
        return f"Default for {display.split(' ')[0]}: {default}"

    def _toggle_key_visibility(self):
        self._show_key = not self._show_key
        if self._key_entry:
            self._key_entry.configure(show="" if self._show_key else "*")

    def _test_connection(self):
        if self._status_label is None:
            return
        self._status_label.configure(text="Testing...", text_color="gray")

        # Build a temporary app-like config snapshot so cloud_llm can use it
        api_key = self.api_key_var.get().strip() if self.api_key_var else ""
        provider_display = self.provider_var.get() if self.provider_var else _PROVIDER_DISPLAY[0]
        provider = _DISPLAY_TO_CODE.get(provider_display, "deepseek")

        if not api_key:
            self._status_label.configure(text="No API key entered.", text_color="orange")
            return

        class _FakeApp:
            config = {
                "cloud_llm": {
                    "enabled": True,
                    "api_key": api_key,
                    "provider": provider,
                    "timeout_seconds": 5,
                }
            }

        def _do_test():
            from samsara import cloud_llm
            ok, info = cloud_llm.check_available(_FakeApp())
            def _update():
                if ok:
                    self._status_label.configure(
                        text=f"Connected to {provider}.", text_color="#66FF66")
                else:
                    self._status_label.configure(
                        text=f"Failed: {info}", text_color="#FF6666")
            try:
                self.parent.after(0, _update)
            except Exception:
                pass

        threading.Thread(target=_do_test, daemon=True).start()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        if not self._built:
            return

        provider_display = self.provider_var.get() if self.provider_var else _PROVIDER_DISPLAY[0]
        provider = _DISPLAY_TO_CODE.get(provider_display, "deepseek")
        api_key = self.api_key_var.get().strip() if self.api_key_var else ""
        model_override = self.model_var.get().strip() if self.model_var else ""
        try:
            timeout = int(self.timeout_var.get())
        except (ValueError, tk.TclError):
            timeout = 30

        cfg = dict(self.app.config.get("cloud_llm", {}) or {})
        cfg["enabled"]         = bool(self.enabled_var.get()) if self.enabled_var else False
        cfg["provider"]        = provider
        cfg["api_key"]         = api_key
        cfg["timeout_seconds"] = timeout
        if model_override:
            cfg["model"] = model_override
        elif "model" in cfg:
            del cfg["model"]

        self.app.update_config({"cloud_llm": cfg}, save=False)
