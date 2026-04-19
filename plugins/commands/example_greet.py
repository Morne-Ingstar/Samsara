from samsara.plugin_commands import command


@command("greet me", aliases=["say hello", "hello"])
def greet(app, text, **kwargs):
    """Say a greeting via text output."""
    import pyperclip
    import pyautogui
    pyperclip.copy("Hello! Samsara plugin system is working.")
    pyautogui.hotkey('ctrl', 'v')
    return True
