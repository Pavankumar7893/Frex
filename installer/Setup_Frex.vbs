Option Explicit

Dim fso, shell, sourceDir, installDir, appData, pythonExe, pythonwExe
Dim startMenu, desktop, shortcut, rc, answer, tempFile, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
sourceDir = fso.GetParentFolderName(WScript.ScriptFullName)
appData = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%")
installDir = appData & "\Programs\Frex"

If Not fso.FileExists(sourceDir & "\Frex_3_1.py") Or Not fso.FileExists(sourceDir & "\Frex_3_1.html") Then
    MsgBox "The Frex application files are incomplete." & vbCrLf & "Extract the entire ZIP first, then run Setup_Frex.vbs.", vbCritical, "Frex Setup"
    WScript.Quit 1
End If

pythonExe = FindPython()
If Len(pythonExe) = 0 Then
    answer = MsgBox("Frex needs Python 3.10 or newer. Python was not found on this PC." & vbCrLf & vbCrLf & _
                    "Open the official Python download page?", vbQuestion + vbYesNo, "Frex Setup")
    If answer = vbYes Then shell.Run "https://www.python.org/downloads/windows/", 1, False
    WScript.Quit 2
End If

pythonwExe = fso.BuildPath(fso.GetParentFolderName(pythonExe), "pythonw.exe")
If Not fso.FileExists(pythonwExe) Then
    MsgBox "The matching pythonw.exe was not found beside Python." & vbCrLf & "Repair your Python installation and try again.", vbCritical, "Frex Setup"
    WScript.Quit 3
End If

On Error Resume Next
If Not fso.FolderExists(installDir) Then fso.CreateFolder installDir
If Err.Number <> 0 Then
    MsgBox "Could not create the Frex application folder:" & vbCrLf & installDir & vbCrLf & Err.Description, vbCritical, "Frex Setup"
    WScript.Quit 4
End If
Err.Clear

fso.CopyFile sourceDir & "\Frex_3_1.py", installDir & "\Frex_3_1.py", True
fso.CopyFile sourceDir & "\Frex_3_1.html", installDir & "\Frex_3_1.html", True
fso.CopyFile sourceDir & "\Frex.ico", installDir & "\Frex.ico", True
fso.CopyFile sourceDir & "\Uninstall_Frex.vbs", installDir & "\Uninstall_Frex.vbs", True
If fso.FileExists(sourceDir & "\README_Frex.txt") Then fso.CopyFile sourceDir & "\README_Frex.txt", installDir & "\README_Frex.txt", True
If fso.FileExists(sourceDir & "\LICENSE") Then fso.CopyFile sourceDir & "\LICENSE", installDir & "\LICENSE", True
If Err.Number <> 0 Then
    MsgBox "Could not copy Frex files:" & vbCrLf & Err.Description, vbCritical, "Frex Setup"
    WScript.Quit 5
End If
On Error GoTo 0

MsgBox "Frex is preparing its desktop components. This step runs quietly and may take a few minutes." & vbCrLf & vbCrLf & _
       "You won't need to use Command Prompt. Please keep this setup window open.", vbInformation, "Installing Frex"

If Not HasWebViewRuntime() Then
    MsgBox "The Microsoft Edge WebView2 Runtime is missing. Setup will install this Microsoft component quietly.", vbInformation, "Frex Desktop Component"
    If Not InstallWebViewRuntime() Then
        MsgBox "WebView2 Runtime could not be installed automatically." & vbCrLf & _
               "Open the official Microsoft download page, install the Evergreen Runtime, then run Setup_Frex.vbs again.", vbExclamation, "Frex Setup"
        shell.Run "https://developer.microsoft.com/microsoft-edge/webview2/", 1, False
        WScript.Quit 7
    End If
End If

cmd = Quote(pythonExe) & " -m pip install --user --upgrade pywebview yt-dlp imageio-ffmpeg"
rc = shell.Run(cmd, 0, True)
If rc <> 0 Then
    MsgBox "Frex copied successfully, but its desktop components could not be installed." & vbCrLf & _
           "Check your internet connection and run Setup_Frex.vbs again." & vbCrLf & vbCrLf & _
           "Installer result: " & rc, vbExclamation, "Frex Setup"
    WScript.Quit 6
End If

startMenu = shell.SpecialFolders("Programs") & "\Frex"
If Not fso.FolderExists(startMenu) Then fso.CreateFolder startMenu
Set shortcut = shell.CreateShortcut(startMenu & "\Frex.lnk")
shortcut.TargetPath = pythonwExe
shortcut.Arguments = Quote(installDir & "\Frex_3_1.py") & " --desktop"
shortcut.WorkingDirectory = installDir
shortcut.IconLocation = installDir & "\Frex.ico"
shortcut.Description = "Frex — Video Downloader and Frame Extractor"
shortcut.Save
Set shortcut = shell.CreateShortcut(startMenu & "\Uninstall Frex.lnk")
shortcut.TargetPath = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\wscript.exe")
shortcut.Arguments = Quote(installDir & "\Uninstall_Frex.vbs")
shortcut.WorkingDirectory = installDir
shortcut.IconLocation = installDir & "\Frex.ico"
shortcut.Description = "Uninstall Frex"
shortcut.Save

