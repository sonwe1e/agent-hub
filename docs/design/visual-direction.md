# Agent Hub 视觉方向规范 · Phase 1

> 本文保留当时的设计和修订记录，所列路径和行号可能已变化；当前代码入口见[开发者导航](../developer-guide.md)。

> 适用：`ui/styles.css`、`ui/daily.css` 的**纯样式重做**。
> 硬约束：DOM 结构、`class` 名、`id`、`data-*` 属性一律不动（`tests/browser.cjs`、`tests/daily_browser.cjs`、`tests/ui_contract.cjs`、`tests/ui_async_contract.cjs`、`tests/workbuddy_browser.cjs`、`tests/live_browser.cjs` 依赖它们）。
> 目标宿主：pywebview 单进程桌面 exe，1440×960 独立窗口，**离线运行，无地址栏 / tab / 刷新按钮**。

---

## 实施记录（2026-10-20）

规范已全部落地。`ui/styles.css` / `ui/daily.css` / `ui/fonts.css` 即本文档的执行结果；
`scripts/verify-css.py` 可确定性回归本文档的硬约束。

**对 §8.1 的两处必要偏离**（均为桌面壳的传输层需要，不涉及视觉）：

1. `ui/index.html` 的 4 处绝对路径改为相对路径（`/styles.css` → `styles.css` 等）。
   原因：桌面版用 `file://` 加载页面，`/styles.css` 会解析成 `file:///styles.css` 而 404，
   导致整个界面不加载。相对路径在浏览器模式（`http://127.0.0.1:8787/`）下同样正确。
2. `ui/index.html` 新增 `<script src="bridge.js">`（置于 `app.js` 之前）。
   作用：把页面里的 `fetch('/api/...')` 改写为一次 WebView2 WebMessage，直达进程内的
   `server.dispatch_api`。浏览器模式下检测不到 `window.pywebview` 会原样放行，
   因此两种模式的界面代码完全相同，回归测试不受影响。

**§8.2 的样式文件治理已执行**：`daily.css` 的 `:root` 块已删除，token 单一来源为
`styles.css`；`verify-css.py` 会断言 `daily.css` 不再定义 `:root`。

**2026-10-20 视觉重点修订**：用户实测反馈「功能正常了，但 UI 很丑，而且没有视觉重点」。
量化核实后确认这是**执行偏差而非方向错误**——「克制」被执行成了「几乎无色」。
修订结论与可实施改动见 **§10** 与同目录 `visual-emphasis.patch.md`（Phase 3 实施）。

**2026-10-21 执行修订（Phase 5）**：视觉审计发现缺口集中在「JS 渲染层绕过 token 体系」
与「制度网眼」（非方向本身）。裁定与变更（JS 身份色收编 --cat-*、失败统一牛血红、
inline 警告收编 notice warn、tnum/墨阶/朱砂预算/基形等 CSS 补丁、死 token 保留位、
brand 显隐统一（Wave A，断点双轨仍悬置））见 **§12**。

**字体落地与 §3 的偏差**：规范建议 B/C 档按界面用字子集化打包 CJK。实测本机已注册
`Noto Sans SC` / `Noto Serif SC` 可变字体（`HKLM\...\Fonts`），因此 v1 只发 A 档
（4 个拉丁 woff2，104 KB），CJK 走系统字体的逐字形回落。若将来要保证跨机一致，
再按 §3.2 的 `pyftsubset` 流程补 B/C 档。

---

## 0. 一句话方向

**把 Agent Hub 从「SaaS 仪表盘」改成「本机台账」**——暖骨白纸面、发丝线分隔、去阴影、墨黑正文、单一朱砂强调色只点刺在"此刻唯一在发生的事"上。

---

## 1. 血缘与判断

### 1.1 这套视觉为什么属于这个产品

Anthropic 视觉真正可迁移的不是 Claude 橙，而是 **editorial / 纸质感**这套编辑设计语言。它能不能站住，取决于有没有共同祖先——有：

| Anthropic | Agent Hub | 共同祖先 |
|---|---|---|
| 安全研究论文 / 技术报告 | 本机私有、可核查的观测与配置记录 | **可核查的技术文献** |
| 引用必须标源 | 「观测来源与未知项始终可见」(侧栏原文) | 主张必须可溯源 |
| 承认不确定性 | 「未知就显式显示未知，不生成健康分，不用配置倒推实际调用」(README 原文) | 不确定性必须可见，不得被填补 |
| 纸面排版、发丝线、去装饰 | 密集数据表 + 事务预览 + 回滚 | 长文/账目的阅读载体 |

Agent Hub 侧栏自己写着 **「本机管理 · 有据可查」**，README 反复强调不做健康分、不用配置倒推实际调用、未知价格不显示为免费。**克制、可信、可核查**是这台机器的性格。纸面/台账这套材质是这套价值观的物理祖先；紫色 SaaS 仪表盘的祖先是"营销看板"，其价值观是说服而非核验——与本品相反。所以合法性来源是**材质与产品行为同源**，不是"因为 Anthropic 好看"。

### 1.2 我砍掉了哪些「抄 Anthropic 但不成立」的做法

替换测试（把主题换成同类任意产品、整套设计依然成立 → slop）逐条跑过：

| 砍掉的做法 | 为什么在本品不成立 |
|---|---|
| **Claude 橙 `#D97757` 作主色** | 它是 Anthropic 的*品牌标*色。Agent Hub 是本机工具，无品牌诉求；刷橙会让界面读作"Claude 的产品"。替换测试：把橙色方案套到任意 LLM 控制台都成立 → slop。改为朱砂墨 `#A93F24`（H12°/S65%/L40%，比 Claude 橙低 19% 明度、低 8° 色相），祖先换成**账页上的朱笔批注 / 校对红线**，正是本品在做的事：标记"哪些是真的、哪些只是配置"。 |
| **超大号衬线 hero 标题（40–64px）** | 1440×960 的密集工作台，60px 衬线会吃掉整屏。衬线只在 **16–28px** 使用（h1、dialog 标题、指标数字），永不做 hero。 |
| **大面积暖色块作分区背景** | Anthropic 官网用大块暖色做 section 底。工作台里，色块衬在表格下方会摧毁行扫描。禁止色块背景，页面保持单一连续纸面。 |
| **柔和圆角 + 投影卡片（现有 `--radius:14px` + soft shadow）** | 来自 SaaS 卡片隐喻，不是印刷传统。改为圆角 ≤5px + 零模糊阴影。 |
| **橙→奶油的大面积渐变** | 用户要"背景有氛围"，但"层叠渐变"在 Anthropic 那里是品牌色渐变。改为**物理材质三层**：台灯光晕（4.5% alpha 径向）、账页横格（28px 周期、2.2% alpha）、纸纤维噪点（3% multiply）。每一层都有可追溯的物理祖先，且都不引入色彩。 |
| **满屏微交互 / staggered fade-up** | 与 Inter 同级老套，明确禁用。开场改为"先落格线、再落内容"的单次编排动作。 |

---

## 2. Token 表

### 2.1 可直接抄写的 `:root`

