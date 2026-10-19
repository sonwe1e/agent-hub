# Agent Hub

本机使用的轻量 Agent 管理与观测应用，只维护 **Claude Code、Codex、pi、dsh**。CLI 与桌面共用配置时归在同一 Agent 下，但每个安装实例的路径、版本与更新渠道独立显示。

## 启动

需要 Python 3.11+（建议 3.12）及已有的 `providers.json`。

```powershell
cd G:\Workspaces\agent-hub
python -m pip install -r requirements.txt
.\hub.cmd gui
# 浏览器入口
.\hub.cmd ui
# 前台服务，默认仅绑定本机
python server.py --no-open
```

`zstandard` 用于 dsh 压缩日志及网关压缩观测。缺少它时，配置管理仍可运行，采集设置中会明确列出 dsh 读取失败的原因。默认地址是 `http://127.0.0.1:8787/`；启动器遇到旧服务占用端口时复用新版服务或选择后续端口，不终止其他进程。当前工作台版本为 6。

## 日常操作

- **Agent**：每行一个 Agent。安装实例下拉框区分 CLI、桌面版、PATH 默认入口和可确认的运行中进程。详情列出绝对路径、安装来源、配置位置与更新诊断。
- **模型切换**：行内直接选该 Agent 连接上的供应商与模型。先生成预览，再确认写入；在设置的「方案与高级同步」保存常用组合、单独指定 Claude 档位、选择 Codex 官方/第三方线路，或恢复上次配置。
- **观测**：概览、请求、对话、上下文共用一套索引与筛选。点击概览摘要进入请求，调用详情可进入原生对话及其上下文变化。时间范围在定位过程中保持一致。
- **设置**：供应商与按 Agent 覆盖的连接、模型库、切换方案与本机采集。常见操作无需编辑原始配置。

配置选择描述的是 Hub 当前意图，不表示正在运行的旧会话已经换模。实际模型与用量只来自观测记录。单 Agent 切换不会改其他 Agent 的 Hub 选择；方案中的“保留当前值”不参与该目标的原生写入。Claude 档位可分别保留、跟随或指定；Codex 未指定推理强度时保留客户端当前值。

## 更新的边界

检查更新与安装更新是两个动作。npm 实例按所在包、发布频道与确切 prefix 检查、更新；Claude 原生安装使用其发布频道与自带更新器。更新结束后重新运行同一入口的版本检测，并核对确切目标版本；命令成功而版本不符会报告验证失败。失败时不会自动改用另一种全局安装方式。

Codex 桌面维护的运行时、Microsoft Store 应用、dsh 桌面及其附带 CLI 使用原安装渠道。dsh 桌面可检查本机 updater feed；执行桌面更新仍需应用内操作。未知来源不会显示为可自动更新。Electron 的运行时版本不会被当作桌面应用版本。

## 观测与隐私

**WorkBuddy AI 海外版反代**已接入 Hub 自有网关。在「设置 → WorkBuddy AI（海外版）」中打开官方登录页，完成登录后自动读取账号模型，再在 Agent 行内选择、预览并应用。支持 Claude Code、Codex、pi、dsh，包含三协议转换、流式与非流式结果、工具调用和 Token 自动刷新；OAuth 凭据单独保存在被忽略的本机数据目录。使用时需保持 Hub 运行。2026-10-19 已完成真实浏览器授权，四个客户端使用 `step-5-preview` 完成随机文件工具往返，Codex app-server 文本流和 Responses 命名空间自定义工具也通过。实际积分扣费、临期刷新和企业账号尚未实测；操作方法与证据范围见 [WorkBuddy 海外版说明](docs/workbuddy.md)。

采用 Magpie 的本机观测默认：自动读取已有原生用量，用量索引默认不自动过期；额外正文留存默认关闭。已有原生对话与工具结果在打开详情时直接读取原文件，无需先录制一份副本。额外副本开启后最多保留 7 天、256 MiB；这两个限制独立于用量索引期限。采集随本地服务启动/退出，每 30 秒扫描变化文件，支持停止、修改用量期限和清空 Hub 索引。停止自动采集后，主动打开已索引对话仍可读取其原文件。

来源为 Claude Code 的 projects JSONL、Codex sessions/archived_sessions rollout（含桌面版）、pi sessions JSONL，以及 dsh 最新代 JSONL/zstd。使用原生会话/响应/消息标识关联，避免多块回复、重复用量事件、迁移代文件和继承 seed 重复计数。没有原生会话身份的调用保留未关联。

`data/observability.sqlite3` 与 `data/observation-settings.json` 是本机私有数据，已被 Git 忽略。正文开关只控制额外副本，关闭会清除副本；不妨碍按需读取客户端已有正文。额外正文按原生事件时间保留，并按正文记录的 UTF-8 字节量淘汰最旧记录至 256 MiB 内（SQLite 还包含用量索引与存储开销）。清空会同时停止采集并设水位，防止旧事件立即重新导入或从详情绕过水位显示；不删除客户端原文件。已知密钥、认证字段会脱敏，任意私人业务文字无法保证自动识别。

费用是 USD 参考估算，或客户端原生记录的参考成本，**不是供应商实际扣费**；未知价格不会显示为免费。额度是带时间的原生历史快照，**不是实时余额**。原生日志没有完整网络重试、首字等待或完整请求构成时，页面明确显示未知或未采集，不生成健康分，不用配置倒推实际调用。

网关已改为仓库内的 Python 实现，不下载、启动或依赖 Magpie 引擎；Magpie 仅作能力与默认采集方式的参考。设置通过现有事务预览接入客户端，自定义连接按 Agent 分开导出，Codex 保留原生登录，pi 内置 OAuth 与 dsh 内置账号保留直连。网关与原生记录按响应 ID 合并，提供线路、流式完成状态、网络计时、Token 和标注方法的上下文构成。关闭额外正文通过自有进程内录制代次失效在途写入并清理副本。运行与隐私边界见 [网关说明](docs/gateway.md)。

2026-10-17 自有网关已用隔离配置完成 Claude、Codex、pi、dsh 的真实调用和随机文件工具验证。Codex 保留 ChatGPT 登录，CLI 与 app-server 完整工具轮次通过，后者有 37 个文本增量；dsh 原生 DeepSeek 账号通过。pi 自定义 API 线路通过，但最新原生 OAuth 刷新被服务端以 invalid_grant 拒绝，未通过；Anthropic OAuth 因无本机缓存未验证。真实记录经正式采集器导入并核对四页一致性；不以历史 Magpie 引擎结果替代当前验收。

## 验证与文档

```powershell
python -m unittest discover -s tests
node tests/ui_contract.cjs
node tests/ui_async_contract.cjs
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
