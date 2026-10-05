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

const PAGES = { models: ["模型库", "管理可用模型，让每个 Agent 都用上合适的模型。"], providers: ["供应商", "连接你的模型服务，所有平台共用一份配置。"], sync: ["平台同步", "将本地配置写入多个平台，每次同步都有迹可循。"] };
const TARGETS = {
  pi: ["pi", "pi", "模型、授权与默认设置"], mcode: ["mcode", "mc", "MiniMax 自定义供应商"],
  zcode: ["zcode", "zc", "供应商规则与模型配置"], dsh: ["dsh", "ds", "CLI 与桌面版共用配置"],
  "dsh-desktop": ["dsh 桌面版", "ds", "随 dsh 一起同步，无需重复选择"],
  claude: ["Claude Code", "cc", "Anthropic 环境变量与模型别名"],
  "cc-switch": ["cc-switch", "cs", "多客户端供应商数据库"], omp: ["omp", "om", "Agent 模型配置"],
  codex: ["Codex", "cx", "显式选择官方或第三方线路；登录态不动"],
};
const palette = [["#f0edff", "#9984cd"], ["#fff1e9", "#c99b7b"], ["#edf3ff", "#84a0cf"], ["#edf7f3", "#80af9b"]];
// 供应商表单里的平台多选：顺序固定，便于用户按习惯找到目标平台。
const PROVIDER_TARGETS = [["pi", "pi"], ["dsh", "dsh"], ["dsh-desktop", "dsh 桌面版"], ["claude", "Claude Code"], ["cc-switch", "cc-switch"], ["mcode", "mcode"], ["zcode", "zcode"], ["omp", "omp"], ["codex", "Codex"]];
const EFFORTS = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
const CODEX_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh"];
// 模型同步卡片里的客户端行；dsh-desktop 与 dsh 共用配置，不接受单独 override。
const SYNC_CLIENTS = [["pi", "pi"], ["dsh", "dsh（含桌面版）"], ["claude", "Claude Code"], ["codex", "Codex"]];
const CLAUDE_TIERS = [["opus", "opus 档位"], ["sonnet", "sonnet 档位"], ["haiku", "haiku 档位"], ["subagent", "subagent 档位"]];
const state = {
  providers: {}, status: null, page: "models", busy: false, loaded: false,
  filters: { query: "", provider: "all", capability: "all" }, modelPage: 1, selection: new Set(),
  providerQuery: "", targetDraft: {}, activeDraft: "", codexMode: "official", codexOptIn: false, preview: null, previewError: "", editor: null, editorSnapshot: "", discovered: [],
  modelDraft: "", modelOptions: [], modelLoading: false,
  modelSync: null, modelSyncError: "", modelSyncLoading: false, modelSyncDraft: null, modelSyncDraftKey: "", modelSyncCustom: {}, modelSyncOpen: new Set(),
};
const pathFor = pid => `/api/providers/${encodeURIComponent(pid)}`;
// ---- Claude 模型切换 ----
// 当前激活供应商在用的模型：claudeModels.model 优先，其次 claudeSettings.model。
const claudeModelOf = pid => {
  const p = state.providers[pid];
  if (!p) return "";
  return (p.claudeModels && p.claudeModels.model) || (p.claudeSettings && p.claudeSettings.model) || "";
};
// 候选列表 = 从供应商拉到的 + 当前在用的（避免切走后下拉里看不到它）
const claudeModelOptions = pid => {
  const ids = new Set(state.modelOptions);
  const cur = claudeModelOf(pid);
  if (cur) ids.add(cur);
  return [...ids].sort();
};
const modelDirty = () => {
  const pid = state.activeDraft;
  return !!pid && !!state.modelDraft && state.modelDraft !== claudeModelOf(pid);
};
const modelKey = (pid, mid) => JSON.stringify([pid, mid]);
const providerName = pid => state.providers[pid]?.displayName || state.providers[pid]?.name || pid;
const providerColor = pid => palette[Math.max(0, Object.keys(state.providers).indexOf(pid)) % palette.length];
const allModels = () => Object.entries(state.providers).flatMap(([pid, p]) => (p.models || []).map(m => ({ ...m, pid, key: modelKey(pid, m.id) })));
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
const targetDirty = () => effectiveTargets().some(([n, t]) => state.targetDraft[n] !== t.enabled) || state.activeDraft !== (state.status?.targets?.claude?.activeProvider || "") || modelDirty() || state.codexMode !== (state.status?.targets?.codex?.mode || "official") || (state.loaded && modelSyncDirty());
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
// 供应商在用的模型 ID：claudeModels 优先，其次供应商模型库与 Claude 顶层设置。
// models / options 里的条目可能是对象（{id,name,...}）也可能是字符串，两种都要接受。
const modelIds = list => (list || []).map(item => (typeof item === "string" ? item : item && item.id)).filter(Boolean);
const providerModels = pid => {
  const p = state.providers[pid] || {};
  const ids = new Set([...modelIds(p.models), p.claudeModels?.model, p.claudeSettings?.model, p.agentModel].filter(Boolean));
  return [...ids].sort();
};
// 模型候选 = 全局选项（/api/modelsync 的 options）+ 供应商模型库 + 当前在用的值。
const modelChoices = pid => {
  const ids = new Set([...(pid ? modelIds(state.modelSync?.options?.[pid]) : []), ...(pid ? providerModels(pid) : [])]);
  const current = state.modelSyncDraft?.default?.model;
  if (current) ids.add(current);
  return [...ids].sort();
};
const cleanEntry = entry => {
  const out = {};
  if (entry?.provider) out.provider = entry.provider;
  if (entry?.model) out.model = entry.model;
  if (entry?.reasoningEffort) out.reasoningEffort = entry.reasoningEffort;
  return out;
};
// 磁盘上还没有 modelSync 时，用后端解析出来的实际值给全局行做初始值。
const resolvedSeed = () => {
  const resolved = state.modelSync?.resolved || {};
  for (const target of ["claude", "pi", "dsh", "dsh-desktop"]) {
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
    state.modelSync = { modelSync: data.modelSync || {}, resolved: data.resolved || {}, tiers: data.tiers || {}, options: data.options || {}, configured: !!data.configured };
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
  const scopes = [["clients", "codex", ""], ["clients", "pi", ""], ["clients", "dsh", ""], ["clients", "claude", ""], ["tiers", "claude", "opus"], ["tiers", "claude", "sonnet"], ["tiers", "claude", "haiku"], ["tiers", "claude", "subagent"]];
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
  const [providers, status] = await Promise.all([api("GET", "/api/providers"), api("GET", "/api/status"), loadModelSync()]);
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
  state.activeDraft = state.status?.targets?.claude?.activeProvider || "";
  state.codexMode = state.status?.targets?.codex?.mode || "official";
  state.codexOptIn = state.codexMode === "third-party";
  invalidatePreview();
  state.modelDraft = "";
  state.modelOptions = [];
  state.modelLoading = false;
  // 多 agent 模型设置的草稿从后端最新数据重建，未保存的改动在此丢弃。
  state.modelSyncDraft = null;
  state.modelSyncDraftKey = "";
  state.modelSyncCustom = {};
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
  $("#content").innerHTML = heading(`<button data-action="add-provider">${icon("plus")}添加供应商</button><button class="primary" data-action="add-model">${icon("plus")}添加模型</button>`) + stats() + `
    <section class="card" aria-label="模型列表">
      <div class="card-heading"><div><h2>所有模型</h2><p>按供应商管理，按需添加与清理。</p></div><button class="ghost small" data-action="go-sync">${icon("sync")}前往同步${icon("arrow")}</button></div>
      <div class="provider-filters" role="group" aria-label="按供应商筛选"><button class="filter-chip ${state.filters.provider === "all" ? "active" : ""}" data-action="filter-provider" data-pid="all" aria-pressed="${state.filters.provider === "all"}">全部供应商<span class="count">${allModels().length}</span></button>${Object.entries(state.providers).map(([pid, p]) => `<button class="filter-chip ${state.filters.provider === pid ? "active" : ""}" data-action="filter-provider" data-pid="${esc(pid)}" aria-pressed="${state.filters.provider === pid}">${esc(providerName(pid))}<span class="count">${p.models?.length || 0}</span></button>`).join("")}</div>
      <div class="filter-bar"><div class="search-box">${icon("search")}<input id="model-search" type="search" placeholder="搜索模型名称、ID 或供应商…" aria-label="搜索模型" value="${esc(state.filters.query)}" /></div><select id="capability-filter" aria-label="筛选模型能力"><option value="all">全部能力</option><option value="reasoning">推理模型</option><option value="image">支持图片</option><option value="text">纯文本</option></select><span class="filter-summary" id="filter-summary"></span></div>
      <div id="bulk-bar" class="bulk-bar" hidden></div>
      <div class="table-wrap"><table class="models-table"><thead><tr><th class="check-cell"><input type="checkbox" id="select-page" aria-label="全选本页模型" /></th><th>模型名称 / ID</th><th>供应商</th><th>上下文</th><th>最大输出</th><th>能力</th><th>操作</th></tr></thead><tbody id="model-rows"></tbody></table></div>
      <div class="table-footer"><span id="page-info"></span><div class="pagination"><button data-action="prev-page" id="prev-page">上一页</button><span id="page-number"></span><button data-action="next-page" id="next-page">下一页</button></div></div>
    </section><div class="hint-line">${icon("shield")}添加与删除先保存到本地；前往「平台同步」后才会写入客户端。</div>`;
  $("#capability-filter").value = state.filters.capability;
  renderModelRows();
}
function filteredModels() {
  const { query, provider, capability } = state.filters;
  const q = query.trim().toLocaleLowerCase();
  return allModels().filter(m => (provider === "all" || m.pid === provider) && (!q || `${m.id} ${m.name} ${providerName(m.pid)} ${m.pid}`.toLocaleLowerCase().includes(q)) && (capability === "all" || capability === "reasoning" && m.reasoning !== false || capability === "image" && (m.input || []).includes("image") || capability === "text" && (m.input || ["text"]).every(v => v === "text")));
}
function visibleModels() {
  const list = filteredModels();
  state.modelPage = Math.min(Math.max(1, state.modelPage), Math.max(1, Math.ceil(list.length / 20)));
  return list.slice((state.modelPage - 1) * 20, state.modelPage * 20);
}
function renderModelRows() {
  const list = filteredModels(), visible = visibleModels();
  $("#filter-summary").textContent = `共 ${list.length} 个模型`;
  $("#model-rows").innerHTML = visible.length ? visible.map(m => `<tr class="${state.selection.has(m.key) ? "selected" : ""}"><td class="check-cell"><input type="checkbox" class="model-check" data-key="${esc(m.key)}" aria-label="选择 ${esc(m.name || m.id)}" ${state.selection.has(m.key) ? "checked" : ""} /></td><td><div class="model-name" title="${esc(m.name || m.id)}">${esc(m.name || m.id)}</div><div class="model-id" title="${esc(m.id)}">${esc(m.id)}</div></td><td><span class="provider-label" style="--provider-color:${providerColor(m.pid)[1]}"><span class="provider-dot"></span>${esc(m.pid)}</span></td><td><span class="token-value" title="${Number(m.contextWindow || 128000).toLocaleString()} tokens">${tokens(m.contextWindow || 128000)}</span></td><td><span class="token-value" title="${Number(m.maxTokens || 32768).toLocaleString()} tokens">${tokens(m.maxTokens || 32768)}</span></td><td><div class="tags">${m.reasoning !== false ? `<span class="tag purple">${icon("brain")}推理</span>` : ""}${(m.input || ["text"]).map(v => `<span class="tag ${v === "image" ? "blue" : ""}">${v === "image" ? icon("image") : ""}${esc(({ text: "文本", image: "图片", audio: "音频", video: "视频" })[v] || v)}</span>`).join("")}</div></td><td><div class="row-actions"><button class="icon-button" data-action="edit-model" data-pid="${esc(m.pid)}" data-mid="${esc(m.id)}" title="编辑模型" aria-label="编辑 ${esc(m.id)}">${icon("edit")}</button><button class="icon-button danger" data-action="delete-model" data-write data-pid="${esc(m.pid)}" data-mid="${esc(m.id)}" title="删除模型" aria-label="删除 ${esc(m.id)}">${icon("trash")}</button></div></td></tr>`).join("") : `<tr><td colspan="7">${empty("grid", allModels().length ? "没有找到匹配的模型" : "从第一个模型开始", allModels().length ? "试试其他关键词，或清除筛选条件。" : "连接供应商后，手动添加或检测远端模型。", allModels().length ? '<button data-action="clear-filters">清除筛选</button>' : `<button class="primary" data-action="${Object.keys(state.providers).length ? "add-model" : "add-provider"}">${icon("plus")}${Object.keys(state.providers).length ? "添加模型" : "添加供应商"}</button>`)}</td></tr>`;
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
  $("#content").innerHTML = heading(`<button class="primary" data-action="add-provider">${icon("plus")}添加供应商</button>`) + stats() + `<div class="filter-bar" style="padding:0;border:0"><div class="search-box">${icon("search")}<input type="search" id="provider-search" placeholder="搜索供应商名称或 ID…" aria-label="搜索供应商" value="${esc(state.providerQuery)}" /></div><span class="filter-summary">密钥不在列表中展示</span></div><div class="provider-grid" id="provider-cards"></div><div class="hint-line">${icon("info")}模型检测只查询远端列表，不会删除或覆盖已配置的模型。</div>`;
  renderProviderCards();
}
function renderProviderCards() {
  const q = state.providerQuery.trim().toLocaleLowerCase();
  const entries = Object.entries(state.providers).filter(([pid]) => `${pid} ${providerName(pid)}`.toLocaleLowerCase().includes(q));
  $("#provider-cards").innerHTML = entries.map(([pid, p]) => {
    const color = providerColor(pid);
    return `<article class="card provider-card"><div class="provider-card-head"><div class="avatar" style="--avatar-bg:${color[0]};--avatar-color:${color[1]}">${esc(providerName(pid).slice(0, 1).toUpperCase())}</div><div><h3>${esc(providerName(pid))}</h3><div class="provider-id">${esc(pid)}</div></div><span class="tag ${p.enabled !== false ? "green" : ""}">${p.enabled !== false ? "已启用" : "已停用"}</span></div><div class="provider-url">${icon("link")}<span title="${esc(p.baseUrl)}">${esc(p.baseUrl || "未配置端点")}</span></div><div class="provider-meta"><span>${icon("grid")}${p.models?.length || 0} 个模型</span><span>${icon("key")}${p.apiKey ? "密钥已配置" : "未配置密钥"}</span>${p.anthropicBaseUrl ? '<span class="tag purple">Claude 兼容</span>' : ""}${p.targets?.length ? `<span class="tag" title="仅同步到：${esc(p.targets.join(", "))}">平台受限</span>` : ""}</div><div class="provider-card-footer"><button class="ghost small" data-action="provider-models" data-pid="${esc(pid)}">查看模型${icon("arrow")}</button><div class="actions"><button class="small" data-action="discover" data-write data-pid="${esc(pid)}">${icon("sparkles")}检测模型</button><button class="small" data-action="edit-provider" data-pid="${esc(pid)}">${icon("edit")}配置</button></div></div></article>`;
  }).join("") + (q && !entries.length ? `<div class="card">${empty("search", "没有找到供应商", "尝试其他关键词。")}</div>` : "") + `<button class="card provider-add-card" data-action="add-provider">${icon("plus")}连接新的供应商<small>OpenAI / Anthropic 兼容服务</small></button>`;
}

function renderSync() {
  const pending = state.status.pendingSync;
  $("#content").innerHTML = heading(`<button data-action="detect-sync" data-write>${icon("refresh")}检测变更并预览</button><button class="primary" data-action="run-sync" data-write id="sync-primary">${icon("sync")}预览已选平台</button>`) + `
    <div class="notice ${pending ? "warn" : ""}">${icon(pending ? "clock" : "shield")}<div><strong>${pending ? "本地配置有更新，等待同步到平台" : "本地配置已同步到所有启用平台"}</strong><br />${pending ? "以 Hub 为准写入已选平台；未选平台保持不变。" : "你可以再次同步，或先检测客户端配置是否有变化。"} 写入前会备份原配置。</div></div>
    <section id="sync-preview" class="card sync-preview" aria-live="polite" hidden></section>
    <section class="card modelsync-card" id="modelsync-card" aria-label="多 agent 使用的模型"></section>
    <div class="sync-layout"><section class="card"><div class="card-heading"><div><h2>同步到哪些平台？</h2><p>dsh 桌面版与 CLI 联动；官方授权不在管理范围内。</p></div><span class="tag" id="target-count"></span></div><div class="target-grid">${Object.entries(state.status.targets || {}).map(([name, t]) => {
      const [title, short, desc] = TARGETS[name] || [name, name.slice(0, 2), t.notes || "模型配置"];
      const readonly = !!t.aliasOf || !t.supported;
      const enabled = t.aliasOf ? state.targetDraft[t.aliasOf] : state.targetDraft[name];
      const existing = (t.files || []).filter(f => f.exists).length;
      return `<label class="target-card ${enabled ? "enabled" : ""} ${readonly ? "readonly" : ""}" data-target="${esc(name)}"><div class="target-top"><span class="target-symbol">${esc(short)}</span><h3>${esc(title)}</h3><input type="checkbox" class="target-check" data-target="${esc(name)}" aria-label="同步到 ${esc(title)}" ${enabled ? "checked" : ""} ${readonly ? "disabled" : ""} /></div><div class="target-desc">${esc(desc)}</div><div class="target-state">${icon(readonly ? "link" : "shield")}${t.aliasOf ? `跟随 ${esc(t.aliasOf)}` : !t.supported ? "不自动写入" : `${existing} / ${t.files?.length || 0} 个配置文件已存在`}</div></label>`;
    }).join("")}</div>${state.status.targets?.claude ? `<div class="target-config"><div><strong>Claude Code 使用的供应商</strong><p>仅展示已启用且具有 Anthropic 兼容端点的供应商。</p></div><select id="claude-provider" aria-label="Claude Code 激活供应商">${(state.status.anthropicCapable || []).includes(state.activeDraft) ? "" : `<option value="">请选择兼容供应商</option>`}${(state.status.anthropicCapable || []).map(pid => `<option value="${esc(pid)}" ${state.activeDraft === pid ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select></div>` + (() => {
      const pid = state.activeDraft;
      const opts = claudeModelOptions(pid);
      const cur = claudeModelOf(pid);
      const draft = state.modelDraft || cur;
      if (!pid) return "";
      return `<div class="target-config"><div><strong>Claude 模型</strong><p>从 ${esc(providerName(pid))} 拉取可用模型；切换后点「保存平台设置」并同步生效。<b>未在列表中的模型</b>可直接在供应商配置里手填。</p></div><div class="model-switch">${state.modelLoading ? `<span class="spinner"></span>` : `<select id="claude-model" aria-label="Claude 模型">${cur ? "" : `<option value="">从供应商检测模型</option>`}${opts.map(m => `<option value="${esc(m)}" ${draft === m ? "selected" : ""}>${esc(m)}</option>`).join("")}</select><button class="small ghost" type="button" data-action="load-claude-models" data-write data-pid="${esc(pid)}" title="向供应商查询可用模型">${icon("sparkles")}获取模型</button>`}</div></div>`;
    })() : ""}<div class="table-footer"><span id="target-draft-status"></span><div class="actions"><button class="small ghost" data-action="reset-targets" id="reset-targets">撤销更改</button><button class="small" data-action="save-targets" data-write id="save-targets">保存平台设置</button></div></div></section>
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
  const resolvedModel = modelSyncResolved(tier ? "claude" : target)?.model || "";
  const current = explicit || resolvedModel;
  const manual = !!custom[customKey(scope, target, tier)];
  const typed = manual ? (custom[customKey(scope, target, tier)] === true ? "" : String(custom[customKey(scope, target, tier)])) : "";
  const choices = modelChoices(provider);
  // 已选中的值即使不在候选里也要能看见，否则换供应商后当前值会消失。
  if (current && !choices.includes(current)) choices.push(current);
  if (typed && !choices.includes(typed)) choices.push(typed);
  const id = `ms-model-${scope}-${target}${tier ? `-${tier}` : ""}`;
  // 显式选中当前值；解析出来的"当前值"只做标记，不写进草稿。
  const label = v => esc(v === current ? `${v}${explicit ? "" : "（当前）"}` : v);
  const options = `<option value="">跟随全局</option>${choices.map(v => `<option value="${esc(v)}" ${v === current ? "selected" : ""}>${label(v)}</option>`).join("")}<option value="${CUSTOM_MODEL}" ${manual ? "selected" : ""}>手动填写模型 ID…</option>`;
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
  const found = { minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "极高", max: "最高" }[value];
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
  // 没有 override 时，档位跟随活跃供应商自己声明的 claudeModels；这里如实显示后端解析出来的值。
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
  const ids = enabledProviders();
  if (value && !ids.includes(value)) ids.unshift(value);
  const id = `ms-provider-${scope}-${target}${tier ? `-${tier}` : ""}`;
  return `<select id="${esc(id)}" data-ms-provider data-scope="${esc(scope)}" data-target="${esc(target)}" ${tier ? `data-tier="${esc(tier)}"` : ""} aria-label="供应商"><option value="">跟随全局</option>${ids.map(pid => `<option value="${esc(pid)}" ${pid === value ? "selected" : ""}>${esc(providerName(pid))}</option>`).join("")}</select>`;
}
function clientRow(name, label) {
  const draft = ensureModelSyncDraft();
  const entry = draft.overrides?.[name] || {};
  const resolved = modelSyncResolved(name);
  const codex = name === "codex";
  const mode = codex ? `<div class="codex-mode"><label class="field"><span>Codex 请求线路</span><select id="codex-mode" aria-label="Codex 请求线路" ${state.status?.targets?.codex ? "" : "disabled"}><option value="official" ${state.codexMode === "official" ? "selected" : ""}>官方 · 恢复原模型与推理强度</option><option value="third-party" ${state.codexMode === "third-party" ? "selected" : ""}>第三方 · 使用 Hub 模型设置</option></select></label>${state.codexMode === "third-party" ? `<label class="check-label codex-optin"><input id="codex-optin" type="checkbox" ${state.codexOptIn ? "checked" : ""} />我明确选择第三方线路，Codex 请求将发送到所选供应商</label>` : `<p class="ms-client-note">同步时恢复 Hub 首次切换第三方前保存的官方 model、model_provider 和 reasoning effort；没有保存记录时仅接受现有官方配置；现有第三方线路会拒绝恢复，需先在 Codex 中恢复官方配置。不会更改 auth.json 登录态。</p>`}</div>` : "";
  if (codex && state.codexMode !== "third-party") {
    return `<div class="ms-client" data-client="codex"><div class="ms-client-head"><strong>Codex</strong><span class="tag green">官方线路</span></div>${mode}<p class="ms-client-note">第三方模型覆盖保留在 Hub 中，官方模式不会将这些模型写入 Codex。${state.status?.targets?.codex ? "勾选 Codex 平台后预览并应用，线路才会生效。" : "当前未配置 Codex 同步目标。"}</p></div>`;
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
  // Claude Code 只有在全局供应商具备 Anthropic 端点时才会跟随全局主模型，否则保持自己的 claudeModels。
  const follows = !!resolved?.model && resolved.provider === draft.default?.provider && resolved.model === draft.default?.model;
  return `<div class="ms-client ${overridden ? "overridden" : ""}" data-client="claude">
    <div class="ms-client-head"><strong>Claude Code</strong>${overridden ? `<span class="tag amber">单独指定</span>` : follows ? `<span class="tag green">跟随全局</span>` : `<span class="tag">独立设置</span>`}${tierCount ? `<span class="tag purple">${tierCount} 个档位单独指定</span>` : ""}</div>
    ${body}
    <p class="ms-client-note">${icon("info")}只有全局供应商具备 Anthropic 端点时，Claude Code 才跟随全局主模型；否则保持活跃供应商自己的 claudeModels / model。预览与同步按这条解析规则选择实际供应商，不会改写 Hub 中的 activeProvider。</p>
    <details class="ms-tiers" data-ms-details="claude" ${state.modelSyncOpen.has("claude") || tierCount ? "open" : ""}>
      <summary>展开档位设置（model / opus / sonnet / haiku / subagent）</summary>
      <p class="ms-tier-note">${icon("info")}Claude Code 的档位分别对应 ANTHROPIC_MODEL 与 ANTHROPIC_DEFAULT_*_MODEL；保存后写入激活供应商的 claudeModels。点「使用默认档位」的档位会跟随 Claude 行的主模型。</p>
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
      <p class="ms-hint">留空时其他客户端保留现有强度；Codex 第三方模式会移除旧强度，官方模式恢复之前保存的值。</p>
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
  if (state.codexMode === "third-party" && !state.codexOptIn) throw new Error("请先勾选 Codex 第三方线路确认，或改回官方线路。");
  // 平台设置与模型设置写在不同接口；这里顺手把未保存的模型设置一起落盘，避免随后的 reload 丢掉它。
  if (modelSyncDirty() && !state.modelSyncError) await saveModelSync();
  const updates = Object.fromEntries(effectiveTargets().map(([name]) => [name, { enabled: state.targetDraft[name] }]));
  if (updates.codex) updates.codex.mode = state.codexMode;
  if (state.activeDraft && state.activeDraft !== state.status.targets?.claude?.activeProvider) updates.claude.activeProvider = state.activeDraft;
  await api("PUT", "/api/targets", { targets: updates });
  // 模型切换落在 provider 上（claudeModels / claudeSettings.model），不是平台配置里。
  if (modelDirty()) {
    const pid = state.activeDraft, p = state.providers[pid];
    const model = state.modelDraft;
    // 各档位（opus/sonnet/haiku/subagent）统一指向所选模型，与既有配置风格一致
    const claudeModels = Object.fromEntries(Object.entries(p.claudeModels || {}).map(([k]) => [k, model]));
    if (!claudeModels.model) claudeModels.model = model;
    const body = { claudeModels };
    if (p.claudeSettings && "model" in p.claudeSettings) body.claudeSettings = { ...p.claudeSettings, model };
    await api("PUT", pathFor(pid), body);
    Object.assign(state.providers[pid], body);
  }
  // 先更新本地基线，reload 就不会误保留已经保存的草稿。
  for (const [name, value] of Object.entries(updates)) Object.assign(state.status.targets[name], value);
  state.modelDraft = "";
  state.modelOptions = [];
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
  if (selected.includes("claude") && !(state.status.anthropicCapable || []).includes(state.activeDraft)) { toast("请先选择 Claude Code 的兼容供应商，或取消该平台", "error"); $("#claude-provider")?.focus(); return; }
  if (state.codexMode === "third-party" && !state.codexOptIn) { toast("请先勾选 Codex 第三方线路确认，或改回官方线路。", "error"); $("#codex-optin")?.focus(); return; }
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
  $("#editor-eyebrow").textContent = type === "discover" ? "MODEL DISCOVERY" : type === "provider" ? "PROVIDER SETTINGS" : "MODEL SETTINGS";
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
// 向激活的 Claude 供应商查询可用模型，填入模型选择器。
// 复用 /api/providers/{pid}/discover —— Anthropic 风格走 {anthropicBaseUrl}/v1/models。
async function loadClaudeModels(pid) {
  if (state.busy || !pid) return;
  state.modelLoading = true;
  renderSync();
  try {
    const result = await api("GET", `${pathFor(pid)}/discover`);
    state.modelOptions = (result.models || [])
      .filter((m, i, list) => m.id && list.findIndex(v => v.id === m.id) === i)
      .map(m => m.id)
      .sort();
    if (!state.modelOptions.length) toast("该供应商没有返回可用模型，可手填模型 ID。", "error");
  } catch (e) {
    state.modelOptions = [];
    toast(`获取模型失败：${e.message}`, "error");
  } finally {
    state.modelLoading = false;
    renderSync();
  }
}
// 供应商表单的 agent 默认模型候选：本供应商模型库 + Claude 映射 + 全局模型选项 + 当前值。
function providerModelChoices(pid, p) {
  const ids = new Set([
    ...modelIds(p.models),
    p.claudeModels?.model, p.claudeSettings?.model, p.agentModel,
    ...modelIds(state.modelSync?.options?.[pid]),
  ].filter(Boolean));
  return ["", ...[...ids].sort()];
}
function openProvider(pid) {
  if (state.busy) return;
  const p = state.providers[pid] || {}, edit = !!pid;
  const protocols = ["openai-completions", "openai-responses", "openai-chat-completions", "anthropic-messages"];
  showEditor("provider", edit ? "供应商连接配置" : "连接新的供应商", `<p class="form-intro">${edit ? "修改连接信息不会覆盖该供应商已有的模型列表。" : "先配置连接，再手动添加或检测模型。供应商 ID 用于各平台的配置映射。"}</p><div class="form-grid"><label class="field"><span>供应商 ID ${edit ? "" : '<b class="required">*</b>'}</span><input name="id" value="${esc(pid || "")}" placeholder="例如 deepseek" pattern="[a-zA-Z0-9][a-zA-Z0-9_\\-]*" required ${edit ? "readonly" : ""} /><small>字母、数字、下划线或连字符</small></label><label class="field"><span>显示名称 <b class="required">*</b></span><input name="displayName" value="${esc(p.displayName || p.name || "")}" placeholder="例如 DeepSeek" required /></label><label class="field full"><span>API 地址 · Base URL <b class="required">*</b></span><input name="baseUrl" type="url" value="${esc(p.baseUrl || "")}" placeholder="https://api.example.com/v1" required /></label><label class="field full"><span>API Key</span><div class="password-wrap"><input name="apiKey" type="password" value="${esc(p.apiKey || "")}" placeholder="输入 API 密钥；或在下方配置环境变量" autocomplete="off" /><button class="icon-button" type="button" data-action="toggle-key" aria-label="显示密钥" title="显示密钥">${icon("eye")}</button></div><small>保存在本机 providers.json 中，请勿共享配置文件。</small></label><label class="field full"><span>API 协议</span><select name="api">${protocols.map(v => `<option value="${v}" ${v === (p.api || protocols[0]) ? "selected" : ""}>${v}</option>`).join("")}</select></label>${edit ? `<label class="check-label full"><input name="enabled" type="checkbox" ${p.enabled !== false ? "checked" : ""} />启用供应商，供平台同步使用</label>` : ""}</div><details class="form-section"><summary>高级设置 · Claude 兼容端点与备注</summary><div class="form-grid">${edit ? `<label class="field full"><span>内部名称</span><input name="name" value="${esc(p.name || pid)}" required /></label>` : ""}<label class="field full"><span>Anthropic Base URL</span><input name="anthropicBaseUrl" type="url" value="${esc(p.anthropicBaseUrl || "")}" placeholder="https://api.example.com/anthropic" /><small>配置后可在平台同步中供 Claude Code 选择。</small></label><label class="field full"><span>备注</span><input name="notes" value="${esc(p.notes || "")}" placeholder="可选，例如套餐或网关说明" /></label><label class="field full"><span>同步到哪些平台</span><div class="check-group">${PROVIDER_TARGETS.map(([name, label]) => `<label class="check-label"><input type="checkbox" name="targets" value="${esc(name)}" ${(p.targets || []).includes(name) ? "checked" : ""} />${esc(label)}</label>`).join("")}</div><small>全不勾选 = 同步到所有平台（默认）。只提供 Anthropic 端点的网关通常只勾 Claude Code，避免污染 pi / dsh 等其他客户端。</small></label><label class="field full"><span>agent 默认模型 · agentModel</span><select name="agentModel">${providerModelChoices(pid, p).map(id => `<option value="${esc(id)}" ${id === (p.agentModel || "") ? "selected" : ""}>${esc(id)}</option>`).join("")}</select><small>可选。作为该供应商在客户端没有单独指定模型时的回退值；留空表示不登记。</small></label></div></details><details class="form-section"><summary>Codex 第三方接入 · Responses 与授权</summary><div class="form-grid"><label class="field full"><span>Responses 兼容性确认</span><select name="codexWireApi"><option value="">尚未确认（API 协议为 openai-responses 时也可用）</option><option value="responses" ${p.codexWireApi === "responses" ? "selected" : ""}>已确认此供应商支持 Responses API</option></select><small>只有明确支持 Responses 的供应商才能写入 Codex；不会自动用 chat 协议替代。</small></label><label class="field full"><span>API Key 环境变量名 · apiKeyEnv</span><input name="apiKeyEnv" value="${esc(p.apiKeyEnv || "")}" pattern="[A-Za-z_][A-Za-z0-9_]*" placeholder="例如 VENDOR_API_KEY" autocomplete="off" /><small>优先使用变量名引用；变量值由你在本机设置。这里只保存名称，不读取或展示变量值。</small></label><label class="field full"><span>Codex 专用 Key · openaiApiKey</span><input type="password" name="openaiApiKey" value="${esc(p.openaiApiKey || "")}" autocomplete="off" /><small>可选。未使用环境变量时，优先于通用 API Key；会保存在本机配置和备份中。</small></label></div></details><details class="form-section"><summary>自定义接入 · Claude 声明式透传与目标平台</summary><div class="form-grid"><label class="field full"><span>Claude 模型映射 · claudeModels</span><textarea name="claudeModels" rows="4" class="mono" spellcheck="false" placeholder='{"model":"claude-sonnet-4-6","opus":"...","sonnet":"...","haiku":"...","subagent":"..."}'>${esc(jsonText(p.claudeModels))}</textarea><small>JSON 对象；映射到 ANTHROPIC_MODEL / ANTHROPIC_DEFAULT_*_MODEL / CLAUDE_CODE_SUBAGENT_MODEL。</small></label><label class="field full"><span>额外环境变量 · claudeEnv</span><textarea name="claudeEnv" rows="3" class="mono" spellcheck="false" placeholder='{"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS":"1","ENABLE_TOOL_SEARCH":"true"}'>${esc(jsonText(p.claudeEnv))}</textarea><small>JSON 对象；原样写入 settings.json 的 env。</small></label><label class="field full"><span>顶层 settings 字段 · claudeSettings</span><textarea name="claudeSettings" rows="5" class="mono" spellcheck="false" placeholder='{"model":"claude-sonnet-4-6","effortLevel":"high","permissions":{"allow":[]}}'>${esc(jsonText(p.claudeSettings))}</textarea><small>JSON 对象；原样写入 settings.json 顶层（model / effortLevel / permissions 等）。切换供应商时，hub 只收回自己写过的键，你手工配置的字段不受影响。</small></label></div></details>`, formFooter(edit ? "保存配置" : "添加供应商", edit ? `<button type="button" class="danger" data-action="delete-provider" data-pid="${esc(pid)}">${icon("trash")}删除</button>` : ""), { pid });
}
function openModel(pid, mid) {
  if (state.busy) return;
  if (!Object.keys(state.providers).length) { openProvider(); toast("先连接一个供应商，再添加模型。"); return; }
  pid = pid || (state.filters.provider !== "all" ? state.filters.provider : Object.keys(state.providers)[0]);
  const m = (state.providers[pid]?.models || []).find(m => m.id === mid) || {};
  const edit = mid !== undefined;
  const inputs = [...new Set(["text", "image", "audio", "video", ...(m.input || [])])];
  showEditor("model", edit ? "编辑模型" : "添加新的模型", `<p class="form-intro">${edit ? "保留高级配置，只更新你在此修改的模型字段。" : "模型 ID 必须与供应商接口中的标识一致。不确定时，可先在供应商页检测模型。"}</p><div class="form-grid"><label class="field full"><span>所属供应商</span><select name="provider" ${edit ? "disabled" : ""}>${Object.keys(state.providers).map(id => `<option value="${esc(id)}" ${id === pid ? "selected" : ""}>${esc(providerName(id))} (${esc(id)})</option>`).join("")}</select></label><label class="field full"><span>模型 ID <b class="required">*</b></span><input class="mono" name="id" value="${esc(m.id || "")}" placeholder="例如 deepseek-chat 或 vendor/model-name" required /></label><label class="field full"><span>显示名称</span><input name="name" value="${esc(m.name || "")}" placeholder="可选，默认使用模型 ID" /></label><label class="field"><span>上下文窗口 · tokens</span><input name="contextWindow" type="number" min="1" max="100000000" step="1" value="${Number(m.contextWindow) || 128000}" required /></label><label class="field"><span>最大输出 · tokens</span><input name="maxTokens" type="number" min="1" max="100000000" step="1" value="${Number(m.maxTokens) || 32768}" required /></label><fieldset class="field full"><legend>输入类型</legend><div class="check-group">${inputs.map(v => `<label class="check-label"><input type="checkbox" name="input" value="${esc(v)}" ${(m.input || ["text"]).includes(v) ? "checked" : ""} />${esc(({ text: "文本", image: "图片", audio: "音频", video: "视频" })[v] || v)}</label>`).join("")}</div></fieldset><label class="check-label full"><input type="checkbox" name="reasoning" ${m.reasoning !== false ? "checked" : ""} />支持推理 / 思考</label></div>`, formFooter(edit ? "保存模型" : "添加模型"), { pid, mid });
}

async function discover(pid) {
  if (state.busy) return;
  await busy("正在检测远端模型…", async () => {
    const result = await api("GET", `${pathFor(pid)}/discover`);
    const existing = new Set((state.providers[pid].models || []).map(m => m.id));
    state.discovered = (result.models || []).filter((m, i, list) => m.id && list.findIndex(v => v.id === m.id) === i);
    const added = state.discovered.filter(m => !existing.has(m.id)).length;
    showEditor("discover", `检测模型 · ${providerName(pid)}`, `<div class="notice" style="margin-bottom:16px">${icon("shield")}仅导入勾选的新模型。不会覆盖已有参数，也不会移除未检测到的模型。</div><div class="search-box" style="max-width:none">${icon("search")}<input id="discover-search" type="search" aria-label="搜索检测结果" placeholder="搜索检测到的模型…" /></div><div class="discover-toolbar"><label class="check-label"><input type="checkbox" id="discover-all" ${added ? "checked" : "disabled"} />选择当前可见的新模型</label><span>检测到 ${state.discovered.length} 个 · ${added} 个新模型</span></div><div class="discover-list" id="discover-list">${state.discovered.map((m, index) => `<label class="discover-row ${existing.has(m.id) ? "existing" : ""}" data-search="${esc(`${m.id} ${m.name || ""}`.toLocaleLowerCase())}"><input class="discover-check" type="checkbox" name="discovered" value="${index}" ${existing.has(m.id) ? "disabled" : "checked"} /><div><div class="mono">${esc(m.id)}</div>${m.name && m.name !== m.id ? `<small>${esc(m.name)}</small>` : ""}</div><span class="tag ${existing.has(m.id) ? "" : "green"}">${existing.has(m.id) ? "已添加" : "新模型"}</span></label>`).join("") || empty("search", "没有检测到模型", "请检查该供应商的接口与授权。")}</div>`, formFooter(`导入 ${added} 个新模型`), { pid });
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

async function submitEditor(event) {
  event.preventDefault();
  if (state.busy) return;
  const fd = new FormData(event.target), editor = { ...state.editor };
  $("#editor-error").hidden = true;
  const value = key => String(fd.get(key) || "").trim();
  let payload;
  if (editor.type === "provider") {
    payload = { id: value("id"), name: editor.pid ? value("name") : value("displayName"), displayName: value("displayName"), baseUrl: value("baseUrl"), apiKey: value("apiKey"), openaiApiKey: value("openaiApiKey"), apiKeyEnv: value("apiKeyEnv"), codexWireApi: value("codexWireApi") || null, api: value("api"), anthropicBaseUrl: value("anthropicBaseUrl"), notes: value("notes"), ...(editor.pid ? { enabled: fd.has("enabled") } : {}) };
    // 自定义供应商的三个声明式字段：空文本 = 清除，非法 JSON 在此拦截并提示
    for (const [key, label] of [["claudeModels", "Claude 模型映射"], ["claudeEnv", "额外环境变量"], ["claudeSettings", "顶层 settings 字段"]]) {
      let parsed = null;
      try { parsed = parseJsonField(fd.get(key), label); }
      catch (err) { $("#editor-error").textContent = err.message; $("#editor-error").hidden = false; return; }
      if (parsed) payload[key] = parsed;
      else if (editor.pid) payload[key] = {};
    }
    if (editor.pid || fd.getAll("targets").length) payload.targets = fd.getAll("targets");
    // 编辑时始终发送 agentModel，让「留空」表示清除；新增时留空就不写这个字段。
    if (editor.pid || value("agentModel")) payload.agentModel = value("agentModel");
  }
  if (editor.type === "model") {
    payload = { id: value("id"), name: value("name"), contextWindow: Number(value("contextWindow")), maxTokens: Number(value("maxTokens")), input: fd.getAll("input"), reasoning: fd.has("reasoning") };
    if (!payload.input.length) { $("#editor-error").textContent = "请至少选择一种输入类型。"; $("#editor-error").hidden = false; return; }
  }
  if (editor.type === "discover") {
    payload = { mode: "merge", models: fd.getAll("discovered").map(i => state.discovered[Number(i)]) };
    if (!payload.models.length) return;
  }
  await busy("正在保存本地配置…", async () => {
    let result;
    if (editor.type === "provider") result = await api(editor.pid ? "PUT" : "POST", editor.pid ? pathFor(editor.pid) : "/api/providers", payload);
    if (editor.type === "model") {
      const pid = editor.mid !== undefined ? editor.pid : value("provider");
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
async function deleteModel(pid, mid) {
  if (!await confirmDialog("删除这个模型？", "仅从本地模型库移除。下次同步时会从客户端配置中移除；写入前会自动备份。", [`${providerName(pid)} / ${mid}`])) return;
  await busy("正在删除模型…", async () => { await api("DELETE", `${pathFor(pid)}/models/${encodeURIComponent(mid)}`); await refreshAfterWrite("模型已删除，等待同步到平台。"); });
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
    models.forEach(m => { if (!groups.has(m.pid)) groups.set(m.pid, []); groups.get(m.pid).push(m.id); });
    let removed = 0;
    try {
      for (const [pid, ids] of groups) { const result = await api("POST", `${pathFor(pid)}/models/batch-delete`, { ids }); removed += result.removed; }
    } catch (e) { await reload(); throw new Error(`已删除 ${removed} 个模型；剩余操作失败：${e.message}`); }
    state.selection.clear();
    await refreshAfterWrite(`已删除 ${removed} 个模型，等待同步到平台。`);
  });
}
async function deleteProvider(pid) {
  if (!await confirmDialog(`删除 ${providerName(pid)}？`, `将删除该供应商的 ${state.providers[pid].models?.length || 0} 个模型，并立即执行现有客户端清理逻辑。这与删除单个模型不同，不需要等待下次同步。配置会自动备份。`, [pid])) return;
  await busy("正在移除供应商…", async () => { await api("DELETE", pathFor(pid)); closeEditor(); await refreshAfterWrite("供应商已移除。请同步并重启客户端，确保所有平台配置对齐。"); });
}

async function navigate(page) {
  if (!PAGES[page]) page = "models";
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
    else if (action === "edit-provider") openProvider(pid);
    else if (action === "add-model") openModel();
    else if (action === "edit-model") openModel(pid, mid);
    else if (action === "close-editor") await requestCloseEditor();
    else if (action === "delete-provider") await deleteProvider(pid);
    else if (action === "delete-model") await deleteModel(pid, mid);
    else if (action === "bulk-delete") await bulkDelete();
    else if (action === "discover") await discover(pid);
    else if (action === "load-claude-models") await loadClaudeModels(pid);
    else if (action === "clear-selection") { state.selection.clear(); renderModelRows(); }
    else if (action === "filter-provider") { state.filters.provider = pid; state.modelPage = 1; renderModels(); }
    else if (action === "clear-filters") { state.filters = { query: "", provider: "all", capability: "all" }; state.modelPage = 1; renderModels(); }
    else if (action === "prev-page" || action === "next-page") { state.modelPage += action === "next-page" ? 1 : -1; renderModelRows(); }
    else if (action === "go-sync") location.hash = "sync";
    else if (action === "provider-models") { state.filters = { query: "", provider: pid, capability: "all" }; state.modelPage = 1; location.hash = "models"; }
    else if (action === "toggle-key") { const input = $('#editor-form [name="apiKey"]'); input.type = input.type === "password" ? "text" : "password"; button.setAttribute("aria-label", input.type === "password" ? "显示密钥" : "隐藏密钥"); button.title = button.getAttribute("aria-label"); }
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
  } catch (e) { toast(e.message, "error"); }
});
document.addEventListener("input", event => {
  const el = event.target;
  if (el.id === "model-search") { state.filters.query = el.value; state.modelPage = 1; renderModelRows(); }
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
  if (el.id === "capability-filter") { state.filters.capability = el.value; state.modelPage = 1; renderModelRows(); }
  if (el.classList.contains("model-check")) { if (el.checked) state.selection.add(el.dataset.key); else state.selection.delete(el.dataset.key); renderSelection(); }
  if (el.id === "select-page") { visibleModels().forEach(m => el.checked ? state.selection.add(m.key) : state.selection.delete(m.key)); renderModelRows(); }
  if (el.classList.contains("target-check") && !el.disabled) { invalidatePreview(); state.targetDraft[el.dataset.target] = el.checked; updateTargetDraftUI(); }
  if (el.id === "codex-mode") {
    invalidatePreview(); state.codexMode = el.value; state.codexOptIn = false; renderModelSyncCard(); updateTargetDraftUI();
  }
  if (el.id === "codex-optin") { invalidatePreview(); state.codexOptIn = el.checked; updateTargetDraftUI(); }
  if (el.id === "claude-provider") {
    invalidatePreview();
    // 换供应商后，模型下拉要回到新供应商当前在用的模型，缓存的候选列表失效
    state.activeDraft = el.value; state.modelDraft = ""; state.modelOptions = [];
    renderSync(); updateTargetDraftUI();
  }
  if (el.id === "claude-model") { invalidatePreview(); state.modelDraft = el.value; updateTargetDraftUI(); }
  if (el.classList.contains("discover-check")) updateDiscoverSelection();
  if (el.id === "discover-all") { $$(".discover-row:not([hidden]) .discover-check:not(:disabled)").forEach(c => { c.checked = el.checked; }); updateDiscoverSelection(); }
  if (el.dataset.msProvider !== undefined || el.dataset.msSelect !== undefined || el.dataset.msEffort !== undefined) {
    const { scope, target, tier = "" } = el.dataset;
    const draft = ensureModelSyncDraft();
    const entry = scope === "default" ? draft.default : scope === "tiers" ? (draft.overrides?.claude?.tiers?.[tier] || {}) : (draft.overrides?.[target] || {});
    delete state.modelSyncCustom[customKey(scope, target, tier)];
    if (el.dataset.msProvider !== undefined) {
      // override 换供应商后，不在新供应商候选里的模型 ID 会清空（后端要求 provider/model 成对）；
      // 全局主模型保留原值，让用户自己决定是否改成新供应商的模型。
      const stale = scope !== "default" && entry.model && !modelChoices(el.value).includes(entry.model);
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

state.page = PAGES[location.hash.slice(1)] ? location.hash.slice(1) : "models";
reload().catch(e => {
  renderChrome();
  $("#content").innerHTML = `<div class="card">${empty("alert", "暂时无法加载工作空间", e.message, '<button data-action="retry" class="primary">重试连接</button>')}</div>`;
  const retry = $('[data-action="retry"]');
  retry.onclick = () => busy("正在重新连接…", reload);
});
