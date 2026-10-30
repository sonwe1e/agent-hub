"use strict";

// These pages operate through the same API/desktop bridge as the workbench.
const workFeatures = {library:null, sessions:null, quota:null, loading:false,
  errors:{}, discovery:null, preview:null, draft:null, targets:null, summaryRoutes:null,
  summaryMaterial:null, summaryPreview:null, updatedAt:0, quotaDraft:null, instances:[], service:null, loadPromise:null};
const workAgentNames = {claude:"Claude Code",codex:"Codex",pi:"pi",dsh:"dsh",opencode:"OpenCode"};
const workAgentIds = Object.keys(workAgentNames);
const workStatusText = {unpublished:"未发布",current:"与发布版一致",changed:"主来源已修改",
  pending:"待更新",conflict:"有冲突",error:"读取失败",source:"主来源",unchanged:"无需更改",
  create:"新增",update:"更新"};
const workSourceText = {unpublished:"尚未发布",current:"主来源与发布版本一致",changed:"主来源有未发布变化",error:"主来源读取失败"};
const workSessionTitleSourceText = {manual:"手工命名","native-title":"客户端标题","user-message":"用户消息节选","gateway-user-message":"网关请求消息节选"};

async function loadWorkFeatures(force=false) {
  while (workFeatures.loading) {
    if (!force || !workFeatures.loadPromise) return;
    await workFeatures.loadPromise;
  }
  if (!force && workFeatures.library && workFeatures.sessions && workFeatures.quota) return;
  workFeatures.loading = true;
  const paths = [["library","/api/capabilities"],["sessions","/api/sessions/recent"],["quota","/api/quota-monitor"],["service","/api/workspace/status"]];
  workFeatures.loadPromise = Promise.all(paths.map(async ([key,path])=>{
    try { workFeatures[key] = await api("GET",path); delete workFeatures.errors[key]; }
    catch (e) { workFeatures.errors[key] = e.message; }
  }));
  await workFeatures.loadPromise;
  workFeatures.loading = false;
  workFeatures.loadPromise = null;
  workFeatures.updatedAt = Date.now();
  if (["agents","sessions","library","settings"].includes(state.page)) renderPage();
}

// Status changes while Hub is minimized. Do not replace controls while the
// user is editing a dialog or a field in the main page.
setInterval(()=>{
  if (!state.loaded || workFeatures.loading || $("#editor-dialog")?.open || $("#confirm-dialog")?.open) return;
  if (!["agents","sessions","library","settings"].includes(state.page)) return;
  const active=document.activeElement;
  if (active?.matches?.("input,textarea,select") && active.closest("#content")) return;
  if (Date.now()-workFeatures.updatedAt >= 30000) loadWorkFeatures(true);
},30000);

function quotaBrief(data) {
  if (!data) return workFeatures.errors.quota || "正在读取额度状态";
  const accounts=Object.entries(data.accounts||{}).filter(([,a])=>a.connected);
  if (!accounts.length) return "暂无支持查询的已接入账号";
  return accounts.map(([id,a])=>{
    const label=id==="codex-chatgpt"?"Codex":id==="zcode-subscription"?"ZCode":id;
    if (!a.quota) return label+" · "+(a.error?.message||"额度未知");
    const nums=(a.quota.windows||[]).filter(w=>Number.isFinite(w.usedPercent));
    const remaining=nums.length ? Math.max(0,Math.min(...nums.map(w=>100-w.usedPercent))) : null;
    return label+" · "+(remaining==null?"额度未知":`最低剩余 ${fmt(remaining)}%`)+(a.stale?"（快照过期）":"");
  }).join("；");
}

function workSummaryHtml() {
  const sessions=workFeatures.sessions?.sessions||[];
  const last=sessions[0];
  const items=workFeatures.library?.items||[];
  const pending=items.filter(x=>x.pendingTransaction || ["unpublished","changed","error"].includes(x.sourceStatus) || Object.values(x.targetStatus||{}).some(s=>["unpublished","pending","conflict","error"].includes(s))).length;
  const workText=last ? `${workAgentNames[last.agent]||last.agent} · ${last.title||last.cwd||(last.sourceKind==="gateway-only"?"仅有网关调用记录":"原生会话")}` : workFeatures.errors.sessions||"暂无已索引会话";
  const libraryText=workFeatures.library ? (pending?`${pending} 项需要查看`:`${items.length} 项已管理`) : workFeatures.errors.library||"正在读取";
  return `<section class="card work-summary" aria-label="工作概览"><div class="work-summary-head"><h2>你的工作</h2><button class="small" data-feature="refresh">刷新状态</button></div>
    <div class="work-summary-grid">
      <div class="work-summary-cell"><strong>继续工作</strong><small>${esc(workText)}</small><a href="#sessions">查看最近会话 →</a></div>
      <div class="work-summary-cell"><strong>技能与指令</strong><small>${esc(libraryText)}</small><a href="#library">查看发布状态 →</a></div>
      <div class="work-summary-cell"><strong>订阅额度</strong><small>${esc(quotaBrief(workFeatures.quota))}</small><a href="#settings">查看额度与提醒 →</a></div>
    </div></section>`;
}

