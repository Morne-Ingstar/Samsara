"""Keyboard selection without an AI backend."""
from samsara.plugin_commands import command


@command("select previous", pack="core", ai_visible=True,
         risk_class="reversible", ai_composable=False, side_effects=["keystrokes"],
         preconditions=[], voice_triggerable=True, reversible=True,
         param_schema={"remainder": {"type": "str", "required": True}},
         preview_template="Select the previous words")
def select_previous(app, remainder):
    """Selects the previous 1 to 100 words; say select previous three words."""
    import re
    import pyautogui
    from samsara.spoken_numbers import parse_spoken_number
    from samsara.text_selection import _notice
    match = re.fullmatch(r"(.+?)\s+words?", remainder.strip(), re.I)
    count = parse_spoken_number(match[1]) if match else None
    if count is None or not 1 <= count <= 100:
        _notice(app, "Say select previous three words; choose 1 to 100 words.")
        return True
    pyautogui.keyDown("ctrl")
    pyautogui.keyDown("shift")
    try:
        pyautogui.press("left", presses=count, interval=0)
    finally:
        pyautogui.keyUp("shift")
        pyautogui.keyUp("ctrl")
    return True
