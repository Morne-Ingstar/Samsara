"""
CUDA availability detection for Samsara.

Single source of truth for "can we actually use CUDA right now?". Used by:
  - Settings UI to gate the "CUDA (NVIDIA GPU)" dropdown option
  - Model loader to fall back gracefully if config says CUDA but DLLs are gone

Detection strategy: look for the CUDA runtime DLLs that ctranslate2 needs.
Specifically `cublas64_12.dll`. If it's not on the search path next to where
ctranslate2 lives (or in a sibling folder PyInstaller bundles into), CUDA
will fail to load — so we treat that as "not available".

We do NOT trust ctranslate2.get_supported_compute_types("cuda") for this
because (a) it raises in some envs, (b) it can lie when libraries are
half-installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


# The DLL whose presence we use as the marker. cublas64_12.dll is the
# largest of the bundled CUDA libs and is the one whose absence causes the
# "Library cublas64_12.dll is not found or cannot be loaded" runtime error.
_MARKER_DLL = "cublas64_12.dll"


def _probable_search_paths() -> list[Path]:
    """Where Samsara would look for CUDA DLLs at runtime."""
    paths: list[Path] = []

    # When frozen by PyInstaller, exe lives next to _internal/
    # ctranslate2 puts its DLLs in _internal/ctranslate2/
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        paths.append(exe_dir / "_internal" / "ctranslate2")
        paths.append(exe_dir / "ctranslate2")  # older PyInstaller layout
        paths.append(exe_dir)                   # CUDA pack drops them next to exe

    # When running from source, ctranslate2 site-package layout
    try:
        import ctranslate2  # noqa: F401
        ct2_path = Path(ctranslate2.__file__).parent
        paths.append(ct2_path)
    except Exception:
        pass

    # Anywhere on PATH (in case user installed CUDA system-wide)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            paths.append(Path(p))

    return paths


def is_cuda_available() -> bool:
    """Return True iff the CUDA marker DLL is findable on a probable path.

    Cheap to call (just stat checks). Cached on first call to avoid hitting
    the filesystem on every dropdown rebuild.
    """
    cached = getattr(is_cuda_available, "_cache", None)
    if cached is not None:
        return cached

    found = False
    for p in _probable_search_paths():
        try:
            if (p / _MARKER_DLL).exists():
                found = True
                break
        except OSError:
            continue

    is_cuda_available._cache = found  # type: ignore[attr-defined]
    return found


def cuda_status_message() -> str:
    """Human-readable single-line status. For settings UI hint text."""
    if is_cuda_available():
        return "CUDA detected — NVIDIA GPU acceleration available."
    return ("CUDA pack not installed. Download Samsara-CUDA-Pack from the "
            "Releases page and extract it next to Samsara.exe to enable "
            "GPU acceleration.")


def resolve_device(configured: str) -> str:
    """Translate config 'device' value to an actually-usable device string.

    If the config asks for CUDA but the DLLs aren't present, transparently
    return 'cpu' so the model loader doesn't crash. Caller is responsible
    for any user-facing notification about the fallback.
    """
    if configured == "cuda" and not is_cuda_available():
        return "cpu"
    return configured


def _reset_cache_for_test() -> None:
    """Clear the is_cuda_available cache. Test-only."""
    if hasattr(is_cuda_available, "_cache"):
        del is_cuda_available._cache  # type: ignore[attr-defined]