const workOriginalAgents = renderAgents;
renderAgents = function() {
  workOriginalAgents();
  const heading=$("#content .page-heading");
  if (heading) heading.insertAdjacentHTML("afterend", workSummaryHtml());
  if (!workFeatures.library && !workFeatures.loading && !workFeatures.errors.library) loadWorkFeatures();
};

function renderLibrary() {
  const list=workFeatures.library?.items||[];
  const syncProblems=(workFeatures.service?.autoResults||[]).filter(x=>x.ok===false||x.partial);
  const warning=workFeatures.service?.autoError || (syncProblems.length?`${syncProblems.length} 项自动同步有冲突或未完成；请查看目标状态并手动预览。`:"");
  const body=workFeatures.errors.library ? blank("能力库读取失败",workFeatures.errors.library) :
    !workFeatures.library ? blank("正在读取","请稍候。") :
    !list.length ? blank("还没有管理的内容","选择已有全局技能或指令作为主来源；导入不会改写客户端。") :
    `<div class="feature-list">${list.map(item=>`<div class="feature-row"><div class="feature-row-main">
      <strong>${esc(item.name)} ${tag(item.kind==="skill"?"技能":"全局指令")}</strong>
      <small class="feature-path">主来源：${esc(item.source)}</small>
      <small>${item.mode==="auto"?"自动同步":"手动发布"} · ${esc(workSourceText[item.sourceStatus]||item.sourceStatus||"状态未知")}${item.sourceError?" · "+esc(item.sourceError):""}</small>
      ${item.pendingTransaction?`<p class="notice warn">${item.pendingTransaction==="unknown"?"事务记录无法读取，请先检查能力库数据。":"存在中断事务，请打开“记录”恢复后再继续。"}</p>`:""}
      ${Object.entries(item.targetPending||{}).map(([a,p])=>`<p class="notice warn">${esc(workAgentNames[a]||a)} 的目标有未完成事务；请在原 Hub 数据目录 ${esc(p.root||"未知")} 恢复，不能强制覆盖。</p>`).join("")}
      ${Object.entries(item.autoWarnings||{}).flatMap(([a,notes])=>(notes||[]).map(note=>`<small class="feature-message">${esc(workAgentNames[a]||a)} · ${esc(note)}</small>`)).join("")}
      <div class="feature-status">${(item.targets||[]).map(a=>tag(`${workAgentNames[a]||a} · ${workStatusText[item.targetStatus?.[a]]||item.targetStatus?.[a]||"未知"}`,item.targetStatus?.[a]==="conflict"?"red":"")).join("")}</div>
      </div><div class="actions"><button data-feature="edit-item" data-id="${esc(item.id)}" ${item.pendingTransaction?"disabled":""}>设置</button><button data-feature="history" data-id="${esc(item.id)}">记录</button>${item.published?`<button data-feature="preview-published" data-id="${esc(item.id)}" ${item.pendingTransaction?"disabled":""}>分发已发布版本</button>`:""}<button class="primary" data-feature="preview" data-id="${esc(item.id)}" ${item.pendingTransaction?"disabled":""}>预览发布</button></div></div>`).join("")}</div>`;
  $("#content").innerHTML = dailyHeading("SKILLS & INSTRUCTIONS","技能与指令","一项内容指定一份主来源；发布前查看变化，端侧独立修改不会被自动覆盖。",`<button data-feature="discover">发现已有内容</button><button class="primary" data-feature="new-item">添加主来源</button>`) +
    `${warning?`<p class="notice warn">${esc(warning)}</p>`:""}<section class="card feature-group">${body}</section><p class="hint-line">${icon("shield")}只管理全局技能与指令。项目规则和 MCP 不在此处修改；已运行会话是否重新加载取决于客户端。发布快照与原文件备份保留在本机，不自动清理。</p>`;
}

function renderSessions() {
  const data=workFeatures.sessions, rows=data?.sessions||[];
  const groups=new Map();
  for (const row of rows) { const name=row.cwd||"项目目录未记录"; if (!groups.has(name)) groups.set(name,[]); groups.get(name).push(row); }
  const body=workFeatures.errors.sessions ? blank("读取会话失败",workFeatures.errors.sessions) :
    !data ? blank("正在读取","请稍候。") : !rows.length ? blank("还没有可用的会话","Hub 根据本机已索引的原生记录显示会话；如果采集未运行，请检查观测设置。") :
    [...groups].map(([cwd,items])=>`<section class="card feature-group"><div class="feature-group-title"><strong>${esc(cwd)}</strong></div>${items.map(s=>`<div class="feature-row"><div class="feature-row-main"><strong>${esc(s.title||"未命名会话")}</strong><small>${esc(workAgentNames[s.agent]||s.agent)} · ${esc(when(s.lastAt))}${workSessionTitleSourceText[s.titleSource]?" · "+esc(workSessionTitleSourceText[s.titleSource]):""}${s.parentSessionId?" · 子会话":""}</small>${s.sourceKind==="gateway-only"?'<p class="feature-message"><small>仅有网关调用记录，未关联原生会话</small></p>':""}</div>
      <div class="actions"><button data-feature="session-rename" data-agent="${esc(s.agent)}" data-session="${esc(s.sessionId)}" aria-label="命名 ${esc(workAgentNames[s.agent]||s.agent)} 会话">命名</button><button data-feature="handoff" data-agent="${esc(s.agent)}" data-session="${esc(s.sessionId)}">跨端交接</button><button class="primary" data-feature="resume-preview" data-agent="${esc(s.agent)}" data-session="${esc(s.sessionId)}" ${s.sourceKind==="gateway-only"?'disabled title="尚未关联原生会话，无法自动继续"':''}>原端继续</button></div></div>`).join("")}</section>`).join("");
  $("#content").innerHTML = dailyHeading("CONTINUE WORK","继续工作","按项目找回原生会话，或预览交接内容并选择另一端。",`<button data-feature="refresh">${icon("refresh")}刷新</button>`) + body +
    `<p class="hint-line">${icon("info")}列表来自本机索引；原端启动与实际恢复结果分开表示。跨端不会自动迁移隐藏上下文或客户端状态。</p>`;
}

