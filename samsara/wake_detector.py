"""Wake phrases share one openWakeWord feature extractor and model instance."""
import logging
import sys
import threading
from pathlib import Path

import numpy as np

from samsara.wake_profiles import PHRASE_TO_MODEL, resolve_wake_model, wake_threshold

logger = logging.getLogger(__name__)
_MODEL_IMPORT_LOCK = threading.Lock()


def _load_model_class():
    # Failed package initialization can leave imported children referring to a
    # discarded parent. Reimport those children with the next package attempt.
    with _MODEL_IMPORT_LOCK:
        if 'openwakeword' not in sys.modules:
            for name in tuple(sys.modules):
                if name.startswith('openwakeword.'):
                    sys.modules.pop(name, None)
        from openwakeword.model import Model
        return Model


class WakeWordDetector:
    def __init__(self, wake_phrase, threshold=0.2, model_path=None):
        entries = ([{'phrase': wake_phrase, 'threshold': threshold, 'model': model_path}]
                   if isinstance(wake_phrase, str) else wake_phrase)
        self._entries = [dict(row) for row in entries if row.get('enabled', True)]
        self._wake_phrase = (self._entries[0]['phrase'].lower().strip() if self._entries else '')
        self._threshold = wake_threshold(threshold)
        self._model_path = str(model_path) if model_path else None
        self._model = None
        self._model_name = None
        self._available = False
        self._lock = threading.Lock()
        self._heads = []
        self.last_detected_phrase = None
        models, missing = [], []
        for row in self._entries:
            phrase = row['phrase'].lower().strip()
            model = resolve_wake_model(phrase, row.get('model'))
            if not model:
                missing.append(phrase)
                continue
            if model not in models:
                models.append(model)
            key = Path(model).stem if model.lower().endswith('.onnx') else model
            self._heads.append((phrase, key, wake_threshold(row.get('threshold', threshold))))
        if models:
            try:
                Model = _load_model_class()
                self._model = Model(wakeword_models=list(models), inference_framework='onnx')
                self._model_name = self._heads[0][1]
                self._available = True
            except Exception as exc:
                logger.warning('[OWW] Multi-model load failed: %s; one recognizer fallback', exc)
                missing = [row['phrase'] for row in self._entries]
                self._heads = []
        self.fallback_phrases = tuple(missing[:1])
        if len(missing) > 1:
            logger.warning('[OWW] Only the first unavailable phrase can use the recognizer: %s', missing[0])

    @property
    def is_available(self):
        return self._available

    @property
    def requires_fallback(self):
        return bool(self.fallback_phrases)

    @property
    def has_enabled_phrases(self):
        return bool(self._entries)

    def _predict(self, audio_chunk):
        if audio_chunk.dtype in (np.float32, np.float64):
            audio_chunk = (np.clip(audio_chunk, -1, 1) * 32767).astype(np.int16)
        if audio_chunk.ndim > 1:
            audio_chunk = audio_chunk.flatten()
        try:
            return self._model.predict(audio_chunk)
        except Exception as exc:
            logger.debug('[OWW] Prediction error: %s', exc)
            return {}

    def process_audio(self, audio_chunk):
        """Largest head score, retained for single-phrase calibration."""
        if not self._available:
            return -1.0
        with self._lock:
            scores = self._predict(audio_chunk)
            return max((float(scores.get(key, 0)) for _, key, _ in self._heads), default=0.0)

    def detected(self, audio_chunk):
        """Return the phrase above its threshold, or None (also a truthy API)."""
        if not self._available:
            return None
        with self._lock:
            scores = self._predict(audio_chunk)
            hits = [(float(scores[key]), phrase) for phrase, key, threshold in self._heads
                    if key in scores and float(scores[key]) >= threshold]
            phrase = max(hits)[1] if hits else None
            if phrase:
                self.last_detected_phrase = phrase
                logger.info('[OWW] Wake phrase detected: %s', phrase)
            return phrase

    def reset(self):
        if not self._available or self._model is None:
            return
        with self._lock:
            try:
                self._model.reset()
            except Exception as exc:
                logger.debug('reset: %s', exc)
