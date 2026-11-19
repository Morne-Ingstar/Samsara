"""Display-only explanations for concealed or empty History rows (1042)."""


def recoverable(row):
    return row.get('privacy_reason') == 'unverified_target' and bool(
        row.get('display_text') or row.get('raw_text'))


def display_text(row, text, no_speech):
    if recoverable(row):
        # PLACEHOLDER COPY (1275)
        return 'Could not check where the words went. Text kept; select Show.'
    if row.get('privacy_reason'):
        # PLACEHOLDER COPY (1042): explain redaction without exposing any words.
        return ('Text hidden for a password field' if row['privacy_reason'] == 'password_field'
                else 'Text hidden because the target could not be verified')
    if no_speech:
        return 'No speech detected'
    if text.startswith('[FAILED] '):
        return 'Transcription failed: ' + text[len('[FAILED] '):]
    return ' '.join(text.split()) or 'No text was saved for this recording'