function workQuotaHtml() {
  const data=workFeatures.quota, settings=data?.settings;
  if (!data) return `<section class="card feature-group"><div class="card-heading"><div><h2>额度提醒</h2><p>${esc(workFeatures.errors.quota||"正在读取…")}</p></div></div></section>`;
  const accounts=Object.entries(data.accounts||{}).map(([id,a])=>{
    const name=id==="codex-chatgpt"?"Codex / ChatGPT":id==="zcode-subscription"?"ZCode":id;
    const q=a.quota;
    return `<div class="feature-row"><div class="feature-row-main"><strong>${esc(name)} · ${a.connected?"已接入":"未接入"}</strong><small>${esc(q ? `${q.source} · 查询于 ${when(q.checkedAt)}${a.stale?" · 快照已过期":""}` : "尚无额度快照")}${a.error?" · "+esc(a.error.message):""}</small>
      ${q?`<div class="feature-status">${(q.windows||[]).map(w=>tag(`${w.name||"额度窗口"} · ${Number.isFinite(w.usedPercent)?`已用 ${fmt(w.usedPercent)}%`:"使用比例未知"}${w.resetsAt?" · 重置 "+when(w.resetsAt):w.expiresAt?" · 到期 "+when(w.expiresAt):""}`)).join("")}</div>`:""}</div></div>`;
  }).join("");
  const draft=workFeatures.quotaDraft||{enabled:String(settings.enabled),interval:String(settings.intervalSeconds/60),threshold:String(settings.thresholdRemainingPercent)};
  return `<section class="card feature-group" id="quota-monitor"><div class="card-heading"><div><h2>额度提醒</h2><p>仅显示可查询账号的上游快照；其他额度未知。限流不一定代表额度耗尽。</p></div><button data-feature="quota-refresh">立即查询</button></div>${accounts}
    <div class="feature-quota-settings"><label><span>后台检查</span><select id="work-quota-enabled"><option value="true" ${draft.enabled==="true"?"selected":""}>开启</option><option value="false" ${draft.enabled==="false"?"selected":""}>关闭</option></select></label>
    <label><span>间隔（分钟）</span><input id="work-quota-interval" type="number" min="1" max="1440" value="${esc(draft.interval)}"></label>
    <label><span>剩余提醒（%）</span><input id="work-quota-threshold" type="number" min="0" max="100" value="${esc(draft.threshold)}"></label>
    <button class="primary" data-feature="quota-save">保存提醒设置</button></div>
    ${(data.events||[]).slice(-3).reverse().map(e=>`<div class="feature-row"><div class="feature-row-main"><strong>${esc(e.title||"额度提醒")}</strong><small>${esc(e.message||"")} · ${esc(when(e.time))}</small></div></div>`).join("")}
  </section>`;
}
const workOriginalSettings = renderSettings;
renderSettings = function() {
  workOriginalSettings();
  const heading=$("#content .page-heading");
  if (heading) heading.insertAdjacentHTML("afterend",workQuotaHtml());
  if (!workFeatures.quota && !workFeatures.loading && !workFeatures.errors.quota) loadWorkFeatures();
};

const workOriginalPage = renderPage;
renderPage = function() {
  if (!state.loaded || !["sessions","library"].includes(state.page)) return workOriginalPage();
  renderChrome();
  if (state.page==="sessions") renderSessions(); else renderLibrary();
  if (!workFeatures.loading && !(state.page==="sessions"?workFeatures.sessions:workFeatures.library)
      && !workFeatures.errors[state.page]) loadWorkFeatures();
};

// Invoked only by the local desktop tray; selecting a saved plan never applies
// it without the existing model-switch preview and confirmation.
window.agentHubOpenPlan = async function(planId) {
  if (state.busy) { toast("正在执行操作，请稍候…"); return; }
  if (state.page==="models" && planDirty() && !await confirmDialog("方案草稿有改动","载入其他方案会替换当前草稿。",[],"放弃并载入",false)) return;
  await navigate("models");
  if (state.page!=="models") return;
  history.replaceState(null,"","#models");
  await loadPlans();
  const plan=state.plans?.store?.plans?.find(p=>p.id===planId);
  if(!plan) { toast("方案已不存在，请在列表中重新选择。","error"); return; }
  loadPlanIntoDraft(plan);
  state.advancedOpen=true;
  renderPage();
  $("#plan-card")?.scrollIntoView({block:"start"});
  toast("已载入方案；请检查并预览后应用。");
};

