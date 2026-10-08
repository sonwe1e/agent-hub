# Agent Hub — 统一 Provider 配置

## 这是什么

机器上有多个 coding agent（pi / mcode / zcode / dsh / cc-switch / omp 等），
每个都有自己的 provider 配置格式。本目录提供**单一事实来源**与**同步引擎**，
改一处即可同步到所有 agent。

当前的个人使用主线是 **Claude Code、pi、dsh（含桌面版）和 Codex**：

1. 在「供应商」中把每个 Coding Plan 建成一条连接。同一家服务的不同 Plan 若端点、密钥或可用模型不同，也分别建成不同供应商。
2. 需要给某个 Agent 单独换端点 / 密钥 / 模型时，在「按 Agent 覆盖」里配一条连接：例如同一个网关给 Claude Code 与 Codex 发不同的 key，两边拉到的模型集合本就不同。
3. 添加或检测该 Plan 的模型（检测时可选对哪条连接执行），在「平台同步」中选择全局主模型；需要差异时再为单个 Agent 单独指定。
4. 选择要更新的 Agent，生成预览并确认应用。以后 Plan 的地址、密钥或模型变动，只改这一条供应商并再次同步。
5. Codex 可选「官方线路」或支持 Responses 的第三方 Plan。首次切到第三方前，在 Codex 内准备好官方配置；Hub 会保存并在切回时恢复官方模型、provider 和推理强度，保留原生登录态。

其他订阅在这里指提供兼容 API 地址与密钥的 Coding Plan。Claude Code、pi、dsh 的原生账号订阅登录态不由 Hub 复制或转换。cc-switch 数据库目标不参与工作台的安全预览同步。

**连接模型**：每个供应商 = 一条顶层默认连接（`baseUrl` / `apiKey` / `api` / `models`）+ 可选的
`agents.<agent>` 按 Agent 覆盖。未声明的 Agent 直接用默认连接，因此「所有供应商都能给所有
Coding Agent 用」——能不能用只取决于该 Agent 的连接是否就绪：Claude Code 需要 Anthropic
Messages 端点，Codex 第三方只接受 Responses，pi / dsh 用 OpenAI 兼容端点。

## 目录

