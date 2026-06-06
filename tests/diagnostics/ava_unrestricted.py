"""
Ava Unrestricted — DeepSeek test harness.

Purpose: talk to DeepSeek through the SAME provider/key/endpoint that
Samsara's Ava uses, but with NONE of the command-router / TTS-brevity
scaffolding. This isolates the variable: it answers the question
"is the limitation the model, or Samsara's cage?"

It reads cloud_llm config straight from the real Samsara config file
(same api_key, provider, base_url, model). It does NOT modify config,
does NOT touch the running app, does NOT change the plugin. Throwaway
diagnostic.

Multi-turn: unlike cloud_llm.send() (single-shot), this keeps full
conversation history so it behaves like a normal chat with DeepSeek.

Run:  F:\envs\sami\python.exe tests\diagnostics\ava_unrestricted.py
Quit: type  /quit   (or Ctrl+C)
Reset conversation:  /reset
Show the active system prompt:  /system
"""

import json
import os
import sys
from pathlib import Path

import requests

# ── Locate and load the real Samsara config (read-only) ───────────────────
def _find_config():
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "DictationApp" / "config.json",
        Path.home() / "AppData" / "Local" / "DictationApp" / "config.json",
        Path(r"C:\Users\Morne\AppData\Local\DictationApp\config.json"),
    ]
    for c in candidates:
        if c and c.is_file():
            return c
    return None


def _load_cloud_cfg():
    cfg_path = _find_config()
    if not cfg_path:
        print("[!] Could not find Samsara config.json. Checked APPDATA and "
              "Local\\DictationApp.")
        sys.exit(1)
    with open(cfg_path, "r", encoding="utf-8") as f:
        full = json.load(f)
    cloud = full.get("cloud_llm", {})
    if not cloud.get("api_key"):
        print(f"[!] No cloud_llm.api_key in {cfg_path}. Enable cloud mode / "
              f"set a key in Samsara settings first.")
        sys.exit(1)
    return cfg_path, cloud


def _provider_bits(cloud):
    provider = cloud.get("provider", "deepseek")
    providers = cloud.get("providers", {})
    defaults = {
        "deepseek": {"base_url": "https://api.deepseek.com/v1",
                     "model": "deepseek-chat"},
        "openai":   {"base_url": "https://api.openai.com/v1",
                     "model": "gpt-4o-mini"},
        "anthropic":{"base_url": "https://api.anthropic.com/v1",
                     "model": "claude-sonnet-4-20250514"},
    }
    pcfg = providers.get(provider, defaults.get(provider, {}))
    model = cloud.get("model") or pcfg.get("model", "deepseek-chat")
    base_url = pcfg.get("base_url", "https://api.deepseek.com/v1")
    return provider, base_url, model


# ── No system prompt ──────────────────────────────────────────────────────
# Deliberately NONE. No command router, no TTS cage, no personality, no
# steering of any kind. The conversation starts directly at the user's
# first turn. This is the closest to raw DeepSeek the API allows — the
# point is to observe what the model is with nothing imposed on it.
# Full conversation memory IS kept (the thing real Ava lacks).
SYSTEM_PROMPT = None  # set to a string only if you want to experiment


def main():
    cfg_path, cloud = _load_cloud_cfg()
    provider, base_url, model = _provider_bits(cloud)
    api_key = cloud["api_key"]
    timeout = cloud.get("timeout_seconds", 60)

    print("=" * 64)
    print("  AVA UNRESTRICTED — direct DeepSeek/cloud test harness")
    print("=" * 64)
    print(f"  config:   {cfg_path}")
    print(f"  provider: {provider}")
    print(f"  base_url: {base_url}")
    print(f"  model:    {model}")
    print(f"  timeout:  {timeout}s")
    print("  commands: /quit  /reset  /system  /history")
    print("=" * 64)
    print("  Raw model, your real key + endpoint, NO system prompt,")
    print("  NO Samsara constraints. Full conversation memory kept.")
    print("=" * 64)

    if provider == "anthropic":
        print("[note] Anthropic uses a different API shape; this harness is "
              "built for the OpenAI-compatible path (DeepSeek/OpenAI). "
              "Switch provider to deepseek to test that connection.")

    def _fresh_history():
        return [{"role": "system", "content": SYSTEM_PROMPT}] if SYSTEM_PROMPT else []

    history = _fresh_history()

    while True:
        try:
            user = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[bye]")
            return

        if not user:
            continue
        if user == "/quit":
            print("[bye]")
            return
        if user == "/reset":
            history = _fresh_history()
            print("[conversation reset — memory cleared]")
            continue
        if user == "/system":
            print("\n--- active system prompt ---")
            print(SYSTEM_PROMPT if SYSTEM_PROMPT else "(none — raw model, no steering)")
            print("--- end ---")
            continue
        if user == "/history":
            print(f"\n[{len(history)} messages in memory]")
            continue

        history.append({"role": "user", "content": user})

        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": history,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        try:
            r = requests.post(url, json=payload, headers=headers,
                              timeout=timeout)
            if r.status_code != 200:
                print(f"[http {r.status_code}] {r.text[:500]}")
                history.pop()  # drop the user turn that failed
                continue
            data = r.json()
            reply = data["choices"][0]["message"]["content"]
        except requests.exceptions.Timeout:
            print(f"[timeout after {timeout}s]")
            history.pop()
            continue
        except Exception as e:
            print(f"[error] {e}")
            history.pop()
            continue

        history.append({"role": "assistant", "content": reply})
        print(f"\nava> {reply}")


if __name__ == "__main__":
    main()
