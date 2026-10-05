# Agent Hub — 统一 Provider 配置

## 这是什么

机器上有多个 coding agent（pi / mcode / zcode / dsh / cc-switch / omp 等），
每个都有自己的 provider 配置格式。本目录提供**单一事实来源**与**同步引擎**，
改一处即可同步到所有 agent。

## 目录

| 文件 | 作用 |
|------|------|
| `providers.json` | 统一配置，可通过工作台维护，无需手工编辑 |
| `hub.py` | 同步引擎 |
| `server.py` | 本地 HTTP API 与静态页面服务 |
| `ui/` | 模型库 / 供应商 / 平台同步工作台，无前端构建依赖 |
| `launch.py` | 本地服务复用与桌面窗口启动器 |
| `gui.py` | 保留的旧版 Tk 桌面界面 |
| `hub.cmd` | Windows 命令入口 |
| `state.json` | 变更检测哈希（自动生成） |
| `backups/` | 每次写入前的自动备份 |
| `scripts/verify-modelsync-e2e.py` | 在真实客户端配置的副本上跑一次同步，检查各适配器产物（不碰真实 HOME） |
| `providers.example.json` | 配置模板（密钥字段已脱敏），复制成 `providers.json` 后填自己的值 |

> **`providers.json` 不进版本库。** 它含明文密钥，已在 `.gitignore` 里；
> 备份目录 `backups/`、变更哈希 `state.json` 同样忽略。首次使用请复制
> `providers.example.json` 为 `providers.json` 再填密钥。

## 快速开始

本目录可以整体任意搬迁（hub.py / server.py / gui.py 按自身位置定位，不依赖用户主目录）。
搬迁后以下命令中的路径替换成实际所在目录即可。

```powershell
# 新版桌面工作台（推荐，Edge 独立窗口，无控制台）
G:\workspaces\agent-hub\hub.cmd gui
# 或双击  G:\workspaces\agent-hub\Launch GUI.vbs
# 没有 Edge 时自动使用默认浏览器

# 在默认浏览器打开相同的工作台
G:\workspaces\agent-hub\hub.cmd ui
# 旧版 Tk 界面仍可使用
G:\workspaces\agent-hub\hub.cmd gui-legacy

# 前台启动服务，便于诊断
python G:\workspaces\agent-hub\server.py --no-open
# 默认 http://127.0.0.1:8787/

# 命令行
G:\workspaces\agent-hub\hub.cmd status
G:\workspaces\agent-hub\hub.cmd push
G:\workspaces\agent-hub\hub.cmd sync
G:\workspaces\agent-hub\hub.cmd model deepseek deepseek-flash   # 多 agent 主模型
G:\workspaces\agent-hub\hub.cmd save-gold        # 备份当前模型列表为黄金模板
G:\workspaces\agent-hub\hub.cmd restore-gold     # 从黄金模板恢复模型列表
G:\workspaces\agent-hub\hub.cmd backup
```

## 新版工作台

桌面入口与网页入口共用同一套界面，不依赖 CDN、前端框架或构建工具。
启动器会复用新版本地服务；若默认端口被旧版服务或其他程序占用，会尝试后续端口，不会终止已有进程。

### 模型库

- 汇总所有供应商的模型，支持名称 / ID 搜索、供应商筛选、推理 / 图片 / 纯文本能力筛选。
- 添加与编辑通过独立表单完成，单次提交即保存；支持带 `/` 的模型 ID、ID 改名与重复检查。
- 编辑时保留 `cost`、`compat` 等高级字段，不再逐行重写整个模型列表。
- 支持跨供应商、跨页勾选与批量删除；全选仅作用于当前页，筛选外的已选项会明确提示。
- **删除模型仅影响本地配置，执行平台同步后才影响客户端。**

### 供应商

- 卡片展示连接地址、模型数量、密钥配置状态、启用状态与 Claude 兼容性。
- 在配置弹窗中添加 / 编辑连接，密钥默认隐藏，可临时显示。
- **检测模型**查询供应商远端列表：区分已添加与新模型，仅导入明确勾选的新项。
- 导入不删除已有模型，也不覆盖已有模型的手工参数。未勾选任何项时禁止导入。
- **删除供应商沿用现有引擎的立即清理逻辑，会影响客户端配置**，弹窗会单独说明；删除后建议再同步，确保所有平台对齐。