| 文件 | 作用 |
|------|------|
| `providers.json` | 统一配置，可通过工作台维护，无需手工编辑 |
| `hub.py` | 同步引擎（含连接模型与旧配置迁移命令） |
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
>
> **旧配置迁移**：早期版本用扁平字段（`anthropicBaseUrl` / `claudeModels` /
> `claudeEnv` / `claudeSettings` / `openaiApiKey` / `apiKeyEnv` / `codexWireApi`）
> 与 `sync.targets.claude.activeProvider`。这些字段仍会被读取（兼容层），执行一次
> `hub migrate-agents` 即可搬进 `agents.<agent>` 与 modelSync。

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
G:\workspaces\agent-hub\hub.cmd model deepseek deepseek-flash --target claude --reasoning-effort ultra
G:\workspaces\agent-hub\hub.cmd model tokenfree gpt-6-astra --target codex
G:\workspaces\agent-hub\hub.cmd migrate-agents  # 旧扁平字段 → agents.<agent>
G:\workspaces\agent-hub\hub.cmd save-gold        # 备份当前模型列表为黄金模板
G:\workspaces\agent-hub\hub.cmd restore-gold     # 从黄金模板恢复模型列表
G:\workspaces\agent-hub\hub.cmd backup
```

## 新版工作台

桌面入口与网页入口共用同一套界面，不依赖 CDN、前端框架或构建工具。
启动器会复用新版本地服务；若默认端口被旧版服务或其他程序占用，会尝试后续端口，不会终止已有进程。

### 模型库

- 汇总所有供应商、所有连接的模型，支持名称 / ID 搜索、供应商筛选、**连接筛选**、推理 / 图片 / 纯文本能力筛选。
- 「连接」列标明模型来自默认连接还是某个 Agent 的连接——同一供应商给不同 Agent 配不同 Key 时，模型库按连接分开存放。
- 添加与编辑通过独立表单完成，单次提交即保存；新建时选择所属连接。支持带 `/` 的模型 ID、ID 改名与重复检查。
- 编辑时保留 `cost`、`compat` 等高级字段，不再逐行重写整个模型列表。
- 支持跨供应商、跨页勾选与批量删除；全选仅作用于当前页，筛选外的已选项会明确提示。
- **删除模型仅影响对应连接的本地配置，执行平台同步后才影响客户端。**

### 供应商

- 卡片展示连接地址、各 Agent 连接就绪状态、模型数量、密钥配置状态与启用状态。
- 在配置弹窗中维护**默认连接**与**按 Agent 覆盖**：端点、密钥、协议、模型与（Claude 的）透传字段都可以只为某个 Agent 配置。
- **检测模型**先选连接（默认 / Claude / Codex / pi / dsh），再向该连接的端点与密钥查询：不同 Key 拉到的模型集合本来就可以不同。
- 导入不删除已有模型，也不覆盖已有模型的手工参数。未勾选任何项时禁止导入。
- **删除供应商沿用现有引擎的立即清理逻辑，会影响客户端配置**，弹窗会单独说明；删除后建议再同步，确保所有平台对齐。

### 平台同步

- 勾选参与同步的平台，支持保存设置或保存后直接同步；未选择的平台不会写入。
- dsh 桌面版跟随 dsh，适配器只执行一次；Codex 的 ChatGPT 登录态仍由客户端自行管理。
- 每个 Agent 用哪个供应商、哪个模型，统一由「多 agent 使用的模型」卡片决定（见下一节），不再有 Claude 专属的选择器。
- 展示待同步状态、最近同步时间和逐平台成功 / 失败信息；失败不会伪装成全局成功。
- 「检测变更并同步」**不是从客户端导入**，仍以 Hub 为准覆盖已选客户端配置，执行前需要确认。
- 定向同步只更新实际成功目标的变更哈希；只有所有启用目标成功写入后才标记本地配置已对齐。

### 多 agent 模型同步

「多 agent 使用的模型」卡片选一次主模型，同步后由兼容该供应商的平台各自采用；ZCode 会写入自己的 provider/model 选择，Codex 仅在明确选择第三方线路时跟随。
Codex 第三方线路管理 `model_provider`、`model`、可选的 `model_reasoning_effort` 和 `[model_providers.agent_hub]` 段；切回官方线路会恢复首次切换前保存的值。ChatGPT 登录态由 Codex 自己维护。

| 客户端 | 主模型落到哪里 | 子 agent / 档位落到哪里 |
|--------|----------------|--------------------------|
| pi | `~/.pi/agent/settings.json` 的 `defaultProvider` + `defaultModel` | pi 没有子代理概念，只写主模型 |
| dsh 桌面版 | `profiles/desktop/cordis.patch.yml` 的 `agent-default-model` | 同文件 `subagent-model-selection-settings.allowedModels`（子代理可选模型） |
| ZCode | `~/.zcode/v2/provider_config.json` 的 `config.defaultModelSelection` | ZCode provider 规则中的 `personalModelIds` / `modelOrder` |
| Claude Code | `~/.claude/settings.json` 的 `env.ANTHROPIC_MODEL` | `ANTHROPIC_DEFAULT_OPUS/SONNET/HAIKU_MODEL` 与 `CLAUDE_CODE_SUBAGENT_MODEL` |
| Codex 桌面版 / CLI | `~/.codex/config.toml` 的 `model_provider` + `model` | 同文件 `[model_providers.agent_hub]`（base_url / wire_api / 授权）；`auth.json` 的登录态不碰 |

模型下拉只列出**该 Agent 连接上**的模型：`agents.claude`、`agents.codex` 与 `agents.zcode` 各有自己的模型库。

`providers.json` 里的结构（也可以用命令行 `hub model` 改）：

```jsonc
"modelSync": {
  "default": { "provider": "tokenfree", "model": "gpt-6-astra" },
  "overrides": {
    "pi":  { "provider": "stepfun", "model": "step-5-preview" },   // 只改 pi
    "dsh": { "provider": "firework", "model": "glm-5.3" },
    "zcode": { "provider": "openrouter", "model": "anthropic/stealth/space-bunny-alpha[1m]" },
    "codex": { "provider": "tokenfree", "model": "gpt-6-astra", "reasoningEffort": "ultra" },
    "claude": {
      "provider": "tokenfree-claude", "model": "claude-opus-5-5",
      "tiers": { "opus": { "provider": "tokenfree-claude", "model": "claude-opus-5-5" } }
    }
  }
}
```

解析优先级（从高到低）：

1. `overrides.<平台>.tiers.<档位>` —— 界面上单独指定的档位（仅 Claude Code 有档位）；
2. `overrides.<平台>` / `modelSync.default` —— 平台级或全局选择；
3. 供应商自己声明的 `provider.agentModel`；
4. 该供应商**该连接**模型列表的第一项。

推理强度词表：全局 `off / minimal / low / medium / high / xhigh / max / ultra`；
Codex 线路用 `none / minimal / low / medium / high / xhigh / max / ultra`（`none` 表示关闭推理）。

几条刻意保留的语义：

- **只有「主模型」会被全局选择改写。** Claude Code 的 opus / sonnet / haiku / subagent 档位
  只在 modelSync 里被显式指定时才会写；一个全局设置不会顺手把它们改掉。
- **Claude Code 跟随全局有前置条件。** 全局选中的供应商必须具备 Claude 连接
  （`agents.claude` 里配了 Anthropic 端点），否则 Claude Code 保持现状——
  baseUrl 与 apiKey 必须和模型配套，跨连接套用模型名只会写出一个对方不认的模型。
  同步时按 modelSync 优先级解析实际使用的连接，不隐式回写 Hub 配置。
- **没配置 `modelSync` 时不动客户端。** pi 的 `defaultProvider` / `defaultModel`、
  dsh 的 `agent-default-model` 都保持原样，只有点同步并配置过才接管。
  （例外：`sync.targets.claude.featureEnv` 是平台级特性开关，配置了就会写入。）
- **手填的模型 ID 会被标记但照样写。** 不在该连接模型库里的 ID 在状态里带 ⚠，客户端可能拒绝。

命令行：

```powershell
G:\workspaces\agent-hub\hub.cmd model deepseek deepseek-flash                     # 全局主模型
G:\workspaces\agent-hub\hub.cmd model firework glm-5.3 --target dsh              # 只改 dsh
G:\workspaces\agent-hub\hub.cmd model tokenfree gpt-6-astra --target codex --reasoning-effort ultra
G:\workspaces\agent-hub\hub.cmd model tokenfree-claude claude-opus-5-5 --target claude  # 只改 Claude Code
G:\workspaces\agent-hub\hub.cmd model openrouter anthropic/stealth/space-bunny-alpha[1m] --target zcode  # 只改 ZCode
G:\workspaces\agent-hub\hub.cmd push                                            # 再同步到各客户端
```

> `hub model` 只改 `providers.json`；必须再 `hub push`（或在工作台点同步）才写进客户端。

### 模型库删除保护

- 勾选跨页 / 跨筛选保留，但删除时只要选中项里有**当前屏幕上看不到**的模型，确认框会要求
  单独勾选「我确认删除当前筛选或页面上看不到的 N 个模型」，否则确认按钮不可用。
- 服务端在写入 `providers.json` 前校验：除本次请求声明的删除外，任何连接上的模型消失都会导致
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

- Pi 会把 `models.json` 与内置模型目录**合并**，不会把它当白名单：例如 OpenRouter
  连接只填 spacebunny，提供密钥后仍可能看到大量内置模型。Hub 现在同时写
  `settings.json.enabledModels`，使用启用的 Pi 连接模型库里的完整 `provider/modelId`，
  `agents.pi.models` 优先于默认连接模型库；不是只留下 `modelSync` 当前主模型。
- 新版 Pi 的 `/model` 有 `all/scoped` 两种范围，有 scope 时默认进入 `scoped`，Tab
  可切回 `all`；`/scoped-models` 用于管理范围，Ctrl+P 在范围内轮换。
  这是默认选择范围，不是删除内置目录或禁止手动选择其他模型。同步后重新启动 Pi；
  旧版本、项目级 `enabledModels` 和 CLI `--models` 可能有不同表现或覆盖全局设置。
- 同步替换 Hub 管理的 Pi provider 范围，保留其他 provider 的用户范围和未托管默认模型，
  不为缩小列表删除用户的其他 provider 或登录态。原有跨 provider 通配符、简写也保留，
  并在预览中提醒：它们仍可能扩大列表。需要严格范围时先在 `/scoped-models` 检查。
  `[1m]` 等字面方括号保持原样，依赖当前 Pi 解析器先匹配完整 `provider/modelId` 的行为；
  不转义成 glob，否则可能误匹配其他 provider 的同名字面 ID。用户安装的 Pi 版本仍需核对。
  此行为已对 Pi `1.0.3` 源码提交 `98d2e1947aa9c75dff4c20474d541873dadc1d05` 验证，
  并不代表已验证用户本机版本；旧版需先核对是否有完整 canonical reference 的优先匹配。
  为避免意外扩大范围，Hub 模型 ID 含 `*`、`?`、反斜杠时拒绝同步；
  方括号 ID 同时以 `:high` 等有效推理档位后缀结尾时，Pi 会先拆后缀，故明确报告歧义，
  不自动改选模型；`:free` 和普通无方括号的 `:high` 字面 ID 不受影响。
  也不会把已有范围清成 `[]`，因为 Pi 将空数组解释为全部可用模型。
- Pi 原生行为依据：[模型配置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)、
  [设置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md)、
  [选择器](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/modes/interactive/components/model-selector.ts)。
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
    "tokenfree":        { /* 自定义网关 OpenAI 线（gpt-*），见下 */ },
    "tokenfree-claude": { /* 同一网关的 Anthropic 线（claude-*），独立供应商 */ }
  },
  "modelSync": { /* 多 agent 模型选择，见上一节 */ },
  "removedProviders": { /* 已删除记录，防止复活 */ },
  "sync": { "targets": { /* 各 agent 启用开关 */ } }
}
```

