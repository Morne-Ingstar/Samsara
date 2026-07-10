# -*- mode: python ; coding: utf-8 -*-
"""
Samsara PyInstaller Spec File
Creates a standalone directory-based distribution
"""

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

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

# 2b. OpenWakeWord — collect everything (Python files, ONNX models, resources)
oww_datas, oww_binaries, oww_hiddenimports = collect_all('openwakeword')
datas    += oww_datas

# 2c. PySide6 / shiboken6 — collect everything (2026-07-10 import audit).
# ~48 samsara/ui/*_qt.py files depend on PySide6, and it was completely
# uncollected here (no datas/binaries/hiddenimports at all) -- this is the
# ModuleNotFoundError that first surfaced from CI's clean-env build.
# Qt's plugin architecture (platforms/qwindows.dll, styles, imageformats,
# translations) loads DLLs dynamically at runtime, not via Python import --
# invisible to PyInstaller's static analysis regardless of hiddenimports,
# so a blanket collect_all (not just hiddenimports) is required, same as
# the openwakeword pattern above. shiboken6 is PySide6's binding-generator
# runtime dependency (see requirements.txt) and needs the same treatment.
pyside6_datas, pyside6_binaries, pyside6_hiddenimports = collect_all('PySide6')
shiboken6_datas, shiboken6_binaries, shiboken6_hiddenimports = collect_all('shiboken6')
datas += pyside6_datas + shiboken6_datas

# 2d. mediapipe — collect everything. Ships model data files (hand-tracking
# .tflite/.binarypb graphs) that PyInstaller's static analysis cannot see
# (loaded by path at runtime, not imported), so hiddenimports alone would
# leave the gesture lane silently broken in a frozen build even with the
# package itself correctly bundled.
mediapipe_datas, mediapipe_binaries, mediapipe_hiddenimports = collect_all('mediapipe')
datas += mediapipe_datas

# 3. customtkinter themes and assets
customtkinter_path = os.path.join(site_packages, 'customtkinter')
if os.path.exists(customtkinter_path):
    datas.append((customtkinter_path, 'customtkinter'))

# 4. sounddevice PortAudio binaries
sounddevice_data = os.path.join(site_packages, '_sounddevice_data')
if os.path.exists(sounddevice_data):
    datas.append((sounddevice_data, '_sounddevice_data'))

# 5. App-specific data files
datas.append((str(app_dir / 'sounds'), 'sounds'))
datas.append((str(app_dir / 'profiles'), 'profiles'))
datas.append((str(app_dir / 'commands.json'), '.'))
# Bundle the entire plugins directory so commands/*.py are available at runtime
datas.append((str(app_dir / 'plugins'), 'plugins'))
# tools/ (2026-07-10) -- plugins/commands/stremio.py does `import
# stremio_control` after inserting Path(__file__).resolve().parents[2] /
# "tools" onto sys.path. plugins/commands/*.py are loaded dynamically at
# runtime (directory scan + exec, not a static import PyInstaller's own
# analysis ever sees -- see the hiddenimports comment above for
# samsara.audio_switch), so that import was never bundled at all: CI's
# frozen smoke test failed with "ModuleNotFoundError: No module named
# 'stremio_control'". parents[2] from a frozen plugin file (dist\Samsara\
# _internal\plugins\commands\stremio.py) already correctly computes
# _internal\tools -- the bootstrap code was right all along, tools/ was
# just never actually placed there. Bundling it as data (same pattern as
# plugins/ immediately above) fixes this with no plugin code changes
# required; plugins/commands/stremio.py additionally hardens its import
# with a sys._MEIPASS-aware fallback in case that ever changes.
# Unlike plugins/ (a curated runtime directory), tools/ also accumulates
# local dev-only artifacts -- notably tools/stremio_remote_token.txt, a
# REAL local secret for the LAN phone remote generated during testing, and
# __pycache__ -- that must never end up inside a distributed build. Bundle
# only *.py files (every tools/ module, so a future plugin depending on a
# different tools/ module doesn't silently break again the same way).
_tools_dir = app_dir / 'tools'
if _tools_dir.exists():
    for _py_file in _tools_dir.glob('*.py'):
        datas.append((str(_py_file), 'tools'))
