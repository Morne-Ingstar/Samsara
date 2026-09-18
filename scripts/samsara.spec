# -*- mode: python ; coding: utf-8 -*-
"""
Samsara PyInstaller Spec File
Creates a standalone directory-based distribution
"""

import os
import sys
import zipfile
from importlib import metadata
from pathlib import Path
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
)
from tools.release_manifest import tracked_tree_datas
from samsara.runtime_manifest import OWW_MODEL_FILENAMES

block_cipher = None

# Get site-packages path (check both system and user locations)
import site
site_packages_list = site.getsitepackages()
user_site = site.getusersitepackages()

# Find ctranslate2 to determine which site-packages is active
def find_package_dir(pkg_name):
    """Find a package in system or user site-packages."""
    for sp in site_packages_list:
        if os.path.exists(os.path.join(sp, pkg_name)):
            return sp
    if os.path.exists(os.path.join(user_site, pkg_name)):
        return user_site
    return site_packages_list[-1]

site_packages = find_package_dir('ctranslate2')

# App directory (parent of scripts folder)
app_dir = Path(SPECPATH).parent

# This hook runs only when build_and_smoke.cmd explicitly asks the frozen exe
# to prove its import inventory.  It is generated under PyInstaller's build
# directory rather than becoming a second application entry point or a shipped
# source file.  ``os._exit`` prevents dictation.py from starting in this mode.
_runtime_import_hook = app_dir / 'build' / 'samsara_runtime_import_check.py'
_runtime_import_hook.parent.mkdir(parents=True, exist_ok=True)
_runtime_import_hook.write_text(r'''
import importlib
import os
import pkgutil
import traceback

def _pass():
    _finish("PASS\n", 0)


def _fail():
    # This hook runs in a windowed executable.  Never let a check exception
    # reach PyInstaller's bootloader: that creates an owner-facing modal
    # dialog and leaves automated smoke blocked behind it.
    traceback.print_exc()
    _finish("FAIL\n", 1)


def _finish(result, exit_code):
    marker = os.environ.get("SAMSARA_FROZEN_IMPORT_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(result)
    os._exit(exit_code)


if os.environ.get("SAMSARA_FROZEN_IMPORT_CHECK") == "1":
    try:
        names = ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtSvg"]
        for package_name in ("samsara", "plugins.commands"):
            package = importlib.import_module(package_name)
            names.extend(item.name for item in pkgutil.walk_packages(package.__path__, package.__name__ + "."))
        for name in names:
            importlib.import_module(name)
    except BaseException:
        _fail()
    _pass()

if os.environ.get("SAMSARA_GESTURE_COMPONENT_CHECK") == "missing":
    try:
        from samsara.components import ComponentNotInstalled
        from samsara.vision.camera_service import CameraService
        CameraService().start()
    except ComponentNotInstalled as exc:
        print(f"[GESTURE-CHECK] {exc}")
        _pass()
    except BaseException:
        _fail()
    try:
        raise RuntimeError("gesture component unexpectedly present in core build")
    except BaseException:
        _fail()

if os.environ.get("SAMSARA_GESTURE_COMPONENT_CHECK") == "installed":
    try:
        import cv2  # noqa: F401
        import mediapipe  # noqa: F401
        from samsara.vision.gesture_loop import GestureLoop

        class _Reader:
            def get(self, timeout=0.1):
                return None

        class _Camera:
            def subscribe(self):
                return _Reader()
            def unsubscribe(self, _reader):
                return None

        loop = GestureLoop(object(), _Camera(), {})
        loop.start()
        loop.stop()
        print("[GESTURE-CHECK] installed component imports and gesture loop starts")
    except BaseException:
        _fail()
    _pass()
''', encoding='utf-8')

# ============================================================================
# DATA FILES
# ============================================================================
datas = []

# 1. ctranslate2 - models and specs (DLLs handled via binaries)
ctranslate2_path = os.path.join(site_packages, 'ctranslate2')
if os.path.exists(ctranslate2_path):
    for subdir in ['converters', 'models', 'specs']:
        src = os.path.join(ctranslate2_path, subdir)
        if os.path.exists(src):
            datas.append((src, f'ctranslate2/{subdir}'))