```css
:root {
  /* ---- 纸面与墨（暖中性，全靠明度统治） ---- */
  --paper:        #F7F3EB;  /* 工作区纸面（最亮，承载内容） */
  --paper-sunk:   #F0EAE0;  /* 侧栏 / 页边下沉区 */
  --surface:      #FCFAF6;  /* 卡片、dialog、输入框（贴在纸上的纸片） */
  --surface-sunk: #F2EDE4;  /* 只读行、禁用控件、行 hover */
  --ink:          #1E1C19;  /* 主文字，15:1 */
  --ink-2:        #4A453E;  /* 次级文字 / 表单值，8.5:1 */
  --ink-3:        #6E675C;  /* 标签 / 表头 / 占位符以外的小字，5.0:1 */
  --ink-4:        #8B8478;  /* 脚注 / 徽标数字，3.6:1（仅非必要信息） */
  --graphite:     #6E685E;  /* 「未知 / 未采集」专用铅笔色，5.0:1 */

  /* ---- 发丝线三级体系（替代阴影，是本方案最大杠杆） ---- */
  --rule-faint:   #E7DFD0;  /* 表格行分隔（最弱） */
  --rule:         #D8CFBE;  /* 卡片内部分隔、只读边框（装饰性，不需 3:1） */
  --rule-strong:  #C0B5A0;  /* 卡片外框、表头下沿、dialog 边框 */
  --rule-control: #A79C8A;  /* 输入框 / 按钮边框，3.1:1（UI 组件边界需达标） */

  /* ---- 强调色：朱砂墨。只用于「此刻唯一在发生的事」 ---- */
  --accent:        #A93F24; /* 主强调，5.5:1（纸面上） */
  --accent-strong: #8A3119; /* hover / active / pressed，7.9:1 */
  --accent-bright: #C2502F; /* 仅用于非文字：采集脉冲点、2px 规则、图表高亮柱 */
  --accent-wash:   #F6E8E1; /* 选中行底色（唯一允许的大面积强调，且仅一行） */

  /* ---- 语义色：全部是「墨」，明度统一在 L35–45%，靠色相区分 ---- */
  --success:      #3D6B54;  /* 松墨绿，5.5:1 */
  --success-wash: #E9EFE8;
  --danger:       #8C2B22;  /* 牛血红，7.6:1（刻意比 accent 更深，避免混淆） */
  --danger-wash:  #F5E7E3;
  --warn:         #8A5A12;  /* 赭石墨，5.3:1 */
  --warn-wash:    #F6EEDC;
  --unknown:      #6E685E;  /* 铅笔石墨（= --graphite） */
  --unknown-rule: #B9B2A4;  /* 未知项的虚线下划线 */

  /* ---- 分类色：provider / agent 身份色。刻意避开 5–25° 红橙区，给 accent 让位 ---- */
  --cat-1: #4A6B8A;  /* 石板蓝 */
  --cat-2: #5E7A4E;  /* 橄榄墨 */
  --cat-3: #7A5A82;  /* 桑葚墨 */
  --cat-4: #8A6A3A;  /* 生赭 */
  --cat-5: #3F7A70;  /* 松石墨 */
  --cat-6: #6B5560;  /* 乌贼墨 */

  /* ---- 字体 ---- */
  --font-display: "Newsreader", "Source Han Serif SC", "Noto Serif CJK SC",
                  "Songti SC", "STSong", SimSun, NSimSun, serif;
  --font-sans:    "IBM Plex Sans", "PingFang SC", "Hiragino Sans GB",
                  "Microsoft YaHei UI", "Microsoft YaHei", "DengXian",
                  "Source Han Sans SC", "Noto Sans CJK SC", sans-serif;
  --font-mono:    "IBM Plex Mono", "Cascadia Mono", "Cascadia Code",
                  "Sarasa Mono SC", "Microsoft YaHei", monospace;
  --fs-num: "tnum" 1, "lnum" 1;   /* 传给 font-feature-settings */

  /* ---- 字号阶梯（基准 13px，最大 28px，永不超过） ---- */
  --fs-2xs: 10px;  /* 脚注、表头、计数徽标 */
  --fs-xs:  11px;  /* 表格单元、元数据、mono ID */
  --fs-sm:  12px;  /* 按钮、表单标签 */
  --fs-base:13px;  /* 正文、表格主文本 */
  --fs-md:  14px;  /* h3、导航项 */
  --fs-lg:  16px;  /* h2 / 卡片标题 */
  --fs-xl:  19px;  /* dialog 标题（衬线） */
  --fs-2xl: 23px;  /* h1（衬线） */
  --fs-3xl: 28px;  /* 指标数字（衬线 + tnum） */

  /* ---- 行高 ---- */
  --lh-tight:   1.25;  /* 标题 */
  --lh-snug:    1.40;  /* 表格单元（紧排 = 编辑设计） */
  --lh-base:    1.55;  /* 正文 */
  --lh-relaxed: 1.75;  /* 说明段 / 确认弹窗正文（中文需要空气） */

  /* ---- 字重（只有四档；禁用 550/650/750） ---- */
  --w-regular: 400; --w-medium: 500; --w-semibold: 600; --w-bold: 700;

  /* ---- 字距 ---- */
  --ls-tight: -0.01em;  /* 衬线标题 */
  --ls-caps:   0.09em;  /* 仅拉丁小标签（.brand small 等） */
  --ls-none:   0;       /* 中文一律 0（见 §7 反模式 11） */

  /* ---- 间距标尺（4 基准） ---- */
  --sp-1: 4px; --sp-2: 8px; --sp-3: 12px; --sp-4: 16px;
  --sp-5: 24px; --sp-6: 32px; --sp-7: 48px;

  /* ---- 密度 ---- */
  --row-py:     10px;  /* 数据表单元纵向 */
  --row-py-lg:  16px;  /* 双行单元（agent 表） */
  --row-px:     14px;  /* 数据表单元横向 */
  --block-gap:  24px;  /* 卡片之间 */
  --section-gap:40px;  /* 区块之间 */
  --page-pad-x: 32px;
  --page-pad-y: 28px;
  --sidebar-w:  224px;
  --topbar-h:   56px;

  /* ---- 圆角（编辑设计：近乎方角） ---- */
  --r-xs: 2px;   /* 徽标、tag */
  --r-sm: 3px;   /* 输入框、按钮 */
  --r-md: 4px;   /* 卡片 */
  --r-lg: 5px;   /* dialog（上限，任何元素不得 >5px） */

  /* ---- 阴影：零模糊，只允许硬边压印 ---- */
  --shadow-none:  none;
  --paper-shade:  #E0D8C9;
  --shadow-press: 3px 3px 0 var(--paper-shade);  /* 仅 dialog / toast */
  --shadow-float: 2px 2px 0 var(--paper-shade);  /* 仅浮层 */
  --scrim:        rgba(30, 28, 25, .42);

  /* ---- 背景氛围三图层（见 §2.10） ---- */
  --field-light: radial-gradient(1200px 800px at 78% -10%, rgba(169,63,36,.045), transparent 60%),
                 radial-gradient(900px 600px at 8% 108%, rgba(30,28,25,.035), transparent 62%);
  --field-rule:  repeating-linear-gradient(to bottom, transparent 0 27px, rgba(30,28,25,.022) 27px 28px);
  --field-fiber: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/><feColorMatrix type='saturate' values='0'/></filter><rect width='140' height='140' filter='url(%23n)'/></svg>");

  /* ---- 动效（见 §6） ---- */
  --ease-out:    cubic-bezier(.22,.61,.36,1);
  --ease-inout:  cubic-bezier(.42,0,.58,1);
  --d-control:   120ms;  /* 控件状态切换 */
  --d-settle:    180ms;  /* 预览落位 */
  --d-mark:      240ms;  /* 批注下划线闪现 */
  --d-rollback:  320ms;  /* 回滚（刻意比应用更慢） */
  --d-open:      420ms;  /* 开场格线 */
  --d-scan:      1100ms; /* 扫描扫掠（无限循环） */
  --d-pulse:     2400ms; /* 采集脉冲（无限循环） */

  font-size: 13px;
  color: var(--ink);
  font-family: var(--font-sans);
  font-synthesis: none;
}
```

### 2.2 关键取值依据

- **强调色 `#A93F24`**：色相 12°、饱和 65%、明度 40%。相对 Claude 橙 `#D97757`（H20°/S62%/L59%）低 8° 色相、低 19% 明度 → 读作**墨**而非**品牌**。在 `--paper` 上对比度 **5.5:1**（AA 正文达标）；白字在其上 **6.1:1**（AA 达标）。
- **`--danger` 比 `--accent` 更深**（L 26% vs L 40%）：近距离可辨，远处一个是"亮点"一个是"暗墨"，不会误读。
- **`--unknown` = 铅笔石墨**：这是全表唯一**完全由产品行为推导**而非由风格推导的颜色。README 要求"未知就显式显示未知"，而给未知配一个彩色徽标会暗示它是一种**状态**；未知是**缺失**。所以未知项渲染为：**石墨色 em dash `—` + 虚线下划线，永远不给填充底**。
- **发丝线三级**：`--rule-control`(3.1:1) > `--rule-strong` > `--rule` > `--rule-faint`。规律是**边缘对比度编码可交互性**——能点的东西边框最深。

### 2.10 背景氛围（三图层，全部有物理祖先）

> ⚠️ **本节已作废（2026-10-20，Phase 4）**。下面的三图层实现（色斑 + 28px 横格 + 纸纤维噪点）
> 在生产环境被判为失败：横格与噪点在任意窗口尺寸下都会顺铺（tiling），全屏周期读作
> 扫描件伪影，不是纸。现行实现见文末 **§11 Phase 4：背景修订**。本节的"为什么需要
> 背景层次"这一判断仍然成立，只有"用什么手段做层次"被替换掉了。

```css
body {
  background-color: var(--paper);
  background-image: var(--field-light), var(--field-rule), var(--field-fiber);
  background-blend-mode: normal, normal, multiply;
}
.sidebar {
  background-color: var(--paper-sunk);
  background-image: var(--field-rule), var(--field-fiber);
  background-blend-mode: normal, multiply;   /* 侧栏需重复声明，否则 body 图层被覆盖 */
}
```

| 图层 | 物理祖先 | 参数 |
|---|---|---|
| `--field-light` | 台灯照在纸上的暖光晕 + 左下自然落影 | 两个径向渐变，alpha 4.5% / 3.5% |
| `--field-rule` | 账页横格 | 28px 周期、1px 线、alpha 2.2%（28px = 正文行律） |
| `--field-fiber` | 纸纤维 | feTurbulence 噪点，multiply，约 3% 视觉强度 |

**关键**：横格只出现在**页边与留白处**——卡片/表格用 `--surface`（不透明）盖住它。效果等于"一张打了横格的纸上贴了几张白纸片"，而不是"表格浮在格子上"。

---

## 3. 字体策略

### 3.1 为什么是这三个

| 角色 | 字体 | 祖先 / 依据 | 替代 Anthropic 的 |
|---|---|---|---|
| 展示（h1、dialog 标题、指标数字、品牌字标） | **Newsreader**（OFL） | 编辑衬线，源自新闻正文传统，带光学尺寸轴，暖而不过分复古 | Tiempos（商业，不可分发） |
| 正文 / UI / 表格 | **IBM Plex Sans**（OFL） | IBM 1970s 企业标识与工程标牌体系 —— 与"仪器/技术台账"同源；tabular 数字、无歧义字形 | Styrene（商业） |
| 等宽（模型 ID、路径、token、费用） | **IBM Plex Mono**（OFL） | 与 Plex Sans 同族，跨机一致 | — |

刻意**排除**：Inter / Roboto / Arial / system-ui（用户明令）；Space Grotesk（用户明令）；Source Serif / Lora / Spectral / Fraunces（已是"新的默认"）；Geist / Switzer / Public Sans（平台默认化）。

### 3.2 中文字体：分档决策

界面约 **90% 拉丁**（模型 ID、provider ID、绝对路径、token 数、费用、时间戳）+ **10% 中文**（导航标签、字段标签、说明句、toast、确认文案）。所以策略是**拉丁随包，中文分档**。

| 档 | 内容 | 体积 | 建议 |
|---|---|---|---|
| **A（必须）** | Newsreader 静态 400+600（Latin+LatExt）、IBM Plex Sans 可变 latin（wght 100–700）、IBM Plex Mono 静态 400 | **约 152 KB** | 首版即发 |
| **B（强烈建议）** | IBM Plex Sans SC Regular+Medium，**按界面实际用字子集化** | **约 +420 KB** | 消除"不同机器中文长得不一样" |
| **C（可选编辑感升级）** | Noto Serif SC Regular/Medium，**按标题用字子集化** | **约 +220 KB** | 让 h1 的中文真正落到宋体/明朝体 |