function workItemForm(item={}, candidate={}) {
  const editing=!!item.id, targets=item.targets||workAgentIds;
  const source=item.source||candidate.source||"", name=item.name||candidate.name||"";
  const kind=item.kind||candidate.kind||"skill";
  const replacements=(item.overrides||{});
  showEditor("read-only",editing?"管理主来源":"添加主来源",
    `<div class="feature-form">
      <label><span>内容类型</span><select id="work-item-kind" ${editing?"disabled":""}><option value="skill" ${kind==="skill"?"selected":""}>技能目录</option><option value="instruction" ${kind==="instruction"?"selected":""}>全局指令文件</option></select></label>
      <label><span>名称</span><input id="work-item-name" type="text" value="${esc(name)}" ${editing?"disabled":""} placeholder="例如 ambitious-prompt"><small>使用小写字母、数字与连字符。</small></label>
      <label><span>主来源绝对路径</span><input id="work-item-source" type="text" value="${esc(source)}" placeholder="技能目录或指令文件的完整路径"></label>
      <div><strong>分发到</strong><div class="feature-checks">${workAgentIds.map(a=>`<label><input type="checkbox" name="work-target" value="${a}" ${targets.includes(a)?"checked":""}> ${esc(workAgentNames[a])}</label>`).join("")}</div></div>
      <label><span>同步方式</span><select id="work-item-mode"><option value="manual" ${item.mode!=="auto"?"selected":""}>手动发布</option><option value="auto" ${item.mode==="auto"?"selected":""}>自动同步</option></select></label>
      <details id="work-overrides" ${kind==="instruction"?"":"hidden"}><summary>各端指令差异（可选）</summary>
        <p>如果主指令写死了客户端名称或记录器标识，可为该端填写要替换的原文和目标文字；不填写时原样分发，发布预览会展示结果。</p>
        ${workAgentIds.map(a=>`<div class="work-override-agent"><strong>${esc(workAgentNames[a])}</strong><div data-override-rows="${a}">${(replacements[a]||[]).map(pair=>workOverrideRow(pair)).join("")}</div><button type="button" class="small" data-feature="override-add" data-agent="${a}">添加替换</button></div>`).join("")}
      </details>
      <p>添加只登记来源；首次发布必须预览并应用。选择自动同步后，后续变化在后台同步；端侧有独立修改时暂停该端。</p></div>`,
    `<button type="button" data-action="close-editor">取消</button>${editing?`<button type="button" data-feature="remove-item" data-id="${esc(item.id)}">停止管理</button>`:""}<button type="button" class="primary" data-feature="save-item" data-id="${esc(item.id||"")}">保存</button>`);
}

function workOverrideRow(pair={}) {
  return `<div class="work-override-row"><label><span>原文</span><input type="text" class="work-replace-from" value="${esc(pair.from||"")}"></label><label><span>替换为</span><input type="text" class="work-replace-to" value="${esc(pair.to||"")}"></label><button type="button" class="small" data-feature="override-remove" aria-label="删除替换">删除</button></div>`;
}

document.addEventListener("change",event=>{
  if (event.target.id==="work-item-kind" && $("#work-overrides")) $("#work-overrides").hidden=event.target.value!=="instruction";
  if (event.target.id.startsWith("work-quota-")) saveWorkQuotaDraft();
  if (event.target.id==="work-handoff-target") fillWorkTargetInstances();
  if (event.target.id==="work-resume-instance") { const b=$('[data-feature="resume"]'); if(b) b.disabled=true; }
});
document.addEventListener("input",event=>{
  if (event.target.id.startsWith("work-quota-")) saveWorkQuotaDraft();
});
function saveWorkQuotaDraft() {
  const enabled=$("#work-quota-enabled"), interval=$("#work-quota-interval"), threshold=$("#work-quota-threshold");
  if(enabled&&interval&&threshold) workFeatures.quotaDraft={enabled:enabled.value,interval:interval.value,threshold:threshold.value};
}

function showWorkDiscovery(data) {
  const rows=data.candidates||[];
  showEditor("read-only","发现已有内容",
    `<p>只列本机发现的全局内容；选择后仍可更改主来源和分发目标。</p>${(data.warnings||[]).map(w=>`<p class="notice warn">${esc(w)}</p>`).join("")}
    ${rows.length?`<div class="feature-list">${rows.map((x,i)=>`<div class="feature-row"><div class="feature-row-main"><strong>${esc(x.name)} · ${x.kind==="skill"?"技能":"全局指令"}</strong><small class="feature-path">${esc(x.source)}</small><small>发现于 ${(x.foundBy||[]).map(a=>esc(workAgentNames[a]||a)).join("、")}${x.reason?" · "+esc(x.reason):""}</small></div><button type="button" data-feature="select-candidate" data-index="${i}" ${x.canImport===false?"disabled":""}>选择</button></div>`).join("")}</div>`:blank("没有发现候选","你也可以手动输入主来源路径。")}`,
    `<button type="button" data-action="close-editor">关闭</button><button type="button" data-feature="new-item">手动添加</button>`);
}

