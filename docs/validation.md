# 验收记录

## 2026-10-19：WorkBuddy AI 海外版适配

工作台版本 **6**，网关 **agent-hub / hub-2**。新增单账号浏览器登录、企业身份、串行 Token 刷新、动态 CLI 模型目录和三种协议转换，包括函数与 Codex custom 文本工具、流式 / 非流式结果、Responses 续接、取消与现有用量账本。

`tests/test_workbuddy.py` 使用临时目录与本机 HTTP 模拟上游检查上述路径，以及四个 Agent 的真实配置预览 / 编译 / 应用（全局网关开启和关闭两种情况）。`tests/workbuddy_browser.cjs` 的五组浏览器验收覆盖登录、取消、账号与模型接入、四个 Agent 选择器、刷新、窄屏和退出账号；现有日常页面与配置管理浏览器回归也通过。夹具覆盖与下面的真实调用分别记录。详细范围与操作见 [WorkBuddy AI](workbuddy.md)。

### 登录后的真实调用与工具往返（14:46–15:00，本机时间）

真实海外个人账号浏览器授权完成，调用经过本机工作台 `127.0.0.1:8787` 与网关 `127.0.0.1:8789`。早期协议探针用当时存在的 `default-model`；目录调整后，客户端及命名空间工具专项改用保留的 `step-5-preview`，没有重新接入或恢复已移除的模型。

| 场景 | 模型 | 实际结果 |
| --- | --- | --- |
| Chat Completions / Anthropic Messages / Responses | `default-model` | 各完成一次工具请求、执行隔离随机文件读取、结果回传与最终随机值核对，共 6 次完整响应；均检查流完成标记 |
| Claude Code | `step-5-preview` | 原生 Read 工具往返通过，16 个内容增量；14.6 秒 |
| Codex CLI exec | `step-5-preview` | 原生命令工具读取及最终回复通过；22.2 秒。exec 输出无文本 delta，未声称它验证了增量文本流 |
| pi | `step-5-preview` | 原生 read 往返通过，11 个文本增量；9.9 秒。此前 `default-model` 也通过 |
| dsh CLI | `step-5-preview` | 原生 read 往返通过，最终随机值正确；14.5 秒。JSON 输出只提供 1 个 text 事件，不能据此断言逐片文本增量 |
| Codex app-server | `step-5-preview` | 纯文本专项正常结束，5 个文本 delta；13.2 秒。该场景没有执行工具，工具证据来自上面的 CLI exec |
| Responses namespace custom 工具 | `step-5-preview` | `functions.read_probe` 经字符串输入转换、客户端执行、`custom_tool_call_output` 与 `previous_response_id` 续接后返回正确随机值 |

真实探测修复了三处兼容问题：Codex 新的 `namespace` 工具定义 / 本地身份还原；WorkBuddy 对固定客户端说明句的拒绝（采用 Magpie 的兼容写法，工具结果保留原文）；dsh 空 `refs: {}` 凭据文件被重复追加 `refs`。隔离 dsh headless profile 另外显式启用本地凭据服务，避免 profile 本身未加载服务造成的假失败。初期失败原样留存，没有改标为成功。

离线报告 `tests/artifacts/workbuddy/live-report-20261019/report.json` 核对选定场景的 **20 条网关记录：19 条完整响应、1 条客户端取消的 dsh 并发请求（499）**。7 条已有原生日志的响应通过精确 response ID 合并；第二次导入无重复。pi 当前 JSON stdout 的两个 response ID 也与账本一致。dsh 本次没有原生 JSONL / session header，工具往返由 stdout 证实；同时段网关记录仅作为运行期间观测，未据此猜测会话归属。当天另有 6 条早期兼容失败账本记录，不计入上述成功数。

本轮所有客户端场景的原配置 / 认证指纹一致，临时凭据清理完成、无残留文件；未复制原生账号或 WorkBuddy OAuth。正式采集正文与网关正文开关均保持关闭。对 44 个探针 / 索引文件检查 10 个当前已知凭据值及 JWT 特征，无命中；此检查不代表任意敏感文本识别。19 条成功响应有上游用量证据；价格和实际积分扣费未知，不当作免费。

最终 Python 全量回归 **456 项：455 通过，1 项既有跳过**；WorkBuddy 专项 25 项及探针隔离 5 项通过，语法与 diff 空白检查通过。企业身份 / 临期刷新仍只有夹具覆盖，真实图片、全部模型与 Responses `/compact` 不在已通过范围内；`/compact` 当前明确不支持。

## 2026-10-17：Hub 自有网关，替换 Magpie 引擎后的验收

工作台版本 **5**，网关 **agent-hub / hub-1**。请求转发、三种原协议的流解析、计时、用量、上下文结构估算、内容代次及保留清理由仓库内 Python 实现；不下载、启动或依赖 Magpie。此前固定版本引擎的代码路径已移除，Hub 数据目录中旧的下载程序已删除。用户独立 Magpie 应用、原配置与历史保留，日常接入稳定之前不卸载。

### 本轮真实客户端结果

