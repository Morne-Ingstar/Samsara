"""Display-only explanations for concealed or empty History rows (1042)."""


def display_text(row, text, no_speech):
    if row.get('privacy_reason'):
        # PLACEHOLDER COPY (1042): explain redaction without exposing any words.
        return ('Text hidden for a password field' if row['privacy_reason'] == 'password_field'
                else 'Text hidden because the target could not be verified')
    if no_speech:
        return 'No speech detected'
    if text.startswith('[FAILED] '):
        return 'Transcription failed: ' + text[len('[FAILED] '):]
    return ' '.join(text.split()) or 'No text was saved for this recording'
