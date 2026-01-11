Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\Users\Morne\Projects\Samsara"
WshShell.Run """C:\Users\Morne\miniconda3\envs\sami\pythonw.exe"" ""C:\Users\Morne\Projects\Samsara\dictation.py""", 0, False
Set WshShell = Nothing