| 场景 | 本轮结果 | 证据 |
|---|---|---|
| Claude 2.1.299 API → Hub 网关 | 通过 | 2 次响应、1 个成功文件工具、5 个文本增量，最终随机文件值匹配 |
| pi 1.3.0 自定义 API → Hub 网关 | 通过 | 2 次响应、1 个成功文件工具、8 个文本增量；正文文件 1→0，切换采集无需重启 |
| dsh 0.2.0-rc.2 桌面附带 CLI → Hub 网关 | 通过 | 2 个主响应加 1 个无会话辅助响应、1 个成功文件工具、1 个文本事件 |
| Codex 0.163.0 CLI 原生 ChatGPT → Hub 网关 | 通过 | 2 次响应、文件工具、只读沙箱和最终随机值匹配 |
| Codex 0.163.0 app-server 原生 ChatGPT → Hub 网关 | 通过 | 完整工具轮次、37 个文本增量；2 个明确完成响应，中间另有 1 条上游连接失败被保留 |
| dsh 原生 DeepSeek 账号直连 | 通过 | 文件工具、2 个文本事件、最终随机值匹配 |
| pi 原生 OpenAI OAuth 直连 | **未通过** | 本轮先 fetch failed，再收到刷新接口 HTTP 400 invalid_grant；一次独立复测同样 invalid_grant，已停止重试 |
| Claude 原生 OAuth | **未验证** | 本机没有原生 OAuth 缓存；API 成功不能代替 OAuth 验证 |

所有测试使用隔离配置，真实配置/凭据文件前后指纹不变，认证副本清理完成。原文件未改写不代表一次 OAuth 刷新绝无远端状态影响：轮换型 refresh token 可能因隔离副本刷新而失效。本轮 pi 的服务端拒绝不能仅凭本地日志确定起因；测试工具现会在 pi OAuth 过期、临近过期或过期时间未知时跳过调用，避免隔离副本自动轮换真实账号的 refresh token。pi 原生模式需先在原客户端恢复有效登录，当前不宣称已通过。

### 统一数据与真实页面

最新报告 `tests/artifacts/hub-gateway-20261017/`。7 组本轮捕获，经正式采集器导入后有 **16 条请求、7 个对话、10 条精确 ID 合并、2 条仅网关记录、1 条未关联**。其中 **13 条成功、3 条失败**（pi OAuth 的 2 条、Codex 上游连接失败的 1 条），没有未确认状态。16 条均缺少适用价格，费用未知而非免费。11 条具有实测首内容等待，7 条可可靠计算速度。

这里请求指进入网关的每个 HTTP 推理调用；当前网关不自动重试。客户端重新发出的请求如果没有可靠关联 ID，不能确认是同一个逻辑请求的重试，保留独立记录。Codex 整轮成功与中间一次网络失败分别呈现，不用整轮结果覆盖实际发生的独立失败；概览不是「失败工作任务数」。历史 Magpie 的显式 handed_on/tries 仍按其明确关系聚合。

- **54 项数据核对通过**：概览/分布/请求总数一致，对话合计加未关联等于概览，同一上下文点与请求 ID/输入/缓存一致，重读两源不增加调用。
- **6 组真实记录浏览器操作通过，0 JS 错误**：失败摘要→请求→原生对话→上下文，网关计时/估算构成→对应工具对话，dsh 原生工具证据，1440/760 宽度，启动自有 Python 网关，现有事务接入/恢复直连。原生 OAuth 错误中的 JSON 转义也在详情中核对。
- **9 项网关传输与保留回归通过**：实际套接字验证首块先于流结束、工具 JSON 原样、限流不偷偷重试、截断为中断、原生认证仅内存转发、无 Content-Type 的 SSE、正文代次保护和无外部二进制启动/退出；另验证多行 SSE 与正文期限/容量删除行为。
- **Python 全量 429 项，428 通过、1 项既有跳过**；前端 26 项契约、2 组异步、5 组日常浏览器通过。结果在 `tests/artifacts/hub-gateway-python.txt` 与报告的 `browser-results.json`。
- 日常服务 `127.0.0.1:8788` 为版本 5，`127.0.0.1:8789` 为自有 Python 网关。真实客户端接入配置尚未应用。实际运行页面截图为 `tests/artifacts/hub-gateway-actual-settings.png`、`hub-gateway-actual-settings-narrow.png`；状态证据为 `hub-gateway-actual-service.json`。

复现本轮脱敏数据（不再请求厂商；输出目录须不存在）：

```powershell
python scripts/verify-live-gateway.py `
  --case tests/artifacts/live-clients/20261017-130949/claude `
  --case tests/artifacts/live-clients/20261017-131229/pi `
  --case tests/artifacts/live-clients/20261017-130949/dsh `
  --case tests/artifacts/live-clients/20261017-131443/codex-app-server `
  --case tests/artifacts/live-clients/20261017-131616/codex `
  --case tests/artifacts/live-clients/20261017-131521/pi-native `
  --case tests/artifacts/live-clients/20261017-131521/dsh-native `
  --out tests/artifacts/hub-gateway-recheck
```

### 本轮修复与仍有的缺口

修复了 pi 认证存储覆盖本机 token 时被误拒绝、Codex 后端未给 Content-Type 导致观测没有 ID/Token、`session-id` 原生头遗漏，以及客户端收到终止事件即退出时的网关账本收尾竞争。正文设置使用子进程锁/代次，不靠重启。观测解压和字段解析失败不会阻断原始流。

