Option Explicit

Dim fso, shell, portableHome, psScript, psExe, logDirectory, logPath, logFile, command, runResult
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
portableHome = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(portableHome, "Start-DonutBrowser.ps1")
psExe = fso.BuildPath(shell.ExpandEnvironmentStrings("%SystemRoot%"), "System32\WindowsPowerShell\v1.0\powershell.exe")
logDirectory = fso.BuildPath(fso.BuildPath(portableHome, "Temp"), "DonutOnly")
If Not fso.FolderExists(logDirectory) Then fso.CreateFolder logDirectory
logPath = fso.BuildPath(logDirectory, "vbs-launch.log")
Set logFile = fso.OpenTextFile(logPath, 8, True, 0)
logFile.WriteLine Now & " INFO VBS entrypoint started."
logFile.Close

command = """" & psExe & """ -NoLogo -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File """ & psScript & """"
runResult = shell.Run(command, 0, True)
Set logFile = fso.OpenTextFile(logPath, 8, True, 0)
logFile.WriteLine Now & " INFO PowerShell launcher exited (result " & runResult & ")."
logFile.Close
