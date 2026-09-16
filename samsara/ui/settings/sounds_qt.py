"""Sounds page of the Samsara settings window.

Moved verbatim out of samsara/ui/settings_qt.py (queue 21, no behaviour change).
SoundsPage is mixed into settings_qt._SettingsWindow, so every method still runs
with the window as self (self._widgets, self._save_fns, self._setting_row ...).
Shared names are imported back from samsara.ui.settings_qt; see
samsara/ui/settings/__init__.py for why that import is cycle-safe.
"""

import shutil
import threading

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from samsara.runtime import thread_registry
from samsara.ui import theme

# Paths below stay relative to settings_qt.py, where this code was written.
from samsara.ui.settings_qt import __file__ as _SETTINGS_QT_FILE
from samsara.ui.settings_qt import _CONTENT_MAX_WIDTH, logger


class SoundsPage:
    """Methods of the Sounds settings page (moved from _SettingsWindow)."""

    def _build_sounds_tab(self):
        from pathlib import Path

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter
        )

        container = QWidget()
        container.setMaximumWidth(_CONTENT_MAX_WIDTH)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(8)

        cfg = self.app.config
        sounds_dir = getattr(self.app, 'sounds_dir', None)
        if sounds_dir is None:
            sounds_dir = Path(_SETTINGS_QT_FILE).parent.parent.parent / 'sounds'

        # ---- Section: Audio Feedback -------------------------------------------
        layout.addWidget(self._section_title("Audio Feedback"))
        layout.addSpacing(4)

        feedback_cb = QCheckBox("Enable audio feedback sounds")
        feedback_cb.setChecked(bool(cfg.get('audio_feedback', True)))
        self._widgets['sound_feedback'] = feedback_cb
        layout.addWidget(feedback_cb)
        layout.addSpacing(10)

        # Volume row: label + slider + percentage label + test button
        vol_row = QHBoxLayout()
        vol_row.setSpacing(12)
        vol_lbl = QLabel("Volume:")
        vol_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        vol_lbl.setFixedWidth(70)

        raw_vol = float(cfg.get('sound_volume', 0.5))
        vol_slider = QSlider(Qt.Orientation.Horizontal)
        vol_slider.setRange(0, 100)
        vol_slider.setValue(int(raw_vol * 100))
        vol_slider.setFixedWidth(200)
        vol_slider.setStyleSheet(
            "QSlider::groove:horizontal {"
            f"  height: 4px; background: {theme.wash(0.12)}; border-radius: 2px;"
            "}"
            "QSlider::handle:horizontal {"
            "  width: 16px; height: 16px; margin: -6px 0;"
            f"  border-radius: 8px; background: {theme.ACCENT};"
            "}"
            "QSlider::sub-page:horizontal {"
            f"  background: {theme.ACCENT}; border-radius: 2px;"
            "}"
        )
        self._widgets['sound_volume_slider'] = vol_slider

        vol_pct = QLabel(f"{int(raw_vol * 100)}%")
        vol_pct.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
        vol_pct.setFixedWidth(40)
        vol_slider.valueChanged.connect(lambda v: vol_pct.setText(f"{v}%"))

        test_btn = QPushButton("Test")
        test_btn.setMinimumWidth(80)  # sizeHint is 73; a few px of margin
        test_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        test_btn.clicked.connect(lambda: self._play(sounds_dir, 'success'))

        vol_row.addWidget(vol_lbl)
        vol_row.addWidget(vol_slider)
        vol_row.addWidget(vol_pct)
        vol_row.addWidget(test_btn)
        vol_row.addStretch()
        layout.addLayout(vol_row)
        layout.addSpacing(20)

        # ---- Section: Sound Theme ----------------------------------------------
        layout.addWidget(self._section_title("Sound Theme"))
        layout.addSpacing(4)

        themes_dir = sounds_dir / 'themes'
        if themes_dir.exists():
            available_themes = sorted(
                d.name for d in themes_dir.iterdir()
                if d.is_dir() and (d / 'start.wav').exists()
            )
        else:
            available_themes = ['cute', 'warm', 'zen', 'classic', 'chirpy']

        current_theme = cfg.get('sound_theme', 'cute')
        theme_combo = QComboBox()
        theme_combo.addItems(available_themes)
        if current_theme in available_themes:
            theme_combo.setCurrentText(current_theme)
        self._widgets['sound_theme_combo'] = theme_combo

        apply_theme_btn = QPushButton("Apply Theme")
        apply_theme_btn.setMinimumWidth(140)  # sizeHint is 132; a few px of margin
        apply_theme_btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        apply_theme_btn.clicked.connect(
            lambda: self._apply_sound_theme(
                theme_combo.currentText(), sounds_dir, themes_dir
            )
        )

        theme_row = QHBoxLayout()
        theme_row.setSpacing(12)
        theme_row.addLayout(self._setting_row(
            "Theme",
            "cute = playful bloops  •  warm = OS boot vibes  •  "
            "zen = singing bowls  •  classic = original  •  chirpy = bright",
            theme_combo,
        ))
        layout.addLayout(theme_row)

        apply_row = QHBoxLayout()
        apply_row.addWidget(apply_theme_btn)
        apply_row.addStretch()
        layout.addLayout(apply_row)

        theme_instant_note = QLabel("\"Apply Theme\" copies the theme's sounds immediately.")
        theme_instant_note.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(theme_instant_note)

        # Queue 103: which of the session's own spoken notices are spoken.
        # Nothing but Ava talks in this app, so a voice after a mouse click
        # reads as a malfunction -- the incident was clicking "Clear draft"
        # and being told out loud to say "bring back my draft", which the
        # chip was already showing.
        from samsara.config_defaults import cfg_get   # noqa: PLC0415
        from samsara import session_modes             # noqa: PLC0415

        notices_combo = QComboBox()
        notices_combo.addItem("Only when it needs an answer", "questions")
        notices_combo.addItem("Everything it does", "everything")
        current_notices = cfg_get(cfg, session_modes.SPOKEN_NOTICES_KEY)
        index = notices_combo.findData(current_notices)
        notices_combo.setCurrentIndex(index if index >= 0 else 0)
        self._widgets['spoken_notices_combo'] = notices_combo

        layout.addLayout(self._setting_row(
            "Spoken notices",
            "What Samsara says out loud about your draft.  "
            "\"Only when it needs an answer\" leaves what it has already done "
            "to the chip.  Choose \"Everything it does\" if you cannot see the "
            "chip.",
            notices_combo,
        ))
        layout.addSpacing(20)

        # ---- Section: Earcon Preview -------------------------------------------
        layout.addWidget(self._section_title("Earcon Preview"))
        desc = QLabel("Preview the audio cues for the active theme.")
        desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        layout.addWidget(desc)
        layout.addSpacing(6)

        _EARCONS = [
            ('start',            'Recording start'),
            ('stop',             'Recording stop'),
            ('success',          'Transcription success'),
            ('error',            'Error'),
            ('capture_started',  'Capture started'),
            ('capture_saved',    'Capture saved'),
            ('agent_routing',    'Agent routing'),
            ('agent_response',   'Agent response'),
            ('confirm_required', 'Confirm required'),
            ('action_complete',  'Action complete'),
            ('thinking_pulse',   'Thinking pulse'),
        ]

        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        cols = 3
        for idx, (sound_key, label_text) in enumerate(_EARCONS):
            row_i = idx // cols
            col_i = (idx % cols) * 2

            name_lbl = QLabel(label_text)
            name_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
            play_btn = QPushButton("▶")
            play_btn.setFixedWidth(36)
            play_btn.setStyleSheet(
                f"QPushButton {{ background-color: {theme.BG2}; border: 1px solid {theme.wash(0.14)};"
                f" border-radius: 5px; color: {theme.ACCENT}; font-size: {theme.TYPE_BODY}px; padding: 4px; }}"
                f"QPushButton:hover {{ background-color: {theme.tint(theme.ACCENT, 0.12)}; }}"
            )
            play_btn.clicked.connect(
                lambda _=False, k=sound_key: self._play(sounds_dir, k)
            )
            grid.addWidget(name_lbl, row_i, col_i)
            grid.addWidget(play_btn, row_i, col_i + 1)

        for c in range(cols * 2):
            if c % 2 == 0:
                grid.setColumnStretch(c, 1)

        layout.addWidget(grid_widget)
        layout.addSpacing(20)

        # ---- Section: Sound Files ----------------------------------------------
        layout.addWidget(self._section_title("Sound Files"))
        files_desc = QLabel(
            f"Active sound files from: {sounds_dir}"
        )
        files_desc.setStyleSheet(f"color: {theme.ICON_IDLE}; font-size: {theme.TYPE_MIN}px;")
        files_desc.setWordWrap(True)
        layout.addWidget(files_desc)
        layout.addSpacing(6)

        _CORE_SOUNDS = [
            ('start',   'Recording start'),
            ('stop',    'Recording stop'),
            ('success', 'Success'),
            ('error',   'Error'),
        ]
        for sound_key, label_text in _CORE_SOUNDS:
            wav = sounds_dir / f"{sound_key}.wav"
            exists = wav.exists()
            file_row = QHBoxLayout()
            file_row.setSpacing(10)

            name_lbl = QLabel(label_text + ":")
            name_lbl.setStyleSheet(f"color: {theme.TEXT_PRIMARY}; font-size: {theme.TYPE_BODY}px;")
            name_lbl.setFixedWidth(150)

            fname_lbl = QLabel(wav.name if exists else "not found")
            fname_lbl.setStyleSheet(
                f"color: {theme.ICON_IDLE if exists else theme.ERROR}; font-size: {theme.TYPE_MIN}px;"
            )
            fname_lbl.setFixedWidth(140)

            play_btn = QPushButton("▶")
            play_btn.setFixedWidth(36)
            play_btn.setEnabled(exists)
            play_btn.setStyleSheet(
                f"QPushButton {{ background-color: {theme.BG2}; border: 1px solid {theme.wash(0.14)};"
                f" border-radius: 5px; color: {theme.ACCENT}; font-size: {theme.TYPE_BODY}px; padding: 4px; }}"
                f"QPushButton:hover {{ background-color: {theme.tint(theme.ACCENT, 0.12)}; }}"
                f"QPushButton:disabled {{ color: {theme.TEXT_DISABLED}; border-color: {theme.wash(0.06)}; }}"
            )
            play_btn.clicked.connect(
                lambda _=False, k=sound_key: self._play(sounds_dir, k)
            )

            file_row.addWidget(name_lbl)
            file_row.addWidget(fname_lbl)
            file_row.addWidget(play_btn)
            file_row.addStretch()
            layout.addLayout(file_row)

        def _save(_acc):
            updates = {}
            if 'sound_feedback' in self._widgets:
                updates['audio_feedback'] = self._widgets['sound_feedback'].isChecked()
                updates['sound_volume'] = self._widgets['sound_volume_slider'].value() / 100.0
                updates['sound_theme'] = self._widgets['sound_theme_combo'].currentText()
            if 'spoken_notices_combo' in self._widgets:
                # NESTED, not the dotted key. settings_qt does a FLAT
                # config.update(updates), while cfg_get() walks the dotted
                # path -- so writing "feedback.spoken_notices" as a literal
                # key would store something cfg_get can never find and the
                # setting would silently do nothing. Copy the section and
                # replace it, the same way the Advanced tab writes
                # smart_corrections.
                feedback_out = dict(self.app.config.get('feedback', {}) or {})
                feedback_out['spoken_notices'] = (
                    self._widgets['spoken_notices_combo'].currentData())
                updates['feedback'] = feedback_out
            return updates
        self._save_fns.append(_save)

        layout.addStretch()
        scroll.setWidget(container)
        return scroll
    def _test_volume(self) -> "float | None":
        """The Sounds tab slider's CURRENT value (0..1), saved or not."""
        slider = self._widgets.get('sound_volume_slider')
        if slider is None:
            return None
        return slider.value() / 100.0

    def _play(self, sounds_dir, sound_key: str) -> None:
        """Play a sound at the slider's current (unsaved) volume.

        Tries app.play_sound first, falls back to winsound. Previously this
        called app.play_sound(key) with no volume, so Test played at the SAVED
        sound_volume and moving the slider changed nothing until Apply.
        """
        volume = self._test_volume()
        try:
            self.app.play_sound(sound_key, volume=volume)
            return
        except TypeError:
            # An app double without the volume parameter -- still play.
            try:
                self.app.play_sound(sound_key)
                return
            except Exception as e:
                logger.debug(f"_play: {e}")
        except Exception as e:
            logger.debug(f"_play: {e}")
        wav = sounds_dir / f"{sound_key}.wav"
        if wav.exists():
            self._play_wav_at_volume(wav, volume)

    @staticmethod
    def _play_wav_at_volume(wav, volume) -> None:
        """winsound fallback that still honours the slider.

        winsound.PlaySound has no volume parameter, so a plain SND_FILENAME
        call plays at full level regardless of the slider. For 16-bit PCM --
        every shipped earcon -- the samples are scaled into an in-memory WAV
        and played with SND_MEMORY. winsound refuses SND_MEMORY together with
        SND_ASYNC, so that playback runs synchronously on a daemon thread to
        keep the Qt thread free. Any other format falls back to the old
        unscaled async playback.
        """
        def _scaled_bytes():
            import array
            import io
            import wave

            with wave.open(str(wav), "rb") as src:
                params = src.getparams()
                if params.sampwidth != 2:
                    return None
                frames = src.readframes(params.nframes)
            samples = array.array("h", frames)
            gain = 1.0 if volume is None else min(max(float(volume), 0.0), 1.0)
            for i, value in enumerate(samples):
                samples[i] = int(value * gain)
            out = io.BytesIO()
            with wave.open(out, "wb") as dst:
                dst.setparams(params)
                dst.writeframes(samples.tobytes())
            return out.getvalue()

        def _run():
            try:
                import winsound
                data = _scaled_bytes() if volume is not None else None
                if data is not None:
                    winsound.PlaySound(data, winsound.SND_MEMORY)
                else:
                    winsound.PlaySound(str(wav), winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception as e:
                print(f"[SOUNDS] Could not play {wav.name}: {e}")

        # Registered daemon (37): a sound preview must never keep the app
        # alive, so it is not joined at shutdown; the registry still sees it.
        thread_registry.spawn("settings-sound-test", _run, daemon=True)

    def _apply_sound_theme(self, theme: str, sounds_dir, themes_dir) -> None:
        """Copy WAV files from the selected theme folder into sounds_dir."""
        theme_path = themes_dir / theme
        if not theme_path.exists():
            print(f"[SOUNDS] Theme folder not found: {theme_path}")
            return

        def _do():
            for wav in theme_path.glob('*.wav'):
                try:
                    shutil.copy2(wav, sounds_dir / wav.name)
                except Exception as e:
                    print(f"[SOUNDS] copy {wav.name}: {e}")
            try:
                self.app._load_sound_cache()
            except Exception as e:
                logger.debug(f"_do: {e}")
            try:
                self.app.play_sound('success')
            except Exception as e:
                logger.debug(f"_do: {e}")
            print(f"[SOUNDS] Theme applied: {theme}")

        thread_registry.spawn("settings_qt._do", _do, daemon=True)