**CJK 为什么不整包随附**：Noto Sans SC / Plex Sans SC 全量 CJK woff2 为 5–10 MB，不划算。但**子集化到实际用字**只要几百 KB——因为 UI 字符串在构建期是已知的：

```bash
# 1) 从源码抽出全部 UI 用字
python - <<'PY'
import re, pathlib
chars=set()
for f in ["ui/app.js","ui/workbench.js","ui/workbuddy.js","ui/index.html"]:
    chars |= set(pathlib.Path(f).read_text(encoding="utf-8"))
chars = {c for c in chars if ord(c) > 0x2E7F}      # 只保留 CJK 及标点
pathlib.Path("ui/fonts/cjk.chars").write_text("".join(sorted(chars)), encoding="utf-8")
PY
# 2) 子集化（fonttools + brotli，项目已是 Python 工程）
pyftsubset IBM_Plex_Sans_SC-Regular.ttf \
  --text-file=ui/fonts/cjk.chars --flavor=woff2 \
  --layout-features='*' --no-hinting --desubroutinize \
  --output-file=ui/fonts/plex-sans-sc-400.woff2
```

**落地栈（A 档即可工作，B/C 档自动接管）**：

```css
/* 展示：拉丁 Newsreader，中文回落宋/明体（Windows 上 SimSun 立即可用） */
--font-display: "Newsreader", "Source Han Serif SC", "Noto Serif CJK SC",
                "Songti SC", "STSong", SimSun, NSimSun, serif;
/* 正文：拉丁 Plex Sans，中文 YaHei（显式点名，非 system-ui 兜底） */
--font-sans:    "IBM Plex Sans", "PingFang SC", "Hiragino Sans GB",
                "Microsoft YaHei UI", "Microsoft YaHei", "DengXian",
                "Source Han Sans SC", "Noto Sans CJK SC", sans-serif;
/* 等宽：路径可能含中文，末段补 YaHei 保可读性 */
--font-mono:    "IBM Plex Mono", "Cascadia Mono", "Cascadia Code",
                "Sarasa Mono SC", "Microsoft YaHei", monospace;
```

顺序正确性是关键的：拉丁字体列在前，中文字体列在后 —— 浏览器按**逐字形**回落，中文字符在 Plex Sans 中不存在才会落到后面的中文族，不会出现中文字体抢走拉丁的情况。

### 3.3 获取方式与合规

- 三者均为 **SIL OFL 1.1**，允许随包分发。来源：Google Fonts（`fonts.google.com/download` 取 woff2，或用 `fonts.googleapis.com/css2?family=...&display=swap` 取响应里的 woff2 URL）。
- 落盘位置 `ui/fonts/`，**必须同时放一份 `OFL.txt`**。
- **禁止** `<link href="https://fonts.googleapis.com/...">`：pywebview 桌面端离线运行，外链必然失败并触发 FOIT。必须 `@font-face` 指向本地 woff2。

```css
@font-face{font-family:"Newsreader";src:url("/fonts/newsreader-400.woff2") format("woff2");
  font-weight:400;font-style:normal;font-display:swap;unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+2000-206F,U+2074,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD;}
@font-face{font-family:"Newsreader";src:url("/fonts/newsreader-600.woff2") format("woff2");
  font-weight:600;font-style:normal;font-display:swap;unicode-range:/* 同上 */;}
@font-face{font-family:"IBM Plex Sans";src:url("/fonts/plexsans-var-latin.woff2") format("woff2-variations");
  font-weight:100 700;font-style:normal;font-display:swap;unicode-range:/* 同上 */;}
@font-face{font-family:"IBM Plex Mono";src:url("/fonts/plexmono-400-latin.woff2") format("woff2");
  font-weight:400;font-style:normal;font-display:swap;unicode-range:/* 同上 */;}
/* B 档：*/ @font-face{font-family:"Plex Sans SC";src:url("/fonts/plex-sans-sc-400.woff2") format("woff2");font-weight:400;font-display:swap;}
```

> 若走 B 档，把 `"Plex Sans SC"` 插到 `--font-sans` 中 `"IBM Plex Sans"` 之后、`"PingFang SC"` 之前。

### 3.4 tabular-nums 清单

以下选择器**必须**带对齐数字（现有 `.stat-value`、`.context-parts td:last-child` 已有，需补齐）：

```
.stat-value, .metric strong, .token-grid strong, .token-value,
.version-number, .nav-count, .table-footer, .filter-summary,
.requests-table td（费用/token 列）, .context-parts td:last-child,
.performance-row b, .pagination, .coverage-list strong, .conversation-count strong
```

写法：`font-variant-numeric: tabular-nums lining-nums; font-feature-settings: var(--fs-num);`
**指标数字额外用 `--font-display`**（衬线数字是 editorial 的关键信号），其余表格数字用 `--font-mono` 或 sans + tnum。

---

## 4. 组件视觉规则

> 以下全部**只改样式不改结构**。括号里是现状，便于 Phase 3 对照。

### 4.1 按钮

| 变体 | 规范 |
|---|---|
| 基础 | `min-height:32px; padding:6px 12px; border:1px solid var(--rule-control); border-radius:var(--r-sm); background:var(--surface); color:var(--ink-2); font-size:12px; font-weight:500;`（现 38px / radius 8 / 550） |
| hover | `border-color:var(--ink-3); background:var(--paper-sunk);` **无位移、无缩放、无阴影** |
| `.primary` | `background:var(--accent); border-color:var(--accent); color:#FCFAF6;` hover `var(--accent-strong)`。**删除现有 `box-shadow:0 3px 7px #6254e81a`。仅用于「确认写入 / 应用 / 保存」——一个工具栏里最多一个 primary。** |
| `.ghost` | `border-color:transparent; background:transparent; color:var(--ink-3);` hover → `color:var(--ink)` 且出现 1px 下划线（印刷式） |
| `.danger` | `color:var(--danger); border-color:var(--danger);` **不做填充红** |
| `.danger-solid` | `background:var(--danger); color:#FCFAF6;` **只允许出现在 confirm-dialog** |
| `:disabled` | `border-color:var(--rule); color:var(--ink-4); background:var(--surface-sunk);` **禁止 `opacity:.5`**（会使对比度不可控）；`opacity:1` |
| `.icon-button` | `28×28; border-color:transparent; background:transparent; color:var(--ink-4);` hover → `background:var(--accent-wash); color:var(--accent);` |
| 过渡 | 仅 `background-color`/`border-color`/`color` `var(--d-control) var(--ease-out)`；**禁止 `transform`、`box-shadow` 过渡** |

### 4.2 输入框 / 下拉 / 复选框

- 输入框：`min-height:32px; padding:6px 10px; border:1px solid var(--rule-control); border-radius:var(--r-sm); background:var(--surface); font-size:12px;`
- **聚焦**：`border-color:var(--accent); box-shadow: inset 0 0 0 1px var(--accent);` —— 得到 2px 硬边，**不是光晕**。删除现有 `box-shadow:0 0 0 3px #6254e815`。
- 全局 `:focus-visible`：`outline:2px solid var(--accent); outline-offset:2px;`（替换 `#b8adff`）。
- 只读：`background:var(--surface-sunk); color:var(--ink-3); border-color:var(--rule);`
- 错误：`border-color:var(--danger)`，且**上方 `.field > span` 标签同步变 `--danger`**。
- placeholder：`color:var(--ink-4)`，**禁止斜体**。
- `select`：`appearance:none`，右侧用 1px 硬角 chevron（inline SVG，`stroke:var(--ink-3); stroke-width:1.2; fill:none`）。
- 复选框：**替换 `accent-color` 方案**为可跨机一致的纸面样式：

```css
input[type=checkbox]{
  appearance:none; width:14px; height:14px; margin:0; flex-shrink:0;
  border:1px solid var(--rule-control); border-radius:1px;
  background:var(--surface); cursor:pointer;
}
input[type=checkbox]:checked{
  background-color:var(--accent); border-color:var(--accent);
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'><path d='M2.5 6.4l2.3 2.3 4.7-5' fill='none' stroke='%23FCFAF6' stroke-width='1.6' stroke-linecap='square'/></svg>");
  background-size:12px 12px; background-position:center; background-repeat:no-repeat;
}
input[type=checkbox]:disabled{ background:var(--surface-sunk); border-color:var(--rule); }
```
- `.check-label`：`font-size:12px; color:var(--ink-2); gap:8px;`

### 4.3 表格与行分隔

| 元素 | 规范 |
|---|---|
| `th` | `font-size:10px; font-weight:600; color:var(--ink-3); background:transparent;` **删除现有色带 `#fafbfe`**；`border-bottom:1px solid var(--rule-strong)`；`padding:8px var(--row-px)`；sticky 时 `background:var(--surface)` |
| 中文表头 | **`text-transform:none; letter-spacing:0;`**（见 §7 反模式 11） |
| `td` | `padding:var(--row-py) var(--row-px); border-bottom:1px solid var(--rule-faint); font-size:12px; line-height:var(--lh-snug);`（现 17–22px / `#f0f1f6`） |
| 数字列 | `text-align:right; font-variant-numeric:tabular-nums;`（复用已有 `.align-right`） |
| 行 hover | `background:var(--surface-sunk);` —— **下沉色，不是亮色洗** |
| 行 `selected` | `background:var(--accent-wash);` + `td:first-child{ box-shadow: inset 2px 0 0 var(--accent); }`（inset 发丝线不算阴影，允许；且不产生布局位移） |
| 斑马纹 | **禁用**。用发丝线而非色带分层 |
| `table-layout` | 保持 `fixed`（daily.css 已用） |

### 4.4 卡片