### 平台同步

- 勾选参与同步的平台，支持保存设置或保存后直接同步；未选择的平台不会写入。
- dsh 桌面版跟随 dsh，适配器只执行一次；Codex 的 ChatGPT 登录态仍由客户端自行管理。
- 可选择 Claude Code 激活的供应商，仅支持已启用且配置了 Anthropic 端点的供应商。
- 展示待同步状态、最近同步时间和逐平台成功 / 失败信息；失败不会伪装成全局成功。
- 「检测变更并同步」**不是从客户端导入**，仍以 Hub 为准覆盖已选客户端配置，执行前需要确认。
- 定向同步只更新实际成功目标的变更哈希；只有所有启用目标成功写入后才标记本地配置已对齐。

### 多 agent 模型同步

「多 agent 使用的模型」卡片选一次模型，同步后 pi、dsh 桌面版、Claude Code、Codex 一起改到同一个模型。
Codex 只写 `model_provider` / `model` 与 `[model_providers.agent_hub]` 段，ChatGPT 登录态由 Codex 自己维护。

| 客户端 | 主模型落到哪里 | 子 agent / 档位落到哪里 |
|--------|----------------|--------------------------|
| pi | `~/.pi/agent/settings.json` 的 `defaultProvider` + `defaultModel` | pi 没有子代理概念，只写主模型 |
| dsh 桌面版 | `profiles/desktop/cordis.patch.yml` 的 `agent-default-model` | 同文件 `subagent-model-selection-settings.allowedModels`（子代理可选模型） |
| Claude Code | `~/.claude/settings.json` 的 `env.ANTHROPIC_MODEL` | `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL` 与 `CLAUDE_CODE_SUBAGENT_MODEL` |
| Codex 桌面版 / CLI | `~/.codex/config.toml` 的 `model_provider` + `model` | 同文件 `[model_providers.agent_hub]`（base_url / wire_api / 授权）；`auth.json` 的登录态不碰 |

`providers.json` 里的结构（也可以用命令行 `hub model` 改）：

```jsonc
"modelSync": {
  "default": { "provider": "deepseek", "model": "deepseek-flash", "reasoningEffort": "medium" },
  "overrides": {
    "pi":  { "provider": "stepfun", "model": "step-5-preview" },   // 只改 pi
    "dsh": { "provider": "firework", "model": "glm-5.3" },
    "claude": {
      "provider": "deepseek", "model": "deepseek-v4-flash",
      "tiers": { "opus": { "provider": "openrouter", "model": "anthropic/claude-opus-5" } }
    }
  }
}
```

解析优先级（从高到低，`reasoningEffort` 只接受 `off/minimal/low/medium/high/xhigh/max`）：

1. `overrides.<平台>.tiers.<档位>` —— 界面上单独指定的档位；
2. `overrides.<平台>` / `modelSync.default` —— 平台级或全局选择；
3. 供应商自己声明的 `provider.agentModel`；
4. 该供应商模型列表的第一项。

几条刻意保留的语义：

- **只有「主模型」会被全局选择改写。** Claude Code 的 opus / sonnet / haiku / subagent 档位
  只在被显式指定、或供应商自己用 `claudeModels` 声明过时才会写；一个全局设置不会顺手把它们改掉。
- **Claude Code 跟随全局有前置条件。** 全局选中的供应商必须具备 Anthropic 端点，
  否则 Claude Code 保持自己的 `claudeModels`（`baseURL` 与密钥必须和模型配套，
  跨供应商套用模型名只会写出一个对方不认的模型）。跟随发生时，同步会把
  同步时按 modelSync 优先级解析实际 Claude 供应商，不隐式回写 Hub 配置。
- **没配置 `modelSync` 时不动客户端。** pi 的 `defaultProvider` / `defaultModel`、
  dsh 的 `agent-default-model` 都保持原样，只有点同步并配置过才接管。
- **手填的模型 ID 会被标记但照样写。** 不在供应商模型库里的 ID 在状态里带 ⚠，客户端可能拒绝。

命令行：