自有网关目前按连接的既有协议转发，没有 Magpie 的跨协议转换、账号池、实时 Plan/余额接口；这些没有宣称完成。上下文分项为结构估算，不推测文件/记忆。没有新的窗口目录推断，仅用原生运行时窗口。直接原生线路仅有原生日志视图。桌面 GUI 手动登录、过期刷新恢复、真实 429 备用线路和强制网络中断尚未验证；限流和中断语义使用真实套接字夹具验证。Windows 父进程退出保护已测，其他平台未做真实客户端验收。

以下全部为**历史阶段**，其 Magpie 引擎通过项不能代替上述自有网关验收。

## 历史：Magpie 网关与真实客户端验收

以下为本轮当前交付状态，后文「验证层次」「后续默认行为」保留此前阶段的历史记录。工作台已升级版本 4；旧版服务不会被启动器当作支持网关的新版复用。

网关实际采用 Magpie 官方 v0.1.1278，并核对 SHA-256。采用隔离 HOME / XDG / APPDATA 和独立配置，关闭账号发现、遥测、自动更新，不读取用户自己的 Magpie 配置；Hub 继续用现有事务生成客户端配置。默认随服务启动，只启动网关不会改原客户端；设置提供接入、恢复直连的预览和应用。

| 实际客户端场景 | 登录 / 线路 | 工具与流式结果 |
|---|---|---|
| Claude Code 2.1.299 | 当前 API 线路 → Magpie | 随机文件读取通过，5 个文本增量，最终随机值匹配；无 Anthropic OAuth 缓存，原生 OAuth 未验证 |
| Codex 0.163.0 CLI | ChatGPT 原生登录 → Magpie 原生入口 | 文件工具通过，最终随机值匹配，auth.json 未被改写 |
| Codex 0.163.0 app-server | ChatGPT 原生登录 → Magpie 原生入口 | 完整工具轮次通过，34 个文本增量，1 个成功文件工具；此前超时/拒绝来自测试目录和 Windows 沙箱初始化方式，本轮已修复 |
| pi 1.3.0 自定义连接 | StepFun 既有 API 连接 → Magpie | 文件工具、7 个文本增量与随机值通过；会话亲和头和响应 ID 均有记录 |
| pi 1.3.0 内置 OpenAI | 原生 OAuth 直连 | 工具及 21 个文本增量通过；一次原生 Connection error 后新调用成功，失败用量不伪记成零 |
| dsh 0.2.0-rc.2 桌面附带 CLI | 既有 API 连接 → Magpie | 文件工具、文本增量、最终随机值通过；用户层、imported 与 profile 层均在隔离目录验证 |
| dsh 内置 DeepSeek 账号 | 原生账号直连，与 Magpie 的原生线路边界一致 | 工具、文本增量及最终随机值通过 |

所有场景均核对真实原配置/凭据文件的前后 SHA-256，结果未变化；测试认证副本已清理。没有执行真实客户端更新，没有写入真实客户端配置。真实调用可能消耗已有账号额度；报告中的 USD 仅为记录的参考成本，不作为本轮实际账单。

最新可复现证据目录：`tests/artifacts/gateway-final-20261017/`。7 组真实调用产物由 `scripts/verify-live-gateway.py` 离线导入正式采集器，形成 **16 次调用、7 个对话**：15 个原生响应 ID，与网关 **10 次精确合并**，另有 **1 次只在网关出现的 dsh 辅助调用**。该辅助调用没有会话头，保留未关联，没有按时间归组。重复索引没有增加请求。

结果为 11 次可确认成功、1 次原生连接失败、4 次单响应结果未确认。4 次未确认属于 Codex 原生透传：网关 HTTP 200 与用量齐全，但该版本账本未保存明确 stop/completed 标记；CLI/app-server 整轮和最终随机值通过，不被用来伪造单响应流结束证据。11 次网关请求具有首内容等待计时；速度按 Magpie 的隐藏推理与突发过滤规则，不能可靠测量的不显示。14 次缺少适用价格，不作为免费。两个有价调用合计约 USD 0.003424，是 pi 原生参考成本。

- 数据一致性：**54 项核对通过**。概览 = 所有对话调用数 + 未关联调用；分布总量一致；每个上下文点与请求的输入/缓存/ID 相同；先读原生或重读两源均不重复累计。
- 浏览器：最新真实记录 **6 组操作通过、0 个脚本错误**，含失败摘要 → 请求 → 对话 → 上下文，真实网关计时/构成、dsh 工具关系、1440/760 窗口、真实网关进程启动和同一事务接入/恢复直连。浏览器接入事务使用隔离测试配置，受管理实例没有导出测试供应商，不向它们发送假密钥。
- 正文：pi 真实网关调用实际产生 1 份会话录制文件；通过 Magpie 进程内接口关闭并清理后为 0，进程 PID 未变化。副本 7 天/256 MiB，保留时限及清理水位另有隔离回归。默认仍关闭额外录制。
- 运行维护：程序哈希校验、拒绝外部端口、子进程退出/恢复与 Windows Job Object 实测通过；内部控制密钥未进入诊断日志。网关正文开关不靠重启中断流式请求。
- 回归：Python 全量 419 项，418 通过、1 项既有跳过；其中新增网关 22 项。前端逻辑 26 项、异步 2 组通过，日常浏览器 5 组通过。最终运行结果以 `tests/artifacts/gateway-python.txt`、`gateway-final-20261017/browser-results.json` 为准。

