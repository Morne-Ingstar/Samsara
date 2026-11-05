from samsara.plugin_commands import command

GREETING_TEXT = "Hello! Samsara plugin system is working."


@command("greet me", aliases=["say hello", "hello"], pack="utilities")
def greet(app, text, **kwargs):
    """Prints a greeting, as an example of how a plugin command works."""
    from samsara.clipboard import copy_private_text
    import pyautogui
    copy_private_text(GREETING_TEXT)
    pyautogui.hotkey('ctrl', 'v')
    return True
