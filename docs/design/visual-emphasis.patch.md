# 视觉重点修订补丁 · visual-emphasis.patch

> 本文是历史改动记录，路径和行号可能已变化；当前代码入口见[开发者导航](../developer-guide.md)。

> 上游文档：`docs/design/visual-direction.md` §10（2026-10-20 用户反馈修订）。
> 本文件是**可直接实施的改动清单**：CSS 含选择器与完整声明，JS 精确到文件与行号。
> 实施者：Phase 3。**不许改业务逻辑**，只许按本文件加 class / 改颜色声明。

## 0. 总则

- **不动的**：纸质感三图层、四级发丝线、衬线标题双轨制、字体三件套、零模糊阴影、圆角 ≤5px、
  动效清单、`--unknown` 石墨虚线、单一朱砂（不得引入第二个色相）、全部现有 class/id/`data-*`。
- **改的**：朱砂的**位置**（从 hover/禁用/小图标，移到数据焦点上）、语义徽标的淡填充、
  指标卡的主次与语义边线、墨阶的分配。**不是增加颜色总量，是重新分配。**
- 新增 class 只有三个，全部由 JS 数据驱动：`trend-column.active`、`context-chart button.peak`、
  `lead`（distribution / metric / stat）。
- **缺口 B（指标卡语义边线）零 JS 改动**：`data-value="failed"/"unknown"` 早已渲染在 DOM 上
  （workbench.js:129），缺的只是 CSS。用属性选择器直接接上，不碰 JS。

---

## 1. CSS 补丁

### A. 图表焦点（`ui/daily.css`）

**A1. 替换 `ui/daily.css:158-162`**（trend 柱块，原 5 行 → 8 行）：

```css
/* 图表柱默认是墨；一图一朱砂：焦点柱由 JS 加 .active（当前限定日 / 最新一天） */
.trend-column i { display: block; width: 100%; min-width: 11px; background: var(--rule-strong); border-radius: 1px 1px 0 0; }
.trend-column:hover i { background: var(--ink-3); }
.trend-column.active i { background: var(--accent); }
.trend-column.active:hover i { background: var(--accent-strong); }
.trend-column.active span { color: var(--accent); font-weight: var(--w-semibold); }
.trend-column small { font-size: 9px; color: var(--ink-2); }
.trend-column.active small { color: var(--accent); }
```

（`.trend-column small` 同时执行 D 表墨阶升位：ink-3 → ink-2，日期轴标签是数据。）

**A2. 在 `ui/daily.css:324`（`.context-chart button:hover i`）之后追加**：

```css
/* 峰值柱 = 卡片标题已宣布的「单次输入峰值」，数字与柱子互相印证 */
.context-chart button.peak i { background: var(--accent); }
.context-chart button.peak:hover i { background: var(--accent-strong); }
```

**A3. 在 `ui/daily.css:338`（`.distribution-list button:hover i`）之后追加**：

```css
/* 份额首行（sort 后 index 0 = 最大来源）*/
.distribution-list button.lead i { background: linear-gradient(to right, var(--accent) var(--share, 0%), var(--rule-faint) var(--share, 0%)); }
.distribution-list button.lead:hover i { background: linear-gradient(to right, var(--accent-strong) var(--share, 0%), var(--rule-faint) var(--share, 0%)); }
```

### B. 语义徽标淡填充（`ui/styles.css:317-327` + `ui/daily.css:104-105`）

**判定线**：颜色回答「现在怎么了」（green/red/amber 状态）→ 可淡填充，且只能用已有 `--*-wash` token；
回答「这是谁」（purple/blue 身份）→ 永远 1px 描边。`--accent` 永不入徽标；`unknown` 永远虚线不填充。

**B1. 替换 `ui/styles.css:319-326`**：

```css
.tag { display: inline-flex; align-items: center; gap: 4px; border: 1px solid var(--rule); border-radius: var(--r-xs); padding: 1px 5px; font-size: var(--fs-2xs); color: var(--ink-2); background: transparent; white-space: nowrap; }
.tag.purple { color: var(--cat-3); border-color: var(--cat-3); background: transparent; }
.tag.blue { color: var(--cat-1); border-color: var(--cat-1); background: transparent; }
/* 语义状态徽标：淡填充。wash 是 L≈92% 的纸邻色 + 2px 圆角 + 1px 同色描边，读作印戳而非胶囊 */
.tag.green { color: var(--success); border-color: var(--success); background: var(--success-wash); }
.tag.amber { color: var(--warn); border-color: var(--warn); background: var(--warn-wash); }
.tag.red { color: var(--danger); border-color: var(--danger); background: var(--danger-wash); }
/* 未知 / 未采集：虚线 = 缺失，永不填色 */
.tag.unknown { color: var(--unknown); border-style: dotted; background: transparent; }
```

