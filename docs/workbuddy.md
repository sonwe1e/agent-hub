# WorkBuddy AI 海外版

**当前状态：Hub 已取消 WorkBuddy 订阅反代。** 设置中不再提供登录和接入入口，旧的 WorkBuddy 供应商配置不会作为网关连接或交接摘要线路导出；旧专用网关入口会返回停用提示。原有供应商配置、账号登录文件、客户端配置和历史记录不会因这项变更被删除。此前使用此连接的客户端需要在 Hub 中选择其他可用连接并应用配置；已运行的旧 Hub 或网关需要重启后才能使用新行为。

`agent_hub/gateway/protocol.py` 中的共用协议转换能力仍由 Codex／ChatGPT 和 ZCode 订阅适配器使用；根目录的 `workbuddy_protocol.py` 是旧导入的兼容入口。`tests/test_workbuddy_retired.py` 检查停用后的网关和摘要行为；`tests/test_workbuddy_cases.py` 继续验证共用协议转换。

以下内容保存停用前的实现、操作方法和验证记录，均属于**历史资料**，不能作为当前产品功能或操作指引。旧真实账号探针命令也仅供追溯，请勿在当前版本执行。

## 历史实现

Hub 内置 WorkBuddy AI（`https://www.workbuddy.ai`）账号适配器。登录、刷新与模型目录由 `workbuddy.py` 管理；`workbuddy_protocol.py` 在 Hub 自有网关中将 Anthropic Messages、OpenAI Chat Completions 和 Responses 转换为 WorkBuddy 的流式 Chat Completions。无需安装或运行 Magpie，也不增加 Node.js 服务依赖。

## 历史使用方法（已停用）

1. 启动或重新打开新版 Hub，进入「设置 → WorkBuddy AI（海外版）」。
2. 点击「登录 WorkBuddy 海外版」，打开出现的官方登录页面，在浏览器中选择账号并完成登录。页面有效期五分钟，可取消。登录完成后界面自动读取此账号的模型目录并接入供应商；读取失败可点击「读取模型并接入」重试。
3. 回到 Agent 页面，在 Claude Code、Codex、pi、dsh 或 OpenCode 的详情中选择 WorkBuddy AI 与模型，预览并应用配置。OpenCode 在旧配置中需先到「模型与方案 → 高级」启用同步；此前已接入的 WorkBuddy 账号需点击「刷新模型并恢复服务」添加 OpenCode 连接。Codex 使用第三方 Responses 连接，现有官方登录凭据仍由 Codex 管理。
4. 开始新会话或重启客户端。使用这些模型时保持 Hub 运行。

「刷新模型并恢复服务」重新查询账号的 CLI 模型目录，更新已有模型元数据并追加新模型；不会删除已保存的模型，避免破坏现有方案。上游不再提供的旧模型可在模型库手动删除。上游请求失败时保留上游 HTTP 状态和安全的错误码，不自动切账号、不把未知用量当作零。唯一的网关内重试是下述 11153「工具结果后跟用户消息」兼容重发；其余失败（含其他 400、11140 受限、连接失败）都只尝试一次。

当前为一个海外版浏览器账号，可以重新登录切换；企业身份随登录保存，并用于模型目录、Token 刷新和推理请求。没有读取或解密桌面 WorkBuddy、Magpie 的登录文件。国内版、账号池、签到和实时积分余额不在此实现内。

## 历史连接与生命周期（已停用）

接入只保存 Hub 供应商，不会自动改写客户端文件。实际客户端写入继续走现有的预览、冲突保护、备份及恢复事务。供应商 ID 为 `workbuddy-ai`；如果此 ID 已被普通供应商占用，接入会明确报错。

默认本机地址为：

| Agent | Base URL | 协议 |
| --- | --- | --- |
| Claude Code | `http://127.0.0.1:8789/workbuddy/claude` | Anthropic Messages |
| Codex | `http://127.0.0.1:8789/workbuddy/codex/v1` | Responses |
| pi | `http://127.0.0.1:8789/workbuddy/pi/v1` | Chat Completions |
| dsh | `http://127.0.0.1:8789/workbuddy/dsh/v1` | Chat Completions |
| OpenCode | `http://127.0.0.1:8789/workbuddy/opencode/v1` | Chat Completions |

使用设置中的网关端口时，上述地址相应改变。接入时生成随机本机连接密钥，它不是 WorkBuddy 的 OAuth Token。启用全局网关后，四个网关 Agent 还可以通过已有的 `hub-<agent>-<hash>/<model>` 模型命名空间调用；OpenCode 始终使用上表专用入口，不会递归转发回自己。

