# Quick Start - PGP Code Extraction

## One-Time Setup (5 minutes)

1. **Install LastPass CLI:**
   ```bash
   choco install lastpass-cli
   ```

2. **Login to LastPass:**
   ```bash
   lpass login your@email.com
   ```
   (Enter your master password)

3. **Install Python library:**
   ```bash
   pip install pyperclip
   ```

4. **Done!** You're ready to use the voice command.

---

## Daily Usage

### Simple 3-Step Workflow:

1. **Copy** the encrypted PGP message (Ctrl+C)
2. **Say:** "command mode on" then "decrypt code"
3. **Paste** the extracted code (Ctrl+V)

That's it! 🎉

---

## If LastPass Session Expires

LastPass CLI sessions last 1 hour by default. If you get an error:

```bash
lpass login your@email.com
```

To extend timeout to 8 hours:
```bash
setx LPASS_AGENT_TIMEOUT 28800
```

---

## Testing

Quick test to make sure everything works:

```bash
# Test 1: LastPass access
lpass show "Pgp public key pass"

# Test 2: Run the script manually
python "C:\Users\Morne\AppData\Local\DictationApp\pgp_code_extractor.py"
```

If both work, the voice command will work too!

---

## What It Does Behind the Scenes

```
Your voice: "decrypt code"
    ↓
1. Gets encrypted text from clipboard
2. Fetches PGP password from LastPass
3. Decrypts with GPG command line
4. Finds code on line 19
5. Puts code in clipboard
    ↓
You: Ctrl+V to paste
```

**Time saved per use:** ~2-3 minutes  
**Effort reduced:** From 12 steps to 3 steps

---

## Help

See full documentation: `PGP_SETUP_GUIDE.md`

Common issues:
- "LastPass CLI not found" → Install with choco
- "Not logged in" → Run `lpass login`
- "GPG not found" → Reinstall Gpg4win