function showWorkPreview(preview) {
  workFeatures.preview=preview;
  const rows=preview.results||[];
  const hasConflict=!!preview.hasConflicts;
  const targetPending=workFeatures.library?.items?.find(x=>x.id===preview.itemId)?.targetPending||{};
  const canRepreview=rows.some(r=>r.status==="conflict" && !r.blockedByRecovery && !targetPending[r.agent]);
  showEditor("read-only","发布预览",
    `<p>本次使用${preview.publishedVersion?"已发布版本":"当前主来源"}。应用时会重新核对主来源和目标文件；如果预览后发生变化，需要重新预览。</p>${(preview.warnings||[]).map(w=>`<p class="notice warn">${esc(w)}</p>`).join("")}${hasConflict?'<p class="notice warn">存在端侧冲突。勾选需要明确覆盖的目标后重新预览，其余内容不会被静默覆盖。</p>':""}
    <div class="feature-list">${rows.map(r=>{
      const d=r.diff||{};
      const changes=[d.text||"",d.added?.length?`新增：${d.added.join("、")}`:"",d.removed?.length?`移除：${d.removed.join("、")}`:"",d.changed?.length?`修改：${d.changed.join("、")}`:"",...Object.values(d.textFiles||{})].filter(Boolean).join("\n");
      return `<div class="feature-row"><div class="feature-row-main"><strong>${esc(workAgentNames[r.agent]||r.agent)} · ${esc(workStatusText[r.status]||r.status)}${r.forced?" · 已选择覆盖":""}</strong><small class="feature-path">${esc(r.path||"")}</small><small>${esc(r.detail||"")}</small>${changes?`<pre class="feature-diff">${esc(changes)}</pre>`:""}
      ${r.status==="conflict" && !r.blockedByRecovery && !targetPending[r.agent]?`<label><input type="checkbox" name="work-force" value="${esc(r.agent)}"> 明确覆盖此端的独立修改</label>`:""}</div></div>`;
    }).join("")}</div>`,
    `<button type="button" data-action="close-editor">取消</button>${hasConflict?(canRepreview?`<button type="button" data-feature="repreview" data-id="${esc(preview.itemId)}">按所选目标重新预览</button>`:""):`<button type="button" class="primary" data-feature="apply-preview">应用并保存恢复点</button>`}`);
}

function showWorkHistory(data) {
  const rows=data.transactions||[];
  showEditor("read-only","发布与恢复记录",
    rows.length?`<div class="feature-list">${rows.map(x=>`<div class="feature-row"><div class="feature-row-main"><strong>${esc(when(x.createdAt))} · ${x.interrupted?"事务中断":esc(x.status||"发布")}</strong><small>${(x.targets||[]).map(a=>esc(workAgentNames[a]||a)).join("、")}</small></div>${x.canRestore?`<button type="button" data-feature="restore" data-id="${esc(x.id)}">${x.interrupted?"恢复中断事务":"恢复此记录"}</button>`:""}</div>`).join("")}</div>`:blank("暂无发布记录","发布内容后可在这里查看恢复点。"),
    `<button type="button" data-action="close-editor">关闭</button>`);
}

function showWorkResume(p) {
  const choices=(workFeatures.instances||[]).filter(x=>x.agent===p.agent && x.kind==="cli");
  showEditor("read-only","原端继续",
    `<p>会在记录的项目目录中打开原客户端。启动成功不等于客户端一定完成恢复。</p>
    <dl><dt>Agent</dt><dd>${esc(workAgentNames[p.agent]||p.agent)}</dd><dt>会话</dt><dd class="mono">${esc(p.sessionId)}</dd><dt>项目目录</dt><dd class="feature-path">${esc(p.cwd||"未记录")}</dd><dt>客户端实例</dt><dd class="feature-path">${esc(p.instance?.path||"未找到")}</dd></dl>
    ${choices.length>1?`<label>选择安装实例 <select id="work-resume-instance">${choices.map(x=>`<option value="${esc(x.id)}" ${p.instance?.id===x.id?"selected":""}>${esc(x.path)} · ${esc(x.version||"版本未知")}</option>`).join("")}</select></label><button type="button" data-feature="resume-repreview" data-agent="${esc(p.agent)}" data-session="${esc(p.sessionId)}">检查所选实例</button>`:""}
    ${p.reason?`<p class="notice ${p.available?"":"warn"}">${esc(p.reason)}</p>`:""}`,
    `<button type="button" data-action="close-editor">关闭</button>${p.available?`<button type="button" class="primary" data-feature="resume" data-agent="${esc(p.agent)}" data-session="${esc(p.sessionId)}" data-instance="${esc(p.instance?.id||"")}">启动并尝试恢复</button>`:""}`);
}

function showSessionTitleForm(session) {
  const manual=session.titleSource==="manual" ? session.title||"" : "";
  const current=session.titleSource!=="manual" && session.title
    ? `<p>当前显示：<strong>${esc(session.title)}</strong>（${esc(workSessionTitleSourceText[session.titleSource]||"已有标题")}）</p>`
    : session.title ? "" : "<p>当前尚无可用标题。</p>";
  showEditor("read-only","命名会话",
    `<div class="feature-form">${current}<label><span>手工名称</span><input id="work-session-title" name="session-title" type="text" value="${esc(manual)}" maxlength="120" autocomplete="off" aria-describedby="work-session-title-help"><small id="work-session-title-help">最多 60 字。留空会清除手工名称，重新显示可用的原有标题或网关请求消息节选。</small></label><p>名称只保存在本机，保存前会进行脱敏；不会修改客户端原生会话。</p></div>`,
    `<button type="button" data-action="close-editor">取消</button><button type="button" class="primary" data-feature="save-session-title" data-agent="${esc(session.agent)}" data-session="${esc(session.sessionId)}">保存名称</button>`,
    {kind:"session-title"});
  $("#editor-eyebrow").textContent="本机会话名称";
}

