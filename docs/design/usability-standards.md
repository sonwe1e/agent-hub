# Agent Hub 可用性规范 · Phase 1

> 本文保留当时的审查和修订记录，所列路径和行号可能已变化；当前代码入口见[开发者导航](../developer-guide.md)。

> 适用：`ui/app.js`、`ui/workbench.js`、`ui/workbuddy.js` 的**交互行为**与 `ui/index.html` 的对话流。
> 与 `visual-direction.md` 的分工：**视觉管材质**（token、排版、色彩、动效形状），**本文管行为**（焦点去哪、反馈何时出现、状态怎么说、危险操作怎么拦）。同一套硬约束：DOM 结构、`class`/`id`/`data-*` 冻结；行为修复落在 JS，DOM 级新增需 Lead 批准。
> 每条判定线 = 一句话判定 + 判定方法 + 现状标记（✅ 现状满足 / 🔧 本轮已修复 / ⏳ 仍有缺口）。这不是通用 a11y 清单——每条都来自 2026-10 本仓库的真实交互流审计，判定方法全部可用现有测试设施（Playwright / 键盘走查 / 故障注入）执行。

---

## 0. 一句话定位

**本机台账的可用性 = 用户说的每一句话都被接住**：正在输入的东西不被后台刷新毁掉，按下的键有确定的去向，失败永远带着下一步，删除之前看得见会改什么。

「未知就显式显示未知」是 README 的产品承诺；本文是它在交互层的对应物——**不确定的状态必须说清楚，打断用户的事必须无痕**。

---

## 1. 判定线 D1–D15

### D1 · dialog 打开焦点

任何 dialog 打开后，焦点必须落在**确定的**第一个可交互件：confirm = 取消按钮（安全默认），表单型 editor = 第一个字段，只读详情 = dialog 内任意元素——不得依赖浏览器默认。

**判定方法**：Playwright 逐个打开 3 个 dialog（供应商表单、确认删除、调用详情），断言 `document.activeElement` 等于预期元素。
**现状**：🔧 本轮已修复。confirm 聚焦取消是原有实现（app.js `confirmDialog`）；表单型 editor 首焦落在第一个 input/select 为本轮新增（app.js `showEditor`，`querySelector("input, select, textarea")` 命中即 focus）——此前落在右上角关闭按钮，回车习惯会直接关窗。

### D2 · dialog 关闭还焦

dialog 经 Esc / 取消 / 保存关闭后，若触发元素仍在文档中，焦点必须回到触发元素；已离档则落 `main#content`，**不得落 body**。

**判定方法**：键盘走查 3 条关闭路径 × 3 个 dialog，断言 `document.activeElement !== document.body`。
**现状**：🔧 第二轮已修复（app.js `captureDialogOpener` / `restoreDialogFocus`：opener 在档且未禁用则还回，否则落 `main#content`；`busy` finally 收口回收被禁用扯掉的焦点——confirm→action 链路由此闭环，否则刚还回的 opener 会被 busy 的禁用立刻 blur）。嵌套链（关闭脏表单弹确认）最终落点以最初 opener 为准；import-dialog 的取消 / 导入 / Esc 统一经 `close` 事件还原；workbench 全部弹窗走全局函数自动受益。

### D3 · 焦点不因刷新丢失

定时刷新（30s 观测轮询、bulk 期间轮询、WorkBuddy 登录轮询）与重绘后，原焦点元素的语义等价物仍在文档中时，焦点必须保持在等价元素上；交互（改筛选、切页签、翻页）后的重绘同理。

**判定方法**：轮询窗口内与逐交互后断言 `document.activeElement` 非 body；Tab 位置不回文档头。
**现状**：🔧 第二轮收口完毕，无残留。轮询路径：观测走输入抢救（workbench.js `renderObservationPreserving`）、Agent 详情清单未变不重写（`refreshAgentDetail`）。手动路径：筛选 / 切 tab / 翻页 / 钻取走 `renderObservationManualPreserving`（workbench.js，只还原焦点与搜索草稿——手动路径的值本就来自用户刚提交的操作）。modelsync 卡片：定位器扩展到既有 `data-*`（app.js `focusSelectorFor`，无 id 的推理强度下拉不改 id、按 `#modelsync-card` / `#model-detail` 容器区分还原，避免还到另一份同属性下拉）。WorkBuddy 登录等待期：`paintWorkbuddy` 以签名比对跳过无变化重绘（workbuddy.js），不再每 1.5s 抢走按钮焦点。

