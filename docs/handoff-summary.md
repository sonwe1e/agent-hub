# 模型生成交接摘要

可以选择一条 Hub 已配置、已启用且包含所选模型的线路，用模型整理交接草稿。资料选择、编辑、线路选择和发送预览都在生成之前；只有点击生成才发送一次请求。结果明确标为模型生成草稿，需要检查任务目标、已完成工作、测试结果、未知项及用户约束。资料节选或原会话不完整会限制摘要质量；提示模型区分事实与推断不能保证它完全正确。

生成草稿和交付给目标 Agent 是两个独立动作。生成本身不会启动目标客户端、执行历史命令或自动发送交接。生成后仍可以修改；交接是否立即开始取决于交接页面上的选择。

调用仅进入 Hub 自有网关选择的连接，之后由该连接发送给它对应的上游。普通 API 连接使用原有 chat、Responses 或 Anthropic 协议；Codex／ChatGPT 和 ZCode 使用各自的订阅适配器。WorkBuddy 订阅反代已取消，即使旧配置仍存在，也不会成为摘要候选线路。连接必须由 Hub 导出；pi 内置 OAuth、dsh 内置账号等没有通过网关导出的原生直连不在候选中，其他尚未支持的直连也不会被悄悄替换。候选只证明配置存在，不证明账号在线、套餐可用或模型一定能生成摘要。模型调用可能消耗费用或订阅额度。

预览显示完整将提交的材料、摘要指令、线路和上游信息。预览完成后，材料、模型、网关、线路地址或订阅账号发生变化都必须重新预览。后台传入的订阅身份先用进程临时密钥计算 HMAC 并纳入预览校验，不返回单独的身份或身份哈希。进程重启后需要重新预览；ZCode 当前账号或套餐发生变更时，原有网关适配器也会按绑定状态检查。界面不显示密钥，失败信息不回显上游正文。账号在服务端最后一次核验之后、网关读取凭据之前仍可能被其他程序并发切换；当前校验无法保证消除这一极短窗口。

本模块不持久化交接正文或模型草稿。请求不包含原生会话标识，现有网关不会为这次请求保存网关会话正文副本，即使正文录制已开启；调用的元数据、用量和估算结构仍可能按网关正常机制记录。Responses 请求声明 `store: false`，实际供应商是否保存或使用请求内容由供应商与套餐决定，不能仅凭该字段保证。为让你判断所选材料是否适合发送，预览应展示原文；脱敏不能保证覆盖所有私人信息。

## 接入契约

`list_summary_routes(cfg)` 返回候选 `[{provider, providerName, agent, model, protocol, destination}]`。`preview_summary(cfg, selection, material, account_identity=None)` 检查配置并返回原文、系统指令、目的地、无密钥的选择及 `digest`；此步骤不执行网络请求。Codex、ZCode 订阅要求后台传入当前账号与套餐的稳定身份，取不到时拒绝预览；普通 API 连接不需要。

点击生成后调用 `generate_summary(cfg, selection, material, digest, transport=None, timeout=60, account_identity=None)`；后台须重新读取当前账号并传入 `account_identity`，`selection` 只含 `provider`、`agent`、`model`。`digest` 绑定材料、系统指令、模型、协议、网关端口、线路地址及订阅账号，并不充当授权凭据。输出 `{draft, generatedBy: "model", selection, completionStatus, notice}`，完成状态为 `complete`、`truncated` 或 `unknown`。默认 HTTP 客户端只连接由 `gateway_url(cfg)` 生成的 `127.0.0.1` 地址，不继承系统代理、不跟随重定向；`transport` 仅用于注入测试。

本地测试：`python -m unittest discover -s tests -p test_handoff_summary.py -v`。普通三协议以及 Codex、ZCode 订阅适配器经真实 Hub 网关和本机模拟上游验证；测试也检查旧 WorkBuddy 配置不会成为摘要线路。WorkBuddy 停用前的模拟验证为历史记录。

另用本机 Codex／ChatGPT 订阅的 `gpt-6-luna`，经独立临时网关对一份合成材料执行过一次摘要请求；结果保留了样例中的目标、约束、模拟测试结果与未知事项，返回完成状态。该次临时账本检查未发现凭据或正文副本。此结果只支持该线路与模型在这一样例的表现，不证明其他账号、模型或材料均可用。可用 `python scripts/verify-handoff-summary-live.py --model gpt-6-luna` 重复验证，每次会发送一次合成材料并消耗订阅额度。
