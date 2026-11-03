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
