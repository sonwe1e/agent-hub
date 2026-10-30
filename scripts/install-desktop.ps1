#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DesktopDirectory = [Environment]::GetFolderPath('DesktopDirectory'),
    [string]$ProgramsDirectory = [Environment]::GetFolderPath('Programs')
)

$ErrorActionPreference = 'Stop'
$HubRoot = Split-Path -Parent $PSScriptRoot
$Executable = Join-Path $HubRoot 'dist\AgentHub.exe'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw 'Build dist\AgentHub.exe first: python build_desktop.py'
}

$Shell = New-Object -ComObject WScript.Shell
$RootPrefix = [IO.Path]::GetFullPath($HubRoot).TrimEnd('\') + '\'
function Test-HubShortcut($Shortcut) {
    return $Shortcut.TargetPath.StartsWith($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $Shortcut.Arguments.IndexOf($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

# Keep an existing Start Menu category instead of adding duplicate search results.
$MenuLinks = @()
if (Test-Path -LiteralPath $ProgramsDirectory) {
    $MenuLinks = @(Get-ChildItem -LiteralPath $ProgramsDirectory -Filter 'Agent Hub.lnk' -Recurse |
        Where-Object { Test-HubShortcut ($Shell.CreateShortcut($_.FullName)) } |
        ForEach-Object { $_.FullName })
}
if (-not $MenuLinks.Count) { $MenuLinks = @(Join-Path $ProgramsDirectory 'Agent Hub.lnk') }
$Destinations = @((Join-Path $DesktopDirectory 'Agent Hub.lnk')) + $MenuLinks
$BackupDirectory = Join-Path $HubRoot ('backups\desktop-shortcuts\' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff'))
$Index = 0
foreach ($Destination in $Destinations) {
    $Shortcut = $Shell.CreateShortcut($Destination)
    if (Test-Path -LiteralPath $Destination) {
        if (-not (Test-HubShortcut $Shortcut)) {
            throw "An unrelated shortcut already exists: $Destination"
        }
        if ($Shortcut.TargetPath -eq $Executable -and -not $Shortcut.Arguments -and
            $Shortcut.WorkingDirectory -eq $HubRoot -and $Shortcut.IconLocation -eq "$Executable,0") {
            Write-Output "Already installed: $Destination"
            continue
        }
        New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
        Copy-Item -LiteralPath $Destination -Destination (Join-Path $BackupDirectory "$Index.lnk")
        $Index++
    }
    New-Item -ItemType Directory -Path (Split-Path -Parent $Destination) -Force | Out-Null
    $Shortcut.TargetPath = $Executable
    $Shortcut.Arguments = ''
    $Shortcut.WorkingDirectory = $HubRoot
    $Shortcut.IconLocation = "$Executable,0"
    $Shortcut.Description = 'Agent Hub desktop application'
    $Shortcut.WindowStyle = 1
    $Shortcut.Save()
    Write-Output "Installed: $Destination -> $Executable"
}
