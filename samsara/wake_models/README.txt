Custom OpenWakeWord ONNX models for Phase 1 multi-wakeword targeting.

Expected files:
  hey_claude.onnx       — phrase "hey claude"    -> focuses claude.exe
  activate_hermes.onnx  — phrase "activate hermes" -> focuses Hermes.exe

Training requirements (not installed in this env):
  pip install openwakeword[train]   # brings in torchinfo, audiomentations, etc.
  # Then use openwakeword's synthetic TTS training pipeline:
  #   python -m openwakeword.train --phrase "hey claude" --output hey_claude.onnx
  # Requires ~2-4h on GPU, ~500MB negative data, piper/edge-tts for positives.

Until .onnx files are present, Samsara uses Whisper-transcript matching
(match_wake_phrase) as a fallback — adequate for 3+ syllable phrases.
Drop the trained .onnx files here and restart Samsara to activate OWW pre-filter.