本轮还修复：dsh 拒绝 pi 专用 compat 参数、多层配置覆盖遗漏、dsh 响应 ID 提取、app-server 非标准产品名的唯一响应 ID 识别、原生 Codex 与网关新输入口径统一、pi 模型增加/移除网关前缀时保留原有高级参数、原生模型目录不可达时的已有缓存回退，以及网关预览内嵌配置锁死锁。真实页面检查后调整上下文构成表格与来源标识；价格配置按批次读取一次，避免历史重建时逐调用反复读取配置。

截图包括 `screenshots/live-overview.png`、`live-request-detail.png`、`live-failed-conversation.png`、`live-failed-context.png`、`gateway-request-context-parts.png`、`gateway-context-linked.png`、`gateway-settings-running.png`、`gateway-connection-preview.png` 和 `gateway-settings-narrow.png`。均为运行页面截图，不是静态设计稿。实际本机默认设置另见 `tests/artifacts/gateway-actual-settings.png` 与 `gateway-actual-settings-narrow.png`；本机 8788 的版本 4 服务和 8789 网关均运行，真实客户端接入配置保持未应用。

交付前本机原生索引快照：915 / 915 个文件已完成，58,241 条历史调用，来源错误 0，额外正文 0。该全量历史快照与上面的 16 条隔离验证记录分别保存，没有把测试数据写入日常索引。

## 复现本轮真实记录

下面仅离线重建脱敏捕获，不会再次调用厂商；`--out` 应为尚不存在的目录。浏览器命令沿用已有 Playwright/Edge 环境设置。

```powershell
python scripts/verify-live-gateway.py `
  --case tests/artifacts/live-clients/20261017-123603/claude `
  --case tests/artifacts/live-clients/20261017-123549/pi `
  --case tests/artifacts/live-clients/20261017-123603/dsh `
  --case tests/artifacts/live-clients/20261017-123603/codex `
  --case tests/artifacts/live-clients/20261017-123620/codex-app-server `
  --case tests/artifacts/live-clients/20261017-122031/pi-native `
  --case tests/artifacts/live-clients/20261017-122031/dsh-native `
  --out tests/artifacts/gateway-review-new
$env:LIVE_OBSERVATION_REPORT = 'tests/artifacts/gateway-review-new'
node tests/live_browser.cjs
```

重新发起真实调用可运行 `python scripts/verify-live-clients.py --agent claude --agent codex --agent pi --agent dsh --gateway`；该命令使用已有线路和账号，可能消耗额度。完整 app-server 工具轮次使用 `--agent codex --codex-transport app-server --gateway`。正文开关实测用 `--agent pi --gateway --gateway-content`，结束后自动关闭并清理副本。原生账号验证另用 `--scenario native`，不会强改 pi/dsh 的原生线路。

## 此前阶段：交付与运行

应用已在本机运行，本轮使用 `http://127.0.0.1:8788/`。已有旧服务占用 8787，未终止它。继续使用原有 `hub.cmd gui`、`hub.cmd ui`、`Launch GUI.vbs` 入口即可；启动器只复用工作台版本 4 及以上服务。

本轮沿用仓库原有未提交工作，没有重置用户改动。模型库、按 Agent 连接、同步预览与事务备份/恢复继续使用原实现。真实客户端配置未通过测试路径写入；真实安装未执行升级。首次真实调用验证期间观测关闭、额外正文关闭；后续按 Magpie 对齐的默认行为见下方更新。

## 验证层次

| 层次 | 结果 | 能说明什么 / 不能说明什么 |
|---|---|---|
| Python 全量回归 | 共 388 项：387 项通过，1 项既有测试跳过；观测相关 18 项、调用隔离器 3 项另行通过 | 包含本轮真实格式与异常清理回归；测试本身不重复发出厂商请求 |
| 原有工作台浏览器回归 | 25 项通过 | 模型 CRUD、高级字段保留、按连接检测、批量删除、同步失败、显式 Codex 第三方选择、预览一次应用、方案保存和恢复、窄窗口 |
| 日常应用浏览器验收 | 5 组完整操作通过，0 个页面脚本错误 | 四行 Agent 首页、桌面实例区分、单 Agent 切换、概览到请求/对话/上下文、长字段、采集留存和清空 |
| 前端逻辑与并发检查 | 26 项逻辑检查及 2 组异步操作通过 | 参数语义、明确范围、无重复应用、预览取消与失效；布局由真实浏览器另验 |
| 真实本机安装检测与发布源检查 | 检出 8 个实例；各自来源/版本分开 | 只读检查。Claude 有可用更新，npm Codex/pi 对应频道已最新；桌面渠道显示应用内操作，未执行真实升级 |
| 真实历史日志抽样 | 四个 Agent 的已有本地记录均解析成功，未额外保存正文 | 历史数据可读；本轮另完成下面的真实新调用验证 |
| 隔离真实客户端调用 | 四个 CLI 均完成工具读取和最终随机值核对；四个均取得文本增量证据 | 真实厂商请求，不是桩。原生登录与未通过的场景逐项列于下表 |
| 真实记录数据链路 | 正式采集器导入 35 条调用、12 个有用量会话；126 项离线核对通过 | 覆盖失败、未知结果、缓存、价格缺失、重复读取。请求、会话、上下文与概览共用实际索引 |
| 真实记录浏览器验收 | 4 组完整操作通过，0 个页面脚本错误；1440 / 760 窗口 | 真实记录回放；不是重新发起 API，也不把人工汇总数写入页面 |


