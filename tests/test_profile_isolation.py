"""Test for wake_profile isolation (P1-A).

Regression covered: each wake_profile carries its own send_word (agentic-
safety requirement -- see samsara/wake_profiles.py's
normalize_profile_mode_and_send_word), but the wake_session termination
check in dictation.py always read the shared/global wake_word_config.
send_words list instead of the DISPATCHING profile's own send_word. Since
the module-level default (_WAKE_SESSION_SEND_WORDS) contains every
profile's example word together, one profile's terminator ("over") could
prematurely end a DIFFERENT profile's session (send_word "send") --
profile-scoped state leaking across a profile switch.

Follows tests/test_dictation_app.py's "Mock() as self, call the unbound
method directly" pattern: DictationApp.__init__ pulls in audio/Whisper/Qt
machinery this fix doesn't touch, so a full instance isn't needed to
exercise _dispatch_wake_profile / _start_wake_session / _reset_wake_dictation.
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from dictation import DictationApp


def test_profile_isolation():
    app = Mock()
    app._wake_session_send_word = None
    app._start_wake_session = Mock(wraps=lambda **kw: DictationApp._start_wake_session(app, **kw))

    # Profile A dispatches ("hey claude", send_word "over").
    with patch('dictation._resolve_target_window', return_value=(1234, 'Target Window')), \
         patch('plugins.commands.window_switcher._force_focus', return_value=True):
        profile_a = {
            'id': 'claude', 'target_process': 'claude.exe',
            'phrase': 'hey claude', 'send_word': 'over', 'mode': 'focus_dictate',
        }
        DictationApp._dispatch_wake_profile(app, profile_a)

    # _dispatch_wake_profile must forward THIS profile's own send_word, and
    # _start_wake_session must store exactly that value.
    assert app._start_wake_session.call_args.kwargs['send_word'] == 'over'
    assert app._wake_session_send_word == 'over'

    # Profile B dispatches next ("hey hermes", send_word "send") -- must NOT
    # inherit profile A's leftover "over".
    with patch('dictation._resolve_target_window', return_value=(5678, 'Other Window')), \
         patch('plugins.commands.window_switcher._force_focus', return_value=True):
        profile_b = {
            'id': 'hermes', 'target_process': 'hermes.exe',
            'phrase': 'hey hermes', 'send_word': 'send', 'mode': 'stage_send',
        }
        DictationApp._dispatch_wake_profile(app, profile_b)

    assert app._start_wake_session.call_args.kwargs['send_word'] == 'send'
    assert app._wake_session_send_word == 'send'
    assert app._wake_session_send_word != 'over'  # no leak from profile A

    # Session end is the isolation boundary: a leftover send_word surviving
    # past _reset_wake_dictation could leak into whatever starts next.
    DictationApp._reset_wake_dictation(app)
    assert app._wake_session_send_word is None