（`.tag` 基色 ink-3 → ink-2 同步执行 D 表：中性徽标是「值」不是「注」。）

**B2. 替换 `ui/daily.css:104-105`**（daily.css 后加载，不同步改则 styles.css 的填充会被覆盖掉）：

```css
.tag.red { color: var(--danger); border-color: var(--danger); background: var(--danger-wash); }
.tag.amber { color: var(--warn); border-color: var(--warn); background: var(--warn-wash); }
```

### C. 指标卡主次与语义边线（`ui/styles.css:282` 后 + `ui/daily.css:142` 后）

**C1. `ui/styles.css` 在 `.stat-icon svg` 规则之后追加**：

```css
/* 主卡 = 本页主题词那一张。复用「选中行」语言：wash + 2px 朱砂左线，不做整面填充 */
.stat.lead { background: var(--accent-wash); box-shadow: inset 2px 0 0 var(--accent); }
.stat.lead .stat-icon { color: var(--accent); border-color: var(--accent); }
```

**C2. `ui/daily.css` 在 `.metric small` 规则之后追加**：

```css
/* 主指标卡 = 页面主题（已记录调用）。不用数值大小驱动：量纲不可比，且 30s 轮询会让焦点跳走 */
.metric.lead { background: var(--accent-wash); box-shadow: inset 2px 0 0 var(--accent); }
.metric.lead:hover { background: var(--accent-wash); }

**C3. 指标卡语义边线（缺口 B，`ui/daily.css` 紧随 C2 之后；零 JS 改动）**

```css
/* 指标卡网格的三层语言：
   inset 2px 线（box-shadow）= 选中/主卡（C2，瞬态，不改变盒模型）；
   border-left 2px = 语义状态（稳态标注，沿用 §4.6 form-error / §4.7 toast 的边线语言）；
   线型编码确定性：实线 = 已确认的事实，点线 = 未确认的缺失（沿用 .tag.unknown / --unknown-rule）。
   失败 = 牛血红 2px 实线、不填色：四卡里一张红底会与主卡 wash 竞争层级，且 danger 比 accent
   更深，铺底即喊叫；2px 边线是「这张卡承载异常类别」的注记，严重度由数字承载（0 失败也是好信息）。
   结果未确认 = 石墨 2px 点线：未知是「缺失」不是「状态」，永不填充、永不显示为正常。
   选择器必须带 .metric 前缀——data-value 同时存在于 distribution 按钮（workbench.js:136，
   value 是模型/供应商名），裸 [data-value="failed"] 可能撞上名为 failed 的模型。
   两条 :hover 守卫（0-3-0）用来压过 button:hover:not(:disabled)（styles.css:151，0-2-1）
   对 border-color 的重设——否则 hover 时点线会变 ink-3，丢掉石墨语义。 */
.metric[data-value="failed"] { border-left: 2px solid var(--danger); }
.metric[data-value="failed"]:hover { border-left-color: var(--danger); }
.metric[data-value="failed"] > span { color: var(--danger); }
.metric[data-value="unknown"] { border-left: 2px dotted var(--unknown); }
.metric[data-value="unknown"]:hover { border-left-color: var(--unknown); }
.metric[data-value="unknown"] > span { color: var(--unknown); }
```

说明：
- 只有 `<span>`（类别标签）上语义色；`<strong>`（大数字）保持 `--ink`——**数据即墨**，未确认的*计数*
  本身是已测得的事实（"存在用量，但缺少最终状态证据"），上石墨色会把measured count误读成missing value。
- 标签色对比度：`--danger` 7.6:1 / `--unknown` 5.0:1（`--surface` 上），11px 达标。
- `box-sizing:border-box`（styles.css:126）全局生效，border-left 不撑破 grid 轨道，内容仅内缩 2px。
- 边线是**静态类别标记**，不随数值开关（失败=0 时边线仍在）——它标识卡的类别，不是告警；
  告警程度由数字传达。这也保住了 CSS-only、零 JS 风险。
- 按钮可点击性认知不受影响：border-left 不是包裹四边的边框变化，hover 仍有 `--surface-sunk` 底
  + `:focus-visible` 朱砂 outline（styles.css:165），钻取行为（workbench.js:238）完全不变。
- 「当前筛选是否限定 failed」不额外高亮：筛选栏的 select 已显示当前值（observationFilters），
  卡片层再做一遍是冗余。
```

### D. 墨阶升位表（数据升 `--ink-2`，脚注数据升 `--ink-3`）

**判定规则**（写给后续所有新规则，不只是本次 23 条）：
- `--ink-2`：表格主文本、**一切数值/计数/ID/路径/状态/轴标签**——删掉会丢信息的内容。
- `--ink-3`：标签、表头、卡片副标题、说明性小字——帮助阅读但不承载事实。
- `--ink-4`：脚注、单位后缀、徽标数字、时间戳小字——**仅「删掉也不丢信息」的信息**。10px 的 ink-4 是全场最弱可见度，不允许出现在数据上。