## 后续默认行为对齐（2026-10-17）

用户确认以 Magpie 的采集默认与可获得信息为范围。已核对上游最新提交 `dccfefdf722ea25f5cf2be1798d5686e43b89fbd`，并作以下调整：

- 用量索引默认自动开启、不自动按天删除；仍可停止、设置期限和清空。
- 已有原生对话按需从源文件读取，显示来源；额外正文关闭也能查看，不为这次查看创建正文副本。
- 额外正文副本默认关闭，最多保留 7 天与 256 MiB，过期或超过容量时清理最旧正文；不清理客户端原文件或无到期限制的用量索引。
- 原生日志删除或不可读时，只有之前明确开启留存且尚未过期的副本可用；详情说明读取失败与实际显示来源。原生会话身份、路径根目录、时间筛选与清理水位均核对。
- Magpie 网关可以提供网络计时、尝试及 Prompt 信息；本项目当前的原生路径未接入这些，明确列为本项目能力差异，不能借“Magpie 也做不到”关闭问题。隐藏信息或两端均未记录的活动不强求重建。
- 先前 Claude OAuth / app-server 工具整轮 / 桌面 GUI 的未验证项继续作为历史验证边界，日常只读观测不依赖接管原生登录或重跑这些流程。

本轮无额外厂商调用。默认与按需读取的隔离回归新增 9 项；默认、保留期限、字节容量淘汰、暂停时主动查看、清理水位、路径身份、删除原文件后的回退均有验证。全量回归共 397 项（396 项通过、1 项既有跳过）；观测相关 27 项通过。原有日常界面 5 组与真实记录界面 4 组通过，额外补验关闭留存后仍可查看原生正文。结果见 `tests/artifacts/magpie-defaults-python.txt`、`magpie-defaults-live.json`、`magpie-defaults-on-demand.json`，实际设置截图为 `tests/artifacts/daily/actual-default-settings.png`。

首次全库扫描中发现一条有效 Codex 原生记录约 14 MiB，超过原先按网关正文取的 8 MiB 单行限制。已将原生读取上限调整为 16 MiB，保留 128 MiB 单文件解压上限，并增加带 9 MiB 图片字段的回归；复扫 914 个来源完成，来源错误为 0。用量索引与正文副本相互独立，实际正文副本仍为 0。超出新边界的来源仍会明确提示，不能归为 Magpie 无法读取。

## 本轮真实客户端验证（11:23–11:35，本机时间）

每次运行使用独立 HOME 和客户端配置目录，只复制该场景需要的既有凭据/连接；关闭测试中的项目指令、扩展及 MCP，要求读取测试目录内唯一的 `probe.txt`。文件含一次性随机值，只有工具结果和最终回复均含该值才算工具场景通过。没有执行登录、登出或替换真实原生配置；已有登录复用不等于重新完成浏览器授权/刷新流程。

| 客户端与场景 | 原生登录 / 线路 | 工具与最终结果 | 流式证据 | 判定及边界 |
|---|---|---|---|---|
| Claude Code 2.1.299 | 当前已配置的 Anthropic 兼容 API 线路 | Read 成功，最终随机值一致 | 3 个 text_delta | API 线路通过。独立 `auth status` 为 loggedIn=false / authMethod=none，本机没有可复制的 Anthropic OAuth；原生订阅登录未验证 |
| Codex npm 0.163.0 exec | 复制 ChatGPT auth.json，由原生 CLI 使用 | 只读 PowerShell Get-Content 成功，退出 0，最终随机值一致 | exec 的 JSONL 生命周期/工具事件可用，不输出逐字 delta | 登录与 CLI 工具通过；不将 exec 事件流冒充逐字输出 |
| Codex npm 0.163.0 app-server，纯文本 | 同一类 ChatGPT 原生登录缓存，另一个隔离目录 | 不使用工具的文本流专项，最终随机值一致 | 17 个 item/agentMessage/delta，收到整轮结束 | 文本流完成通过。app-server 的工具整轮另两次超时，见下文，不能合并宣称全部通过 |
| pi 1.3.0 当前线路 | 当前 StepFun API 配置 | read 成功，最终随机值一致 | 8 个 text_delta | 通过；本次两次模型响应的参考价格未知 |
| pi 1.3.0 原生登录 | auth.json 中现有 OpenAI OAuth；gpt-6.1-sol | read 成功，最终随机值一致 | 17 个 text_delta | 登录复用通过；原生客户端记录的成本仍是标价参考，不是订阅实际扣费 |
| dsh 桌面附带 CLI 0.2.0-rc.2 当前线路 | 独立 headless profile + 当前 StepFun 线路 | read 成功，最终随机值一致 | stdout 有 text 事件；原生消息留有分块流记录 | 通过；缓存字段缺失时上下文仍按下界显示 |
| dsh 桌面附带 CLI 0.2.0-rc.2 原生账号 | 复制 DeepSeek 账号凭据；deepseek-account / deepseek-flash | read 成功，最终随机值一致 | 原生 19 个文本片段，含分片时间；正常 finish(stop) | 原生账号复用通过；没有操作桌面 GUI，也没有把 Electron 版本用于本次判定 |