## 按 Agent 覆盖连接（agents.\<agent\>）

顶层字段是**默认连接**：`baseUrl` / `apiKey` / `api` / `models`。
`agents.<agent>` 可以只为某个 Agent 覆盖其中的任意几项，未覆盖的继续用默认值：

```jsonc
"tokenfree": {
  "name": "TokenFree",
  "baseUrl": "https://api.tokenfree.shop/v1",   // 默认连接（OpenAI 兼容）
  "apiKey": "sk-openai-key",
  "api": "openai-completions",
  "agents": {
    "codex": {
      "baseUrl": "https://api.tokenfree.shop/v1",
      "api": "openai-responses",                          // Codex 第三方只接受 Responses
      "apiKey": "sk-codex-key",                           // 这个 key 拉出 gpt-* 模型
      "apiKeyEnv": "TOKENFREE_API_KEY",                   // 或用环境变量引用（优先）
      "models": [ { "id": "gpt-6-astra", ... } ]
    },
    "pi":  { "apiKey": "sk-pi-key" },                     // 只换密钥，其余用默认
    "dsh": { "baseUrl": "https://dsh.example/v1" }        // 只换端点
  },
  "models": []                                            // 默认连接的模型库
}
```

**什么时候拆成两个供应商**：`agents.<agent>` 适合「同一协议线内」的差异（换 key、换端点、
各自模型库）。如果一个网关的两条线协议完全不同（比如 Anthropic Messages 与 OpenAI
Responses/Completions 各发一把 key、各有一套模型库），建议拆成两个独立供应商条目
（如 `tokenfree` 与 `tokenfree-claude`），各自 `targets` 限定平台，模型管理与检测更清晰。
Anthropic 线的 `baseUrl` 填不带 `/v1` 的前缀——Claude Code 会自行追加 `/v1/messages`。