- `.card`：`background:var(--surface); border:1px solid var(--rule-strong); border-radius:var(--r-md); box-shadow:var(--shadow-none); overflow:hidden;`（删 `0 3px 8px #20243302`）
- `.card-heading`：`padding:16px 20px; border-bottom:1px solid var(--rule);` h2 用 **`--font-sans` 600 / 16px**（h1 才用衬线）。
- `.stat` / `.metric`：删 `box-shadow`；`.stat-label` 10px `--ink-3`；`.stat-value` `--font-display` 28px 700 + tnum；**`.stat-icon` 三种紫/绿/蓝配色统一为单个中性墨**（`border:1px solid var(--rule); color:var(--ink-3); background:transparent`）。
- `.provider-add-card`：`border:1px dashed var(--rule-control); background:transparent;` hover → `border-color:var(--accent); background:var(--accent-wash);`

### 4.5 徽标 / 计数 / 状态

- `.tag` / `.badge`：**去填充**。`border:1px solid var(--rule); border-radius:var(--r-xs); padding:1px 5px; font-size:10px; color:var(--ink-3); background:transparent;`
  - 语义变体**只改边框+文字色**：`.tag.green→var(--success)`、`.tag.red→var(--danger)`、`.tag.amber→var(--warn)`、`.tag.purple/.blue→var(--cat-*)`。
  - **未知/未采集变体**：`color:var(--unknown); border-style:dotted;` —— 虚线 = 缺失，不给填充。
- `.nav-count`：同上透明描边 + `tabular-nums`。
- 状态点（`.online-dot` / `.offline-dot` / `.nav-dot` / `.provider-dot`）：`width:6px;height:6px;border-radius:50%;` **禁止 box-shadow 光晕、禁止 ping/涟漪动画**。
  - 本机运行在线 → `--success`
  - 离线/未确认 → `background:transparent; border:1px solid var(--unknown);`（**空心 = 未确认**）
  - 有未保存改动（`#nav-sync-dot`）→ `--accent` 实心
  - 采集进行中 → `--accent-bright` + §6 的 2.4s 脉冲
- `.avatar` / `.brand-mark` / `.agent-symbol` / `.target-symbol`：紫色方块 → `border:1px solid var(--rule-strong); background:var(--surface); color:var(--ink-2); border-radius:1px;`。**删 box-shadow**。`--provider-color` 默认值由 `#a595db` 改为 `var(--cat-1)`。

### 4.6 Dialog

- `.dialog`：`border:1px solid var(--rule-strong); border-radius:var(--r-lg); box-shadow:var(--shadow-press); background:var(--surface);`（删 `0 24px 90px #20243326`）
- `::backdrop`：`background:var(--scrim); backdrop-filter:none;`（纸是不透明的；毛玻璃是网页感残留）
- `.dialog-header`：`padding:20px 24px 14px; border-bottom:1px solid var(--rule);`
- `.dialog-footer`：`padding:14px 24px; border-top:1px solid var(--rule); background:var(--surface-sunk); justify-content:flex-end;`
- `.eyebrow`：**取消大写字距方案**（内容是中文），改为带引导短线的边注：
  ```css
  .eyebrow{ font-size:9px; color:var(--ink-4); letter-spacing:.05em; }
  .eyebrow::before{ content:""; display:inline-block; width:12px; height:1px;
    background:var(--rule-strong); vertical-align:middle; margin-right:6px; }
  ```
- `.form-error`：`background:var(--danger-wash); color:var(--danger); border-left:2px solid var(--danger); border-radius:var(--r-sm);`
- `.confirm-symbol`：`background:var(--danger-wash); color:var(--danger); border-radius:var(--r-sm);`（删紫/粉系 `#db8991`）
- `.confirm-ack`：`background:var(--warn-wash); border-color:var(--rule)`（删 `#fff8f1`）

### 4.7 Toast

- `.toast`：`background:var(--surface); border:1px solid var(--rule-strong); border-radius:var(--r-sm); box-shadow:var(--shadow-press); padding:12px 14px; font-size:12px; color:var(--ink-2);`
- **左侧 2px 竖线**表达语义：`border-left:2px solid var(--success|danger|warn)`；图标同色。
- 位置保持右下，`gap:8px`。**禁止圆角胶囊、禁止彩色填充**。

### 4.8 侧栏导航

- `.sidebar`：`width:var(--sidebar-w); background:var(--paper-sunk); border-right:1px solid var(--rule-strong);` 无阴影、无卡片。
- `.nav-item`：`padding:8px 10px; color:var(--ink-3); font-size:13px; font-weight:400; border-radius:var(--r-xs);`
  - hover → `color:var(--ink)`，图标 `color:var(--accent)`（**不用填充底**）
  - `.active` → `color:var(--ink); font-weight:600; box-shadow: inset 2px 0 0 var(--accent);` **删除 `--accent-soft` 填充胶囊**，改为账页边注式的 2px 朱砂左线。
- `.nav-caption`：`font-size:10px; color:var(--ink-4); letter-spacing:.05em; margin:28px 10px 8px;`（删 1.8px 大写字距）
- `.sidebar-note`：**删除渐变紫底卡片**，改为夹在两条发丝线之间的印刷边注：
  `border:0; border-top:1px solid var(--rule); border-bottom:1px solid var(--rule); background:transparent; padding:14px 10px;`
- `.sidebar-footer`：`border-top:1px solid var(--rule); font-size:10px; color:var(--ink-4);`
- `.brand`：字标用 `--font-display` 700 / 17px；`.brand small`（"LOCAL AGENT WORKSPACE"，**纯拉丁**）→ `font-size:9px; letter-spacing:.12em; color:var(--ink-4);` —— 这是全界面**唯一**允许大写字距的地方。
- `.brand-mark`：`background:var(--ink); color:var(--paper); border-radius:1px; width:30px;height:30px;` 无阴影 —— 读起来像一枚压印的印记。

### 4.9 进度条与图表

- `progress`：`appearance:none; height:2px; border:0; background:var(--rule-faint);` `::-webkit-progress-value{background:var(--accent);}` —— 一条 2px 墨线在涨。
- `.trend-column i`（图表柱）：**默认 `--rule-strong`（数据即墨）**，只有"当前选中/异常"那一根用 `--accent`。图表绝不整片强调色。

---

## 5. 密度与分隔规范

核心原则：**记录内紧、记录间松**。账页里「一行 = 一条记录」要排紧，记录之间用发丝线分隔，而**记录组**之间给足空气。这是真实编辑实践，也是把 SaaS 密度调成 editorial 密度而不动 DOM 的唯一杠杆。

| 选择器 | 现状 | 新值 | 理由 |
|---|---|---|---|
| `:root font-size` | 14px | **13px** | 同屏多约 8% 信息量 |
| `main` padding | 36px 38px | `var(--page-pad-y) var(--page-pad-x)` = 28/32 | 无浏览器边框，可收边距 |
| `main max-width` | 1560px（daily.css 1800px） | **`max-width:none`** | 编辑页面铺满到页边；1440 窗口下原值无意义 |
| `.topbar` height | 68px，底 `rgba(255,255,255,.7)` | **56px**，`background:var(--paper)` 不透明 | 半透明吸顶是网页感残留 |
| `.models-table td` | `17px 18px` | **`10px 14px`** | 行内收紧 |
| `.agent-table td` | `22px 18px`（1600px 时 26px） | **`16px 14px`**，删 1600px 放大规则 | 双行单元仍需呼吸，但不翻倍 |
| `.requests-table td` | `15px 16px` | **`11px 14px`** | 观测表最密集 |
| `.card` margin-bottom | 22px | **`var(--block-gap)` = 24px** | 组间加空气 |
| `.stats` gap / margin-bottom | 18 / 28 | **`16 / var(--section-gap)` = 40px** | 区块级留白加倍 |
| `.card-heading` padding | `22px 24px 18px` | **`16px 20px`** | 标题区收紧，靠发丝线与内容分界 |
| `.filter-bar` / `.table-footer` padding | `16px 24px` | **`12px 20px`** | 工具条是页边注，不该厚度 |
| `body line-height` | 1.55 | **1.55 正文 / 1.40 表格 / 1.75 说明段** | 分级，不再一刀切 |
| `--radius` | 14px | **≤5px** | 印刷无大圆角 |

**发丝线用法（唯一的分隔手段）**：
1. 卡片外框 `--rule-strong`
2. 卡片内分区（heading/工具条/页脚）`--rule`
3. 表格表头下沿 `--rule-strong`（比行线强，形成层级）
4. 表格行线 `--rule-faint`（最弱）
5. 可交互控件边框 `--rule-control`（最强）
6. 侧栏与本体的分界 `--rule-strong`

**禁止**用色块/底色做分隔（现有 `.filter-bar` 的 `#f1f2f7` 上边线、`.bulk-bar` 的紫底 `#f6f3ff`、thead 的 `#fafbfe` 全部改为发丝线 + 透明底；`.bulk-bar` 改为 `background:var(--accent-wash); border-top:1px solid var(--accent);`——它是"此刻正在发生的事"，属于强调色的合法使用场景）。

---

## 6. 动效语义

这台机器的主体时间行为是：**本机轮询采集（每 30s 扫描变化文件）→ 写入前预览 → 事务回滚**。动效必须复制这三个行为本身的时间形状。