完整结果保存在 `tests/artifacts/live-report-20261017/verification.json`。严格结果区分 `finalNonceVerified`、`successfulToolResults`、`textDeltaEvents`、`timedOut` 和原始配置哈希；客户端退出 0 不作为任务成功的充分条件。

## 本轮发现与修复

1. **dsh 原生账号的成功状态遗漏**：DeepSeek 原生回放头没有 replayState.response.stopReason，但该条 assistant/message 的 stream 中有 finish.reason。现在读取同一消息的原生结束原因，正常、工具、长度、错误、中断分别识别；无结束证据仍未知。
2. **工具参数与结果关联丢失**：保留 Codex custom_tool_call.input / call_id，pi 的 toolCallId / toolName / isError，dsh tool/result.message.toolCallId / isError。实际随机文件内容和工具 ID 能在对话中核对。失败助手消息的原生 errorMessage 也进入对话详情。
3. **失败的全零用量被当成已测零值**：pi 以旧模型 gpt-5.3-codex 访问 ChatGPT 账号时返回真实 400，客户端仍退出 0，并留下全零初始化 usage。现在失败/中断中的这种全零记录标为用量未确认，费用不可计价，详情解释原因。改用当前目录已有的 gpt-6.1-sol 后复测成功，没有把同名模型当作所有登录模式均可用。
4. **修复后旧来源没有重新读取**：来源指纹加入解析器版本，升级后即使原生文件大小/修改时间相同也会重新索引；修正能应用到既有数据，仍按原生 ID 去重。
5. **整轮时间与单次调用混淆风险**：Codex task_complete 的 duration_ms / time_to_first_token_ms 作为原生整轮事件留在对话中，没有填到多个 response_id 的耗时/首字列。界面继续将调用数、消息数与整轮结果分开。
6. **留存与显示**：清除证据文件中下载链接的认证查询参数，生产正文脱敏也清除 URL 查询；只读详情标题不再显示为模型设置。
7. **既有迁移幂等性**：全量回归跨秒时发现 migrate-agents 无变化也更新时间戳。现在无变化不写配置、不建备份；测试固定两次不同时间并核对无写入，消除偶发通过的情况。

客户端测试本身也经过纠正：Claude 首次触发 $0.30 测试预算上限，缩短测试系统提示并改用 $0.50 上限后完成；该预算是客户端按标价估算的门槛，不是本次实际扣费。Codex 系统临时目录会拒绝创建原生 helper，且首次缺少 Windows sandbox 配置导致只读命令被策略拦截；改为工作区内的隔离目录、明确使用原生 elevated sandbox 后 CLI 完成。没有放开到 danger-full-access。

Codex app-server 工具场景两次分别在 100s / 75s 超时：第一次有真实文本增量和原生工具等待记录；第二次已调用官方 Windows sandbox setup API，但在总时限内没有产生用量响应。测试只结束自己启动的进程树。没有将这些运行改标为成功，也没有将等待工具的每次模型响应误作网络重试。后续纯文本 app-server 专项完整结束，验证的是文本流，不替代该工具缺口。

## 真实记录的定位证据

本轮报告将真实原生日志拷入不含凭据的隔离源目录，运行正式采集器后建立 SQLite 索引，再让正式 HTTP API/UI 使用该索引。14 次捕获场景中，1 份早期 pi 捕获因测试脱敏器误遮盖了非敏感模型 ID 而不进入统计；已修复脱敏并以新调用补验，排除理由保存在报告中。13 个原生文件中有 12 个会话包含可索引用量；另一个超时会话没有用量，不能把“无索引”当成零调用。

- **统计口径**：35 条已记录模型调用，12 条成功、1 条 pi 明确失败、22 条 Codex 结果未确认。35 不是 14 次测试运行，也不是 HTTP 尝试数。Claude 的测试预算失败属于整轮结果，原生记录的已结束模型响应仍按响应本身统计。
- **Token/缓存**：34 条有可确认输入/输出；缓存读写各 32 条有记录。概览累计上下文输入 620014，仅用于核对各点之和，不表示任一会话占用了这么大的窗口。缓存在 Codex input 内，不重复相加。
- **费用**：33 条无法计价；仅 2 条 pi 原生参考成本合计 $0.003384。此数不是全部测试的成本或供应商扣费，尤其不能与 ChatGPT/DeepSeek 订阅扣费混用。
- **失败链路**：概览点“失败” → pi 的 400 请求 → 同一个原生会话 → 同一条上下文点。错误明确为 ChatGPT 登录不支持该旧模型；没有用量证据，输入/窗口占用为未知，不能归因为上下文超限，也不能认定免费。
- **成功与上下文链路**：dsh 原生账号同一会话有两条调用，单次输入含缓存分别为 6123 和 6280；原生窗口 1000000，累计输出 91。工具调用和结果的 ID 一致，结果包含测试随机值；第二条输入是 136 新输入 + 6144 缓存，不是该对话累计 Token。
- **重复读取**：第二次扫描调用数仍为 35；四页 ID、会话归属、缓存和单次上下文点逐条核对，126 项通过。

