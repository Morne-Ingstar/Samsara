# Troubleshoot Samsara Issues

Use this prompt to get AI help diagnosing Samsara without exposing your full
configuration or destroying a working profile.

---

## Prompt Template

Copy the block below and fill in the brackets:

---

I need help troubleshooting Samsara, a Windows voice-control and dictation
application.

### Relevant facts

- Transcription uses faster-whisper and supports CPU or optional NVIDIA CUDA.
- Samsara has hold-to-dictate, Continuous, persistent HANDS FREE, wake-word,
  command, and Ava entry points.
- Per-user settings and logs are normally under
  `%USERPROFILE%\.samsara\` for both source and packaged execution.
- The live log is `%USERPROFILE%\.samsara\logs\samsara.log`.

### Problem

**What happened:**
[Describe the visible behavior.]

**What should have happened:**
[Describe the expected behavior.]

**When it began:**
[After an update, setting change, device change, or always?]

**Relevant live-log tail:**
```text
[Paste only the lines around the failure. Remove private dictated text if needed.]
```

**Relevant settings:**
```json
[Paste only the necessary keys. Remove API keys, tokens, webhooks, and private paths.]
```

**System:**
- Windows version:
- CPU and NVIDIA GPU, if any:
- Samsara version:
- Packaged EXE or source checkout:
- Selected microphone:
- Selected device setting (auto/cpu/cuda):

Please diagnose the evidence first. Do not delete or overwrite my config, do
not install software, and do not change code unless I explicitly approve a
specific fix. Prefer one batched diagnostic and targeted tests.

---

## Safe First Checks

### App does not start

- Read the tail of `%USERPROFILE%\.samsara\logs\samsara.log`.
- Confirm another Samsara process is not already running.
- If the error names CUDA DLLs, use the official CPU build or reinstall the
  **complete** optional Samsara CUDA pack. Do not copy only cuBLAS, and do not
  install an arbitrary CUDA Toolkit as a generic fix. The packaged layout and
  exact ten-file requirement are documented in [../CUDA.md](../CUDA.md).
- Do **not** delete `config.json`. Samsara maintains `config.json.bak`; preserve
  both files while diagnosing.

### No transcription

- Confirm the selected microphone under **Settings → General**.
- Run **Run Mic Setup Guide** after changing devices or mic position.
- Check whether the live log shows speech reaching Whisper and whether an
  anti-hallucination or empty-output gate rejected it.

### Commands do not trigger

- Search for the phrase under **Settings → Commands**.
- Confirm its command pack is enabled.
- In HANDS FREE, commands must be exact complete utterances. Prefix ordinary
  dictation with `literal` when intentionally dictating a reserved command.
- Use **Reload** in the Commands page after an external `commands.json` edit.

### Slow transcription

- Check the startup log for the actual device and compute type. CUDA should
  report `Device: cuda, Compute: float16`.
- Try a smaller Whisper model or the Fast performance preset.
- Close other GPU-intensive applications before changing dependencies.

## Configuration Location

Source and packaged builds use the same default profile:

```text
%USERPROFILE%\.samsara\config.json
%USERPROFILE%\.samsara\config.json.bak
%USERPROFILE%\.samsara\logs\samsara.log
```

`SAMSARA_HOME_DIR` can deliberately point an isolated test or preview process
at another profile. A repository-root or EXE-folder `config.json` is not the
normal v0.22 settings location.
