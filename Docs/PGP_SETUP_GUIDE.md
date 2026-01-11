# PGP Code Extractor - Setup Guide

## What This Does
Automates your PGP workflow with a single voice command "decrypt code":
1. ✅ Gets encrypted message from clipboard
2. ✅ Retrieves your PGP password from LastPass automatically  
3. ✅ Decrypts using GPG command line
4. ✅ Extracts the code from line 19
5. ✅ Puts code in clipboard ready to paste

**Before:** 8+ steps involving multiple apps, password lookup, copy/paste operations
**After:** Say "decrypt code" → paste the result

## Prerequisites

### 1. LastPass CLI (Command Line Interface)

**Install:**
```powershell
choco install lastpass-cli
```

Or download from: https://github.com/lastpass/lastpass-cli/releases

**First-time setup:**
```bash
# Login to LastPass (one-time setup)
lpass login your@email.com

# Verify it works
lpass show "Pgp public key pass"
```

**Note:** LastPass CLI session lasts 1 hour by default. You can extend it:
```bash
# Set timeout to 8 hours (in seconds)
setx LPASS_AGENT_TIMEOUT 28800
```

### 2. GPG Command Line (Should Already Be Installed)

If you have Kleopatra installed, GPG is already there. The script will automatically find it at:
- `C:\Program Files (x86)\GnuPG\bin\gpg.exe`

**Verify:**
```bash
gpg --version
```

If not found, reinstall Gpg4win: https://gpg4win.org/

### 3. Python Libraries

The script needs pyperclip for clipboard access:
```bash
pip install pyperclip
```

## Usage

### Method 1: Voice Command (Recommended)

1. **Copy encrypted PGP message to clipboard** (Ctrl+C)
2. **Enable command mode:** Say "command mode on"
3. **Say:** "decrypt code"
4. **Paste the result** into the text box (Ctrl+V)

### Method 2: Direct Execution

```bash
python "C:\Users\Morne\AppData\Local\DictationApp\pgp_code_extractor.py"
```

## Workflow Example

**Old way:**
1. Copy public key → Paste into website
2. Copy encrypted response
3. Open Kleopatra
4. Paste into Kleopatra
5. Open LastPass
6. Find PGP password
7. Copy password
8. Paste into Kleopatra
9. Click Decrypt
10. Find line 19
11. Copy code
12. Paste into website

**New way:**
1. Copy encrypted response (Ctrl+C)
2. Say "decrypt code"
3. Paste code into website (Ctrl+V)

## Troubleshooting

### "LastPass CLI not found"
- Install using: `choco install lastpass-cli`
- Add to PATH if needed

### "Are you logged in?"
- Run: `lpass login your@email.com`
- Enter your master password

### "GPG not found"
- Reinstall Gpg4win
- Or add GPG to your PATH

### "Could not find code in decrypted message"
The script looks for line 19, but if the format is different:
- Check the console output - it shows what it found
- The script will search for code patterns if line 19 fails
- You may need to adjust the line number in the script

### Code is on a different line?
Edit `pgp_code_extractor.py` and change:
```python
# Line 19 (0-indexed = line 18)
if len(lines) >= 19:
    code = lines[18].strip()  # Change 18 to your line number - 1
```

## Security Notes

- LastPass CLI stores credentials encrypted on disk
- Session timeout helps protect your passwords
- GPG passphrase is passed securely via command line
- Temporary files are cleaned up automatically
- No passwords are logged or saved by the script

## Files Created

- `C:\Users\Morne\AppData\Local\DictationApp\pgp_code_extractor.py` - Main script
- Updated: `C:\Users\Morne\AppData\Local\DictationApp\commands.json` - Voice command

## Testing

1. **Test LastPass access:**
   ```bash
   lpass show "Pgp public key pass"
   ```
   Should show your PGP password

2. **Test GPG:**
   ```bash
   gpg --version
   ```
   Should show GPG version info

3. **Test the full workflow:**
   - Copy any encrypted PGP message
   - Run the script manually first
   - Check if code is extracted correctly
   - Then try with voice command

## Customization

### Different LastPass Entry Name
Edit the script and change:
```python
passphrase = get_lastpass_password("Pgp public key pass")
```

### Different Line Number for Code
Edit the script and change:
```python
code = lines[18].strip()  # Line 19 (0-indexed)
```

### Add More Commands
See `CUSTOM_COMMANDS.md` in the dictation app folder for adding more automation.

## Support

If you encounter issues:
1. Run the script manually to see detailed error messages
2. Check that LastPass CLI is logged in
3. Verify GPG is accessible
4. Check the console output for hints

The script provides detailed feedback at each step, making it easy to identify where issues occur.
