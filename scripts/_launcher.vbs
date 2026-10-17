Set WshShell = CreateObject("WScript.Shell")
Set Fso = CreateObject("Scripting.FileSystemObject")
ProjectDir = Fso.GetParentFolderName(Fso.GetParentFolderName(WScript.ScriptFullName))
WshShell.CurrentDirectory = ProjectDir
WshShell.Run "cmd /c """ & ProjectDir & "\run_samsara.bat""", 0, False
Set Fso = Nothing
Set WshShell = Nothing