Set shortcut = shell.CreateShortcut(shell.SpecialFolders("Desktop") & "\Frex.lnk")
shortcut.TargetPath = pythonwExe
shortcut.Arguments = Quote(installDir & "\Frex_3_1.py") & " --desktop"
shortcut.WorkingDirectory = installDir
shortcut.IconLocation = installDir & "\Frex.ico"
shortcut.Description = "Frex — Video Downloader and Frame Extractor"
shortcut.Save

MsgBox "Frex is installed!" & vbCrLf & vbCrLf & _
       "A Frex shortcut is now on your Desktop and in the Start Menu." & vbCrLf & _
       "Double-click Frex whenever you want to use it. No terminal window is needed.", vbInformation, "Frex Ready"
shell.Run Quote(pythonwExe) & " " & Quote(installDir & "\Frex_3_1.py") & " --desktop", 1, False

Function FindPython()
    Dim candidates, i, p, localPrograms, programFiles, tfile, runCmd, outText
    localPrograms = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python"
    programFiles = shell.ExpandEnvironmentStrings("%ProgramFiles%")
    candidates = Array( _
        localPrograms & "\Python313\python.exe", _
        localPrograms & "\Python312\python.exe", _
        localPrograms & "\Python311\python.exe", _
        localPrograms & "\Python310\python.exe", _
        programFiles & "\Python313\python.exe", _
        programFiles & "\Python312\python.exe", _
        programFiles & "\Python311\python.exe", _
        programFiles & "\Python310\python.exe")
    For i = 0 To UBound(candidates)
        If fso.FileExists(candidates(i)) Then
            FindPython = candidates(i)
            Exit Function
        End If
    Next
    ' Fallback to the Python launcher; the probe is run hidden and writes only its path to a temp file.
    tfile = shell.ExpandEnvironmentStrings("%TEMP%") & "\frex_python_path.txt"
    If fso.FileExists(tfile) Then fso.DeleteFile tfile, True
    runCmd = "cmd.exe /c py -3.12 -c ""import sys; print(sys.executable)"" > " & Quote(tfile) & " 2>nul"
    shell.Run runCmd, 0, True
    If fso.FileExists(tfile) Then
        Set p = fso.OpenTextFile(tfile, 1, False)
        outText = Trim(p.ReadAll)
        p.Close
        fso.DeleteFile tfile, True
        If fso.FileExists(outText) Then
            FindPython = outText
            Exit Function
        End If
    End If
    FindPython = ""
End Function

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function


Function HasWebViewRuntime()
    Dim roots, root, folder, child, exe
    roots = Array( _
        shell.ExpandEnvironmentStrings("%ProgramFiles(x86)%") & "\Microsoft\EdgeWebView\Application", _
        shell.ExpandEnvironmentStrings("%ProgramFiles%") & "\Microsoft\EdgeWebView\Application", _
        shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Microsoft\EdgeWebView\Application")
    For Each root In roots
        If fso.FolderExists(root) Then
            Set folder = fso.GetFolder(root)
            For Each child In folder.SubFolders
                exe = child.Path & "\msedgewebview2.exe"
                If fso.FileExists(exe) Then
                    HasWebViewRuntime = True
                    Exit Function
                End If
            Next
        End If
    Next
    HasWebViewRuntime = False
End Function

Function InstallWebViewRuntime()
    Dim http, stream, installer, statusCode, result
    On Error Resume Next
    installer = shell.ExpandEnvironmentStrings("%TEMP%") & "\MicrosoftEdgeWebView2Setup.exe"
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    http.setTimeouts 15000, 15000, 60000, 60000
    http.Open "GET", "https://go.microsoft.com/fwlink/p/?LinkId=2124703", False
    http.Send
    statusCode = http.Status
    If statusCode <> 200 Then
        InstallWebViewRuntime = False
        Exit Function
    End If
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 1
    stream.Open
    stream.Write http.responseBody
    stream.SaveToFile installer, 2
    stream.Close
    If Err.Number <> 0 Or Not fso.FileExists(installer) Then
        Err.Clear
        InstallWebViewRuntime = False
        Exit Function
    End If
    result = shell.Run(Quote(installer) & " /silent /install", 0, True)
    InstallWebViewRuntime = (result = 0 Or HasWebViewRuntime())
    If fso.FileExists(installer) Then fso.DeleteFile installer, True
    On Error GoTo 0
End Function