| 字段 | 作用 |
|------|------|
| `baseUrl` / `apiKey` / `apiKeyEnv` / `api` | 覆盖该 Agent 的端点、密钥与协议 |
| `models` | 该连接自己的模型库；**没有这一项就沿用默认连接的模型** |
| `env` / `settings` | 仅 `claude`：原样写入 `~/.claude/settings.json` 的 env 与顶层字段 |

首次在某个 Agent 连接上添加或编辑模型时，Hub 会把继承的模型库复制为该连接的独立模型库；后续修改只作用于该连接。

**连接是否就绪**决定供应商能否进入某个平台的同步：

| Agent | 要求 |
|-------|------|
| pi / dsh / mcode / omp / cc-switch | 有 OpenAI 兼容端点（默认或该 Agent 覆盖） |
| ZCode | 有 Chat Completions、Responses 或 Anthropic Messages 端点（默认或 `agents.zcode` 覆盖） |
| Claude Code | 有 Anthropic Messages 端点（`agents.claude.baseUrl`，或旧 `anthropicBaseUrl`，或整个 provider 就是 `anthropic-messages`） |
| Codex 第三方 | 端点的协议是 `openai-responses` |

`targets` 仍然可以限定供应商只进入列出的平台；未声明时进入所有平台。

**不同 Key 不同模型**：模型按连接存放，「检测模型」时先选连接——同一网关给 Claude 与
Codex 发不同 key，两边拉到的模型集合本来就可以不同。模型切换只在各自连接的模型库里选择。

