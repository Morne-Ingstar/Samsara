"""
CUDA availability detection for Samsara.

Single source of truth for "can we actually use CUDA right now?". Used by:
  - Settings UI to gate the "CUDA (NVIDIA GPU)" dropdown option
  - Model loader to fall back gracefully if config says CUDA but DLLs are gone

Detection strategy: look for the complete set of CUDA runtime DLLs that
ctranslate2 needs. If any are absent from the probable DLL search paths, CUDA
will fail to load — so we treat an incomplete pack as "not available".

We do NOT trust ctranslate2.get_supported_compute_types("cuda") for this
because (a) it raises in some envs, (b) it can lie when libraries are
half-installed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from samsara.log import get_logger

logger = get_logger(__name__)


# Keep this aligned with the CUDA pack assembled in scripts/samsara.spec.
_REQUIRED_CUDA_DLLS = (
    "cudnn_adv64_9.dll",
    "cudnn_cnn64_9.dll",
    "cudnn_engines_precompiled64_9.dll",
    "cudnn_engines_runtime_compiled64_9.dll",
    "cudnn_graph64_9.dll",
    "cudnn_heuristic64_9.dll",
    "cudnn_ops64_9.dll",
    "cublas64_12.dll",
    "cublasLt64_12.dll",
    "cudart64_12.dll",
)


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
    except Exception as e:
        logger.debug(f"_probable_search_paths: {e}")

    # Anywhere on PATH (in case user installed CUDA system-wide)
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            paths.append(Path(p))

    return paths


def _missing_cuda_dlls() -> tuple[str, ...]:
    """Return required CUDA DLLs not found on any probable search path."""
    remaining = set(_REQUIRED_CUDA_DLLS)
    for path in _probable_search_paths():
        for dll in tuple(remaining):
            try:
                if (path / dll).exists():
                    remaining.remove(dll)
            except OSError:
                continue
        if not remaining:
            break
    return tuple(dll for dll in _REQUIRED_CUDA_DLLS if dll in remaining)


def is_cuda_available() -> bool:
    """Return True iff every required CUDA DLL is findable.

    Cheap to call (just stat checks). Cached on first call to avoid hitting
    the filesystem on every dropdown rebuild.
    """
    cached = getattr(is_cuda_available, "_cache", None)
    if cached is not None:
        return cached

    missing = _missing_cuda_dlls()
    available = not missing
    is_cuda_available._missing_dlls = missing  # type: ignore[attr-defined]
    is_cuda_available._cache = available  # type: ignore[attr-defined]
    return available


def cuda_status_message() -> str:
    """Human-readable single-line status. For settings UI hint text."""
    if is_cuda_available():
        return "CUDA detected — NVIDIA GPU acceleration available."
    missing = getattr(is_cuda_available, "_missing_dlls", ())
    return (
        f"CUDA pack incomplete. Missing DLLs: {', '.join(missing)}. "
        "Extract the complete Samsara-CUDA-Pack into the ctranslate2 folder."
    )


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
    for attribute in ("_cache", "_missing_dlls"):
        if hasattr(is_cuda_available, attribute):
            delattr(is_cuda_available, attribute)