| 时刻 | 动效 | 时长 / 缓动 | 祖先 |
|---|---|---|---|
| **开场**（冷启动） | 不做内容 fade-up。① 顶栏下沿发丝线由左向右画出 300ms；② 首屏表格整体（**单个图层，不是逐行**）opacity 0→1 用 200ms，从 150ms 起；③ 侧栏与本体的分界线由上向下 420ms。全程 500ms 后静止。 | `--d-open` / `--ease-out` | 落笔先打格线，再写字 |
| **加载态**（`.load-state`、内联 `.spinner`） | `.spinner` 由旋转圆环改为**扫描扫掠**：一条 1px `--accent` 短线在一条 `--rule-faint` 轨道内左右平移。`@keyframes scan-sweep{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}`，`--d-scan` linear infinite。**class 名 `.spinner` 保留，只改样式。** | 1100ms linear | 扫描头 / 绘图仪笔架往复 —— 就是它在做的事 |
| **采集进行中**（侧栏/采集状态点） | 只有 opacity 呼吸，`.45→1`，**无缩放、无位移**。 | `--d-pulse` 2400ms `--ease-inout` infinite | 每 30s 一次的慢速定周期扫描 |
| **生成预览 → 落位** | 预览面板整体 180ms opacity + 4px 上移，**单个元素，无 stagger**。随后"将被改动"的行**由左向右画出一条 2px `--accent` 下划线**，220ms。 | `--d-settle` + `--d-mark` / `--ease-out` | 校对红笔划线 |
| **确认写入** | 无额外动效。写入结果直接呈现。 | — | 事务是离散事件，不配动画 |
| **回滚** | 批注下划线**反向收回**，且**比应用更慢**。 | `--d-rollback` 320ms `--ease-inout` | 撤销必须比执行更慢，才读作"慎重" |
| **轮询刷新指标** | **禁止数字滚动/计数动画**。数字直接替换，同时在其下方闪现一条 2px `--accent` 下划线，240ms 后淡出。 | `--d-mark` | "这个值刚被重新观测过" |
| **Toast** | 底部上滑入场 160ms（无回弹），停留 4.5s，退场 200ms fade。 | `--ease-out` | — |
| **控件状态** | 只有颜色变化，120ms。无 transform。 | `--d-control` | — |

**明确禁止**：
- staggered fade-up / 逐项入场（与 Inter 同级老套）
- 卡片/行/按钮的 hover 位移、`translateY`、缩放
- 任何带过冲的 `cubic-bezier`（弹簧、回弹、bounce）
- 数字 count-up / 计数补间
- 骨架屏 shimmer
- 成功对勾描边、礼花、confetti
- 视差、渐变文字、glow/pulse 光晕、ping 涟漪
- 页面切换滑动
- 加载转圈（除上表的扫描扫掠外）
- 满屏散落微交互 —— 全站动效总数是**个位数**

`@media (prefers-reduced-motion: reduce)` 现有规则保留，并额外把 `--d-*` 归零、把 scan/pulse 改为静态（`.spinner` 退化为一条静态 1px 线、采集点退化为实心点）。

---

## 7. 反模式清单（本项目禁止出现）

1. **Claude 橙 `#D97757`** 或任何橙色作 UI 色。
2. **任何紫/靛**：`--accent:#6254e8`、`#f0edff`/`#f5f3ff` 洗底、`#b8adff` 焦点环、`#c89bdc` required 标记 —— 全部清除。
3. **渐变作填充**：按钮/卡片/背景块不得用渐变。渐变只允许作为 §2.10 两个 ≤4.5% alpha 的氛围图层。
4. **带模糊的 box-shadow**（除 dialog/toast 的零模糊硬压印）。
5. **填充色胶囊徽标 / tag**（pastel pill）。
6. **强调色铺满**：任何视口内 accent 像素占比 >3%。具体判定：不得作为大于一行的背景块；一个工具栏内 primary 按钮 ≤1 个。
7. **圆角 >5px**（现 14px/18px 全超标）。
8. **指标数字计数动画**。
9. **staggered fade-up**。
10. **hover 位移 / 缩放 / 抬升阴影**。
11. **对中文施加 `letter-spacing` 或 `text-transform`**（中文表头、`.eyebrow`、`.nav-caption` 一律 `letter-spacing:.05em` 上限且 `text-transform:none`）。
12. **`font-weight` 出现 400/500/600/700 之外的值**（现 550/650/750 全部归一化；CJK 回落字体只有 Regular/Bold 两档，非整数权重会被合成或错档）。
13. **`opacity` 调暗禁用态**（改用显式 `--ink-4` + `--surface-sunk`）。
14. **斑马纹 + 色带表头**。
15. **Emoji 当图标 / 文案里带 emoji**。
16. **任何 `http://127.0.0.1:8787`、端口号、URL 文案**；任何"在浏览器中打开""刷新页面"类网页感元素；任何地址栏/tab 条形态的 chrome。
17. **毛玻璃 / `backdrop-filter: blur`**。
18. **骨架屏 shimmer**。
19. **多色统计图标块**（现 `.stat-icon` 紫/绿/蓝三色）→ 统一中性墨。
20. **亮薄荷绿表示成功**（现 `#4aaf8b` 在线点）→ 松墨绿 `--success`。
21. **健康分 / 分数条 / 星级** —— 与产品"不生成健康分"的承诺矛盾。
22. **高饱和语义色**：所有语义色必须是明度 L35–45% 的"墨"。
23. **外链字体**(`fonts.googleapis.com`)：桌面端离线，必须本地 woff2。
24. **`.sidebar-note` 的渐变紫底卡片**、`.notice` 的紫底 → 纸面 + 发丝线。
25. **未知/未采集显示为 0 或"免费"** —— 视觉上必须是石墨 em dash + 虚线，不是绿色 0。

---

## 8. Phase 3 硬约束

### 8.1 结构类（改动即破坏回归测试）

1. **不动 DOM**：`ui/index.html` 63 行骨架一个字不改（除 `<link rel="icon">` 路径若需打包调整）。
2. **不动 class / id / `data-*`**：测试直接依赖 `nav a[href="#agents"]`、`#editor-dialog`、`#editor-footer [data-action="close-editor"]`、`#confirm-ok`、`[data-workbuddy="login|cancel|connect|logout"]`、`.workbuddy-login-link`、`[data-daily="switch"][data-agent=...]`、`#quick-provider`、`#quick-model`、`.metric`、`#workbuddy-settings`。
3. **不动 `hidden` 语义**：`[hidden]{display:none!important}` 必须保留；`.nav-dot[hidden]`、`.sync-preview[hidden]` 等依赖它。
4. 只允许新增**伪元素**（`::before/::after/::-webkit-progress-*`），不新增真实节点。

### 8.2 样式文件治理（重要发现）

**`ui/daily.css` 第 2 行重新定义了 `:root`**（`--accent:#42665c` 苔绿、`--bg:#f7f8fa`），且 daily.css 在 `index.html` 中后于 styles.css 加载 → **当前应用实际运行的是两套互相打架的主题**（紫色系硬编码值 + 绿色 token 覆盖）。Phase 3 必须：

- **删除 `ui/daily.css` 的 `:root` 块**，token 单一来源 = `ui/styles.css:1`。
- daily.css 只保留 `.obs-*`/`.agent-*`/布局类规则，不得再声明颜色 token。
- 两份文件里约 **120 个硬编码 hex** 需换成 token。高频映射：

| 现有硬编码 | → |
|---|---|
| `#f8f9fc` `#fafbfe` `#fbfcfe` `#fdfcff` `#fcfcfe` `#fcfcff` `#f7f8fc` `#f6f7fa` `#f6f7fb` `#f6f7fc` | `--surface` / `--surface-sunk` / `--paper-sunk` |
| `#f0f1f6` `#f1f2f7` `#e9ebf2` `#dfe2ec` `#e8ebf2` `#e4e7e8` | `--rule-faint` / `--rule` / `--rule-control` |
| `#a0a6b6`(×9) `#a2a9ba` `#a0a5b5` `#a1a7b8` `#a4aabb` `#99a1b3` `#96a0b3` `#a0a5b4` `#b2b7c6` | `--ink-4` |
| `#7f8ba1` `#8991a6` `#7b8398` `#788199` `#929bae` `#959cae` `#7c8295` | `--ink-3` |
| `#747c91` `#656c82` `#6b7388` `#6f778c` `#787f94` `#7a859b` `#7b8092` `#666d80` | `--ink-2` |
| `#202433` | `--ink` |
| `#20243302` `#20243326` `#22253914` `#22253959` `rgba(28,38,71,.04)` | 删除 / → `--scrim` |
| 紫系：`#6254e8` `#5143d3` `#f0edff` `#f5f3ff` `#f6f3ff` `#fbfaff` `#faf8ff` `#f5f0fc` `#e5dfff` `#e2dcff` `#e9e2ff` `#e8e2fd` `#eee9ff` `#c9bfef` `#dcd5fa` `#c8c2e8` `#cfd6ea` `#b8adff` `#8a7df0` `#a595db` `#8b7cdb` `#8e80e9` `#8065b5` `#7767a9` `#8a7abc` `#9b88cf` `#b0a3e2` `#9a8ba6` `#c89bdc` `#8566ab` | 全部删除 → `--accent` / `--rule-*` / `--ink-*` / `--cat-*` |
| `#d34c58` `#b83d49` `#fff2f3` `#f0ced3` `#db8991` `#b4595c` `#b64545` `#fff0ef` | `--danger` / `--danger-wash` |
| `#b77920` `#fff8ea` `#f3e5c9` `#a38c64` `#a07c43` `#8a5e23` `#fff8f1` `#7a4a12` `#fbf1dc` `#9c741d` | `--warn` / `--warn-wash` |
| `#18876b` `#edf9f4` `#5baf93` `#5aaf91` `#4aaf8b` | `--success` / `--success-wash` |
| `#b56a4e` `#f9eee8` `#f0dfd6` | `--cat-4`（Claude） |
| `#7d9c8d` `#8cae9d` `#aec3b5` `#42665c` `#304e46` `#eaf1ed`（daily.css 苔绿） | `--cat-5` / `--cat-2` |
| `#a5aca8` `.offline-dot` | `--unknown`（且改空心） |
| `#d8a349` `.nav-dot` | `--accent`（未保存改动） |

### 8.3 视觉硬约束（不可协商）

