"""Smoke test for WinRTEngine. Run from the samsara-dev root:

    python scripts/tts_smoke_test.py

Expected output:
  - List of installed OneCore voices
  - Audible speech via sounddevice (not via WinRT's own audio session)
  - Clean exit
"""

import sys
import time
from pathlib import Path

# Allow running from repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from samsara.tts import WinRTEngine


def main():
    print("Initializing WinRTEngine...")
    engine = WinRTEngine()

    print("\nAvailable voices:")
    for v in engine.list_voices():
        print(f"  [{v.gender:6}] {v.display_name!r} ({v.language})  id={v.voice_id!r}")

    phrase = "Note saved. Your reminder will be in the brain dump."
    print(f"\nSpeaking: {phrase!r}")

    t0 = time.monotonic()
    handle = engine.speak(phrase)
    elapsed_ms = (time.monotonic() - t0) * 1000
    print(f"speak() returned in {elapsed_ms:.1f} ms (should be <50 ms)")

    print("Waiting for playback to finish...")
    while engine.is_speaking():
        time.sleep(0.05)

    print("\nDone.")
    engine.shutdown()


if __name__ == "__main__":
    main()