# 2. faster_whisper assets (VAD model)
faster_whisper_assets = os.path.join(site_packages, 'faster_whisper', 'assets')
if os.path.exists(faster_whisper_assets):
    datas.append((faster_whisper_assets, 'faster_whisper/assets'))

# 2b. OpenWakeWord — keep runtime code imports and binaries from collect_all, then
# explicitly pin OWW ONNX artifacts needed for bundled inference.
oww_model_filenames = list(OWW_MODEL_FILENAMES)
oww_datas, oww_binaries, oww_hiddenimports = collect_all('openwakeword')
datas += oww_datas
oww_model_datas = collect_data_files('openwakeword', subdir='resources/models', includes=oww_model_filenames)
datas += oww_model_datas
# The openwakeword wheel ships no model files: they arrive via
# openwakeword.utils.download_models() the first time the app runs, so a fresh
# CI runner has none and collect_data_files() above returns [] without a word
# (v0.23.0-beta.1's CI ZIP shipped 0 of 9). Never build without them --
# tools/release_preflight.py --fetch-oww-models downloads the pinned,
# SHA-256-verified set into the interpreter's openwakeword package.
_oww_collected = {os.path.basename(src) for src, _dest in oww_model_datas}
_oww_missing = [name for name in oww_model_filenames if name not in _oww_collected]
if _oww_missing:
    raise SystemExit(
        "samsara.spec: bundled OpenWakeWord model file(s) not found in the openwakeword "
        f"package: {', '.join(_oww_missing)} -- run `python tools/release_preflight.py "
        "--fetch-oww-models` before PyInstaller"
    )

# 2c. PySide6.  The app imports only QtCore, QtGui, QtWidgets and QtSvg.
# PyInstaller's Qt hooks follow their DLL dependencies and provide qwindows,
# qico/qsvg image handlers and the Windows style; collect_all('PySide6') used
# to also ship WebEngine, QML/Quick, Multimedia, translations and developer
# tooling that no executable path imports.
pyside6_datas, pyside6_binaries = [], []
pyside6_hiddenimports = [
    'PySide6', 'PySide6.QtCore', 'PySide6.QtGui', 'PySide6.QtWidgets', 'PySide6.QtSvg',
]
shiboken6_datas, shiboken6_binaries, shiboken6_hiddenimports = collect_all('shiboken6')
datas += pyside6_datas + shiboken6_datas

# 2d. Gesture control is a downloadable component, not part of the core
# onedir build. _write_gesture_component() below puts its files under
# _internal, the frozen interpreter's existing sys._MEIPASS import root.

# 3. sounddevice PortAudio binaries
sounddevice_data = os.path.join(site_packages, '_sounddevice_data')
if os.path.exists(sounddevice_data):
    datas.append((sounddevice_data, '_sounddevice_data'))

# 5. App-specific data files. Use Git's tracked-file manifest when building
# from a checkout so ignored/untracked workstation assets and __pycache__
# files cannot make a local build differ from the clean CI artifact.
datas += tracked_tree_datas(app_dir, 'sounds', 'sounds')
datas += tracked_tree_datas(app_dir, 'profiles', 'profiles')
datas.append((str(app_dir / 'commands.json'), '.'))
# `command_catalog.ROOT` is the frozen runtime data root (`_MEIPASS`), as it
# is for commands.json.  The execution-policy risk fallback needs this exact
# checked-in catalog; release smoke verifies the staged copy is parseable.
datas.append((str(app_dir / 'commands_catalog.json'), '.'))
# Plugins are runtime-loaded Python/data files, so preserve their relative
# paths while applying the same tracked-file release manifest.
datas += tracked_tree_datas(app_dir, 'plugins', 'plugins')
# Brave/Chromium DOM Show Numbers extension -- bundled wholesale (same
# tracked-file rule as plugins/ above) so a frozen build still has a folder to point
# Brave's "Load unpacked" at; see browser_extension/README.md.
datas += tracked_tree_datas(app_dir, 'browser_extension', 'browser_extension')
# plugins/commands/stremio.py imports this runtime helper dynamically after
# adding the bundled tools directory to sys.path. Keep this an explicit
# whitelist: tools/ also contains diagnostics, build helpers, and local
# artifacts that must never be shipped merely because they use a .py suffix.
datas.append((str(app_dir / 'tools' / 'stremio_control.py'), 'tools'))
# Runtime icon files only -- not the whole assets/icon/ tree (generated PNGs).
# samsara.ico: setWindowIcon and the exe icon. samsara.svg: the single-source
# mark that samsara/ui/tray_qt.render_mark draws for the tray, the listening
# indicator and the splash (tray_qt.mark_svg_path resolves it under _MEIPASS).
datas.append((str(app_dir / 'assets' / 'icon' / 'samsara.ico'), 'assets/icon'))
datas.append((str(app_dir / 'assets' / 'icon' / 'samsara.svg'), 'assets/icon'))
# NOTE: config.json is intentionally NOT bundled — it contains dev-machine
# paths and credentials. A fresh config is generated on first run.

