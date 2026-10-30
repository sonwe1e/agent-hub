# Hub 自有本地网关

Agent Hub 自行实现并维护请求转发与观测，不下载、启动或依赖 Magpie。Magpie 仅作为信息能力、默认设置和采集口径的参考。此前采用官方无界面引擎的实现已经替换，旧验证记录标为历史记录；旧账本仍可读取，不需要运行旧引擎。

实现位于 `gateway_server.py` 与 `gateway_runtime.py`；zstd 请求/响应解压使用 `zstandard`，读取 ZCode 加密凭据使用 `cryptography`。没有额外 Go 程序、npm 服务或独立应用依赖。运行状态返回 `engine=agent-hub`、`version=hub-2`，随 Hub 一起维护。Codex／ChatGPT 和 ZCode 使用各自显式适配器，见[订阅共享说明](subscription-sharing.md)；普通 API 连接保持原协议透传。WorkBuddy 订阅反代已取消，旧供应商不会导出为网关连接；旧客户端需切换其他连接。过去的实现与验证保留在 [WorkBuddy 历史说明](workbuddy.md)，其中共用的协议转换模块仍供 Codex 和 ZCode 使用。

## 启动、接入和恢复

默认地址 `http://127.0.0.1:8789`。设置的「Hub 本地网关」可以启动/恢复组件，预览接入已启用 Agent，再应用配置。网关运行与配置已接入分别显示；只启动网关不改客户端配置。已有会话可能保留原线路，应用后重启或新建会话。

配置继续使用现有预览、源文件指纹、事务写入、备份与恢复。模型库保存原连接和模型 ID，仅输出给客户端的模型增加精确连接前缀。取消接入通过同一事务恢复真实连接及原 Codex `openai_base_url`；外部改动触发冲突保护。已停用的同步目标若仍指向网关，会阻止停止，避免把客户端留在失效地址。

只绑定回环地址；端口被占用会失败，不接管或终止其他进程。Hub 启动 Python 子进程，Windows 使用 Job Object 保护父进程异常退出，监控恢复意外退出的自有子进程。源码运行直接执行 `gateway_server.py`；打包 exe 内没有脚本文件，子进程改走 `AgentHub.exe --gateway-worker` 隐藏入口（与 `--compile-worker` 同一模式），网关依赖随 exe 捆绑，`--selftest` 会检查 `zstandard` 是否在包内。正常退出先停止接收新请求，给在途请求最多 5 秒收尾，再结束自有子进程。长请求在退出时可能中断，恢复直连应先应用配置。

上游转发、订阅认证、模型目录、额度查询和推理请求使用同一套代理选择：优先读取 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 与 `NO_PROXY`，没有环境配置时兼容系统代理设置。支持 HTTP 代理（含认证）、SOCKS5 和 SOCKS5H（后者由代理解析域名）；不支持的代理协议或无效配置会报告错误，不会悄悄改走直连。`NO_PROXY` 明确匹配的地址按用户设置直连。使用其他形式的透明系统代理时仍由系统决定网络出口。

## 转发范围

| 模式 | 实际处理 | 边界 |
|---|---|---|
| Claude Code 自定义连接 | Anthropic Messages 原协议转发；key 只用于对应连接 | 本机无原生 Anthropic OAuth 缓存，原生 OAuth 未验证 |
| Codex 自定义连接 | OpenAI Responses 原协议转发 | 不把 Chat Completions 连接当作 Responses 使用 |
| Codex 原生登录 | `openai_base_url` 指向本机 `/backend-api/codex`；Bearer/account 头仅在内存中透传到官方后端 | 不复制/修改 auth.json，不接管登录刷新；WebSocket 返回 426 后客户端回退 HTTP SSE。原生额外 MCP/遥测不冒充本网关覆盖 |
| pi 自定义连接 | 按连接的 Chat Completions / Responses / Messages 协议转发，保留会话亲和头 | auth.json 的原连接 key 若覆盖本机 token，仍按同一连接验证，不接受其他连接 key |
| pi 内置 OAuth | 原客户端直连，Hub 读取原生日志 | 最新复测服务端返回 invalid_grant，未通过；历史成功不代替本轮结果 |
| dsh 自定义连接 | 原协议转发，验证覆盖 user、imported、profile 层 | 不注入当前 dsh 不支持的 pi compat 参数 |
| dsh 内置账号 | 原客户端直连，Hub 读取原生日志 | 本轮 DeepSeek 账号工具与流式验证通过 |
| OpenCode 自定义连接 | 普通供应商保持直连；订阅共享走专用本机入口 | OpenCode v2 原生日志可只读索引；Codex 与 ZCode 订阅已验证真实 OpenCode 工具往返 |
| Codex / ChatGPT 订阅 | 只读当前 Codex 登录，Responses 上游与本机协议转换 | 不自动刷新登录；限制和验证见[订阅共享](subscription-sharing.md) |
| ZCode 订阅 | 只读当前 ZCode 账号，支持个人、团队 Coding Plan 和 Start Plan；本机转换至 Anthropic Messages | 个人账号已真实验证；团队和 Start Plan 只有模拟验证，限制见[订阅共享](subscription-sharing.md) |

