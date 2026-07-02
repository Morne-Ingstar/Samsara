"""
Lightweight wake word detection using OpenWakeWord.
Runs as a pre-filter before Whisper to prevent CPU saturation.

OpenWakeWord uses ONNX models that run in ~5ms on CPU.
Pre-built models: hey_jarvis, alexa, hey_mycroft.
Custom models can be trained for other wake phrases and dropped into
samsara/wake_models/ — pass model_path to WakeWordDetector to use them.
"""

import logging
import threading
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Map Samsara wake phrases to OpenWakeWord model identifiers.
# The identifier is what is passed to Model(wakeword_models=[...]) and
# is also the key returned in the prediction dict.
PHRASE_TO_MODEL = {
    "jarvis":      "hey_jarvis",
    "hey jarvis":  "hey_jarvis",
    "alexa":       "alexa",
    "hey mycroft": "hey_mycroft",
}

# Phrases that have no pre-built OWW model fall back to Whisper-based
# detection: "samsara", "hey samsara", "samsa", "computer", "hey computer".
# Custom phrases ("hey claude", "activate hermes") also fall back until their
# .onnx files are trained and placed in samsara/wake_models/.


class WakeWordDetector:
    """
    Lightweight wake word detector using OpenWakeWord.

    Usage:
        detector = WakeWordDetector("jarvis")
        # In audio callback (16kHz int16 or float32 audio):
        if detector.detected(chunk_16k):
            # Wake word confirmed — send buffer to Whisper for command.

    For custom ONNX models (e.g. trained via openWakeWord's training pipeline):
        detector = WakeWordDetector("hey claude", model_path="/path/to/hey_claude.onnx")
    """

    def __init__(self, wake_phrase, threshold=0.2, model_path=None):
        self._wake_phrase  = wake_phrase.lower().strip()
        self._threshold    = threshold
        self._model_path   = str(model_path) if model_path else None
        self._model        = None
        self._model_name   = None   # prediction dict key, e.g. 'hey_jarvis'
        self._available    = False
        self._lock         = threading.Lock()

        self._init_model()

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _init_model(self):
        """Try to load the OpenWakeWord ONNX model for the configured phrase.

        Resolution order:
          1. Custom model_path (if provided and file exists).
          2. Pre-trained model via PHRASE_TO_MODEL lookup.
          3. No model → is_available stays False → Whisper-based fallback.
        """
        # 1. Custom ONNX path (drop-in from samsara/wake_models/)
        if self._model_path:
            if not Path(self._model_path).exists():
                logger.warning(
                    f"[OWW] Custom model path '{self._model_path}' not found "
                    f"for '{self._wake_phrase}' — falling back to Whisper detection"
                )
            else:
                try:
                    from openwakeword.model import Model  # noqa: PLC0415
                    self._model = Model(
                        wakeword_models=[self._model_path],
                        inference_framework="onnx",
                    )
                    # Prediction dict key is the stem of the file (e.g. "hey_claude")
                    self._model_name = Path(self._model_path).stem
                    self._available  = True
                    logger.info(
                        f"[OWW] Loaded custom model '{self._model_name}' "
                        f"from {self._model_path} for phrase '{self._wake_phrase}'"
                    )
                    return
                except ImportError as exc:
                    logger.warning(
                        f"[OWW] openwakeword import failed: {exc} — "
                        "falling back to Whisper-based detection"
                    )
                    return
                except Exception as exc:
                    logger.warning(
                        f"[OWW] Custom model load failed for '{self._wake_phrase}': {exc} — "
                        "falling back to Whisper-based detection"
                    )
                    return

        # 2. Pre-trained model via phrase lookup
        model_name = PHRASE_TO_MODEL.get(self._wake_phrase)

        if model_name is None:
            logger.info(
                f"[OWW] No pre-trained model for '{self._wake_phrase}' "
                f"- falling back to Whisper-based detection"
            )
            return

        try:
            from openwakeword.model import Model  # noqa: PLC0415
            self._model = Model(
                wakeword_models=[model_name],
                inference_framework="onnx",
            )
            self._model_name = model_name
            self._available  = True
            logger.info(
                f"[OWW] Loaded '{model_name}' model for "
                f"wake phrase '{self._wake_phrase}'"
            )
        except ImportError as e:
            logger.warning(
                f"[OWW] openwakeword import failed: {e} - "
                "falling back to Whisper-based detection"
            )
        except Exception as e:
            logger.warning(
                f"[OWW] Failed to load model '{model_name}': {e} - "
                f"falling back to Whisper-based detection"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def is_available(self):
        """True if OpenWakeWord is loaded and ready."""
        return self._available

    def process_audio(self, audio_chunk):
        """
        Feed an audio chunk to OpenWakeWord and return the detection score.

        Args:
            audio_chunk: 1-D numpy array at 16 kHz.
                         float32 [-1.0, 1.0] or int16 both accepted.

        Returns:
            float in [0.0, 1.0], or -1.0 when unavailable.
        """
        if not self._available:
            return -1.0

        if audio_chunk.dtype in (np.float32, np.float64):
            audio_chunk = (audio_chunk * 32767).astype(np.int16)

        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()

        with self._lock:
            try:
                prediction = self._model.predict(audio_chunk)
                return float(prediction.get(self._model_name, 0.0))
            except Exception as e:
                logger.debug(f"[OWW] Prediction error: {e}")
                return 0.0

    def detected(self, audio_chunk):
        """
        Return True if the wake word is detected above the threshold.
        Logs the score on a positive hit.
        """
        score = self.process_audio(audio_chunk)
        if score >= self._threshold:
            logger.info(
                f"[OWW] Wake word detected! "
                f"score={score:.3f} threshold={self._threshold}"
            )
            return True
        return False

    def reset(self):
        """Reset the model's internal rolling-window state between detections."""
        if not self._available or self._model is None:
            return
        with self._lock:
            try:
                self._model.reset()
            except Exception as e:
                logger.debug(f"reset: {e}")
