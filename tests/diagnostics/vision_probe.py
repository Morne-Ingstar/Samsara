"""
Hemispheres vision probe — time Qwen2.5-VL on a real screenshot.
Same pattern as ACE-00: a throwaway diagnostic that answers the
gating question with a number.

Run: F:\envs\sami\python.exe tests\diagnostics\vision_probe.py
"""

import time
import base64
import io
import json
import requests
from PIL import ImageGrab

OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen2.5vl:3b"

def screenshot_to_base64():
    """Capture primary monitor, return base64 JPEG."""
    t0 = time.perf_counter()
    img = ImageGrab.grab()
    # Resize to reduce token count — 1280px wide max
    if img.width > 1280:
        ratio = 1280 / img.width
        img = img.resize((1280, int(img.height * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    elapsed = time.perf_counter() - t0
    print(f"Screenshot: {img.width}x{img.height}, "
          f"{len(buf.getvalue())//1024}KB JPEG, captured in {elapsed:.3f}s")
    return b64

def probe_vision(b64_image, prompt):
    """Send image to Qwen2.5-VL via Ollama, return response + timing."""
    payload = {
        "model": MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [b64_image],
            }
        ],
        "stream": False,
    }
    t0 = time.perf_counter()
    r = requests.post(OLLAMA_URL, json=payload, timeout=300)
    elapsed = time.perf_counter() - t0
    if r.status_code != 200:
        return None, elapsed, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json()
    reply = data.get("message", {}).get("content", "")
    return reply, elapsed, None

def main():
    print("=" * 60)
    print("  HEMISPHERES VISION PROBE — Qwen2.5-VL latency test")
    print("=" * 60)
    print(f"  Model: {MODEL}")
    print(f"  Endpoint: {OLLAMA_URL}")
    print()

    # Test 1: "What's on screen?" — general description
    print("--- Test 1: General screen description ---")
    b64 = screenshot_to_base64()
    reply, elapsed, err = probe_vision(b64,
        "Briefly describe what applications and windows are visible "
        "on this screen. Be concise — 2-3 sentences max.")
    if err:
        print(f"ERROR: {err}")
        return
    print(f"Time: {elapsed:.2f}s")
    print(f"Reply: {reply}")
    print()

    # Test 2: "Find UI elements" — structured output
    print("--- Test 2: UI element identification ---")
    b64 = screenshot_to_base64()
    reply, elapsed, err = probe_vision(b64,
        "List the clickable UI elements visible in this screenshot "
        "(buttons, links, input fields, tabs). For each, give a short "
        "name and approximate position (top-left, center, bottom-right, "
        "etc). Return as a short list, max 10 items.")
    if err:
        print(f"ERROR: {err}")
        return
    print(f"Time: {elapsed:.2f}s")
    print(f"Reply: {reply}")
    print()

    # Test 3: Targeted question — specific content extraction
    print("--- Test 3: Content extraction ---")
    b64 = screenshot_to_base64()
    reply, elapsed, err = probe_vision(b64,
        "What text is visible in the largest window on screen? "
        "Give me the first 2-3 lines of visible text content.")
    if err:
        print(f"ERROR: {err}")
        return
    print(f"Time: {elapsed:.2f}s")
    print(f"Reply: {reply}")
    print()

    # Summary
    print("=" * 60)
    print("  RESULTS SUMMARY")
    print("  If all three tests completed under 5s each: GO")
    print("  If 5-10s each: usable with async 'thinking' indicator")
    print("  If 10+s: vision projector likely on CPU, investigate")
    print("=" * 60)

if __name__ == "__main__":
    main()
