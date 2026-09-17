"""Optional components: the wizard page and the headless fetch mode (34).

Two consumers of the release manifest (docs/RELEASE_MANIFEST.md) and of
samsara.components (33):

  * ComponentsPage -- ONE page added to the first-run wizard after the
    microphone step. It lists every component that is not installed, with
    its size and what it enables, and offers "Get <name>" now or "Later".
    Everything the installer offered stays reachable here, forever.

  * fetch_components_main(argv) -- the headless mode the installer runs
    after copying files:

        Samsara.exe --fetch-components cuda-pack,wake-word-models \\
            --manifest https://.../manifest.json [--components-dir DIR]
            [--app-root DIR] [--downloads-dir DIR] [--progress-window]

    It validates every id against the manifest BEFORE touching disk (a bad
    id exits 2 with nothing written), downloads through
    components.download_component (SHA-256, resume), unpacks into the
    component's install_dir, and is cancellable at any point: a cancel or a
    lost network leaves the core install exactly as it was (exit 4 / 5), so
    the installer still succeeds with core only and says so.

    The dispatch that routes "--fetch-components" from Samsara.exe's entry
    point to this function landed in 108: dictation.py calls it from
    _dispatch_startup_argument, at the TOP of the module, above every heavy
    import and above the single-instance lock. Above the lock is the part
    that matters -- below it, a fetch requested while a session was already
    running exited 0 without fetching anything.

No network access at import time. Every network or file-serving path goes
through an injectable opener so tests never touch the network.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from samsara import components
from samsara.log import get_logger
from samsara.runtime import thread_registry

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_BAD_ARGS = 2
EXIT_UNAVAILABLE = 3
EXIT_NETWORK = 4
EXIT_CANCELLED = 5
EXIT_FAILED = 6

DEFAULT_REPO = "Morne-Ingstar/Samsara"
MIN_TARGET = 44
INSTALLED_MARKER_SUFFIX = ".installed"
#: The wizard reads the network in 256 KB pieces (capped_opener) so a
#: cancel, polled after every piece, is honoured within about a second even
#: on a slow link; the headless installer mode keeps the fetcher's 1 MB reads.
WIZARD_CHUNK = 256 * 1024
#: How long the page waits for a cancelled worker when its window closes.
CLOSE_JOIN_S = 1.0

#: What each component enables, in one line, for the wizard page. Unknown
#: ids fall back to the manifest description.
COMPONENT_BENEFITS = {
    "core": "The application itself.",
    "cuda-pack": "Faster transcription on an NVIDIA GPU.",
    "wake-word-models": "Hands-free control: say the wake word instead of pressing a key.",
    "command-model": "A local model for the hands-free command lane.",
}
COMING_SOON = "Coming soon"
NO_NVIDIA = "No NVIDIA GPU detected"


class FetchCancelled(RuntimeError):
    """The user cancelled; nothing under the final name was touched."""


# ---------------------------------------------------------------------------
# Paths and state
# ---------------------------------------------------------------------------

def default_downloads_dir() -> Path:
    from samsara.paths import samsara_home_dir  # noqa: PLC0415

    return samsara_home_dir() / "components"


def default_app_root() -> Path:
    """The folder Samsara.exe runs from (frozen) or the checkout (source)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_manifest_url(version: str, repo: str = DEFAULT_REPO) -> str:
    tag = version if version.startswith("v") else f"v{version}"
    return f"https://github.com/{repo}/releases/download/{tag}/manifest.json"


def format_size(size_bytes) -> str:
    if size_bytes is None:
        return "size unknown"
    value = float(size_bytes)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _marker(component: dict, downloads_dir: Path) -> Path:
    return Path(downloads_dir) / (component["id"] + INSTALLED_MARKER_SUFFIX)