```powershell
G:\workspaces\agent-hub\hub.cmd model deepseek deepseek-flash                     # 全局主模型
G:\workspaces\agent-hub\hub.cmd model firework glm-5.3 --target dsh              # 只改 dsh
G:\workspaces\agent-hub\hub.cmd model deepseek deepseek-v4-flash --target claude --reasoning-effort high
G:\workspaces\agent-hub\hub.cmd push                                            # 再同步到各客户端
```

> `hub model` 只改 `providers.json`；必须再 `hub push`（或在工作台点同步）才写进客户端。

### 模型库删除保护

- 勾选跨页 / 跨筛选保留，但删除时只要选中项里有**当前屏幕上看不到**的模型，确认框会要求
  单独勾选「我确认删除当前筛选或页面上看不到的 N 个模型」，否则确认按钮不可用。
- 服务端在写入 `providers.json` 前校验：除本次请求声明的删除外，任何模型的消失都会导致
  写入被拒绝（`ModelLossError`，HTTP 409），不会静默落盘。
- 若某次写入仍让某个供应商的模型减少一半以上，会自动快照 `providers.json.drop-*` 留证。

### 操作反馈

- Ctrl+S 保存当前弹窗或平台设置，Escape 关闭弹窗；有未保存修改时先确认。
- 模型勾选后，Delete 可触发批量删除确认（输入框内不会误触）。
- 写入期间禁用重复提交与表单修改；错误保留输入，并给出明确反馈。
- 同时适配桌面与窄屏，支持键盘焦点、原生弹窗焦点约束与减少动画偏好。

旧版 Tk 界面仍保留原来的表格双击编辑方式，与新版共用 `providers.json`。

## 开始菜单

GUI 快捷方式位于个人开始菜单目录的 `AI 与模型\Agent Hub.lnk`，带专用图标，无控制台启动；可搜索 **Agent Hub** 打开。
快捷方式目录已按用途归类，重复入口归档到 `backups/start-menu/`；Windows 默认项和 Startup 保留。
**目录分类不会改变 Windows 11「视图：类别」的自动分组，也不会创建「已固定」文件夹。**
公共目录整理需要管理员权限，目前尚未应用；它同样不能修正系统自动类别。分类规则和本次撤回命令见 [scripts/START-MENU.md](scripts/START-MENU.md)。

## 安全

- 工作台每次修改配置前都会备份；备份名含微秒，连续操作不会覆盖前一次备份。
- 删除操作均需确认；清理大量模型时会显示额外警告，且筛选外的选中项必须单独勾选确认。
- **整文件写入前会校验模型数量**：除本次操作明确删除的模型外，任何供应商的模型变少都会拒绝写入。
- `pull` / `hub sync` / `hub watch` 会先重新读取磁盘配置再合并，不会用进程启动时的旧快照回写。
- 每次写 `providers.json` 若骤降（总量腰斩或某个供应商减少一半以上）会自动快照 `providers.json.drop-*`。
- `providers.gold.json` 是模型列表黄金模板，可用 `restore-gold` 一键找回；恢复时会保留模型已有的 `cost` / `compat`。
- API 拒绝跨站来源的写入请求；服务默认只监听 `127.0.0.1`，不要暴露到公网。
- `providers.json` 中密钥为明文，备份也包含密钥，不要共享配置与备份文件。

## Windows 运行环境

- `hub.cmd` / `Launch GUI.vbs` 会依次尝试 `%MIMO_PYTHON%`、PATH 上的 `python.exe`、常见安装目录、
  DSH 运行时自带解释器；PATH 上的 Microsoft Store 0 字节占位符会被语法探测淘汰，不是可用的 Python。
- 若都找不到，命令会给出明确提示而不是静默失败。显式指定：
  `setx MIMO_PYTHON "C:\Python312\python.exe"`。
- 命令行输出在 GBK 控制台下不会因 `✓` / `✗` 崩溃，无法编码的字符会退化成 `[OK]` / `[FAIL]`。

### pi 适配器写入内容

- `models.json` 中每个模型的 `input` 必须写成数组（如 `["text","image"]`），写成字符串会被 pi 判为 schema 错误。
- provider 级 `compat`（`maxTokensField` / `supportsDeveloperRole` / `thinkingFormat` 等）会下发；
  pi 侧已有的 `cost` / `compat` / `headers` 等字段在 hub 未显式配置时原样保留。

