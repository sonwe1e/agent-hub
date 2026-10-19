# WorkBuddy AI 海外版

Hub 内置 WorkBuddy AI（`https://www.workbuddy.ai`）账号适配器。登录、刷新与模型目录由 `workbuddy.py` 管理；`workbuddy_protocol.py` 在 Hub 自有网关中将 Anthropic Messages、OpenAI Chat Completions 和 Responses 转换为 WorkBuddy 的流式 Chat Completions。无需安装或运行 Magpie，也不增加 Node.js 服务依赖。

## 使用

1. 启动或重新打开新版 Hub，进入「设置 → WorkBuddy AI（海外版）」。
2. 点击「登录 WorkBuddy 海外版」，打开出现的官方登录页面，在浏览器中选择账号并完成登录。页面有效期五分钟，可取消。登录完成后界面自动读取此账号的模型目录并接入供应商；读取失败可点击「读取模型并接入」重试。
3. 回到 Agent 页面，在 Claude Code、Codex、pi 或 dsh 行内选择 WorkBuddy AI 与模型，预览并应用配置。Codex 使用第三方 Responses 连接，现有官方登录凭据仍由 Codex 管理。
4. 开始新会话或重启客户端。使用这些模型时保持 Hub 运行。

「刷新模型并恢复服务」重新查询账号的 CLI 模型目录，更新已有模型元数据并追加新模型；不会删除已保存的模型，避免破坏现有方案。上游不再提供的旧模型可在模型库手动删除。请求失败时保留上游 HTTP 状态和安全的错误码，不自动重试、不自动切账号、不把未知用量当作零。

当前为一个海外版浏览器账号，可以重新登录切换；企业身份随登录保存，并用于模型目录、Token 刷新和推理请求。没有读取或解密桌面 WorkBuddy、Magpie 的登录文件。国内版、账号池、签到和实时积分余额不在此实现内。

## 连接与生命周期

接入只保存 Hub 供应商，不会自动改写客户端文件。实际客户端写入继续走现有的预览、冲突保护、备份及恢复事务。供应商 ID 为 `workbuddy-ai`；如果此 ID 已被普通供应商占用，接入会明确报错。

默认本机地址为：

| Agent | Base URL | 协议 |
| --- | --- | --- |
| Claude Code | `http://127.0.0.1:8789/workbuddy/claude` | Anthropic Messages |
| Codex | `http://127.0.0.1:8789/workbuddy/codex/v1` | Responses |
| pi | `http://127.0.0.1:8789/workbuddy/pi/v1` | Chat Completions |
| dsh | `http://127.0.0.1:8789/workbuddy/dsh/v1` | Chat Completions |

使用设置中的网关端口时，上述地址相应改变。接入时生成随机本机连接密钥，它不是 WorkBuddy 的 OAuth Token。启用全局网关后，同样可以通过已有的 `hub-<agent>-<hash>/<model>` 模型命名空间调用，不会递归转发回自己。

WorkBuddy 供应商启用时会随 Hub 恢复其网关；全局「恢复直连」仍保留 WorkBuddy 必需的本机适配。退出 Hub 会结束其网关。点击「退出此账号」取消待完成登录、删除 Hub 保存的 OAuth 文件、停用该供应商并刷新网关路由，保留模型与方案以便重新登录后使用；这不会撤销已发送到上游的在途请求，也不会修改客户端已有配置。

## 凭据与观测