// The shared editor owns other form types. Catch Enter for this dialog before
// its generic submit handler can process it as a provider form.
$("#editor-form").addEventListener("submit",event=>{
  if (state.editor?.kind!=="session-title") return;
  event.preventDefault(); event.stopImmediatePropagation();
  $('[data-feature="save-session-title"]')?.click();
},true);

function selectHandoffTarget(agent,sid) {
  showEditor("read-only","选择交接目标",
    `<p>交接内容会先生成可编辑的草稿，你可以检查后再决定是否启动目标 Agent。</p><div class="feature-form"><label><span>目标 Agent</span><select id="work-handoff-target">${workAgentIds.filter(a=>a!==agent).map(a=>`<option value="${a}">${esc(workAgentNames[a])}</option>`).join("")}</select></label><label><span>目标安装实例</span><select id="work-target-instance"><option value="">自动选择可用 CLI</option></select></label></div>`,
    `<button type="button" data-action="close-editor">取消</button><button type="button" class="primary" data-feature="draft" data-agent="${esc(agent)}" data-session="${esc(sid)}">生成交接草稿</button>`);
  fillWorkTargetInstances();
}

function fillWorkTargetInstances() {
  const target=$("#work-handoff-target")?.value, select=$("#work-target-instance");
  if(!select) return;
  const rows=(workFeatures.instances||[]).filter(x=>x.agent===target && x.kind==="cli");
  select.innerHTML='<option value="">自动选择可用 CLI</option>'+rows.map(x=>`<option value="${esc(x.id)}">${esc(x.path)} · ${esc(x.version||"版本未知")}</option>`).join("");
}

function showWorkDraft(draft,cap) {
  workFeatures.draft=draft; workFeatures.targets=cap;
  showEditor("read-only","预览交接内容",
    `<p>${esc(draft.notice||"请核对并补充目标、进展、验证和下一步。")} 原生日志节选不等于模型总结。</p>
      ${draft.contentError?`<p class="notice warn">${esc(draft.contentError)}</p>`:""}
      <div class="feature-form"><label><span>交接给 ${esc(workAgentNames[draft.targetAgent]||draft.targetAgent)}（可编辑）</span><textarea id="work-handoff-text">${esc(draft.text||"")}</textarea></label>
      <label><input type="checkbox" id="work-handoff-start" ${cap.startSupported?"":"disabled"}> 交接后立即开始</label>
      <small>${esc(cap.startSupported?"勾选后，确认的内容会作为任务发送给目标 Agent。":cap.reason||"此客户端目前不能自动发送；可复制后手动交接。")}</small>
      <small>${esc(cap.openSupported?"未勾选时复制内容并打开目标客户端，不自动发送。":cap.openReason||"此客户端需要手动打开。")}</small></div>`,
    `<button type="button" data-action="close-editor">取消</button><button type="button" data-feature="summary-select">用模型整理</button><button type="button" data-feature="copy-draft">复制内容</button>${cap.openSupported?'<button type="button" data-feature="open-target-only">只打开目标</button>':""}<button type="button" class="primary" data-feature="handoff-confirm">交接</button>`);
}

function showSummarySelect() {
  const routes=workFeatures.summaryRoutes||[];
  showEditor("read-only","选择摘要线路",
    `<p>生成前可以再次查看完整材料和接收方。模型调用可能消耗所选账号额度或产生费用；结果仍需核对。</p>
      ${routes.length?`<div class="feature-form"><label><span>供应商 / Agent 连接 / 模型</span><select id="work-summary-route">${routes.map((r,i)=>`<option value="${i}">${esc(r.providerName)} · ${esc(workAgentNames[r.agent]||r.agent)} · ${esc(r.model)}</option>`).join("")}</select></label></div>`:blank("没有可用线路","请先在 Hub 配置可用的供应商、连接与模型。")}`,
    `<button type="button" data-feature="draft-back">返回编辑</button>${routes.length?'<button type="button" class="primary" data-feature="summary-preview">查看发送内容</button>':""}`);
}

function showSummaryPreview(preview) {
  workFeatures.summaryPreview=preview;
  showEditor("read-only","确认摘要材料",
    `<p class="notice warn">${esc(preview.notice)}</p><dl><dt>供应商</dt><dd>${esc(preview.providerName)}</dd><dt>模型</dt><dd>${esc(preview.selection.model)}</dd><dt>接收方</dt><dd>${esc(preview.destination)}</dd></dl>
     <h3>将发送的材料</h3><pre class="feature-diff">${esc(preview.material)}</pre>
     <details><summary>查看整理指令</summary><pre class="feature-diff">${esc(preview.systemInstruction)}</pre></details>`,
    `<button type="button" data-feature="draft-back">返回编辑</button><button type="button" class="primary" data-feature="summary-generate">发送并生成草稿</button>`);
}