def component_installed(component: dict, app_root, downloads_dir) -> bool:
    """True when the component's contents are in the app folder.

    The core is the running application. Otherwise the marker this module
    writes after a verified unpack must carry the manifest's hash; as a
    belt and braces, the two known component layouts are probed directly
    (a machine set up from the full ZIP or by hand has no marker)."""
    cid = component.get("id")
    if cid == "core":
        return True
    app_root = Path(app_root)
    try:
        if _marker(component, downloads_dir).read_text(encoding="ascii").split()[0] == component.get("sha256"):
            return True
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    if cid == "wake-word-models":
        from samsara.runtime_manifest import OWW_MODEL_FILENAMES  # noqa: PLC0415
        names = OWW_MODEL_FILENAMES
        target = app_root / component.get("install_dir", ".")
        return bool(names) and all((target / n).is_file() for n in names)
    if cid == "cuda-pack":
        # Path-local on purpose: the ten runtime DLLs in THIS app folder's
        # install_dir, not whatever the running process can see.
        try:
            from samsara.cuda_detect import _REQUIRED_CUDA_DLLS as names  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            names = ("cudnn_adv64_9.dll", "cudnn_cnn64_9.dll", "cudnn_engines_precompiled64_9.dll",
                     "cudnn_engines_runtime_compiled64_9.dll", "cudnn_graph64_9.dll",
                     "cudnn_heuristic64_9.dll", "cudnn_ops64_9.dll", "cublas64_12.dll",
                     "cublasLt64_12.dll", "cudart64_12.dll")
        target = app_root / component.get("install_dir", ".")
        return all((target / n).is_file() for n in names)
    return False


def missing_components(manifest: dict, app_root, downloads_dir) -> list:
    """Components not installed, in manifest order: available ones first,
    then declared-but-unavailable ones (shown as coming soon)."""
    rows = [c for c in manifest["components"]
            if c.get("id") != "core" and not component_installed(c, app_root, downloads_dir)]
    return sorted(rows, key=lambda c: (not c.get("available"), c["id"]))


# ---------------------------------------------------------------------------
# GPU detection (the wizard side; the installer has its own in [Code])
# ---------------------------------------------------------------------------