**切换连接时的收回语义**：`env` 与 `settings` 里的键，在切换到另一家时，只有上一个
provider 声明过、当前 provider 没声明的才会被删除——避免残留别家的 `model` / `effortLevel`。
你自己手工写进 `settings.json` 的键（`hooks` / `statusLine` / `modelSettings` 等）永远保留。

web 界面在「供应商 → 配置」的「按 Agent 覆盖」一节里编辑这些连接；「平台同步」页的模型
下拉只列出对应连接上的模型。

## Provider ↔ Agent 映射

| hub key | pi | mcode | zcode | dsh / dsh 桌面版 | claude | codex |
|---------|----|-------|-------|----------------|--------|-------|
| `stepfun` | stepfun | stepfun | StepFun | stepfun | —（没有 Claude 连接） | 不写入 |
| `firework` | firework | volcano | 火山 | firework | —（没有 Claude 连接） | 不写入 |
| `deepseek` | deepseek | deepseek | DeepSeek | deepseek | `agents.claude` → `/anthropic` | —（协议不是 responses） |
| `openrouter` | openrouter | openrouter | OpenRouter | openrouter | `agents.claude` → `/api` + 模型库 | `agents.codex` → responses |
| `tokenfree` | tokenfree（默认连接模型库） | tokenfree | — | tokenfree | —（Claude 线在独立供应商） | `agents.codex` → `/v1/responses` |
| `tokenfree-claude` | — | — | — | — | 顶层 `anthropic-messages` → `/v1/messages` + `env` / `settings` 透传 | — |

> 命名差异是各 agent 的历史习惯，同步引擎会自动映射。
>
> `codex` 是正式写入目标：只写 `model_provider` / `model` 与 `[model_providers.<providerId>]`，
> `auth.json` 里的 ChatGPT 登录态一个字都不动，所以界面显示的账号不受影响。
>
> 同一个网关给 Claude 与 Codex 发不同 key 时（模型集合不同），分别配 `agents.claude` 与
> `agents.codex`：各自的 `apiKey`（或 `apiKeyEnv`）与 `models` 都是独立的。hub 优先使用
> `apiKeyEnv` 环境变量引用，其次连接上的 `apiKey`，最后顶层默认 `apiKey`。
> Codex 只支持 Responses：`agents.codex.api` 必须是 `openai-responses`（或旧字段
> `codexWireApi: "responses"`），Chat Completions 网关会被拒绝。

## 新增供应商

界面上「供应商 → 添加」即可，不需要改代码：

| 字段 | 说明 |
|------|------|
| 供应商 ID | 各平台配置里的路由名（字母、数字、`_`、`-`） |
| 默认连接 Base URL / API Key / API 协议 | OpenAI 兼容端点；只给 Claude Code 用时可留空，改在「按 Agent 覆盖」里配 Anthropic 端点 |
| 启用 | 关闭后不进入任何平台的同步 |
| **同步到哪些平台** | 勾选目标平台；**一个都不勾 = 同步到所有平台**。连接不就绪的平台不会写入 |
| **agent 默认模型** | 该供应商给 agent 用的默认模型 ID；「多 agent 模型」没有显式选择时用它兜底 |
| **按 Agent 覆盖** | 为 pi / dsh / Claude Code / Codex 单独配端点、密钥、协议与模型；Claude 连接还可透传 `env` / `settings` |

新增供应商进入 dsh 时，hub 会按 `<供应商ID>_API_KEY` 推导凭据引用名，并把密钥写进
`~/.dsh/.credentials.yaml` 的 `refs`；进入 Claude Code 需要 Anthropic Messages 端点；
进入 Codex 需要 Responses 端点，hub 会把它写进 `[model_providers.<providerId>]`。

