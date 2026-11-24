"""Six packaged explanations. Callers supply current config; no I/O or glossary fetch."""
from dataclasses import dataclass

from samsara.onboarding.guidance_facts import facts_from_config
from samsara.support_feedback import DOCUMENTATION_URL


@dataclass(frozen=True)
class HelpTerm:
    id: str
    title: str
    body: str
    guide: str | None


# PLACEHOLDER COPY (932): approved design copy, not owner-authored final copy.
# PLACEHOLDER COPY (1127): canonical feature names.
# PLACEHOLDER COPY (1331): shared by the Microphone row and its voice shortcut.
MICROPHONE_HELP = "Choose the microphone Samsara listens to."

TERMS = {
    'command-reference': HelpTerm('command-reference', 'Command Reference',
        'Command Reference shows what to say to use a voice command. Search for an action, then select a command to see its exact wording.', 'commands/'),
    'wake-word': HelpTerm('wake-word', 'Wake word',
        'Say “{phrase}” to make Samsara listen for one command. To set it up, open Settings > Dictation > Wake word setup.', 'hands-free/#wake'),
    'hands-free': HelpTerm('hands-free', 'Hands-free',
        'Hands-free lets you speak commands or turn speech into text without holding a key. To start with a shortcut, turn on Session button in Settings > Dictation.', 'hands-free/#sessions'),
    'listening-display': HelpTerm('listening-display', 'Listening display',
        'The on-screen display that shows what Samsara is doing while you speak. Compact is the small status display; Full is the larger dictation display. Choose your display in Settings.', 'settings/#ui.listening_display'),
    'command-packs': HelpTerm('command-packs', 'Command packs',
        'Groups of commands you can turn on or off. A command in a pack that is off cannot run until you turn that pack on. Opening this explanation does not change your packs.', 'commands/'),
    'ava': HelpTerm('ava', 'Ava',
        "Samsara's AI assistant. Availability depends on your Ava setup. Some features use a local model; others use the provider you configure. Open the Ava Setup Guide to see what is ready.", 'ava/#routes'),
}
_GUIDES = frozenset(term.guide for term in TERMS.values())

# PLACEHOLDER COPY (1024): local page help never supplies a browser destination.
PAGE_HELP = {
    'Home': 'Use the listening control to pause or resume. Open Commands to browse commands, or Help for offline explanations, Quick Reference and recovery tools.',
    'General': 'In General, you can change the size of text and buttons and how Samsara looks. Choose Interface size and Theme, then select Apply.',
    'Dictation': 'Dictation turns what you say into text. In Settings > Dictation, use Hands-free sends to choose when that text goes into your app.',
    'Audio': 'In Audio, you can choose which sounds and spoken replies you hear. Use Feedback sounds and Voice feedback, then select Apply.',
    'Ava': 'Choose and set up your assistant. Availability depends on your configured provider and permissions. Apply saves changes.',
    'Shortcuts': 'Choose keyboard shortcuts for dictation and commands. Apply saves changes.',
    'Help': 'Open an offline explanation or Quick Reference without a microphone or Internet connection. Setup guides, support and diagnostics remain below. Web links open only when you select them.',
    'History': 'Review earlier dictation here. Opening history does not dictate or run a command.',
    'Dictionary': 'Add words and preferred spellings to improve recognition.',
    'Memos': 'Review saved memos here.',
    'Snippets': 'Manage reusable text here.',
    'Voice help': 'Review voice troubleshooting and microphone guidance here. You can use these controls while the microphone is off.',
}


def page_explanation(name):
    return HelpTerm('page', name + ' help', PAGE_HELP.get(name,
        'Review the controls on this page. Open Settings > Help for offline explanations, Quick Reference and recovery tools.'), None)


def guide_url(term_id):
    """Only packaged destinations, never caller text or config in a URL."""
    term = TERMS.get(term_id)
    if term is None or term.guide not in _GUIDES or term.guide is None:
        return None
    if DOCUMENTATION_URL != 'https://morneis.com/samsara/docs/':
        return None
    return DOCUMENTATION_URL + term.guide


def explanation(term_id, config=None, *, session_detail=None):
    term = TERMS.get(term_id)
    if term is None:
        # PLACEHOLDER COPY (932)
        return HelpTerm('', '', 'This explanation is unavailable in this version.', None)
    body = term.body
    if term_id == 'wake-word':
        facts = facts_from_config(config)
        body = body.format(phrase=facts.wake_phrase)
        if facts.wake_activation == 'hands_free_session':
            # 1331: the wake phrase starts ongoing listening in this setup.
            body = body.replace('listen for one command', 'start listening without a key')
    elif term_id == 'hands-free' and session_detail:
        # Reuse the tutorial's existing detail_from_config result (keys/words/exit).
        if ((config or {}).get('hands_free') or {}).get('send_mode') == 'as_i_speak':
            import re
            # PLACEHOLDER COPY (939): existing Settings label, current delivery.
            session_detail = re.sub(r"Say a thought, then '[^']*' to paste it\.",
                                    'Hands-free sends: As I speak.', session_detail)
        body = session_detail + ' ' + body
    return HelpTerm(term.id, term.title, body, guide_url(term_id))
