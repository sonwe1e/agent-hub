# 脚本目录

脚本用途与影响不同；先检查脚本和参数，再按本次任务选择。功能代码入口见[开发导航](../docs/developer-guide.md)。

| 用途 | 入口 |
|---|---|
| 样式静态验证 | `verify-css.py`：对照 Git HEAD 检查选择器、字体和样式约束 |
| 桌面样式验证 | `verify-desktop-style.py`：使用桌面运行环境进行检查 |
| 实际客户端或上游验证 | `verify-live-*.py`、`verify-*-live.py`、`verify-subscription-conversations-live.py`；可能读取账号或启动客户端并消耗额度 |
| 模型同步与 OpenCode 验证 | `verify-modelsync-e2e.py`、`verify-opencode.py`；先检查本机路径和读取范围，其中前者可能复制真实配置到临时目录 |
| Windows 安装和系统维护 | `install-desktop.ps1`、`organize-start-menu.ps1`、`fix_cc_switch_created_at.py`；会修改快捷方式、开始菜单或客户端数据 |
| 图标资产生成 | `make_icon.py`、`create-start-menu-icon.py`；会写入图标文件 |
| 内容扫描 | `audit-leak-scan.py`；检查目标和排除规则后再运行，避免扫描本机私有目录 |

`START-MENU.md` 和 `start-menu-rules.json` 属于开始菜单整理脚本。历史运行结果和适用范围见[验证记录](../docs/validation.md)，不能代替针对当前改动的测试。