1. 全站 `font-weight` 枚举值只有 **400 / 500 / 600 / 700**。
2. 全站 `border-radius` ≤ **5px**。
3. 全站带模糊的 `box-shadow` 数量 = **0**；允许且仅允许 dialog/toast 的 `3px 3px 0`。
4. 强调色 `--accent` 不得作为**大于一行的填充背景**；`.primary` 按钮在每个可见工具栏内 ≤1 个。
5. 中文文本上 `letter-spacing` ≤ `.05em` 且 `text-transform:none`。
6. 表格表头**不得有背景色带**；表格**不得有斑马纹**。
7. 数字类文本必须 `font-variant-numeric: tabular-nums`。
8. 字体必须本地 woff2，不得外链 CDN。
9. 动效总数个位数；`transform` 只允许用在 §6 列出的三处（预览落位、扫描扫掠、回滚收回）。
10. UI 文案/界面上不得出现 `127.0.0.1`、端口、"浏览器""刷新页面""打开新窗口"等网页宿主残留（现有 `open-ui.cmd` / `Launch GUI.vbs` 是启动器，不在 UI 内，无需改）。

---

## 10. 视觉重点修订（2026-10-20 用户反馈）

> 用户原话：「功能正常了，但 UI 很丑，而且没有视觉重点。」
> 可实施改动：`docs/design/visual-emphasis.patch.md`（CSS 片段 + JS 行级改动）。

### 10.1 诊断：方向没错，执行偏了

| # | 量化事实（已核实） | 后果 |
|---|---|---|
| 1 | `var(--accent)` 在两份 CSS 共 **27** 处；`var(--ink-3)`+`var(--ink-4)` 共 **120** 处（≈1:4.4） | 一屏密集界面里朱砂只出现在 2–3 个小点，其余全是灰褐 |
| 2 | `ui/workbench.js:135` 渲染 `.trend-column` 时**从不加 `active` class**，而 `.trend-column.active i{background:var(--accent)}` 是死规则 | 观测页核心图表「调用趋势」整片 `--rule-strong` 灰，数据零焦点 |
| 3 | `.tag.green/amber/red/blue/purple` 全部 `background:transparent`（styles.css:320-324） | 密集表格里语义状态（已启用/失败/推理/图片）几乎不可辨 |
| 4 | `.stat-icon` 统一中性墨（styles.css:281） | 三个指标卡并排视觉权重完全相同，无主次 |
| 5 | `data-value="failed"/"unknown"` 早已渲染在 `.metric` 上（workbench.js:129），但两份 CSS **没有任何 `.metric[data-value]` 规则** | 「失败 / 结果未确认」两个异常计数器与普通卡完全同形，README「未知就显式显示未知」的承诺在视觉上落空 |

结论：问题**不是色不够，是色的位置错了**。朱砂总量没少，但全落在 hover / 禁用 / 小图标上，
没有一根落在「数据的焦点」上。纸质感血缘（用户明确喜欢 Anthropic 画风）不动，修的是分配。

### 10.2 五条新判定线

1. **一图一朱砂**：每张图表恰好一根数据驱动的朱砂标记。趋势图 = 当前限定日，否则数据里最新的一天
   （UTC 今天 = 仍在累积的「此刻」）；分布图 = 份额首行；上下文图 = 输入峰值柱（与标题数字互证）。
   朱砂在图表里回答「此刻 / 最大 / 在看」，不是装饰。**拒绝**「调用量最高」方案：那是统计量不是此刻，
   且在「最近一年」视图里高亮古代峰值会误导。
2. **状态可淡填充，身份永远描边**：颜色回答「现在怎么了」（green/red/amber）→ 可淡填充，
   且只能用已有 `--*-wash` token（L≈92% 纸邻色）；回答「这是谁」（purple/blue/`--cat-*`）→ 永远 1px 描边。
   `--accent` 永不入徽标；`unknown` 永远虚线不填充。这不违反 §7 反模式 5/6：填充胶囊是「高饱和浅底 + 全圆角 + 到处用」，
   我们是 2px 圆角 + 1px 同色描边 + 10px 字 + 纸邻 wash，读作账台上的印戳；且填充仅限状态，一排身份标签永不铺色。
3. **主卡 = 主题卡**：并排指标卡里，本页主题词那一张用「选中行语言」（`--accent-wash` + 2px 朱砂左线 + 图标朱砂）。
   **不靠数值大小**——不同量纲不可比，且 30s 轮询会让焦点跳走，违反「轮询刷新指标：数字直接替换」的安静原则。
   指标卡网格由此形成三层语言：**inset 2px 线（box-shadow）= 选中/主卡**（瞬态，不改变盒模型）；
   **border-left 2px = 语义状态**（稳态标注，接续 §4.6 form-error / §4.7 toast 的边线传统）；
   **线型编码确定性：实线 = 已确认的事实，点线 = 未确认的缺失**（接续 `.tag.unknown` / `--unknown-rule`）。
   「失败」= 牛血红 2px 实线不填色——四卡里一张红底会与主卡 wash 竞争层级，danger 比 accent 更深，铺底即喊叫；
   「结果未确认」= 石墨 2px 点线——未知是缺失不是状态。边线是静态类别标记（失败=0 时仍在），
   严重度由数字承载；大数字保持墨色（数据即墨），只有类别标签上语义色。
   这不算是给界面添乱的理由：无填充（不与三张纸面卡竞争）、2px（系统既有边线语言）、
   静态（不随轮询闪烁）、且 `data-value` 早在 DOM 里——CSS 只是补上了一直缺席的实现。
4. **墨阶按信息类型分配**：数据（数值/计数/ID/路径/状态/轴标签）→ `--ink-2`；标签/表头/说明 → `--ink-3`；
   脚注/单位/徽标数字 → `--ink-4`，且 `--ink-4` 只允许出现在「删掉也不丢信息」的地方。
   本次按此规则升位 23 条选择器（清单见 patch §D），升位后 ink 占比从 1:4.4 降到约 1:2.3，
   且高频数据（表格单元格每屏出现 20 次）比低频规则（每屏 1 次）先升。
5. **primary 制度不变**：朱砂实底按钮只给写入动作，一个可见工具栏 ≤1 个（已审计通过：models/providers/sync
   页头各 1，dialog 页脚各 1，agent 表按行计）。观测页是只读页，「读取新记录」**不升** primary——
   只读页的视觉重点由图表焦点与主卡承担，primary 的语义（写入）不许稀释。

### 10.3 砍掉了什么（替换测试）

| 砍掉 | 替换为 | 替换测试 |
|---|---|---|
| §4.9「图表绝不整片强调色」的一刀切执行（= 图表永远无色） | 一图一朱砂 | 「最新一天=朱砂」套到任意仪表盘也成立，但我们的依据是产品自己的行为定义（30s 轮询 + UTC 日分组 + 点击钻取），不是仪表盘惯例 → 通过 |
| §4.5「徽标全部去填充」的一刀切 | 状态/身份二分 | 「身份标签也淡填充」套到任意 SaaS 表格都成立 → 那正是 slop，已拒绝；状态填充的合法性来自密集台账里状态必须秒辨的产品事实 |
| §4.4「stat-icon 统一中性墨」的一刀切 | 仅主卡图标可朱砂 | 用户的问题是「三卡无主次」不是「图标有色」；多色图标块（反模式 19）依然禁止，全站仍只有一个朱砂图标 |

**没有砍**（防止 Phase 3 过度修正）：纸质感三图层、四级发丝线、衬线双轨、字体三件套、
零模糊阴影、圆角 ≤5px、动效个位数、`--unknown` 石墨虚线、单一朱砂色相。

---

## 附录：深色皮肤（**不在 v1 范围**，仅记录原则）

纸面是本方案的概念核心，v1 只做浅色。若日后要求深色，必须是**"墨纸"而非蓝黑**：`--paper:#1B1917`、`--surface:#232019`、`--ink:#EDE7DC`，发丝线反转为主体的"刻痕"（比 `--surface` 更暗的 1px），强调色升明度到 `#C2502F`。**禁止**把浅色 token 用 `filter:invert()` 或简单明度反转生成——那会得到蓝黑 + 荧光橙，直接命中反模式 1 与 22。

---

## 11. Phase 4：背景修订（2026-10-20）

### 11.1 旧三层为什么被判为失败

用户的原话是"背景奇怪"。定位到 `ui/styles.css` 三条 token 与三处宿主
（`body` / `.sidebar` / `main::before`）：

| token | 实现 | 实际后果 |
|---|---|---|
| `--field-light` | 两条 radial 色斑 | 尚可，但叠在纹理上加剧了"脏" |
| `--field-rule` | `repeating-linear-gradient`，28px 周期 1px 线 | **满屏横格**，像作业本；1440×960 下能看到 34 条贯穿整页的线 |
| `--field-fiber` | `feTurbulence` 生成 SVG 噪点，`multiply` 混合 | **满屏颗粒**，读作扫描件伪影/低质量截图 |

失败不是"参数没调好"，是**手段选错**：

1. **不可控**。`repeating-linear-gradient` 与 SVG 噪点贴图都会顺铺。它们和窗口尺寸、
   页面高度、缩放比无关地重复，所以"看哪儿都一样"。任何"纸感"都要求纹理与阅读位置有关
   （页边更密、正文区更疏），而 tiling 纹理天然没有这个性质。
2. **与内容竞争**。台账页的主体是大面积表格与发丝线；背景再来一层等强度的重复线，
   两套"线"互相干扰，四级发丝线的层级被拉平。
3. **噪点在 LCD 上不可控**。`feTurbulence` 的颗粒在高 DPI 屏与 100% 缩放下呈现完全不同的
   强度，相当于把不可预测性写进了视觉。

### 11.2 新三层：不重复的光场

原则：**层次靠明度与发丝线，不靠纹理**。三条新 token，全部是单束、不重复的渐变，
尺寸显式声明在 `body` 的 `background-size` 里，光的落点与页面长短无关：

