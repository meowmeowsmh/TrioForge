' TrioForge - double-click THIS to start, completely hidden.
'
' A .bat always opens a console window when you double-click it, no matter what.
' This VBScript runs the same thing with the window hidden (0), so the only thing
' you ever see is TrioForge's own app window. The server lives silently in the
' background.
'
' It simply calls start.bat invisibly; everything real is in there.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
path = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = path
sh.Run """" & path & "\start.bat""", 0, False