WorkBuddy 供应商启用时会随 Hub 恢复其网关；全局「恢复直连」仍保留 WorkBuddy 必需的本机适配。退出 Hub 会结束其网关。点击「退出此账号」取消待完成登录、删除 Hub 保存的 OAuth 文件、停用该供应商并刷新网关路由，保留模型与方案以便重新登录后使用；这不会撤销已发送到上游的在途请求，也不会修改客户端已有配置。

## 历史凭据与观测（已停用）

- OAuth 数据：`data/gateway/config/agent-hub/workbuddy-auth.json`，在 Git 忽略的本机私有数据目录中。`providers.json` 仅含本机连接密钥，不含上游 access/refresh Token。登录状态 API 不返回 OAuth 凭据。
- 在请求和模型目录查询前检查 Token，临近到期一分钟尝试刷新。跨线程、跨 Hub/网关进程的文件锁避免旋转刷新凭据被同时使用；刷新成功立即原子落盘。刷新失败且旧 Token 尚有效时继续使用，已过期时明确要求重新登录。
- 工具定义、函数参数片段、工具结果、图片、推理内容、三种协议的流式和非流式结果均通过适配；上游始终使用流式请求。工具名转换后会还原为客户端原名。Responses 的 `namespace` 工具组会展平后发给上游，返回及历史重放保留本地工具名与命名空间。Codex custom 文本工具（如 `apply_patch`）使用字符串参数函数向上游请求，并还原为 `custom_tool_call` 事件和输入；格式说明保留给模型，不提供额外语法约束解码。
- 工具参数 schema 按 Magpie WorkBuddy 适配器的 `fitSchema`/`fitShape`/`fitEnum`/`objectRoot` 语义归一：`pattern` 中的 `\0` 改写为 `\u0000`（转义反斜杠与八进制 `\01` 不动）；`items` 列表合成一个 schema（空列表为任意、单条展开、多条 `anyOf`，丢弃 `additionalItems`）；`type` 统一小写并过滤未知项（含列表形式）；非列表 `required` 与非字符串项剔除；无类型的 `enum` 取其值类型（含 `null` 时加 `"null"`），多类型 `enum` 拆成带类型 `enum` 的 `anyOf`，空 `enum` 删除；布尔子 schema 变为 `{}` / `{"not":{}}`（`additionalProperties` 本身的布尔值保留）。参数根合成为对象：`allOf` 分支整体合并，`anyOf`/`oneOf` 的对象分支合并属性（各分支共同 required 的字段保留 required，同名不同 schema 的字段合成 `anyOf`），本地 `$ref` 先解析；无类型或空根按对象处理。
- **11153 兼容重发**：部分模型（如 gemini-3.8-flash）对「工具结果后紧跟用户消息」的请求回 400 code 11153。网关收到该拒绝时在工具结果与用户消息之间插入一条空 assistant 消息重发一次（同一发送沿用同一会话归因头），并按模型记住该行为——该模型的后续请求直接以分隔形态发出，省去一次被拒往返。账本记 `tries=2` 并标注 `retry=after-tools`。以 assistant 消息结尾的 prefill 请求不做此处理；其他 400 不重试。
- **回包模型名回写为客户端请求名**：上游回显的模型拼写只进账本的 `served` 字段；发给客户端的回复（含流式事件）一律使用客户端请求时使用的模型名，读回模型名的客户端（如 Claude Code 用它判断上下文窗口）看到的是它请求的名字。
- **Responses 历史可移植性**（跨线路切换后续接旧会话时）：不透明 `reasoning` 项跳过；`compaction`、`item_reference` 及携带顶层 `encrypted_content` 的条目显式返回 400 并提示「用文字摘要并新建会话」，不静默丢弃历史；历史 `web_search_call` 转成一段说明性 assistant 消息（不重建搜索结果）；消息内容数组中嵌套的 `encrypted_content` 部分跳过。
- 对 Claude Code / Codex 的少量固定客户端说明句采用 Magpie WorkBuddy 适配器的兼容写法，移除 Anthropic billing 传输头，避免真实上游拒绝这些客户端元数据。转换在请求副本上进行，工具执行结果保持原文。
- Responses 的 `previous_response_id` 使用按供应商、会话及账号身份隔离的进程内缓存续接；缓存最多 64 条 / 32 MiB / 15 分钟，不写入磁盘。过期、重启或跨会话引用会明确报错，需要重发完整历史。
- 不支持 Responses `/compact`、后台 Responses、服务端内置工具、多候选生成和 WebSocket。客户端应使用 HTTP SSE 与自行执行的函数或自定义文本工具；Codex 的不透明 reasoning 项没有 Chat 协议等价物，历史按上条可移植性规则处理。
- 上游流不完整时发出协议错误并记为失败，客户端断开时取消上游连接并记为 499。Windows 阻塞网络读取与处理线程分开，避免取消后等待模型首字。
- 用量与响应 ID 进入现有网关账本，继续与原生记录合并。未返回用量时保留未知；模型的积分倍率不转换成 USD Token 单价。正文留存沿用现有开关与录制代次；缓存续接与磁盘正文留存是不同机制。