# ============================================================================
# BINARIES (DLLs)
# ============================================================================
binaries = []

# OpenWakeWord binaries (collected earlier)
binaries += oww_binaries

# PySide6 / shiboken6 binaries (collected earlier).
binaries += pyside6_binaries + shiboken6_binaries

# ctranslate2 DLLs
for dll in ['ctranslate2.dll', 'cudnn64_9.dll', 'libiomp5md.dll']:
    dll_path = os.path.join(ctranslate2_path, dll)
    if os.path.exists(dll_path):
        binaries.append((dll_path, 'ctranslate2'))

# ctranslate2 pyd file
pyd_files = [f for f in os.listdir(ctranslate2_path) if f.endswith('.pyd')]
for pyd in pyd_files:
    binaries.append((os.path.join(ctranslate2_path, pyd), 'ctranslate2'))

# cuDNN DLLs from torch (required by ctranslate2 for CUDA inference)
# Set INCLUDE_CUDA=1 environment variable to bundle CUDA libraries
# Otherwise builds CPU-only version (users can add CUDA pack separately)
INCLUDE_CUDA = os.environ.get('INCLUDE_CUDA', '0') == '1'

if INCLUDE_CUDA:
    torch_lib_path = os.path.join(site_packages, 'torch', 'lib')
    if os.path.exists(torch_lib_path):
        cudnn_dlls = [
            'cudnn_adv64_9.dll',
            'cudnn_cnn64_9.dll', 
            'cudnn_engines_precompiled64_9.dll',
            'cudnn_engines_runtime_compiled64_9.dll',
            'cudnn_graph64_9.dll',
            'cudnn_heuristic64_9.dll',
            'cudnn_ops64_9.dll',
            'cublas64_12.dll',
            'cublasLt64_12.dll',
            'cudart64_12.dll',
        ]
        for dll in cudnn_dlls:
            dll_path = os.path.join(torch_lib_path, dll)
            if os.path.exists(dll_path):
                # Put in ctranslate2 folder so the stub can find them
                binaries.append((dll_path, 'ctranslate2'))
                print(f"[SPEC-CUDA] bundling {dll}")
            else:
                print(f"[SPEC-CUDA] MISSING {dll} at {dll_path}")
    else:
        print(f"[SPEC-CUDA] torch_lib_path does not exist: {torch_lib_path}")
else:
    print("[SPEC-CUDA] INCLUDE_CUDA not set — CPU-only build")

# PortAudio DLLs
portaudio_path = os.path.join(sounddevice_data, 'portaudio-binaries')
if os.path.exists(portaudio_path):
    for f in os.listdir(portaudio_path):
        if f.endswith('.dll'):
            binaries.append((os.path.join(portaudio_path, f), '.'))

