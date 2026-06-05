"""Adaptive learning for transcription corrections.

Tracks user-reported corrections and auto-promotes them to the corrections
dictionary once they've been confirmed enough times.
"""
import json
from pathlib import Path
from collections import defaultdict
from datetime import datetime


class AdaptiveLearner:
    THRESHOLD = 3

    def __init__(self, config_dir):
        self.candidates_file = Path(config_dir) / 'correction_candidates.json'
        self.candidates = defaultdict(
            lambda: {'count': 0, 'last_seen': None, 'promoted': False}
        )
        self.recent_transcriptions = []
        self.load_candidates()

    def load_candidates(self):
        if not self.candidates_file.exists():
            return
        try:
            with open(self.candidates_file, encoding='utf-8') as f:
                data = json.load(f)
            for key, val in data.items():
                self.candidates[key] = val
        except Exception as e:
            print(f"[LEARN] Failed to load candidates: {e}")

    def save_candidates(self):
        try:
            with open(self.candidates_file, 'w', encoding='utf-8') as f:
                json.dump(dict(self.candidates), f, indent=2)
        except Exception as e:
            print(f"[LEARN] Failed to save candidates: {e}")

    def record_transcription(self, text):
        """Track recent transcriptions so the dialog can pre-fill original text."""
        self.recent_transcriptions.append(text)
        self.recent_transcriptions = self.recent_transcriptions[-10:]

    def record_correction(self, original, corrected):
        """Record a correction. Returns True when the promotion threshold is reached."""
        original = original.strip()
        corrected = corrected.strip()
        if not original or not corrected or original.lower() == corrected.lower():
            return False

        key = f"{original.lower()}|{corrected.lower()}"
        entry = self.candidates[key]
        entry['count'] = entry.get('count', 0) + 1
        entry['last_seen'] = datetime.now().isoformat()
        entry['original'] = original
        entry['corrected'] = corrected
        if 'promoted' not in entry:
            entry['promoted'] = False
        self.save_candidates()

        return entry['count'] >= self.THRESHOLD and not entry['promoted']

    def get_last_transcription(self):
        """Return the most recent transcription, or an empty string."""
        return self.recent_transcriptions[-1] if self.recent_transcriptions else ''

    def mark_promoted(self, original, corrected):
        """Mark a correction as promoted so it isn't re-suggested."""
        key = f"{original.lower()}|{corrected.lower()}"
        if key in self.candidates:
            self.candidates[key]['promoted'] = True
            self.save_candidates()
