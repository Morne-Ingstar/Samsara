from samsara.plugin_commands import command


@command("greet me", aliases=["say hello", "hello"], pack="utilities")
def greet(app, text, **kwargs):
    """Prints a greeting, as an example of how a plugin command works."""
    import pyperclip
    import pyautogui
    pyperclip.copy("Hello! Samsara plugin system is working.")
    pyautogui.hotkey('ctrl', 'v')
    return True
