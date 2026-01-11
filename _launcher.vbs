Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Morne\AppData\Local\DictationApp"
WshShell.Run """C:\Users\Morne\miniconda3\envs\sami\pythonw.exe"" ""C:\Users\Morne\AppData\Local\DictationApp\dictation.py""", 0, False
Set WshShell = Nothing