# ============================================================================
# HIDDEN IMPORTS
# ============================================================================
hiddenimports = [
    'shiboken6',

    # Cloud-fallback TTS voice (2026-07-10 import audit)
    'edge_tts',

    # Rhyme/phonetic lookup (2026-07-10 import audit)
    'pronouncing',

    # WASAPI loopback capture for echo cancellation (2026-07-10 import audit)
    'pyaudiowpatch',

    # Filesystem change notifications (2026-07-10 import audit)
    'watchdog',
    'watchdog.observers',
    'watchdog.events',

    # WebSocket client (2026-07-10 import audit)
    'websockets',

    # Core ML/Audio
    'ctranslate2',
    'faster_whisper',
    'faster_whisper.audio',
    'faster_whisper.feature_extractor', 
    'faster_whisper.tokenizer',
    'faster_whisper.transcribe',
    'faster_whisper.utils',
    'faster_whisper.vad',
    
    # Audio
    'sounddevice',
    '_sounddevice_data',
    
    # Input handling
    'pynput',
    'pynput.keyboard',
    'pynput.keyboard._win32',
    'pynput.mouse',
    'pynput.mouse._win32',
    'keyboard',
    
    # Clipboard/GUI automation
    'pyperclip',
    'pyautogui',
    
    # Image/Tray
    'PIL',
    'PIL.Image',
    'PIL.ImageDraw',
    'PIL.ImageTk',
    'pystray',
    'pystray._win32',
    
    # Windows-specific
    'win32api',
    'win32con', 
    'win32gui',
    'win32clipboard',
    'win10toast_click',
    'winsound',
    'msvcrt',
    
    # ONNX runtime for VAD
    'onnxruntime',
    
    # Huggingface for model downloads
    'huggingface_hub',
    'huggingface_hub.file_download',
    
    # HTTP client
    'requests',
    'urllib3',

    # Process info
    'psutil',

    # Win32 bindings
    'win32process',

    # Samsara core modules
    'samsara',
    'samsara.calibration',
    'samsara.clipboard',
    'samsara.command_parser',
    'samsara.command_registry',
    'samsara.command_stats',
    'samsara.commands',
    'samsara.constants',
    'samsara.transcript_gates',
    'samsara.echo_cancel',
    'samsara.key_macros',
    'samsara.notifications',
    'samsara.alarms',
    'samsara.profiles',
    'samsara.wake_word_matcher',
    'samsara.wake_corrections',
    'samsara.plugin_commands',
    'samsara.phonetic_wash',
    'samsara.history',
    'samsara.cleanup',
    'samsara.languages',
    'samsara.tasks_store',
    'samsara.cloud_llm',
    'samsara.ava_corrections',
    'samsara.ava_profile',
    # Only imported from plugins/commands/*.py, which are loaded dynamically
    # at runtime (a directory scan + exec, not a static import) -- invisible
    # to PyInstaller's own dependency analysis, so these must be listed
    # explicitly or they silently go missing from the frozen build.
    'samsara.audio_switch',
    'samsara.browser_bridge',

    # Samsara Smart Actions
    'samsara.smart_actions_bridge',
    'samsara.smart_actions_session',
    'samsara.smart_actions_tools',

    # Samsara CUDA detection
    'samsara.cuda_detect',

    # Privacy-explicit, verified frozen-build updater. The Settings/tray UI
    # reaches this module lazily, so keep it explicit in packaged builds.
    'samsara.updater',
    'samsara.update_customizations',

    # Samsara OWW pre-filter
    'samsara.wake_detector',
]

# Merge imports collected by collect_all('openwakeword')
hiddenimports += oww_hiddenimports

# Merge the narrow Qt inventory plus imports collected for shiboken6.
hiddenimports += pyside6_hiddenimports + shiboken6_hiddenimports

# samsara.ui / samsara.tts (2026-07-10): both packages are only reached via
# qt_runtime.post()-scheduled lazy instantiation, invisible to PyInstaller's
# static analysis -- same blind spot as samsara.audio_switch above, just at
# package scale. Previously hand-listed here, but the real modules are all
# _qt-suffixed (main_window_qt, settings_qt, history_qt, ...) while the list
# named non-suffixed/renamed/removed names (samsara.ui.main_window, a phantom
# samsara.ui.tabs.* subpackage, samsara.tts.edge_engine) that don't exist --
# PyInstaller hard-errors on an unresolvable hiddenimport and never produces
# Samsara.exe. collect_submodules() enumerates the package's ACTUAL contents
# at build time instead of trusting a hand-typed list to stay in sync with
# every future rename.
hiddenimports += collect_submodules('samsara.ui')
hiddenimports += collect_submodules('samsara.tts')
# The frozen import-check runtime hook walks every Samsara and dynamic plugin
# module. Include them all so that check proves the artifact, not the source.
hiddenimports += collect_submodules('samsara')
hiddenimports += collect_submodules('plugins.commands')

