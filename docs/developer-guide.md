# 开发者与 Agent 导航

本文回答“改一个功能应该从哪里入手”。先在功能表找到业务逻辑，再检查对应 API、界面和测试；涉及客户端配置、会话或网关的改动还要核对下文的数据边界。使用方法及功能限制见 [README](../README.md)。

## 架构和入口

```text
浏览器：ui/index.html → fetch → server.Handler ─┐
桌面：  ui/index.html → ui/bridge.js → desktop_app.py ─┤
                                              ↓
                                 server.dispatch_api / ROUTES
                                              ↓
                         功能服务、模型同步、观测与网关
```

浏览器管理界面使用本机 HTTP；桌面版通过 WebView2 WebMessage 进程内调用同一个 `server.dispatch_api`，不会为界面额外启动管理 HTTP 服务。可选模型网关是单独的转发服务，和上述界面传输不是一回事。涉及 API 参数、错误或返回格式时，应兼顾两种界面传输。

| 入口或目录 | 职责 |
|---|---|
| `hub.cmd`、`hub.py` | 命令行入口和模型/客户端配置引擎。`hub.cmd ui` 通过启动器打开浏览器工作台。 |
| `desktop.cmd`、`desktop_app.py` | 桌面入口、配置发现、WebView2 桥接、托盘、生命周期和打包后的子进程入口；`desktop.cmd` 可能启动已打包的程序，调试源码可运行 `python desktop_app.py`。 |
| `launch.py`、`server.py` | 浏览器服务启动、HTTP 静态资源和请求处理；业务 API 在 `server.py` 的 `ROUTES` 中统一分发。 |
| `build_desktop.py` | PyInstaller 打包、隐藏导入、UI 和其他资源。源码网关由 `gateway_runtime.py` 启动 `gateway_server.py`；打包版通过桌面入口的 `--gateway-worker` 启动。模型编译也有 `--compile-worker` 入口。 |
| `agent_hub/` | 按功能组织的后端实现，目录职责见下表。根目录保留的对应旧模块是导入兼容壳，新的业务修改应进入这里。 |
| `ui/` | 静态页面；`index.html` 决定脚本和样式加载顺序，`app.js` 含共享状态、导航、API 调用及模型页面，`bridge.js` 负责桌面传输适配；`features/` 按功能放页面资源。 |
| `tests/`、`scripts/` | 单元/集成/浏览器测试及验证脚本；`tests/artifacts/` 保存本地测试输出。按用途查找见[测试目录](../tests/README.md)和[脚本目录](../scripts/README.md)；部分脚本会访问真实客户端或上游，先检查脚本再运行。 |
| `docs/` | 各功能的行为、边界和历史验证说明。历史验证记录代表当时的覆盖范围，不自动证明当前版本。 |

`hub.py`、`server.py`、`sync_transaction.py`、`gateway_runtime.py`、`gateway_server.py`、`codex_subscription.py`、`zcode_subscription.py` 等仍包含实际实现。不要把根目录下所有 Python 文件都当作兼容壳。

## 后端与界面分区

```text
agent_hub/
  agents/inventory.py              Agent 发现、实例和更新
  capabilities/library.py          Skills/指令库与分发事务
  sessions/handoff.py              最近会话、恢复和交接
  sessions/titles.py               原生会话标题与手动标题
  sessions/gateway_titles.py       网关短标题保存
  sessions/summary.py              交接材料摘要
  quota/monitor.py                 额度监控与提醒
  quota/usage.py                   订阅额度查询
  observability/store.py           原生日志解析、观测索引和查询
  gateway/observation.py           网关观测记录读取
  gateway/transport.py             上游连接与代理传输
  gateway/protocol.py              订阅共享所用协议转换
  workspace/services.py            工作区功能服务组合

ui/
  index.html, app.js, bridge.js    页面装配、共享逻辑、桌面桥接
  styles.css, fonts.css, fonts/    全局样式和字体
  features/workbench/              Agent 与观测界面
  features/workspace/              首页、Skills/指令、会话与额度界面
  features/capabilities/           Skills 批量操作
  features/subscriptions/          Codex、ZCode 订阅及额度界面
  features/models/                 模型页面样式；主要逻辑仍在 app.js
```