```css
--field-glow:   radial-gradient(1240px 620px at 50% -12%, rgba(255,253,248,.42), rgba(255,253,248,0) 66%);
--field-settle: linear-gradient(180deg, rgba(30,28,25,0), rgba(30,28,25,.022));
--field-warm:   radial-gradient(760px 460px at 100% -6%, rgba(169,63,36,.032), rgba(169,63,36,0) 64%);

body {
  background-color: var(--paper);
  background-image: var(--field-glow), var(--field-settle), var(--field-warm);
  background-repeat: no-repeat;                    /* 承重：repeat 会把光场退化成贴图 */
  background-size: 100% 620px, 100% 420px, 760px 460px;
  background-position: 50% 0, 50% 100%, 100% 0;
  background-blend-mode: normal, normal, multiply;
}
```

| 层 | 物理祖先 | 实测强度 |
|---|---|---|
| `--field-glow` | 台灯从上方打在纸面 | 首屏上 335px 内提亮 2–5 个色阶 |
| `--field-settle` | 纸页靠近自身重量塌下去的那点暗 | 底部 420px 带压暗 ≤4 个色阶（multiply） |
| `--field-warm` | 朱砂印泥在纸角洇开的一点余温 | 右上角 ≤3% alpha，只能"感觉到"，看不到形状 |

`main::before` 整段删除（它原来只承载横格），`main` 上的 `isolation: isolate` 一并删除
（只为那层 `z-index:-1` 服务）。`.sidebar` 退化为**纯色 `--paper-sunk` + 1px `--rule-strong`
右边线**——全站最安静的一块面。

层次改由三级明度阶梯承担（实测值）：

```
侧栏 rgb(240,234,224)  <  纸面 rgb(247,243,235)  <  卡片 rgb(252,250,246)
```

### 11.3 §2.10 条款处置

- **作废**：`--field-light` / `--field-rule` / `--field-fiber` 三个 token（已从 `:root` 删除，
  不留空壳）；`.sidebar` 的 `background-image` 与 `background-blend-mode` 叠加；
  "横格只出现在页边与留白处"这条实现口径。
- **保留**：背景要有层次的判断；"卡片/表格不许用阴影，只许明度差 + 发丝线"；纸面统治。
- **§10.3「没有砍：纸质感三图层」更正**：那句话当时是在防止 Phase 3 过度修正，
  但"三图层"这个具体手段本身是错的，已按本节替换。纸质感仍然保留，载体换成了光场与明度阶梯。

### 11.4 回归约束（已写进 `scripts/verify-css.py`）

把"不许再出现全屏重复纹理"从口头约定变成可回归断言：

1. `styles.css` / `daily.css` / `fonts.css` 的**声明**里（注释豁免）不得出现
   `repeating-linear-gradient`、`feTurbulence`、`fractalNoise`。
2. `body` 必须同时满足：`background-image` 引用三条新 token、不含 `url()`、
   声明了 `background-size`、`background-repeat` 不含平铺值。
3. `REQUIRED_TOKENS` 用三条新 token 替换掉三条旧 token。

已用变异测试验证断言是活的（分别把横格写回、把噪点写回、删掉 `background-size`、
把 `no-repeat` 改成 `repeat`、让 `body` 少一束光，五种变异全部如期报红）。

### 11.5 同一轮的图标：深色任务栏的取舍（刻意不做）

新标记是墨色印版 + 骨白负空间开槽（详见 `scripts/make_icon.py` 头部）。
它在浅色底上对比度 15.3:1，在深色任务栏（`#202020`）上印版会并入背景
（1.0–1.04:1），只剩三个浅槽可辨。**决定不加外描边**：本机
`AppsUseLightTheme=1` / `SystemUsesLightTheme=1`，应用与任务栏都是浅色，
加描边等于为一个不存在的场景在浅底上引入一道 1px 灰边。
若将来支持深色皮肤，再补一枚描边变体。

---

## 13. Phase 6：溢出与触达目标修订（2026-10-25）

> 起点：UI 设计指令的 Phase 4 视觉质量循环。性质与 §10/§11/§12 相同：
> **判定线的补丁与澄清**，不改变「本机台账」方向本身。
> 方法：fixture + Playwright 几何探针（`scripts/visual-probe.cjs`）逐视口量化
> 「谁被裁剪 / 谁低于 24px / 哪张表溢出容器」，再只改超标项。

### 13.1 判定线补充：探针必须区分「滚动容器」与「真裁剪」

`.agent-table-wrap` / `.obs-table-scroll` / `.table-wrap` 的 `overflow-x:auto`
是 §5 的设计意图（密集表格在窄屏横向滚动，`min-width:890px` 是刻意声明）。
因此 `scrollWidth > clientWidth` 本身**不是缺陷**——第一版探针把它全部报红，
是判据写错，不是页面写错。修正后按 `overflow-x` 分三类：

| `overflow-x` | 含义 | 判据 |
|---|---|---|
| `auto` / `scroll` | 设计意图的滚动容器 | 不计缺陷；只记录表格宽 vs 容器宽 |
| `visible` | 内容真的被切掉 | 缺陷（P1） |
| `ellipsis` | 刻意的单行截断 | 不计缺陷 |

### 13.2 已修：模型库表格溢出右栏（P1，实测）

**事实**：`.models-layout` 是 `1fr 340px` 双栏，所以左栏在 1440 下只有 **786px**
（不是 1150px）。而 `.models-compact` 是 `white-space:nowrap` 的 auto 表格，
浏览器把「模型 / 连接」列算成 521px，加固定列共 **802.5px** → 溢出 17px，
右侧详情卡被顶出视口。实测 **1240 / 1440 均复现，1600 才不溢出**——即主流桌面
宽度全中。1041px 下更严重：表格 789px 对容器 451px，溢出 338px。

**修法**（`ui/models.css`，纯 CSS，零 DOM 改动）：把列宽从「auto 均分」改成
**显式标尺**——供应商 96px、上下文 58px、操作 84px 固定，勾选列 45px 固定，
「模型 / 连接」`min-width:0` 吃掉剩余宽度（286px 固定，1440 下留 500px 给模型名）。
同时 `th { white-space: nowrap }` 钉住表头，避免中文列名在窄列里折行读出
「两行表头」的错位感。

**验证**：修复后 1041 / 1240 / 1440 / 1600 四档 `table=802.5 wrapClient=803`
（1440）等各档均不溢出；全视口 `clipped=0`。

### 13.3 已修：三处触达目标低于 24px（P1，实测）

探针按 WCAG 2.2 AA 的 24px 目标尺寸最小值扫描 `button/a/input/select/summary`，
实测三处不达标：

| 位置 | 实测 | 修法 |
|---|---|---|
| `.collection-line a`「采集设置」（观测页） | 56×16 | `min-height:24px` + `padding:0 4px`；仍是「小字 + 箭头」边注，不改成按钮 |
| `.notice a`「前往登录与模型刷新」（供应商页 WorkBuddy 通知） | 99×14 | `padding:5px 2px` + 印刷式下划线；**保持 `display:inline`**——改成 block/flex 会把链接从句子末尾推到下一行 |
| `.text-button`「重新扫描本机安装」（Agent 页） | 96×21 | `padding:2px 0` + `min-height:24px`；不给边框——那会把它从「句子级标题」抬成控件，与同列路径、时间戳抢层级 |

**保留不计**：`input[type=checkbox]` 14×14（其可点目标是整个 `.check-label` /
表格单元格标签，符合 WCAG 2.2 的例外条款）；`.range-detail summary` 15.5px 高
（这是 `<details>` 折行条，同属例外条款，且改高会改变筛选区的密度节奏）。

### 13.4 保留与未做（防止过度修正）

- **窄屏表格横向滚动不改**：890px / 1000px 的 `min-width` 是 §5 的刻意声明，
  390px 下横向滚动 + 操作列粘右缘是既定设计，不是缺陷。
- **断点双轨**（styles 1180/850/620 vs daily 1200/900/620）：本轮未触碰，已由文末 §14 Phase 7 统一（2026-10-26）。
  与 §12.4 记录一致。
- **D14 nav-sync-dot 层级错位仍保留**：DOM 冻结下的有意保留，见 usability-standards.md。

---

## 14. Phase 7：断点双轨统一（2026-10-26）

> 起点：§12.4 / §13.4 悬置项。性质：判定线补丁，不改变「本机台账」方向本身。
> 方法：先全量映射两文件 @media 职责，再以 fixture + Playwright 几何探针
> （42 组 = 3 页 × 14 视口）取改前基线，最小改动后同探针复测并跑全量浏览器回归。

### 14.1 映射结论（修订 §12.4/§13.4 的旧描述）

实际残留的双轨只有一处：**styles 1180（外壳收窄）vs daily 1200（观测内容紧凑化）**。
850（icon-rail 与子导航收起）与 620（移动顶栏，同值）已由 Wave A 对齐；
styles 720（方案同步控件）与 models.css 1300 / 1240 / 1040 是组件级断点，
只服务自己的容器，不进入页面梯子。统一后页面梯子为
**1600 / 1200 / 900 / 850 / 620**，两份文件可在同一张表上对读。

### 14.2 改动

- `styles.css`：`@media (max-width: 1180px)` → `1200px`（唯一行为改动，附梯子注释）；
- `daily.css` 头注释的断点清单同步更正；
- 行为变化仅限 1181–1200 区间：侧栏 224→200px（并入既有 200px 形态），内容列相应加宽约 24px。

### 14.3 验证

- 几何探针前后对比（agents / observe / providers × 14 视口）：**12 处差异全部落在
  1181–1200，区间外零差异**；差异内容仅为侧栏宽与内容列宽；
- `daily_browser.cjs` 5/5 步、`browser.cjs` 25 项全过（含 mobile layouts and dialog fit）；
- `scripts/verify-css.py` 全部通过；
- 前后截图与 geometry.json 存档 `tests/artifacts/design-round-2/{before,after}/`（Git 忽略）。

### 14.4 保留与备注

- 顺带核实：styles 该块内的 `main { padding: 26px 24px }` 实际被 daily.css 的
  `#content` id 规则覆盖（id 特异性更高），探针实测 ≤1200 内容边距仍为 28/32——
  该规则在改动前后同为死规则，行为不变；记录在案，不顺手清理。
