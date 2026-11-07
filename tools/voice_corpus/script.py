"""Fixed references, never decoder-generated truth (packet 0.42.0, 1181)."""


def _item(number, text, instruction, tags, *, expected=None, kind="prose", seconds=15):
    return dict(id=f"line_{number:02d}", intended_text=text,
                expected_text=text if expected is None else expected,
                instruction=instruction, tags=tags.split(), kind=kind,
                suggested_seconds=seconds, sitting=(number - 1) // 15 + 1)


NUMBERS = ("one two three four five six seven eight nine ten eleven twelve "
           "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()
SCRIPT = [_item(i, f"Test {word}", "Say this once, then release.", "short repeated_holds packet_4", seconds=5)
          for i, word in enumerate(NUMBERS, 1)]

_LINES = [
    ("Recording still works", "Say this after the quick silent taps.", "recovery packet_5", None),
    ("", "Tap and release without speaking.", "silence quick_tap packet_5", None),
    ("", "Make another very quick silent tap.", "silence quick_tap packet_5", None),
    ("", "Hold for five seconds and breathe quietly.", "breathing silence packet_9", None),
    ("", "Hold for five seconds; say nothing.", "silence pause", None),
    ("Back to normal", "Speak normally after the silent takes.", "recovery packet_9", None),
    ("The parcel is on the table and that is all", "Make the final five words soft but audible.", "quiet_end packet_8", None),
    ("The parcel is on the table and that is all", "Repeat with a soft ending.", "quiet_end packet_8", None),
    ("Please leave the small blue parcel beside the garden gate", "Say garden gate softly.", "quiet_end soft_last_word", None),
    ("Every word before this pause should remain and these words come after the pause", "Pause five seconds after remain; keep holding.", "pause stray_words", None),
    ("The room is quiet and I have finished", "Wait five silent seconds after finished before releasing.", "pause quiet_end stray_words", None),
    ("We need to keep every single word", "Say the last word softly and release immediately.", "quiet_end release_edge", None),
    ("Section one every word before this pause should remain section two these words are after the earlier pause section three this is the final sentence of this section", "Speak slowly for about 46 seconds; pause five seconds between sections.", "long pause joins packet_7", None),
    ("Section one the first part is complete section two the second part is complete section three the final part is complete", "Take about one minute; pause five seconds between sections.", "long pause joins packet_7", None),
    ("I really really want this to stay exactly the same", "The repeated really is intentional.", "repeated_words legitimate_repeat", None),
    ("This sentence should appear once and only once", "Speak once at a normal pace.", "doubled_words", None),
    ("Morne sent the parcel to Quilter on Tuesday", "Say the names clearly.", "names", None),
    ("The invoice is for forty six dollars and seventy five cents", "Say every number as written.", "numbers", None),
    ("On Tuesday July fourteen the package weighed two point seven five kilograms", "Speak naturally.", "numbers names", None),
    ("First line new line second line", "Say new line as words.", "punctuation formatting", "First line\nsecond line"),
    ("Hello comma world full stop", "Say comma and full stop as words.", "punctuation formatting", "Hello, world."),
    ("Is the parcel ready question mark", "Say question mark as words.", "punctuation formatting", "Is the parcel ready?"),
    ("The music is background noise while I dictate this sentence", "Use music already playing; do not start it just for this take.", "background_music", None),
    ("The television is background noise while I dictate this sentence", "Use background speech already playing, if available; otherwise skip.", "background_speech", None),
    ("", "With existing background speech, hold silently for five seconds; otherwise skip.", "background_speech silence", None),
    ("Restore this exact draft", "Speak normally; this is prose.", "hands_free packet_15", None),
    ("clear draft", "Say this command; the recorder only saves it.", "command packet_15", None),
    ("bring it back", "Say this command; the recorder only saves it.", "command packet_15", None),
    ("show the change", "Say this command; nothing will be changed here.", "command review", None),
    ("keep that", "Say this command; the recorder saves the spoken script.", "command keep", None),
    ("yes", "Say the short reply normally.", "reply hands_free", None),
    ("no", "Say the short reply softly but audibly.", "reply hands_free quiet", None),
    ("stop", "Say this short command normally.", "reply command hands_free packet_17", None),
    ("cancel", "Say this short reply normally.", "reply hands_free", None),
    ("pause VLC", "Say this command; no player will be controlled.", "command media packet_19", None),
    ("VLC skip forward thirty seconds", "Say this command; no player will be controlled.", "command media numbers packet_19", None),
    ("play VLC", "Say this command; no player will be controlled.", "command media packet_19", None),
    ("pause Spotify", "Say this command; no player will be controlled.", "command media packet_20", None),
    ("Spotify volume up", "Say this command; no player will be controlled.", "command media packet_20", None),
    ("Before the switch and after the switch", "Speak once without switching windows.", "prose packet_11", None),
]
for number, (text, instruction, tags, expected) in enumerate(_LINES, 21):
    SCRIPT.append(_item(number, text, instruction, tags, expected=expected,
                        kind="command" if "command" in tags.split() else
                             "reply" if "reply" in tags.split() else "prose",
                        seconds=60 if "long" in tags else 15))

assert len(SCRIPT) == 60