原来的 `agent_inventory.py`、`capability_library.py`、`session_handoff.py`、`session_titles.py`、`gateway_titles.py`、`handoff_summary.py`、`quota_monitor.py`、`subscription_usage.py`、`observation.py`、`gateway_observation.py`、`upstream_transport.py`、`workbuddy_protocol.py` 和 `workspace_services.py` 保留旧导入名，分别指向上述实现。它们的作用是兼容既有导入和模块身份，不是业务代码的第二份来源。

文件已经按职责放入目录，但并不代表内部逻辑全部拆开：`hub.py`、`server.py` 仍覆盖多个功能；`ui/features/workbench/workbench.js` 同时负责 Agent 和观测，`ui/features/workspace/workspace-features.js` 同时负责首页、技能、会话及额度。需要修改其中某一部分时，可从下表列出的函数定位。脚本共享全局作用域，部分功能会再绑定已有渲染函数；改动顺序或拆分时要检查 `index.html` 和现有调用，不要假设脚本彼此独立。

## 改功能时看哪里

所有管理 API 的最终入口都是 `server.py` 的 `ROUTES` 和 `dispatch_api`；下面列的是功能的主要业务实现和相关端点。测试名称都相对于 `tests/`。

| 要修改的功能 | 后端业务与 API | 界面 | 相关测试 |
|---|---|---|---|
| Agent 安装实例、检测、更新 | `agent_hub/agents/inventory.py` 的 `Inventory`；`server.py` 的 `/api/agents*` | `ui/features/workbench/workbench.js` 的 `renderAgents`、`agentDetailBody`，样式 `ui/features/workbench/daily.css` | `test_inventory.py`、`test_daily_scope.py`、`daily_browser.cjs` |
| 供应商、模型、发现与同步 | `hub.py` 的 `agent_connection`、`agent_model_tiers`、`resolve_agent_model`；模型发现 `discover.py`；`server.py` 的 `/api/providers*`、`/api/modelsync`、`/api/import/*` | `ui/app.js`；`ui/features/models/models.css`；快捷切换还涉及 `ui/features/workbench/workbench.js` | `test_modelsync.py`、`test_api_modelsync.py`、`test_agent_connections.py`、`test_discover.py`、`ui_contract.cjs` |
| 模型切换方案、预览、写入与恢复 | 方案校验和编译在 `switch_plans.py`；文件快照/事务在 `sync_transaction.py`；各客户端写入在 `hub.py` 的 `Hub.push_*`；`server.py` 的 `/api/switch-plans*`、`/api/preview`、`/api/apply`、`/api/restore` | `ui/app.js` 和 `ui/features/workbench/workbench.js` | `test_switch_plans.py`、`test_sync_transaction.py`、`test_api_preview.py`、`test_modelsync.py` |
| Skills 和全局指令：发现、导入、预览、分发、冲突、恢复 | `agent_hub/capabilities/library.py` 的 `CapabilityLibrary`；`server.py` 的 `/api/capabilities*`；详见 [能力库](capability-library.md) | `ui/features/workspace/workspace-features.js` 的 `renderLibrary` 及表单、预览；批量选择和强制覆盖在 `ui/features/capabilities/skills-bulk.js` | `test_capability_library.py`、`test_workspace_integration.py`、`skills_bulk_browser.cjs`、`workspace_browser.cjs` |
| 最近会话、标题、原端继续、跨端交接 | `agent_hub/sessions/handoff.py`；标题在 `agent_hub/sessions/titles.py`、`agent_hub/sessions/gateway_titles.py`；原生日志在 `agent_hub/observability/store.py`；`server.py` 的 `/api/sessions/*` | `ui/features/workspace/workspace-features.js` 的 `renderSessions`、`showSessionTitleForm`、`showWorkResume` 及交接表单 | `test_session_handoff.py`、`test_session_titles.py`、`test_session_title_manual.py`、`test_gateway_titles.py`、`workspace_browser.cjs` |
| 交接材料整理 | `agent_hub/sessions/summary.py`；`server.py` 的 `/api/sessions/summary/*`；请求可能经过网关，参见 [模型摘要](handoff-summary.md) | `ui/features/workspace/workspace-features.js` 的 `showSummarySelect`、`showSummaryPreview` | `test_handoff_summary.py`、`test_workspace_integration.py`、`workspace_browser.cjs` |
| 额度查询、提醒、托盘 | `agent_hub/quota/monitor.py` 的 `QuotaMonitor`、`agent_hub/quota/usage.py`；`agent_hub/workspace/services.py` 组合服务；`server.py` 的 `/api/quota-monitor*`、订阅额度 API；托盘在 `desktop_app.py` | `ui/features/workspace/workspace-features.js` 的 `workQuotaHtml`，及 `ui/features/subscriptions/subscription_monitor.js` | `test_quota_monitor.py`、`test_subscription_usage.py`、`test_workspace_desktop.py`、`workspace_browser.cjs` |
| Codex/ChatGPT、ZCode 订阅共享 | `codex_subscription.py`、`zcode_subscription.py`、`agent_hub/gateway/protocol.py`、`gateway_server.py`；`server.py` 的 `/api/codex-subscription*`、`/api/zcode-subscription*` | `ui/features/subscriptions/codex_subscription.js`、`ui/features/subscriptions/zcode_subscription.js`、`ui/features/subscriptions/subscription_monitor.js` | `test_codex_subscription.py`、`test_zcode_subscription.py`、两个订阅的 `*_browser.cjs`；详见 [订阅共享](subscription-sharing.md) |
| 网关转发、代理、网关观测 | `gateway_runtime.py` 管理子进程，`gateway_server.py` 接收与转发；`agent_hub/gateway/transport.py` 处理上游连接，`agent_hub/gateway/observation.py` 读取网关记录；`server.py` 的 `/api/gateway*` | 设置界面在 `ui/features/workbench/workbench.js`；相关订阅卡片见上一行 | `test_gateway.py`、`test_gateway_transport.py`、`test_proxy_transport.py`、`test_workbuddy_cases.py`；详见 [网关](gateway.md) |
| 原生日志、观测索引、请求和对话详情 | `agent_hub/observability/store.py` 的 `parse_native`、`parse_opencode_database`、`Store.collect/query/session_detail`；`agent_hub/gateway/observation.py` 处理网关来源；`server.py` 的 `/api/observation*` | `ui/features/workbench/workbench.js` 的 `renderObservation`、`conversationDetail` 及 `ui/features/workbench/daily.css` | `test_observation.py`、`test_live_observation_formats.py`、`test_opencode_observation.py`、`daily_browser.cjs`；详见 [观测来源](observation-sources.md) |
| 桌面/浏览器传输与应用生命周期 | `server.py` 的 `dispatch_api` 和 `Handler`；`desktop_app.py` 桥接与托盘；`ui/bridge.js`；`build_desktop.py` | `ui/index.html`、`ui/app.js`、`ui/bridge.js` | `bridge_contract.cjs`、`ui_assets_contract.cjs`、`test_desktop_close.py`、`test_workspace_desktop.py`、`test_launch.py` |