只转发明确配置的连接和模型，不引入备用供应商。普通 API 连接的上游响应与工具 JSON 逐块原样传给客户端，旁路观测字段解析失败不改写响应；订阅共享的显式适配器按上游协议转换。订阅卡片可手动查询 Codex 与 ZCode 的上游额度快照；账号池和自动轮询额度尚未实现。普通转发的 gzip/zstd 可解压观察，未知压缩仍原样转发并保留缺口。

Codex 本次上游缺少 Content-Type 的 SSE 已通过流格式识别解决。只有协议完成事件/完整非流 JSON 才确认成功，HTTP 200 本身不足。无完成标记的截断 SSE 标为中断。用精确响应 ID 与原生日志合并；缺失 ID/会话不靠时间猜测。

每个进入网关的 HTTP 推理请求是一条请求。客户端自行重发且没有可靠关联 ID 时，不能确认属于同一次逻辑重试，保留为独立请求并说明缺口。历史 handed_on / tries 按明确关系聚合，最终成功不会重复记最终失败；WorkBuddy 停用前的 11153 兼容重发属于历史记录，不是当前可用功能。

## 数据和隐私

- 当前目录 `data/gateway/config/agent-hub/`；`config/magpie/` 仅用于历史兼容读取。不读取用户自己的 Magpie 配置。
- 配置导出四个网关 Agent 明确配置且凭据可解析的连接，另为 OpenCode 导出订阅共享专用连接；OpenCode 的普通供应商仍直连。配置受本机文件权限和 Git 忽略规则保护。原生认证头只存在内存，不进入账本、路由、正文或诊断。配置 key、常见认证字段、JWT、URL 查询脱敏。
- 默认记录必要用量账本，不额外录制完整正文。根据用户选择，观测开启时会将网关请求的用户消息提取为本机脱敏短标题，保存在观测数据库而非追加账本；短标题仍可能含个人文字。暂停采集会停止捕获标题，不停止转发及原始账本。清空索引会删除短标题并以水位和采集代次阻止在途旧请求恢复，不删除原生日志或整个原始账本。
- 正文明确开启后最多 7 天/256 MiB。关闭使用私有控制接口，在子进程锁内增加同意代次并清理副本；旧代次在途响应不能在再次开启后落盘。控制接口使用单次启动随机密钥和临时回环端口，不暴露给 UI。
- 正文观察上限与字符串长度限制会截断并标记 cut。默认构成只保存系统、工具定义、消息、工具结果结构估算，不留正文片段名称，不从文字推测文件/记忆。ASCII 约 4 字符/Token，非 ASCII 约 1 字符/Token，再按实测总量缩放，不是精确分词。
- 路由详情最多 30 天/200 MiB，用量账本默认不自动过期。追加后检查上限。窗口优先取原生运行时声明，自有网关没有额外目录窗口推断。模型目录不可达时，已有 Codex 模型缓存可用于目录回退，不复制认证。

2026-10-17 的验证只覆盖隔离配置和隔离数据集，见 [validation.md](validation.md)；不代表当前日常客户端都已完成网关接入验证。Hub 不需要 Magpie 程序，旧的下载引擎已删除，用户原配置与历史未删除。
