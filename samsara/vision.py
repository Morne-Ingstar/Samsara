"""
Vision bridge — local screenshot capture + Ollama vision model.

Privacy boundary: screenshots never leave this machine. The vision model
(qwen2.5vl:3b) runs locally via Ollama. Cloud LLM paths receive only
TEXT descriptions produced by the local model, never raw images or
base64-encoded data.
"""
import base64
import io
import logging

import requests

logger = logging.getLogger(__name__)


def _vision_config(app):
    return getattr(app, "config", {}).get("vision", {})


def is_vision_enabled(app) -> bool:
    return _vision_config(app).get("enabled", False)


class VisionBridge:
    """Screenshot capture + local Ollama vision model query."""

    def __init__(self, app):
        self._app = app

    def _model(self) -> str:
        return _vision_config(self._app).get("model", "qwen2.5vl:3b")

    def _host(self) -> str:
        cfg = getattr(self._app, "config", {})
        return cfg.get("ollama", {}).get("host", "http://localhost:11434")

    def _timeout(self) -> int:
        return _vision_config(self._app).get("timeout", 90)

    # ------------------------------------------------------------------
    # Screenshot methods
    # ------------------------------------------------------------------

    def screenshot_full(self) -> str:
        """Capture the primary monitor. Returns base64 JPEG string."""
        from PIL import ImageGrab
        img = ImageGrab.grab()
        if img.width > 1280:
            ratio = 1280 / img.width
            img = img.resize((1280, int(img.height * ratio)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def screenshot_window(self, hwnd) -> str:
        """Capture a specific window by HWND. Returns base64 JPEG string."""
        import ctypes
        import ctypes.wintypes as wintypes
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = (rect.left, rect.top, rect.right, rect.bottom)
        from PIL import ImageGrab
        img = ImageGrab.grab(bbox=bbox)
        if img.width > 1280:
            ratio = 1280 / img.width
            img = img.resize((1280, int(img.height * ratio)))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def screenshot_by_letter(self, letter: str):
        """Screenshot the window assigned to letter. Returns base64 JPEG or None."""
        try:
            from plugins.commands.window_switcher import get_window_by_letter
            hwnd = get_window_by_letter(letter)
            if hwnd is None:
                return None
            return self.screenshot_window(hwnd)
        except Exception as e:
            logger.error("[VISION] screenshot_by_letter failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Vision model
    # ------------------------------------------------------------------

    def describe(self, image_b64: str, prompt: str, timeout: int = 0) -> str | None:
        """Send image + prompt to the local vision model. Returns text or None."""
        t = timeout or self._timeout()
        payload = {
            "model": self._model(),
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [image_b64],
            }],
            "stream": False,
        }
        try:
            r = requests.post(
                f"{self._host()}/api/chat",
                json=payload,
                timeout=t,
            )
            if r.status_code != 200:
                logger.error("[VISION] HTTP %s from vision model", r.status_code)
                return None
            return r.json().get("message", {}).get("content", "")
        except Exception as e:
            logger.error("[VISION] describe failed: %s", e)
            return None

    def is_available(self) -> bool:
        """Return True if Ollama is reachable. Does not verify model is pulled."""
        try:
            r = requests.get(f"{self._host()}/api/tags", timeout=2)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Model warm-up
    # ------------------------------------------------------------------

    def warmup(self):
        """Send a 1x1 JPEG to pre-load the model into VRAM.

        Call from a background thread; eliminates the ~23s cold-start
        penalty on the first real vision request.
        """
        try:
            from PIL import Image
            img = Image.new("RGB", (1, 1), color=(255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            tiny_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
            logger.info("[VISION] Warming up vision model...")
            self.describe(tiny_b64, "ok", timeout=90)
            logger.info("[VISION] Vision model warm.")
        except Exception as e:
            logger.warning("[VISION] Warmup failed: %s", e)