客户端差异分布在多个功能中：实例发现看 `agent_hub/agents/inventory.py`；原生日志解析看 `agent_hub/observability/store.py`；恢复与交接命令看 `agent_hub/sessions/handoff.py`。原生配置写入及重点测试按客户端定位：

| 客户端 | 配置入口 | 重点测试 |
|---|---|---|
| Claude Code | `hub.py` 的 `Hub.push_claude` | `tests/test_agent_connections.py`、`tests/test_modelsync.py`、`tests/test_session_handoff.py` |
| Codex | `hub.py` 的 `Hub.push_codex`、`codex_config.py`；订阅另看 `codex_subscription.py` | `tests/test_codex_reconciliation.py`、`tests/test_codex_official.py`、`tests/test_codex_subscription.py` |
| pi | `hub.py` 的 `Hub.push_pi` | `tests/test_pi_scope.py`、`tests/test_session_titles.py`、`tests/test_session_handoff.py` |
| dsh | `hub.py` 的 `Hub.push_dsh` | `tests/test_dsh_scope.py`、`tests/test_live_observation_formats.py` |
| OpenCode | `hub.py` 的 `Hub.push_opencode`；原生数据库另看 `agent_hub/observability/store.py` 的 `parse_opencode_database` | `tests/test_opencode_observation.py`、`tests/test_modelsync.py` |

