"""Audio Settings destination (285; placeholder copy)."""

TITLE = "Audio"
SUBTITLE = "Manage sounds, speech and captions in one place."
BASE = [
    [
        "Output device",
        "Choose where Samsara plays sound.",
        "output_combo",
        "output_device,output_device_name,output_device_hostapi"
    ],
    [
        "Feedback sounds",
        "Hear signals for recording and actions.",
        "sound_feedback",
        "audio_feedback"
    ],
    [
        "Sound volume",
        "Adjust feedback volume and test it.",
        "sound_volume_slider,sounds:Test",
        "sound_volume"
    ],
    [
        "Sound theme",
        "Choose the feedback sound set.",
        "sound_theme_combo,soundPreview",
        "sound_theme"
    ],
    [
        "While recording",
        "Choose how other apps' audio is handled.",
        "adv_ducking_recording_mode",
        "ducking.recording_mode"
    ],
    [
        "Voice feedback",
        "Choose how much Samsara says aloud.",
        "tts_feedback_level",
        "tts.feedback_level,tts.enabled"
    ],
    [
        "Speech engine",
        "Choose local Windows speech or a network voice.",
        "tts_engine",
        "tts.engine"
    ],
    [
        "Voice",
        "Choose a voice and hear a sample.",
        "tts_voice_combo,tts:Test Voice",
        "tts.voice_id"
    ],
    [
        "Speech speed",
        "Adjust spoken response speed.",
        "tts_speed",
        "tts.speed"
    ],
    [
        "Ava captions",
        "Show Ava's responses as text.",
        "ava_captions",
        "accessibility.ava_captions"
    ],
    [
        "Mute Ava's voice",
        "Silence Ava while keeping other speech available.",
        "ava_mute_audio",
        "accessibility.ava_mute_audio"
    ]
]
ADVANCED = [
    [
        "Speech volume",
        "Adjust speech independently of feedback sounds.",
        "tts_volume_slider",
        "tts.volume"
    ],
    [
        "Speech during feedback",
        "Remaining speech volume while feedback sounds play.",
        "tts_duck_slider",
        "audio_coordinator.duck_factor"
    ],
    [
        "Session-end sound",
        "Play a sound when a session ends.",
        "cmd_exit_earcon",
        "command_mode.exit_earcon"
    ],
    [
        "Spoken reply limit",
        "Limit the length of spoken acknowledgements.",
        "cmd_tts_char_limit",
        "command_mode.tts_char_limit"
    ],
    [
        "Ducked volume",
        "Remaining other-app volume while recording.",
        "adv_ducking_level",
        "ducking.level"
    ],
    [
        "Hands-free audio",
        "Reduce other apps during hands-free listening.",
        "adv_hands_free_ducking_enabled",
        "ducking.hands_free_enabled"
    ],
    [
        "Hands-free capture volume",
        "Remaining other-app volume while capturing speech.",
        "adv_hands_free_ducking_level",
        "ducking.hands_free_level"
    ],
    [
        "Hands-free idle volume",
        "Remaining other-app volume while listening.",
        "adv_hands_free_idle_level",
        "ducking.hands_free_idle_level"
    ],
    [
        "Echo cancellation",
        "Experimental microphone echo cancellation.",
        "adv_aec_enabled",
        "echo_cancellation.enabled"
    ],
    [
        "Echo latency",
        "Compensate for measured playback latency.",
        "adv_aec_latency",
        "echo_cancellation.latency_ms"
    ]
]

GROUPS = {
    'output_combo': 'Output',
    'sound_feedback': 'Audio Feedback',
    'tts_feedback_level': 'Text-to-Speech',
    'tts_engine': 'Voice',
    'tts_volume_slider': 'Voice',
    'adv_ducking_level': 'Playback Reduction While Dictating',
}


def build(owner):
    from .calm_layout import group_page
    from PySide6.QtWidgets import QPushButton

    page = owner.make_page(TITLE, SUBTITLE, BASE, [row for row in ADVANCED if not row[3].startswith("echo_cancellation.")])
    from .presentation import SettingRow
    guide_button = QPushButton("Set up microphone...")
    guide_button.clicked.connect(
        lambda: getattr(owner.app, "open_mic_setup_guide", lambda: None)()
    )
    guide_row = SettingRow("Microphone", "Choose and test the microphone Samsara listens to.", [guide_button], parent=page.container)
    guide_row.setObjectName("microphoneSetupGuide")
    page.base.insertWidget(0, guide_row)
    owner._widgets["audio_mic_setup_guide_button"] = guide_button
    group_page(page, GROUPS)
    return page