# NOTE: config.json is intentionally NOT bundled — it contains dev-machine
# paths and credentials. A fresh config is generated on first run.

# ============================================================================
# BINARIES (DLLs)
# ============================================================================
binaries = []

# OpenWakeWord binaries (collected earlier)
binaries += oww_binaries

# PySide6 / shiboken6 / mediapipe binaries (collected earlier) -- Qt platform
# plugins, shiboken6's compiled binding runtime, mediapipe's compiled graph
# runner .pyd/.dll files.
binaries += pyside6_binaries + shiboken6_binaries + mediapipe_binaries

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
    # Qt UI framework (2026-07-10 import audit) -- collect_all('PySide6')
    # above already pulls in the bulk of it; these specific submodules are
    # listed explicitly too as a defensive backstop, matching this file's
    # existing style for faster_whisper's submodules below.
    'PySide6',
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'shiboken6',

    # Screen/webcam frame handling (2026-07-10 import audit)
    'cv2',

    # Cloud-fallback TTS voice (2026-07-10 import audit)
    'edge_tts',

    # Gesture lane webcam hand-tracking (2026-07-10 import audit) --
    # collect_all('mediapipe') above handles its model data files.
    'mediapipe',

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
    
    # UI
    'customtkinter',
    'tkinter',
    'tkinter.ttk',
    'tkinter.messagebox',
    
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

    # Samsara TTS subsystem
    'samsara.tts',
    'samsara.tts.coordinator',
    'samsara.tts.winrt_engine',
    'samsara.tts.edge_engine',
    'samsara.tts.exceptions',

    # Samsara Smart Actions
    'samsara.smart_actions_bridge',
    'samsara.smart_actions_session',
    'samsara.smart_actions_tools',

    # Samsara UI
    'samsara.ui',
    'samsara.ui.settings_window',
    'samsara.ui.first_run_wizard',
    'samsara.ui.history_window',
    'samsara.ui.splash',
    'samsara.ui.profile_manager_ui',
    'samsara.ui.wake_word_debug',
    'samsara.ui.listening_indicator',
    'samsara.ui.main_window',
    'samsara.ui.history_frame',
    'samsara.ui.dictionary_frame',
    'samsara.ui.command_cheatsheet',
    'samsara.ui.tts_settings_tab',
    'samsara.ui.task_overlay',
    # Same plugin-only dynamic-load blind spot as samsara.audio_switch above.
    'samsara.ui.numbers_overlay_qt',
    'samsara.ui.status_overlay',
    'samsara.ui.workflow_capture_qt',
    'samsara.ui.tabs',
    'samsara.ui.tabs.general_tab',
    'samsara.ui.tabs.advanced_tab',
    'samsara.ui.tabs.cloud_llm_tab',
    'samsara.ui.tabs.hotkeys_tab',
    'samsara.ui.tabs.sounds_tab',
    'samsara.ui.tabs.commands_tab',
    'samsara.ui.tabs.alarms_tab',

    # Samsara CUDA detection
    'samsara.cuda_detect',

    # Samsara OWW pre-filter
    'samsara.wake_detector',

    'voice_training',
]

# Merge imports collected by collect_all('openwakeword')
hiddenimports += oww_hiddenimports

# Merge imports collected by collect_all('PySide6' / 'shiboken6' / 'mediapipe')
hiddenimports += pyside6_hiddenimports + shiboken6_hiddenimports + mediapipe_hiddenimports

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
    runtime_hooks=[],
    excludes=[
        # Exclude problematic modules
        'charset_normalizer',
        # Exclude unnecessary large packages
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
        # torchaudio is needed by Silero VAD — do NOT exclude
        'xformers',
        'triton',
        'altair',
        'streamlit',
        'gradio',
        # More unused transitive deps
        # NOTE (2026-07-10): 'cv2' / 'opencv-python' were WRONGLY excluded
        # here -- cv2 is directly imported by the gesture lane and show-
        # numbers overlay (3 files) and is a hard mediapipe dependency.
        # This exclusion was actively breaking every frozen build that
        # touched those features; removed, not just left uncommented, so
        # it can't silently come back via a careless copy-paste.
        'numba',
        'llvmlite',
        'librosa',
        'pandas',
        'h5py',
        'pytest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

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
    icon=None,
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