# ============================================================================
# ANALYSIS
# ============================================================================
a = Analysis(
    [str(app_dir / 'dictation.py')],
    pathex=[str(app_dir)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(_runtime_import_hook)],
    excludes=[
        # Exclude problematic modules
        'charset_normalizer',
        # Exclude unnecessary large packages
        # Exclude heavy ML frameworks. Runtime VAD is provided by
        # faster_whisper's bundled ONNX model; torch/torchaudio are not needed.
        # INCLUDE_CUDA above may still harvest selected CUDA DLLs from an
        # installed torch package without bundling the Python framework.
        'matplotlib',
        'pandas',
        'IPython',
        'jupyter',
        # Exclude heavy ML frameworks not needed for faster_whisper
        'torch',
		'torch._C',
		'torch.cuda',
		'torch.nn',
	    'torch.utils',
        'torchgen',
		'torchaudio',
        'tensorflow',
        'keras',
        'tensorboard',
        'tf_keras',
        'tensorflow_hub',
        'tensorflow_estimator',
        'transformers',  # Not needed - faster_whisper has its own tokenizer
        'langchain',
        'langchain_core',
        'langchain_community',
        'opentelemetry',
        'bitsandbytes',
        'fairscale',
        'timm',
        'torchvision',  # Not needed for audio
        'xformers',
        'triton',
        'altair',
        'streamlit',
        'gradio',
        # More unused transitive deps
        # Gesture control installs cv2 and mediapipe under _internal. The
        # guarded vision modules let core run until that component is added.
        'cv2',
        'mediapipe',
        'numba',
        'llvmlite',
        'librosa',
        'pandas',
        'h5py',
        'pytest',
        # No production module imports these. customtkinter is a requirements
        # passenger; its PyInstaller hook was the only reason Tcl/Tk shipped.
        'customtkinter',
        'tkinter',
        '_tkinter',
        # mediapipe's broad package collector reaches its model-maker stack,
        # but Samsara's gesture feature uses only mediapipe.solutions.
        'pyarrow',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# Qt Widgets renders through the Windows raster/D3D path; it does not need
# Mesa's software OpenGL fallback. The frozen import inventory and normal
# smoke run without it on the real Windows platform before this is shipped.
a.binaries = [entry for entry in a.binaries
              if os.path.basename(entry[0]).lower() != 'opengl32sw.dll']

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Samsara',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # No console window (GUI app)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(app_dir / 'assets' / 'icon' / 'samsara.ico'),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Samsara',
)


# ---------------------------------------------------------------------------
# Optional gesture-control component
# ---------------------------------------------------------------------------
# A PyInstaller hidden import would put Python modules into PYZ, which cannot
# be added after installation. This archive instead preserves the installed
# wheel files beneath _internal; a frozen onedir executable already imports
# from sys._MEIPASS (_internal), so a restart after unpacking sees them.
_CV2_DISTRIBUTION = next(iter(metadata.packages_distributions().get('cv2', ())), None)
if _CV2_DISTRIBUTION is None:
    raise SystemExit('samsara.spec: cv2 has no installed distribution metadata')
_GESTURE_DISTRIBUTIONS = (
    'mediapipe', _CV2_DISTRIBUTION, 'absl-py', 'protobuf',
    'flatbuffers', 'attrs', 'six', 'packaging',
    # MediaPipe's current solutions API imports drawing_utils, which imports
    # matplotlib.  These are its wheel dependencies that are not core.
    'matplotlib', 'contourpy', 'cycler', 'fonttools', 'kiwisolver',
    'pillow', 'pyparsing', 'python-dateutil',
)


def _write_gesture_component(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix('.zip.tmp')
    seen = set()
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for distribution_name in _GESTURE_DISTRIBUTIONS:
                distribution = metadata.distribution(distribution_name)
                for relative in distribution.files or ():
                    source = Path(distribution.locate_file(relative))
                    if not source.is_file():
                        continue
                    arcname = source.relative_to(site_packages).as_posix()
                    if arcname not in seen:
                        archive.write(source, arcname)
                        seen.add(arcname)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    if not any(name.startswith('mediapipe/') for name in seen):
        raise SystemExit('samsara.spec: gesture component has no mediapipe files')
    if not any(name.startswith('cv2/') for name in seen):
        raise SystemExit('samsara.spec: gesture component has no cv2 files')
    os.replace(temporary, output)


from samsara import __version__  # noqa: E402
_write_gesture_component(app_dir / 'dist' / f'Samsara-GestureControl-v{__version__}.zip')
