"""
Samsara Speech Processing Module

Handles speech recognition and text processing.
"""

import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np


class SpeechRecognizer:
    """Wrapper for Whisper speech-to-text model."""

    def __init__(
        self,
        model_size: str = "base",
        device: str = "auto",
        language: str = "en",
    ):
        """
        Initialize speech recognizer.

        Args:
            model_size: Whisper model size (tiny, base, small, medium, large-v3)
            device: Compute device (auto, cuda, cpu)
            language: Language code for recognition
        """
        self.model_size = model_size
        self.device = device
        self.language = language

        self._model = None
        self._loaded = False
        self._loading = False
        self._load_error: Optional[str] = None

    @property
    def is_loaded(self) -> bool:
        """Check if model is loaded."""
        return self._loaded

    @property
    def is_loading(self) -> bool:
        """Check if model is currently loading."""
        return self._loading

    @property
    def load_error(self) -> Optional[str]:
        """Get any error that occurred during loading."""
        return self._load_error

    def load(self, callback: Optional[Callable[[], None]] = None) -> None:
        """
        Load the Whisper model (blocking).

        Args:
            callback: Optional callback when loading completes
        """
        if self._loaded or self._loading:
            return

        self._loading = True
        self._load_error = None

        try:
            from faster_whisper import WhisperModel

            actual_device = self._resolve_device()
            compute_type = "float16" if actual_device == "cuda" else "int8"

            self._model = WhisperModel(
                self.model_size,
                device=actual_device,
                compute_type=compute_type,
            )
            self._loaded = True
            print(f"[OK] Model loaded ({actual_device})")

        except Exception as e:
            self._load_error = str(e)
            print(f"[ERROR] Failed to load model: {e}")

        finally:
            self._loading = False
            if callback:
                callback()

    def load_async(self, callback: Optional[Callable[[], None]] = None) -> None:
        """
        Load the Whisper model in a background thread.

        Args:
            callback: Optional callback when loading completes
        """
        thread = threading.Thread(
            target=self.load,
            args=(callback,),
            daemon=True,
        )
        thread.start()

    def _resolve_device(self) -> str:
        """Resolve 'auto' device to actual device."""
        if self.device != "auto":
            return self.device

        try:
            import ctranslate2
            return "cuda" if 'cuda' in ctranslate2.get_supported_compute_types('cuda') else "cpu"
        except Exception:
            return "cpu"

    def transcribe(
        self,
        audio: np.ndarray,
        initial_prompt: str = "",
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Transcribe audio to text.

        Args:
            audio: NumPy array of audio data
            initial_prompt: Context prompt for recognition
            beam_size: Beam size for decoding
            vad_filter: Use voice activity detection

        Returns:
            Tuple of (transcribed_text, info_dict)
        """
        if not self._loaded or self._model is None:
            return "", {"error": "Model not loaded"}

        try:
            segments, info = self._model.transcribe(
                audio,
                language=self.language,
                beam_size=beam_size,
                vad_filter=vad_filter,
                initial_prompt=initial_prompt,
            )

            text = "".join([segment.text for segment in segments]).strip()

            return text, {
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
            }

        except Exception as e:
            return "", {"error": str(e)}

    def set_model_size(self, model_size: str) -> None:
        """Set model size (requires reload)."""
        if model_size != self.model_size:
            self.model_size = model_size
            self._loaded = False
            self._model = None

    def set_language(self, language: str) -> None:
        """Set recognition language."""
        self.language = language


class TextProcessor:
    """Processes transcribed text with formatting and corrections."""

    NUMBER_WORDS = {
        'zero': '0', 'one': '1', 'two': '2', 'three': '3', 'four': '4',
        'five': '5', 'six': '6', 'seven': '7', 'eight': '8', 'nine': '9',
        'ten': '10', 'eleven': '11', 'twelve': '12', 'thirteen': '13',
        'fourteen': '14', 'fifteen': '15', 'sixteen': '16', 'seventeen': '17',
        'eighteen': '18', 'nineteen': '19', 'twenty': '20', 'thirty': '30',
        'forty': '40', 'fifty': '50', 'sixty': '60', 'seventy': '70',
        'eighty': '80', 'ninety': '90', 'hundred': '100', 'thousand': '1000',
        'million': '1000000', 'billion': '1000000000',
    }

    TENS = {
        'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50,
        'sixty': 60, 'seventy': 70, 'eighty': 80, 'ninety': 90,
    }

    ONES = {
        'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
        'six': 6, 'seven': 7, 'eight': 8, 'nine': 9,
    }

    def __init__(
        self,
        auto_capitalize: bool = True,
        format_numbers: bool = True,
        corrections: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize text processor.

        Args:
            auto_capitalize: Capitalize first letter and after sentences
            format_numbers: Convert spoken numbers to digits
            corrections: Dictionary of word corrections
        """
        self.auto_capitalize = auto_capitalize
        self.format_numbers = format_numbers
        self.corrections = corrections or {}

    def process(self, text: str) -> str:
        """
        Process transcribed text with all enabled transformations.

        Args:
            text: Raw transcribed text

        Returns:
            Processed text
        """
        if not text:
            return text

        # Apply corrections first
        if self.corrections:
            text = self.apply_corrections(text)

        # Format numbers
        if self.format_numbers:
            text = self.convert_numbers(text)

        # Auto-capitalize
        if self.auto_capitalize:
            text = self.capitalize(text)

        return text

    def apply_corrections(self, text: str) -> str:
        """
        Apply word corrections dictionary.

        Args:
            text: Text to correct

        Returns:
            Corrected text
        """
        if not self.corrections:
            return text

        words = text.split()
        corrected = []

        for word in words:
            # Preserve punctuation
            prefix = ''
            suffix = ''
            core = word

            while core and not core[0].isalnum():
                prefix += core[0]
                core = core[1:]
            while core and not core[-1].isalnum():
                suffix = core[-1] + suffix
                core = core[:-1]

            # Check for correction
            if core.lower() in self.corrections:
                corrected.append(prefix + self.corrections[core.lower()] + suffix)
            else:
                corrected.append(word)

        return ' '.join(corrected)

    def convert_numbers(self, text: str) -> str:
        """
        Convert spoken numbers to digits.

        Args:
            text: Text with spoken numbers

        Returns:
            Text with digit numbers
        """
        # Handle compound numbers (twenty one -> 21)
        for ten_word, ten_val in self.TENS.items():
            for one_word, one_val in self.ONES.items():
                pattern = rf'\b{ten_word}[\s-]{one_word}\b'
                text = re.sub(pattern, str(ten_val + one_val), text, flags=re.IGNORECASE)

        # Replace standalone number words
        words = text.split()
        new_words = []

        for word in words:
            prefix = ''
            suffix = ''
            core = word

            while core and not core[0].isalnum():
                prefix += core[0]
                core = core[1:]
            while core and not core[-1].isalnum():
                suffix = core[-1] + suffix
                core = core[:-1]

            if core.lower() in self.NUMBER_WORDS:
                new_words.append(prefix + self.NUMBER_WORDS[core.lower()] + suffix)
            else:
                new_words.append(word)

        return ' '.join(new_words)

    def capitalize(self, text: str) -> str:
        """
        Auto-capitalize text.

        Args:
            text: Text to capitalize

        Returns:
            Capitalized text
        """
        if not text:
            return text

        # Capitalize first letter
        text = text[0].upper() + text[1:] if len(text) > 1 else text.upper()

        # Capitalize after sentence-ending punctuation
        def capitalize_after(match):
            return match.group(1) + match.group(2).upper()

        text = re.sub(r'([.!?]\s+)([a-z])', capitalize_after, text)

        return text

    def set_corrections(self, corrections: Dict[str, str]) -> None:
        """Update corrections dictionary."""
        self.corrections = corrections

    def add_correction(self, from_word: str, to_word: str) -> None:
        """Add a single correction."""
        self.corrections[from_word.lower()] = to_word

    def remove_correction(self, word: str) -> None:
        """Remove a correction."""
        self.corrections.pop(word.lower(), None)
