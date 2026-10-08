"""Settings controller services, separate from window chrome."""
from samsara.ui.settings.shared_qt import *
from contextlib import nullcontext

class SettingsServices:
    def _refresh_sc_status(self) -> None:
        """Refresh the Smart Corrections "Active backend" status line --
        called on tab build and every time the Advanced tab is shown, since
        Ollama reachability can change between settings-window visits."""
        label = self._widgets.get('sc_status_label')
        if label is None:
            return
        try:
            backend = smart_corrections.describe_backend_status(self.app)
            _state, cloud = _cloud_ai_state(
                self.app, reachable=self._cloud_reachable
            )
            status = f"{backend} · {cloud}"
        except Exception as e:
            logger.debug(f"[SETTINGS] Smart Corrections status refresh failed: {e}")
            status = "unknown"
        label.setText(f"Active backend: {status}")

    def _refresh_sc_cloud_hint(self) -> None:
        """Keep the Cloud-backend warning aligned with the status line."""
        hint = self._widgets.get('sc_cloud_hint')
        backend = self._widgets.get('sc_backend')
        if hint is None or backend is None:
            return
        state, message = _cloud_ai_state(
            self.app, reachable=self._cloud_reachable
        )
        hint.setText(message)
        hint.setVisible(
            backend.currentText() == 'cloud' and state != 'enabled_reachable'
        )

    # ------------------------------------------------------------------
    # Search: live filter across all tabs
    # ------------------------------------------------------------------

    #: Buttons whose minimum width was a literal tuned against Segoe UI
    #: metrics. The literal stays as the touch-target FLOOR (never narrower
    #: than it renders today, per the 40px+ accessibility rule); the effective
    #: minimum is whichever is larger, the floor or the button's own sizeHint.
    _METRIC_MIN_WIDTH_BUTTONS = {
        "microphoneRefreshButton": 104,
    }

    def _apply_metric_minimum_widths(self) -> None:
        """Keep 44-DIP hit regions and derive button widths from font metrics.

        Runs AFTER the pages are in self._stack, which is when the window's
        stylesheet() cascade actually reaches them: a QPushButton's sizeHint()
        only includes the stylesheet's padding once it is parented and
        polished (bare 98px -> 146px styled, measured). Reading it inside the
        page builder returns the unstyled width, which is exactly the mistake
        the literals encoded -- they were correct for Segoe UI and clipped the
        label under any wider font stack.
        """
        for widget_type in (
            QAbstractButton, QComboBox, QLineEdit, QAbstractSpinBox,
            QSlider, QPlainTextEdit, QTableWidget,
        ):
            for widget in self.findChildren(widget_type):
                widget.setMinimumHeight(max(theme.HIT_TARGET_MIN, widget.minimumHeight()))
                widget.setMinimumWidth(max(theme.HIT_TARGET_MIN, widget.minimumWidth()))

        for object_name, floor_px in self._METRIC_MIN_WIDTH_BUTTONS.items():
            button = self.findChild(QPushButton, object_name)
            if button is None:
                continue
            button.ensurePolished()
            button.setMinimumWidth(max(floor_px, button.sizeHint().width()))

    def _build_search_registry(self) -> None:
        """Discover every setting row across all tabs by walking the widget
        tree the tab builders already constructed, instead of hand-listing
        one registration call at each of the ~60 _setting_row(...) call
        sites. Any future setting that uses _setting_row is covered
        automatically.

        Every _setting_row(...) call produces a distinctive shape: a
        QHBoxLayout whose first item is a QWidget (objectName
        "settingRowLabelColumn" -- hosting the label column in a real
        widget, not a bare addLayout(), is what gives its word-wrapped
        description a working height-for-width layout pass) whose OWN
        layout starts with a QLabel (the row label, optionally followed by
        a description QLabel), and whose remaining row items include the
        control widget. That shape is unique in this file, so walking for
        it can't pick up an unrelated QHBoxLayout (button rows, header
        rows, etc.) by accident.

        Two tabs -- Commands and Health -- build their content from
        QTableWidget-based editors rather than _setting_row, so they
        contribute zero rows here; _apply_search_filter() below leaves
        those tabs untouched by the filter rather than falsely marking
        them "no match".
        """
        self._search_rows = []
        for tab_index in range(self._stack.count()):
            tab_widget = self._stack.widget(tab_index)
            content = tab_widget.widget() if isinstance(tab_widget, QScrollArea) else tab_widget
            if content is None:
                continue
            for row in content.findChildren(QHBoxLayout):
                if row.count() < 2:
                    continue
                left_item = row.itemAt(0)
                left_widget = left_item.widget() if left_item is not None else None
                left_layout = left_widget.layout() if left_widget is not None else None
                if left_layout is None or left_layout.count() < 1:
                    continue
                label_item = left_layout.itemAt(0)
                label_widget = label_item.widget() if label_item is not None else None
                if not isinstance(label_widget, QLabel):
                    continue
                desc_item = left_layout.itemAt(1) if left_layout.count() > 1 else None
                desc_widget = desc_item.widget() if desc_item is not None else None
                if not isinstance(desc_widget, QLabel):
                    desc_widget = None
                # Responsive rows may place a stretch between the descriptive
                # column and the control. Find the first actual widget instead
                # of assuming the control is item 1.
                control_widget = None
                for item_index in range(1, row.count()):
                    control_item = row.itemAt(item_index)
                    candidate = (
                        control_item.widget() if control_item is not None else None
                    )
                    if candidate is not None:
                        control_widget = candidate
                        break
                if control_widget is None:
                    continue
                self._search_rows.append((
                    label_widget.text(),
                    desc_widget.text() if desc_widget is not None else "",
                    tab_index,
                    label_widget,
                    desc_widget,
                    control_widget,
                ))

    def _apply_search_filter(self) -> None:
        """Show/hide each registered row by case-insensitive substring match
        against its label (and description, since that's cheap to include
        too) and dim the sidebar entry for any tab left with zero matches.
        Empty query restores everything."""
        query = self._search_edit.text().strip().lower()

        if not query:
            for _label, _desc, _tab, label_w, desc_w, control_w in self._search_rows:
                label_w.setVisible(True)
                if desc_w is not None:
                    desc_w.setVisible(True)
                control_w.setVisible(True)
            self._set_sidebar_dimmed(set())
            return

        tabs_with_rows: set = set()
        matched_tabs: set = set()
        for label_text, desc_text, tab_index, label_w, desc_w, control_w in self._search_rows:
            tabs_with_rows.add(tab_index)
            match = query in label_text.lower() or query in desc_text.lower()
            label_w.setVisible(match)
            if desc_w is not None:
                desc_w.setVisible(match)
            control_w.setVisible(match)
            if match:
                matched_tabs.add(tab_index)

        self._set_sidebar_dimmed(tabs_with_rows - matched_tabs)

    def _set_sidebar_dimmed(self, no_match_stack_indices: set) -> None:
        """Dim -- not disable -- the sidebar entry for each tab in
        no_match_stack_indices. Chose dimming over QListWidgetItem
        disabling so every tab stays reachable even while filtered out:
        the label/description substring match is a hint, not ground
        truth (it won't catch a setting whose on-screen wording differs
        from what was typed), so locking navigation away from a tab could
        strand the user looking for something that's actually there."""
        dimmed = _css_color_to_qcolor(theme.TEXT_DISABLED)
        normal = QColor(theme.TEXT_PRIMARY)
        for row, stack_index in self._sidebar_row_to_stack_index.items():
            item = self._sidebar.item(row)
            if item is None:
                continue
            item.setForeground(dimmed if stack_index in no_match_stack_indices else normal)

    # ------------------------------------------------------------------
    # Tab builders
    # ------------------------------------------------------------------

    def _build_placeholder(self):
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label = QLabel("Coming soon — this tab is being migrated.")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_BODY}px;")
        layout.addWidget(label)
        return w

    def show_tab(self, name: str) -> None:
        """Show the window on the named tab (a _TAB_NAMES entry)."""
        stack_index = _TAB_NAMES.index(name)
        for row, index in self._sidebar_row_to_stack_index.items():
            if index == stack_index:
                self._sidebar.setCurrentRow(row)
                break
        self._stack.setCurrentIndex(stack_index)
        self.show()
        self.raise_()
        self.activateWindow()

    # Amber "may conflict" style, shared by the Modes tab collision banner.
    @staticmethod
    def _collision_warn_style() -> str:
        """See _HotkeyButton._idle_qss(): built per call, not per import."""
        return (            f"color: {theme.WARNING}; font-size: {theme.TYPE_BODY}px; "
            f"background-color: {theme.tint(theme.WARNING, 0.07)}; "
            f"border: 1px solid {theme.tint(theme.WARNING, 0.2)}; "
            "border-radius: 6px; padding: 6px 10px;"
        )

    # ------------------------------------------------------------------
    # Commands tab helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Sounds tab helpers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # TTS tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Alarms tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Ava / Cloud tab slots and helpers
    # ------------------------------------------------------------------

    def _on_test_result(self, msg: str, color: str):
        label = self._widgets.get('cloud_test_status')
        if label:
            label.setText(msg)
            label.setStyleSheet(f"color: {color}; font-size: {theme.TYPE_BODY}px;")
        if msg.startswith("Connected to "):
            self._cloud_reachable = True
        elif msg.startswith(("Failed:", "Error:")):
            self._cloud_reachable = False
        self._refresh_sc_status()
        self._refresh_sc_cloud_hint()

    def apply_theme(self) -> None:
        """Refresh this live window without rebuilding unsaved controls."""
        self.setStyleSheet(stylesheet())
        for widget in self.findChildren(QWidget):
            inline = widget.styleSheet()
            if inline:
                widget.setStyleSheet(theme.retheme_stylesheet(inline))
        for row in range(self._sidebar.count()):
            item = self._sidebar.item(row)
            if row in self._sidebar_row_to_stack_index:
                item.setForeground(QColor(theme.TEXT_PRIMARY))
            else:
                item.setForeground(QColor(theme.TEXT_PRIMARY))
        self._apply_search_filter()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _section_title(self, text):
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.ACCENT}; font-size: {theme.TYPE_HEADING}px; font-weight: bold;")
        return label

    def _section_card(self, title: str, subtitle: str | None = None):
        # No per-card max-width: the cap lives once at each tab's shared
        # scroll-content container (_CONTENT_MAX_WIDTH). Expanding here lets
        # the card fill whatever width that container gives it, so every
        # card in a tab shares the same left/right edges regardless of its
        # own content's natural size.
        card = QFrame()
        card.setObjectName("settingsSectionCard")
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        card.setStyleSheet(
            "QFrame#settingsSectionCard {"
            f" background-color: {theme.BG1};"
            f" border: 1px solid {theme.wash(0.12)};"
            " border-radius: 12px;"
            "}"
        )

        v = QVBoxLayout(card)
        v.setContentsMargins(18, 14, 18, 14)
        v.setSpacing(12)

        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: {theme.TYPE_HEADING}px; font-weight: 700;"
        )
        v.addWidget(title_label)

        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setWordWrap(True)
            subtitle_label.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
            v.addWidget(subtitle_label)
        return card, v

    def _setting_row(self, label, description, widget, control_width: int | None = 260):
        """Build a label+description row with a right-aligned control.

        control_width is a MINIMUM, not an exact width: widget gets
        setMinimumWidth(control_width) only (no maximum), with a
        Minimum/Fixed sizePolicy, so a control whose own content needs more
        room than the minimum (a QComboBox with long option text, a
        checkbox with a long trailing label) can grow to fit it instead of
        clipping. Every row still right-aligns its control within the row's
        own full width, so all controls in the same card share one right
        edge regardless of how wide any individual control's content makes
        it -- that alignment does not depend on the widths matching.
        """
        row = QHBoxLayout()
        row.setContentsMargins(0, 10, 0, 10)
        row.setSpacing(20)

        # The label column is hosted in a _HeightForWidthWidget rather than a
        # bare QVBoxLayout added via addLayout(). A word-wrapped QLabel's
        # height depends on its resolved width, and Qt's height-for-width
        # sizePolicy propagation does not reliably reach through this row's
        # full nesting depth (row -> card -> scroll content) even when every
        # level advertises hasHeightForWidth(True) -- see
        # _HeightForWidthWidget's docstring. It self-corrects its own
        # minimum height on resize instead, which works regardless of how
        # much nesting surrounds it.
        left_widget = _HeightForWidthWidget()
        left_widget.setObjectName("settingRowLabelColumn")
        left_widget.setStyleSheet(
            "QWidget#settingRowLabelColumn { background-color: transparent; }"
        )
        left = QVBoxLayout(left_widget)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(4)

        lbl = QLabel(label)
        lbl.setStyleSheet(f"font-weight: 600; font-size: {theme.TYPE_BODY}px; color: {theme.TEXT_PRIMARY};")
        left.addWidget(lbl)

        if description:
            desc = QLabel(description)
            desc.setStyleSheet(f"color: {theme.TEXT_SECONDARY}; font-size: {theme.TYPE_BODY}px;")
            desc.setWordWrap(True)
            left.addWidget(desc)

        # No addStretch() here: left_widget already carries the row's only
        # stretch factor (1), and the control widget below is fixed-width,
        # so an extra stretch item only competed with the label column for
        # the same leftover space without adding anything.
        row.addWidget(left_widget, 1)

        interactive_height = theme.HIT_TARGET_MIN
        if isinstance(widget, QCheckBox):
            widget.setMinimumHeight(theme.HIT_TARGET_MIN)
        elif isinstance(widget, (QPushButton, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QSlider)):
            widget.setMinimumHeight(interactive_height)
        else:
            widget.setMinimumHeight(interactive_height)

        if control_width is not None:
            width = max(theme.HIT_TARGET_MIN, 170, control_width)
            widget.setMinimumWidth(width)

        widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        row.addWidget(widget, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return row

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _replace_tts_runtime(self, requested: dict) -> None:
        """Delegate while Apply holds the shared runtime/config lock."""
        from samsara.tts.runtime import rebuild_tts_runtime
        rebuild_tts_runtime(self.app, requested)

    def _apply_and_close(self):
        """Collect updates from each tab's registered save fn, in
        registration order (== tab-build order in __init__), merge into one
        dict, then a single locked config.update + save_config.

        Registration order preserves the merge semantics tabs depend on:
        Modes (command_mode mode/debounce/timeout/miss_limit/button/suppress_button,
        wake_word_config wake_command_timeout/quick_silence/oww_threshold,
        ava_command_session) is registered before Advanced (wake_word_config
        manual speech_threshold) -- Advanced's fn reads the accumulated
        `updates` dict to merge onto the Modes tab's partial write instead
        of clobbering it.
        """
        updates: dict = {}
        for save_fn in self._save_fns:
            updates.update(save_fn(updates))
        if hasattr(self, '_config_at_open'):
            from samsara.settings_merge import merge_outside_changes
            updates, kept = merge_outside_changes(
                self._config_at_open, self.app.config, updates)
            if kept:
                logger.info('[SETTINGS] kept changes made outside this window: %s', ', '.join(kept))

        # Switch the live stream before the bulk config write. If the new ID
        # were written first, switch_microphone() would hit its same-ID guard
        # and ACE would continue recording from the old device.
        requested_mic = updates.get('microphone')
        current_mic = self.app.config.get('microphone')
        if 'microphone' in updates and requested_mic != current_mic:
            updates.pop('microphone', None)
            updates.pop('microphone_name', None)
            self.app.switch_microphone(requested_mic)


        requested_output = updates.get('output_device')
        requested_output_name = updates.get('output_device_name')
        current_output = (
            self.app.config.get('output_device'),
            self.app.config.get('output_device_name'),
        )
        if 'output_device' in updates and (
            requested_output, requested_output_name
        ) != current_output:
            self.app.switch_output_device(requested_output, requested_output_name)
        was_sc_enabled = bool(
            self.app.config.get('smart_corrections', {}).get('enabled', False)
        )
        was_ai_pack_enabled = bool(
            (self.app.config.get('command_packs', {}) or {}).get('ai', False)
        )

        from samsara.tts.runtime import tts_runtime_lock
        with tts_runtime_lock(self.app):
            old_tts = self.app.config.get('tts', {}) or {}
            requested_tts = updates.get('tts', old_tts) or {}
            replace_tts = ('tts' in updates and (
                bool(old_tts.get('enabled')) != bool(requested_tts.get('enabled'))
                or old_tts.get('engine') != requested_tts.get('engine')
                or (not requested_tts.get('enabled') and getattr(self.app, 'tts_engine', None) is not None)
                or (requested_tts.get('enabled') and getattr(self.app, 'tts_engine', None) is None)
                or getattr(self.app, 'audio_coordinator', None) is None
            ))
            self.app.config.update(updates)
            if replace_tts:
                self._replace_tts_runtime(requested_tts)
            self.app.save_config()
            if 'diagnostics' in updates:
                # Recording paths use this cached flag; Apply takes effect on
                # the next utterance without restarting the running app.
                self.app._stage_timing_enabled = bool(
                    (self.app.config.get('diagnostics', {}) or {}).get('stage_timing', False)
                )

        if "tts" in updates:
            from samsara.config_schema import spoken_notices_for_level
            setter = getattr(getattr(self.app, "_session_mode_manager", None), "set_spoken_notices", None)
            if callable(setter):
                setter(spoken_notices_for_level((updates["tts"] or {}).get("feedback_level")))

        now_ai_pack_enabled = bool(
            (self.app.config.get('command_packs', {}) or {}).get('ai', False)
        )
        if now_ai_pack_enabled and not was_ai_pack_enabled:
            rebuild = getattr(
                getattr(self.app, 'command_executor', None), 'rebuild_matcher', None
            )
            if callable(rebuild):
                rebuild()

        if 'ui' in updates:
            theme.set_theme(updates['ui'].get('theme'), refresh=True)

        if {
            'listening_indicator_enabled',
            'listening_indicator_position',
            'listening_indicator_custom_position',
        }.intersection(updates):
            apply_indicator = getattr(
                self.app, 'apply_listening_indicator_settings', None
            )
            if callable(apply_indicator):
                apply_indicator()

        # Mouse 4/5 bindings (main hotkey, command-mode button) live in one
        # Win32 hook; moving a binding between mouse and keyboard reinstalls it.
        if {'hotkey', 'command_mode'}.intersection(updates):
            refresh_mouse_hook = getattr(self.app, 'refresh_mouse_hook', None)
            if callable(refresh_mouse_hook):
                refresh_mouse_hook()

        now_sc_enabled = bool(
            self.app.config.get('smart_corrections', {}).get('enabled', False)
        )
        if now_sc_enabled and not was_sc_enabled:
            try:
                smart_corrections.warm_up(self.app)
            except Exception as e:
                logger.debug(f"[SETTINGS] Smart Corrections warm_up call failed: {e}")

        self.close()
