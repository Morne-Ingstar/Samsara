"""Print the Word pack's dry-run effect table without launching Word."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.commands import word
from tools.dump_command_metadata import build_executor


TRACE_ACTIONS = {
    "font bigger": "hotkey Ctrl+]",
    "font smaller": "hotkey Ctrl+[",
    "bold that": "hotkey Ctrl+B",
    "italic that": "hotkey Ctrl+I",
    "underline that": "hotkey Ctrl+U",
    "single spacing": "hotkey Ctrl+1",
    "one and a half spacing": "hotkey Ctrl+5",
    "double spacing": "hotkey Ctrl+2",
    "align left": "hotkey Ctrl+L",
    "align center": "hotkey Ctrl+E",
    "align right": "hotkey Ctrl+R",
    "justify text": "hotkey Ctrl+J",
    "bullet list": "hotkey Ctrl+Shift+L",
    "select paragraph in word": "hotkey Ctrl+Shift+Down",
    "undo in word": "hotkey Ctrl+Z",
    "redo in word": "hotkey Ctrl+Y",
    "numbered list": "COM Selection.Range.ListFormat toggle",
    "font size": "COM Selection.Font.Size = <6..72>",
    "line spacing": "COM Selection.ParagraphFormat.LineSpacingRule = <1|1.5|2>",
    "set font": "COM Selection.Font.Name = installed <name>",
    "select sentence": "COM Selection.Expand(wdSentence)",
    "select line": "COM Selection.Expand(wdLine)",
    "select word": "COM Selection.Expand(wdWord)",
    "word count": "COM Selection.Words.Count; speak result",
}


def main() -> int:
    executor = build_executor()
    phrases = sorted({entry.phrase for entry in executor._matcher._sorted if entry.pack == "word"})
    if set(phrases) != set(TRACE_ACTIONS):
        raise SystemExit("Word trace table is out of sync with the registered pack")
    print("phrase\tfocus guard\tdry-run effect")
    for phrase in phrases:
        print(f"{phrase}\twinword.exe required\t{TRACE_ACTIONS[phrase]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
