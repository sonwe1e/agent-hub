' Agent Hub desktop launcher - no browser, address bar, or console window.
' Prefer the packaged app; desktop.cmd discovers a working source runtime.
' Keep this file ASCII-only: WScript reads .vbs in the ANSI codepage.
Option Explicit
Dim shell, fso, folder, entry, rc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
entry = fso.BuildPath(folder, "dist\AgentHub.exe")
shell.CurrentDirectory = folder
If fso.FileExists(entry) Then
  shell.Run """" & entry & """", 1, False
  WScript.Quit 0
End If
entry = fso.BuildPath(folder, "desktop.cmd")

If Not fso.FileExists(entry) Then
  MsgBox "Agent Hub entry point not found:" & vbCrLf & entry, 16, "Agent Hub"
  WScript.Quit 1
End If

rc = shell.Run("cmd.exe /c """"" & entry & """""", 0, True)
If rc <> 0 Then
  MsgBox "Agent Hub failed to start (exit code " & rc & ")." & vbCrLf & vbCrLf & _
         "Run desktop.cmd in a terminal to see the reason." & vbCrLf & _
         "Install requirements-desktop.txt or build dist\AgentHub.exe.", 16, "Agent Hub"
End If
