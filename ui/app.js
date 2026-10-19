"use strict";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icons = {
  hub: '<rect x="3" y="3" width="6" height="6" rx="1.5"/><rect x="15" y="3" width="6" height="6" rx="1.5"/><rect x="3" y="15" width="6" height="6" rx="1.5"/><path d="M18 15v6m-3-3h6M9 6h6M6 9v6"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
  layers: '<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
  sync: '<path d="M20 7a8 8 0 0 0-14-2L3 8m0-5v5h5M4 17a8 8 0 0 0 14 2l3-3m0 5v-5h-5"/>',
  refresh: '<path d="M20 11a8 8 0 1 0-2 6M20 4v7h-7"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  x: '<path d="m6 6 12 12M6 18 18 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  edit: '<path d="m15 4 5 5M4 20l4-1L21 6a2 2 0 0 0-3-3L5 16l-1 4Z"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  shield: '<path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7l-9-4Z"/><path d="m8 12 3 3 5-6"/>',
  sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4m-2-2h4"/>',
  brain: '<path d="M12 4c-4-4-8 0-7 3-4 1-4 7-1 8-1 4 5 7 8 3m0-14c4-4 8 0 7 3 4 1 4 7 1 8 1 4-5 7-8 3V4Zm-5 7 2 1M5 16l3-1m9-4-2 1m4 4-3-1"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v.01"/>',
  alert: '<path d="m12 3 10 18H2L12 3Zm0 6v5m0 3v.01"/>',
  link: '<path d="m10 13 4-4m-6 6-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 4 1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0"/>',
  arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  key: '<circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-3-3 3-3m-6 0 3-3"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.grid}</svg>`;
$$('[data-icon]').forEach(el => { el.innerHTML = icon(el.dataset.icon); });

const PAGES = { agents: ["Agent", "维护实际使用的安装实例与模型选择。"], observe: ["观测", "从实际记录理解使用情况。"], settings: ["设置", "连接、模型与本机采集设置。"], models: ["模型库", "管理可用模型，让每个 Agent 都用上合适的模型。"], providers: ["供应商", "连接你的模型服务，所有平台共用一份配置。"], sync: ["平台同步", "将本地配置写入多个平台，每次同步都有迹可循。"] };
const TARGETS = {claude:["Claude Code","cc","原生配置"], codex:["Codex","cx","CLI 与桌面版配置"], pi:["pi","pi","模型与默认设置"], dsh:["dsh","ds","CLI 与桌面版共用配置"], "dsh-desktop":["dsh 桌面版","ds","跟随 dsh"]};
const palette = [["#f0edff", "#9984cd"], ["#fff1e9", "#c99b7b"], ["#edf3ff", "#84a0cf"], ["#edf7f3", "#80af9b"]];
// 供应商表单里的平台多选：顺序固定，便于用户按习惯找到目标平台。
const PROVIDER_TARGETS = [["claude","Claude Code"],["codex","Codex"],["pi","pi"],["dsh","dsh"]];
const DEFAULT_PROVIDER_TARGETS = ["claude","codex","pi","dsh"];
const AGENT_OVERRIDES = [["claude","Claude Code","Anthropic Messages"],["codex","Codex","Responses"],["pi","pi","OpenAI 兼容端点"],["dsh","dsh（含桌面版）","OpenAI 兼容端点"]];
const AGENT_APIS = {claude:["anthropic-messages"],codex:["openai-responses"]};
const AGENT_LABEL = {claude:"Claude Code",codex:"Codex",pi:"pi",dsh:"dsh"};
const EFFORTS = ["off","minimal","low","medium","high","xhigh","max","ultra"];
const CODEX_EFFORTS = ["none","minimal","low","medium","high","xhigh","max","ultra"];
const SYNC_CLIENTS = PROVIDER_TARGETS;
const MODE_CLIENTS = {codex: {label:"Codex",lineLabel:"Codex 请求线路",noun:"线路",officialOption:"官方 · 恢复原模型与推理强度",thirdPartyOption:"第三方 · 使用 Hub 模型设置",officialTag:"官方线路",optInText:"我明确选择第三方线路，Codex 请求将发送到所选供应商",officialNote:"同步时恢复 Hub 首次切换第三方前保存的官方 model、model_provider 和 reasoning effort；不会更改 auth.json 登录态。",officialKeepNote:"官方模式保留 Hub 的第三方选择。"}};
const CLAUDE_TIERS = [["opus", "opus 档位"], ["sonnet", "sonnet 档位"], ["haiku", "haiku 档位"], ["subagent", "subagent 档位"]];
// ---- 切换方案（一次选择，明确应用到多个 Agent）----
// 参与切换的 Agent 与展示顺序；后端 switch_plans.PLAN_TARGETS 是同一份名单。
const PLAN_TARGETS = [["claude", "Claude Code"], ["codex", "Codex"], ["pi", "pi"], ["dsh", "dsh（含桌面版）"]];
// 每个客户端的推理强度词表：Codex 没有 off、多了 none，不能和其它 Agent 混为一谈。
const PLAN_EFFORTS = {
  codex: ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
  claude: EFFORTS, pi: EFFORTS, dsh: EFFORTS, zcode: EFFORTS,
};
// 该客户端有没有"推理强度"这个参数：pi 没有，界面必须如实说"不生效"。
const PLAN_EFFORT_SUPPORT = { claude: true, codex: true, pi: true, dsh: true, zcode: true };
const PLAN_POLICY_LABEL = { follow: "跟随本次选择", fixed: "单独指定", keep: "保留客户端当前值" };
const PLAN_STATUS_LABEL = { ok: "将写入", warning: "将写入（有提示）", skipped: "跳过", error: "不兼容", failed: "写入失败" };
const emptyPlanDraft = () => ({ base: { provider: "", model: "", reasoningEffort: "" }, targets: [], agents: {} });
const state = {
  providers: {}, status: null, page: "models", busy: false, loaded: false,
  filters: { query: "", provider: "all", agent: "all", capability: "all" }, modelPage: 1, selection: new Set(),
  providerQuery: "", targetDraft: {}, clientMode: { codex: "official", zcode: "official" }, clientOptIn: { codex: false, zcode: false }, preview: null, previewError: "", editor: null, editorSnapshot: "", discovered: [], importCandidates: null, importTarget: "claude",
  modelSync: null, modelSyncError: "", modelSyncLoading: false, modelSyncDraft: null, modelSyncDraftKey: "", modelSyncCustom: {}, modelSyncOpen: new Set(),
  discoverAgent: "",
  // 切换方案：已保存方案来自后端，草稿是本次要应用的意图。
  plans: null, plansError: "", planDraft: null, planLoaded: "", planName: "", planDescription: "",
  planTargets: new Set(), planOverrides: {}, planAnalysis: null, planPreviewResult: null, planBusy: false,
};
const pathFor = pid => `/api/providers/${encodeURIComponent(pid)}`;
const modelKey = (pid, agent, mid) => JSON.stringify([pid, agent || "", mid]);
const providerName = pid => state.providers[pid]?.displayName || state.providers[pid]?.name || pid;
const providerColor = pid => palette[Math.max(0, Object.keys(state.providers).indexOf(pid)) % palette.length];
// 模型库里同时列出默认连接与各 Agent 连接上的模型；agent 为空表示默认连接。
const connectionLabel = agent => agent ? (AGENT_LABEL[agent] || agent) : "默认";
const allModels = () => Object.entries(state.providers).flatMap(([pid, p]) => {
  const groups = [["", p.models || []]];
  for (const [name, block] of Object.entries(p.agents || {}).filter(([agent]) => AGENT_LABEL[agent])) groups.push([name, (block && block.models) || []]);
  return groups.flatMap(([agent, list]) => (list || []).map(m => ({ ...m, pid, agent, key: modelKey(pid, agent, m.id) })));
});
// 自定义供应商：把对象渲染成可编辑的 JSON 文本（空对象显示为空白而不是 {}）。
const jsonText = obj => (obj && typeof obj === "object" && Object.keys(obj).length ? JSON.stringify(obj, null, 2) : "");
// 自定义供应商：解析表单里的 JSON 文本；空文本表示清除该字段。
const parseJsonField = (raw, label) => {
  const text = String(raw || "").trim();
  if (!text) return null;
  let parsed;
  try { parsed = JSON.parse(text); }
  catch (err) { throw new Error(`${label} 不是合法的 JSON：${err.message}`); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error(`${label} 必须是 JSON 对象`);
  return parsed;
};
const effectiveTargets = () => Object.entries(state.status?.targets || {}).filter(([, t]) => t.supported && !t.aliasOf);
const enabledCount = () => effectiveTargets().filter(([, t]) => t.enabled).length;
const targetDirty = () => effectiveTargets().some(([n, t]) => state.targetDraft[n] !== t.enabled) || Object.keys(MODE_CLIENTS).some(name => state.clientMode[name] !== (state.status?.targets?.[name]?.mode || "official")) || (state.loaded && modelSyncDirty());
const formSnapshot = () => JSON.stringify([...new FormData($("#editor-form")).entries()]);
const editorDirty = () => state.editor && state.editor.type !== "discover" && formSnapshot() !== state.editorSnapshot;
const dateLabel = value => {
  if (!value) return "尚未同步";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
};
const tokens = n => n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(2)}M` : n >= 1000 ? `${+(n / 1000).toFixed(1)}K` : String(n);

// ---- 多 agent 模型设置（modelSync）----
const enabledProviders = () => Object.entries(state.providers).filter(([, p]) => p.enabled !== false).map(([pid]) => pid);
// 供应商在用的模型 ID（默认连接）：models / options 里的条目可能是对象也可能是字符串。
const modelIds = list => (list || []).map(item => (typeof item === "string" ? item : item && item.id)).filter(Boolean);
const providerModels = (pid, target) => {
  const p = state.providers[pid] || {};
  const override = target ? p.agents?.[target] : null;
  const source = Array.isArray(override?.models) ? override.models : p.models;
  const ids = new Set([...modelIds(source), ...(!target ? [p.agentModel] : [])].filter(Boolean));
  return [...ids].sort();
};
// 模型候选 = 该 Agent 连接上的远端选项与本地模型库；没有 target 时退到总览选项。
// 同一供应商给不同 Agent 配不同 key 时，各自连接的模型集合本就不同。
const knownModels = (pid, target) => new Set([
  ...(pid ? modelIds(target ? state.modelSync?.optionsByTarget?.[target]?.[pid] : state.modelSync?.options?.[pid]) : []),
  ...(pid ? providerModels(pid, target) : []),
]);
const modelChoices = (pid, target) => [...knownModels(pid, target)].sort();
const cleanEntry = entry => {
  const out = {};
  if (Array.isArray(entry?.preserveTiers)) out.preserveTiers = [...entry.preserveTiers];
  if (entry?.provider) out.provider = entry.provider;
  if (entry?.model) out.model = entry.model;
  if (entry?.reasoningEffort) out.reasoningEffort = entry.reasoningEffort;
  return out;
};
// 磁盘上还没有 modelSync 时，用后端解析出来的实际值给全局行做初始值。
const resolvedSeed = () => {
  const resolved = state.modelSync?.resolved || {};
  for (const target of ["claude", "pi", "dsh", "dsh-desktop", "zcode"]) {
    const entry = resolved[target];
    if (entry?.provider && entry?.model) return { provider: entry.provider, model: entry.model };
  }
  return {};
};
// 把后端返回的 modelSync 收敛成前端使用的固定形状：只保留认识的键，空对象表示没有 override。
const normalizeModelSync = (raw, fallback = {}) => {
  const d = raw?.default || {};
  const result = { default: cleanEntry(d), overrides: {} };
  // 没有 provider / model 时依次沿用上一份草稿、后端解析结果、第一个已启用供应商。
  const seed = resolvedSeed();
  result.default.provider = result.default.provider || fallback?.default?.provider || seed.provider || enabledProviders()[0] || "";
  result.default.model = result.default.model || fallback?.default?.model || (result.default.provider === seed.provider ? seed.model : "") || "";
  for (const [name, entry] of Object.entries(raw?.overrides || {})) {
    if (name === "dsh-desktop" || !entry || typeof entry !== "object") continue;
    const item = cleanEntry(entry);
    if (name === "claude") {
      const tiers = {};
      for (const [tier, value] of Object.entries(entry.tiers || {})) {
        const t = cleanEntry(value);
        if (t.model) tiers[tier] = t;
      }
      if (Object.keys(tiers).length) item.tiers = tiers;
    }
    if (Object.keys(item).length) result.overrides[name] = item;
  }
  return result;
};
const modelSyncResolved = target => state.modelSync?.resolved?.[target] || null;
// Claude 档位明细由后端放在顶层 tiers.claude；这里兼容旧的 resolved.claude.tiers 形状。
const claudeTiersView = () => state.modelSync?.tiers?.claude || state.modelSync?.resolved?.claude?.tiers || {};
// 客户端行显示的值：优先草稿，其次后端解析结果；override 里没有写就说明继承全局。
const draftEntry = (scope, target, tier) => {
  const draft = state.modelSyncDraft;
  if (!draft) return {};
  if (scope === "tiers") return draft.overrides?.claude?.tiers?.[tier] || {};
  if (scope === "clients") return draft.overrides?.[target] || {};
  return draft.default || {};
};
function ensureModelSyncDraft() {
  const key = JSON.stringify(state.modelSync || {});
  if (!state.modelSyncDraft || state.modelSyncDraftKey !== key) {
    // 只把 modelSync 这一块交给归一化：resolved / tiers / options 是展示用的解析结果。
    state.modelSyncDraft = normalizeModelSync(state.modelSync?.modelSync || {}, state.modelSyncDraft);
    state.modelSyncDraftKey = key;
    state.modelSyncCustom = {};
  }
  return state.modelSyncDraft;
}
function modelSyncDirty() {
  // 与后端返回的原始配置比较：从解析结果预填出来的"当前值"不算用户改动，只能显式操作才算。
  return JSON.stringify(ensureModelSyncDraft()) !== JSON.stringify(normalizeModelSync(state.modelSync?.modelSync || {}));
}
async function loadModelSync() {
  state.modelSyncLoading = true;
  try {
    // 卡片不只需要 modelSync 本身，还需要 resolved / tiers / options，所以整份响应都保留。
    const data = await api("GET", "/api/modelsync");
    state.modelSync = { modelSync: data.modelSync || {}, resolved: data.resolved || {}, tiers: data.tiers || {}, options: data.options || {}, optionsByTarget: data.optionsByTarget || {}, configured: !!data.configured };
    state.modelSyncError = "";
  } catch (e) {
    // 接口缺失或后端报错时保留现有设置，只在卡片里提示，不让整页崩掉。
    state.modelSync = null;
    state.modelSyncError = e.message;
  } finally {
    state.modelSyncLoading = false;
  }
}
function renderModelSyncCard() {
  $("#modelsync-card").innerHTML = modelSyncCard();
  if ($("#target-count")) updateTargetDraftUI();
}
async function saveModelSync() {
  const draft = ensureModelSyncDraft();
  const scopes = [["clients", "codex", ""], ["clients", "pi", ""], ["clients", "dsh", ""], ["clients", "zcode", ""], ["clients", "claude", ""], ["tiers", "claude", "opus"], ["tiers", "claude", "sonnet"], ["tiers", "claude", "haiku"], ["tiers", "claude", "subagent"]];
  // 界面上填了 provider 却没填模型（或反过来）的行无法写入，先拦下来。
  for (const [scope, target, tier] of scopes) {
    const entry = scope === "tiers" ? draft.overrides?.claude?.tiers?.[tier] : draft.overrides?.[target];
    if (!entry) continue;
    const label = scope === "tiers" ? `Claude Code · ${tier} 档位` : (SYNC_CLIENTS.find(c => c[0] === target)?.[1] || target);
    if (entry.provider && !entry.model) throw new Error(`${label} 已指定供应商，还需要选择一个模型（或点「跟随全局」清除）。`);
    if (entry.model && !entry.provider) throw new Error(`${label} 已指定模型，还需要选择供应商（或点「跟随全局」清除）。`);
  }
  const result = await api("PUT", "/api/modelsync", { modelSync: draft });
  // 后端可能以 200 + ok:false 返回校验警告，这时不能当作保存成功。
  if (result && result.ok === false) throw new Error((result.warnings || []).join("；") || "模型设置未通过后端校验。");
  // 只更新 modelSync 这一块；resolved / tiers / options 仍以随后的 reload 结果为准。
  state.modelSync = { ...(state.modelSync || {}), modelSync: normalizeModelSync((result && result.modelSync) || draft, draft), configured: true };
  state.modelSyncCustom = {};
  renderModelSyncCard();
  const warnings = (result && result.warnings) || [];
  return { warnings };
}

// ---- 切换方案：拉取、草稿、渲染、预览、应用 ----
async function loadPlans() {
  try {
    const data = await api("GET", "/api/switch-plans");
    state.plans = data;
    state.plansError = "";
    if (!state.planDraft) state.planDraft = draftFromPlan(data.store.plans.find(p => p.id === data.store.default));
  } catch (e) {
    state.plans = null;
    state.plansError = e.message;
  }
}
// 已保存方案 -> 可编辑草稿；只保留本轮真正会用到的键，避免把失效的旧字段带进新方案。
function draftFromPlan(plan) {
  if (!plan) return emptyPlanDraft();
  const agents = {};
  for (const [name, spec] of Object.entries(plan.agents || {})) {
    agents[name] = { policy: spec.policy || "follow", provider: spec.provider || "", model: spec.model || "", reasoningEffort: spec.reasoningEffort || "" };
  }
  return {
    base: { provider: plan.base?.provider || "", model: plan.base?.model || "", reasoningEffort: plan.base?.reasoningEffort || "" },
    targets: [...(plan.targets || [])],
    agents,
  };
}
// 草稿 -> 方案请求体；空字段一律省略，不产生"看起来有值其实是空串"的配置。
function planPayload() {
  const base = state.planDraft.base || {};
  // 勾选框就是本次范围：以它为准，而不是草稿里上一次保存的范围。
  const targets = PLAN_TARGETS.map(([name]) => name).filter(name => state.planTargets.has(name));
  const plan = { name: state.planName.trim(), description: state.planDescription.trim(), targets };
  if (base.provider && base.model) {
    plan.base = { provider: base.provider, model: base.model };
    if (base.reasoningEffort) plan.base.reasoningEffort = base.reasoningEffort;
  }
  const agents = {};
  for (const name of targets) {
    const spec = state.planOverrides[name];
    if (!spec) continue;
    const entry = { policy: spec.policy || "fixed" };
    if (entry.policy === "fixed") { entry.provider = spec.provider; entry.model = spec.model; }
    if (spec.reasoningEffort) entry.reasoningEffort = spec.reasoningEffort;
    if (spec.allowUnknownModel) entry.allowUnknownModel = true;
    agents[name] = entry;
  }
  if (Object.keys(agents).length) plan.agents = agents;
  return plan;
}
function planDirty() {
  if (!state.planDraft) return false;
  return JSON.stringify(planPayload()) !== JSON.stringify(state.planLoaded);
}
function planTargetsForModel(provider, model) {
  // 同一个模型名不代表每条连接都有权限：逐连接判断哪个 Agent 真能用这个模型。
  const out = [];
  for (const [name] of PLAN_TARGETS) {
    const conn = state.plans?.targets?.[name]?.connections?.[provider];
    if (conn && conn.ready && conn.enabled && conn.models.includes(model)) out.push(name);
  }
  return out;
}
function planModelOptions(provider) {
  const seen = new Set();
  for (const [name] of PLAN_TARGETS) {
    const conn = state.plans?.targets?.[name]?.connections?.[provider];
    if (conn && conn.ready && conn.enabled) (conn.models || []).forEach(m => seen.add(m));
  }
  return [...seen].sort();
}
function planProviderIds() {
  const seen = new Set();
  for (const [name] of PLAN_TARGETS) {
    Object.entries(state.plans?.targets?.[name]?.connections || {}).forEach(([pid, conn]) => {
      if (conn.ready && conn.enabled) seen.add(pid);
    });
  }
  return [...seen].sort((a, b) => providerName(a).localeCompare(providerName(b)));
}
function planIssueList(analysis, severity) {
  const items = (analysis?.issues || []).filter(i => i.severity === severity);
  if (!items.length) return "";
  return `<ul class="plan-issues">${items.map(i => `<li class="${esc(severity)}">${icon(severity === "error" ? "alert" : "info")}<span>${i.target ? `<strong>${esc(PLAN_TARGETS.find(t => t[0] === i.target)?.[1] || i.target)}</strong> ` : ""}${esc(i.message)}${i.fix ? `<em>建议：${esc(i.fix)}</em>` : ""}</span></li>`).join("")}</ul>`;
}
function planResultRows(result) {
  const rows = result?.results || result?.analysis?.details?.map(d => ({ ...d, message: d.reason })) || [];
  if (!rows.length) return "";
  return rows.map(row => {
    const status = row.status === "warning" ? "ok" : row.status;
    const effort = row.effort || {};
    const effortText = effort.support === "unsupported"
      ? "该客户端没有推理强度参数"
      : effort.requested ? `强度 ${effort.requested}` : (effort.current ? `保留当前强度 ${effort.current}` : "未指定强度");
    const tiers = (row.tiers || []).length ? ` · 档位 ${row.tiers.map(t => `${t.tier}:${t.action === "clear" ? "清除" : t.provider + "/" + t.model}`).join("、")}` : "";
    const files = (row.files || []).length ? `<p class="mono plan-files">${row.files.map(f => esc(f.path)).join("<br />")}</p>` : "";
    return `<div class="result-row ${status === "ok" ? "" : "error"}"><div class="result-symbol">${icon(status === "ok" ? "check" : status === "skipped" ? "clock" : "x")}</div><div>
      <strong>${esc(row.label || row.target)}</strong>
      <p>${esc(PLAN_STATUS_LABEL[row.status] || row.status)} · ${row.provider ? `${esc(row.provider)} / ${esc(row.model)}` : "未指定模型"} · ${esc(effortText)}${esc(tiers)}</p>
      ${row.message ? `<p>${esc(row.message)}</p>` : ""}
      ${row.needsClientAction ? `<p class="plan-client-action">${icon("info")}${esc(row.needsClientAction)}</p>` : ""}
      ${files}</div><span class="tag ${status === "ok" ? "green" : "amber"}" style="margin-left:auto">${esc(PLAN_STATUS_LABEL[row.status] || row.status)}</span></div>`;
  }).join("");
}
function fallbackPanel(diag) {
  if (!diag || !(diag.routes || []).length) return "";
  const rows = diag.routes.map(route => {
    const bad = (route.checks || []).filter(c => !c.usable);
    const detail = bad.length
      ? bad.map(c => `${PLAN_TARGETS.find(t => t[0] === c.target)?.[1] || c.target}：${c.reason}`).join("；")
      : "约束与连接检查都通过";
    return `<li class="${route.usable ? "ok" : ""}">${icon(route.usable ? "check" : "alert")}<span><strong>${esc(route.label)}</strong> · ${esc(detail)}</span></li>`;
  }).join("");
  const unknown = (diag.unknowns || []).length ? `<p class="plan-note">未知信息（不猜）：${esc(diag.unknowns.join("、"))}</p>` : "";
  return `<details class="plan-fallback"><summary>备用线路诊断（白名单顺序 · 不自动执行）</summary>
    <p class="plan-note">${esc(diag.note)}</p><ul class="plan-issues">${rows}</ul>
    <p class="plan-note">自动切换还缺：${esc((diag.autoSwitchBlockedBy || []).join(" "))}</p>${unknown}</details>`;
}
function planTargetRow(name, label) {
  const selected = state.planTargets.has(name);
  const base = state.planDraft.base || {};
  const spec = state.planOverrides[name];
  const policy = spec?.policy || "follow";
  const provider = policy === "fixed" ? spec.provider : (base.provider || "");
  const model = policy === "fixed" ? spec.model : (base.model || "");
  const conn = provider ? state.plans?.targets?.[name]?.connections?.[provider] : null;
  const ready = !!(conn && conn.ready && conn.enabled);
  const known = !!(conn && model && conn.models.includes(model));
  const effort = policy === "fixed" ? (spec.reasoningEffort || "") : (base.reasoningEffort || "");
  const effortOk = !effort || (PLAN_EFFORTS[name] || EFFORTS).includes(effort);
  const choices = provider ? planModelOptions(provider) : [];
  const ownProviders = Object.entries(state.plans?.targets?.[name]?.connections || {})
    .filter(([, c]) => c.ready && c.enabled).map(([pid]) => pid);
  const connNote = !selected ? "未参与本次切换"
    : !provider ? "还没有选主模型"
    : !ready ? `${provider} 没有可用的 ${label} 连接`
    : !model ? "还没有选模型"
    : known ? "连接就绪、模型在该连接上"
    : "模型不在该连接的模型库里";
  return `<div class="plan-target ${selected ? "on" : ""}" data-plan-target="${esc(name)}">
    <label class="plan-target-head"><input type="checkbox" data-plan-pick="${esc(name)}" ${selected ? "checked" : ""} aria-label="让 ${esc(label)} 参与本次切换" /><strong>${esc(label)}</strong>
      <span class="tag ${!selected ? "" : ready && known ? "green" : "amber"}">${esc(connNote)}</span></label>
    <div class="plan-target-body">
      <div class="plan-summary">${provider ? `<span class="mono">${esc(provider)} / ${esc(model || "—")}</span>` : "<span>—</span>"}
        <span class="plan-effort">${esc(effort ? `强度 ${effort}` : "强度跟随默认")}${effort && !effortOk ? `（${esc(label)} 不支持！）` : ""}${effort && !PLAN_EFFORT_SUPPORT[name] ? "（该客户端无此参数）" : ""}</span>
        <select data-plan-policy="${esc(name)}" aria-label="${esc(label)} 的取值方式">
          ${["follow", "fixed", "keep"].map(p => `<option value="${p}" ${policy === p ? "selected" : ""}>${esc(PLAN_POLICY_LABEL[p])}</option>`).join("")}
        </select></div>
      ${policy === "fixed" ? `<div class="plan-fixed">
        <select data-plan-provider="${esc(name)}" aria-label="${esc(label)} 供应商"><option value="">选择供应商</option>${ownProviders.map(pid => `<option value="${esc(pid)}" ${pid === spec.provider ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select>
        <select data-plan-model="${esc(name)}" aria-label="${esc(label)} 模型"><option value="">选择模型</option>${choices.map(m => `<option value="${esc(m)}" ${m === spec.model ? "selected" : ""}>${esc(m)}</option>`).join("")}${spec.model && !choices.includes(spec.model) ? `<option value="${esc(spec.model)}" selected>${esc(spec.model)}（不在该连接模型库）</option>` : ""}</select>
        <select data-plan-effort="${esc(name)}" aria-label="${esc(label)} 推理强度"><option value="">强度：跟随本次选择</option>${(PLAN_EFFORTS[name] || EFFORTS).map(v => `<option value="${v}" ${v === spec.reasoningEffort ? "selected" : ""}>${esc(effortLabel(v))}</option>`).join("")}</select>
        ${known ? "" : `<label class="check-label plan-allow"><input type="checkbox" data-plan-allow="${esc(name)}" ${spec.allowUnknownModel ? "checked" : ""} />该模型不在连接模型库里，仍按手填值写入</label>`}
      </div>` : ""}
    </div></div>`;
}
function planCard() {
  const header = `<div class="card-heading"><div><h2>切换方案</h2><p>选一次模型，勾上要跟着换的 Agent，先看清单再确认；方案可以存下来反复用。</p></div>${state.planBusy ? `<span class="tag amber">处理中…</span>` : planDirty() ? `<span class="tag amber">清单有改动</span>` : `<span class="tag">未改动</span>`}</div>`;
  if (state.plansError) {
    return `<section class="card plan-card" id="plan-card">${header}<div class="notice warn" id="plan-warning">${icon("alert")}<div><strong>切换方案接口不可用</strong><br />${esc(state.plansError)}<br />其余同步功能不受影响。</div></div></section>`;
  }
  if (!state.plans) return `<section class="card plan-card" id="plan-card">${header}<div class="load-state"><span class="spinner"></span><p>正在读取切换方案…</p></div></section>`;
  const saved = state.plans.store.plans || [];
  const base = state.planDraft.base || {};
  const providers = planProviderIds();
  const models = base.provider ? planModelOptions(base.provider) : [];
  const usable = base.provider && base.model ? planTargetsForModel(base.provider, base.model) : [];
  const chosen = [...state.planTargets];
  const stuck = chosen.filter(n => !usable.includes(n));
  const options = saved.length
    ? saved.map(p => `<option value="${esc(p.id)}" ${p.id === state.planLoaded?.id ? "selected" : ""}>${esc(p.name)}${p.stale ? "（引用已失效）" : ""}</option>`).join("")
    : "";
  const planPicker = `<div class="plan-picker">
    <label class="field"><span>已保存方案</span><select id="plan-select" aria-label="已保存的切换方案"><option value="">— 新方案 —</option>${options}</select></label>
    <div class="actions"><button class="small" type="button" data-action="plan-load">载入</button>
      <button class="small ghost" type="button" data-action="plan-duplicate" ${state.planLoaded ? "" : "disabled"}>复制为新方案</button>
      <button class="small ghost" type="button" data-action="plan-delete" ${state.planLoaded ? "" : "disabled"}>删除</button></div>
    <label class="field"><span>方案名称</span><input id="plan-name" value="${esc(state.planName)}" placeholder="例如：省钱模式" /></label>
    <label class="field"><span>说明（可选）</span><input id="plan-description" value="${esc(state.planDescription)}" placeholder="这份方案什么时候用" /></label>
    <div class="actions"><button class="small" type="button" data-action="plan-save" data-write>${state.planLoaded ? "更新这个方案" : "保存为新方案"}</button></div>
  </div>`;
  const stale = state.planLoaded && state.plans.store.plans.find(p => p.id === state.planLoaded.id)?.stale;
  const staleNotice = stale ? `<div class="notice warn">${icon("alert")}<div><strong>这份方案引用的供应商或模型已经不存在</strong><br />失效的引用会逐条列出，<b>不会自动换成别的模型</b>；请重新选择后再应用。</div></div>` : "";
  return `<section class="card plan-card" id="plan-card">${header}
    ${staleNotice}
    <div class="plan-switch">
      <div class="ms-row">
        <label class="ms-field"><span>选一个模型 · 供应商</span><select id="plan-provider" aria-label="切换目标供应商"><option value="">请选择供应商</option>${providers.map(pid => `<option value="${esc(pid)}" ${pid === base.provider ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select></label>
        <label class="ms-field"><span>模型</span><select id="plan-model" aria-label="切换目标模型" ${base.provider ? "" : "disabled"}><option value="">${base.provider ? "请选择模型" : "先选择供应商"}</option>${models.map(m => `<option value="${esc(m)}" ${m === base.model ? "selected" : ""}>${esc(m)}${usable.includes(m) || base.model !== m ? "" : ""}</option>`).join("")}${base.model && !models.includes(base.model) ? `<option value="${esc(base.model)}" selected>${esc(base.model)}（不在该连接模型库）</option>` : ""}</select></label>
        <label class="ms-field"><span>推理强度（可选）</span><select id="plan-effort" aria-label="切换目标推理强度"><option value="">各 Agent 保留当前值 / 各自词表</option>${EFFORTS.concat(["none"]).map(v => `<option value="${v}" ${v === base.reasoningEffort ? "selected" : ""}>${esc(effortLabel(v))}</option>`).join("")}</select></label>
        <div class="ms-field"><span>一次选中可用它的 Agent</span><button class="small" type="button" data-action="plan-pick-usable" ${usable.length ? "" : "disabled"}>选中 ${usable.length} 个连接就绪的 Agent</button></div>
      </div>
      <p class="ms-hint">${icon("info")}${base.provider && base.model
        ? `模型 <span class="mono">${esc(base.model)}</span> 在 ${usable.length} 个 Agent 的连接上可用${stuck.length ? `；<b>${esc(stuck.map(n => PLAN_TARGETS.find(t => t[0] === n)?.[1] || n).join("、"))}</b> 的连接上没有它，会在下面单独标出` : ""}。`
        : "先选供应商与模型；下面每个 Agent 的连接会分别核对，同一个模型名不代表每条连接都有权限。"}</p>
    </div>
    <div class="plan-targets">${PLAN_TARGETS.map(([name, label]) => planTargetRow(name, label)).join("")}</div>
    ${planPicker}
    <div class="table-footer"><span class="ms-foot-note">${icon("shield")}预览会先说明每个目标实际会写什么、跳过什么；密钥不会出现在预览里。</span>
      <div class="actions">
        <button class="small ghost" type="button" data-action="plan-analyze" ${chosen.length ? "" : "disabled"}>只检查不写入</button>
        <button class="small" type="button" data-action="plan-preview" data-write ${chosen.length && base.provider && base.model ? "" : "disabled"}>预览这次切换</button>
        <button class="primary small" type="button" data-action="plan-apply" data-write ${chosen.length && base.provider && base.model ? "" : "disabled"}>应用这次切换</button>
      </div></div>
    <div id="plan-report" class="plan-report">${state.planAnalysis ? `<h3>切换清单</h3>${planIssueList(state.planAnalysis, "error")}${planTargetsReport(state.planAnalysis)}${planIssueList(state.planAnalysis, "warning")}${fallbackPanel(state.planAnalysis.fallback)}` : ""}</div>
    <div id="plan-result" class="plan-result">${state.planPreviewResult ? planResultRows(state.planPreviewResult) : ""}</div>
    ${state.planPreviewResult?.planId ? `<div class="table-footer"><span>预览已生成，尚未写入客户端。确认后只应用本次预览。</span><div class="actions"><button class="small ghost" type="button" data-action="plan-cancel">取消预览</button><button class="primary small" type="button" data-action="plan-confirm-apply" data-write>确认应用并写入客户端</button></div></div>` : ""}
    ${state.planPreviewResult?.transactionId ? `<div class="table-footer"><span>${esc(state.planPreviewResult.disclaimer || "")}</span><div class="actions"><button class="small" type="button" data-action="restore-sync" data-write>恢复这次切换前的配置</button></div></div>` : ""}
  </section>`;
}
function planTargetsReport(analysis) {
  const details = analysis?.details || [];
  if (!details.length) return "";
  const rows = details.map(d => {
    const effortText = d.effort?.support === "unsupported" ? "该客户端没有推理强度参数"
      : d.effort?.requested ? `强度 ${d.effort.requested}`
      : d.effort?.current ? `保留当前强度 ${d.effort.current}` : "不改强度";
    return `<tr><td>${esc(d.label)}</td><td class="mono">${d.provider ? esc(`${d.provider} / ${d.model}`) : "—"}</td><td>${esc(effortText)}</td>
      <td><span class="tag ${d.status === "ok" ? "green" : d.status === "skipped" ? "" : "amber"}">${esc(PLAN_STATUS_LABEL[d.status] || d.status)}</span>${d.reason ? `<br /><small>${esc(d.reason)}</small>` : ""}</td></tr>`;
  }).join("");
  return `<table class="plan-table"><thead><tr><th>Agent</th><th>供应商 / 模型</th><th>推理强度</th><th>本次结果</th></tr></thead><tbody>${rows}</tbody></table>
    <p class="plan-note">${icon("info")}写入原生配置成功不等于正在运行的会话已经换模；${esc(analysis.disclaimer || "")}</p>`;
}
function renderPlanCard() {
  const card = $("#plan-card");
  if (card) card.outerHTML = planCard();
}
async function planAnalyze() {
  const payload = planPayload();
  if (!payload.targets.length) throw new Error("请至少勾选一个参与本次切换的 Agent。");
  const data = await api("POST", "/api/switch-plans/analyze", { plan: payload, targets: payload.targets });
  state.planAnalysis = data;
  renderPlanCard();
  return data;
}
async function planPreview() {
  const payload = planPayload();
  const data = await api("POST", "/api/switch-plans/preview", { plan: payload, targets: payload.targets });
  state.planAnalysis = data.analysis || state.planAnalysis;
  state.planPreviewResult = data.ok ? data : null;
  renderPlanCard();
  if (!data.ok) throw new Error(data.error || "本次方案有未解决的不兼容项，没有写入任何文件。");
  return data;
}
async function planApply() {
  const payload = planPayload();
  const data = await api("POST", "/api/switch-plans/apply", { plan: payload, targets: payload.targets });
  state.planAnalysis = data.analysis || state.planAnalysis;
  state.planPreviewResult = data.planId ? data : (data.ok ? data : null);
  renderPlanCard();
  if (!data.ok) throw new Error(data.error || (data.results || []).map(r => r.message).join("；") || "写入被拒绝。");
  return data;
}
async function savePlan() {
  const payload = planPayload();
  if (!payload.name) throw new Error("请先给方案起一个名字。");
  if (!payload.targets.length) throw new Error("请至少勾选一个参与本次切换的 Agent。");
  if (!payload.base && !Object.keys(payload.agents || {}).length) throw new Error("请先选择主模型，或至少为某个 Agent 单独指定模型。");
  const store = state.plans?.store || { version: 1, default: null, plans: [] };
  const plans = Object.fromEntries((store.plans || []).map(p => [p.id, stripPlanView(p)]));
  const existing = store.plans.find(p => p.name === payload.name);
  const id = existing?.id || planSlug(payload.name, plans);
  plans[id] = payload;
  const data = await api("PUT", "/api/switch-plans", { switchPlans: { version: 1, default: id, plans } });
  state.plans = { ...(state.plans || {}), store: data.store };
  state.planLoaded = { id, name: payload.name };
  state.planLoadedPayload = JSON.parse(JSON.stringify(payload));
  state.planName = payload.name;
  renderPlanCard();
  return existing ? `已更新方案「${payload.name}」` : `已保存方案「${payload.name}」`;
}
function planSlug(name, plans) {
  const base = String(name).trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48) || "plan";
  let id = base, n = 2;
  while (plans[id]) id = `${base}-${n++}`;
  return id;
}
// 只把方案本体写回后端；stale / refs / updatedAt 是后端算出来的展示字段。
function stripPlanView(plan) {
  const out = { name: plan.name, description: plan.description || "", targets: [...(plan.targets || [])] };
  if (plan.base) out.base = { ...plan.base };
  if (plan.agents && Object.keys(plan.agents).length) out.agents = JSON.parse(JSON.stringify(plan.agents));
  if (plan.fallback) out.fallback = JSON.parse(JSON.stringify(plan.fallback));
  return out;
}
async function deletePlan() {
  if (!state.planLoaded) return;
  const data = await api("DELETE", `/api/switch-plans/${encodeURIComponent(state.planLoaded.id)}`, {});
  state.plans = { ...(state.plans || {}), store: data.store };
  state.planLoaded = null;
  state.planLoadedPayload = null;
  state.planDraft = emptyPlanDraft();
  state.planTargets = new Set();
  state.planOverrides = {};
  state.planName = ""; state.planDescription = "";
  state.planAnalysis = null; state.planPreviewResult = null;
  renderPlanCard();
  return data.defaultCleared ? "方案已删除；它原本是默认方案，默认指针也一并清空了。" : "方案已删除。";
}
function loadPlanIntoDraft(plan) {
  state.planDraft = draftFromPlan(plan);
  state.planTargets = new Set(plan.targets || []);
  state.planOverrides = {};
  for (const [name, spec] of Object.entries(plan.agents || {})) {
    if ((spec.policy || "follow") !== "follow") {
      state.planOverrides[name] = { policy: spec.policy || "fixed", provider: spec.provider || "", model: spec.model || "", reasoningEffort: spec.reasoningEffort || "", allowUnknownModel: !!spec.allowUnknownModel };
    }
  }
  state.planName = plan.name; state.planDescription = plan.description || "";
  state.planLoaded = { id: plan.id, name: plan.name };
  state.planLoadedPayload = JSON.parse(JSON.stringify(planPayload()));
  state.planAnalysis = null; state.planPreviewResult = null;
}

function toast(message, type = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${type === "error" ? "error" : ""}`;
  el.innerHTML = `${icon(type === "error" ? "alert" : "check")}<span>${esc(message)}</span><button class="icon-button" aria-label="关闭提示">${icon("x")}</button>`;
  $("button", el).onclick = () => el.remove();
  $("#toasts").append(el);
  while ($$(".toast").length > 2) $(".toast").remove();
  setTimeout(() => el.remove(), type === "error" ? 9000 : 4500);
}

async function api(method, path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120000);
  try {
    const res = await fetch(path, { method, headers: { "Content-Type": "application/json" }, signal: controller.signal, body: body === undefined ? undefined : JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
    if (method !== "GET" && !["/api/preview", "/api/apply"].includes(path)) invalidatePreview();
    return data;
  } catch (e) {
    if (e.name === "AbortError") throw new Error("请求超时。操作可能仍在后台执行，请稍后刷新确认，勿重复提交。");
    if (e instanceof TypeError) throw new Error("无法连接本地服务，请确认 Agent Hub 已启动后重试。");
    throw e;
  } finally { clearTimeout(timeout); }
}

async function busy(label, task) {
  if (state.busy) return;
  state.busy = true;
  document.body.classList.add("busy");
  $("#content").setAttribute("aria-busy", "true");
  const buttons = $$("button[data-write], #refresh, #content input, #content select, #content textarea, #editor-form input, #editor-form select, #editor-form textarea, #editor-form button");
  const disabled = buttons.map(b => b.disabled);
  buttons.forEach(b => { b.disabled = true; });
  $("#save-state").innerHTML = `<span class="spinner"></span>${esc(label)}`;
  try { return await task(); }
  catch (e) {
    if ($("#editor-dialog").open) {
      $("#editor-error").textContent = e.message;
      $("#editor-error").hidden = false;
    } else { toast(e.message, "error"); }
    return null;
  } finally {
    state.busy = false;
    document.body.classList.remove("busy");
    $("#content").removeAttribute("aria-busy");
    buttons.forEach((b, i) => { if (b.isConnected) b.disabled = disabled[i]; });
    renderChrome();
  }
}

async function reload() {
  invalidatePreview();
  const keepDraft = state.loaded && targetDirty();
  const [providers, status] = await Promise.all([api("GET", "/api/providers"), api("GET", "/api/status"), loadModelSync(), loadPlans()]);
  state.providers = providers.providers || {};
  state.status = status;
  if (!keepDraft) resetTargetDraft();
  if (state.filters.provider !== "all" && !state.providers[state.filters.provider]) state.filters.provider = "all";
  const keys = new Set(allModels().map(m => m.key));
  state.selection = new Set([...state.selection].filter(k => keys.has(k)));
  state.loaded = true;
  renderPage();
}

async function refreshAfterWrite(message) {
  try { await reload(); toast(message); }
  catch (e) { toast(`${message}\n列表刷新失败：${e.message} 请点击右上角刷新。`, "error"); }
}

function resetTargetDraft() {
  state.targetDraft = Object.fromEntries(effectiveTargets().map(([n, t]) => [n, t.enabled]));
  for (const name of Object.keys(MODE_CLIENTS)) {
    state.clientMode[name] = state.status?.targets?.[name]?.mode || "official";
    state.clientOptIn[name] = state.clientMode[name] === "third-party";
  }
  invalidatePreview();
  // 多 agent 模型设置的草稿从后端最新数据重建，未保存的改动在此丢弃。
  state.modelSyncDraft = null;
  state.modelSyncDraftKey = "";
  state.modelSyncCustom = {};
  state.discoverAgent = "";
}

function renderChrome() {
  const models = allModels();
  $("#nav-model-count").textContent = models.length;
  $("#nav-provider-count").textContent = Object.keys(state.providers).length;
  $("#nav-sync-dot").hidden = !state.status?.pendingSync;
  $$("[data-page]").forEach(el => { el.classList.toggle("active", el.dataset.page === state.page); if (el.dataset.page === state.page) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); });
  $("#breadcrumb-current").textContent = PAGES[state.page][0];
  document.title = `Agent Hub · ${PAGES[state.page][0]}`;
  if (!state.busy) $("#save-state").innerHTML = state.loaded ? `${icon("check")}本地配置已保存${state.status?.pendingSync ? " · 待同步" : ""}` : "服务未连接";
  $("#footer-sync").textContent = state.status?.lastSync ? `最近同步 ${dateLabel(state.status.lastSync)}` : "配置写入前自动备份";
}

function heading(actions) {
  const [title, description] = PAGES[state.page];
  return `<div class="page-heading"><div><span class="eyebrow">${state.page === "models" ? "MODEL LIBRARY" : state.page === "providers" ? "PROVIDERS" : "PLATFORM SYNC"}</span><h1>${title}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
}
function stats() {
  return `<div class="stats"><div class="stat"><div><div class="stat-label">已管理模型</div><div class="stat-value">${allModels().length}<small>个模型</small></div></div><div class="stat-icon">${icon("grid")}</div></div><div class="stat"><div><div class="stat-label">已连接供应商</div><div class="stat-value">${Object.keys(state.providers).length}<small>个供应商</small></div></div><div class="stat-icon blue">${icon("layers")}</div></div><div class="stat"><div><div class="stat-label">参与同步的平台</div><div class="stat-value">${enabledCount()}<small>个独立目标</small></div></div><div class="stat-icon green">${icon("sync")}</div></div></div>`;
}
function empty(iconName, title, description, action = "") {
  return `<div class="empty-state"><div class="empty-icon">${icon(iconName)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
}

function renderPage() {
  renderChrome();
  if (!state.loaded) return;
  if (state.page === "models") renderModels();
  if (state.page === "providers") renderProviders();
  if (state.page === "sync") renderSync();
}

function renderModels() {
  const agentFilters = [...new Set(allModels().map(m => m.agent))];
  $("#content").innerHTML = heading(`<button data-action="add-provider">${icon("plus")}添加供应商</button><button class="primary" data-action="add-model">${icon("plus")}添加模型</button>`) + stats() + `
    <section class="card" aria-label="模型列表">
      <div class="card-heading"><div><h2>所有模型</h2><p>按供应商与连接（API Key）管理；同一供应商给不同 Agent 配不同 Key 时，模型库也按连接分开。</p></div><button class="ghost small" data-action="go-sync">${icon("sync")}前往同步${icon("arrow")}</button></div>
      <div class="provider-filters" role="group" aria-label="按供应商筛选"><button class="filter-chip ${state.filters.provider === "all" ? "active" : ""}" data-action="filter-provider" data-pid="all" aria-pressed="${state.filters.provider === "all"}">全部供应商<span class="count">${allModels().length}</span></button>${Object.entries(state.providers).map(([pid, p]) => `<button class="filter-chip ${state.filters.provider === pid ? "active" : ""}" data-action="filter-provider" data-pid="${esc(pid)}" aria-pressed="${state.filters.provider === pid}">${esc(providerName(pid))}<span class="count">${(p.models || []).length + Object.values(p.agents || {}).reduce((n, b) => n + ((b && b.models) || []).length, 0)}</span></button>`).join("")}</div>
      <div class="filter-bar"><div class="search-box">${icon("search")}<input id="model-search" type="search" placeholder="搜索模型名称、ID 或供应商…" aria-label="搜索模型" value="${esc(state.filters.query)}" /></div><select id="agent-filter" aria-label="筛选连接"><option value="all">全部连接</option>${agentFilters.map(a => `<option value="${esc(a)}" ${state.filters.agent === a ? "selected" : ""}>${esc(connectionLabel(a))}连接</option>`).join("")}</select><select id="capability-filter" aria-label="筛选模型能力"><option value="all">全部能力</option><option value="reasoning">推理模型</option><option value="image">支持图片</option><option value="text">纯文本</option></select><span class="filter-summary" id="filter-summary"></span></div>
      <div id="bulk-bar" class="bulk-bar" hidden></div>
      <div class="table-wrap"><table class="models-table"><thead><tr><th class="check-cell"><input type="checkbox" id="select-page" aria-label="全选本页模型" /></th><th>模型名称 / ID</th><th>供应商</th><th>连接</th><th>上下文</th><th>最大输出</th><th>能力</th><th>操作</th></tr></thead><tbody id="model-rows"></tbody></table></div>
      <div class="table-footer"><span id="page-info"></span><div class="pagination"><button data-action="prev-page" id="prev-page">上一页</button><span id="page-number"></span><button data-action="next-page" id="next-page">下一页</button></div></div>
    </section><div class="hint-line">${icon("shield")}添加与删除先保存到本地；前往「平台同步」后才会写入客户端。</div>`;
  $("#capability-filter").value = state.filters.capability;
  renderModelRows();
}
function filteredModels() {
  const { query, provider, agent, capability } = state.filters;
  const q = query.trim().toLocaleLowerCase();
  return allModels().filter(m => (provider === "all" || m.pid === provider) && (agent === "all" || m.agent === agent) && (!q || `${m.id} ${m.name} ${providerName(m.pid)} ${m.pid}`.toLocaleLowerCase().includes(q)) && (capability === "all" || capability === "reasoning" && m.reasoning !== false || capability === "image" && (m.input || []).includes("image") || capability === "text" && (m.input || ["text"]).every(v => v === "text")));
}
function visibleModels() {
  const list = filteredModels();
  state.modelPage = Math.min(Math.max(1, state.modelPage), Math.max(1, Math.ceil(list.length / 20)));
  return list.slice((state.modelPage - 1) * 20, state.modelPage * 20);
}
function renderModelRows() {
  const list = filteredModels(), visible = visibleModels();
  $("#filter-summary").textContent = `共 ${list.length} 个模型`;
  $("#model-rows").innerHTML = visible.length ? visible.map(m => `<tr class="${state.selection.has(m.key) ? "selected" : ""}"><td class="check-cell"><input type="checkbox" class="model-check" data-key="${esc(m.key)}" aria-label="选择 ${esc(m.name || m.id)}" ${state.selection.has(m.key) ? "checked" : ""} /></td><td><div class="model-name" title="${esc(m.name || m.id)}">${esc(m.name || m.id)}</div><div class="model-id" title="${esc(m.id)}">${esc(m.id)}</div></td><td><span class="tag ${m.agent ? "purple" : ""}" title="${m.agent ? esc(`来自 ${providerName(m.pid)} 的 ${connectionLabel(m.agent)} 连接（可配独立 Key）`) : "默认连接"}">${connectionLabel(m.agent)}</span></td><td><span class="provider-label" style="--provider-color:${providerColor(m.pid)[1]}"><span class="provider-dot"></span>${esc(m.pid)}</span></td><td><span class="token-value" title="${Number(m.contextWindow || 128000).toLocaleString()} tokens">${tokens(m.contextWindow || 128000)}</span></td><td><span class="token-value" title="${Number(m.maxTokens || 32768).toLocaleString()} tokens">${tokens(m.maxTokens || 32768)}</span></td><td><div class="tags">${m.reasoning !== false ? `<span class="tag purple">${icon("brain")}推理</span>` : ""}${(m.input || ["text"]).map(v => `<span class="tag ${v === "image" ? "blue" : ""}">${v === "image" ? icon("image") : ""}${esc(({ text: "文本", image: "图片", audio: "音频", video: "视频" })[v] || v)}</span>`).join("")}</div></td><td><div class="row-actions"><button class="icon-button" data-action="edit-model" data-pid="${esc(m.pid)}" data-agent="${esc(m.agent)}" data-mid="${esc(m.id)}" title="编辑模型" aria-label="编辑 ${esc(m.id)}">${icon("edit")}</button><button class="icon-button danger" data-action="delete-model" data-write data-pid="${esc(m.pid)}" data-agent="${esc(m.agent)}" data-mid="${esc(m.id)}" title="删除模型" aria-label="删除 ${esc(m.id)}">${icon("trash")}</button></div></td></tr>`).join("") : `<tr><td colspan="8">${empty("grid", allModels().length ? "没有找到匹配的模型" : "从第一个模型开始", allModels().length ? "试试其他关键词，或清除筛选条件。" : "连接供应商后，手动添加或检测远端模型。", allModels().length ? '<button data-action="clear-filters">清除筛选</button>' : `<button class="primary" data-action="${Object.keys(state.providers).length ? "add-model" : "add-provider"}">${icon("plus")}${Object.keys(state.providers).length ? "添加模型" : "添加供应商"}</button>`)}</td></tr>`;
  const start = list.length ? (state.modelPage - 1) * 20 + 1 : 0;
  $("#page-info").textContent = `显示 ${start}–${Math.min(state.modelPage * 20, list.length)}，共 ${list.length} 个模型`;
  $("#page-number").textContent = `${state.modelPage} / ${Math.max(1, Math.ceil(list.length / 20))}`;
  $("#prev-page").disabled = state.modelPage <= 1;
  $("#next-page").disabled = state.modelPage * 20 >= list.length;
  $("#select-page").disabled = !visible.length;
  renderSelection();
}
function renderSelection() {
  const visible = visibleModels();
  const count = visible.filter(m => state.selection.has(m.key)).length;
  $("#select-page").checked = !!visible.length && count === visible.length;
  $("#select-page").indeterminate = count > 0 && count < visible.length;
  const bar = $("#bulk-bar");
  bar.hidden = !state.selection.size;
  bar.innerHTML = `<span>已选择 <strong>${state.selection.size}</strong> 个模型${state.selection.size > count ? `（含其他页或筛选外的 ${state.selection.size - count} 个）` : ""}</span><div class="actions"><button class="ghost small" data-action="clear-selection">取消选择</button><button class="danger small" data-action="bulk-delete" data-write>${icon("trash")}删除选中</button></div>`;
  $$(".model-check").forEach(c => c.closest("tr").classList.toggle("selected", c.checked));
}

function renderProviders() {
  $("#content").innerHTML = heading(`<button data-action="import-client">${icon("download")}从客户端导入</button><button class="primary" data-action="add-provider">${icon("plus")}添加供应商</button>`) + stats() + `<div class="filter-bar" style="padding:0;border:0"><div class="search-box">${icon("search")}<input type="search" id="provider-search" placeholder="搜索供应商名称或 ID…" aria-label="搜索供应商" value="${esc(state.providerQuery)}" /></div><span class="filter-summary">密钥不在列表中展示</span></div><div class="provider-grid" id="provider-cards"></div><div class="hint-line">${icon("info")}模型检测只查询远端列表，不会删除或覆盖已配置的模型。</div>`;
  renderProviderCards();
}
function renderProviderCards() {
  const q = state.providerQuery.trim().toLocaleLowerCase();
  const entries = Object.entries(state.providers).filter(([pid]) => `${pid} ${providerName(pid)}`.toLocaleLowerCase().includes(q));
  $("#provider-cards").innerHTML = entries.map(([pid, p]) => {
    const color = providerColor(pid);
    return `<article class="card provider-card"><div class="provider-card-head"><div class="avatar" style="--avatar-bg:${color[0]};--avatar-color:${color[1]}">${esc(providerName(pid).slice(0, 1).toUpperCase())}</div><div><h3>${esc(providerName(pid))}</h3><div class="provider-id">${esc(pid)}</div></div><span class="tag ${p.enabled !== false ? "green" : ""}">${p.enabled !== false ? "已启用" : "已停用"}</span></div><div class="provider-url">${icon("link")}<span title="${esc(p.baseUrl)}">${esc(p.baseUrl || "未配置端点")}</span></div><div class="provider-meta"><span>${icon("grid")}${allModels().filter(m => m.pid === pid).length} 个模型</span><span>${icon("key")}${p.apiKey ? "密钥已配置" : "未配置密钥"}</span>${AGENT_OVERRIDES.map(([name, label]) => { const c = state.status?.connections?.[pid]?.[name]; if (!c || !c.ready) return ""; const over = c.overridden ? "*" : ""; return `<span class="tag purple" title="${esc(label)} 连接已就绪${over ? "（按 Agent 覆盖）" : ""}">${esc(label)}</span>`; }).join("")}${p.targets?.length ? `<span class="tag" title="仅同步到：${esc(p.targets.filter(t=>AGENT_LABEL[t]).join(", "))}">平台受限</span>` : ""}</div><div class="provider-card-footer"><button class="ghost small" data-action="provider-models" data-pid="${esc(pid)}">查看模型${icon("arrow")}</button><div class="actions"><button class="small" data-action="discover" data-write data-pid="${esc(pid)}">${icon("sparkles")}检测模型</button><button class="small" data-action="edit-provider" data-pid="${esc(pid)}">${icon("edit")}配置</button></div></div></article>`;
  }).join("") + (q && !entries.length ? `<div class="card">${empty("search", "没有找到供应商", "尝试其他关键词。")}</div>` : "") + `<button class="card provider-add-card" data-action="add-provider">${icon("plus")}连接新的供应商<small>OpenAI / Anthropic 兼容服务</small></button>`;
}

function renderSync() {
  const pending = state.status.pendingSync;
  $("#content").innerHTML = heading(`<button data-action="detect-sync" data-write>${icon("refresh")}检测变更并预览</button><button class="primary" data-action="run-sync" data-write id="sync-primary">${icon("sync")}预览已选平台</button>`) + `
    <div class="notice ${pending ? "warn" : ""}">${icon(pending ? "clock" : "shield")}<div><strong>${pending ? "本地配置有更新，等待同步到平台" : "本地配置已同步到所有启用平台"}</strong><br />${pending ? "以 Hub 为准写入已选平台；未选平台保持不变。" : "你可以再次同步，或先检测客户端配置是否有变化。"} 写入前会备份原配置。</div></div>
    <section id="sync-preview" class="card sync-preview" aria-live="polite" hidden></section>
    ${planCard()}
    <section class="card modelsync-card" id="modelsync-card" aria-label="多 agent 使用的模型（精细设置）"></section>
    <div class="sync-layout"><section class="card"><div class="card-heading"><div><h2>同步到哪些平台？</h2><p>dsh 桌面版与 CLI 联动；官方授权不在管理范围内。</p></div><span class="tag" id="target-count"></span></div><div class="target-grid">${Object.entries(state.status.targets || {}).filter(([name]) => AGENT_LABEL[name]).map(([name, t]) => {
      const [title, short, desc] = TARGETS[name] || [name, name.slice(0, 2), t.notes || "模型配置"];
      const readonly = !!t.aliasOf || !t.supported;
      const enabled = t.aliasOf ? state.targetDraft[t.aliasOf] : state.targetDraft[name];
      const existing = (t.files || []).filter(f => f.exists).length;
      return `<label class="target-card ${enabled ? "enabled" : ""} ${readonly ? "readonly" : ""}" data-target="${esc(name)}"><div class="target-top"><span class="target-symbol">${esc(short)}</span><h3>${esc(title)}</h3><input type="checkbox" class="target-check" data-target="${esc(name)}" aria-label="同步到 ${esc(title)}" ${enabled ? "checked" : ""} ${readonly ? "disabled" : ""} /></div><div class="target-desc">${esc(desc)}</div><div class="target-state">${icon(readonly ? "link" : "shield")}${t.aliasOf ? `跟随 ${esc(t.aliasOf)}` : !t.supported ? "不自动写入" : `${existing} / ${t.files?.length || 0} 个配置文件已存在`}</div></label>`;
    }).join("")}</div><div class="table-footer"><span id="target-draft-status"></span><div class="actions"><button class="small ghost" data-action="reset-targets" id="reset-targets">撤销更改</button><button class="small" data-action="save-targets" data-write id="save-targets">保存平台设置</button></div></div></section>
    <section class="card"><div class="card-heading"><div><h2>最近同步结果</h2><p>${state.status.lastSync ? `执行于 ${esc(dateLabel(state.status.lastSync))}` : "同步后，这里会显示每个平台的执行结果。"}</p></div>${state.status.lastTransaction ? `<button class="small" data-action="restore-sync" data-write>恢复上次同步</button>` : state.status.lastSync ? `<span class="tag">${icon("clock")}最近一次</span>` : ""}</div><div class="sync-results" id="sync-results">${renderResults(state.status.lastResults || [])}</div></section></div>
    <details class="diagnostics"><summary>查看配置路径与诊断信息</summary><pre>${esc(state.status.configPath)}\n\n${esc(state.status.statusText)}${state.status.changedFiles?.length ? `\n\n检测到外部变更：\n${esc(state.status.changedFiles.join("\n"))}` : ""}</pre></details>`;
  updateTargetDraftUI();
  renderModelSyncCard();
  renderPreview();
}
// 把某个作用域（default / clients / tiers）的模型设置写进草稿；空字符串表示删除该字段。
function setModelEntry(scope, target, tier, patch) {
  invalidatePreview();
  const draft = ensureModelSyncDraft();
  // 必须先落到草稿里的真实对象再改，不能对中间对象整体赋值，否则 tiers 下的修改会写丢。
  let entry = draft.default;
  let tiers = null;
  if (scope === "tiers") {
    const claude = (draft.overrides.claude ||= {});
    tiers = (claude.tiers ||= {});
    entry = (tiers[tier] ||= {});
  } else if (scope === "clients") {
    entry = (draft.overrides[target] ||= {});
  }
  for (const [key, value] of Object.entries(patch)) {
    if (value) entry[key] = value;
    else delete entry[key];
  }
  // 客户端 override 只有显式点「跟随全局」才移除；档位则直接跟着字段收回，不留空对象。
  if (scope === "tiers") {
    const claude = draft.overrides.claude;
    if (!Object.keys(entry).length) delete tiers[tier];
    if (!Object.keys(claude.tiers).length) delete claude.tiers;
    if (!Object.keys(claude).length) delete draft.overrides.claude;
  }
}
// 每个选择器带一个「手动填写」哨兵值；state.modelSyncCustom 记住哪些行处于手填模式。
const CUSTOM_MODEL = "__custom__";
const customKey = (scope, target, tier) => `${scope}:${target}:${tier || ""}`;
function modelSelect(scope, target, tier, value, provider) {
  const custom = state.modelSyncCustom || {};
  // 没有显式值时显示后端解析出来的模型（父行 / 供应商级），让界面始终反映"现在会用什么"。
  const explicit = value || "";
  const resolved = tier ? claudeTiersView()[tier] : modelSyncResolved(target);
  const resolvedModel = resolved?.provider === provider ? resolved.model || "" : "";
  const current = explicit || resolvedModel;
  const manual = !!custom[customKey(scope, target, tier)];
  const typed = manual ? (custom[customKey(scope, target, tier)] === true ? "" : String(custom[customKey(scope, target, tier)])) : "";
  const choices = modelChoices(provider, target === "default" ? null : target);
  // 已选中的值即使不在候选里也要能看见，否则换供应商后当前值会消失。
  if (current && !choices.includes(current)) choices.push(current);
  if (typed && !choices.includes(typed)) choices.push(typed);
  const id = `ms-model-${scope}-${target}${tier ? `-${tier}` : ""}`;
  // 显式选中当前值；解析出来的"当前值"只做标记，不写进草稿。
  const label = v => esc(v === current ? `${v}${explicit ? "" : "（当前）"}` : v);
  const options = `<option value="">${scope === "default" ? "请选择模型" : "跟随全局"}</option>${choices.map(v => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${label(v)}</option>`).join("")}<option value="${CUSTOM_MODEL}" ${manual ? "selected" : ""}>手动填写模型 ID…</option>`;
  const select = provider
    ? `<select id="${esc(id)}" data-ms-select data-scope="${esc(scope)}" data-target="${esc(target)}" ${tier ? `data-tier="${esc(tier)}"` : ""} aria-label="模型">${options}</select>`
    : `<select id="${esc(id)}" disabled aria-label="模型"><option value="">先选择供应商</option></select>`;
  return `<div class="model-picker">${select}<input class="mono model-custom" id="${esc(`${id}-custom`)}" data-ms-custom data-scope="${esc(scope)}" data-target="${esc(target)}" ${tier ? `data-tier="${esc(tier)}"` : ""} placeholder="手填模型 ID" aria-label="手填模型 ID" value="${esc(typed)}" ${manual ? "" : "hidden"} /></div>`;
}
// 全局主模型的一行：供应商下拉 + 模型下拉（手填走哨兵值）。
function modelRow(scope, target, tier, entry, resolvedEntry, provider, label = "模型") {
  const value = entry.model || "";
  const provider2 = provider !== undefined ? provider : (entry.provider || resolvedEntry?.provider || state.modelSyncDraft?.default?.provider || "");
  const hint = entry.model ? "已指定" : resolvedEntry?.model ? `当前：${resolvedEntry.model}` : "尚未确定";
  return `<div class="ms-field"><span>${esc(label)} <small class="ms-state">${esc(hint)}</small></span>${modelSelect(scope, target, tier, value, provider2)}</div>`;
}
const effortLabel = value => {
  if (!value) return "";
  if (value === "off" || value === "none") return "关闭推理";
  const found = { minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高", ultra: "极限" }[value];
  return found ? `${value}（${found}）` : value;
};
// 客户端行显示用的紧凑文本：pi/deepseek-flash · 强度 high
function modelSummary(entry) {
  if (!entry || !entry.model) return "";
  const head = [entry.provider, entry.model].filter(Boolean).join(" / ");
  return entry.reasoningEffort ? `${head} · ${entry.reasoningEffort}` : head;
}
function claudeTierRow(tier, label) {
  const entry = draftEntry("tiers", "claude", tier);
  const resolved = claudeTiersView()[tier] || null;
  const overridden = !!entry.model;
  const needModel = !!entry.provider && !overridden;
  const provider = entry.provider || resolved?.provider || draftEntry("clients", "claude").provider || state.modelSyncDraft?.default?.provider || "";
  // 未显式指定的档位不写入；这里如实显示后端解析出来的值。
  const stateText = needModel ? "请选择模型" : overridden ? "单独指定" : resolved?.model ? `跟随供应商：${modelSummary(resolved)}` : "未设置";
  return `<div class="ms-tier">
    <div class="ms-tier-head"><strong>${esc(label)}</strong><span class="tag ${needModel || overridden ? "amber" : ""}">${esc(stateText)}</span></div>
    <div class="ms-tier-row">
      ${providerSelect("tiers", "claude", tier, provider)}
      ${modelSelect("tiers", "claude", tier, entry.model, provider)}
      <button class="small ghost" type="button" data-action="ms-follow-global" data-scope="tiers" data-target="claude" data-tier="${esc(tier)}" ${overridden || entry.provider ? "" : "disabled"}>使用默认档位</button>
    </div>
  </div>`;
}
function providerSelect(scope, target, tier, value) {
  // 只列出该 Agent 连接就绣的供应商：Claude Code 需要 Anthropic 端点，
  // Codex 第三方只认 Responses；未配置连接的供应商不会出现在下拉里。
  const canonical = target === "default" ? null : (target === "tiers" ? "claude" : target);
  const capable = canonical ? (state.status?.agentCapable?.[canonical] || []) : null;
  const ids = enabledProviders().filter(pid => {
    if (capable && !capable.includes(pid)) return false;
    return !state.providers[pid].targets?.length || state.providers[pid].targets.includes(canonical || "pi");
  });
  const unavailable = value && !ids.includes(value);
  const id = `ms-provider-${scope}-${target}${tier ? `-${tier}` : ""}`;
  return `<select id="${esc(id)}" data-ms-provider data-scope="${esc(scope)}" data-target="${esc(target)}" ${tier ? `data-tier="${esc(tier)}"` : ""} aria-label="供应商"><option value="">跟随全局</option>${unavailable ? `<option value="${esc(value)}" selected disabled>${esc(providerName(value))}（当前不可用）</option>` : ""}${ids.map(pid => `<option value="${esc(pid)}" ${pid === value ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select>`;
}
function clientRow(name, label) {
  const draft = ensureModelSyncDraft();
  const entry = draft.overrides?.[name] || {};
  const resolved = modelSyncResolved(name);
  const codex = name === "codex";
  const modeful = MODE_CLIENTS[name];
  const mode = modeful ? `<div class="codex-mode"><label class="field"><span>${esc(modeful.lineLabel)}</span><select id="${name}-mode" aria-label="${esc(modeful.lineLabel)}" ${state.status?.targets?.[name] ? "" : "disabled"}><option value="official" ${state.clientMode[name] === "official" ? "selected" : ""}>${esc(modeful.officialOption)}</option><option value="third-party" ${state.clientMode[name] === "third-party" ? "selected" : ""}>${esc(modeful.thirdPartyOption)}</option></select></label>${state.clientMode[name] === "third-party" ? `<label class="check-label codex-optin"><input id="${name}-optin" type="checkbox" ${state.clientOptIn[name] ? "checked" : ""} />${esc(modeful.optInText)}</label>` : `<p class="ms-client-note">${esc(modeful.officialNote)}</p>`}</div>` : "";
  if (modeful && state.clientMode[name] !== "third-party") {
    return `<div class="ms-client" data-client="${esc(name)}"><div class="ms-client-head"><strong>${esc(modeful.label)}</strong><span class="tag green">${esc(modeful.officialTag)}</span></div>${mode}<p class="ms-client-note">${esc(modeful.officialKeepNote)}${state.status?.targets?.[name] ? `勾选 ${esc(modeful.label)} 平台后预览并应用，设置才会生效。` : `当前未配置 ${esc(modeful.label)} 同步目标。`}</p></div>`;
  }
  const overridden = !!(entry.model || entry.provider || entry.reasoningEffort);
  const body = overridden
    ? `<div class="ms-row">${providerSelect("clients", name, "", entry.provider || "")}${modelSelect("clients", name, "", entry.model, entry.provider || resolved?.provider || draft.default?.provider || "")}${codex ? `<label class="ms-field"><span>推理强度</span><select data-ms-effort data-scope="clients" data-target="codex" aria-label="Codex 推理强度"><option value="">供应商默认（移除覆盖）</option>${CODEX_EFFORTS.map(v => `<option value="${v}" ${v === entry.reasoningEffort ? "selected" : ""}>${esc(effortLabel(v))}</option>`).join("")}</select></label>` : ""}<button class="small ghost" type="button" data-action="ms-follow-global" data-scope="clients" data-target="${esc(name)}">跟随全局</button></div>`
    : `<div class="ms-client-body"><span class="ms-resolved">当前：${esc(modelSummary(resolved) || "尚未确定；跟随全局主模型")}</span><button class="small ghost" type="button" data-action="ms-override" data-target="${esc(name)}">单独指定</button></div>`;
  const needModel = !!entry.provider && !entry.model;
  return `<div class="ms-client ${overridden ? "overridden" : ""}" data-client="${esc(name)}">
    <div class="ms-client-head"><strong>${esc(label)}</strong>${needModel ? `<span class="tag amber">请选择模型</span>` : overridden ? `<span class="tag amber">单独指定</span>` : `<span class="tag green">跟随全局</span>`}</div>
    ${mode}${body}
  </div>`;
}
function claudeRow() {
  const draft = ensureModelSyncDraft();
  const entry = draft.overrides?.claude || {};
  const tierCount = Object.keys(entry.tiers || {}).length;
  const overridden = !!entry.model;
  const resolved = modelSyncResolved("claude");
  const body = overridden
    ? `<div class="ms-row">${providerSelect("clients", "claude", "", entry.provider || "")}${modelSelect("clients", "claude", "", entry.model, entry.provider || resolved?.provider || draft.default?.provider || "")}<button class="small ghost" type="button" data-action="ms-follow-global" data-scope="clients" data-target="claude">跟随全局</button></div>`
    : `<div class="ms-client-body"><span class="ms-resolved">当前：${esc(modelSummary(resolved) || "尚未确定；跟随全局主模型")}</span><button class="small ghost" type="button" data-action="ms-override" data-target="claude">单独指定</button></div>`;
  // Claude Code 与其它 Agent 一样只认 modelSync；全局主模型的供应商需要配置了
  // Claude 连接（Anthropic 端点）才会跟随，否则保持现状。
  const follows = !!resolved?.model && resolved.provider === draft.default?.provider && resolved.model === draft.default?.model;
  return `<div class="ms-client ${overridden ? "overridden" : ""}" data-client="claude">
    <div class="ms-client-head"><strong>Claude Code</strong>${overridden ? `<span class="tag amber">单独指定</span>` : follows ? `<span class="tag green">跟随全局</span>` : `<span class="tag">独立设置</span>`}${tierCount ? `<span class="tag purple">${tierCount} 个档位单独指定</span>` : ""}</div>
    ${body}
    <p class="ms-client-note">${icon("info")}Claude Code 与其它 Agent 一样只认 modelSync：全局主模型的供应商需要配置 Anthropic 连接（供应商配置里的「按 Agent 覆盖」）才会跟随，否则保持现状。</p>
    <details class="ms-tiers" data-ms-details="claude" ${state.modelSyncOpen.has("claude") || tierCount ? "open" : ""}>
      <summary>展开档位设置（model / opus / sonnet / haiku / subagent）</summary>
      <p class="ms-tier-note">${icon("info")}Claude Code 的档位分别对应 ANTHROPIC_MODEL 与 ANTHROPIC_DEFAULT_*_MODEL；只有在这里显式指定过的档位才会写入，未指定的档位不会被顺手改掉。</p>
      <div class="ms-tier-list">${CLAUDE_TIERS.map(([tier, label]) => claudeTierRow(tier, label)).join("")}</div>
    </details>
  </div>`;
}
function modelSyncCard() {
  const warn = state.modelSyncError;
  const draft = ensureModelSyncDraft();
  const providers = enabledProviders();
  const defaultProvider = draft.default?.provider || "";
  const defaultModel = draft.default?.model || "";
  const dirty = modelSyncDirty();
  const header = `<div class="card-heading"><div><h2>多 agent 使用的模型</h2><p>一份设置，对齐 pi、dsh（含桌面版）、Claude Code 与显式选择第三方线路的 Codex；改完保存，预览后再应用。</p></div><span class="tag ${dirty ? "amber" : ""}" id="modelsync-state">${dirty ? "有未保存修改" : "已保存到本地"}</span></div>`;
  if (warn) {
    return `${header}<div class="notice warn" id="modelsync-warning">${icon("alert")}<div><strong>模型设置接口不可用</strong><br />${esc(warn)}<br />下面的选择会先留在页面上，但无法保存或读取解析结果；确认服务已更新后点右侧按钮重试。</div><div class="actions"><button class="small" type="button" data-action="reload-modelsync">${icon("refresh")}重新读取</button></div></div><div class="ms-global"><div class="ms-global-note">${icon("info")}<div>接口 <span class="mono">GET /api/modelsync</span> 暂时不可用，解析状态无法展示；供应商与模型下拉仍可正常使用。</div></div></div><div class="table-footer"><span class="ms-foot-note">${icon("info")}保存只写入 Hub 配置；客户端文件需先预览并确认应用。</span><div class="actions"><button class="small ghost" type="button" data-action="ms-reset" ${dirty ? "" : "disabled"}>撤销更改</button><button class="small" type="button" data-action="save-modelsync" data-write disabled>保存模型设置</button></div></div>`;
  }
  const degraded = !state.modelSync;
  return `${header}
    ${degraded ? `<div class="notice warn">${icon("alert")}<div>没有读到模型解析结果，可能是本地服务尚未提供该接口。供应商与模型下拉仍可用，但「解析结果」会显示为空。</div></div>` : ""}
    <div class="ms-global">
      ${!providers.length ? `<div class="notice">${icon("info")}<div>还没有启用的供应商，先到「供应商」页连接一个。</div></div>` : ""}
      <div class="ms-row">
        <label class="ms-field"><span>全局主模型 · 供应商</span><select id="ms-default-provider" data-ms-provider data-scope="default" data-target="default" aria-label="全局主模型供应商"><option value="">请选择供应商</option>${providers.map(pid => `<option value="${esc(pid)}" ${pid === defaultProvider ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select></label>
        ${modelRow("default", "default", "", draft.default || {}, null, defaultProvider, "全局主模型 · 模型")}
        <label class="ms-field"><span>推理强度（可选）</span><select id="ms-default-effort" data-ms-effort data-scope="default" data-target="default" aria-label="推理强度"><option value="">不改客户端现有值</option>${EFFORTS.map(v => `<option value="${esc(v)}" ${v === draft.default?.reasoningEffort ? "selected" : ""}>${esc(effortLabel(v))}</option>`).join("")}</select></label>
      </div>
      <p class="ms-hint">留空时保留客户端现有强度；Codex 官方模式恢复之前保存的值。</p>
    </div>
    <div class="ms-clients">
      ${clientRow("pi", "pi")}
      ${clientRow("dsh", "dsh（含桌面版）")}
      ${claudeRow()}
      ${clientRow("codex", "Codex")}
    </div>
    <div class="table-footer"><span class="ms-foot-note">${icon("info")}保存只写入 Hub 配置；「保存平台设置」也会先落盘这里的改动，写入客户端需先预览并确认应用。</span><div class="actions"><button class="small ghost" type="button" data-action="ms-reset" ${dirty ? "" : "disabled"}>撤销更改</button><button class="small" type="button" data-action="save-modelsync" data-write ${dirty ? "" : "disabled"}>保存模型设置</button></div></div>`;
}
function renderResults(results) {
  return results.length ? results.map(r => `<div class="result-row ${r.ok ? "" : "error"}"><div class="result-symbol">${icon(r.ok ? "check" : "x")}</div><div><strong>${esc(TARGETS[r.target]?.[0] || r.target)}</strong><p>${esc(r.message)}</p></div><span class="tag ${r.ok ? "green" : "amber"}" style="margin-left:auto">${r.ok ? "成功" : "失败"}</span></div>`).join("") : empty("sync", "还没有同步记录", "选择平台，开始第一次同步。");
}
function updateTargetDraftUI() {
  const dirty = targetDirty();
  const selected = Object.values(state.targetDraft).filter(Boolean).length;
  $("#target-count").textContent = `已选 ${selected} 个独立目标`;
  $("#target-draft-status").textContent = dirty ? "平台设置尚未保存；生成预览时会先保存到 Hub。" : "平台设置已保存，修改不会立即触发同步。";
  $("#save-targets").disabled = !dirty;
  $("#reset-targets").disabled = !dirty;
  $("#sync-primary").disabled = !selected;
  $$(".target-card").forEach(el => {
    const name = el.dataset.target, alias = state.status.targets[name].aliasOf;
    const checked = !!state.targetDraft[alias || name];
    $("input", el).checked = checked;
    el.classList.toggle("enabled", checked);
  });
}
// 多 agent 模型卡片自身的状态：脏标记、按钮可用性与重绘（不重建用户的输入焦点）。
function updateModelSyncUI() {
  const dirty = modelSyncDirty();
  const stateTag = $("#modelsync-state");
  if (stateTag) { stateTag.textContent = dirty ? "有未保存修改" : "已保存到本地"; stateTag.className = `tag ${dirty ? "amber" : ""}`; }
  $$('#modelsync-card [data-action="save-modelsync"], #modelsync-card [data-action="ms-reset"]').forEach(b => {
    if (b.dataset.action === "save-modelsync") b.disabled = !dirty || !!state.modelSyncError;
    else b.disabled = !dirty;
  });
}
async function saveTargets() {
  if (!targetDirty() && !modelSyncDirty()) return;
  const unconfirmed = Object.keys(MODE_CLIENTS).filter(name => state.clientMode[name] === "third-party" && !state.clientOptIn[name]);
  if (unconfirmed.length) { const m = MODE_CLIENTS[unconfirmed[0]]; throw new Error(`请先勾选 ${m.label} 第三方${m.noun}确认，或改回官方${m.noun}。`); }
  // 平台设置与模型设置写在不同接口；这里顺手把未保存的模型设置一起落盘，避免随后的 reload 丢掉它。
  if (modelSyncDirty() && !state.modelSyncError) await saveModelSync();
  const updates = Object.fromEntries(effectiveTargets().map(([name]) => [name, { enabled: state.targetDraft[name] }]));
  for (const name of Object.keys(MODE_CLIENTS)) if (updates[name]) updates[name].mode = state.clientMode[name];
  await api("PUT", "/api/targets", { targets: updates });
  // 先更新本地基线，reload 就不会误保留已经保存的草稖。
  for (const [name, value] of Object.entries(updates)) Object.assign(state.status.targets[name], value);
}
function invalidatePreview() {
  state.preview = null;
  state.previewError = "";
  const card = $("#sync-preview");
  if (card) { card.hidden = true; card.innerHTML = ""; }
}
function renderPreview() {
  const card = $("#sync-preview");
  if (!card) return;
  const plan = state.preview;
  card.hidden = !plan && !state.previewError;
  if (card.hidden) return;
  if (state.previewError) {
    card.innerHTML = `<div class="notice warn"><div>${icon("alert")}<strong>同步未完成</strong><p>${esc(state.previewError)}</p><p>请检查设置后重新生成预览；没有有效预览时不会应用。</p></div></div>`;
    return;
  }
  const actions = { create: "新建", update: "更新", delete: "删除", unchanged: "不变" };
  card.innerHTML = `<div class="card-heading"><div><h2>同步预览 · 尚未写入客户端</h2><p>目标：${(plan.targets || []).map(name => esc(TARGETS[name]?.[0] || name)).join("、")}。确认后仅应用本次预览；文件变化时会拒绝过期预览。</p></div><span class="tag">${plan.files.length} 个文件</span></div><ul class="preview-files">${plan.files.map(f => `<li><span class="tag">${esc(actions[f.action] || f.action)}</span><span class="mono">${esc(f.path)}</span></li>`).join("") || "<li>没有需要修改的客户端文件</li>"}</ul>${plan.warnings?.length ? `<div class="preview-warnings">${plan.warnings.map(w => `<p>${icon("alert")}${esc(w)}</p>`).join("")}</div>` : ""}<div class="table-footer"><span>仅显示路径与操作；密钥和配置内容不在预览中展示</span><div class="actions"><button class="small" data-action="cancel-preview">取消</button><button class="primary small" data-action="apply-preview" data-write>确认应用预览</button></div></div>`;
}
async function runSync(detect = false) {
  const selected = Object.entries(state.targetDraft).filter(([, enabled]) => enabled).map(([name]) => name);
  if (!selected.length) { toast("请至少选择一个同步平台", "error"); return; }
  const unconfirmedSync = Object.keys(MODE_CLIENTS).filter(name => state.clientMode[name] === "third-party" && !state.clientOptIn[name]);
  if (unconfirmedSync.length) { const m = MODE_CLIENTS[unconfirmedSync[0]]; toast(`请先勾选 ${m.label} 第三方${m.noun}确认，或改回官方${m.noun}。`, "error"); $(`#${unconfirmedSync[0]}-optin`)?.focus(); return; }
  if (detect && !await confirmDialog("检测并预览客户端变更？", "这不是从客户端导入配置。仍将以 Hub 配置为准生成预览，确认应用后才会写入已选平台。", [], "生成预览", false)) return;
  await busy("正在生成同步预览…", async () => {
    invalidatePreview();
    try {
      await saveTargets();
      await reload();
      state.preview = await api("POST", "/api/preview", { targets: selected });
      if (!state.preview.planId || !Array.isArray(state.preview.files)) throw new Error("服务返回的预览不完整，请更新服务后重试。");
      renderPreview();
      $("#sync-preview")?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) {
      state.preview = null;
      state.previewError = e.message;
      renderPreview();
      throw e;
    }
  });
}
async function applyPreview() {
  if (!state.preview?.planId) return;
  const planId = state.preview.planId;
  await busy("正在应用已确认的预览…", async () => {
    try {
      const result = await api("POST", "/api/apply", { planId });
      invalidatePreview();
      state.status.lastResults = result.details || [];
      try { await reload(); }
      catch (e) { $("#sync-results").innerHTML = renderResults(result.details || []); toast(`同步已执行，但状态刷新失败：${e.message}`, "error"); }
      const failed = (result.details || []).filter(d => !d.ok).length;
      toast(result.ok === false || failed ? "同步未全部完成，请查看结果后重新预览。" : `已同步 ${result.details?.length || 0} 个平台，重启客户端后生效。`, result.ok === false || failed ? "error" : "ok");
    } catch (e) {
      state.preview = null;
      state.previewError = e.message;
      renderPreview();
      throw e;
    }
  });
}
async function restoreSync() {
  const transactionId = state.status?.lastTransaction;
  if (!transactionId || !await confirmDialog("恢复上次同步前的配置？", "恢复该次同步涉及的文件。若这些文件在同步后被其他程序修改，将拒绝恢复，避免覆盖新改动。", [], "恢复配置", false)) return;
  await busy("正在恢复配置…", async () => {
    await api("POST", "/api/restore", { transactionId });
    await refreshAfterWrite("已恢复同步前的配置；再次同步前请重新预览。");
  });
}

// 原生 dialog 提供焦点约束与键盘支持；表单修改在关闭前进行确认。
function showEditor(type, title, body, footer, context = {}) {
  state.editor = { type, ...context };
  $("#editor-title").textContent = title;
  $("#editor-eyebrow").textContent = type === "read-only" ? "只读详情" : type === "discover" ? "MODEL DISCOVERY" : type === "provider" ? "PROVIDER SETTINGS" : "MODEL SETTINGS";
  $("#editor-body").innerHTML = body;
  $("#editor-footer").innerHTML = footer;
  $("#editor-error").hidden = true;
  state.editorSnapshot = formSnapshot();
  $("#editor-dialog").showModal();
}
function closeEditor() {
  $("#editor-dialog").close();
  state.editor = null;
  state.discovered = [];
}
async function requestCloseEditor() {
  if (state.busy) return;
  if (editorDirty() && !await confirmDialog("放弃未保存的修改？", "这次编辑尚未保存。返回编辑可以继续修改。", [], "放弃修改", false)) return;
  closeEditor();
}
function formFooter(label, extra = "") {
  return `${extra}<button type="button" data-action="close-editor">取消</button><button type="submit" class="primary">${icon("check")}${label}</button>`;
}
function providerTargetsFromForm(fd) {
  if (fd.has("allTargets")) return [];
  const targets = fd.getAll("targets");
  if (!targets.length) throw new Error("请至少选择一个同步平台，或明确选择所有平台。");
  return targets;
}
function openProvider(pid) {
  if (state.busy) return;
  const p = state.providers[pid] || {}, edit = !!pid;
  const protocols = ["openai-completions", "openai-responses", "openai-chat-completions", "anthropic-messages"];
  const allTargets = edit && !p.targets?.length;
  const selectedTargets = edit ? (p.targets || []) : DEFAULT_PROVIDER_TARGETS;
  const agents = p.agents || {};
  // 按 Agent 覆盖：端点 / 密钥 / 协议 / 模型都可以只给某个 Agent 配。
  // 同一容网关给 Claude 与 Codex 发不同 key 时，各自的模型集合也不同。
  const agentSections = AGENT_OVERRIDES.map(([name, label, hint]) => {
    const block = agents[name] || {};
    const allowed = AGENT_APIS[name];
    const apiOptions = allowed || protocols;
    const ready = state.status?.connections?.[pid]?.[name]?.ready;
    const models = block.models || (name in agents ? [] : p.models) || [];
    return `<details class="form-section" data-agent-section="${esc(name)}"><summary>${esc(label)} 连接${ready ? " · 已就绪" : ""} <small>${esc(hint)}</small></summary><div class="form-grid">
      <label class="field full"><span>${esc(label)} Base URL</span><input name="agent-${esc(name)}-baseUrl" type="url" value="${esc(block.baseUrl || "")}" placeholder="留空则使用默认连接" /><small>留空：该 Agent 直接用顶层默认连接。</small></label>
      <label class="field"><span>${esc(label)} API Key</span><div class="password-wrap"><input name="agent-${esc(name)}-apiKey" type="password" value="${esc(block.apiKey || "")}" autocomplete="off" /><button class="icon-button" type="button" data-action="toggle-key" aria-label="显示密钥" title="显示密钥">${icon("eye")}</button></div></label>
      ${name === "codex" ? `<label class="field"><span>API Key 环境变量名</span><input name="agent-${esc(name)}-apiKeyEnv" value="${esc(block.apiKeyEnv || "")}" pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder="例如 VENDOR_API_KEY" autocomplete="off" /><small>只保存变量名</small></label>` : ""}
      <label class="field"><span>API 协议</span><select name="agent-${esc(name)}-api"><option value="">默认（${esc((allowed || [p.api || protocols[0]])[0])}）</option>${apiOptions.map(v => `<option value="${esc(v)}" ${v === block.api ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>
      <label class="field full"><span>${esc(label)} 模型</span><input value="${models.length} 个模型（${models.slice(0, 3).map(m => m.id).join("、")}${models.length > 3 ? " …" : ""}）" readonly /><small>模型按连接存放；在「检测模型」里选择该连接即可拉取它独有的模型列表。</small></label>
      ${name === "claude" ? `<label class="field full"><span>额外环境变量 · env</span><textarea name="agent-claude-env" rows="3" class="mono" spellcheck="false" placeholder='{"ENABLE_TOOL_SEARCH":"true"}'>${esc(jsonText(block.env))}</textarea><small>JSON 对象；原样写入 settings.json 的 env。</small></label><label class="field full"><span>顶层 settings 字段 · settings</span><textarea name="agent-claude-settings" rows="4" class="mono" spellcheck="false" placeholder='{"effortLevel":"high"}'>${esc(jsonText(block.settings))}</textarea><small>JSON 对象；只收回 Hub 写过的键，保留你手工配置的字段。</small></label>` : ""}
    </div></details>`;
  }).join("");
  showEditor("provider", edit ? "供应商连接配置" : "连接新的供应商", `
    <p class="form-intro">${edit ? "修改连接信息不会覆盖已有模型列表。" : "一个 Coding Plan 可作为独立供应商添加，模型可稍后手填或检测。"}</p>
    <div class="form-grid">
      <label class="field"><span>供应商 ID <b class="required">*</b></span><input name="id" value="${esc(pid || "")}" placeholder="例如 my_plan" pattern="[a-zA-Z0-9][a-zA-Z0-9_\\-]*" required ${edit ? "readonly" : ""} /><small>用于各客户端的配置映射</small></label>
      <label class="field"><span>显示名称 <b class="required">*</b></span><input name="displayName" value="${esc(p.displayName || p.name || "")}" placeholder="例如 My Coding Plan" required /></label>
      <label class="field full"><span>默认连接 Base URL</span><input name="baseUrl" type="url" value="${esc(p.baseUrl || "")}" placeholder="https://api.example.com/v1" ${p.anthropicBaseUrl || AGENT_OVERRIDES.some(([name]) => agents[name]?.baseUrl) ? "" : "required"} /><small>默认服务端点；只给一个 Agent 用时可留空，改在下面的对应连接里。</small></label>
      <label class="field full"><span>默认 API Key</span><div class="password-wrap"><input name="apiKey" type="password" value="${esc(p.apiKey || "")}" placeholder="输入 API 密钥；或在下方为单个 Agent 配置专用 Key" autocomplete="off" /><button class="icon-button" type="button" data-action="toggle-key" aria-label="显示密钥" title="显示密钥">${icon("eye")}</button></div><small>保存在本机 providers.json 中，请勿共享配置文件。</small></label>
      <label class="field full"><span>默认 API 协议</span><select name="api">${protocols.map(v => `<option value="${v}" ${v === (p.api || protocols[0]) ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      ${edit ? `<label class="check-label full"><input name="enabled" type="checkbox" ${p.enabled !== false ? "checked" : ""} />启用供应商，供平台同步使用</label>` : ""}
    </div>
    <details class="form-section"><summary>高级设置 · 备注与目标平台</summary><div class="form-grid">
      ${edit ? `<label class="field full"><span>内部名称</span><input name="name" value="${esc(p.name || pid)}" required /></label>` : ""}
      <label class="field full"><span>备注</span><input name="notes" value="${esc(p.notes || "")}" placeholder="可选，例如套餐或网关说明" /></label>
      <div class="field full"><span>同步到哪些平台</span><label class="check-label"><input type="checkbox" name="allTargets" ${allTargets ? "checked" : ""} />四个受支持 Agent（按连接兼容性）</label><div class="check-group">${PROVIDER_TARGETS.map(([name, label]) => `<label class="check-label"><input type="checkbox" name="targets" value="${esc(name)}" ${selectedTargets.includes(name) ? "checked" : ""} ${allTargets ? "disabled" : ""} />${esc(label)}</label>`).join("")}</div><small>未配置连接的平台不会写入；新供应商默认选择常用 Agent。</small></div>
      <label class="field full"><span>agent 默认模型 · agentModel</span><input name="agentModel" value="${esc(p.agentModel || "")}" placeholder="例如 deepseek-chat" /><small>可选；客户端未单独指定模型时使用。</small></label>
    </div></details>
    <details class="form-section" open><summary>按 Agent 覆盖 · 端点 / 密钥 / 模型</summary>${agentSections}<p class="form-intro">留空的 Agent 使用默认连接。Claude Code 需要 Anthropic Messages 端点；Codex 第三方线路只接受 Responses。</p></details>`, formFooter(edit ? "保存配置" : "添加供应商", edit ? `<button type="button" class="danger" data-action="delete-provider" data-pid="${esc(pid)}">${icon("trash")}删除</button>` : ""), { pid });
}
function openModel(pid, mid, agent = "") {
  if (state.busy) return;
  if (!Object.keys(state.providers).length) { openProvider(); toast("先连接一个供应商，再添加模型。"); return; }
  pid = pid || (state.filters.provider !== "all" ? state.filters.provider : Object.keys(state.providers)[0]);
  agent = agent || (state.filters.agent !== "all" ? state.filters.agent : "");
  const p = state.providers[pid] || {};
  const source = agent ? ((p.agents || {})[agent] || {}).models || [] : p.models || [];
  const m = source.find(m => m.id === mid) || {};
  const edit = mid !== undefined;
  const inputs = [...new Set(["text", "image", "audio", "video", ...(m.input || [])])];
  // 模型属于哪条连接；编辑时不允许改变（避免静默验证失败）。
  const connections = [["", `默认连接（${(p.models || []).length} 个模型）`],
    ...Object.entries(p.agents || {}).filter(([agent]) => AGENT_LABEL[agent]).map(([name, block]) => [name, `${connectionLabel(name)} 连接（${((block && block.models) || []).length} 个模型）`])];
  showEditor("model", edit ? "编辑模型" : "添加新的模型", `<p class="form-intro">${edit ? "保留高级配置，只更新你在此修改的模型字段。" : "模型 ID 必须与该连接接口中的标识一致。不确定时，可先在供应商页检测模型。"}</p><div class="form-grid"><label class="field full"><span>所属供应商</span><select name="provider" ${edit ? "disabled" : ""}>${Object.keys(state.providers).map(id => `<option value="${esc(id)}" ${id === pid ? "selected" : ""}>${esc(providerName(id))} (${esc(id)})</option>`).join("")}</select></label><label class="field full"><span>所属连接</span><select name="agent" ${edit ? "disabled" : ""}>${connections.map(([name, label]) => `<option value="${esc(name)}" ${name === agent ? "selected" : ""}>${esc(label)}</option>`).join("")}</select><small>同一供应商的不同连接（不同 Key）可以有不同的模型列表。</small></label><label class="field full"><span>模型 ID <b class="required">*</b></span><input class="mono" name="id" value="${esc(m.id || "")}" placeholder="例如 deepseek-chat 或 vendor/model-name" required /></label><label class="field full"><span>显示名称</span><input name="name" value="${esc(m.name || "")}" placeholder="可选，默认使用模型 ID" /></label><label class="field"><span>上下文窗口 · tokens</span><input name="contextWindow" type="number" min="1" max="100000000" step="1" value="${Number(m.contextWindow) || 128000}" required /></label><label class="field"><span>最大输出 · tokens</span><input name="maxTokens" type="number" min="1" max="100000000" step="1" value="${Number(m.maxTokens) || 32768}" required /></label><fieldset class="field full"><legend>输入类型</legend><div class="check-group">${inputs.map(v => `<label class="check-label"><input type="checkbox" name="input" value="${esc(v)}" ${(m.input || ["text"]).includes(v) ? "checked" : ""} />${esc(({ text: "文本", image: "图片", audio: "音频", video: "视频" })[v] || v)}</label>`).join("")}</div></fieldset><label class="check-label full"><input type="checkbox" name="reasoning" ${m.reasoning !== false ? "checked" : ""} />支持推理 / 思考</label></div>`, formFooter(edit ? "保存模型" : "添加模型"), { pid, mid, agent });
}

async function discover(pid) {
  if (state.busy) return;
  state.discoverAgent = state.discoverAgent || "";
  await busy("正在检测远端模型…", async () => {
    const query = state.discoverAgent ? `?agent=${encodeURIComponent(state.discoverAgent)}` : "";
    const result = await api("GET", `${pathFor(pid)}/discover${query}`);
    const p = state.providers[pid] || {};
    const source = state.discoverAgent ? ((p.agents || {})[state.discoverAgent] || {}).models || [] : p.models || [];
    const existing = new Set(source.map(m => m.id));
    state.discovered = (result.models || []).filter((m, i, list) => m.id && list.findIndex(v => v.id === m.id) === i);
    const added = state.discovered.filter(m => !existing.has(m.id)).length;
    const p2 = state.providers[pid] || {};
    const connections = [["", `默认连接（${(p2.models || []).length} 个模型）`],
      ...Object.entries(p2.agents || {}).filter(([agent]) => AGENT_LABEL[agent]).map(([name, block]) => [name, `${connectionLabel(name)} 连接（${((block && block.models) || []).length} 个模型）`])];
    showEditor("discover", `检测模型 · ${providerName(pid)}`, `<div class="notice" style="margin-bottom:16px">${icon("shield")}仅导入勾选的新模型。不会覆盖已有参数，也不会移除未检测到的模型。</div><label class="field full" style="margin-bottom:12px"><span>检测哪来连接（不同 Key 返回的模型集合可能不同）</span><select id="discover-agent" aria-label="选择连接">${connections.map(([name, label]) => `<option value="${esc(name)}" ${name === state.discoverAgent ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label><div class="search-box" style="max-width:none">${icon("search")}<input id="discover-search" type="search" aria-label="搜索检测结果" placeholder="搜索检测到的模型…" /></div><div class="discover-toolbar"><label class="check-label"><input type="checkbox" id="discover-all" ${added ? "checked" : "disabled"} />选择当前可见的新模型</label><span>检测到 ${state.discovered.length} 个 · ${added} 个新模型</span></div><div class="discover-list" id="discover-list">${state.discovered.map((m, index) => `<label class="discover-row ${existing.has(m.id) ? "existing" : ""}" data-search="${esc(`${m.id} ${m.name || ""}`.toLocaleLowerCase())}"><input class="discover-check" type="checkbox" name="discovered" value="${index}" ${existing.has(m.id) ? "disabled" : "checked"} /><div><div class="mono">${esc(m.id)}</div>${m.name && m.name !== m.id ? `<small>${esc(m.name)}</small>` : ""}</div><span class="tag ${existing.has(m.id) ? "" : "green"}">${existing.has(m.id) ? "已添加" : "新模型"}</span></label>`).join("") || empty("search", "没有检测到模型", "请检查该连接的接口与授权。")}</div>`, formFooter(`导入 ${added} 个新模型`), { pid });
    updateDiscoverSelection();
  });
}
function updateDiscoverSelection() {
  const checked = $$(".discover-check:checked:not(:disabled)").length;
  const submit = $('#editor-form button[type="submit"]');
  submit.innerHTML = `${icon("download")}导入 ${checked} 个新模型`;
  submit.disabled = !checked;
  const visible = $$(".discover-row:not([hidden]) .discover-check:not(:disabled)");
  const selected = visible.filter(c => c.checked).length;
  $("#discover-all").checked = !!visible.length && selected === visible.length;
  $("#discover-all").indeterminate = selected > 0 && selected < visible.length;
  $("#discover-all").disabled = !visible.length;
}

// ---- 从客户端导入供应商（反向同步的读取侧：任一平台的新供应商都可读取并写入其它平台）----
const IMPORT_CLIENTS = [ ["pi", "pi"], ["dsh", "dsh"], ["codex", "Codex"], ["claude", "Claude Code"]];
function importListHtml(candidates, client) {
  if (!candidates.length) {
    return `<div class="notice">${icon("info")}${client} 的原生配置里没有 Hub 尚未管理的供应商。先在该客户端里添加（或确认未被 Hub 覆盖）后，再回来扫描。</div>`;
  }
  return `<p class="form-intro">来自 ${esc(client)} 的原生配置；勾选后写入 Hub，随后即可在「平台同步」里推送给其它所有平台。OAuth/订阅登录与 Hub 已管理的条目不会出现在这里。</p>
  <div class="discover-list">${candidates.map((c, index) => `<label class="discover-row"><input class="import-check" type="checkbox" data-index="${index}" checked /><div><div><strong>${esc(c.name)}</strong> <span class="tag">${esc(c.api)}</span>${c.apiKeyEnv ? `<span class="tag" title="密钥经环境变量 ${esc(c.apiKeyEnv)} 引用">env</span>` : ""}</div><div class="mono" title="${esc(c.baseUrl)}">${esc(c.baseUrl)}</div></div><span class="tag purple">${c.modelCount} 个模型</span></label>`).join("")}</div>`;
}
function updateImportSelection() {
  const checked = $$(".import-check:checked").length;
  const button = $('#import-footer [data-action="import-apply"]');
  if (button) { button.disabled = !checked; button.innerHTML = `${icon("download")}导入 ${checked} 个供应商`; }
}
async function scanImportClient(target) {
  state.importTarget = target;
  await busy("正在扫描供应商…", async () => {
    const result = await api("GET", `/api/import/${encodeURIComponent(target)}`);
    state.importCandidates = result.candidates || [];
  });
  const client = (IMPORT_CLIENTS.find(([name]) => name === target) || ["", target])[1];
  $("#import-body").innerHTML = `<label class="field full" style="margin-bottom:12px"><span>读取哪个平台的原生配置</span><select id="import-client" aria-label="选择平台">${IMPORT_CLIENTS.map(([name, label]) => `<option value="${esc(name)}" ${name === target ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>` + importListHtml(state.importCandidates, client);
  $("#import-footer").innerHTML = state.importCandidates.length
    ? `<button type="button" data-action="close-import">取消</button><button type="button" class="primary" data-action="import-apply" data-write>${icon("download")}导入 ${state.importCandidates.length} 个供应商</button>`
    : `<button type="button" data-action="close-import">关闭</button>`;
  updateImportSelection();
}
async function applyImport() {
  const indexes = $$(".import-check:checked").map(el => Number(el.dataset.index));
  const items = indexes.map(i => state.importCandidates[i]).filter(Boolean);
  if (!items.length || !state.importTarget) return;
  const target = state.importTarget;
  await busy("正在导入供应商…", async () => {
    await api("POST", `/api/import/${encodeURIComponent(target)}`, { items });
  });
  $("#import-dialog").close();
  state.importCandidates = null;
  await refreshAfterWrite(`已导入 ${items.length} 个供应商；到「平台同步」预览并应用后才会写入各客户端。`);
}

async function submitEditor(event) {
  event.preventDefault();
  if (state.busy) return;
  const fd = new FormData(event.target), editor = { ...state.editor };
  $("#editor-error").hidden = true;
  const value = key => String(fd.get(key) || "").trim();
  let payload;
  if (editor.type === "provider") {
    if (!value("baseUrl") && !AGENT_OVERRIDES.some(([name]) => value(`agent-${name}-baseUrl`)) && !value("anthropicBaseUrl")) {
      $("#editor-error").textContent = "请填写默认 Base URL，或在「按 Agent 覆盖」里为需要的 Agent 配置端点。";
      $("#editor-error").hidden = false;
      return;
    }
    let targets;
    try { targets = providerTargetsFromForm(fd); }
    catch (err) { $("#editor-error").textContent = err.message; $("#editor-error").hidden = false; return; }
    payload = { id: value("id"), name: editor.pid ? value("name") : value("displayName"), displayName: value("displayName"), baseUrl: value("baseUrl"), apiKey: value("apiKey"), api: value("api"), notes: value("notes"), ...(editor.pid ? { enabled: fd.has("enabled") } : {}) };
    payload.targets = targets;
    // 编辑时始终发送 agentModel，让「留空」表示清除；新增时留空就不写这个字段。
    if (editor.pid || value("agentModel")) payload.agentModel = value("agentModel");
    // 按 Agent 覆盖：只把填了内容的 Agent 写进 agents；全部留空表示清除该覆盖。
    const agents = {};
    for (const [name] of AGENT_OVERRIDES) {
      const block = {};
      for (const field of ["baseUrl", "apiKey", "apiKeyEnv", "api"]) {
        const raw = value(`agent-${name}-${field}`);
        if (raw) block[field] = raw;
      }
      if (name === "claude") {
        for (const [field, label] of [["env", "额外环境变量"], ["settings", "顶层 settings 字段"]]) {
          let parsed = null;
          try { parsed = parseJsonField(fd.get(`agent-claude-${field}`), label); }
          catch (err) { $("#editor-error").textContent = err.message; $("#editor-error").hidden = false; return; }
          if (parsed) block[field] = parsed;
        }
      }
      // 模型列表不在表单里编辑（由模型库 / 检测模型维护），保留原值。
      const existing = (state.providers[editor.pid]?.agents || {})[name];
      if (existing && Array.isArray(existing.models)) block.models = existing.models;
      if (Object.keys(block).length) agents[name] = block;
    }
    payload.agents = agents;
  }
  if (editor.type === "model") {
    payload = { id: value("id"), name: value("name"), agent: value("agent"), contextWindow: Number(value("contextWindow")), maxTokens: Number(value("maxTokens")), input: fd.getAll("input"), reasoning: fd.has("reasoning") };
    if (!payload.input.length) { $("#editor-error").textContent = "请至少选择一种输入类型。"; $("#editor-error").hidden = false; return; }
  }
  if (editor.type === "discover") {
    payload = { mode: "merge", agent: value("agent"), models: fd.getAll("discovered").map(i => state.discovered[Number(i)]) };
    if (!payload.models.length) return;
  }
  await busy("正在保存本地配置…", async () => {
    let result;
    if (editor.type === "provider") result = await api(editor.pid ? "PUT" : "POST", editor.pid ? pathFor(editor.pid) : "/api/providers", payload);
    if (editor.type === "model") {
      const pid = editor.pid || value("provider");
      result = await api(editor.mid !== undefined ? "PUT" : "POST", `${pathFor(pid)}/models${editor.mid !== undefined ? `/${encodeURIComponent(editor.mid)}` : ""}`, payload);
    }
    if (editor.type === "discover") result = await api("POST", `${pathFor(editor.pid)}/models/import`, payload);
    // 服务端已保存就关闭表单，刷新失败也不让用户误以为保存失败而重复提交。
    closeEditor();
    await refreshAfterWrite(editor.type === "discover" ? `已导入 ${result.added} 个新模型，已有配置保持不变。` : editor.type === "model" ? "模型已保存。同步到平台后即可使用。" : "供应商已保存，可在供应商页检测模型。");
  });
}

function confirmDialog(title, description, items = [], label = "确认删除", danger = true, ack = "") {
  return new Promise(resolve => {
    const dialog = $("#confirm-dialog");
    $("#confirm-title").textContent = title;
    $("#confirm-description").textContent = description;
    $("#confirm-symbol").innerHTML = icon(danger ? "trash" : "info");
    $("#confirm-list").hidden = !items.length;
    $("#confirm-list").innerHTML = items.map(item => `<div>${esc(item)}</div>`).join("");
    // 影响范围里包含当前屏幕看不到的模型时，必须显式勾选确认才能继续。
    const ackRow = $("#confirm-ack"), ackBox = $("#confirm-ack-box"), ok = $("#confirm-ok");
    ackRow.hidden = !ack;
    ackBox.checked = false;
    if (ack) $("#confirm-ack-label").textContent = ack;
    const syncOk = () => { ok.disabled = !!ack && !ackBox.checked; };
    ok.textContent = label;
    ok.className = danger ? "danger-solid" : "primary";
    ackBox.onchange = syncOk;
    syncOk();
    const finish = value => { dialog.close(); dialog.oncancel = null; ok.onclick = null; $("#confirm-cancel").onclick = null; ackBox.onchange = null; resolve(value); };
    ok.onclick = () => finish(true);
    $("#confirm-cancel").onclick = () => finish(false);
    dialog.oncancel = event => { event.preventDefault(); finish(false); };
    dialog.showModal();
    $("#confirm-cancel").focus();
  });
}
async function deleteModel(pid, mid, agent = "") {
  const where = agent ? `${providerName(pid)} · ${connectionLabel(agent)} 连接 / ${mid}` : `${providerName(pid)} / ${mid}`;
  if (!await confirmDialog("删除这个模型？", "仅从该连接的本地模型库移除。下次同步时会从客户端配置中移除；写入前会自动备份。", [where])) return;
  const query = agent ? `?agent=${encodeURIComponent(agent)}` : "";
  await busy("正在删除模型…", async () => { await api("DELETE", `${pathFor(pid)}/models/${encodeURIComponent(mid)}${query}`); await refreshAfterWrite("模型已删除，等待同步到平台。"); });
}
async function bulkDelete() {
  const models = allModels().filter(m => state.selection.has(m.key));
  if (!models.length) return;
  const severe = models.length >= 5 && models.length >= allModels().length / 2;
  const visibleKeys = new Set(visibleModels().map(m => m.key));
  const offscreen = models.filter(m => !visibleKeys.has(m.key)).length;
  const warning = `${severe ? "注意：此次将清理至少一半的模型库。\n" : ""}仅删除本地记录，下次同步才影响客户端。可从备份恢复。`;
  // 跨页 / 跨筛选的选择是刻意支持的，但屏幕上看不到的部分必须逐个勾选确认。
  const ack = offscreen ? `我确认删除当前筛选或页面上看不到的 ${offscreen} 个模型` : "";
  if (!await confirmDialog(`删除选中的 ${models.length} 个模型？`, warning, models.map(m => `${m.pid} / ${m.id}`), "确认删除", true, ack)) return;
  await busy("正在批量删除…", async () => {
    const groups = new Map();
    models.forEach(m => { const key = JSON.stringify([m.pid, m.agent || ""]); if (!groups.has(key)) groups.set(key, []); groups.get(key).push(m.id); });
    let removed = 0;
    try {
      for (const [key, ids] of groups) {
        const [pid, agent] = JSON.parse(key);
        const result = await api("POST", `${pathFor(pid)}/models/batch-delete`, { ids, agent });
        removed += result.removed;
      }
    } catch (e) { await reload(); throw new Error(`已删除 ${removed} 个模型；剩余操作失败：${e.message}`); }
    state.selection.clear();
    await refreshAfterWrite(`已删除 ${removed} 个模型，等待同步到平台。`);
  });
}
async function deleteProvider(pid) {
  if (!await confirmDialog(`删除 ${providerName(pid)}？`, `将删除该供应商的 ${allModels().filter(m => m.pid === pid).length} 个模型，并立即执行现有客户端清理逻辑。这与删除单个模型不同，不需要等待下次同步。配置会自动备份。`, [pid])) return;
  await busy("正在移除供应商…", async () => { await api("DELETE", pathFor(pid)); closeEditor(); await refreshAfterWrite("供应商已移除。请同步并重启客户端，确保所有平台配置对齐。"); });
}

async function navigate(page) {
  if (!PAGES[page]) page = "agents";
  if (page === state.page) return;
  if (state.busy || state.page === "sync" && targetDirty() && !await confirmDialog("平台设置尚未保存", "离开将放弃未保存的平台选择与模型设置。你也可以取消，先保存设置。", [], "放弃并离开", false)) {
    history.replaceState(null, "", `#${state.page}`);
    return;
  }
  if (state.page === "sync") resetTargetDraft();
  state.page = page;
  renderPage();
}

// 委托事件避免重绘后重复绑定；所有写入都有统一的忙碌状态。
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled || state.busy) return;
  const { action, pid, mid } = button.dataset;
  try {
    if (action === "add-provider") openProvider();
    else if (action === "import-client") await scanImportClient(state.importTarget || "claude");
    else if (action === "import-apply") await applyImport();
    else if (action === "close-import") $("#import-dialog").close();
    else if (action === "edit-provider") openProvider(pid);
    else if (action === "add-model") openModel();
    else if (action === "edit-model") openModel(pid, mid, button.dataset.agent);
    else if (action === "close-editor") await requestCloseEditor();
    else if (action === "delete-provider") await deleteProvider(pid);
    else if (action === "delete-model") await deleteModel(pid, mid, button.dataset.agent);
    else if (action === "bulk-delete") await bulkDelete();
    else if (action === "discover") await discover(pid);
    else if (action === "clear-selection") { state.selection.clear(); renderModelRows(); }
    else if (action === "filter-provider") { state.filters.provider = pid; state.modelPage = 1; renderModels(); }
    else if (action === "clear-filters") { state.filters = { query: "", provider: "all", agent: "all", capability: "all" }; state.modelPage = 1; renderModels(); }
    else if (action === "prev-page" || action === "next-page") { state.modelPage += action === "next-page" ? 1 : -1; renderModelRows(); }
    else if (action === "go-sync") location.hash = "sync";
    else if (action === "provider-models") { state.filters = { query: "", provider: pid, agent: "all", capability: "all" }; state.modelPage = 1; location.hash = "models"; }
    else if (action === "toggle-key") {
      // 表单里可能有多个密钥框（默认 + 各 Agent 覆盖），只切换按钮所属那个。
      const input = button.parentElement.querySelector("input");
      input.type = input.type === "password" ? "text" : "password";
      button.setAttribute("aria-label", input.type === "password" ? "显示密钥" : "隐藏密钥");
      button.title = button.getAttribute("aria-label");
    }
    else if (action === "reset-targets") { resetTargetDraft(); renderSync(); }
    else if (action === "save-targets") await busy("正在保存平台设置…", async () => { await saveTargets(); await reload(); toast("平台设置已保存，尚未执行同步。"); });
    else if (action === "ms-override") {
      // 为这一行建立 override 编辑态；预填当前解析结果，用户只需改想改的部分。
      const target = button.dataset.target;
      const resolved = modelSyncResolved(target);
      setModelEntry("clients", target, "", { provider: resolved?.provider || state.modelSyncDraft?.default?.provider || "", model: resolved?.model || "" });
      renderModelSyncCard();
    }
    else if (action === "ms-follow-global") {
      invalidatePreview();
      // 删除 override 键 = 这一行回到全局设置；档位则回到 Claude 行的主模型。
      const { scope, target, tier } = button.dataset;
      if (scope === "tiers") {
        const tiers = state.modelSyncDraft.overrides?.claude?.tiers;
        if (tiers) { delete tiers[tier]; if (!Object.keys(tiers).length) delete state.modelSyncDraft.overrides.claude.tiers; }
        delete state.modelSyncCustom[customKey("tiers", "claude", tier)];
      } else {
        delete state.modelSyncDraft.overrides?.[target];
        delete state.modelSyncCustom[customKey("clients", target, "")];
      }
      renderModelSyncCard();
    }
    else if (action === "save-modelsync") await busy("正在保存模型设置…", async () => {
      const { warnings } = await saveModelSync();
      await reload();
      toast(warnings.length
        ? `模型设置已保存，但需注意：${warnings.join("；")}。还需点同步才会写入客户端。`
        : "模型设置已保存到本地。还需点同步才会写入客户端。", warnings.length ? "error" : "ok");
    });
    else if (action === "ms-reset") { resetTargetDraft(); renderSync(); toast("已撤销未保存的模型设置。"); }
    else if (action === "reload-modelsync") await busy("正在读取模型设置…", async () => {
      await loadModelSync();
      state.modelSyncDraft = null;
      state.modelSyncDraftKey = "";
      renderModelSyncCard();
      if (state.modelSyncError) { toast(`模型设置接口仍不可用：${state.modelSyncError}`, "error"); return; }
      toast("模型设置已重新读取。");
    });
    else if (action === "apply-preview") await applyPreview();
    else if (action === "cancel-preview") invalidatePreview();
    else if (action === "restore-sync") await restoreSync();
    else if (action === "run-sync" || action === "detect-sync") await runSync(action === "detect-sync");
    // ---- 切换方案 ----
    else if (action === "plan-load") {
      const id = $("#plan-select")?.value || "";
      if (!id) { state.planLoaded = null; state.planLoadedPayload = null; state.planDraft = emptyPlanDraft(); state.planTargets = new Set(); state.planOverrides = {}; state.planName = ""; state.planDescription = ""; state.planAnalysis = null; state.planPreviewResult = null; renderPlanCard(); }
      else {
        const plan = state.plans.store.plans.find(p => p.id === id);
        loadPlanIntoDraft(plan);
        renderPlanCard();
        const stale = (plan.refs || []).filter(r => !r.valid);
        toast(stale.length ? `已载入「${plan.name}」；有 ${stale.length} 个引用已失效，请先修正。` : `已载入「${plan.name}」。`, stale.length ? "error" : "ok");
      }
    }
    else if (action === "plan-duplicate") {
      // 复制为新方案：保留清单，只清掉"当前方案"的绑定，避免误覆盖原方案。
      state.planLoaded = null; state.planLoadedPayload = null;
      state.planName = `${state.planName || "方案"} 副本`;
      renderPlanCard();
      toast("已复制为未保存的新方案；改好名字后点「保存为新方案」。");
    }
    else if (action === "plan-save") await busy("正在保存切换方案…", async () => { const message = await savePlan(); await reload(); toast(message); });
    else if (action === "plan-delete") {
      const plan = state.planLoaded;
      if (!plan) return;
      if (!await confirmDialog(`删除方案「${plan.name}」？`, "只删除这份保存的方案，不改动任何客户端配置，也不改动当前生效的模型设置。", [], "删除方案")) return;
      await busy("正在删除方案…", async () => { const message = await deletePlan(); toast(message); });
    }
    else if (action === "plan-analyze") await busy("正在检查每个 Agent…", async () => {
      const data = await planAnalyze();
      toast(data.errors?.length ? `发现 ${data.errors.length} 个不兼容项，没有写入任何文件。` : `检查完成：${data.summary.ready.length} 个 Agent 可以写入${data.summary.skipped.length ? `，${data.summary.skipped.length} 个会跳过` : ""}。`, data.errors?.length ? "error" : "ok");
    });
    else if (action === "plan-preview") await busy("正在生成原生配置预览…", async () => {
      const data = await planPreview();
      toast(`预览已生成：${data.targets.join("、")}，共 ${data.files.length} 个文件。确认后才会写入客户端。`);
    });
    else if (action === "plan-confirm-apply") {
      if (!state.planPreviewResult?.planId) { toast("请先生成预览，再确认应用。", "error"); return; }
      if (!await confirmDialog("确认写入这些客户端？", "会按上面的清单写入已选 Agent 的原生配置文件，写入前自动备份；正在运行的客户端需要重启才会使用新模型。", [], "确认应用", false)) return;
      await busy("正在写入客户端配置…", async () => {
        // 应用用户实际检查过的预览；源文件或 Hub 配置发生变更时拒绝写入。
        const preview = state.planPreviewResult;
        const receipt = await api("POST", "/api/apply", { planId: preview.planId });
        const data = { ...preview, ...receipt, results: preview.results.map(row => {
          const written = receipt.details?.find(d => d.target === row.target);
          return written ? { ...row, status: written.ok ? "ok" : "failed", writeOk: written.ok, message: written.message } : row;
        }), written: (receipt.details || []).filter(d => d.ok).map(d => d.target), failed: (receipt.details || []).filter(d => !d.ok).map(d => d.target) };
        state.planPreviewResult = data;
        await reload();
        state.planPreviewResult = data;
        renderPlanCard();
        const written = data.written || [], failed = data.failed || [];
        toast(failed.length ? `${written.length} 个写入成功，${failed.length} 个失败：请查看结果区，可按需恢复。`
                            : `已写入 ${written.length} 个客户端；重启对应客户端后才会用上新模型。`, failed.length ? "error" : "ok");
      });
    }
    else if (action === "plan-pick-usable") {
      const base = state.planDraft.base || {};
      const usable = planTargetsForModel(base.provider, base.model);
      state.planTargets = new Set(usable);
      Object.keys(state.planOverrides).forEach(n => { if (!usable.includes(n)) delete state.planOverrides[n]; });
      renderPlanCard();
      toast(`已选中 ${usable.length} 个连接上有这个模型的 Agent。`);
    }
    else if (action === "plan-cancel") { state.planPreviewResult = null; renderPlanCard(); }
  } catch (e) { toast(e.message, "error"); }
});
document.addEventListener("input", event => {
  const el = event.target;
  if ((el.name === "baseUrl" || el.name.startsWith("agent-")) && state.editor?.type === "provider") {
    const hasAgentBase = AGENT_OVERRIDES.some(([name]) => String($(`#editor-form [name="agent-${name}-baseUrl"]`)?.value || "").trim());
    $("#editor-form [name='baseUrl']").required = !String(el.value || "").trim() && !hasAgentBase && el.name === "baseUrl";
  }
  if (el.id === "model-search") { state.filters.query = el.value; state.modelPage = 1; renderModelRows(); }
  if (el.id === "plan-name") { state.planName = el.value; }
  if (el.id === "plan-description") { state.planDescription = el.value; }
  if (el.id === "provider-search") { state.providerQuery = el.value; renderProviderCards(); }
  if (el.id === "discover-search") { const q = el.value.trim().toLocaleLowerCase(); $$(".discover-row").forEach(row => { row.hidden = !row.dataset.search.includes(q); }); updateDiscoverSelection(); }
  if (el.dataset.msCustom !== undefined && !state.busy) {
    // 手填模型 ID：只更新草稿与按钮状态，不重建卡片，避免打断输入；清空则退出该行的手填模式。
    const scope = el.dataset.scope, target = el.dataset.target, tier = el.dataset.tier || "", text = el.value.trim();
    if (text) {
      state.modelSyncCustom[customKey(scope, target, tier)] = text;
      setModelEntry(scope, target, tier, { model: text });
      updateModelSyncUI();
    } else {
      delete state.modelSyncCustom[customKey(scope, target, tier)];
      renderModelSyncCard();
    }
  }
});
document.addEventListener("change", event => {
  const el = event.target;
  if (state.busy) return;
  // ---- 切换方案：任何只读字段改动都作废已有预览，避免用旧清单写新配置 ----
  if (el.dataset.planPick !== undefined) {
    const name = el.dataset.planPick;
    if (el.checked) state.planTargets.add(name); else state.planTargets.delete(name);
    if (!el.checked) delete state.planOverrides[name];
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.dataset.planPolicy !== undefined) {
    const name = el.dataset.planPolicy, value = el.value;
    if (value === "follow") delete state.planOverrides[name];
    else {
      const previous = state.planOverrides[name] || {};
      const base = state.planDraft.base || {};
      // 从"跟随"切到"单独指定"时预填当前值，用户只改想改的那一项。
      const conn = state.plans?.targets?.[name]?.connections;
      const seed = conn?.[base.provider]?.ready ? base.provider : Object.keys(conn || {}).find(pid => conn[pid].ready) || "";
      state.planOverrides[name] = { policy: value, provider: previous.provider || (value === "fixed" ? seed : ""), model: previous.model || (value === "fixed" ? (conn?.[seed]?.models?.[0] || "") : ""), reasoningEffort: previous.reasoningEffort || "" };
    }
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.dataset.planProvider !== undefined) {
    const name = el.dataset.planProvider, spec = state.planOverrides[name] || { policy: "fixed" };
    const models = Object.entries(state.plans?.targets?.[name]?.connections?.[el.value]?.models || {});
    const known = (state.plans?.targets?.[name]?.connections?.[el.value]?.models) || [];
    spec.policy = "fixed"; spec.provider = el.value;
    if (spec.model && !known.includes(spec.model)) spec.model = known[0] || "";
    state.planOverrides[name] = spec;
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.dataset.planModel !== undefined) {
    const name = el.dataset.planModel, spec = state.planOverrides[name] || { policy: "fixed", provider: "" };
    spec.policy = "fixed"; spec.model = el.value;
    state.planOverrides[name] = spec;
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.dataset.planEffort !== undefined) {
    const name = el.dataset.planEffort, spec = state.planOverrides[name] || { policy: "fixed" };
    spec.reasoningEffort = el.value;
    state.planOverrides[name] = spec;
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.dataset.planAllow !== undefined) {
    const name = el.dataset.planAllow, spec = state.planOverrides[name] || { policy: "fixed" };
    spec.allowUnknownModel = el.checked;
    state.planOverrides[name] = spec;
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.id === "plan-provider") {
    // 换供应商后清掉不属于新供应商的旧模型，要求重新选择；不静默换模型。
    const draft = state.planDraft;
    const models = planModelOptions(el.value);
    draft.base.provider = el.value;
    if (draft.base.model && !models.includes(draft.base.model)) draft.base.model = "";
    Object.entries(state.planOverrides).forEach(([, spec]) => { if (!spec.allowUnknownModel && spec.model && !models.includes(spec.model)) spec.model = ""; });
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.id === "plan-model" || el.id === "plan-effort") {
    const key = el.id === "plan-model" ? "model" : "reasoningEffort";
    state.planDraft.base[key] = el.value;
    if (el.id === "plan-model") {
      // 选完模型就把"这条模型在哪些 Agent 的连接上可用"直接选中：用户只需取消不想换的，
      // 不必先猜哪些能用；不可用的 Agent 不会被勾上，因此不会写到不认识的连接上。
      const usable = el.value ? planTargetsForModel(state.planDraft.base.provider, el.value) : [];
      state.planTargets = new Set(usable);
      Object.keys(state.planOverrides).forEach(n => { if (!usable.includes(n)) delete state.planOverrides[n]; });
    }
    state.planAnalysis = null; state.planPreviewResult = null;
    renderPlanCard();
    return;
  }
  if (el.id === "plan-select") return;
  if (el.id === "capability-filter") { state.filters.capability = el.value; state.modelPage = 1; renderModelRows(); }
  if (el.classList.contains("model-check")) { if (el.checked) state.selection.add(el.dataset.key); else state.selection.delete(el.dataset.key); renderSelection(); }
  if (el.id === "select-page") { visibleModels().forEach(m => el.checked ? state.selection.add(m.key) : state.selection.delete(m.key)); renderModelRows(); }
  if (el.classList.contains("target-check") && !el.disabled) { invalidatePreview(); state.targetDraft[el.dataset.target] = el.checked; updateTargetDraftUI(); }
  if (el.name === "allTargets" && state.editor?.type === "provider") {
    $("#editor-form").querySelectorAll('[name="targets"]').forEach(input => { input.disabled = el.checked; });
  }
  if (el.id.endsWith("-mode")) {
    const name = el.id.slice(0, -5);
    if (MODE_CLIENTS[name]) { invalidatePreview(); state.clientMode[name] = el.value; state.clientOptIn[name] = false; renderModelSyncCard(); updateTargetDraftUI(); }
  }
  if (el.id.endsWith("-optin")) {
    const name = el.id.slice(0, -6);
    if (MODE_CLIENTS[name]) { invalidatePreview(); state.clientOptIn[name] = el.checked; updateTargetDraftUI(); }
  }
  if (el.id === "agent-filter") { state.filters.agent = el.value; state.modelPage = 1; renderModelRows(); }
  if (el.id === "discover-agent") { state.discoverAgent = el.value; discover(state.editor?.pid); }
  if (el.classList.contains("discover-check")) updateDiscoverSelection();
  if (el.classList.contains("import-check")) updateImportSelection();
  if (el.id === "import-client" && el.value !== state.importTarget) { scanImportClient(el.value).catch(e => toast(e.message, "error")); }
  if (el.id === "discover-all") { $$(".discover-row:not([hidden]) .discover-check:not(:disabled)").forEach(c => { c.checked = el.checked; }); updateDiscoverSelection(); }
  if (el.dataset.msProvider !== undefined || el.dataset.msSelect !== undefined || el.dataset.msEffort !== undefined) {
    const { scope, target, tier = "" } = el.dataset;
    const draft = ensureModelSyncDraft();
    const entry = scope === "default" ? draft.default : scope === "tiers" ? (draft.overrides?.claude?.tiers?.[tier] || {}) : (draft.overrides?.[target] || {});
    delete state.modelSyncCustom[customKey(scope, target, tier)];
    if (el.dataset.msProvider !== undefined) {
      // 换供应商后清除不属于新供应商的旧模型，要求重新选择或手填。
      const stale = entry.model && !knownModels(el.value, target === "default" ? null : target).has(entry.model);
      setModelEntry(scope, target, tier, { provider: el.value, model: stale ? "" : entry.model });
    } else if (el.dataset.msSelect !== undefined) {
      // 「手动填写」只切换编辑方式，保留原值等用户改写；选具体模型则直接写入草稿。
      if (el.value === CUSTOM_MODEL) {
        state.modelSyncCustom[customKey(scope, target, tier)] = true;
      } else {
        // 后端要求 provider 与 model 成对：覆盖行没显式选过供应商时，补上当前生效的供应商。
        const fallback = entry.provider || (scope === "tiers" ? claudeTiersView()[tier]?.provider : modelSyncResolved(target)?.provider) || draft.default?.provider || "";
        setModelEntry(scope, target, tier, { provider: entry.provider || fallback, model: el.value });
      }
    } else {
      setModelEntry(scope, target, tier, { reasoningEffort: el.value });
    }
    renderModelSyncCard();
  }
});
document.addEventListener("toggle", event => {
  // 档位面板的展开状态跨重绘保留，保存后不用重新展开。
  const details = event.target.closest?.("[data-ms-details]");
  if (!details || state.busy) return;
  const key = details.dataset.msDetails;
  if (details.open) state.modelSyncOpen.add(key);
  else state.modelSyncOpen.delete(key);
}, true);
$("#editor-form").addEventListener("submit", submitEditor);
$("#editor-dialog").addEventListener("cancel", event => { event.preventDefault(); requestCloseEditor(); });
$("#editor-dialog").addEventListener("click", event => {
  if (event.target !== event.currentTarget) return;
  const rect = event.currentTarget.getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) requestCloseEditor();
});
$("#refresh").addEventListener("click", async () => {
  if (state.busy) return;
  if (targetDirty() && !await confirmDialog("刷新并放弃平台设置？", "未保存的平台选择与模型设置会丢失。", [], "刷新", false)) return;
  resetTargetDraft();
  await busy("正在刷新配置…", async () => { await reload(); toast("配置已刷新。"); });
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
window.addEventListener("beforeunload", event => { if (editorDirty() || targetDirty() || state.busy) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (state.busy || $("#confirm-dialog").open) return;
    if ($("#editor-dialog").open) $("#editor-form").requestSubmit();
    else if (state.page === "sync" && targetDirty()) awaitSaveSyncPage();
  }
  if (event.key === "Delete" && state.page === "models" && state.selection.size && !state.busy && !$("#editor-dialog").open && !$("#confirm-dialog").open && !event.target.closest("input, textarea, select, [contenteditable]")) { event.preventDefault(); bulkDelete(); }
});
// Ctrl+S 与「保存模型设置」共用一条保存路径：模型设置先写，再保存平台勾选。
async function awaitSaveSyncPage() {
  await busy("正在保存模型设置…", async () => {
    if (modelSyncDirty() && !state.modelSyncError) {
      const { warnings } = await saveModelSync();
      if (warnings.length) toast(`模型设置已保存；需注意：${warnings.join("；")}`, "error");
    }
    await saveTargets();
    await reload();
    toast("平台设置已保存。模型设置还需点同步才会写入客户端。");
  });
}

state.page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : "agents";
reload().catch(e => {
  renderChrome();
  $("#content").innerHTML = `<div class="card">${empty("alert", "暂时无法加载工作空间", e.message, '<button data-action="retry" class="primary">重试连接</button>')}</div>`;
  const retry = $('[data-action="retry"]');
  retry.onclick = () => busy("正在重新连接…", reload);
});