### D4 · 输入容器不被轮询重建

用户正在输入的控件（观测搜索框、精确时间 datetime、手填模型 ID）所在容器，在输入未提交期间不得被无差别 innerHTML 重建；确需重绘时，未提交值、光标选区、滚动位置必须原样归还。

**判定方法**：在 `#obs-search` 输入到一半、精确时间填到一半时强制触发一次轮询渲染，断言值、`selectionStart/End`、`window.scrollY` 保持。
**现状**：🔧 本轮已修复（workbench.js `loadObservation(poll)`：数据未变直接跳过重绘；变了走 `snapshotObserveInputs` / `restoreObserveInputs` 抢救值、range-detail 展开态、滚动与选区后重建）。手填模型 ID 是既有先例（app.js input 处理器注释「不重建卡片，避免打断输入」）。

### D5 · details 展开态跨重绘保留

所有 `<details>` 的展开状态必须跨重绘保留：轮询重建后用户刚展开的面板不得自己合上。

**判定方法**：展开「精确时间与关联条件」「采集缺口」面板，各等一次 30s 刷新，断言仍展开。
**现状**：🔧 本轮补齐 range-detail（进 D4 的快照）。既有先例原本满足：`gapErrors` / `gapDegraded`（workbench.js toggle 捕获监听）、`bulkOpen`、`models-advanced`（`state.advancedOpen`）、`data-ms-details`（`state.modelSyncOpen`）。

### D6 · Esc 语义分级

Esc 必须按层级走受控路径：表单脏 → 先确认放弃再关；confirm dialog = 取消；只读详情 = 直接关；无 dialog 打开时 Esc 才收起页内详情。任何一层都不得静默丢弃未保存内容。

**判定方法**：逐 dialog 键盘走查：脏表单按 Esc 必须出现「放弃未保存的修改？」确认。
**现状**：✅ 现状满足（app.js `requestCloseEditor` + editor `cancel` 拦截 + 全局 Esc 的 dialog 开启守卫），本轮无改动。

### D7 · 数据视图四态完备

每个数据视图必须能处于 loading / empty / error / 部分 error 四态，且 error 文案包含**下一步动作**（重试按钮或明确指引）；「读取中」「读不到」「确实没有」三种含义不得共用一句话。

**判定方法**：对 `/api/agents`、`/api/observation`、`/api/modelsync`、`/api/switch-plans` 逐个注入 500 / 断网 / 空数据走查四态。
**现状**：🔧 基线原本满足（agents 三相区分 `updateTag(e, outcome, phase)`、modelsync 错误分支带重试按钮、首屏失败页带「重试连接」）；本轮补上设置页观测读取失败的 error 态与重试（workbench.js `renderSettings`，复用 `obs-retry`），不再困在「正在读取设置」。

### D8 · 部分错误分级

某一部分失败、其余成功时，两类信息必须分开呈现且语义不同：「读取失败」可重试，「降级 / 缺依赖」不可重试（重试只会再跳过一遍）；不得用一个笼统的警告吃掉区别。

**判定方法**：同时注入 zstd 缺失 + 一条读失败路径，断言两块独立出现、仅前者带重试按钮。
**现状**：✅ 现状满足（workbench.js `collectionGaps`：errors 带 `retry-source`/`retry-all`，degraded 用琥珀边线降一档，`zstdNotice` 给出可复制的安装命令）。

### D9 · 危险操作全确认

删除类动作（模型 / 批量删除 / 供应商 / 方案 / 清空观测 / 退出服务）与跨实例批量更新，必须经 confirm-dialog；批量删除涉及当前筛选或页面看不到的对象时，必须出现 `#confirm-ack` 勾选确认才能继续。

