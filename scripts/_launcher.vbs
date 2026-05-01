Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Morne\Projects\Samsara-dev"
WshShell.Run """C:\Users\Morne\miniconda3\envs\sami\pythonw.exe"" ""C:\Users\Morne\Projects\Samsara-dev\dictation.py""", 0, False
Set WshShell = Nothing
