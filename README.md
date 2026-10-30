# Agent Hub

本机使用的轻量 Agent 管理与观测应用，只维护 **Claude Code、Codex、pi、dsh、OpenCode**。CLI 与桌面共用配置时归在同一 Agent 下，但每个安装实例的路径、版本与更新渠道独立显示。

开发者与 Agent 可先看[开发导航](docs/developer-guide.md)，按功能查找后端、API、界面和测试入口。

## 启动

日常使用：双击桌面上的 **Agent Hub**，或从开始菜单搜索 **Agent Hub**。也可直接打开 `dist\AgentHub.exe`。这些入口会打开独立应用窗口，无需打开浏览器、输入地址或先启动服务；打包版不依赖系统 Python。

首次设置桌面和开始菜单入口（先完成打包）：

```powershell
.\scripts\install-desktop.ps1
```

已有的开始菜单分类会保留，原快捷方式改为直达桌面程序。窗口右上角关闭后留在系统托盘；再次点击快捷方式即可恢复原窗口，托盘菜单的「退出」会结束应用。

从源码运行需要 Python 3.11+（建议 3.12）及已有的 `providers.json`：

```powershell
cd G:\Workspaces\agent-hub
python -m pip install -r requirements-desktop.txt

# 桌面版（默认）：独立 WebView2 窗口，界面无需 HTTP 端口
.\desktop.cmd
# 或双击 Launch GUI.vbs；hub.cmd gui / hub.cmd desktop 也打开桌面版
# 直接运行源码（跳过现有 exe）：python desktop_app.py

# 打包当前源码并创建快捷方式
python -m pip install pyinstaller
python build_desktop.py
.\scripts\install-desktop.ps1

# 浏览器版：前台服务，默认仅绑定本机
.\hub.cmd ui
python server.py --no-open
```

`zstandard` 用于 dsh 压缩日志及网关压缩观测。缺少它时，配置管理仍可运行，采集设置中会明确列出 dsh 读取失败的原因。浏览器版默认地址是 `http://127.0.0.1:8787/`；启动器遇到旧服务占用端口时复用新版服务或选择后续端口，不终止其他进程。当前工作台版本为 6。

### 桌面版与浏览器版的差别

两者共用同一份业务逻辑与同一套 ROUTES 路由表，差别只在传输层：

| | 浏览器版 | 桌面版 |
|---|---|---|
| 进程 | Python 服务 + Edge `--app` 窗口 | 单进程 |
| API 传输 | HTTP（`127.0.0.1:8787`） | WebView2 **WebMessage**，进程内直调 `server.dispatch_api` |
| 界面端口 | 有（可能漂移） | **无**（可选 API 网关另行监听，不用于打开界面） |
| 关闭窗口 | 服务继续跑 | 缩到系统托盘继续采集，托盘「退出」才结束 |
| 打包 | — | `python build_desktop.py` → `dist/AgentHub.exe`（含运行时与应用图标） |

桌面界面不会启动管理用的 HTTP 服务。UI 里的 `fetch('/api/...')` 由 `ui/bridge.js` 改写为一次 WebMessage；浏览器模式下 `bridge.js` 检测不到 `window.pywebview` 会原样放行，所以两种模式的界面代码完全相同。可选模型网关仍按自身设置运行，与打开窗口的方式独立。自检：`python desktop_app.py --smoke`。

### 配置发现（exe 不需要手动放 providers.json）

`providers.json` 含明文密钥，**不打包进 exe**。启动时按以下优先级自动查找：