**判定方法**：逐个触发删除与 update-all，断言先出现 `#confirm-dialog`；构造跨页选择断言 `#confirm-ok` 初始 disabled。
**现状**：🔧 本轮补齐「全部更新」确认（workbench.js `check-all`/`update-all` 分支：列出可更新实例数与重启提示）；删除类与 `#confirm-ack` 越屏勾选为原有实现（app.js `bulkDelete`）。

### D10 · 确认信息充分性

确认文案必须回答三问：**改哪些对象 / 多少个、何时生效、如何恢复**。预览面板显示路径与动作（新建 / 更新 / 删除），不显示也不暗示密钥内容。

**判定方法**：逐条确认文案对照三问清单；预览断言含路径与动作 tag。
**现状**：✅ 现状满足（范本：app.js `deleteProvider` 数模型数 + 说明立即生效 + 自动备份；`renderPreview` 列文件与动作）。B4 新增的 update-all 确认文案（实例数 + 重启提示）同样符合。

### D11 · 反馈不静默

每个写操作必须有三者之一：toast、save-state 变化、内联错误。空 catch 与「按设计静默」的点（剪贴板失败、busy 期拦截）必须给出一句解释——**用户按下按钮后页面必须有可感知的响应**。

**判定方法**：grep 空 catch 块逐个核对；走查全部 `data-write` / `data-daily` 写按钮的成功与失败两路。
**现状**：🔧 本轮修复三处：剪贴板失败 toast（workbench.js `copy-command`）、busy 期导航被拦 toast（app.js `navigate`）、导入扫描失败由 busy 统一转 toast（app.js `scanImportClient`）。既有范本：api() 超时文案、`refreshAfterWrite` 的「列表刷新失败请点右上角刷新」。

### D12 · 错误文案可行动

面向用户的错误必须包含下一步；**原始异常串（TypeError、 Cannot read properties 等）不得直出**。

**判定方法**：故障注入走查全部失败路径；文案评审对照「下一步」清单。
**现状**：🔧 本轮消灭最后一处：导入扫描失败曾在 `state.importCandidates.length` 上抛 TypeError 并以英文 toast 直出（app.js `scanImportClient` 重构后失败停在原地、busy 统一转中文错误）。范本：api() 的「请求超时。操作可能仍在后台执行，请稍后刷新确认，勿重复提交。」

### D13 · 首次体验

首次配置引导页必须讲清三件事：为什么需要这个文件、Hub 会去哪找、选完之后发生什么。空数据页不得暗示「没有活动」：观测页区分「采集尚未开启」与「当前范围没有已采集记录」，后者明说「没有记录不能证明没有活动」。

**判定方法**：全新 HOME 冷启动走查（无 providers.json → 引导页；空索引 → 观测页；零供应商 → 供应商页）。
**现状**：🔧 基线原本满足（desktop_app.py 引导页三问齐全；观测空态两分）；本轮补齐供应商页零供应商空态（app.js `renderProviderCards` 的 empty() 引导卡，说明 providers.json 是单一来源与下一步）。

### D14 · nav 状态自明

侧栏的状态信号（nav-dot、nav-count）必须**无悬停也可理解**（title / aria-label / 文本），且指向能解决该状态的入口；不得让用户对着一个裸点猜含义。

**判定方法**：读屏 / 无鼠标走查：能复述 dot 的含义；顺着指向两层内到达解决动作。
**现状**：🔧 部分修复。`#nav-sync-dot` 已补 `title` / `aria-label`「本地配置有未同步变更」（index.html，Lead 批准的属性级新增）。**残留 ⏳**：dot 挂在「设置」而解决动作在「模型与方案 → 高级」，层级错位——DOM 冻结下不移动节点，靠 save-state 的「· 待同步」文本线补偿；若未来解冻，dot 应移至 models 导航项。

### D15 · 键盘全路径

核心任务（切换模型、看请求详情、批量删除）必须全程纯键盘可完成：行用 tabindex + Enter/Space 激活，详情打开时焦点主动落进面板，dialog 内 Tab 循环由原生 modal 保证。