## Codex 桌面版 / CLI

需要 Python 3.11+；使用标准库 `tomllib` 校验输入及生成结果，不增加运行依赖。

- 在同步页显式选择「官方订阅」或「第三方供应商」，先预览，再应用。没有 `mode` 的旧配置安全地按官方模式处理，不会隐式切换第三方。
- 首次第三方切换前，请先在 Codex 中选择官方 provider、model、effort。Hub 只记录这三个字段的值及是否存在到 `~/.codex/.agent-hub-state.json`。恢复官方时一起恢复，缺省字段会恢复成缺省，第三方模型不会残留。
- 使用独立 `[model_providers.agent_hub]`，保留已有 `custom` 和其他 provider。无托管记录的同名表冲突仍会拒绝。恢复官方会移除本轮 Hub 写入的 provider 表。
- 在 Codex 中改过模型、推理强度或线路后，Web 同步页的新预览会以当前文件为基准，明确警告将按 Hub 选择重新设置托管字段；确认应用后才写入。预览后再发生外部更改，应用仍会拒绝，需重新预览。CLI push、watch 和旧 `/api/sync` 不会自动接管这些外部编辑。
- 如果已在 Codex 中切回官方，新预览保留当前官方模型与推理强度，下一次第三方切换记录新的官方基线。损坏的恢复记录、无法安全改写的 TOML、外部 OpenAI 路由或 API 登录覆盖仍需先人工检查；不建议删除托管记录来绕过检查。
- 第三方只接受 Responses。`agents.codex.api` 设为 `openai-responses`（或旧字段 `codexWireApi: "responses"`）；不能把 Chat Completions 当成兼容证明。
- 优先用 `apiKeyEnv`（环境变量由用户在原生环境中设置，必要时重启客户端）。兼容现有配置时支持连接上的 `apiKey` / 顶层 `apiKey` 字面量，这些会落入配置和备份明文；官方建议优先环境变量引用。预览不显示凭据值。
- 显式配置 `agents.codex.apiKey: ""` 或 `apiKeyEnv: ""` 会清空对应继承字段，不再偷偷回退到顶层/旧密钥；省略或 `null` 仍继承。界面留空仍按原设计省略字段。环境变量引用优先级不变。
- 第三方表设置 `requires_openai_auth = false`，不借用 ChatGPT 凭据。Hub 从不读取、写入、复制或删除 Codex `auth.json`、系统钥匙串，也不执行 login/logout。现有登录失效时由 Codex 自己重新登录。
- 推理强度接受 `none / minimal / low / medium / high / xhigh / max / ultra`；`max` / `ultra` 是否被具体模型接受由 Codex 自己判断。
- **仅管理基础 `~/.codex/config.toml`**。Codex 0.134+ 命名配置是 `<name>.config.toml`，由 `--profile` 选择；Hub 不触碰它们。项目配置、环境变量、CLI 参数及运行中的会话可能覆盖基础配置，因此应用成功不是线路/登录/付费套餐验证。旧 `[profiles.*]` 检测到后先要求迁移。
- 不读取安装版本或运行真实 Codex。兼容目标依据 2026-10-05 官方文档；启用后请在自己的客户端检查当前配置和选中模型。