- 探针脚本为一次性工具，未入库（同 §12.5 惯例）。

---

## 12. Phase 5：执行修订记录（2026-10-21）

> 起点：视觉专家 Phase 1 审计（12 项发现）→ Lead 裁定 → 本阶段实施。
> 性质与 §10/§11 相同：**判定线的补丁与澄清**，不改变方向本身。
> 所有改动已落地；`scripts/verify-css.py`（本阶段起覆盖 `ui/models.css`）全量回归通过。

### 12.1 JS 渲染层收编进 token 体系（颜色事实回到单一来源）

| 改动 | 依据 |
|---|---|
| `app.js` 的 `palette` 第二色表（4 组硬编码 hex）删除；`providerColor` 改为输出 `var(--cat-1..6)`，按供应商顺序 6 档取模 | token 单一来源（§2/§8.2）。旧实现 `indexOf % 4` 使第 5 个供应商与第 1 个撞同色，`--cat-5/--cat-6` 从未被使用；身份色撞车直接破坏 §10.2.2 的「这是谁」判读 |
| `--avatar-bg` / `--avatar-color` 两个 inline 自定义属性移除 | `.avatar`（styles.css）从不消费它们——死变量，只制造「JS 里还有一份颜色表」的错觉 |
| 同步结果 / 方案状态徽标：失败 → `red`；「跳过」保留 `amber` | §10.2.2 状态二分：颜色回答「现在怎么了」，失败=牛血红。同一个词「失败」曾一侧赭石一侧牛血红，破坏「同一事实同一记号」；跳过是未执行不是失败，amber 语义正确。*已知残留：方案历史明细表（app.js 的 plan report 行）else 分支仍 amber，待 JS 侧补齐* |
| 降级 / zstd 缺依赖两条通知的 inline 样式收编为 `notice warn` | 同语义同形：quick-switch 与网关的同级警告早已用完整 `notice warn`（wash + 边线 + 图标色），inline 复制体只有边线，永远收不到样式更新 |

### 12.2 inline 收编暴露的形态缺口（本轮新判定线）

`.notice` 基座的 `display:flex` 只服务「图标 + 内容」两件套的横排（如批量检测通知）。
warn 卡多为**多段正文**（标题 + 解释器路径 + 说明 + 命令 + 附注），是纵向文档流；
收编成 class 后由 `.notice.warn { display:block }` 接住（即旧 inline 版 `display:block` 的语义）。
实测证据（fixture + Playwright 几何探针）：修复前 zstd 卡 5 个子元素被 flex 挤成一行 5 列
（x≈247/361/610/844/1152 同一 y）、降级 `<details>` 的 summary 与段落并排；
修复后 `allStacked: true`、`summaryThenFirstStacked: true`。

### 12.3 判定线的执行补丁（纯 CSS）

| 改动 | 依据 |
|---|---|
| `.requests-table td` 整表 + `.trend-column` 补 `font-variant-numeric: tabular-nums lining-nums` | §3.4 清单项；观测页是数字最密、唯一 30s 轮询刷新的页面 |
| `.discover-row.existing` 删 `opacity:.6` → `color:var(--ink-4); background:var(--surface-sunk)`；`.filter-chip .count` 删 `opacity:.75` → `color:var(--ink-3)` | §7.13 禁 opacity 调暗（对比度不可控）。existing 行的 `.mono`（ID）保持 ink-3 比行级 ink-4 亮一档：ID 是该行的关键数据，刻意不压平 |
| `.empty-state h3` / `.obs-empty h3` 删 `font-family:var(--font-display)` | §1.2 衬线只在 16–28px；两处 14px 越界，空状态标题回 sans semibold |
| `.provider-id` ink-4 → ink-3 | D 表判定规则：供应商 ID 是核证数据（同步写入的真相键），ink-4 仅「删掉也不丢信息」 |
| `.instance-picker` 删 color 覆盖 | select 的值是数据，继承 input 基座的 ink |
| models 高级抽屉标题 hover、会话标题链接 hover：朱砂 → ink | §7.6/§10.2.5 朱砂预算：两者不是数据焦点；ghost 类 hover 规范是 ink + 下划线 |
| `.button-link` 补 `font-weight:var(--w-medium); min-height:32px` | 与 button 基座同形（同工具条并排出现两个字重破坏一致性） |
| `#content` 删 `max-width:1800px` | §5 明令 `max-width:none` 的残留（1440 窗口下无实际影响，属规范清账） |
| 620px 断点补 `.nav-item { font-size:var(--fs-sm); gap:6px }` | styles.css ≤850 的 `font-size:0 / gap:0` 是 icon-rail 专用，在 620 横条布局下是残留；恢复图标+文字并读。实测 620/390 均无横向溢出 |

### 12.4 保留与悬置

- **死 token 三枚保留为规范保留位**：`--accent-bright` / `--graphite` / `--shadow-float`
  当前零消费。`--accent-bright` 是「采集脉冲点 / 图表高亮柱用更亮一档非文字朱砂」的既定
  规范位（§2.1/§4.5），当前实现统一用了 `--accent` 属合并执行；若日后需要区分
  「非文字朱砂比文字朱砂亮一档」，从 token 起用而非新造色。`verify-css.py` 的
  REQUIRED_TOKENS 仍强制其存在。
- **brand 显隐 620 冲突已统一（Wave A，用户批准延后项后实施）**：daily.css ≤620
  不再整体隐藏 `.brand`，与 styles.css ≤850 的 icon-rail 行为对齐——文字 span 由
  ≤850 规则隐藏，只留 brand-mark 印记（观测页窄窗为 26×26）。几何验证（fixture +
  Playwright）：620px 下 mark 26×26、文字 display:none、主导航三项 194px 等分同排；
  390px 下 117.3px 等分；两档均无横向溢出。**仍悬置的是断点双轨本身**（styles
  1180/850/580 vs daily 1200/900/620 作用于同一 DOM）：本轮只对齐了 brand 这一处
  可见行为，统一断点体系待后续。
- **§7.16 增补（网关端点豁免）**：Hub 自身网关的端点文案（如 `http://127.0.0.1:8789`）
  属「有据可查」的数据展示，以 `.mono` 弱化呈现（已在 workbench 落地），不算网页宿主
  残留；反模式 16 的本义是清除「在浏览器打开 / 刷新页面」类宿主痕迹。

### 12.5 本阶段验证方式存档

- `python scripts/verify-css.py`：本阶段起 `ui/models.css` 纳入校验
  （语法 / 选择器守恒 / token / 反模式 / 圆角 / 字重 / 字距）。
- 实测探针：`tests/daily_fixture_server.py` 副本 + `zstd_available()=False` 补丁
  （fixture 采集改 enabled）触发真实 `collect` 路径产生降级与 zstd 缺依赖态；
  Playwright 以几何矩形 + 计算样式断言 warn 卡堆叠、discover 行墨阶构成、
  620/390 无溢出。探针脚本为一次性工具，未入库；结论已并入 12.2/12.3。

---

## 4. 变更记录（本文档）

- 2026-10-20 初版（§1–§8）+ Phase 3 实施记录。
- 2026-10-20 §10 视觉重点修订（一图一朱砂 / 状态可淡填充身份永远描边 / 主卡 = 主题卡 / 墨阶按信息类型分配 / primary 制度不变）。
- 2026-10-20 §11 Phase 4 背景修订（三图层 → 三束不重复光场；可平铺纹理进回归断言）。
- 2026-10-21 §12 Phase 5 执行修订（JS 颜色收编 token、inline 收编、墨阶/tnum/基形补丁、死 token 保留位、brand 断点对齐）。
- **2026-10-25 §13 Phase 6**：UI 设计指令的质量循环。新增三条判定线——探针须按 `overflow-x` 区分「滚动容器」与「真裁剪」；双栏布局的列宽必须按左栏实际可用宽度计算；触达目标按 WCAG 2.2 AA 的 24px 计量。修复模型库表格在 1240/1440 下溢出右栏 17px（1041 下 338px）与三处 <24px 触达目标；验证设施为 fixture + Playwright 几何探针，全视口 clipped=0。

- **2026-10-25 设计总监走查（全产品）**：以 fixture + Playwright 对 1440×1000 全部页面与关键交互态重新渲染复核，确认「本机台账」方向执行到位，无方向性偏差（纸面/发丝线/单一朱砂/未知即缺失四语言在全站一致；观测页是密度与秩序成立的样板）。本轮主问题：模型与方案页首屏把同一句操作指导印了三遍——两张卡片标题各一遍、右栏空态第三遍，首屏最贵的两处位置都在解释用法而非呈现内容。修复：交互引导收敛到右栏空态一处（恰在无选中时出现、选中后消失），卡片标题回到内容描述（客户端范围 / 勾选与连接语义）；仅 `ui/app.js` 三处字符串，无 DOM、样式与契约断言变动。验证：`scripts/verify-css.py` 通过；ui_contract 29 / ui_async_contract 2 / workbench_contract 10 / bridge_contract 11 / daily_browser 5 步 / browser 25 项全部通过；前后渲染对比（`tests/artifacts/design-round-1/`）确认模型页 1544→1525px，其余页面仅剩夹具运行时数据（时间戳、随机临时路径）造成的亚感知差异。§12.4 / §13.4 的悬置项（断点双轨、D14 nav-sync-dot）复核后维持原判，未动。
- **2026-10-26 §14 Phase 7**：断点双轨统一——styles 1180 → 1200，页面梯子定为 1600 / 1200 / 900 / 850 / 620（850 与 620 此前已由 Wave A 对齐）；几何探针 42 组（3 页 × 14 视口）证明行为变化仅限 1181–1200 区间（侧栏 224→200px），区间外零差异；daily_browser 5/5、browser.cjs 25/25、verify-css 全过。§12.4 / §13.4 的断点悬置项就此关闭；遗留记录：styles 该块 `main padding` 为 daily `#content` id 规则覆盖的死规则（前后行为一致，未清理）。
