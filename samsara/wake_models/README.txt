Primary wake classifiers
========================
hey_samsara.onnx: "hey Samsara" -> Samsara commands/dictation by default.
hey_ava.onnx: "hey Ava" -> Ava conversation by default; OFF until enabled.
Both use the existing shared openWakeWord feature extractor. Each classifier
is 822,269 bytes. See ATTRIBUTION.md and the copied notices for CC BY 4.0
classifier attribution and training-input licences; app source stays separate.

Held-out evaluation at threshold 0.2 (2026-11-03, task 1042a):
- hey Samsara: 301/308 synthetic target clips (97.73%); zero observed false
  episodes in 5.4035 hours of LibriSpeech test-clean.
- hey Ava: 305/308 (99.03%); 9 false episodes / 5.4035 hours (1.6656/hour).
These are restricted-corpus measurements, not real-microphone guarantees.
Hey Ava remains OFF by default because of its measured false-trigger rate.

Dictionary > Wake Words: enable hey Ava or add hey Samsara from the phrase
picker. The Role column selects Samsara or Ava independently of the model.
Custom ONNX files can also be selected with Add model, or placed here or in
the configured profile's wake_models directory. Only one enabled phrase
without an available model may use recognizer-based detection.

App-target profiles remain separate: hey_claude.onnx and
activate_hermes.onnx are optional models for their respective target apps.

Computer wake candidates (1274, 2026-11-19)
==========================================
computer.onnx: "computer" -> Samsara; OFF by default.
Screening threshold 0.2: 272/308 synthetic clips;
2 episodes / 1.767639 h saved dictation;
9 episodes / 5.403467 h LibriSpeech test-clean.
hey_computer.onnx: "hey computer" -> Samsara; OFF by default.
Screening threshold 0.2: 273/308 synthetic clips;
0 episodes / 1.767639 h saved dictation;
2 episodes / 5.403467 h LibriSpeech test-clean.
Bare computer is also an ordinary dictated word. Leave it OFF for ambient
listening pending a separately implemented/tested pause-and-command gate.
Thresholds are descriptive choices on these corpora, not owner-microphone
acceptance. Dictionary > Wake Words lists both phrases; adding either
starts disabled. Preserve Jarvis and test with Check wake words before
considering enablement. No pause gate is implemented by this task.
See COMPUTER_METRICS.json, COMPUTER_ATTRIBUTION.md and COMPUTER_LICENSE.txt.