**D1. `ui/daily.css`（13 条）**：

| 行 | 选择器 | 现 → 改 | 内容 |
|---|---|---|---|
| 60 | `.agent-table small, .agent-table .cell-secondary` | ink-3 → **ink-2** | 「N 个安装实例」计数 |
| 80 | `.model-current span` | ink-3 → **ink-2** | 供应商名 |
| 98 | `.path-line` | ink-3 → **ink-2** | 路径 |
| 107 | `.collection-line` | ink-3 → **ink-2** | 采集状态行 |
| 142 | `.metric small` | ink-4 → **ink-3** | 注脚含样本计数 |
| 150 | `.token-grid small` | ink-4 → **ink-3** | 「3 / 12 次有数据」 |
| 162 | `.trend-column small` | ink-3 → **ink-2** | 日期轴标签（并入 A1） |
| 167 | `.coverage-list span` | ink-3 → **ink-2** | 文件计数 |
| 187 | `.requests-table small` | ink-3 → **ink-2** | Agent 名 / 返回模型 |
| 198 | `.conversation-main small` | ink-3 → **ink-2** | Agent / 时间戳 |
| 201 | `.conversation-count small` | ink-3 → **ink-2** | Token / 费用数值 |
| 217 | `.collection-path` | ink-3 → **ink-2** | 索引路径 |
| 325 | `.context-chart small` | ink-3 → **ink-2** | 序号轴标签 |

**D2. `ui/styles.css`（10 条）**：

| 行 | 选择器 | 现 → 改 | 内容 |
|---|---|---|---|
| 250 | `.save-state` | ink-3 → **ink-2** | 保存状态 |
| 299 | `.filter-summary` | ink-4 → **ink-2** | 筛选计数 |
| 313 | `.model-id` | ink-3 → **ink-2** | 模型 ID |
| 314 | `.provider-label` | ink-3 → **ink-2** | 供应商名 |
| 316 | `.token-value` | ink-3 → **ink-2** | Token 数值 |
| 319 | `.tag` | ink-3 → **ink-2** | 中性徽标（并入 B1） |
| 331 | `.table-footer` | ink-3 → **ink-2** | 页码计数 |
| 347 | `.provider-url` | ink-3 → **ink-2** | 端点 URL |
| 378 | `.target-state` | ink-4 → **ink-3** | 目标状态标签 |
| 411 | `.ms-resolved` | ink-3 → **ink-2** | 解析出的供应商/模型 |

---

## 2. JS 改动点（只加 class，不动业务逻辑）

### J1. 趋势图焦点柱 — `ui/workbench.js:131-135`

在 `overviewHtml` 的 `return` 之前插入焦点日期计算，并在 map 里加 class：

```js
function overviewHtml(data) {
  const s=data.summary;
  // 一图一朱砂：已限定到的那一天；否则数据里最新的一天（UTC 今天 = 仍在累积的此刻）
  const trendFocus=(()=>{const f=daily.filter;
    if(f.start&&f.end&&f.start%86400===0&&f.end<f.start+86400){
      const d=new Date(f.start*1000).toISOString().slice(0,10);
      if(data.trend.some(x=>x.date===d))return d;}
    return data.trend.length?data.trend.reduce((a,b)=>a.date>=b.date?a:b).date:'';})();
  return `...<div class="trend-chart">${data.trend.map(d=>`<button class="trend-column${d.date===trendFocus?' active':''}" data-daily="trend" data-date="${d.date}" title="${d.date} · ${d.calls} 次调用">...`).join('')}</div>...`;
}
```

驱动数据与鲁棒性说明：
- `f.start%86400===0 && f.end<f.start+86400` 精确识别「趋势柱点击」留下的单日筛选
  （workbench.js:243 设 start=UTC 零点、end=start+86400−0.001）。默认 7/30/365 天范围跨度远大于一天，
  不会误判；loadObservation 的 range 钉扎同样不会误判。
- 回退到 `reduce` 取最大日期，不假设数组顺序（observation.py:798 虽已 sorted，但不依赖）。
- 用户点柱 → 跳转请求页；返回概览时该日仍带 `active`——「你在看的那天」保持可见。
- `data.trend` 为空时 `trendFocus=''`，无焦点柱，不报错。

### J2. 分布图首行 — `ui/workbench.js:136`

