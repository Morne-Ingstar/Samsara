# -*- mode: python ; coding: utf-8 -*-
"""
Samsara PyInstaller Spec File
Creates a standalone directory-based distribution
"""

import os
import sys
from pathlib import Path

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
if (app_dir / 'config.json').exists():
    datas.append((str(app_dir / 'config.json'), '.'))

# ============================================================================
# BINARIES (DLLs)
# ============================================================================
binaries = []

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
    
    # Samsara modules
    'samsara',
    'samsara.calibration',
    'samsara.clipboard',
    'samsara.command_parser',
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
    'samsara.ui',
    'samsara.ui.settings_window',
    'samsara.ui.first_run_wizard',
    'samsara.ui.history_window',
    'samsara.ui.splash',
    'samsara.ui.profile_manager_ui',
    'samsara.ui.wake_word_debug',
    'samsara.ui.listening_indicator',
    'voice_training',
]

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
        'scipy',
        'pandas',
        'IPython',
        'jupyter',
        # Exclude heavy ML frameworks not needed for faster_whisper
        'torch',
        'torchgen',
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
        'torchaudio',   # faster_whisper doesn't use this
        'xformers',
        'triton',
        'altair',
        'streamlit',
        'gradio',
        'sklearn',
        'scikit-learn',
        # More unused transitive deps
        'cv2',
        'opencv-python',
        'numba',
        'llvmlite',
        'librosa',
        'pandas',
        'h5py',
        'scipy',
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
