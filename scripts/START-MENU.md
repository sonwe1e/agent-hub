# 开始菜单快捷方式目录整理

## 目录分类与 Windows 类别视图不是一回事

本次操作整理的是 `Programs` 下的快捷方式文件夹，不是 Windows 11「全部 → 视图：类别」的自动分类卡片。
系统类别视图使用自身的应用识别与分类映射，不按这里的文件夹名称分组；原生界面目前没有手动重新分配类别的入口。
因此，继续整理公共目录也不能修正类别卡片中的误分类，不应为此运行公共整理脚本。

可以切换为「列表」视图检查快捷方式列表；若需要可命名、可手动调整的开始页分组，应使用「已固定」区域的文件夹。
「已固定」文件夹需要单独创建，本次脚本没有修改它们。

## 当前已完成

个人开始菜单目录：

`%USERPROFILE%\AppData\Roaming\Microsoft\Windows\Start Menu\Programs`

- 将 32 个应用快捷方式按用途归类。
- 将 6 个重复入口移到备份目录，不永久删除。
- 新增 `AI 与模型\Agent Hub.lnk`，使用专用图标，通过 `pythonw.exe launch.py gui` 无控制台启动。
- Windows 默认目录、管理工具和 Startup 的 21 个原始文件校验一致，未改变。
- 公共菜单的 119 个文件仍在原始位置；尚未应用公共部分的分类。

个人快捷方式目录分类：`AI 与模型`、`开发与终端`、`办公与沟通`、`网络与远程`、`媒体与设计`、`游戏与娱乐`、`其他应用`。
不确定用途的 Orca 暂放在「其他应用」，不擅自判断；公共菜单应用后还会包含「系统与工具」。
卸载、帮助和文档入口放在对应分类的「维护与帮助」子目录，复杂开发工具保留原有工具分组。

开始菜单「所有应用」和搜索会自动发现快捷方式；若没有立即显示，稍等后重新打开开始菜单即可。
此操作没有强制修改 Windows 的固定区域，也没有重启 Explorer。

## 继续整理公共菜单

公共目录为 `C:\ProgramData\Microsoft\Windows\Start Menu\Programs`。
当前账户没有管理员权限，因此没有改权限、没有把公共入口搬进个人目录，也没有擅自提权。

在**管理员 PowerShell 7**中运行：

```powershell
pwsh -NoProfile -File "G:\workspaces\agent-hub\scripts\organize-start-menu.ps1" -Scope Common -Preview
pwsh -NoProfile -File "G:\workspaces\agent-hub\scripts\organize-start-menu.ps1" -Scope Common
```

当前公共计划：归类 82 个入口，归档 3 个重复入口。操作前会另建公共菜单完整备份与撤回记录。
公共与个人目录使用一致的文件夹名称，便于维护；这不代表 Windows 的自动类别视图会采用这些名称。软件本体不受影响。

## 备份与撤回本次个人整理

完整原始个人文件和重复项：

`G:\workspaces\agent-hub\backups\start-menu\20260930-170735-812-1f073c\`

本次 GUI 创建重试记录已合并到该目录中的 `manifest.json`，一次撤回即可恢复原始布局并移除新增的 GUI 快捷方式。
重试的审计信息仍保留在备份目录，不重复执行撤回。

```powershell
pwsh -NoProfile -File "G:\workspaces\agent-hub\scripts\organize-start-menu.ps1" -RestoreManifest "G:\workspaces\agent-hub\backups\start-menu\20260930-170735-812-1f073c\manifest.json"
```

撤回前校验快捷方式内容；若整理后被修改或原始位置已新增同名文件，会报告冲突并保留，不直接覆盖。
公共整理需要用对应的新 `manifest.json` 撤回，并同样需要管理员权限。

## 规则与验证

- 分类规则：`scripts/start-menu-rules.json`。
- 原始清单：`backups/start-menu/inventory-before.json`。
- 整理脚本按这份清单运行，不扫描和移动未知的新安装入口；检测到已有快捷方式更新时会跳过并报告。
- 仅对启动目标、参数和工作目录完全一致的 `.lnk` 去重，保留公共入口优先；Shell / 商店应用的空目标快捷方式不参与去重。
- 不处理 Windows 商店虚拟应用列表和开始菜单固定区域。
- 4 项临时菜单回归覆盖预览无改动、整理 / 撤回无损、重复执行、保留后续修改。测试不接触真实菜单。

```powershell
python -m unittest discover -s tests -p "test_start_menu.py" -v
```