真实页面截图在 `tests/artifacts/live-report-20261017/screenshots/`：`live-overview.png`、`live-failed-requests.png`、`live-request-detail.png`、`live-failed-conversation.png`、`live-failed-context.png`、`live-dsh-tool-conversation.png`、`live-dsh-context.png`、`live-context-narrow.png`。浏览器结果为同目录 `../browser-results.json`。

## 隔离、清理与重现

正常完成场景的原始客户端配置/认证文件前后哈希相同。临时复制的 OAuth、API key、dsh 账号凭据均已删除；正文只包含本次无敏感业务内容的探针任务，正式 Hub 的采集与正文设置没有被开启。某些 Codex 原生 elevated sandbox 会创建管理员拥有的 sandbox_users.json，普通用户有写权限但无删除权限；两处测试目录仅留下已清空的 0 字节文件与父目录，不含原生登录凭据或 sandbox 密码。脚本报告 `cleanup.remainingFiles`；本轮全部残留位置另列在 `tests/artifacts/live-report-20261017/cleanup-audit.json`，不会隐瞒残留。

本机产物另核对 24 个已知凭据值及 JWT 特征，扫描 77 个 JSON / JSONL / SQLite 文件没有命中，结果在 `privacy-audit.json`。这是已知凭据与特征的检查，不等于能识别任意私人业务文本；本轮探针本身只包含随机校验值和读取操作。

真实调用命令见 README。它们使用当前配置/现存账号，可能产生额度与费用消耗；实时测试脚本验证最终内容、工具结果和清理状态，失败时退出非 0。离线复核和浏览器重放不发起厂商请求：

```powershell
python scripts/verify-live-observation.py --out tests/artifacts/live-review-new
$env:LIVE_OBSERVATION_REPORT = (Resolve-Path tests/artifacts/live-review-new).Path
node tests/live_browser.cjs
```

需要已有 playwright / Edge（沿用 PLAYWRIGHT_MODULE、BROWSER_EXECUTABLE 环境参数）。所有捕获、报告、截图与 SQLite 均被 Git 忽略；测试脚本、回归用例与说明可随代码保存。

## 本机实例核对

| Agent | 安装实例 / 版本 | 判定 |
|---|---|---|
| Claude Code | 原生 CLI `2.1.299` | WindowsApps 包装脚本解析到 `.local/bin/claude.exe`；发布频道 latest；检查发现 `2.1.302`。未安装更新 |
| Codex | npm CLI `0.163.0` | 精确 npm prefix；对应 npm 包已最新 |
| Codex | 桌面维护运行时 `0.159.2` | 不是 npm 实例；只能在桌面渠道维护 |
| Codex | 桌面维护运行时 `0.163.0-alpha.10.2` | 可匹配运行中的进程映像；不因 PATH npm 版本较新而更新另一份安装 |
| Codex | Store 桌面应用 `26.1013.4894.0` | 取 MSIX 元数据；没有用 CLI 版本冒充桌面版本 |
| pi | npm CLI `1.3.0` | 从实际包装脚本确认 `@earendil-works/pi-coding-agent`，不硬套旧包名 |
| dsh | 桌面附带 CLI `0.2.0-rc.2` | 与独立 npm 安装分开；随桌面渠道更新 |
| dsh | 桌面应用 `0.2.0-rc.2` | 读取 `app.asar` 中应用自己的 package.json；Electron 运行时 `44.0.0` 不作为应用版本。使用本机 updater 的 nightly feed 检查 |

版本号与发布状态是本轮快照，之后以重新检查的结果为准。npm prefix、应用路径和进程识别只保存在本机产物中，文档不列个人主目录。

## 可复现的证据链

运行 `node tests/daily_browser.cjs`。测试服务器建立临时 HOME，用 `tests/daily_records.py` 写入脱敏的四类原生格式文件，再由正式解析器导入；页面上的统计由这些记录计算，没有直接设置概览总数。

1. 六次已记录调用来自四个 Agent；Claude 的两个内容块合并为一次调用，Codex 的两类用量事件也只计一次。
2. 从概览的失败摘要进入请求，只有一条 pi 429 失败；长错误完整保存在详情中，表格仅作摘要。
3. 详情输入量为 `23000` Token，原生会话 ID 是 `evidence-work`，没有已知窗口，因此占用百分比未知。
4. 进入对话后，仍使用原时间/Agent/模型条件，展开该对话的全部结果：一次成功、一次失败、一次中断，总共三次调用。对话累计输出为 `450` Token，与单次输入量分开。
5. 上下文图三个点依次为 `21200`、`23000`、`6000`；有一条原生 compaction 事件。不能仅凭输入量下降证明压缩，因此页面单独显示原生事件时间。
6. 第三次调用的价格未知，不显示为免费；重试次数和首字等待都无证据，显示未知/未采集。
7. 页面再导入同一份文件，调用总数不增加。dsh 同目录 v3/v4 只读最新代；继承 seed 不重复计费。

