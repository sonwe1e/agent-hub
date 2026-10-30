# Codex / ChatGPT 与 ZCode 订阅共享

Hub 可以复用本机当前登录的 Codex ChatGPT 订阅和 ZCode 当前选中的订阅，供同一台机器的 Claude Code、Codex、pi、dsh 和 OpenCode 使用。请求经本机 Hub 网关发送，使用期间保持 Hub 运行。账号实际可用的模型、额度和服务限制以上游为准。

ZCode 个人 Coding Plan 已用真实账号验证。团队 Coding Plan 和 Start Plan 的账号选择、请求和额度处理通过了隔离模拟测试，**尚未使用有对应权益的真实账号验收**；上游行为可能不同。

## 接入和停用

1. 先在 Codex 中登录 ChatGPT，或在 ZCode 中登录并选择个人、团队 Coding Plan 或 Start Plan。打开一次对应应用，确保本机模型目录已更新。团队套餐需要在 ZCode 中已有该团队项目使用的密钥；Hub 不创建密钥。
2. 启动此版本的 Hub，在「设置」的订阅卡片选择「读取模型并接入」。这会读取当前账号和模型并保存 Hub 供应商，不会立刻更改客户端模型配置。
3. 在 Agent 详情中选择该供应商和模型，生成预览，核对后应用。旧会话可能继续使用原配置，需要新建会话或重启客户端。可用事务恢复之前的客户端配置。
4. 在卡片中「停用共享」会停用 Hub 供应商，不退出原应用登录。已经指向网关的客户端要通过预览重新选模型或恢复配置。

在 ZCode 切换账号、套餐、组织或项目后，回到卡片点击「刷新模型并恢复服务」。Hub 会校验当前选择与已接入的账号绑定；不一致时拒绝发送，避免把请求交给另一个套餐。新套餐的可选模型来自其当前目录，旧模型元数据不会因此被删除。

Codex 选择 ZCode 时，需要在 `~/.codex/config.toml` 顶层设置 `web_search = "disabled"`，因为 ZCode 当前适配器不实现 Codex 的服务端网页搜索。切换预览会提示，Hub 不自动改这个全局设置。项目配置、命令行或组织策略也可能覆盖它；也可在单次命令中使用 `codex -c 'web_search="disabled"'`。

## 账号状态和用量

- 卡片显示的最近调用、最近成功及最近失败或中断来自 Hub 本机观测索引，不是上游账号健康检查。索引可能有采集延迟，采集暂停时可能不完整，且历史线路记录不区分之前使用过的账号。
- 点击「查询额度」才读取上游快照，并显示来源、查询时间及上游提供的窗口；该快照只对应查询时的账号，不自动轮询。未知或上游未提供的数字不会显示成零或免费。上游接口和套餐字段可能变化，查询失败会显示错误。
- Codex 查询使用 Codex 客户端使用的账号用量接口；ZCode 使用所选 Coding Plan 或 Start Plan 的用量接口。页面显示的是查询时的额度快照，不是最终计费账单。ZCode 的窗口含义可参考[官方用量说明](https://zcode.z.ai/en/docs/usage-stats)。

## 登录与凭据

- Codex：每次请求只读当前 `auth.json` 中的 access token 和账号标识；Hub 不复制、写入或刷新登录文件。access token 过期时，在 Codex 续期或重新登录后重试。模型目录使用 Codex 本地缓存的客户端版本访问账号目录。此实现复用 Codex 的内部登录与后端接口，并非独立应用授权流程；上游变更可能使其失效。
- ZCode：只读当前选择、加密凭据和本机模型目录，支持迁移后的数据目录。个人套餐可读取已有密钥，或按本机保存的位置向上游只读获取现有项目密钥。团队套餐只读取当前组织和项目中已有的 `zcode-team-api-key`；不存在时提示先在 ZCode 中使用该团队。Start Plan 使用当前 ZCode 登录并依据上游返回的有效套餐额度筛选可用模型。Hub 不创建、修改或持久化订阅密钥，也不刷新或修改 ZCode 登录文件。
- Hub 供应商配置保存随机本机网关密钥和不含明文账号信息的账号绑定，不保存订阅密钥或 OAuth refresh token；上游凭据仅在内存中使用。请求共用 HTTP / SOCKS5 / SOCKS5H 代理设置。日志会对已知凭据脱敏；启用额外正文录制时仍应考虑业务文字的隐私。

ZCode 凭据格式、团队密钥读取及 Start Plan 的客户端兼容资料参考 [magpie-community ZCode 适配器](https://github.com/magpie-community/plugins/tree/main/packages/zcode)，相关许可见 [ZCODE-LICENSE.txt](ZCODE-LICENSE.txt)。Start Plan 请求会在用户系统指令之外加入该适配器所需的 ZCode 兼容提示；因此两者的指令可能相互影响，实际行为仍需用可用的 Start Plan 账号验证。

## 协议能力与限制

- Codex 订阅上游为 Responses 流；其他客户端的 Chat / Messages 在本机转换，支持文本、客户端执行的工具调用、工具结果和常见图片输入。完整历史需由客户端提交；不支持 `previous_response_id`、后台请求和 compact。原生 Responses 的 `max_output_tokens` 会移除，因为本次验证的 Codex 后端拒绝该字段；Chat 的部分采样参数不保证一一对应。Anthropic 的历史 thinking / 加密块无法安全迁移，会提示新建会话；Codex 的推理摘要也不伪造 Anthropic 签名。
- ZCode 上游使用 Anthropic Messages。Claude 的 Messages 走该格式；Chat / Responses 客户端在本机转换，支持文本、多轮历史、工具调用、包含图片的工具结果及常见图片输入；可见的 Responses 推理摘要作为标记过的历史摘要保留。非 Anthropic 请求未提供输出上限时，网关使用 8192；不支持 `previous_response_id`、后台请求、compact 或服务端网页搜索。加密推理和其他无法安全迁移的工具结果会报错。客户端执行工具以外的服务端工具不受支持。
- HTTP 成功不代表流完整：上游错误、截断、额度限制和客户端中断分别记录，订阅请求不自动重试。模型和工具协议持续变化，不支持的内容会报错。

## 验证范围

2026-10-28 用本机真实账号和隔离客户端配置验证：Codex ChatGPT 的 `gpt-6-luna`、ZCode 智谱个人 Coding Plan 的 `GLM-5.3-Flash` 分别通过 Claude Code、Codex、pi、dsh 与 OpenCode 完成随机文件读取工具往返；ZCode 的 Codex 验证显式关闭网页搜索。另通过三种网关协议各做了多轮历史和工具往返；在打包网关中，两者都能读取一段包含并行和连续工具结果的合成历史；两个账号的额度接口只读查询成功。ZCode 的图片测试成功；Codex 的 `gpt-6.1-sol` 成功识别测试图片，`gpt-6-luna` 对相同红色图片有时答错，直接请求上游也复现过答错。单次图片识别不证明其他图片或模型均可用。

团队 Coding Plan 和 Start Plan 只有合成凭据和模拟上游的回归证据；没有可用对应账号和额度，尚未完成真实请求、额度接口或模型可用性的验收。原始 Codex 和 ZCode 登录文件在真实测试后保持不变，隔离网关账本未包含订阅凭据。

再次运行真实测试会消耗账号额度：`python scripts/verify-subscription-conversations-live.py --provider codex --provider zcode`；也可分别运行 `scripts/verify-codex-subscription-live.py` 或 `scripts/verify-zcode-subscription-live.py`。脚本使用隔离网关与合成输入，不修改原登录；常规回归不触及真实账号。