## 统一配置结构

```jsonc
{
  "providers": {
    "stepfun": {
      "name": "StepFun Step Plan",
      "baseUrl": "https://api.stepfun.com/step_plan/v1",
      "apiKey": "...",
      "api": "openai-completions",
      "agentModel": "step-5-preview",     // 可选：该供应商给 agent 用的默认模型
      "models": [ { "id": "step-5-preview", "contextWindow": 1000000, ... } ]
    },
    "firework":  { /* 火山网关，dsh 路由名 firework */ },
    "deepseek":  { /* DeepSeek 官方 */ },
    "openrouter":{ /* 聚合网关 */ },
    "tokenfree":  { /* 自定义 Anthropic 网关，见下 */ }
  },
  "modelSync": { /* 多 agent 模型选择，见上一节 */ },
  "removedProviders": { /* 已删除记录，防止复活 */ },
  "sync": { "targets": { /* 各 agent 启用开关 */ } }
}
```

## 自定义供应商接入

只提供 Anthropic 兼容端点、没有 OpenAI `/v1` 的第三方网关，用 `ANTHROPIC_*` 那组固定键表达不完。
因此 provider 支持三个**声明式透传**字段（均为可选，缺省时行为与以前完全一致）：

```jsonc
"tokenfree": {
  "name": "TokenFree",
  "baseUrl": "https://api.tokenfree.shop",
  "anthropicBaseUrl": "https://api.tokenfree.shop",   // 有它才会进入 Claude Code
  "apiKey": "...",
  "models": [],                                        // 可留空，纯 Claude 网关没有模型库
  "targets": ["claude"],                               // 只同步到 Claude Code

  "claudeModels": {                                    // 映射到 ANTHROPIC_MODEL / *_MODEL / SUBAGENT
    "model": "claude-sonnet-4-6",
    "opus": "claude-sonnet-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-sonnet-4-6",
    "subagent": "claude-sonnet-4-6"
  },
  "claudeEnv": {                                       // 原样写入 settings.json 的 env
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1",
    "ENABLE_TOOL_SEARCH": "true"
  },
  "claudeSettings": {                                  // 原样写入 settings.json 顶层
    "model": "claude-sonnet-4-6",
    "effortLevel": "high",
    "skipDangerousModePermissionPrompt": true,
    "includeCoAuthoredBy": false,
    "permissions": { "allow": [] }
  }
}
```

| 字段 | 作用 |
|------|------|
| `targets` | 数组。声明后该 provider 只进入列出的平台，不污染 pi / mcode / zcode / dsh。留空 = 进入所有平台 |
| `claudeModels` | `model` / `opus` / `sonnet` / `haiku` / `fable` / `subagent` → `ANTHROPIC_*` 环境变量 |
| `claudeEnv` | 任意环境变量，原样追加进 `settings.json` 的 `env` |
| `claudeSettings` | 任意顶层字段（`model` / `effortLevel` / `permissions` / …），原样写进 `settings.json` |

**切换 provider 时的收回语义**：`claudeSettings` 中的键，在切到另一个 provider 时，
只有上一个 provider 声明过、当前 provider 没声明的才会被删除——避免残留别家的 `model` / `effortLevel`。
你自己手工写进 `settings.json` 的键（`hooks` / `statusLine` / `modelSettings` 等）永远保留。
`env` 同理：hub 只动自己声明的键，未声明的 `env` 键不会被清掉。

web 界面在「供应商 → 配置」的「自定义接入」一节里可以编辑这三个字段（JSON 文本框，非法 JSON 会被拦截）。

### 在界面上切换 Claude 模型

「平台同步」页，选好 Claude 供应商后，下面会出现一个**模型选择器**：

1. 点「获取模型」→ hub 向该供应商的 `{anthropicBaseUrl}/v1/models` 查询可用模型列表；
2. 下拉里选一个模型（不在列表中的也可在供应商配置里手填）；
3. 点「保存平台设置」→ hub 把 `claudeModels` 的各档位（`model` / `opus` / `sonnet` / `haiku` / `subagent`）统一指向新模型，
   若该 provider 还声明了 `claudeSettings.model`，它会被一起改成同一个值，避免出现「切了模型但顶层 `model` 还指着旧型号」的漂移；