```js
// 现状：.map(x=>`<button data-daily="distribution" ...
// 改为：.map((x,i)=>`<button class="${i===0?'lead':''}" data-daily="distribution" ...
```

驱动数据：该行已按 `b.calls-a.calls` 降序，index 0 即份额最大者。三个维度卡（Agent/供应商/模型）各有一根。

### J3. 上下文图峰值柱 — `ui/workbench.js:150`

```js
// 在 points.map 之前：
const peak=points.length?points.reduce((best,p,i)=>((p.input||0)>(points[best].input||0)?i:best),0):-1;
// map 内：
points.map((p,i)=>`<button class="${i===peak&&(p.input||0)>0?'peak':''}" data-daily="request" data-id="${p.id}" ...`
```

驱动数据：`p.input` 最大值 = 卡片标题已宣布的「单次输入峰值 X Token」——朱砂柱与标题数字互证。
全零输入时不标（`(p.input||0)>0` 守卫），避免把「无数据」画成焦点。

### J4. 主指标卡 — `ui/workbench.js:128-130, 133`

```js
function metric(label,value,note,filter='',status='',lead=false) {
  return `<button class="metric${lead?' lead':''}" data-daily="metric" data-filter="${filter}" data-value="${status}"><span>${label}</span><strong>${value}</strong><small>${note}</small></button>`;
}
// 第一个调用（已记录调用）加第六参 true：
metric('已记录调用',fmt(s.calls),`${s.succeeded} 次已完成 · ${s.interrupted} 次中断`,'','',true)
```

驱动数据：页面主题词（观测页 h1「已记录调用的局部视图」的主语），非数值大小。

### J5. 主统计卡 — `ui/app.js:643-645, 660, 706`

```js
function stats(lead=0) {
  return `<div class="stats"><div class="stat${lead===0?' lead':''}">...已管理模型...</div><div class="stat${lead===1?' lead':''}">...已连接供应商...</div><div class="stat">...参与同步的平台...</div></div>`;
}
// app.js:660（模型页）  stats() → stats(0)
// app.js:706（供应商页）stats() → stats(1)
```

驱动数据：当前页主题词（模型页=已管理模型；供应商页=已连接供应商）。**保留 `stat-icon blue` / `stat-icon green`
class 输出**（选择器守恒；CSS 已把它们中性化，lead 卡的图标由 C1 的 `.stat.lead .stat-icon` 上朱砂）。
同步页不渲染 `.stats`，无需改。

### J6.（建议）详情弹窗状态徽标 — `ui/workbench.js:156`

```js
// 现状：tag(statusNames[r.status])
// 改为：tag(statusNames[r.status],r.status==='failed'?'red':r.status==='succeeded'?'green':'')
```

驱动数据：与请求表格同一状态词，保持 wash 语义一致。

---

## 3. 明确不做（防止过度修正）

1. 趋势图**不许多根**朱砂、不 top-3 高亮、不按数值大小渐变、不彩虹柱。
2. 身份徽标（`.tag.purple/.blue`、`--cat-*`、provider/agent 标识）**不得**加任何填充。
3. `.tag.unknown` 保持虚线无填充；`--accent` 不得作为徽标底色。
4. 不得新增第二个强调色相；不得把 `.lead` 卡做成整面朱砂（只能 wash + 2px 左线）。
5. `button.primary` 制度不变（朱砂实底只给写入动作，可见工具栏 ≤1）；观测页是只读页，
   「读取新记录」**不升** primary——只读页的重点由图表焦点与主卡承担。
6. 不得动纸质感三图层、四级发丝线、衬线双轨、字体三件套、动效清单、`[hidden]` 语义。
7. 不得删除任何现有 class/id/`data-*`；新增 class 必须由上述 JS 驱动（不允许死规则）。

---

## 4. 验收清单

1. `python scripts/verify-css.py` 通过（选择器守恒 / token / 反模式 / 圆角 / 字重 / 无 :root in daily.css）。
2. `node tests/ui_contract.cjs && node tests/ui_async_contract.cjs && node tests/browser.cjs && node tests/daily_browser.cjs` 全绿。
3. 观测概览：调用趋势**恰好一根**朱砂柱（默认=最新一天；点某日跳请求页后返回概览=该日，data-date 驱动）；
   三张分布卡各有一根朱砂 sliver（首行）；「已记录调用」卡有 wash + 左线；
   「失败」卡左侧 2px 牛血红实线、「结果未确认」卡左侧 2px 石墨点线（hover 不变色、不填充）；
   请求表格状态徽标有淡填充且身份徽标（连接/能力）仍为描边。
4. 模型页：「已管理模型」卡 wash + 左线 + 朱砂图标；供应商页同理（第二张）。
5. 上下文页：输入峰值柱朱砂，与标题「单次输入峰值」数字一致。
6. 朱砂预算：任何视口 accent 实色像素 <3%（修订后观测概览 ≈0.15%，模型页 ≈0.3%）。
7. 1440×960 与 760/390 宽下无横向溢出（daily_browser 的 noOverflow 已覆盖）。