“请求详情进入会话”展开同一时间范围的会话结果，不把失败状态筛选限制带入对话解释；回到统一筛选区域可看到当前限定的 Agent 与会话，并一键重置。

## 更新与配置的失败路径

- 更新命令退出 0，但原入口仍输出旧版本：报告验证失败。
- 新版本实际可从同一入口启动且版本匹配：报告已核验，并提示重启已有客户端。
- npm 精确 prefix 与包名匹配；安装前 manifest 或原生版本发生变化时拒绝执行。
- 发布源不可达、来源未知、需原渠道操作、文件占用/权限错误分别报告。
- 一项测试运行真正的隔离 Python 更新器进程，修改临时版本文件后重跑 `--version`；厂商安装器执行路径只用隔离桩验证，真实厂商更新未执行。
- 单 Agent 选择保留其他 Agent 的 Hub 选择；“保留当前值”的 Agent 不进入原生写入范围。
- Claude `keep` 档位的原生模型及显示名保持，`clear` 才移除；Codex 未指定推理强度时保留当前值。
- 确认应用消费的是用户看过的原预览；预览之后外部修改拒绝写入。中途失败可回滚，恢复遇到后续外部修改会拒绝覆盖。
- 隐藏的历史 Agent 连接、方案与启用状态作为数据保留；默认批量操作不写它们。

## 采集、留存与退出

正式采集器经过开启、重复扫描、停止、重新读取、关闭正文、缩短保留期、清空与水位测试。停止后新增测试文件不会被索引。关闭正文删除已留内容，SQLite 使用 secure_delete，清理后截断 WAL；清空时压缩数据库。清空只影响 Hub 索引，不删除客户端原文件。

服务启动后按保存设置每 30 秒扫描；退出服务时停止监测与采集线程。设置新增“退出本地服务”，并明确关闭浏览器窗口不会结束后台服务。退出 API 与非本机 Host 拒绝均有隔离 HTTP 测试。

## 截图与本机产物

截图来自真实渲染页面（Edge / Codex 内置浏览器），不是设计稿：

- `tests/artifacts/daily/actual-agents.png`：真实本机安装、当前配置及 Claude 更新检查。
- `tests/artifacts/daily/agents-desktop.png`、`agents-mobile.png`：隔离安装实例布局。
- `tests/artifacts/daily/overview-desktop.png`、`failed-requests.png`、`request-detail.png`：隔离记录的概览与请求定位。
- `tests/artifacts/daily/conversation-detail.png`、`context-desktop.png`、`context-narrow.png`：同一证据链与窄窗口。
- `tests/artifacts/daily/conversations-narrow.png`、`requests-narrow.png`、`settings-narrow.png`、`mobile-empty.png`：长模型名、横向滚动、采集设置和空状态。
- `tests/artifacts/daily/browser-results.json`：日常浏览器回归结果。
- `tests/artifacts/daily/live-inventory.json`、`live-native-validation.json`：真实本机只读核对快照。

产物目录和私有观测 data 目录已被 Git 忽略。密钥扫描只输出命中文件与数量，不打印可疑值；现有回归夹具中仍有被扫描器识别的假密钥字符串，均为测试数据；gold 文件没有非空密钥字段。

## 当前仍存在的能力缺口

1. 仅实际经过网关的调用有网络视图。pi 内置 OAuth、dsh 内置账号依照原生客户端保持直连，只有原生日志覆盖；客户端未写的失败/重试无法补造。强行把 pi 内置 OAuth 改指 Codex 后端的探测曾得到拒绝，已去掉该改写，恢复直连后复测通过。
2. dsh 目前未在这条自定义线路发送 session header。主调用可按 responseId 与原生精确关联，缺少原生对应的辅助调用保持未关联。没有通过文本/时间猜归属。
3. Magpie 当前原生 Codex 透传账本没有明确 stop；单响应最终状态保留未知。原生整轮事件单独展示，不能代表每个网络响应完成。
4. 上下文分项来自 Magpie 的字符与结构估算，虽然可用总输入校准，仍非精确分项计量；网关目录窗口标为参考，原生有效窗口另标。隐藏推理、未记录文件注入、已丢失内容不能重建，不做健康评分。
5. 价格未知不免费，模型标价/客户端参考成本不是扣费；未接入实时账号额度和实际账单。额度仅为原生带时间快照。网关路由详情按上游 30 天/200 MiB 保留，用量账本与 Hub 索引有独立边界。
6. Claude 原生 OAuth 无已登录缓存，重新授权、过期刷新、桌面 GUI 手动流程、强制断网/取消后的完整恢复和跨版本适配未在本轮实测。Codex CLI 与 app-server 工具/流式已实测，不能再称为未通过。
7. 真实调用中没有制造厂商 429 后切换备用线，也未强制中断正在计费的流。重试后最终成功、尝试用量累计、缺价、未知结果和中断分类通过脱敏隔离记录回归；不把它们称为真实厂商故障验收。
8. 桌面安装器/Store 更新仍在原应用执行，未知安装来源不自动更新。本轮没有执行真实更新。受管理 Magpie 程序目前仅 Windows x64，其他系统尚未提供自动下载适配。

组件细节与数据边界见 [gateway.md](gateway.md)，逐项来源、条件与精度见 [observation-sources.md](observation-sources.md)。
