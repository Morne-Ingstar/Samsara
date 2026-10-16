import sys
print(f"Python: {sys.executable}")
print(f"Version: {sys.version}")

deps = [
    'faster_whisper', 'pystray', 'sounddevice', 'customtkinter',
    'pyautogui', 'pyperclip', 'pynput', 'PIL', 'numpy',
    'ctranslate2', 'onnxruntime', 'PyInstaller'
]
missing = []
for dep in deps:
    try:
        __import__(dep)
        print(f"  OK: {dep}")
    except ImportError:
        print(f"  MISSING: {dep}")
        missing.append(dep)

if missing:
    print(f"\nMissing: {', '.join(missing)}")
else:
    print("\nAll dependencies available. Ready to build.")