官方参考：[配置字段](https://learn.chatgpt.com/docs/config-file/config-reference)、[profile 迁移](https://learn.chatgpt.com/docs/config-file/config-advanced)、[原生认证](https://learn.chatgpt.com/docs/auth)。

## Claude Code

- 写入 `~/.claude/settings.json` 的 `env.ANTHROPIC_*`（只改 env，保留 hooks / permissions / statusLine）。
- Claude 同步只写原生 settings.json，不打开或修改 cc-switch 数据库。
- **用哪个供应商、哪个模型由 `modelSync` 决定**（见「多 agent 模型同步」）：没有为 Claude 解析出主模型时，一个键都不会动；平台级特性开关 `sync.targets.claude.featureEnv` 例外，配置了就会写入。
- 端点与密钥来自 `agents.claude` 连接；没有该连接的供应商不会进入 Claude Code。
- `agents.claude.env` / `agents.claude.settings` 可透传任意 env 与顶层字段，见「按 Agent 覆盖连接」。

## dsh 桌面版

- 与 dsh CLI 共用 `~/.dsh`。桌面 profile 在 `profiles/desktop/`。
- dsh 会把供应商的 `models: []` 回退成内置全部模型。Hub 同步时只启用该 dsh
  连接模型库非空的供应商，并从三层配置中移除没有选模型的 Hub 路由，保留已存凭据。
  因此 OpenRouter 没有选模型时不会自动展开整个目录；添加模型后再同步即可恢复。
  显式的 `agents.dsh.models: []` 同样表示该连接未选模型，不继承默认连接的模型库。
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
- pi / dsh / ZCode / claude / codex 是主要目标；mcode / omp / cc-switch 适配器保留，不主动写入。
- **qwen-token-plan-cn** 已过期并删除；qwen 系列模型仍可通过 firework（火山）使用。
- 每次写入前会把原文件备份到 `backups/`，可随时回滚。
- 修改 `providers.json` 的连接或模型后，运行 `hub push` 即可全局生效。
- 需要自动同步时运行 `hub watch`，它会轮询各 agent 配置哈希，变更后重新对齐。
- 多 agent 模型：`hub model <provider> <model>` 改配置（`--target pi/dsh/zcode/claude/codex`），`hub push` 落到客户端。
- 旧版扁平字段（`anthropicBaseUrl` / `claudeModels` / `claudeEnv` / `claudeSettings` /
  `openaiApiKey` / `apiKeyEnv` / `codexWireApi` / `sync.targets.claude.activeProvider`）仍可读取，
  `hub migrate-agents` 会搬进 `agents.<agent>` 与 modelSync（可重复执行）。

## 测试

```powershell
# 后端与启动器回归（Python 标准库，无额外依赖）
python -m unittest discover -s tests -p "test_*.py"

# 多 agent 模型同步的引擎回归
python -m unittest discover -s tests -p "test_modelsync.py"
python -m unittest discover -s tests -p "test_api_modelsync.py"

# 连接模型与旧配置迁移
python -m unittest discover -s tests -p "test_agent_connections.py"

# 浏览器回归（可选，Playwright 仅作为测试依赖）
npm install --prefix tests --no-save playwright
# 可使用已安装的 Edge，无需下载 Chromium
$env:BROWSER_EXECUTABLE = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
node tests/browser.cjs

# 界面逻辑契约（无需浏览器）
node tests/ui_contract.cjs
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
- 只有 `enabled: true` 的目标参与同步；缺失的目标、缺失的 enabled、空的显式目标选择不会默认为全选。Claude 同步不会打开 cc-switch 数据库。示例仍关闭 legacy mcode/omp/cc-switch；需要 ZCode 时启用 `sync.targets.zcode`，并在模型同步卡片中选择 ZCode。
- 每次先在隔离临时目录编译全部选中目标；任何配置错误都会阻止整组原生文件写入。应用前核对源文件未变，再建立 `backups/transactions/<id>/manifest.json` 和逐文件原始备份，原子替换文件。可捕获的中途写入错误会回滚已经写入的文件；回滚本身失败会报出事务和未恢复文件。
- Web UI 可恢复最近一次事务；CLI 用 `python hub.py restore-transaction <id>`。恢复也会建立反向事务。应用后被外部修改的文件不会被强行恢复，请先人工核对。
- **不是跨进程数据库事务**：原生客户端可能在多文件替换之间读到中间状态；建议关闭相关客户端再应用。进程被强杀或断电后，`committing`/`prepared` 清单、`.write-lock` 和备份会保留，需要人工核对并恢复；不要直接删除仍在运行的进程的锁。
- JSON/TOML 会先校验。DSH/旧 YAML 适配器仅支持能够独立验证和无歧义往返的严格 YAML 子集；锚点、复杂字符串、歧义标量等会安全拒绝，不会猜测性改写。DSH 仍限现有 desktop profile，任意 profile 的选择发现留待下一步。
- cc-switch SQLite 不进入此文件事务，选中时会明确拒绝。它与本次直接配置 coding agent 的目标无关。
- 新文件和备份尽量使用 0600/0700 权限；Windows 的 ACL 语义不同，请保持仓库/备份目录私有。现有明文 API key 仍可能出现在原生配置和备份中。
- 测试只验证合成文件的配置变换、UI/API 契约与故障恢复。没有登录、真实供应商调用、模型权限或费用验证。