**判定方法**：一次完整的「切模型」与「看请求详情」纯键盘走查（Tab / Enter / Esc / Delete），中途不触碰鼠标。
**现状**：🔧 已修复（第二轮收口）。行激活与 `openDetail` 主动聚焦为原有实现；第一轮补 D1（editor 首焦）、D3 的 select 还焦，第二轮补 D2（关闭还焦）与 D3 手动路径后，主链路完整；收口动作分别记录在 D1 / D2 / D3 各条。

---

## 2. 本轮修复对照表（B1–B12 账本）

审计缺口编号沿用第一阶段报告；「落点」为函数级位置（行号会漂移，以函数为准）。验证基线：`node tests/ui_contract.cjs`（27 项）+ `node tests/ui_async_contract.cjs` 本轮全绿；「补回归」列为建议追加、尚未落地的浏览器级用例。

| # | 问题 | 落点 | 状态 | 验证 |
|---|---|---|---|---|
| B1 | 「从客户端导入」永不开 dialog；失败时 TypeError 直出 | app.js `scanImportClient`（busy 内渲染 + `showModal()` 带 open 守卫；`importCandidates` 初始 `[]`） | 🔧 已修复 | ui_contract「ZCode import list」用例绿；补回归：Playwright 点击断言 dialog visible + 500 注入无英文 toast |
| B2 | 30s 轮询整页重建毁掉输入/选区/展开态 | workbench.js `snapshotObserveInputs` / `restoreObserveInputs` / `renderObservationPreserving` + `loadObservation(poll)` 数据未变跳过；reading 轮询 2.5s→5s 降频 | 🔧 已修复 | 补回归：输入中途触发 `loadObservation(true)`，断言值/选区/滚动/展开态保持 |
| B3 | 重绘系统性丢焦点 | app.js `renderModelSyncCard` + `focusSelectorFor`（id 与既有 data-* 定位、按容器还原）；workbench.js `renderObservationPreserving`（轮询）/ `renderObservationManualPreserving`（手动）；workbuddy.js `paintWorkbuddy` 无变化跳过；关闭还焦见 D2（app.js `captureDialogOpener` / `restoreDialogFocus` + busy 收口） | 🔧 已修复（第二轮收口） | ui_contract 27 项 + ui_async 2 项绿；补回归：Playwright 逐交互断言 activeElement≠body（用例名 `D3-focus-after-rerender`） |
| B4 | 「全部更新」无确认，与单实例更新不一致 | workbench.js `check-all`/`update-all` 分支补 confirmDialog（实例数 + 重启提示） | 🔧 已修复 | 补回归：断言点击 update-all 先出现 `#confirm-dialog` |
| B5 | editor dialog 首焦点落在关闭按钮 | app.js `showEditor` 聚焦第一个 input/select/textarea | 🔧 已修复 | 补回归：打开「连接新的供应商」断言 activeElement 为 ID 输入框 |
| B6 | IME 组词回车误触发观测搜索 | workbench.js obs-search keydown 补 `!event.isComposing` | 🔧 已修复 | 补回归：合成事件 `{key:'Enter', isComposing:true}` 断言不触发 |
| B7 | 设置页观测失败黏死、无重试、文案误导 | workbench.js `renderSettings` error 态 blank + `obs-retry` 按钮 | 🔧 已修复 | 补回归：注入 `/api/observation` 500 断言重试按钮出现 |
| B8 | bulk 期间 2s 轮询重置详情抽屉滚动/焦点 | workbench.js `refreshAgentDetail` 以 inventory 快照跳过无变化重写 | 🔧 已修复 | 补回归：bulk 运行中打开详情断言 scrollTop/activeElement 稳定 |
| B9 | nav-sync-dot 语义不自明且层级错位 | index.html `#nav-sync-dot` 补 title/aria-label（Lead 批准） | 🔧 部分修复 | 读屏走查能复述含义；**未修**：dot 挂「设置」而动作在「模型与方案→高级」（DOM 冻结），由 save-state「· 待同步」文本补偿 |
| B10 | 剪贴板复制失败完全静默 | workbench.js `copy-command` catch 补 toast | 🔧 已修复 | 补回归：拒绝剪贴板权限断言出现「复制失败」toast |
| B11 | busy 期间点导航被静默吞掉 | app.js `navigate` busy 分支补 toast | 🔧 已修复 | 补回归：busy 窗口内点 nav 断言 toast |
| B12 | 供应商页空态弱 +「检测」一词三义 | app.js `renderProviderCards` empty() 引导卡；词汇表：「预览变更」「拉取模型列表」全量替换（按钮/标题/busy/toast/注释 8 处，含错别字「检测哪来连接」修正）；workbench.js 按钮/进度句「检测更新」→「检查更新」；agent_inventory.py 后端提示同步 | 🔧 已修复 | 四套契约 + unittest 全绿 |

