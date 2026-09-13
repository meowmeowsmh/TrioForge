' TrioForge - the light way: start the server hidden and open it in YOUR browser.
'
' Same app, same server, same data. The difference is memory: a browser tab reuses the
' engine that is already running (~150-300 MB), while the app's own window has to load
' a private WebView2 engine (~600 MB). Use this when the machine is tight; use
' start.vbs when you want TrioForge in a window of its own.
'
' Nothing is shown while the server starts.

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
path = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = path
sh.Run """" & path & "\start-web.bat""", 0, False
