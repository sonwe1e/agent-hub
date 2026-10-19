# 观测依据与实现范围

核对日期：2026-10-17。参考 Magpie main 提交 `dccfefdf722ea25f5cf2be1798d5686e43b89fbd`；实际网关为本仓库 `gateway_server.py`，不依赖 Magpie 程序，见 [网关运行与隐私](gateway.md)。

已核对 [usage/page.js](https://github.com/yetone/magpie/blob/dccfefdf722ea25f5cf2be1798d5686e43b89fbd/internal/gui/assets/plugins/usage/page.js)、[context.go](https://github.com/yetone/magpie/blob/dccfefdf722ea25f5cf2be1798d5686e43b89fbd/internal/gui/context.go)、[gateway-sessions.md](https://github.com/yetone/magpie/blob/dccfefdf722ea25f5cf2be1798d5686e43b89fbd/docs/subsystems/gateway-sessions.md)、[reference.md](https://github.com/yetone/magpie/blob/dccfefdf722ea25f5cf2be1798d5686e43b89fbd/docs/reference.md)，以及网关 ledger、routing、prompt、Codex 原生透传、pi/dsh 连接与正文录制源码。

当前参考 Magpie 采用「网关实际调用 + 原生会话」方式，SQLite 保存统一索引。转发、SSE 解析、计时、用量、结构估算、正文代次与清理均由 Hub 自行维护；历史 Magpie 账本只读兼容。原有模型库、连接覆盖、模型同步、方案、预览、事务备份/恢复仍是客户端配置写入的唯一实现。

| 信息项 | 来源 | 采集条件 | 精度与缺口 |
|---|---|---|---|
| 请求与实际线路 | Hub `usage.jsonl` 的 route_id、provider、req、served、response_id | 经过本机自有网关 | provider 为实际转发的配置连接；模型请求与返回分列。各 Agent/连接独立前缀，原生透传不推测账号或 Hub 供应商身份 |
| 原生调用 | Claude message.id；Codex token_usage_record.response_id；pi message.responseId；dsh replayState.response.responseId | 默认读取各客户端原生日志，包括 Codex archive、dsh 最新代 zstd | 无网关时只有原生局部视图；dsh 原生账号无 responseId 时保留原生 seq 身份，不凭时间合并 |
| 最终结果与重试 | Hub 的协议完成事件、HTTP、流异常；历史 handed_on / tries | 请求已结束，账本可读 | 当前无网关自动重试，每条 HTTP 请求一次上游尝试；客户端重发无可靠关联 ID 时不猜逻辑重试关系。截断 SSE 为中断，HTTP 200 不独立证明完成；Codex 无 Content-Type 的 SSE 也可识别 |
| Token、缓存 | 网关 in/out/cache_read/cache_write/reasoning；原生 usage | 上游或客户端记录字段 | 新输入统一不含缓存；原生 Codex 输入先减明确 cacheRead。单次上下文为输入总量，累计 Token 独立；缺失字段不是 0。失败全零初始化值不作为实际零用量 |
| 尝试用量 | 同一 route_id 的 billable handed-on 与最终记录 | 各尝试有用量 | 累计用量包含已知尝试消耗，单次上下文保持最后一次输入；缺少尝试用量/价格时不把总成本当完整实测 |
| 响应耗时与首内容等待 | 网关 ms、ttft_ms、first_text_ms、flow_ms；原生 durationMs | 实际流式请求经过网关 | 首内容含推理/工具，与首文字分列；速度沿用 Magpie DecodeOf 的解码窗口、隐藏推理及突发过滤，不能测则未知；平均值显示样本数 |
| 费用 | 精确 Agent/连接模型单价，或客户端原生参考成本 | 价格与所需用量分项齐全 | USD 参考估算，不是实际扣费或订阅余额；缺价不免费，不用其他连接同名模型的价格。网关未知特殊成本来源尚未自动计价 |
| 对话与主子关系 | 客户端原生 session ID、cwd、消息、工具；网关显式 session/native_session/subagent | 原生日志或请求头给出可靠标识 | 响应 ID 可补回同一次调用的原生会话，不使用时间、文本或 Magpie 的内容摘要关联。当前 dsh 网关主响应能合并；无 session 的辅助调用保留未关联 |
| 两源去重 | Agent + 客户端实际 response/message ID | 两端均保留 ID | 同一个 ID 只计一次，网关补充线路/计时，原生补充窗口/会话/工具。未知产品名只有匹配唯一原生 response ID 才纳入四 Agent；无 ID 不强制合并，来源缺口保留 |
| 对话内容 | 已有原生日志按需读取；可选 Hub gateway-conversations | 原生文件可读，或额外正文明确开启且未过期 | 原生优先。网关回退显示每次 exchange，输入可能含重放历史，不作为新增消息数。脱敏与长度限制可能截断 |
| 单次上下文与变化 | 网关输入总量或原生 usage；原生有效窗口/网关模型目录窗口 | 有用量与可靠窗口来源 | 与整段累计量分开。原生窗口与目录参考窗口有不同标签；未知不填 200K。上下文点链接到同一个请求 ID |
| 上下文构成 | Hub routing.prompt.parts | 路由详情尚可读 | 系统/工具定义/消息/工具结果分组，不从文字猜文件/记忆。ASCII 约 4 字符/Token、非 ASCII 约 1 字符/Token，再按实测总量校准；分项仍标估算。不留正文片段名称，不做健康评分 |
| 压缩与整轮事件 | 原生 compact/compaction、Codex task_complete/task_failed/turn_aborted | 客户端记录 | 不把整轮 TTFT/耗时充作单响应计时，不从上下文下降猜压缩 |
| Plan、额度、余额 | Codex 原生 rate_limits 快照 | 原生明确提供且带时间 | 是历史快照，不是实时余额；其他账号/厂商额度未接入。订阅用量不等于 Token 参考费用 |
| 保留、暂停和清理 | Hub 设置/水位；自有网关锁与录制代次 | 本地服务与本地目录 | 默认自动索引、用量不自动过期、额外正文关闭；正文 7 天/256 MiB，路由 30 天/200 MiB。暂停 Hub 导入不关闭转发账本；清空索引不删除原文件或整个网关账本。停止网关先恢复直连 |

原生目录：Claude `projects/**/*.jsonl`；Codex `sessions` 与 `archived_sessions`；pi `agent/sessions`；dsh 最新 `session[.vN].jsonl[.zstd]`。支持相应 CLAUDE_CONFIG_DIR、CODEX_HOME、PI_CODING_AGENT_DIR、DSH_HOME。解析器版本包含在来源指纹中，升级后重建有变化的索引。未支持的平台/版本、远端 HOME、客户端未记录内容和不经网关的网络活动仍可能缺失。

本轮自有网关验证：Claude 2.1.299 API 线路，Codex 0.163.0 ChatGPT CLI 与 app-server 完整工具轮次，pi 1.3.0 自定义线路，dsh 0.2.0-rc.2 桌面附带 CLI 自定义线路及原生账号通过。pi 内置 OAuth 本轮刷新以 invalid_grant 拒绝，未通过；Claude OAuth 无缓存未验证。记录与截图见 [validation.md](validation.md)。桌面 GUI、重新授权、强制断网恢复、跨版本兼容不冒充已验证。
