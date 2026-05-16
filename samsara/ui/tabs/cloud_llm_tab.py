"""Cloud LLM settings tab — premium license gate + cloud AI configuration."""

import threading
import tkinter as tk

import customtkinter as ctk

from samsara import premium


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
    """Settings tab for Ava's cloud LLM provider, gated by premium license."""

    def __init__(self, parent_frame, app):
        self.parent = parent_frame
        self.app    = app
        self._built = False

        # License UI
        self._license_key_var    = None
        self._license_key_entry  = None
        self._license_status_lbl = None
        self._licensed_section   = None  # CTkFrame shown only when licensed
        self._unlicensed_section = None  # CTkFrame shown only when unlicensed

        # Cloud settings UI (inside _licensed_section)
        self.enabled_var     = None
        self.provider_var    = None
        self.api_key_var     = None
        self.model_var       = None
        self.timeout_var     = None
        self._show_api_key   = False
        self._api_key_entry  = None
        self._status_label   = None
        self._model_hint     = None

    # ------------------------------------------------------------------
    # Build (generator)
    # ------------------------------------------------------------------

    def build(self):
        self._scroll = ctk.CTkScrollableFrame(self.parent, fg_color="transparent")
        self._scroll.pack(fill='both', expand=True)

        self._build_license_section(self._scroll)
        yield

        self._build_unlicensed_section(self._scroll)
        yield

        self._build_licensed_section(self._scroll)
        yield

        self._refresh_license_state()
        self._built = True

    # ------------------------------------------------------------------
    # License header — always visible
    # ------------------------------------------------------------------

    def _build_license_section(self, parent):
        ctk.CTkLabel(parent, text="Cloud AI — Premium Feature",
                     font=ctk.CTkFont(size=16, weight="bold")
                     ).pack(anchor='w', pady=(15, 10))

        hdr = ctk.CTkFrame(parent, corner_radius=10)
        hdr.pack(fill='x', pady=(0, 16))

        key = premium.get_license_key(self.app)
        has_license = premium.validate_key(key)

        if has_license:
            row = ctk.CTkFrame(hdr, fg_color="transparent")
            row.pack(fill='x', padx=15, pady=(12, 4))
            ctk.CTkLabel(row, text="Premium license active",
                         text_color="#66FF66",
                         font=ctk.CTkFont(size=13, weight="bold")
                         ).pack(side='left')

            masked = premium.masked_key(key)
            ctk.CTkLabel(hdr, text=masked,
                         text_color="gray",
                         font=ctk.CTkFont(size=11)
                         ).pack(anchor='w', padx=15, pady=(0, 8))

            ctk.CTkButton(hdr, text="Remove License", width=140,
                          fg_color="gray40", hover_color="gray30",
                          command=self._remove_license
                          ).pack(anchor='w', padx=15, pady=(0, 12))
        else:
            # License entry row
            key_row = ctk.CTkFrame(hdr, fg_color="transparent")
            key_row.pack(fill='x', padx=15, pady=(15, 8))
            ctk.CTkLabel(key_row, text="License Key:", width=100, anchor='w'
                         ).pack(side='left')
            self._license_key_var = tk.StringVar()
            self._license_key_entry = ctk.CTkEntry(
                key_row,
                textvariable=self._license_key_var,
                width=260,
                placeholder_text="SAMSARA-XXXX-XXXX-XXXX",
            )
            self._license_key_entry.pack(side='left', padx=(0, 8))
            ctk.CTkButton(key_row, text="Activate", width=90,
                          command=self._activate_license
                          ).pack(side='left')

            self._license_status_lbl = ctk.CTkLabel(
                hdr, text="", font=ctk.CTkFont(size=11))
            self._license_status_lbl.pack(anchor='w', padx=15, pady=(0, 4))

            ctk.CTkLabel(
                hdr,
                text="Get a license at morneis.com/samsara/premium",
                text_color="#5EEAD4",
                font=ctk.CTkFont(size=11),
                cursor="hand2",
            ).pack(anchor='w', padx=15, pady=(0, 12))

    # ------------------------------------------------------------------
    # Unlicensed explanation — hidden once licensed
    # ------------------------------------------------------------------

    def _build_unlicensed_section(self, parent):
        self._unlicensed_section = ctk.CTkFrame(parent, corner_radius=10)

        body = (
            "Samsara is free. Every voice command, dictation feature, plugin, and the "
            "local AI assistant are yours with no restrictions, no trial period, no nag screens.\n\n"
            "Cloud AI connects Ava to larger language models for more capable conversations "
            "and smarter command interpretation. Revenue from Cloud AI licenses helps fund "
            "Samsara's continued development as a free accessibility tool.\n\n"
            "If you need Samsara for accessibility and genuinely cannot afford a license, "
            "get in touch at morneis.com/samsara/business — we’ll work something out."
        )
        ctk.CTkLabel(
            self._unlicensed_section,
            text=body,
            wraplength=500,
            justify='left',
            text_color="gray",
            font=ctk.CTkFont(size=12),
        ).pack(anchor='w', padx=15, pady=15)

    # ------------------------------------------------------------------
    # Licensed settings — hidden until licensed
    # ------------------------------------------------------------------

    def _build_licensed_section(self, parent):
        self._licensed_section = ctk.CTkFrame(parent, fg_color="transparent")

        cfg = self.app.config.get("cloud_llm", {})

        # ── Enable ────────────────────────────────────────────────────
        ctk.CTkLabel(self._licensed_section, text="Cloud LLM",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(anchor='w', pady=(0, 8))

        enable_frame = ctk.CTkFrame(self._licensed_section, corner_radius=10)
        enable_frame.pack(fill='x', pady=(0, 16))

        self.enabled_var = tk.BooleanVar(value=bool(cfg.get("enabled", False)))
        ctk.CTkCheckBox(
            enable_frame,
            text="Enable cloud LLM (Ava routes requests to the cloud provider)",
            variable=self.enabled_var,
        ).pack(anchor='w', padx=15, pady=(15, 8))
        ctk.CTkLabel(
            enable_frame,
            text="When enabled, voice requests are sent to the selected provider. "
                 "Falls back to local Ollama on error.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
            justify='left',
        ).pack(anchor='w', padx=15, pady=(0, 12))

        # ── Provider ──────────────────────────────────────────────────
        ctk.CTkLabel(self._licensed_section, text="Provider",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(anchor='w', pady=(0, 8))

        prov_frame = ctk.CTkFrame(self._licensed_section, corner_radius=10)
        prov_frame.pack(fill='x', pady=(0, 16))

        current_provider = cfg.get("provider", "deepseek")
        self.provider_var = tk.StringVar(
            value=_CODE_TO_DISPLAY.get(current_provider, _PROVIDER_DISPLAY[0])
        )

        prow = ctk.CTkFrame(prov_frame, fg_color="transparent")
        prow.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(prow, text="Provider:", width=90, anchor='w').pack(side='left')
        ctk.CTkComboBox(
            prow,
            variable=self.provider_var,
            values=_PROVIDER_DISPLAY,
            width=220,
            state='readonly',
            command=self._on_provider_changed,
        ).pack(side='left')

        key_row = ctk.CTkFrame(prov_frame, fg_color="transparent")
        key_row.pack(fill='x', padx=15, pady=(0, 10))
        ctk.CTkLabel(key_row, text="API Key:", width=90, anchor='w').pack(side='left')
        self.api_key_var = tk.StringVar(value=cfg.get("api_key", ""))
        self._api_key_entry = ctk.CTkEntry(
            key_row,
            textvariable=self.api_key_var,
            width=300,
            show="*",
            placeholder_text="Paste your API key here",
        )
        self._api_key_entry.pack(side='left', padx=(0, 8))
        ctk.CTkButton(key_row, text="Show", width=60,
                      command=self._toggle_api_key_visibility
                      ).pack(side='left')

        ctk.CTkLabel(
            prov_frame,
            text="Your API key is stored locally in config.json and never uploaded.",
            text_color="gray",
            font=ctk.CTkFont(size=11),
        ).pack(anchor='w', padx=15, pady=(0, 8))

        model_row = ctk.CTkFrame(prov_frame, fg_color="transparent")
        model_row.pack(fill='x', padx=15, pady=(0, 4))
        ctk.CTkLabel(model_row, text="Model:", width=90, anchor='w').pack(side='left')
        self.model_var = tk.StringVar(value=cfg.get("model", ""))
        ctk.CTkEntry(
            model_row,
            textvariable=self.model_var,
            width=220,
            placeholder_text="Leave blank to use provider default",
        ).pack(side='left')

        self._model_hint = ctk.CTkLabel(
            prov_frame,
            text=self._model_hint_text(),
            text_color="gray",
            font=ctk.CTkFont(size=11),
        )
        self._model_hint.pack(anchor='w', padx=15, pady=(0, 12))
        self.provider_var.trace_add('write', self._update_model_hint)

        # ── Connection ────────────────────────────────────────────────
        ctk.CTkLabel(self._licensed_section, text="Connection",
                     font=ctk.CTkFont(size=14, weight="bold")
                     ).pack(anchor='w', pady=(0, 8))

        conn_frame = ctk.CTkFrame(self._licensed_section, corner_radius=10)
        conn_frame.pack(fill='x', pady=(0, 16))

        timeout_row = ctk.CTkFrame(conn_frame, fg_color="transparent")
        timeout_row.pack(fill='x', padx=15, pady=(15, 10))
        ctk.CTkLabel(timeout_row, text="Timeout (s):", width=110, anchor='w').pack(side='left')
        self.timeout_var = tk.IntVar(value=int(cfg.get("timeout_seconds", 30)))
        ctk.CTkEntry(timeout_row, textvariable=self.timeout_var, width=70).pack(side='left')

        test_row = ctk.CTkFrame(conn_frame, fg_color="transparent")
        test_row.pack(fill='x', padx=15, pady=(0, 15))
        ctk.CTkButton(test_row, text="Test Connection", width=150,
                      command=self._test_connection
                      ).pack(side='left')
        self._status_label = ctk.CTkLabel(
            test_row, text="", text_color="gray", font=ctk.CTkFont(size=11))
        self._status_label.pack(side='left', padx=(12, 0))

    # ------------------------------------------------------------------
    # License state switching
    # ------------------------------------------------------------------

    def _refresh_license_state(self):
        has_license = premium.is_premium(self.app)
        if has_license:
            if self._unlicensed_section:
                self._unlicensed_section.pack_forget()
            if self._licensed_section:
                self._licensed_section.pack(fill='x', pady=(0, 8))
        else:
            if self._licensed_section:
                self._licensed_section.pack_forget()
            if self._unlicensed_section:
                self._unlicensed_section.pack(fill='x', pady=(0, 16))

    def _activate_license(self):
        if not self._license_key_var:
            return
        key = self._license_key_var.get().strip()
        if not premium.validate_key(key):
            if self._license_status_lbl:
                self._license_status_lbl.configure(
                    text="Invalid key format. Expected: SAMSARA-XXXX-XXXX-XXXX",
                    text_color="#FF6666",
                )
            return
        premium.set_license_key(self.app, key)
        self.app.update_config({"premium_license": key}, save=True)
        self._rebuild_tab()

    def _remove_license(self):
        premium.set_license_key(self.app, "")
        # Also disable cloud mode when license removed
        cfg = dict(self.app.config.get("cloud_llm", {}) or {})
        cfg["enabled"] = False
        self.app.update_config({
            "premium_license": "",
            "cloud_llm": cfg,
        }, save=True)
        self._rebuild_tab()

    def _rebuild_tab(self):
        """Destroy and rebuild this tab's contents to reflect license change."""
        try:
            for child in self.parent.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._built = False
        self._unlicensed_section = None
        self._licensed_section = None
        # Re-run build as a one-shot (not staged — we're inside a callback)
        for _ in self.build():
            pass

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
        return f"Default for {display.split(' ')[0]}: {_DEFAULT_MODELS.get(code, '')}"

    def _toggle_api_key_visibility(self):
        self._show_api_key = not self._show_api_key
        if self._api_key_entry:
            self._api_key_entry.configure(show="" if self._show_api_key else "*")

    def _test_connection(self):
        if self._status_label is None:
            return
        self._status_label.configure(text="Testing...", text_color="gray")
        api_key = self.api_key_var.get().strip() if self.api_key_var else ""
        provider_display = self.provider_var.get() if self.provider_var else _PROVIDER_DISPLAY[0]
        provider = _DISPLAY_TO_CODE.get(provider_display, "deepseek")
        if not api_key:
            self._status_label.configure(text="No API key entered.", text_color="orange")
            return

        class _FakeApp:
            config = {"cloud_llm": {
                "enabled": True, "api_key": api_key,
                "provider": provider, "timeout_seconds": 5,
            }}

        def _do_test():
            from samsara import cloud_llm
            ok, info = cloud_llm.check_available(_FakeApp())
            msg = f"Connected to {provider}." if ok else f"Failed: {info}"
            color = "#66FF66" if ok else "#FF6666"
            try:
                self.parent.after(0, lambda: self._status_label.configure(
                    text=msg, text_color=color))
            except Exception:
                pass

        threading.Thread(target=_do_test, daemon=True).start()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        if not self._built or not premium.is_premium(self.app):
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
