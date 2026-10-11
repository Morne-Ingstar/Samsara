"""Pure first-run recommendations and owner-approved copy (373)."""
from samsara.config_schema import SETTINGS_SCHEMA


def recommend_model(facts):
    """Use conservative CPU defaults when a hardware fact is unavailable."""
    cores, ram = facts.get('cpu_cores'), facts.get('ram_gb')
    if isinstance(cores, (int, float)) and 0 < cores < 4:
        return 'tiny', 'low_cores'
    if isinstance(ram, (int, float)) and 0 < ram < 8:
        return 'tiny', 'low_ram'
    if facts.get('gpu_usable') is True and facts.get('cuda_chosen') is True:
        return 'small', 'gpu_cuda'
    if facts.get('gpu_usable') is True:
        return 'base', 'cuda_not_chosen'
    if facts.get('gpu_usable') is False:
        return 'base', 'no_gpu'
    return 'base', 'unknown_hardware'


def recommend_language(system_locale_name, allowed):
    """Match a supported language code; never select Auto on a failed probe."""
    code = (system_locale_name or '').replace('-', '_').split('_')[0].lower()
    if code in allowed:
        return code
    return SETTINGS_SCHEMA['language']['default']


MODEL_REASONS = {
    'low_cores': 'Picked Tiny because this computer has fewer than four CPU cores.',
    'low_ram': 'Picked Tiny because this computer has less than 8 GB of memory.',
    'gpu_cuda': 'Picked Small because this computer has a supported NVIDIA graphics card and the CUDA add-on.',
    'cuda_not_chosen': 'Picked Base because the CUDA add-on has not been installed.',
    'no_gpu': 'Picked Base because this computer has no supported NVIDIA graphics card.',
    'unknown_hardware': 'Picked Base because the graphics hardware could not be checked.',
}
COPY = {
    'recognition.title': 'Recognition',
    'progress.microphone': 'Mic',
    'progress.components': 'Add-ons',
    'progress.recognition': 'Speech',
    'progress.display': 'Display',
    'recognition.change': 'You can change it here or later in Settings.',
    'recognition.language': 'Language',
    'recognition.current': 'Current recognition model',
    'talking.title': 'Talking',
    'talking.question': 'How do you want to record?',
    'talking.current': 'Current recording mode',
    'talking.hold': 'Hold',
    'talking.toggle': 'Tap',
    'talking.record_key': 'Record key',
    'talking.hands_free': 'Hands-free button',
    'talking.session_off': 'Hands-free is off; turn it on in Settings > Dictation.',
    'talking.wake': 'Use a wake word?',
    'talking.on': 'On',
    'talking.off': 'Off',
    'talking.phrase': 'Wake phrase',
    'talking.tutorial': 'The other ways of talking are shown in the tutorial.',
    'music.title': 'Music',
    'music.question': 'What happens to other audio while you record?',
    'voice.title': 'Voice',
    'voice.question': 'How much should Samsara speak?',
    'voice.engine': 'Which voice?',
    'voice.local': 'Local',
    'voice.network': 'Network',
    'voice.natural': 'The network voice sounds more natural.',
    'complete.startup': 'Start Samsara when I sign in to Windows',
    'welcome.recognition': 'Choose recognition and language',
    'welcome.talking': 'Choose how to talk, music and voice',
}
CHOICE_DETAILS = {
    'duck': 'Lowers other audio while you record, then restores its volume.',
    'pause': 'Stops media while you record and resumes only sessions Samsara paused.',
    'off': 'Leaves other audio playing at its current volume.',
    'silent': 'Shows feedback on screen without speaking.',
    'brief': 'Speaks questions and read-backs; routine results use a short sound.',
    'full': 'Speaks every result.',
    'winrt': 'Makes speech on this computer without sending it to a voice service.',
    'edge': 'Sends the text of spoken feedback to Microsoft to make speech.',
}