**账本小结**：12 条中 11 条已修复、1 条部分修复（B9 层级错位，DOM 冻结下的有意保留）；无新增回归。B9 的补偿措施记录在 D14，不另立新条目。

### 第二轮修复对照（延后项，用户批准后实施）

第一轮 B1–B12 账本之外的修复，同账本文风记录（行内含落点与验证，不再编号）。

| 问题 | 落点 | 验证 |
|---|---|---|
| 对话详情长会话一次渲染全部消息，打开转圈近秒（实测 300 条端到端 ~0.9s） | workbench.js 消息分批渲染：首批 50 条即时可读，「显示剩余 N 条」按批追加，重新打开归零（workbench.js:476-490） | 打开 300+ 条会话断言首批 ≤50 条且立即可交互；走查「显示剩余」逐批追加 |
| 趋势图范围超 120 天时逐日画柱过密、无法读 | observation.py:1282-1290 范围 >120 天按 ISO 周聚合到周一；`trendGranularity` 随响应如实标注，前端轴标同步写「UTC 周 · 长范围已按周聚合」，不让周聚合冒充逐日（workbench.js overview 轴标） | 断言 >120 天响应 `trendGranularity==='week'`、轴标含「按周」；≤120 天仍为逐日 |
| 桌面桥接（WebMessage）请求无独立超时，卡死时用户无反馈 | bridge.js:74-81 60s 超时——比 app.js `api()` 的 120s 先触发、文案更可行动，两层超时各司其职 | 注入不响应的 pywebview api，断言 60s 内转为可行动错误文案 |
| ≤620px 断点整体隐藏 .brand，与 ≤850 icon-rail 形态不一致 | daily.css:280-281 ≤620 不再隐藏 .brand，文字 span 由 ≤850 规则隐藏、只留 brand-mark 印记，两断点对齐 | 620 / 850 两宽度截图对照，brand-mark 均在位且形态一致 |

---

## 3. 回归执行约定

- **每次涉及 ui/*.js 交互行为的改动**：跑 `node tests/ui_contract.cjs && node tests/ui_async_contract.cjs`（当前全绿基线）；涉及观测页渲染路径的加跑 `node tests/daily_browser.cjs`。
- **对照表「补回归」列**：落地时优先加进 `tests/browser.cjs` / `tests/daily_browser.cjs`（Playwright 已在环境中），每条用 D 线编号命名用例（如 `D4-poll-preserves-input`），让本文件与测试一一对应。
- **本文件与 visual-direction.md 同级维护**：交互行为的规范变更（新增 D 线、修改判定）需在文末补记一行变更原因与日期，不静默改判定线。

---

## 4. 变更记录

- **2026-10-20 初版**：D1–D15 判定线 + 第一轮（Wave 1）修复对照表（B1–B12）。
- **2026-10-21 第二轮**：用户批准修复延后项。D2 ⏳→🔧（app.js `captureDialogOpener` / `restoreDialogFocus` + busy 收口）；D3 转已修复（workbench.js `renderObservationManualPreserving` 手动路径、workbuddy.js `paintWorkbuddy` 无变化跳过、app.js `focusSelectorFor` data-* 定位）；D15 随之收口；B3 账本行更新；补记第二轮四项修复对照（对话详情分批 / 趋势周聚合 / bridge 60s 超时 / 620px brand 对齐）。