## 历史验证与参考

`tests/test_workbuddy.py` 使用临时目录与真实本机 HTTP 套接字模拟上游，覆盖浏览器登录、企业身份、并发刷新、动态目录、三个协议、工具往返、Responses 续接、取消、上游拒绝、未知用量和凭据边界。浏览器操作覆盖位于 `tests/workbuddy_browser.cjs`。协议转换的确定性用例以数据形式放在 `tests/workbuddy_cases.json`（Magpie 社区仓库 `cases.json` 的模式），由 `tests/test_workbuddy_cases.py` 按网关的调用方式驱动：schema 归一 18 条、11153 分隔 5 条、请求转换 8 条、显式拒绝 4 条，共 35 条。

2026-10-19 已使用真实海外个人账号完成浏览器授权，并经正在运行的 Hub 发起真实模型调用。三种协议的流式函数工具往返均通过（当时目录中的 `default-model`）；目录调整后，Claude Code、Codex CLI、pi、dsh 四个真实客户端均使用 `step-5-preview` 读出隔离文件内的随机值，工具结果和最终回复核对一致。Codex app-server 另完成 5 个文本增量的流式验证，Responses 命名空间 custom 文本工具与 `previous_response_id` 续接通过。没有将原生 Claude / ChatGPT / DeepSeek 账号复制进测试；客户端只使用 Hub 的本机连接密钥。

2026-10-23 按 Magpie 社区适配器补齐四组能力并完成回归：工具 schema 归一（`fitSchema`/`fitShape`/`fitEnum`/`objectRoot` 全量语义）、11153 兼容重发（含按模型记忆与会话归因头稳定）、回包模型名回写为客户端请求名、Responses 历史可移植性（显式拒绝与 `web_search_call` 合成消息）；转换用例迁入 `workbuddy_cases.json`。全量 Python 回归 496 项通过（1 项既有跳过）。以上行为在模拟上游验证；11153 拒绝形态、`web_search_call` 历史与 schema 归一的各条规则在停用前未完成真实账号复现。

本机证据在 Git 忽略的 `tests/artifacts/workbuddy/live-report-20261019/`，包括 `report.json`、原始响应的索引和可离线重跑的 `audit.py`。选取的工具 / 流式成功场景共对应 19 条完整响应，另保留 1 条 dsh 客户端取消的并发请求（499，用量未知）；7 条原生响应按精确 response ID 与网关合并，重复导入不增加记录。dsh 的隔离 profile 未产生原生 JSONL，其工具证据来自客户端 JSON 输出，网关记录没有凭时间强行归入原生会话。早期兼容失败记录继续保留，未混入成功数。

456 项 Python 回归完成（455 通过，1 项既有跳过）。原客户端配置 / 凭据指纹不变，临时配置凭据已清理，正文额外留存保持关闭。真实企业账号、临期 Token 刷新、图片与全部模型遍历尚未实测，实际积分扣费未知。上游模型与登录接口可能改变，异常时应保留错误再针对当前接口检查。

以下是停用前曾使用的真实账号探针命令，仅保留以便追溯，不应在当前版本执行：

```powershell
python scripts/verify-live-workbuddy.py --model step-5-preview --out tests/artifacts/workbuddy/live-new
python scripts/verify-live-workbuddy.py --model step-5-preview --protocol responses --custom --out tests/artifacts/workbuddy/live-custom-new
python scripts/verify-live-clients.py --agent claude --agent codex --agent pi --agent dsh --workbuddy-model step-5-preview --timeout 150
python scripts/verify-live-clients.py --agent codex --codex-transport app-server --stream-only --workbuddy-model step-5-preview --timeout 150
```

协议参考来自 [magpie-community/plugins 的 WorkBuddy 适配器](https://github.com/magpie-community/plugins/tree/47fbf634e7718247a21ed414905fe77f67210e94/packages/workbuddy)，版本 `0.1.25`，提交 `47fbf634e7718247a21ed414905fe77f67210e94`。相关派生实现保留 [MIT 许可](WORKBUDDY-LICENSE.txt)。
