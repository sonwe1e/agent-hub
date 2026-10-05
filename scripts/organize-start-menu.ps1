#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet('User', 'Common', 'All')][string]$Scope = 'User',
    [switch]$Preview,
    [string]$RestoreManifest
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$HubRoot = Split-Path -Parent $PSScriptRoot
$BackupRoot = Join-Path $HubRoot 'backups\start-menu'
$Rules = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'start-menu-rules.json') -Raw | ConvertFrom-Json -AsHashtable
$Roots = @{ user = [Environment]::GetFolderPath('Programs'); common = [Environment]::GetFolderPath('CommonPrograms') }
$Shell = New-Object -ComObject WScript.Shell
$Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$IsAdmin = ([Security.Principal.WindowsPrincipal]::new($Identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Test-InRoot([string]$Path, [string]$Root) {
    return [IO.Path]::GetFullPath($Path).StartsWith([IO.Path]::GetFullPath($Root).TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Save-Manifest($Manifest, [string]$Path) {
    $temp = $Path + '.tmp'
    $Manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $temp -Encoding utf8
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Get-Fingerprint($Record) {
    $text = @($Record.Target.ToLowerInvariant(), $Record.Arguments, $Record.WorkingDirectory.ToLowerInvariant()) -join "`0"
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($text)))
}

function Remove-EmptyParents([string[]]$Paths, [string[]]$AllowedRoots) {
    $directories = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($path in $Paths) {
        $parent = Split-Path -Parent $path
        while ($parent -and @($AllowedRoots | Where-Object { Test-InRoot $parent $_ }).Count) {
            [void]$directories.Add($parent)
            $parent = Split-Path -Parent $parent
        }
    }
    foreach ($directory in @($directories | Sort-Object Length -Descending)) {
        $root = @($AllowedRoots | Where-Object { Test-InRoot $directory $_ })[0]
        $top = $directory.Substring($root.Length + 1).Split('\')[0]
        if ($top -in $Rules.protected) { continue }
        if ((Test-Path -LiteralPath $directory -PathType Container) -and @(Get-ChildItem -LiteralPath $directory -Force).Count -eq 0) {
            try { Remove-Item -LiteralPath $directory -Force } catch { Write-Warning "空目录保留：$directory" }
        }
    }
}

if ($RestoreManifest) {
    $ManifestPath = (Resolve-Path -LiteralPath $RestoreManifest).Path
    if (-not (Test-InRoot $ManifestPath $BackupRoot)) { throw '仅允许还原本工具备份目录中的记录。' }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json -AsHashtable
    if (@($manifest.Operations | Where-Object { $_.Scope -eq 'common' }).Count -and -not $IsAdmin) { throw '还原公共菜单需要在管理员 PowerShell 中执行。' }
    $allowedRoots = @($manifest.Roots.user, $manifest.Roots.common)
    $runRoot = Split-Path -Parent $ManifestPath
    $restored = 0
    $conflicts = @()
    $cleanup = @()
    $operations = @($manifest.Operations)
    [Array]::Reverse($operations)
    foreach ($entry in $operations) {
        if ($entry.Restored -or $entry.State -eq 'Failed') { continue }
        try {
            if (-not @($allowedRoots | Where-Object { Test-InRoot $entry.Source $_ }).Count) { throw '原始路径超出开始菜单范围' }
            if (-not @(@($allowedRoots) + @($runRoot) | Where-Object { Test-InRoot $entry.Destination $_ }).Count) { throw '备份路径超出允许范围' }
            if (-not (Test-Path -LiteralPath $entry.Destination -PathType Leaf)) { throw '整理后的文件不存在' }
            if (-not $entry.Hash -or (Get-FileHash -LiteralPath $entry.Destination -Algorithm SHA256).Hash -ne $entry.Hash) { throw '文件已被修改，为避免覆盖而保留' }
            if ($entry.Kind -eq 'Add') {
                Remove-Item -LiteralPath $entry.Destination
            } else {
                if (Test-Path -LiteralPath $entry.Source) { throw '原始位置已有文件，不覆盖' }
                New-Item -ItemType Directory -Path (Split-Path -Parent $entry.Source) -Force | Out-Null
                Move-Item -LiteralPath $entry.Destination -Destination $entry.Source
            }
            $cleanup += $entry.Destination
            $entry.Restored = $true
            $restored++
            Save-Manifest $manifest $ManifestPath
        } catch { $conflicts += "$($entry.Source): $($_.Exception.Message)" }
    }
    Remove-EmptyParents $cleanup $allowedRoots
    @{ Restored = $restored; Conflicts = $conflicts; Manifest = $ManifestPath } | ConvertTo-Json -Depth 5
    exit $(if ($conflicts.Count) { 1 } else { 0 })
}

$InventoryPath = Join-Path $BackupRoot 'inventory-before.json'
if (-not (Test-Path -LiteralPath $InventoryPath)) { throw "缺少清单：$InventoryPath" }
$inventory = Get-Content -LiteralPath $InventoryPath -Raw | ConvertFrom-Json
$categoryMap = @{}
foreach ($category in $Rules.categories.Keys) { foreach ($name in $Rules.categories[$category]) { $categoryMap[$name] = $category } }
$allRecords = @($inventory.Records | Where-Object {
    $_.Extension -in '.lnk', '.url', '.appref-ms' -and $_.Relative.Split('\')[0] -notin $Rules.protected
})

function Get-Category($Record) {
    $top = $Record.Relative.Split('\')[0]
    if ($categoryMap.ContainsKey($top)) { return $categoryMap[$top] }
    return $Rules.fallbackCategory
}

function Get-Destination($Record) {
    $category = Get-Category $Record
    $top = $Record.Relative.Split('\')[0]
    $name = Split-Path -Leaf $Record.Relative
    if ($top -in $Rules.preserveToolGroups) {
        $relative = $Record.Relative
    } elseif ($name -match '(?i)uninstall|卸载|documentation|文档|帮助|help|support|faqs?|release notes|license|爱奇艺首页|edit cpu-z config') {
        $relative = Join-Path '维护与帮助' $Record.Relative
    } else {
        $relative = if ($Rules.rename.ContainsKey($name)) { $Rules.rename[$name] } else { $name }
    }
    return Join-Path (Join-Path $Record.Root $category) $relative
}

# 只归档启动目标、参数与工作目录完全相同的 .lnk。空目标的 Shell/商店快捷方式不参与去重。
$groups = @{}
foreach ($record in $allRecords) {
    if ($record.Extension -ne '.lnk' -or -not $record.Target) { continue }
    $fingerprint = Get-Fingerprint $record
    if (-not $groups.ContainsKey($fingerprint)) { $groups[$fingerprint] = @() }
    $groups[$fingerprint] += $record
}
$duplicates = @{}
foreach ($group in $groups.Values) {
    if ($group.Count -lt 2) { continue }
    # 保留公共入口，避免移除其他用户的唯一入口；同一范围内优先正式的应用目录名称。
    $sorted = @($group | Sort-Object @{ Expression = { $_.Scope -eq 'common' }; Descending = $true }, @{ Expression = { $_.Relative.Split('\').Count }; Descending = $true }, @{ Expression = { $_.Relative.Length }; Descending = $false })
    foreach ($duplicate in $sorted[1..($sorted.Count - 1)]) { $duplicates[$duplicate.FullName] = $sorted[0] }
}
$scopes = if ($Scope -eq 'All') { @('user', 'common') } else { @($Scope.ToLowerInvariant()) }
$records = @($allRecords | Where-Object { $_.Scope -in $scopes })
foreach ($record in $records) {
    if ($record.Root -ne $Roots[$record.Scope] -or -not (Test-InRoot $record.FullName $record.Root)) { throw "清单所属用户或路径不匹配：$($record.FullName)" }
}
$guiPath = Join-Path $Roots.user 'AI 与模型\Agent Hub.lnk'
if ($Preview) {
    @{
        Scope = $Scope
        Move = @($records | Where-Object { -not $duplicates.ContainsKey($_.FullName) }).Count
        ArchiveDuplicates = @($records | Where-Object { $duplicates.ContainsKey($_.FullName) }).Count
        Categories = @($records | Where-Object { -not $duplicates.ContainsKey($_.FullName) } | Group-Object { Get-Category $_ } | ForEach-Object { @{ Name = $_.Name; Count = $_.Count } })
        AgentHubShortcut = if ('user' -in $scopes) { $guiPath } else { $null }
        NeedsAdministrator = 'common' -in $scopes -and -not $IsAdmin
    } | ConvertTo-Json -Depth 6
    exit 0
}
if ('common' -in $scopes -and -not $IsAdmin) { throw '公共开始菜单需要管理员权限；不会尝试修改权限或移动到个人菜单。请用管理员 PowerShell 执行 -Scope Common。' }

$runName = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$runRoot = Join-Path $BackupRoot $runName
New-Item -ItemType Directory -Path $runRoot -Force | Out-Null
$ManifestPath = Join-Path $runRoot 'manifest.json'
$manifest = @{ CreatedAt = (Get-Date).ToString('o'); Scope = $Scope; Roots = $Roots; Operations = @(); Failures = @() }

# 完整复制选定范围的原始文件，包括系统快捷方式；备份不在开始菜单内。
foreach ($scopeName in $scopes) {
    $root = $Roots[$scopeName]
    foreach ($file in @(Get-ChildItem -LiteralPath $root -Recurse -File -Force)) {
        $relative = $file.FullName.Substring($root.Length + 1)
        $copy = Join-Path (Join-Path $runRoot "original\$scopeName") $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $copy) -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $copy
    }
}
Save-Manifest $manifest $ManifestPath

# 先移动保留项，再归档重复项；全程不覆盖任何现有目的文件。
$ordered = @($records | Sort-Object @{ Expression = { $duplicates.ContainsKey($_.FullName) }; Descending = $false }, FullName)
foreach ($record in $ordered) {
    $entry = $null
    try {
        if (-not (Test-Path -LiteralPath $record.FullName -PathType Leaf)) { continue }
        if ($record.Extension -eq '.lnk' -and $record.Target) {
            $current = $Shell.CreateShortcut($record.FullName)
            $currentFingerprint = Get-Fingerprint @{ Target = $current.TargetPath; Arguments = $current.Arguments; WorkingDirectory = $current.WorkingDirectory }
            if ($currentFingerprint -ne (Get-Fingerprint $record)) { throw '快捷方式已更新，不按旧清单移动' }
        }
        $duplicate = $duplicates.ContainsKey($record.FullName)
        $destination = if ($duplicate) { Join-Path (Join-Path $runRoot "duplicates\$($record.Scope)") $record.Relative } else { Get-Destination $record }
        if ($duplicate) {
            $keeper = $duplicates[$record.FullName]
            if (-not (Test-Path -LiteralPath $keeper.FullName) -and -not (Test-Path -LiteralPath (Get-Destination $keeper))) { throw '保留入口不存在，不归档重复项' }
        }
        if (Test-Path -LiteralPath $destination) { throw "目的位置已有文件，不覆盖：$destination" }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        $entry = @{ Kind = $(if ($duplicate) { 'Archive' } else { 'Move' }); Scope = $record.Scope; Source = $record.FullName; Destination = $destination; Hash = (Get-FileHash -LiteralPath $record.FullName -Algorithm SHA256).Hash; State = 'Planned'; Restored = $false }
        $manifest.Operations += $entry
        Save-Manifest $manifest $ManifestPath
        Move-Item -LiteralPath $record.FullName -Destination $destination
        if ((Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash -ne $entry.Hash) { throw '移动后文件校验失败' }
        $entry.State = 'Done'
        Save-Manifest $manifest $ManifestPath
    } catch {
        if ($entry) { $entry.State = 'Failed' }
        $manifest.Failures += "$($record.FullName): $($_.Exception.Message)"
        Save-Manifest $manifest $ManifestPath
    }
}

if ('user' -in $scopes) {
    $guiEntry = $null
    try {
        $launcher = Join-Path $HubRoot 'Launch GUI.vbs'
        $launchPy = Join-Path $HubRoot 'launch.py'
        if (-not (Test-Path -LiteralPath $launcher) -and -not (Test-Path -LiteralPath $launchPy)) { throw 'Agent Hub GUI 启动器不存在' }
        $pythonCommand = Get-Command $(if ($env:MIMO_PYTHON) { $env:MIMO_PYTHON } else { 'python.exe' }) -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        $pythonw = if ($pythonCommand) { Join-Path (Split-Path -Parent $pythonCommand.Source) 'pythonw.exe' } else { $null }
        if (-not (Test-Path -LiteralPath $guiPath)) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $guiPath) -Force | Out-Null
            $guiEntry = @{ Kind = 'Add'; Scope = 'user'; Source = $guiPath; Destination = $guiPath; Hash = $null; State = 'Planned'; Restored = $false }
            $manifest.Operations += $guiEntry
            Save-Manifest $manifest $ManifestPath
            $shortcut = $Shell.CreateShortcut($guiPath)
            if ($pythonw -and (Test-Path -LiteralPath $pythonw) -and (Test-Path -LiteralPath $launchPy)) {
                $shortcut.TargetPath = $pythonw
                $shortcut.Arguments = '"' + $launchPy + '" gui'
            } elseif (Test-Path -LiteralPath $launcher) {
                $shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
                $shortcut.Arguments = '"' + $launcher + '"'
            } else { throw '无法找到无控制台启动方式，请安装 pythonw 或恢复 VBS 启动器。' }
            $shortcut.WorkingDirectory = $HubRoot
            $shortcut.Description = 'Agent Hub — 模型、供应商与多平台同步工作台'
            $ico = Join-Path $HubRoot 'ui\agent-hub.ico'
            $shortcut.IconLocation = if (Test-Path -LiteralPath $ico) { "$ico,0" } else { "$env:WINDIR\System32\imageres.dll,85" }
            $shortcut.WindowStyle = 1
            $shortcut.Save()
            $guiEntry.Hash = (Get-FileHash -LiteralPath $guiPath -Algorithm SHA256).Hash
            $guiEntry.State = 'Done'
            Save-Manifest $manifest $ManifestPath
        }
    } catch {
        if ($guiEntry) { $guiEntry.State = 'Failed' }
        $manifest.Failures += "Agent Hub: $($_.Exception.Message)"
        Save-Manifest $manifest $ManifestPath
    }
}
$success = @($manifest.Operations | Where-Object { $_.State -eq 'Done' })
Remove-EmptyParents @($success | Where-Object { $_.Kind -ne 'Add' } | ForEach-Object { $_.Source }) @($scopes | ForEach-Object { $Roots[$_] })
@{
    Scope = $Scope; Moved = @($success | Where-Object Kind -eq 'Move').Count
    ArchivedDuplicates = @($success | Where-Object Kind -eq 'Archive').Count
    Added = @($success | Where-Object Kind -eq 'Add').Count
    AgentHubShortcut = if ('user' -in $scopes) { $guiPath } else { $null }
    Manifest = $ManifestPath; Failures = $manifest.Failures
} | ConvertTo-Json -Depth 6
exit $(if ($manifest.Failures.Count) { 1 } else { 0 })
