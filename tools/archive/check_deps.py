import importlib
for pkg in ['anthropic', 'openai', 'google.generativeai', 'dotenv', 'customtkinter']:
    try:
        m = importlib.import_module(pkg)
        v = getattr(m, '__version__', getattr(m, 'VERSION', '?'))
        print(f"  {pkg}: {v}")
    except ImportError:
        print(f"  {pkg}: NOT INSTALLED")
