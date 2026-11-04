"""Read-aloud microphone comparison. UI copy is provisional."""
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QVBoxLayout, QWidget,
)

from samsara.ui import theme


class MicScoreWindow(QDialog):
    finished_work = Signal(object)

    def __init__(self, app, *, preview=False):
        super().__init__()
        self.app = app
        self.run = None
        self.busy = False
        self.cancel = threading.Event()
        self.record_done = threading.Event()
        self.index = 0
        self.setWindowTitle("Mic score")
        self.resize(800, 720)
        self.setStyleSheet(
            f"QDialog, QWidget {{background:{theme.BG0}; color:{theme.TEXT_PRIMARY};}}"
            f"QLabel {{font-size:{theme.TYPE_BODY}px;}} QComboBox {{padding:8px;}}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        layout.addWidget(theme.hub_header(
            "Mic score", "Read five short sentences to compare your microphones."))
        self.devices = QComboBox()
        layout.addWidget(self.devices)
        self.all_mics = QCheckBox("Test every connected mic")
        layout.addWidget(self.all_mics)
        self.status = QLabel("Start with a second of quiet, then read when Listening appears.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.sentence = QLabel("Open Stremio and play my favourite show.")
        self.sentence.setWordWrap(True)
        self.sentence.setStyleSheet(f"font-size:{theme.TYPE_TITLE}px; color:{theme.ACCENT}; padding:18px 0;")
        layout.addWidget(self.sentence)
        self.record = QPushButton("Start test")
        theme.make_primary(self.record)
        layout.addWidget(self.record, alignment=Qt.AlignmentFlag.AlignLeft)
        self.cancel_button = QPushButton("Cancel test")
        theme.make_secondary(self.cancel_button)
        self.cancel_button.clicked.connect(self.cancel_test)
        self.cancel_button.setVisible(False)
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.results = QVBoxLayout(content)
        self.results.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.note = QLabel("Mic choices are ordered by Recognition estimates. Read-aloud accuracy is measured separately. Command matching is about the router, not the mic.")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        if preview:
            self.devices.addItem("Desk microphone (preview)", 1)
            self.add_result("Desk microphone · example", "9 / 10 · 97% word accuracy",
                            "Good input level · Background: −52 dBFS\n1 word missed · Said: Jarvis → Heard: service\nRecognition (decoder confidence estimate): 8 / 10\nCommand matching (router, not about the mic): 60% matched", lambda: None)
            self.add_result("Headset · example", "6 / 10 · 88% word accuracy",
                            "Too quiet · Background: −44 dBFS\n4 words missed · Recognition estimate: not enough data\nCommand matching (router, not about the mic): no recent commands", lambda: None)
        else:
            from samsara.mic_score import SENTENCES
            self.sentence.setText(SENTENCES[0])
            self.record.clicked.connect(self.advance)
            self.finished_work.connect(self.on_work)
            self.refresh()

    def refresh(self):
        from samsara.mic_score import (ResultStore, everyday_estimate, microphone_name,
                                      comparison_context, context_difference, rank_results, diagnostic_lines)
        if not self.busy:
            self.devices.clear()
            try:
                for device in self.app.get_available_microphones():
                    self.devices.addItem(device["name"], device)
                current = self.devices.findText(microphone_name(self.app))
                if current >= 0:
                    self.devices.setCurrentIndex(current)
            except Exception:
                self.status.setText("Microphones could not be listed. Reconnect them and reopen Mic score.")
        while self.results.count():
            widget = self.results.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        try:
            stored = ResultStore().load()
            if not stored:
                self.results.addWidget(QLabel("No test scores yet. Your first result will appear here."))
            manager = getattr(self.app, "history_db", None)
            names = {r["device_name"] for r in stored}
            names.update(self.devices.itemText(i) for i in range(self.devices.count()))
            estimates = {name: everyday_estimate(manager.recent_for_microphone(name), name)
                         if manager else None for name in names}
            engine = getattr(self.app, "_ace_engine", None)
            current_context = comparison_context(self.app, microphone_name(self.app),
                                                 getattr(engine, "_native_rate", None))
            for result in rank_results(stored, estimates):
                name, takes = result["device_name"], result["takes"]
                errors = sum(t["errors"] for t in takes)
                levels = set(t["level"] for t in takes)
                level = "clipping" if "clipping" in levels else "too quiet" if "too quiet" in levels else "good"
                noises = [t["noise_dbfs"] for t in takes if t["noise_dbfs"] is not None]
                noise = (f"{'quiet' if sum(noises)/len(noises) < -40 else 'noticeable noise'} "
                         f"({sum(noises)/len(noises):.0f} dBFS)" if noises else "not measured")
                detail = f"{errors} word errors · Input level: {level} · Background: {noise}\n{diagnostic_lines(estimates.get(name))}"
                context = result.get("context", {})
                # Native rate comes from the running engine, without opening
                # devices just to display history.
                warning = context_difference(result, current_context)
                if warning:
                    detail += "\n" + warning
                if context:
                    detail += (f"\nTest context: {context.get('model_name') or 'unknown model'} · "
                               f"{context.get('compute_device') or 'unknown compute device'} · "
                               f"{context.get('input_sample_rate') or 'unknown'} Hz · "
                               f"Samsara {context.get('samsara_version', 'unknown')}")
                if result["sequential"]:
                    detail += "\nTested separately because it could not open alongside the others. Different take."
                for take in takes:
                    for difference in take["differences"]:
                        detail += f"\nSaid: {difference['said']} → Heard: {difference['heard']}"
                self.add_result(f"{name} · {result['date'][:16].replace('T', ' ')} UTC",
                                f"Read-aloud accuracy (measured): {result['score']} / 10 · {result['accuracy']:.0%}",
                                detail, lambda checked=False, n=name: self.use(n))
            for name in sorted(names - {r["device_name"] for r in stored}):
                label = QLabel(f"{name}\nRead-aloud accuracy: not measured yet\n{diagnostic_lines(estimates.get(name))}")
                label.setTextFormat(Qt.TextFormat.PlainText)
                label.setWordWrap(True)
                self.results.addWidget(label)
        except Exception:
            self.status.setText("Saved scores could not be read. Your existing results have been kept.")

    def use(self, name):
        if self.busy or self.run is not None:
            self.status.setText("Finish or cancel the test before switching microphones.")
            return
        from samsara.mic_score import use_microphone
        from samsara.runtime import thread_registry
        self.busy = True
        self.record.setEnabled(False)

        def switch():
            try:
                use_microphone(self.app, name)
                self.finished_work.emit(("switched", name))
            except Exception as exc:
                self.finished_work.emit(("error", str(exc)))
        thread_registry.spawn("mic-score-switch", switch, daemon=True)

    def advance(self):
        if self.busy:
            self.record_done.set()
            self.record.setEnabled(False)
            self.status.setText("Scoring this sentence…")
            return
        from samsara.mic_score import ComparisonRun
        from samsara.runtime import thread_registry
        if self.run is None:
            if getattr(self.app, "_mic_score_active", False):
                self.status.setText("Finish the current microphone check before starting another test.")
                return
            devices = [self.devices.itemData(i) for i in range(self.devices.count())] if self.all_mics.isChecked() else [self.devices.currentData()]
            devices = [d for d in devices if d]
            if not devices:
                self.status.setText("No microphones detected. Reconnect one and reopen Mic score.")
                return
            if getattr(self.app, "recording", False):
                self.status.setText("Finish your current recording before starting the mic test.")
                return
            self.run = ComparisonRun(self.app, devices)
            self.app._mic_score_active = True
            self.index = 0
            self.cancel.clear()
        self.busy = True
        self.record_done.clear()
        self.devices.setEnabled(False)
        self.all_mics.setEnabled(False)
        self.record.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.status.setText("Measuring background noise. Stay quiet for one second…")
        run, index = self.run, self.index

        def work():
            try:
                if not run.sources and not run.prepare():
                    raise RuntimeError("No microphone could be opened. Reconnect one and try again.")
                recordings = run.record(self.record_done, self.cancel,
                                        lambda: self.finished_work.emit(("listening", None)))
                self.finished_work.emit(("scoring", None))
                run.score_take(index, recordings, self.cancel)
                more = index < 4
                if not more:
                    more = run.next_group()
                self.finished_work.emit(("take", (more, index)))
            except Exception as exc:
                run.close()
                self.finished_work.emit(("error", str(exc)))
        thread_registry.spawn("mic-score-take", work, daemon=True)

    def cancel_test(self):
        self.cancel.set()
        if not self.busy and self.run is not None:
            self.run.close()
            self.run = None
            self.app._mic_score_active = False
            self.devices.setEnabled(True)
            self.all_mics.setEnabled(True)
            self.record.setText("Start test")
            self.cancel_button.setVisible(False)
            self.status.setText("Mic test cancelled.")

    def on_work(self, event):
        from samsara.mic_score import SENTENCES
        kind, value = event
        if kind == "listening":
            label = ", ".join(self.run.sources) if self.run else ""
            fallback = (" Some microphones could not open together; they will have separate takes."
                        if self.run and self.run.fallback else "")
            self.status.setText(f"Listening · {label}. Read the sentence, then choose Done reading. Stops after 15 seconds.{fallback}")
            self.record.setText("Done reading")
            self.record.setEnabled(True)
            return
        if kind == "scoring":
            self.record.setEnabled(False)
            self.status.setText("Scoring this sentence…")
            return
        self.busy = False
        self.cancel_button.setVisible(False)
        self.record.setEnabled(True)
        if kind == "take":
            more, index = value
            self.refresh()
            if more:
                self.index = index + 1 if index < 4 else 0
                self.sentence.setText(SENTENCES[self.index])
                extra = " This microphone needs a separate take." if self.run.sequential else ""
                if self.run.fallback:
                    extra += " Some microphones need separate takes after this group."
                self.status.setText(f"Sentence {self.index + 1} of 5.{extra}")
                self.record.setText("Record next sentence")
                self.cancel_button.setVisible(True)
                return
            failures = "; ".join(f"{n}: {why}" for n, why in self.run.failures.items())
            self.status.setText("Test complete. Results saved." + (" Untested: " + failures if failures else ""))
        elif kind == "switched":
            self.status.setText(f"Using {value}.")
            self.refresh()
        else:
            self.status.setText(value)
        self.run = None
        self.app._mic_score_active = False
        self.record.setText("Start test")
        self.devices.setEnabled(True)
        self.all_mics.setEnabled(True)

    def closeEvent(self, event):
        self.cancel.set()
        if self.run is not None and not self.busy:
            self.run.close()
            self.run = None
            self.app._mic_score_active = False
        super().closeEvent(event)

    def add_result(self, name, score, details, use):
        row = QWidget()
        column = QVBoxLayout(row)
        title = QLabel(name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        column.addWidget(title)
        value = QLabel(score)
        value.setStyleSheet(f"font-size:{theme.TYPE_HEADING}px; font-weight:600;")
        column.addWidget(value)
        description = QLabel(details)
        description.setTextFormat(Qt.TextFormat.PlainText)
        description.setWordWrap(True)
        column.addWidget(description)
        button = QPushButton("Use this mic")
        theme.make_secondary(button)
        button.clicked.connect(use)
        column.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.results.addWidget(row)


def open_mic_score(app):
    from samsara.ui import qt_runtime

    def show():
        window = getattr(app, "_mic_score_window", None)
        if window is None:
            window = MicScoreWindow(app)
            app._mic_score_window = window
        if not window.busy and window.run is None:
            window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()
    qt_runtime.post(show)