def nvidia_gpu_present() -> Optional[bool]:
    """True/False, or None when detection itself failed (never silently off)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes  # noqa: PLC0415
        ctypes.WinDLL("nvcuda.dll")
        return True
    except OSError:
        pass
    except Exception:  # noqa: BLE001
        return None
    try:
        import subprocess  # noqa: PLC0415
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name) -join ';'"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if out.returncode != 0:
            return None
        return "nvidia" in out.stdout.lower()
    except Exception:  # noqa: BLE001
        return None


def component_note(component: dict, gpu_present: Optional[bool]) -> Optional[str]:
    """Why a component is not selectable right now, or None."""
    if not component.get("available"):
        return COMING_SOON
    if "nvidia_gpu" in component.get("requires", []):
        if gpu_present is False:
            return NO_NVIDIA
        if gpu_present is None:
            return "Could not detect a GPU; choose it if you have an NVIDIA card"
    return None


# ---------------------------------------------------------------------------
# Fetch + install
# ---------------------------------------------------------------------------

def local_dir_opener(components_dir):
    """An opener that serves component files from a local folder (the
    --components-dir route for machines without network), honouring Range
    the way the HTTP one does so resume and verification are identical."""
    components_dir = Path(components_dir)

    class _Resp:
        def __init__(self, path: Path, start: int):
            self._fh = open(path, "rb")
            size = path.stat().st_size
            self._fh.seek(start)
            self.status = 206 if start else 200
            self.headers = {"Content-Length": str(max(0, size - start))}

        def read(self, n: int) -> bytes:
            return self._fh.read(n)

        def close(self):
            self._fh.close()

    def _open(url: str, headers: dict):
        name = Path(urlparse(url).path).name
        path = components_dir / name
        if not path.is_file():
            raise components.DownloadError(f"{name} is not in {components_dir}")
        rng = headers.get("Range", "")
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        return _Resp(path, start)

    return _open


def capped_opener(opener, cap: int = WIZARD_CHUNK):
    """Wrap an opener so every response.read(n) returns at most `cap`
    bytes. components.download_component reports progress (and so polls
    cancel) after each read, so this bounds cancel latency without changing
    the fetcher's own chunk size or fetch_and_install's signature."""
    base = opener or components._default_open

    class _Capped:
        def __init__(self, resp):
            self._resp = resp
            self.status = getattr(resp, "status", 200)
            self.headers = getattr(resp, "headers", {})

        def read(self, n: int) -> bytes:
            return self._resp.read(min(int(n), cap))

        def close(self):
            self._resp.close()

    def _open(url: str, headers: dict):
        return _Capped(base(url, headers))

    return _open


def _safe_members(zf: zipfile.ZipFile) -> list:
    members = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.endswith("/"):
            continue
        parts = Path(name).parts
        if not parts or name.startswith("/") or ".." in parts or Path(name).is_absolute() or ":" in parts[0]:
            raise components.DownloadError(f"archive member refused: {info.filename!r}")
        members.append((info, Path(*parts)))
    return members


def install_component(archive, component: dict, app_root, downloads_dir, *,
                      cancel: Optional[Callable[[], bool]] = None) -> list:
    """Unpack a verified archive into app_root/install_dir. Members are
    extracted to a temp folder first and moved into place one file at a
    time with os.replace, then the installed marker is written. Returns the
    installed relative paths. `cancel()` is polled every megabyte during
    extraction; a True answer raises FetchCancelled before any file has
    been moved into place (the temp folder is removed)."""
    target = Path(app_root) / component.get("install_dir", ".")
    target.mkdir(parents=True, exist_ok=True)
    installed = []
    with zipfile.ZipFile(archive) as zf:
        members = _safe_members(zf)
        with tempfile.TemporaryDirectory(prefix=".samsara-install-", dir=str(target)) as tmp:
            tmp_path = Path(tmp)
            for info, rel in members:
                out = tmp_path / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    while True:
                        if cancel is not None and cancel():
                            raise FetchCancelled(component["id"])
                        piece = src.read(1 << 20)
                        if not piece:
                            break
                        dst.write(piece)
            if cancel is not None and cancel():
                raise FetchCancelled(component["id"])
            for _info, rel in members:
                final = target / rel
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path / rel, final)
                installed.append(rel.as_posix())
    Path(downloads_dir).mkdir(parents=True, exist_ok=True)
    _marker(component, downloads_dir).write_text(
        f"{component['sha256']}  {component['filename']}\n", encoding="ascii")
    return installed


def fetch_and_install(component: dict, app_root, downloads_dir, *,
                      progress: Optional[Callable[[int, int], None]] = None,
                      cancel: Optional[Callable[[], bool]] = None,
                      opener=None, chunk_size: int = components.DEFAULT_CHUNK) -> Path:
    """Download (verified, resumable) then unpack one component. `cancel()`
    is polled after every chunk of the download and every megabyte of the
    unpack; a True answer raises FetchCancelled with the part file kept for
    resume and nothing installed."""
    def _progress(done, total):
        if cancel is not None and cancel():
            raise FetchCancelled(component["id"])
        if progress is not None:
            progress(done, total)

    archive = components.download_component(component, downloads_dir, progress=_progress,
                                            opener=opener, chunk_size=chunk_size)
    if cancel is not None and cancel():
        raise FetchCancelled(component["id"])
    install_component(archive, component, app_root, downloads_dir, cancel=cancel)
    return archive


# ---------------------------------------------------------------------------
# Headless mode
# ---------------------------------------------------------------------------

def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="Samsara.exe --fetch-components", add_help=True,
                                     description="Fetch and install optional components (docs/INSTALL.md).")
    parser.add_argument("--fetch-components", required=True, metavar="IDS",
                        help="comma-separated component ids from the manifest")
    parser.add_argument("--manifest", required=True, help="manifest.json URL (https) or local path")
    parser.add_argument("--components-dir", default=None,
                        help="serve the component archives from this folder instead of the network")
    parser.add_argument("--app-root", default=None, help="the application folder (default: where Samsara.exe is)")
    parser.add_argument("--downloads-dir", default=None, help="where archives are kept (default: ~/.samsara/components)")
    parser.add_argument("--progress-window", action="store_true",
                        help="show a small progress window with a Cancel button")
    return parser.parse_args(argv)


def fetch_components_main(argv=None, *, fetch=None, opener=None, cancel=None,
                          out=None, progress_ui=None) -> int:
    """Entry point for the headless mode. Returns an exit code (EXIT_*).
    `fetch`/`opener` inject the transports; `cancel()` polls for a cancel;
    `progress_ui` is a factory for the optional progress window."""
    out = out or sys.stdout
    try:
        args = _parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:
        return EXIT_BAD_ARGS if exc.code else EXIT_OK
    ids = [i.strip() for i in args.fetch_components.split(",") if i.strip()]
    if not ids:
        print("[FAIL] no component ids given", file=out)
        return EXIT_BAD_ARGS

    try:
        manifest = components.load_manifest(args.manifest, fetch=fetch)
    except components.ManifestError as exc:
        print(f"[FAIL] manifest: {exc}", file=out)
        return EXIT_BAD_ARGS
    except Exception as exc:  # noqa: BLE001 - network, DNS, proxy
        print(f"[FAIL] could not reach the manifest: {exc}. The core install is complete; "
              "get components later from Settings.", file=out)
        return EXIT_NETWORK

    by_id = {c["id"]: c for c in manifest["components"]}
    unknown = [i for i in ids if i not in by_id]
    if unknown:
        print(f"[FAIL] unknown component id(s): {', '.join(unknown)}; "
              f"known: {', '.join(sorted(by_id))}", file=out)
        return EXIT_BAD_ARGS
    unavailable = [i for i in ids if not by_id[i].get("available")]
    if unavailable:
        print(f"[FAIL] not available yet: {', '.join(unavailable)}", file=out)
        return EXIT_UNAVAILABLE

    app_root = Path(args.app_root) if args.app_root else default_app_root()
    downloads_dir = Path(args.downloads_dir) if args.downloads_dir else default_downloads_dir()
    if opener is None and args.components_dir:
        opener = local_dir_opener(args.components_dir)

    ui = progress_ui(len(ids)) if (args.progress_window and progress_ui is not None) else None
    if ui is None and args.progress_window:
        ui = _make_progress_window(len(ids))

    def _cancelled() -> bool:
        if cancel is not None and cancel():
            return True
        return bool(ui is not None and ui.cancelled())

    rc = EXIT_OK
    try:
        for index, cid in enumerate(ids):
            component = by_id[cid]
            if cid == "core" or component_installed(component, app_root, downloads_dir):
                print(f"[SKIP] {cid}: already installed", file=out)
                continue
            if ui is not None:
                ui.begin(index, component["name"], component["size_bytes"])

            def _progress(done, total, _cid=cid):
                print(f"PROGRESS {_cid} {done} {total}", file=out)
                if ui is not None:
                    ui.update(done, total)

            try:
                fetch_and_install(component, app_root, downloads_dir,
                                  progress=_progress, cancel=_cancelled, opener=opener)
            except FetchCancelled:
                print(f"[CANCELLED] {cid}: nothing installed; the core install is intact "
                      "(a partial download is kept for resume)", file=out)
                return EXIT_CANCELLED
            except components.HashMismatch as exc:
                print(f"[FAIL] {cid}: {exc}", file=out)
                rc = EXIT_FAILED
                continue
            except components.DownloadError as exc:
                print(f"[FAIL] {cid}: {exc}. The core install is complete; "
                      "get components later from Settings.", file=out)
                return EXIT_NETWORK
            print(f"[PASS] {cid}: installed", file=out)
    finally:
        if ui is not None:
            ui.close()
    return rc


def _make_progress_window(count: int):
    """A minimal Qt progress window with a Cancel button, created only when
    --progress-window is passed. Returns None when Qt cannot start."""
    try:
        from PySide6.QtWidgets import QApplication, QProgressDialog  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    app = QApplication.instance() or QApplication([])

    class _UI:
        def __init__(self):
            self.dialog = QProgressDialog("Preparing...", "Cancel", 0, 1000)
            self.dialog.setWindowTitle("Samsara components")
            self.dialog.setMinimumDuration(0)
            self.dialog.setAutoClose(False)
            self.dialog.setAutoReset(False)
            self.dialog.setMinimumWidth(420)
            self._name = ""
            self._index = 0
            self.dialog.show()
            app.processEvents()

        def begin(self, index, name, size):
            self._index, self._name = index, name
            self.dialog.setLabelText(f"Downloading {name} ({format_size(size)}), {index + 1} of {count}")
            self.dialog.setValue(0)
            app.processEvents()

        def update(self, done, total):
            self.dialog.setValue(int(1000 * done / total) if total else 0)
            app.processEvents()

        def cancelled(self) -> bool:
            app.processEvents()
            return self.dialog.wasCanceled()

        def close(self):
            self.dialog.close()
            app.processEvents()

    return _UI()


# ---------------------------------------------------------------------------
# Wizard page
# ---------------------------------------------------------------------------

def _load_manifest_default() -> dict:
    """The wizard's default manifest source: a manifest cached beside the
    downloads (written by the installer's fetch), else the release URL."""
    cached = default_downloads_dir() / "manifest.json"
    if cached.is_file():
        return components.load_manifest(cached)
    from samsara import __version__  # noqa: PLC0415

    return components.load_manifest(default_manifest_url(__version__))


class ComponentsPage:
    """The wizard's optional-components page, built on a QWidget the caller
    owns. Rows: name, size, what it enables, a "Get <name>" button (or a
    visible reason when it cannot be fetched), a progress bar and a
    "Cancel <name>" button while fetching. One "Later" button advances.

    Accessibility: every button is at least 44 px tall with its visible
    text as its accessible name; rows are in tab order top to bottom.
    """

    def __init__(self, parent_widget, *, on_later: Callable[[], None],
                 manifest_loader: Callable[[], dict] = _load_manifest_default,
                 app_root=None, downloads_dir=None, opener=None,
                 gpu_probe: Callable[[], Optional[bool]] = nvidia_gpu_present,
                 run_in_thread: bool = True):
        from PySide6.QtCore import Qt  # noqa: PLC0415
        from PySide6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget  # noqa: PLC0415

        from samsara.ui import theme  # noqa: PLC0415

        self._parent = parent_widget
        self._on_later = on_later
        self._loader = manifest_loader
        self._app_root = Path(app_root) if app_root else default_app_root()
        self._downloads = Path(downloads_dir) if downloads_dir else default_downloads_dir()
        self._opener = opener
        self._gpu_probe = gpu_probe
        self._threaded = run_in_thread
        self._manifest: Optional[dict] = None
        self._rows: dict = {}
        self._cancel_flags: dict = {}
        self._workers: dict = {}
        self._close_hooked = None
        self._error: Optional[str] = None
        self._manifest_generation = 0

        self.widget = QWidget(parent_widget)
        lay = QVBoxLayout(self.widget)
        lay.setContentsMargins(28, 16, 28, 16)
        lay.setSpacing(12)
        self._intro = QLabel("Optional parts you can add now or any time later from Settings.")
        self._intro.setWordWrap(True)
        self._intro.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_BODY}px;")
        lay.addWidget(self._intro)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setAccessibleName("Components status")
        self._status.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
        lay.addWidget(self._status)
        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(12)
        lay.addWidget(self._rows_host)
        lay.addStretch(1)
        self._later_btn = QPushButton("Later")
        self._later_btn.setAccessibleName("Later")
        self._later_btn.setMinimumHeight(MIN_TARGET)
        self._later_btn.setMinimumWidth(MIN_TARGET)
        theme.make_secondary(self._later_btn)
        self._later_btn.clicked.connect(lambda: self._on_later())
        lay.addWidget(self._later_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        self._Qt = Qt

    # ---- data ------------------------------------------------------------

    def refresh(self) -> list:
        """Reload the manifest and rebuild the rows. Returns the listed
        components (missing, in order). Never raises: a manifest that cannot
        be loaded becomes a visible status line and an empty list."""
        self._hook_window_close()
        self._manifest_generation += 1
        try:
            self._manifest = self._loader()
            self._error = None
        except Exception as exc:  # noqa: BLE001
            self._manifest = None
            self._error = f"The component list is unavailable right now ({exc}). You can add components later from Settings."
        self._rebuild()
        return list(self._listed)

    def refresh_async(self) -> list:
        """Load the manifest without blocking the Qt event thread."""
        self._hook_window_close()
        if not self._threaded:
            return self.refresh()
        self._manifest_generation += 1
        generation = self._manifest_generation
        self._status.setText("Loading component list…")

        def _load():
            try:
                manifest = self._loader()
            except Exception as exc:  # noqa: BLE001
                self._post(lambda: self._finish_manifest_refresh(generation, None, exc))
            else:
                self._post(lambda: self._finish_manifest_refresh(generation, manifest, None))

        thread_registry.spawn("components.manifest", _load, daemon=True)
        return list(getattr(self, "_listed", []))

    def _finish_manifest_refresh(self, generation: int, manifest, error):
        if generation != self._manifest_generation:
            return
        if error is None:
            self._manifest, self._error = manifest, None
        else:
            self._manifest = None
            self._error = (
                f"The component list is unavailable right now ({error}). "
                "You can add components later from Settings."
            )
        self._rebuild()

    @property
    def listed(self) -> list:
        return list(getattr(self, "_listed", []))

    def _rebuild(self):
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget  # noqa: PLC0415

        from samsara.ui import theme  # noqa: PLC0415

        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._rows = {}
        self._listed = []
        if self._manifest is None:
            self._status.setText(self._error or "")
            return
        gpu = None
        self._listed = missing_components(self._manifest, self._app_root, self._downloads)
        if any("nvidia_gpu" in c.get("requires", []) for c in self._listed):
            gpu = self._gpu_probe()
        if not self._listed:
            self._status.setText("Everything is installed.")
            return
        self._status.setText("")
        for component in self._listed:
            card = QWidget()
            theme.style_card(card)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(16, 12, 16, 12)
            cl.setSpacing(6)
            title = QLabel(f"{component['name']}  ({format_size(component.get('size_bytes'))})")
            title.setStyleSheet(f"color:{theme.TEXT_PRIMARY};font-size:{theme.TYPE_BODY}px;font-weight:600;")
            title.setAccessibleName(component["name"])
            cl.addWidget(title)
            benefit = QLabel(COMPONENT_BENEFITS.get(component["id"], component.get("description", "")))
            benefit.setWordWrap(True)
            benefit.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            cl.addWidget(benefit)
            note = component_note(component, gpu)
            row = QHBoxLayout()
            row.setSpacing(12)
            get_btn = QPushButton(f"Get {component['name']}")
            get_btn.setAccessibleName(get_btn.text())
            get_btn.setMinimumHeight(MIN_TARGET)
            get_btn.setMinimumWidth(MIN_TARGET)
            theme.make_primary(get_btn)
            get_btn.clicked.connect(lambda _=False, c=component: self.start_fetch(c["id"]))
            row.addWidget(get_btn)
            cancel_btn = QPushButton(f"Cancel {component['name']}")
            cancel_btn.setAccessibleName(cancel_btn.text())
            cancel_btn.setMinimumHeight(MIN_TARGET)
            cancel_btn.setMinimumWidth(MIN_TARGET)
            theme.make_secondary(cancel_btn)
            cancel_btn.setVisible(False)
            cancel_btn.clicked.connect(lambda _=False, c=component: self.cancel_fetch(c["id"]))
            row.addWidget(cancel_btn)
            note_lbl = QLabel(note or "")
            note_lbl.setAccessibleName(f"{component['name']} note")
            note_lbl.setStyleSheet(f"color:{theme.WARNING};font-size:{theme.TYPE_MIN}px;")
            note_lbl.setVisible(bool(note))
            row.addWidget(note_lbl)
            row.addStretch(1)
            cl.addLayout(row)
            bar = QProgressBar()
            bar.setRange(0, 1000)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setAccessibleName(f"{component['name']} progress")
            bar.setVisible(False)
            cl.addWidget(bar)
            result = QLabel("")
            result.setWordWrap(True)
            result.setAccessibleName(f"{component['name']} result")
            result.setStyleSheet(f"color:{theme.TEXT_SECONDARY};font-size:{theme.TYPE_MIN}px;")
            cl.addWidget(result)
            if note == COMING_SOON or note == NO_NVIDIA:
                get_btn.setEnabled(False)
            self._rows_layout.addWidget(card)
            self._rows[component["id"]] = {
                "component": component, "get": get_btn, "cancel": cancel_btn,
                "bar": bar, "result": result, "note": note_lbl,
            }
        # Tab order: top to bottom, Get then Cancel per row, Later last.
        prev = None
        for cid in [c["id"] for c in self._listed]:
            for key in ("get", "cancel"):
                w = self._rows[cid][key]
                if prev is not None:
                    QWidget.setTabOrder(prev, w)
                prev = w
        if prev is not None:
            QWidget.setTabOrder(prev, self._later_btn)

    # ---- actions ---------------------------------------------------------

    def start_fetch(self, component_id: str):
        row = self._rows.get(component_id)
        if row is None:
            return
        component = row["component"]
        flag = threading.Event()
        self._cancel_flags[component_id] = flag
        row["get"].setEnabled(False)
        row["cancel"].setVisible(True)
        row["bar"].setVisible(True)
        row["result"].setText("")

        def _progress(done, total):
            self._post(lambda: row["bar"].setValue(int(1000 * done / total) if total else 0))

        def _work():
            try:
                fetch_and_install(component, self._app_root, self._downloads,
                                  progress=_progress, cancel=flag.is_set,
                                  opener=capped_opener(self._opener))
                self._post(lambda: self._finish(component_id, "Installed."))
            except FetchCancelled:
                self._post(lambda: self._finish(component_id, "Cancelled. Nothing was changed.", retry=True))
            except Exception as exc:  # noqa: BLE001
                self._post(lambda: self._finish(component_id, f"Could not install: {exc}", retry=True))

        if self._threaded:
            # Registered with the thread registry (37) so it shows in
            # snapshot()/dump(). Daemon on purpose: registry.shutdown() joins
            # only non-daemon threads, and the app's exit path joins the
            # registry BEFORE anything closes this window, so a non-daemon
            # download in flight would hold the quit for the registry's whole
            # deadline. The page owns the lifetime instead: cancel_all() on
            # window close (or aboutToQuit) cancels every worker and joins it
            # for at most CLOSE_JOIN_S -- the cancel is polled every
            # WIZARD_CHUNK of download (capped_opener) and every megabyte of
            # unpack, so a
            # worker exits within about a second and never outlives the
            # wizard. A download cut at process exit leaves a resumable
            # .part file and nothing under a final name.
            self._workers[component_id] = thread_registry.spawn(
                f"components.fetch.{component_id}", _work, daemon=True)
        else:
            _work()

    def cancel_fetch(self, component_id: str):
        flag = self._cancel_flags.get(component_id)
        if flag is not None:
            flag.set()

    def cancel_all(self, join_timeout: float = CLOSE_JOIN_S) -> list:
        """Cancel every in-flight fetch and wait for the workers, at most
        join_timeout seconds in total. Returns the names of workers still
        alive after that (normally empty)."""
        for flag in self._cancel_flags.values():
            flag.set()
        import time  # noqa: PLC0415
        deadline = time.monotonic() + max(0.0, join_timeout)
        for worker in list(self._workers.values()):
            worker.join(max(0.0, deadline - time.monotonic()))
        alive = [w.name for w in self._workers.values() if w.is_alive()]
        if alive:
            logger.warning("[COMPONENTS] worker(s) still running %.1fs after cancel: %s",
                           join_timeout, ", ".join(alive))
        return alive

    def _hook_window_close(self):
        """Cancel on close: when the hosting top-level window closes, or the
        application is about to quit, every fetch is cancelled and joined
        (bounded). Installed once, lazily, because the page is parented into
        the wizard's stack after construction."""
        try:
            from PySide6.QtCore import QEvent, QObject  # noqa: PLC0415
            from PySide6.QtWidgets import QApplication  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return
        window = self.widget.window()
        if window is None or window is self._close_hooked:
            return
        page = self

        class _CloseFilter(QObject):
            def eventFilter(self, obj, event):
                if event.type() == QEvent.Type.Close:
                    page._manifest_generation += 1
                    page.cancel_all()
                return False

        self._close_filter = _CloseFilter(window)
        window.installEventFilter(self._close_filter)
        self._close_hooked = window
        app = QApplication.instance()
        if app is not None and not getattr(self, "_quit_hooked", False):
            app.aboutToQuit.connect(lambda: page.cancel_all())
            self._quit_hooked = True

    def _finish(self, component_id: str, message: str, retry: bool = False):
        row = self._rows.get(component_id)
        if row is None:
            return
        row["cancel"].setVisible(False)
        row["bar"].setVisible(retry)
        row["result"].setText(message)
        row["get"].setEnabled(retry)
        if not retry:
            row["get"].setText(f"Installed {row['component']['name']}")
            row["get"].setAccessibleName(row["get"].text())

    def _post(self, fn):
        """Run fn on the Qt thread (the fetch runs on a worker)."""
        if not self._threaded:
            fn()
            return
        try:
            from samsara.ui import qt_runtime  # noqa: PLC0415
            qt_runtime.post(fn)
        except Exception:  # noqa: BLE001
            from PySide6.QtCore import QTimer  # noqa: PLC0415
            QTimer.singleShot(0, fn)


__all__ = [
    "COMPONENT_BENEFITS", "COMING_SOON", "NO_NVIDIA", "MIN_TARGET",
    "EXIT_OK", "EXIT_BAD_ARGS", "EXIT_UNAVAILABLE", "EXIT_NETWORK", "EXIT_CANCELLED", "EXIT_FAILED",
    "FetchCancelled", "ComponentsPage", "component_installed", "component_note", "missing_components",
    "fetch_and_install", "install_component", "fetch_components_main", "format_size", "capped_opener",
    "local_dir_opener", "nvidia_gpu_present", "default_manifest_url", "default_downloads_dir", "default_app_root",
]