- OAuth 数据：`data/gateway/config/agent-hub/workbuddy-auth.json`，在 Git 忽略的本机私有数据目录中。`providers.json` 仅含本机连接密钥，不含上游 access/refresh Token。登录状态 API 不返回 OAuth 凭据。
- 在请求和模型目录查询前检查 Token，临近到期一分钟尝试刷新。跨线程、跨 Hub/网关进程的文件锁避免旋转刷新凭据被同时使用；刷新成功立即原子落盘。刷新失败且旧 Token 尚有效时继续使用，已过期时明确要求重新登录。
- 工具定义、函数参数片段、工具结果、图片、推理内容、三种协议的流式和非流式结果均通过适配；上游始终使用流式请求。工具名转换后会还原为客户端原名。Responses 的 `namespace` 工具组会展平后发给上游，返回及历史重放保留本地工具名与命名空间。Codex custom 文本工具（如 `apply_patch`）使用字符串参数函数向上游请求，并还原为 `custom_tool_call` 事件和输入；格式说明保留给模型，不提供额外语法约束解码。
- 对 Claude Code / Codex 的少量固定客户端说明句采用 Magpie WorkBuddy 适配器的兼容写法，移除 Anthropic billing 传输头，避免真实上游拒绝这些客户端元数据。转换在请求副本上进行，工具执行结果保持原文。
- Responses 的 `previous_response_id` 使用按供应商、会话及账号身份隔离的进程内缓存续接；缓存最多 64 条 / 32 MiB / 15 分钟，不写入磁盘。过期、重启或跨会话引用会明确报错，需要重发完整历史。
- 不支持 Responses `/compact`、后台 Responses、服务端内置工具、多候选生成和 WebSocket。客户端应使用 HTTP SSE 与自行执行的函数或自定义文本工具；Codex 的不透明 reasoning 项没有 Chat 协议等价物，只重放可表示的消息和工具历史。
- 上游流不完整时发出协议错误并记为失败，客户端断开时取消上游连接并记为 499。Windows 阻塞网络读取与处理线程分开，避免取消后等待模型首字。
- 用量与响应 ID 进入现有网关账本，继续与原生记录合并。未返回用量时保留未知；模型的积分倍率不转换成 USD Token 单价。正文留存沿用现有开关与录制代次；缓存续接与磁盘正文留存是不同机制。

## 验证与参考

`tests/test_workbuddy.py` 使用临时目录与真实本机 HTTP 套接字模拟上游，覆盖浏览器登录、企业身份、并发刷新、动态目录、三个协议、工具往返、Responses 续接、取消、上游拒绝、未知用量和凭据边界。浏览器操作覆盖位于 `tests/workbuddy_browser.cjs`。

2026-10-19 已使用真实海外个人账号完成浏览器授权，并经正在运行的 Hub 发起真实模型调用。三种协议的流式函数工具往返均通过（当时目录中的 `default-model`）；目录调整后，Claude Code、Codex CLI、pi、dsh 四个真实客户端均使用 `step-5-preview` 读出隔离文件内的随机值，工具结果和最终回复核对一致。Codex app-server 另完成 5 个文本增量的流式验证，Responses 命名空间 custom 文本工具与 `previous_response_id` 续接通过。没有将原生 Claude / ChatGPT / DeepSeek 账号复制进测试；客户端只使用 Hub 的本机连接密钥。

本机证据在 Git 忽略的 `tests/artifacts/workbuddy/live-report-20261019/`，包括 `report.json`、原始响应的索引和可离线重跑的 `audit.py`。选取的工具 / 流式成功场景共对应 19 条完整响应，另保留 1 条 dsh 客户端取消的并发请求（499，用量未知）；7 条原生响应按精确 response ID 与网关合并，重复导入不增加记录。dsh 的隔离 profile 未产生原生 JSONL，其工具证据来自客户端 JSON 输出，网关记录没有凭时间强行归入原生会话。早期兼容失败记录继续保留，未混入成功数。

456 项 Python 回归完成（455 通过，1 项既有跳过）。原客户端配置 / 凭据指纹不变，临时配置凭据已清理，正文额外留存保持关闭。真实企业账号、临期 Token 刷新、图片与全部模型遍历尚未实测，实际积分扣费未知。上游模型与登录接口可能改变，异常时应保留错误再针对当前接口检查。

重新执行会产生真实账号用量，须使用账号目录中仍保留的模型。协议和客户端探针命令为：

```powershell
python scripts/verify-live-workbuddy.py --model step-5-preview --out tests/artifacts/workbuddy/live-new
python scripts/verify-live-workbuddy.py --model step-5-preview --protocol responses --custom --out tests/artifacts/workbuddy/live-custom-new
python scripts/verify-live-clients.py --agent claude --agent codex --agent pi --agent dsh --workbuddy-model step-5-preview --timeout 150
python scripts/verify-live-clients.py --agent codex --codex-transport app-server --stream-only --workbuddy-model step-5-preview --timeout 150
```

协议参考来自 [magpie-community/plugins 的 WorkBuddy 适配器](https://github.com/magpie-community/plugins/tree/47fbf634e7718247a21ed414905fe77f67210e94/packages/workbuddy)，版本 `0.1.25`，提交 `47fbf634e7718247a21ed414905fe77f67210e94`。相关派生实现保留 [MIT 许可](WORKBUDDY-LICENSE.txt)。
