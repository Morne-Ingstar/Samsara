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
TERMS = {
    'command-reference': HelpTerm('command-reference', 'Command Reference',
        'The list of commands you can use in Samsara. Search for an action, then open a command to see its wording and details. Opening the list does not run a command.', 'commands/'),
    'wake-word': HelpTerm('wake-word', 'Wake word',
        'A phrase that tells Samsara to listen for a command without holding a key. {phrase} is the wake word offered in this build. Turn on Wake activation and complete its calibration first.', 'hands-free/#wake'),
    'hands-free': HelpTerm('hands-free', 'Hands-free',
        'A session for dictation and commands. Toggle starts it with one press and keeps it open; Hold keeps it open while you hold the button. How text is inserted follows your Hands-free sends setting.', 'hands-free/#sessions'),
    'listening-display': HelpTerm('listening-display', 'Listening display',
        'The on-screen display that shows what Samsara is doing while you speak. Compact is the small status display; Full is the larger dictation display. Choose your display in Settings.', 'settings/#ui.listening_display'),
    'command-packs': HelpTerm('command-packs', 'Command packs',
        'Groups of commands you can turn on or off. A command in a pack that is off cannot run until you turn that pack on. Opening this explanation does not change your packs.', 'commands/'),
    'ava': HelpTerm('ava', 'Ava',
        "Samsara's AI assistant. Availability depends on your Ava setup. Some features use a local model; others use the provider you configure. Open the Ava Setup Guide to see what is ready.", 'ava/#routes'),
}
_GUIDES = frozenset(term.guide for term in TERMS.values())


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
            # PLACEHOLDER COPY (939): required config-dependent version of 932.
            body = body.replace('listen for a command', 'start a hands-free session')
    elif term_id == 'hands-free' and session_detail:
        # Reuse the tutorial's existing detail_from_config result (keys/words/exit).
        if ((config or {}).get('hands_free') or {}).get('send_mode') == 'as_i_speak':
            import re
            # PLACEHOLDER COPY (939): existing Settings label, current delivery.
            session_detail = re.sub(r"Say a thought, then '[^']*' to paste it\.",
                                    'Hands-free sends: As I speak.', session_detail)
        body = session_detail + ' ' + body
    return HelpTerm(term.id, term.title, body, guide_url(term_id))
