# 原生会话恢复与跨 Agent 交接

`agent_hub/sessions/handoff.py` 使用 Hub 的观测索引定位会话；根目录的 `session_handoff.py` 是旧导入的兼容入口。调用方只传入 Agent、原生会话 ID 和库存中的实例 ID，不能指定要运行的命令、项目目录或原生文件。查看正文沿用 `Store.session_detail` 的按需读取、脱敏与保留水位；交接草稿只在请求时生成，不会被 Hub 自动提交给模型。

## 接口与行为

- `recent_sessions(store, limit=30)` 返回最近的索引会话；范围是 Hub 已采集且含调用记录的会话，不代表客户端全部会话。
- `set_session_title(store, agent, sid, title)` 允许为已索引会话设置本机短名称；留空清除手动名称，不改写客户端会话。
- `preview_resume(store, agent, sid, inventory, instance_id=None)` 校验索引、原生身份、客户端目录及项目 cwd，返回可用性与将运行的参数；`resume_session` 在启动时重新校验并在 Windows 创建可交互的新终端。返回成功只表示启动了进程，不代表客户端已恢复。
- `draft_handoff(store, agent, sid, target)` 返回有待填写项和来源标识的原文节选，可能截断，必须由用户核对和编辑；它**不是智能摘要**，也不会补造任务结果。模型生成摘要的可选流程另见[交接摘要](handoff-summary.md)。
- `handoff_target_capabilities(inventory, target, instance_id=None)` 报告能否打开空的目标客户端、能否把确认后的内容作为初始信息立即发送。
- `launch_target(store, agent, sid, target, inventory, instance_id=None)` 打开目标 Agent，但不携带或发送交接内容。用户可在界面复制编辑后的交接内容并手动粘贴。
- `start_handoff(store, agent, sid, target, text, inventory, instance_id=None)` 只在用户显式选择立即开始后调用。它再次验证原生身份和项目 cwd，将确认后的交接正文放入 Hub 数据目录下的临时文件；CLI 参数只含文件路径，不含正文。支持 Windows 上经过参数核对的 pi 1.4.0、Claude Code 2.1.312、Codex CLI 0.165.0 和 OpenCode 2.2.1。pi 通过 `@file` 直接提供文件内容；其他三端使用短初始指令让 Agent 先读取文件。文件在目标进程退出后由 Hub 删除，继承数据目录的系统访问权限；Hub 先退出或崩溃则可能残留。目标 Agent 仍可能因权限设置无法读取文件，或未遵循读取指令，需在终端确认；Hub 不改变其权限设置。

最近会话的 `titleSource` 标明标题来自手动命名（`manual`）、客户端原生命名（`native-title`）、原生日志用户消息节选（`user-message`）、网关请求消息节选（`gateway-user-message`）或未知（`unavailable`）。手动名称优先；否则优先使用可验证、符合清理水位的原生标题或原生用户消息，最后使用本机保存的网关短标题。Codex、Claude、pi、dsh 的日志和 OpenCode v2 数据库按当前已识别格式处理，不保证所有版本、记录或全部历史都能取得标题。系统模板、工具输出和单独的“继续”等通用文字不会自动作为任务名；缺少可信时间戳、记录不完整或可读文本时可能显示未知。节选不是模型摘要，不推断任务目标。

命名会在本机按需读取最多 30 个已索引会话的有限原生记录；内存缓存随来源和水位变化失效。根据用户选择，Hub 网关在观测采集开启时还会从新请求的用户消息中提取最多 60 字及省略号，脱敏后作为标题保存到观测 SQLite，而不保存完整对话；这与额外正文留存开关无关。已保存的自动短标题最多保留一万条；自动和手动标题均遵循观测保留期限，清空观测记录时删除，清空前开始的在途请求不会将标题写回。关闭采集后不再自动捕获。旧网关用量记录不含消息，不能凭它们还原标题，可在列表手动命名。网关标识仅能证明对应请求，不代表存在可恢复的原生会话；没有关联原生来源时界面会说明并禁用原端继续。

列表中的节选可能包含个人文字，已知密钥和常见凭据格式会尝试脱敏，但不能保证识别所有敏感内容。普通原生日志只读取开头及末尾各最多 1 MiB，压缩日志只读取开头，因此大量记录或单条超长记录可能取不到标题；读取故障显示未知并可在之后重试。不会为命名调用外部模型。

不通过 `.cmd`、`.bat`、PowerShell 或 `cmd.exe` 作为中间 shell 传递用户路径和正文。库存中无法直接启动的实例会明确提示选择其他实例。客户端源文件或 cwd 被移动、身份变化、路径超出原生目录时拒绝启动；Claude 子会话的复合标识不会被误当成可恢复的主会话。

## 能力证据与尚未验证的部分

以下为本机 CLI 帮助和项目解析器层面的确认；尚未运行真实交互会话进行恢复验收。多种客户端和桌面版本可能使用不同格式，安装升级后需要重新核实。

| Agent | 已确认的 CLI 参数 | 当前自动动作与边界 |
|---|---|---|
| Claude Code 2.1.312 | `--resume <id>`、交互模式位置参数为初始消息；帮助提及 `--desktop --resume <id>` | 在新终端恢复；可用初始消息要求读取交接文件，桌面恢复尚未集成验证 |
| Codex CLI 0.165.0 | `resume <SESSION_ID>`、交互模式位置参数为初始消息；`-C` 控制工作根目录 | 在新终端恢复；可用初始消息要求读取交接文件，桌面恢复尚未集成验证 |
| pi 1.4.0 | `--session <path\|id>`，`@file` 作为初始信息 | 通过验证后的原生路径恢复；源码的 `processFileArguments`、`buildInitialMessage` 和交互模式的 `session.prompt(initialMessage)` 支持初始发送；仍未真实请求上游或实测终端交互 |
| dsh 0.2.1-rc.3 | 顶层帮助给出 `dsh tui --resume <session>`；[官方 CLI 说明](https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/README.md)说明具体参数属于 profile | 代码在识别到已安装的受支持 TUI profile 与可直接执行的 CLI 时，提供 `--profile tui --resume <id>`；本机 `tui` profile 不存在，尚未真实验收 |
| OpenCode | [官方 CLI 说明](https://dev.opencode.ai/v2/docs/cli/commands/)提供 `mini --session <id>` 和 `mini --prompt`；[2.2.1 的交互队列源码](https://github.com/anomalyco/opencode/blob/v2.2.1/packages/tui/src/mini/runtime.queue.ts)将初始输入提交为消息 | 代码对可运行的 v2 CLI 提供 mini 交互恢复，对 2.2.1 支持用初始消息要求读取交接文件；本机命令不在 PATH，npm 包中的可执行文件启动失败，尚未真实验收 |

pi 的文件参数转换已用本机安装的模块和独立临时样本执行验证，证明文件内容进入初始消息，不证明实际模型回复或工具执行。Claude、Codex、OpenCode 的临时文件读取路径使用真实启动的隔离假客户端验证；假客户端能读到样本，正文不在参数中，这不等于真实模型一定会读取文件。隔离单元测试还覆盖索引、脱敏、身份和路径校验、可见控制台参数、默认不发送与临时文件清理。`python -m unittest discover -s tests -p test_session_handoff.py -v` 仅使用合成记录，不启动真实用户会话、不调用上游。