4. 再点「开始同步」写入 `~/.claude/settings.json`。

> `claudeModels.model` 与 `claudeSettings.model` 同时声明时，前者优先——它对应 `env.ANTHROPIC_MODEL`。
>
> 注意：这一步只改 hub 的配置，**必须再同步一次**才写进 Claude Code 的 `settings.json`。

## Provider ↔ Agent 映射

| hub key | pi | mcode | zcode | dsh / dsh 桌面版 | claude | codex |
|---------|----|-------|-------|----------------|--------|-------|
| `stepfun` | stepfun | stepfun | StepFun | stepfun | — (无 Anthropic 端点) | 不写入 |
| `firework` | firework | volcano | 火山 | firework | — (无 Anthropic 端点) | 不写入 |
| `deepseek` | deepseek | deepseek | DeepSeek | deepseek | `/anthropic` + 模型别名 | 不写入 |
| `openrouter` | openrouter | openrouter | OpenRouter | openrouter | `/api` + 模型别名 | 不写入 |
| `tokenfree` | tokenfree（无模型，留空） | tokenfree | — | tokenfree | `/v1/messages` + `claudeEnv` / `claudeSettings` 透传 | `/v1/responses` + `openaiApiKey` |

> 命名差异是各 agent 的历史习惯，同步引擎会自动映射。
>
> `codex` 是正式写入目标：只写 `model_provider` / `model` 与 `[model_providers.<providerId>]`，
> `auth.json` 里的 ChatGPT 登录态一个字都不动，所以界面显示的账号不受影响。
>
> 同一个网关给 Claude 与 Codex 发不同 key 时（模型集合不同），用 `apiKey` 给 Claude Code、
> `openaiApiKey` 给 Codex；hub 优先使用 `apiKeyEnv` 环境变量引用，其次 `openaiApiKey`，最后 `apiKey`。
> 当前 Codex 只支持 Responses。必须确认后声明 `codexWireApi: "responses"`，或 `api: "openai-responses"`；Chat Completions 网关会被拒绝。

## 新增供应商

界面上「供应商 → 添加」即可，不需要改代码：

| 字段 | 说明 |
|------|------|
| 供应商 ID | 各平台配置里的路由名（字母、数字、`_`、`-`） |
| Base URL / API Key / API 协议 | OpenAI 兼容端点；只提供 Anthropic 兼容端点时填「Claude 兼容端点」 |
| 启用 | 关闭后不进入任何平台的同步 |
| **同步到哪些平台** | 勾选目标平台；**一个都不勾 = 同步到所有平台**。例如只有 Anthropic 端点的网关只勾 Claude Code |
| **agent 默认模型** | 该供应商给 agent 用的默认模型 ID；「多 agent 模型」没有显式选择时用它兜底 |
| **Codex 专用 Key** | 同一个网关给 Codex 发另一个 key 时用（写进 `config.toml` 的 `experimental_bearer_token`） |

新增供应商进入 dsh 时，hub 会按 `<供应商ID>_API_KEY` 推导凭据引用名，并把密钥写进
`~/.dsh/.credentials.yaml` 的 `refs`；进入 Claude Code 需要配置 Claude 兼容端点；
进入 Codex 需要 `baseUrl`（Responses 兼容端点），hub 会把它写进 `[model_providers.<providerId>]`。

## Codex 桌面版 / CLI

需要 Python 3.11+；使用标准库 `tomllib` 校验输入及生成结果，不增加运行依赖。