async function workCopy(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch (_) { /* Some file:// WebViews do not grant Clipboard API access. */ }
  const field=$("#work-handoff-text");
  if (field) {
    field.focus(); field.select();
    if (document.execCommand?.("copy")) return;
  }
  throw new Error("复制失败，请选中交接内容并手动复制。");
}

function workError(error) {
  const el=$("#editor-error");
  if ($("#editor-dialog").open && el) { el.hidden=false; el.textContent=error.message; }
  else toast(error.message,"error");
}

document.addEventListener("click",async event=>{
  const button=event.target.closest("[data-feature]");
  if (!button || button.disabled || state.busy) return;
  const action=button.dataset.feature, id=button.dataset.id, agent=button.dataset.agent, sid=button.dataset.session;
  button.disabled=true;
  try {
    if (action==="refresh") { await loadWorkFeatures(true); toast("状态已刷新"); }
    else if (action==="new-item") workItemForm();
    else if (action==="discover") { workFeatures.discovery=await api("GET","/api/capabilities/discover"); showWorkDiscovery(workFeatures.discovery); }
    else if (action==="select-candidate") { const c=workFeatures.discovery?.candidates?.[Number(button.dataset.index)]; if(c) workItemForm({},c); }
    else if (action==="edit-item") { const item=workFeatures.library?.items?.find(x=>x.id===id); if(item) workItemForm(item); }
    else if (action==="override-add") { const rows=$(`[data-override-rows="${agent}"]`); if(rows) rows.insertAdjacentHTML("beforeend",workOverrideRow()); }
    else if (action==="override-remove") button.closest(".work-override-row")?.remove();
    else if (action==="save-item") {
      const payload={source:$("#work-item-source").value.trim(),targets:$$('[name="work-target"]:checked').map(e=>e.value),mode:$("#work-item-mode").value};
      if (!id) { payload.name=$("#work-item-name").value.trim(); payload.kind=$("#work-item-kind").value; }
      if ($("#work-item-kind").value==="instruction") {
        payload.overrides={};
        for (const a of workAgentIds) {
          const pairs=$$(`[data-override-rows="${a}"] .work-override-row`).map(row=>({from:row.querySelector(".work-replace-from").value,to:row.querySelector(".work-replace-to").value}));
          if (pairs.some(p=>!p.from && p.to)) throw new Error("每项端侧替换都需要填写原文。");
          const filled=pairs.filter(p=>p.from);
          if (filled.length) payload.overrides[a]=filled;
        }
      }
      await api(id?"PUT":"POST",id?`/api/capabilities/${encodeURIComponent(id)}`:"/api/capabilities/import",payload);
      closeEditor(); await loadWorkFeatures(true); toast("主来源已保存；发布前请先查看预览。");
    }
    else if (action==="remove-item") {
      if (!await confirmDialog("停止管理？","只移除 Hub 的跟踪与自动同步，不删除主来源或已分发文件。",[],"停止管理",false)) return;
      await api("DELETE",`/api/capabilities/${encodeURIComponent(id)}`); closeEditor(); await loadWorkFeatures(true); toast("已停止管理，原文件保留。");
    }
    else if (action==="preview" || action==="preview-published") { showWorkPreview(await api("POST",`/api/capabilities/${encodeURIComponent(id)}/preview`,{published:action==="preview-published"})); }
    else if (action==="repreview") {
      const forceTargets=[...new Set([...(workFeatures.preview?.results||[]).filter(r=>r.forced).map(r=>r.agent),...$$('[name="work-force"]:checked').map(e=>e.value)])];
      showWorkPreview(await api("POST",`/api/capabilities/${encodeURIComponent(id)}/preview`,{forceTargets,published:!!workFeatures.preview?.publishedVersion}));
    }
    else if (action==="apply-preview") {
      const planId=workFeatures.preview?.planId; if(!planId) throw new Error("预览已失效，请重新预览。");
      workFeatures.preview=null;
      const result=await api("POST","/api/capabilities/apply",{planId}); closeEditor(); await loadWorkFeatures(true);
      toast(result.partial?"部分目标已发布，请查看结果。":"已发布，可在记录中恢复。");
    }
    else if (action==="history") showWorkHistory(await api("GET",`/api/capabilities/${encodeURIComponent(id)}/history`));
    else if (action==="restore") {
      if (!await confirmDialog("恢复这次发布？","将恢复该次发布之前的文件内容；若目标后来又有改动，恢复会被阻止。",[],"恢复",false)) return;
      const result=await api("POST","/api/capabilities/restore",{transactionId:id}); closeEditor(); await loadWorkFeatures(true); toast(result.message|| (result.autoSyncPaused?"恢复完成；该项已改为手动发布。":"恢复完成。"));
    }
    else if (action==="session-rename") {
      const session=workFeatures.sessions?.sessions?.find(s=>s.agent===agent && s.sessionId===sid);
      if (!session) throw new Error("会话已不在列表中，请刷新后重试。");
      showSessionTitleForm(session);
    }
    else if (action==="save-session-title") {
      const title=$("#work-session-title").value.trim();
      if (Array.from(title).length>60) throw new Error("会话名称不能超过 60 字。");
      $("#editor-error").hidden=true;
      state.busy=true;
      try { await api("PUT","/api/sessions/title",{agent,sessionId:sid,title}); }
      finally { state.busy=false; }
      closeEditor(); await loadWorkFeatures(true);
      toast(workFeatures.errors.sessions ? "名称已保存，但列表刷新失败；请手动刷新。" : (title?"会话名称已保存。":"手工名称已清除。"),workFeatures.errors.sessions?"error":"success");
    }
    else if (action==="resume-preview") {
      const [p,inventory]=await Promise.all([api("POST","/api/sessions/resume/preview",{agent,sessionId:sid}),api("GET","/api/agents")]);
      workFeatures.instances=inventory.instances||[]; showWorkResume(p);
    }
    else if (action==="resume-repreview") showWorkResume(await api("POST","/api/sessions/resume/preview",{agent,sessionId:sid,instanceId:$("#work-resume-instance").value||null}));
    else if (action==="resume") { const r=await api("POST","/api/sessions/resume",{agent,sessionId:sid,instanceId:button.dataset.instance||null}); closeEditor(); toast(r.message||"已启动客户端；请在客户端确认恢复结果。"); }
    else if (action==="handoff") { const inventory=await api("GET","/api/agents"); workFeatures.instances=inventory.instances||[]; selectHandoffTarget(agent,sid); }
    else if (action==="draft") {
      const target=$("#work-handoff-target").value, instanceId=$("#work-target-instance").value||null;
      const [draft,cap]=await Promise.all([api("POST","/api/sessions/handoff",{agent,sessionId:sid,target}),api("POST","/api/sessions/target",{target,instanceId})]);
      showWorkDraft(draft,cap);
    }
    else if (action==="summary-select") {
      const material=$("#work-handoff-text").value;
      if(!material.trim()) throw new Error("请先填写要整理的交接材料。");
      workFeatures.summaryMaterial=material;
      const result=await api("GET","/api/sessions/summary/routes");
      workFeatures.summaryRoutes=result.routes||[];
      showSummarySelect();
    }
    else if (action==="draft-back") {
      const draft={...workFeatures.draft,text:workFeatures.summaryMaterial||workFeatures.draft?.text||""};
      showWorkDraft(draft,workFeatures.targets);
    }
    else if (action==="summary-preview") {
      const route=workFeatures.summaryRoutes?.[Number($("#work-summary-route").value)];
      if(!route) throw new Error("请选择可用的摘要线路。");
      const selection={provider:route.provider,agent:route.agent,model:route.model};
      showSummaryPreview(await api("POST","/api/sessions/summary/preview",{selection,material:workFeatures.summaryMaterial}));
    }
    else if (action==="summary-generate") {
      const p=workFeatures.summaryPreview;
      if(!p) throw new Error("预览已失效，请重新选择线路。");
      button.textContent="正在生成…";
      const result=await api("POST","/api/sessions/summary/generate",{selection:p.selection,material:p.material,digest:p.digest});
      const draft={...workFeatures.draft,text:result.draft,notice:result.notice,generatedBy:"model"};
      workFeatures.summaryMaterial=result.draft; workFeatures.summaryPreview=null;
      showWorkDraft(draft,workFeatures.targets);
    }
    else if (action==="copy-draft") { await workCopy($("#work-handoff-text").value); toast("交接内容已复制；尚未发送。"); }
    else if (action==="open-target-only") {
      const draft=workFeatures.draft, cap=workFeatures.targets;
      if(!draft||!cap?.openSupported) throw new Error("目标客户端目前无法自动打开。");
      const r=await api("POST","/api/sessions/target/open",{agent:draft.sourceAgent,sessionId:draft.sourceSessionId,target:draft.targetAgent,instanceId:cap.instance?.id||null});
      toast(r.message||"目标客户端已打开；尚未发送内容。");
    }
    else if (action==="handoff-confirm") {
      const draft=workFeatures.draft,cap=workFeatures.targets,text=$("#work-handoff-text").value;
      if(!draft || !text.trim()) throw new Error("请先填写要交接的内容。");
      const payload={agent:draft.sourceAgent,sessionId:draft.sourceSessionId,target:draft.targetAgent,text,instanceId:cap.instance?.id||null};
      if ($("#work-handoff-start").checked) {
        const r=await api("POST","/api/sessions/handoff/start",payload); closeEditor(); toast(r.message||"已启动目标 Agent。");
      } else {
        await workCopy(text);
        if(cap.openSupported) {
          const r=await api("POST","/api/sessions/target/open",payload); closeEditor(); toast(r.message||"交接内容已复制，目标客户端已打开。");
        } else { closeEditor(); toast("交接内容已复制，请手动打开目标客户端并粘贴。"); }
      }
    }
    else if (action==="quota-refresh") { workFeatures.quota=await api("POST","/api/quota-monitor/refresh",{}); renderPage(); toast("已请求额度查询；请以各账号的查询时间为准。"); }
    else if (action==="quota-save") {
      const body={enabled:$("#work-quota-enabled").value==="true",intervalSeconds:Number($("#work-quota-interval").value)*60,thresholdRemainingPercent:Number($("#work-quota-threshold").value)};
      workFeatures.quota=await api("PUT","/api/quota-monitor/settings",body); workFeatures.quotaDraft=null; renderPage(); toast("额度提醒设置已保存。");
    }
  } catch(error) { workError(error); }
  finally { if (button.isConnected) button.disabled=false; }
});