1. 上次选择并记住的位置（`%LOCALAPPDATA%\AgentHub\config-path.txt`）
2. exe 同级目录（便携安装）
3. exe 的上级目录（exe 常被放在 `<项目>\dist\` 下，因此直接可用）
4. 当前工作目录

全都找不到时，窗口会显示一张**首次配置引导页**，用原生文件对话框让你选一次 `providers.json`，选中后记住路径并自动重启。观测数据、备份与网关配置都跟在配置旁边（`<配置目录>/data/`），不会散落到别处。

网关运行时（`gateway_runtime` / `gateway_server` / `gateway_observation`）与业务模块一起打包，无需单独安装。

确定性自检（不起窗口，不依赖 WebView2，打包后立刻可跑）：

```powershell
dist\AgentHub.exe --selftest
```

输出包含 `config.resolved`（实际解析到的配置）、`hubConfig`（注入后的路径）、路由总数、关键 API 的分发结果与 `gateway.importable`。

## 日常操作

- **Agent**：每行一个 Agent。安装实例下拉框区分 CLI、桌面版、PATH 默认入口和可确认的运行中进程。详情列出绝对路径、安装来源、配置位置与更新诊断。
- **技能与指令**：发现已有全局内容或手动选择主来源，指定分发目标并预览后首次发布；后续可按项选择手动或自动同步。目标独立修改会提示冲突；全局指令可按客户端配置明确的文字替换。项目规则与 MCP 不由此功能管理。详见[能力库](docs/capability-library.md)。
- **继续工作**：按项目查找已索引的原生会话，检查后启动对应客户端；跨端交接先预览和编辑，可选择模型整理材料。默认只准备、不会自动发送；立即开始仅对已核实入口的客户端开放。详见[会话恢复与交接](docs/session-handoff.md)和[模型摘要](docs/handoff-summary.md)。
- **模型切换**：打开 Agent 详情，选择该连接上的供应商与模型。先生成预览，再确认写入；在「模型与方案」保存常用组合、单独指定 Claude 档位、选择 Codex 官方/第三方线路，或恢复上次配置。切换预览不会保存模型选择；确认时 Hub 选择与客户端文件一起进入事务，恢复时也一起还原。
- **观测**：概览、请求、对话、上下文共用一套索引与筛选。点击概览摘要进入请求，调用详情可进入原生对话及其上下文变化。时间范围在定位过程中保持一致。
- **设置**：供应商与按 Agent 覆盖的连接、模型库、切换方案与本机采集。常见操作无需编辑原始配置。

首页显示最近工作、能力库状态和额度摘要；托盘可进入最近会话、选择已保存的方案及查看额度。额度提醒默认在 Hub 运行时定期查询已接入的 Codex／ChatGPT 和 ZCode 额度，不支持查询的账号显示未知；实际限流提醒受网关或原生日志的观测范围限制。额度提醒不会切换账号、模型或线路。[额度提醒说明](docs/quota-monitor.md)

配置选择描述的是 Hub 当前意图，不表示正在运行的旧会话已经换模。实际模型与用量只来自观测记录。单 Agent 切换不会改其他 Agent 的 Hub 选择；方案中的“保留当前值”不参与该目标的原生写入。Claude 档位可分别保留、跟随或指定；Codex 未指定推理强度时保留客户端当前值。

### OpenCode（2026-10-20 接入）

Hub 把 OpenCode 当第五个 Agent：检测 `@opencode/cli` 安装（`opencode` 命令），把供应商与模型写进 `~/.config/opencode/opencode.json`，支持模型同步与切换方案。

旧版配置中未登记 OpenCode 时，它默认不参与同步。到「模型与方案 → 高级」显式启用后，即可预览和应用；启用操作本身不会改写 OpenCode 配置。切换预览和恢复包括 Hub 模型选择与 OpenCode 配置。

- **写什么**：只接管 `provider` 段与顶层 `model`。你的 `mcp` / `skills` / `agent` / `plugin` / `lsp` 配置原样保留。
- **provider 段是「完全接管」**：Hub 是供应商清单的唯一来源，`opencode.json` 里手写的 `provider` 条目会被 Hub 的清单替换。这是刻意的选择——两套来源会互相漂移。
- **写进去的是 Hub 的密钥**：推送后 OpenCode 用的是 `providers.json` 里那份 `apiKey`。你在 OpenCode 里单独改过的 key 不会自动同步回 Hub；要让 OpenCode 用某个 key，请在 Hub 里配同一个供应商。
- **内置供应商不受影响**：`opencode/`、`opencode-go/`、`deepseek/` 这些来自 OpenCode 客户端本身与它的 auth 凭证库，不由 `opencode.json` 控制，因此仍会出现在 `opencode models` 里。想彻底只剩 Hub 的模型，需要另配 `experimental.policies`，当前未启用。
- **协议**：四种协议都支持。`openai-completions` / `openai-chat-completions` → `@ai-sdk/openai-compatible`，`openai-responses` → `@ai-sdk/openai`，`anthropic-messages` → `@ai-sdk/anthropic`。
- **生效时机**：OpenCode 有常驻后台服务并缓存配置。写入后执行 `opencode reload`，或 `opencode service restart`，再跑 `opencode models` 确认。
- **限额字段的硬规则**：OpenCode v2 要求 `limit` 同时含 `context` 与 `output`，缺任一项它会**整条丢弃该模型**。所以模型缺 `maxTokens` 时 Hub 不写 `limit`，让客户端用默认值——宁可少一个提示，也不让模型消失。

验证：`python scripts/verify-opencode.py`（映射规则 + 真实 `opencode models` 识别 + `push_opencode` 端到端写盘）。

## 更新的边界

检查更新与安装更新是两个动作。npm 实例按所在包、发布频道与确切 prefix 检查、更新；Claude 原生安装使用其发布频道与自带更新器。更新结束后重新运行同一入口的版本检测，并核对确切目标版本；命令成功而版本不符会报告验证失败。失败时不会自动改用另一种全局安装方式。

Codex 桌面维护的运行时、Microsoft Store 应用、dsh 桌面及其附带 CLI 使用原安装渠道。dsh 桌面可检查本机 updater feed；执行桌面更新仍需应用内操作。未知来源不会显示为可自动更新。Electron 的运行时版本不会被当作桌面应用版本。

## 观测与隐私

**WorkBuddy AI 海外版反代已取消**。原有 provider 配置、账号文件和历史记录保留，但这条线路不再可用。客户端如果仍选用该线路，需要在 Hub 中选择其他模型并预览应用；Codex／ChatGPT 与 ZCode 订阅共享仍可使用。停用前的说明与验证保留在 [WorkBuddy 历史记录](docs/workbuddy.md)。

**Codex／ChatGPT 与 ZCode 订阅共享**：在「设置」读取本机已登录账号和模型，在 Agent 详情选择后通过预览应用。卡片显示本机线路的最近调用、成功与失败；点击「查询额度」可读取带来源和时间的上游快照。Codex 登录只读、不自动刷新；ZCode 支持当前选中的个人、团队 Coding Plan 和 Start Plan，账号或套餐切换后需刷新接入。Codex 使用 ZCode 时需要 `web_search = "disabled"`。Claude Code、Codex、pi、dsh 和 OpenCode 已用 Codex 与 ZCode 个人订阅完成真实工具往返；ZCode 团队版和 Start Plan 暂只有模拟验证。登录来源、协议边界及使用步骤见 [订阅共享说明](docs/subscription-sharing.md)。

采用 Magpie 的本机观测默认：自动读取已有原生用量，用量索引默认不自动过期；额外正文留存默认关闭。已有原生对话与工具结果在打开详情时直接读取原文件，无需先录制一份副本。根据用户选择，观测开启时 Hub 网关会另外提取并在本机保存经过脱敏的简短会话标题；用户也可手动命名。短标题可能含个人文字，与正文副本开关独立，遵循观测保留期限并在清空观测记录时删除。额外正文副本开启后最多保留 7 天、256 MiB；这两个限制独立于用量索引期限。采集随本地服务启动/退出，每 30 秒扫描变化文件，支持停止、修改用量期限和清空 Hub 索引。停止自动采集后，主动打开已索引对话仍可读取其原文件。

来源为 Claude Code 的 projects JSONL、Codex sessions/archived_sessions rollout（含桌面版）、pi sessions JSONL、dsh 最新代 JSONL/zstd，以及 OpenCode v2 的 `opencode.db` 会话/消息投影。OpenCode 数据库以只读模式读取，支持运行中的 WAL，不读取凭据表；旧版数据库格式暂不支持。OpenCode 的 output 和 reasoning 相加作为输出总量，仅使用唯一的原生 `providerState.responseId` 与网关记录合并；没有可靠 ID 时，原生与网关记录可能分别计数。其他来源使用原生会话/响应/消息标识关联；没有原生会话身份的调用保留未关联。

`data/observability.sqlite3` 与 `data/observation-settings.json` 是本机私有数据，已被 Git 忽略。正文开关只控制额外副本，关闭会清除副本；不妨碍按需读取客户端已有正文。额外正文按原生事件时间保留，并按正文记录的 UTF-8 字节量淘汰最旧记录至 256 MiB 内（SQLite 还包含用量索引与存储开销）。清空会同时停止采集并设水位，防止旧事件立即重新导入或从详情绕过水位显示；不删除客户端原文件。已知密钥、认证字段会脱敏，任意私人业务文字无法保证自动识别。

费用是 USD 参考估算，或客户端原生记录的参考成本，**不是供应商实际扣费**；未知价格不会显示为免费。模型详情可填写输入、输出及缓存的 USD/百万 Token 参考单价；空白代表未知，0 代表明确的零单价。已有索引不会因编辑单价立即重新计价。观测页面的额度是带时间的原生历史快照；订阅卡片另外支持手动查询上游当时的额度，均不是实时账单。原生日志没有完整网络重试、首字等待或完整请求构成时，页面明确显示未知或未采集，不生成健康分，不用配置倒推实际调用。

拉取或手动添加模型时，未提供的窗口、最大输出、输入类型和推理能力保留为未知；部分客户端要求这些字段，同步时仍会采用客户端适配默认值。上游转发以及订阅认证、目录与推理共用代理选择，支持 HTTP、SOCKS5 和 SOCKS5H；无效代理不会静默退回直连，详见[网关说明](docs/gateway.md)。

网关已改为仓库内的 Python 实现，不下载、启动或依赖 Magpie 引擎；Magpie 仅作能力与默认采集方式的参考。设置通过现有事务预览接入客户端，自定义连接按 Agent 分开导出，Codex 保留原生登录，pi 内置 OAuth 与 dsh 内置账号保留直连。网关与原生记录按响应 ID 合并，提供线路、流式完成状态、网络计时、Token 和标注方法的上下文构成。关闭额外正文通过自有进程内录制代次失效在途写入并清理副本。运行与隐私边界见 [网关说明](docs/gateway.md)。

2026-10-17 自有网关已用隔离配置完成 Claude、Codex、pi、dsh 的真实调用和随机文件工具验证。Codex 保留 ChatGPT 登录，CLI 与 app-server 完整工具轮次通过，后者有 37 个文本增量；dsh 原生 DeepSeek 账号通过。pi 自定义 API 线路通过，但最新原生 OAuth 刷新被服务端以 invalid_grant 拒绝，未通过；Anthropic OAuth 因无本机缓存未验证。真实记录经正式采集器导入并核对四页一致性；不以历史 Magpie 引擎结果替代当前验收。

## 验证与文档

```powershell
python -m unittest discover -s tests
node tests/ui_contract.cjs
node tests/ui_async_contract.cjs
node tests/workbench_contract.cjs
node tests/bridge_contract.cjs
# 环境中提供 playwright（可使用 PLAYWRIGHT_MODULE 指向已有模块）
node tests/daily_browser.cjs
node tests/browser.cjs
# 可选：会使用现有线路/账号，产生真实调用；配置与凭据副本在隔离目录运行
python scripts/verify-live-clients.py --agent claude --agent pi --agent dsh
python scripts/verify-live-clients.py --agent codex --scenario native
python scripts/verify-live-clients.py --agent pi --agent dsh --scenario native
python scripts/verify-live-clients.py --agent codex --scenario native --codex-transport app-server --stream-only
# 离线核对已有真实捕获；--out 需指定一个尚不存在的新目录
python scripts/verify-live-observation.py --out tests/artifacts/live-review-new
# 用真实记录索引做浏览器检查（设置 LIVE_OBSERVATION_REPORT 指向上述目录）
node tests/live_browser.cjs
# 实际经过网关；额外正文可选测试会在结束后关闭并清空副本
python scripts/verify-live-clients.py --agent claude --agent codex --agent pi --agent dsh --gateway
python scripts/verify-live-clients.py --agent pi --gateway --gateway-content
```

常规回归使用临时 HOME 与脱敏格式夹具，截图在 `tests/artifacts/daily/`。真实客户端捕获另存于 Git 忽略的 `tests/artifacts/live-clients/`；本轮真实记录的四页报告、126 项核对结果和截图位于 `tests/artifacts/live-report-20261017/`。真实失败定位场景是 pi 的 ChatGPT 账号拒绝旧模型；详情保留原生错误，对话和上下文使用相同会话 ID。失败中的默认全零 Token 标为未确认，不当作免费。此前的 429/重试/压缩夹具仍用于离线回归，与真实调用证据分开。

- [信息项—来源—采集条件—精度与缺口](docs/observation-sources.md)
- [验收结果与未验证能力](docs/validation.md)
- [配置引擎详细参考与历史迁移说明](docs/configuration-reference.md)

现有模型库、连接、同步与事务引擎继续使用；历史配置和数据保留。备用线路继续只做显式白名单与能力/费用诊断，不执行自动切线，不放宽用户约束。

最新自有网关验证位于 `tests/artifacts/hub-gateway-20261017/`：16 条真实调用、7 个对话、10 条按响应 ID 合并、1 条辅助调用未关联，54 项一致性核对和 6 组浏览器操作。13 条成功、3 条失败（2 条 pi OAuth、1 条 Codex 上游连接失败），费用均无法计价而非免费。`gateway-final-20261017` 与上面 `live-report` 均为此前阶段的历史验收。