- 在同步页显式选择「官方订阅」或「第三方供应商」，先预览，再应用。没有 `mode` 的旧配置安全地按官方模式处理，不会隐式切换第三方。
- 首次第三方切换前，请先在 Codex 中选择官方 provider、model、effort。Hub 只记录这三个字段的值及是否存在到 `~/.codex/.agent-hub-state.json`。恢复官方时一起恢复，缺省字段会恢复成缺省，第三方模型不会残留。
- 使用独立 `[model_providers.agent_hub]`，保留已有 `custom` 和其他 provider。名称冲突或外部更改会报错，不覆盖。恢复官方会移除本轮 Hub 写入的 provider 表。
- 第三方只接受 Responses。显式声明 `codexWireApi: "responses"`，或 `api: "openai-responses"`；不能把 Chat Completions 当成兼容证明。
- 优先用 `apiKeyEnv`（环境变量由用户在原生环境中设置，必要时重启客户端）。兼容现有配置时支持 `openaiApiKey` / `apiKey` 字面量，这些会落入配置和备份明文；官方建议优先环境变量引用。预览不显示凭据值。
- 第三方表设置 `requires_openai_auth = false`，不借用 ChatGPT 凭据。Hub 从不读取、写入、复制或删除 Codex `auth.json`、系统钥匙串，也不执行 login/logout。现有登录失效时由 Codex 自己重新登录。
- **仅管理基础 `~/.codex/config.toml`**。Codex 0.134+ 命名配置是 `<name>.config.toml`，由 `--profile` 选择；Hub 不触碰它们。项目配置、环境变量、CLI 参数及运行中的会话可能覆盖基础配置，因此应用成功不是线路/登录/付费套餐验证。旧 `[profiles.*]` 检测到后先要求迁移。
- 不读取安装版本或运行真实 Codex。兼容目标依据 2026-10-05 官方文档；启用后请在自己的客户端检查当前配置和选中模型。

