' Agent Hub workbench launcher - opens the local workbench with no console window.
' Interpreter discovery lives in hub.cmd (PATH python.exe can be a Microsoft
' Store stub that exits 9009), so run that hidden and surface its exit code.
' Keep this file ASCII-only: WScript reads .vbs in the ANSI codepage.
Option Explicit
Dim shell, fso, folder, entry, rc
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
folder = fso.GetParentFolderName(WScript.ScriptFullName)
entry = fso.BuildPath(folder, "hub.cmd")

If Not fso.FileExists(entry) Then
  MsgBox "Agent Hub entry point not found:" & vbCrLf & entry, 16, "Agent Hub"
  WScript.Quit 1
End If

rc = shell.Run("cmd.exe /c """"" & entry & """ gui""", 0, True)
If rc <> 0 Then
  MsgBox "Agent Hub failed to start (exit code " & rc & ")." & vbCrLf & vbCrLf & _
         "Run  hub.cmd status  in a terminal to see the reason," & vbCrLf & _
         "or set MIMO_PYTHON to a working python.exe.", 16, "Agent Hub"
End If
