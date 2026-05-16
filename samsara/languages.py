"""Shared language definitions for Whisper transcription and TTS voice matching."""

LANGUAGES = [
    ("English",    "en"),
    ("Spanish",    "es"),
    ("French",     "fr"),
    ("German",     "de"),
    ("Portuguese", "pt"),
    ("Italian",    "it"),
    ("Dutch",      "nl"),
    ("Japanese",   "ja"),
    ("Korean",     "ko"),
    ("Chinese",    "zh"),
    ("Russian",    "ru"),
    ("Arabic",     "ar"),
    ("Hindi",      "hi"),
    ("Turkish",    "tr"),
    ("Polish",     "pl"),
    ("Swedish",    "sv"),
]

DEFAULT_TTS_VOICES = {
    "en": "en-US-AvaNeural",
    "es": "es-MX-DaliaNeural",
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "pt": "pt-BR-FranciscaNeural",
    "it": "it-IT-ElsaNeural",
    "nl": "nl-NL-ColetteNeural",
    "ja": "ja-JP-NanamiNeural",
    "ko": "ko-KR-SunHiNeural",
    "zh": "zh-CN-XiaoxiaoNeural",
    "ru": "ru-RU-SvetlanaNeural",
    "ar": "ar-SA-ZariyahNeural",
    "hi": "hi-IN-SwaraNeural",
    "tr": "tr-TR-EmelNeural",
    "pl": "pl-PL-ZofiaNeural",
    "sv": "sv-SE-SofieNeural",
}