增加或调整某个客户端的支持时，要分别核对发现、配置、观测和会话入口，不能只改列表名称。

WorkBuddy 订阅反代已经退役；`workbuddy.py`、`ui/workbuddy.js` 和 [WorkBuddy 说明](workbuddy.md)保留历史实现或记录，不能当作当前可用入口。`agent_hub/gateway/protocol.py` 虽由原 `workbuddy_protocol.py` 演进，仍被当前 Codex 和 ZCode 订阅的协议转换使用；修改它需要覆盖两条订阅线路及共用协议测试。

## 配置、数据和边界

- `providers.json` 和 `providers.json.drop-*` 可能包含真实密钥、账号或个人路径；`providers.example.json` 是仓库示例。不要把实际配置放进提交、日志或测试夹具。
- 观测、能力库、网关及额度等数据通常位于当前配置旁的 `data/`；它可能包含本地会话索引、标题、正文或其他个人信息。客户端自身的配置与原生日志分布在各客户端目录，不属于仓库源代码。
- `state.json`、`backups/`、应用记住的配置路径、`dist/`、`build/`、`tests/artifacts/` 和客户端账号目录均可能是本机状态、备份或测试/构建产物；不要在整理源码时把它们当成可以随意迁移或清理的源码文件。
- 能力库写入客户端文件有预览、冲突检查和事务恢复；模型切换也使用预览与事务。变更这些流程时注意已有数据和恢复路径，参见 [能力库](capability-library.md)和 [配置参考](configuration-reference.md)。
- 标题和观测有脱敏、保留期限与清理水位语义；网关标题只存简短节选，不等同于保存完整对话。变更标题来源时同时检查原生、网关、手动标题优先级及清理行为。

## 验证与构建

以下命令从仓库根目录运行。常规回归主要使用合成记录、模拟服务和临时目录，部分测试会启动测试用子进程或监听本机随机端口；单项测试便于定位问题，全量回归用于覆盖跨模块影响。

```powershell
# 后端全量回归
python -m unittest discover -s tests

# 只验证指定功能，可替换文件名
python -m unittest discover -s tests -p "test_capability_library.py" -v
python -m unittest discover -s tests -p "test_session_titles.py" -v
python -m unittest discover -s tests -p "test_gateway_transport.py" -v

# 无浏览器的界面及桥接契约
node tests/ui_contract.cjs
node tests/ui_async_contract.cjs
node tests/workbench_contract.cjs
node tests/bridge_contract.cjs
node tests/ui_assets_contract.cjs
```

浏览器测试需要 Node.js、Playwright 和可用的浏览器。可用 `PLAYWRIGHT_MODULE` 指向已有 Playwright 模块、`BROWSER_EXECUTABLE` 指向浏览器；若 `python` 不是所用解释器，可设置 `PYTHON`。这些夹具启动隔离的本地测试服务，结果和截图写入 `tests/artifacts/`：

```powershell
node tests/daily_browser.cjs
node tests/workspace_browser.cjs
node tests/skills_bulk_browser.cjs
node tests/codex_subscription_browser.cjs
node tests/zcode_subscription_browser.cjs
```

桌面构建使用 `requirements-desktop.txt`、PyInstaller 和 `python build_desktop.py`；`python build_desktop.py --skip-build` 检查依赖并展示构建命令，不执行打包。打包自检为 `AgentHub.exe --selftest`，需要 GUI 的窗口自检为 `--smoke`。这两种运行可能按桌面配置发现规则读取已记住的配置、程序目录或当前目录中的 `providers.json`；使用隔离配置及隔离运行位置验证，不能只依赖设置 `AGENTHUB_CONFIG`。自检覆盖资源与关键路由，不能替代真实桌面交互或全部业务测试。

实际客户端或上游验证脚本可能读取现有账号、启动客户端并消耗额度；例如 `scripts/verify-live-clients.py`、`scripts/verify-modelsync-e2e.py`。各脚本的范围应以脚本自身和 [验证记录](validation.md)为准。离线回归、浏览器夹具和真实调用的证据要分别记录。
