Option Explicit
Dim fso, shell, root, startMenu, desktop, answer
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(WScript.ScriptFullName)
startMenu = shell.SpecialFolders("Programs") & "\Frex"
desktop = shell.SpecialFolders("Desktop") & "\Frex.lnk"
answer = MsgBox("Remove the Frex application and its shortcuts?" & vbCrLf & vbCrLf & _
                "Your settings, frame cache, and downloaded videos will be kept.", vbQuestion + vbYesNo, "Uninstall Frex")
If answer <> vbYes Then WScript.Quit 0
On Error Resume Next
If fso.FileExists(desktop) Then fso.DeleteFile desktop, True
If fso.FolderExists(startMenu) Then fso.DeleteFolder startMenu, True
' Remove the installed application directory after this script has finished.
shell.Run "cmd.exe /c ping 127.0.0.1 -n 2 >nul & rmdir /s /q " & Chr(34) & root & Chr(34), 0, False
On Error GoTo 0
MsgBox "Frex has been removed. Your user data was kept in %APPDATA%\Frex.", vbInformation, "Frex Uninstalled"
