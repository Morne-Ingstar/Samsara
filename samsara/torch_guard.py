"""Keep optional CTranslate2 conversion dependencies out of app startup.

Call install() before third-party imports. Set SAMSARA_ALLOW_TORCH=1 before
startup for development tools that need PyTorch in the same interpreter.
"""

from importlib.abc import MetaPathFinder
import os
import sys

_checked = False


class _TorchGuardFinder(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "torch" or fullname.startswith("torch."):
            raise ImportError(
                f"{fullname} import blocked by Samsara; "
                "set SAMSARA_ALLOW_TORCH=1 before startup to allow PyTorch"
            )
        return None


_finder = _TorchGuardFinder()


def install() -> None:
    """Block future torch imports once, without disturbing an existing module."""
    global _checked
    if _checked:
        return

    if os.environ.get("SAMSARA_ALLOW_TORCH") == "1":
        status = "disabled (SAMSARA_ALLOW_TORCH=1)"
    elif "torch" in sys.modules:
        status = "disabled (torch already in sys.modules)"
    else:
        # Keep sys.modules untouched: scipy probes it without importing torch.
        sys.meta_path.insert(0, _finder)
        status = "enabled (optional torch imports blocked)"
    _checked = True

    from samsara.log import get_logger

    get_logger(__name__).info("[TORCH-GUARD] %s", status)


def uninstall() -> None:
    """Remove this guard and allow a later explicit install()."""
    global _checked
    if _finder in sys.meta_path:
        sys.meta_path.remove(_finder)
    _checked = False