官方参考：[配置字段](https://learn.chatgpt.com/docs/config-file/config-reference)、[profile 迁移](https://learn.chatgpt.com/docs/config-file/config-advanced)、[原生认证](https://learn.chatgpt.com/docs/auth)。

## Claude Code

- 写入 `~/.claude/settings.json` 的 `env.ANTHROPIC_*`（只改 env，保留 hooks / permissions / statusLine）。
- Claude 同步只写原生 settings.json，不打开或修改 cc-switch 数据库。
- 当前激活由 `providers.json → sync.targets.claude.activeProvider` 控制（默认 `deepseek`）。
- stepfun / firework 没有 Anthropic 兼容端点，不会写入 Claude Code。
- 自定义网关可用 `claudeEnv` / `claudeSettings` 透传任意 env 与顶层字段，见上一节。

## dsh 桌面版

- 与 dsh CLI 共用 `~/.dsh`。桌面 profile 在 `profiles/desktop/`。
- 同步写入：`settings.yaml`（Models 页用户层）、`settings.yaml.imported`、`cordis.patch.yml`、`.credentials.yaml`。
- `cordis.patch.yml` 里 `llm-pi-ai.providers` 已有该段才会更新；hub 不会凭空新建这一段。
- 配了「多 agent 模型」后，还会更新 `agent-default-model`（主 agent 模型）与
  `subagent-model-selection-settings`（子代理可选模型）；没配置时这两段保持原样。
- 新增供应商的凭据引用名按 `<供应商ID>_API_KEY` 推导并写入 `.credentials.yaml` 的 `refs`，
  历史四个供应商继续沿用 `FIREWORK_API_KEY` / `MCODE_DEEPSEEK_API_KEY` / `STEPFUN_API_KEY` / `MYOPENROUTER_API_KEY`。

## cc-switch 数据库写入约束

- `~/.cc-switch/cc-switch.db` 的 `providers.created_at` 是 **INTEGER（epoch 毫秒）**列。
  cc-switch 启动 setup 阶段按整数读取；写入 TEXT（例如 SQLite 的 `datetime('now')`）会让它直接
  崩溃：`数据库错误: Invalid column type Text at index: 5, name: created_at`，整个 app 打不开。
- hub 只写整数毫秒（`now_epoch_ms()`），且每次写入后校验：发现 TEXT 立刻报错，不会静默留下坏库。
- 万一因历史数据或其它工具写入 TEXT 导致打不开，运行：

  ```powershell
  python G:\workspaces\agent-hub\scripts\fix_cc_switch_created_at.py
  ```

  脚本会先备份到 `~/.cc-switch/backups/`，把 TEXT 转成整数毫秒并 VACUUM，再自检验证。

## 约定

- **Codex 的 ChatGPT 登录态**完全不在本工具管理范围：`auth.json` 不读不写，恢复官方模式会同时恢复 provider、model、effort。
- pi / dsh / claude / codex 是主要目标；mcode / zcode / omp / cc-switch 适配器保留，
  不主动写入。
- **qwen-token-plan-cn** 已过期并删除；qwen 系列模型仍可通过 firework（火山）使用。
- 每次写入前会把原文件备份到 `backups/`，可随时回滚。
- 修改 `providers.json` 的 `apiKey` / `baseUrl` / `models` 后，运行 `hub push` 即可全局生效。
- 需要自动同步时运行 `hub watch`，它会轮询各 agent 配置哈希，变更后重新对齐。
- 切换 Claude Code 激活 provider：改 `sync.targets.claude.activeProvider` 后 `hub push claude`。
- 多 agent 模型：`hub model <provider> <model>` 改配置，`hub push` 落到客户端。

## 测试

```powershell
# 后端与启动器回归（Python 标准库，无额外依赖）
python -m unittest discover -s tests -p "test_*.py"

# 多 agent 模型同步的引擎回归
python -m unittest discover -s tests -p "test_modelsync.py"
python -m unittest discover -s tests -p "test_api_modelsync.py"

# 浏览器回归（可选，Playwright 仅作为测试依赖）
npm install --prefix tests --no-save playwright
# 可使用已安装的 Edge，无需下载 Chromium
$env:BROWSER_EXECUTABLE = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
node tests/browser.cjs
```

浏览器用例自动启动临时配置服务，模型检测与平台适配器均为桩，不接触真实密钥或客户端配置。
截图输出到 `tests/artifacts/`。也可通过 `PLAYWRIGHT_MODULE` 指定已有 Playwright 包路径。

> 本机 PATH 上的 `python.exe` 是 Microsoft Store 的 0 字节占位符，跑测试要用真实解释器：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py -m unittest discover -s tests -p "test_*.py"

# 端到端：复制真实配置到临时目录后跑一次同步，检查各客户端产物
& $py scripts\verify-modelsync-e2e.py
```

## 已清理

- `qwen-token-plan-cn`（过期 plan）：已从 pi / models-store / 各 agent 移除。
- 历史别名：`volcano`（pi）、`cc-switch-step-fun`、`provider-0b8770`（mcode）。

## 安全预览与恢复（本次里程碑）

- Web UI 的同步流程为「保存 Hub 选择 → 预览 → 应用」。预览只列目标、文件动作和警告，不回传配置正文或密钥。预览在内存中保留 15 分钟，重启服务后需要重新生成。
- CLI 可先 `python hub.py preview codex`，确认后 `python hub.py push codex`。CLI push 会重新编译、校验并检查源文件；预览 ID 不跨 CLI 进程保存。
- 只有 `enabled: true` 的目标参与同步；缺失的目标、缺失的 enabled、空的显式目标选择不会默认为全选。Claude 同步不会打开 cc-switch 数据库。示例关闭了 legacy mcode/zcode/omp/cc-switch。
- 每次先在隔离临时目录编译全部选中目标；任何配置错误都会阻止整组原生文件写入。应用前核对源文件未变，再建立 `backups/transactions/<id>/manifest.json` 和逐文件原始备份，原子替换文件。可捕获的中途写入错误会回滚已经写入的文件；回滚本身失败会报出事务和未恢复文件。
- Web UI 可恢复最近一次事务；CLI 用 `python hub.py restore-transaction <id>`。恢复也会建立反向事务。应用后被外部修改的文件不会被强行恢复，请先人工核对。
- **不是跨进程数据库事务**：原生客户端可能在多文件替换之间读到中间状态；建议关闭相关客户端再应用。进程被强杀或断电后，`committing`/`prepared` 清单、`.write-lock` 和备份会保留，需要人工核对并恢复；不要直接删除仍在运行的进程的锁。
- JSON/TOML 会先校验。DSH/旧 YAML 适配器仅支持能够独立验证和无歧义往返的严格 YAML 子集；锚点、复杂字符串、歧义标量等会安全拒绝，不会猜测性改写。DSH 仍限现有 desktop profile，任意 profile 的选择发现留待下一步。
- cc-switch SQLite 不进入此文件事务，选中时会明确拒绝。它与本次直接配置 coding agent 的目标无关。
- 新文件和备份尽量使用 0600/0700 权限；Windows 的 ACL 语义不同，请保持仓库/备份目录私有。现有明文 API key 仍可能出现在原生配置和备份中。
- 测试只验证合成文件的配置变换、UI/API 契约与故障恢复。没有登录、真实供应商调用、模型权限或费用验证。
