"use strict";

// Daily workspace built around the existing configuration/transaction UI.
const daily = {inventory:null, inventoryError:"", inventoryLoading:false, chosen:{}, quick:null,
  obs:null, obsError:"", obsLoading:false, tab:"overview", detail:null, conversation:null,
  filter:{days:7,agent:"",model:"",provider:"",status:"",q:"",page:1,start:"",end:""}, generation:0};
const agentIds = ["claude","codex","pi","dsh"];
const labels = {claude:"Claude Code",codex:"Codex",pi:"pi",dsh:"dsh"};
const statusNames = {succeeded:"已完成",failed:"失败",interrupted:"中断",unknown:"结果未确认"};
const updateNames = {"not-checked":"尚未检查",available:"有可用更新",current:"对应频道已是最新",manual:"需在原渠道更新","source-unknown":"来源未知","check-failed":"检查失败",unverified:"版本顺序待核对"};
const sourceNames = {npm:"npm 安装", "claude-native":"Claude 原生安装", "desktop-bundled":"桌面版附带 CLI", "desktop-updater":"桌面应用更新器","codex-app-managed":"Codex 桌面维护的运行时",msix:"Microsoft Store",unknown:"来源待确认"};
const fmt = n => n == null ? "未采集" : Number(n).toLocaleString("zh-CN",{maximumFractionDigits:2});
const costFmt = n => n == null ? "无法计价" : `≈ $${Number(n).toFixed(4)}`;
const when = n => n ? new Date(n*1000).toLocaleString("zh-CN",{hour12:false}) : "未记录";
const tokenCell = n => n == null ? '<span class="muted">未知</span>' : esc(tokens(n));
const tag = (text,kind="") => `<span class="tag ${kind}">${esc(text)}</span>`;
const blank = (title, text) => `<div class="obs-empty">${icon("info")}<h3>${esc(title)}</h3><p>${esc(text)}</p></div>`;
const dailyHeading = (eyebrow,title,description,actions="") => `<div class="page-heading"><div><span class="eyebrow">${eyebrow}</span><h1>${title}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
const originalChrome = renderChrome;
renderChrome = function() {
  originalChrome();
  const settings = ["settings","models","providers","sync"].includes(state.page);
  $("#settings-nav").hidden = !settings;
  const nav = $('[data-page="settings"]');
  nav.classList.toggle("active",settings);
  if (settings) nav.setAttribute("aria-current","page");
};
const originalPage = renderPage;
renderPage = function() {
  if (!state.loaded) return originalPage();
  if (state.page === "agents") {renderChrome(); renderAgents(); if (!daily.inventory && !daily.inventoryLoading) loadInventory();}
  else if (state.page === "observe") {renderChrome();renderObservation(); if (!daily.obs && !daily.obsLoading) loadObservation();}
  else if (state.page === "settings") {renderChrome();renderSettings(); if (!daily.obs && !daily.obsLoading) loadObservation();}
  else originalPage();
};

async function loadInventory(detect=false) {
  daily.inventoryLoading = true; daily.inventoryError = "";
  if (state.page === "agents") renderAgents();
  try {daily.inventory = await api(detect?"POST":"GET",detect?"/api/agents/detect":"/api/agents");}
  catch(e){daily.inventoryError = e.message;}
  finally {daily.inventoryLoading=false;if(state.page==="agents")renderAgents();}
}
function pickedInstance(agent) {
  const list = (daily.inventory?.instances || []).filter(e=>e.agent===agent);
  return list.find(e=>e.id===daily.chosen[agent]) || list.find(e=>e.running) || list.find(e=>e.activePath) || list[0];
}
function renderAgents() {
  const jobs = daily.inventory?.jobs || [];
  const latest = jobs.slice(-3);
  $("#content").innerHTML = dailyHeading("DAILY WORKSPACE","Agent","四个常用 Agent。配置选择与实际调用分别记录，更新定位到具体安装。",`<button data-daily="detect" ${daily.inventoryLoading?'disabled':''}>${icon("refresh")}${daily.inventoryLoading?'正在检测…':'重新检测安装'}</button><a class="button-link" href="#sync">${icon("sync")}切换方案</a>`) +
    (daily.inventoryError?`<div class="notice warn">${esc(daily.inventoryError)}<button data-daily="detect">重试</button></div>`:"")+
    `<section class="card agent-list"><div class="agent-table-wrap"><table class="agent-table"><thead><tr><th>Agent / 安装实例</th><th>版本与更新</th><th>配置选择 · 供应商 / 模型</th><th>推理强度</th><th class="align-right">操作</th></tr></thead><tbody>${agentIds.map(agent=>{
      const e = pickedInstance(agent), list=(daily.inventory?.instances||[]).filter(x=>x.agent===agent);
      const r = modelSyncResolved(agent), official = agent==='codex' && state.status?.targets?.codex?.mode!=='third-party';
      const job = jobs.find(j=>j.instanceId===e?.id && j.state==='running');
      return `<tr><td><div class="agent-identity"><span class="agent-symbol ${agent}">${({claude:"C",codex:"⎔",pi:"π",dsh:"D"})[agent]}</span><div><strong>${labels[agent]}</strong><small>${!daily.inventory ? '正在检测安装…' : list.length ? `${list.length} 个安装实例`:'未发现安装入口'}</small></div></div>${list.length?`<select class="instance-picker" data-instance="${agent}" aria-label="${labels[agent]} 安装实例">${list.map(x=>`<option value="${x.id}" ${x.id===e?.id?'selected':''}>${x.kind==='desktop'?'桌面':'CLI'} · ${esc(x.version||'版本未知')}${x.running?' · 运行中':x.activePath?' · PATH 默认':''}</option>`).join('')}</select>`:''}</td><td><strong class="version-number">${esc(e?.version||'版本未知')}</strong><div class="cell-secondary">${tag(updateNames[e?.updateState]||'尚未检测',e?.updateState==='available'?'amber':e?.updateState==='current'?'green':'')}</div>${e?.latestVersion&&e.updateState==='available'?`<small>可更新至 ${esc(e.latestVersion)}</small>`:''}</td><td><div class="model-current" title="${esc(r?`${r.provider} / ${r.model}`:'原生客户端管理')}"><span>${esc(official?'官方线路':r?.provider?providerName(r.provider):'原生客户端管理')}</span><strong>${esc(official?'原生模型（Hub 第三方选择未应用）':r?.model||'保留客户端当前值')}</strong></div><small>这是配置选择，实际调用见观测</small></td><td>${tag(official?'原生配置':r?.reasoningEffort||'保留当前值')}</td><td><div class="actions row-actions"><button class="small" data-daily="check" data-id="${e?.id||''}" ${!e||job?'disabled':''}>检查更新</button><button class="small primary" data-daily="update" data-id="${e?.id||''}" ${!e?.canUpdate||job?'disabled':''}>${job?'更新中…':'更新'}</button><button class="small" data-daily="switch" data-agent="${agent}">切换模型</button><button class="icon-button" data-daily="agent-detail" data-agent="${agent}" aria-label="${labels[agent]} 详情">${icon('info')}</button></div></td></tr>`;
    }).join('')}</tbody></table></div></section>
    <div class="hint-line">${icon('shield')}更新只作用于选中的安装实例；桌面版与 CLI 的版本、更新方式独立显示。</div>
    ${latest.length?`<section class="card job-list"><div class="card-heading"><div><h2>更新记录</h2><p>退出码与同一入口的版本核验分别报告。</p></div></div>${latest.reverse().map(j=>`<div class="job-row"><strong>${labels[j.agent]} · ${esc(j.stage)}</strong>${tag(j.state==='succeeded'?'已核验':j.state==='failed'?'失败':'执行中',j.state==='failed'?'red':'')}<p>${esc(j.messages.join('；'))}</p>${j.state==='running'?'<progress aria-label="更新执行进度"></progress>':''}${j.actualVersion?`<small>实际版本 ${esc(j.actualVersion)} / 预期 ${esc(j.expectedVersion)}</small>`:''}</div>`).join('')}</section>`:''}
    <section class="card workspace-note"><div>${icon('eye')}<strong>看到发生过的调用</strong><p>原生会话里的 Token、模型、对话与上下文，共用一个可追溯的索引。</p></div><a class="button-link" href="#observe">进入观测 ${icon('arrow')}</a></section>`;
}
function agentDetail(agent) {
  const list=(daily.inventory?.instances||[]).filter(x=>x.agent===agent);
  const files=state.status?.targets?.[agent]?.files||[];
  showEditor('read-only',`${labels[agent]} · 安装与配置`, `<div class="notice">PATH 默认入口只代表从当前环境启动时的解析结果；运行中表示进程映像匹配。桌面/终端已有会话可能使用其他入口。</div>${list.map(e=>`<section class="instance-detail"><h3>${e.kind==='desktop'?'桌面应用':'CLI'} ${tag(sourceNames[e.source])} ${e.running?tag('运行中','green'):''}${e.activePath?tag('PATH 默认'):''}</h3><dl><dt>执行 / 安装路径</dt><dd class="mono">${esc(e.path)}</dd><dt>版本</dt><dd>${esc(e.version||'未能启动或版本无法识别')}</dd><dt>发布频道</dt><dd>${esc(e.channel||e.releaseSource||'原安装渠道')}</dd><dt>更新状态</dt><dd>${esc(updateNames[e.updateState])} · ${esc(e.message||e.updateNote||'尚未查询发布渠道')}</dd>${e.prefix?`<dt>npm prefix</dt><dd class="mono">${esc(e.prefix)}</dd>`:''}</dl></section>`).join('')||blank('未找到安装','配置文件的存在不代表客户端已经安装。')}<h3>配置位置</h3>${files.map(f=>`<p class="path-line">${tag(f.exists?'存在':'尚不存在')}${esc(f.path)}</p>`).join('')}`,`<button type="button" data-action="close-editor">关闭</button>`);
}

function openQuickSwitch(agent) {
  daily.quick={agent,preview:null};
  const current=modelSyncResolved(agent), connections=state.plans?.targets?.[agent]?.connections||{};
  const providers=Object.entries(connections).filter(([,c])=>c.ready&&c.enabled&&c.hasKey).map(([p])=>p);
  showEditor('quick-switch',`${labels[agent]} · 切换模型`,`<p class="form-intro">只列出该 Agent 对应连接上的模型；先预览，确认后通过现有事务写入。</p>${agent==='codex'&&state.status?.targets?.codex?.mode!=='third-party'?'<div class="notice warn">当前是官方线路。第三方模型会跳过；可在「方案与高级同步」中明确选择第三方线路后应用。</div>':''}<div class="form-grid"><label class="field full"><span>供应商</span><select id="quick-provider" name="provider"><option value="">请选择</option>${providers.map(pid=>`<option value="${esc(pid)}" ${pid===current?.provider?'selected':''}>${esc(providerName(pid))}</option>`).join('')}</select></label><label class="field full"><span>模型 · 按连接校验</span><select id="quick-model" name="model" required></select></label><label class="field full"><span>推理强度</span><select name="effort"><option value="">保留客户端当前值</option>${(agent==='codex'?CODEX_EFFORTS:EFFORTS).map(e=>`<option value="${e}">${e}</option>`).join('')}</select></label></div>${agent==='claude'?`<details class="form-section"><summary>Claude 档位 · 默认保留各档当前值</summary>${CLAUDE_TIERS.map(([tier,label])=>`<label class="field"><span>${label}</span><select name="tier-${tier}"><option value="keep">保留当前值</option><option value="follow">跟随本次选择</option></select></label>`).join('')}<p>单独指定模型和高级档位语义可在切换方案中维护。</p></details>`:''}<div id="quick-result" aria-live="polite"></div>`,`<button type="button" data-action="close-editor">取消</button><button type="submit" class="primary">预览切换</button>`,{agent});
  quickModels(current?.model);
}
function quickModels(selected='') {
  const agent=daily.quick.agent, pid=$('#quick-provider').value;
  const models=state.plans?.targets?.[agent]?.connections?.[pid]?.models||[];
  $('#quick-model').innerHTML='<option value="">请选择模型</option>'+models.map(m=>`<option value="${esc(m)}" ${m===selected?'selected':''}>${esc(m)}</option>`).join('');
}
async function previewQuick(fd) {
  const agent=daily.quick.agent;
  const spec={policy:'fixed',provider:fd.get('provider'),model:fd.get('model')};
  if(fd.get('effort'))spec.reasoningEffort=fd.get('effort');
  if(agent==='claude')spec.tiers=Object.fromEntries(CLAUDE_TIERS.map(([tier])=>[tier,fd.get(`tier-${tier}`)==='follow'?{policy:'explicit',provider:spec.provider,model:spec.model}:{policy:'keep'}]));
  const plan={name:`切换 ${labels[agent]}`,targets:[agent],agents:{[agent]:spec}};
  const result=await api('POST','/api/switch-plans/preview',{plan});
  daily.quick.preview=result;
  $('#quick-result').innerHTML=planTargetsReport(result.analysis)+`<p class="form-intro">${esc(result.error||'配置写入前自动备份；配置冲突会拒绝应用。')}</p>`;
  if(result.planId) $('#editor-footer').innerHTML=`<button type="button" data-action="close-editor">取消</button><button type="button" data-daily="quick-apply" class="primary">确认应用并写入客户端</button>`;
}

function obsParams(extra={}) {
  const f={...daily.filter,...extra};
  return new URLSearchParams(Object.fromEntries(Object.entries(f).filter(([,v])=>v!==''&&v!=null))).toString();
}
async function loadObservation() {
  const generation=++daily.generation;
  daily.obsLoading=true;daily.obsError='';
  try {
    const data=await api('GET','/api/observation?'+obsParams());
    if(generation!==daily.generation)return;
    daily.obs=data;
    // Pin the range while drilling through pages and details.
    if(!daily.filter.start&&!daily.filter.end){daily.filter.start=data.range.start;daily.filter.end=data.range.end;}
  }catch(e){if(generation===daily.generation)daily.obsError=e.message;}
  finally {if(generation===daily.generation){daily.obsLoading=false;if(state.page==='observe')renderObservation();if(state.page==='settings')renderSettings();}}
}
function collectionNotice(status) {
  const s=status?.settings, c=status?.collector;
  if(!s)return '';
  return `<div class="collection-line"><span class="${s.enabled?'online-dot':'offline-dot'}"></span><strong>${s.enabled?'自动观测索引已开启':'采集已停止'}</strong><span>${c?.state==='reading'?`正在索引 ${c.processed} / ${c.total} 个文件`:c?.finishedAt?`上次扫描 ${when(c.finishedAt)}`:'尚未完成首次采集'}</span><a href="#settings">采集设置 ${icon('arrow')}</a></div>${c?.errors?.length?`<details class="notice warn"><summary>${c.errors.length} 个来源读取失败 · 查看缺口</summary>${c.errors.map(e=>`<p class="path-line">${esc(e.agent||'')} · ${esc(e.message)}<br>${esc(e.path||'')}</p>`).join('')}</details>`:''}`;
}
function observationFilters() {
  const f=daily.filter;
  const dateValue=n=>n?new Date(Number(n)*1000-new Date(Number(n)*1000).getTimezoneOffset()*60000).toISOString().slice(0,16):'';
  return `<div class="obs-filters"><label><span>时间范围</span><select data-obs-filter="days"><option value="1" ${f.days==1?'selected':''}>最近 24 小时</option><option value="7" ${f.days==7?'selected':''}>最近 7 天</option><option value="30" ${f.days==30?'selected':''}>最近 30 天</option><option value="365" ${f.days==365?'selected':''}>最近一年</option><option value="all" ${f.days==='all'?'selected':''}>全部时间</option></select></label><label><span>Agent</span><select data-obs-filter="agent"><option value="">全部 Agent</option>${agentIds.map(a=>`<option value="${a}" ${f.agent===a?'selected':''}>${labels[a]}</option>`).join('')}</select></label><label class="filter-model"><span>模型</span><input data-obs-filter="model" value="${esc(f.model)}" list="obs-models" placeholder="全部模型"><datalist id="obs-models">${(daily.obs?.models||[]).map(m=>`<option value="${esc(m)}">`).join('')}</datalist></label><label><span>结果</span><select data-obs-filter="status"><option value="">全部结果</option>${Object.entries(statusNames).map(([v,t])=>`<option value="${v}" ${f.status===v?'selected':''}>${t}</option>`).join('')}</select></label><label class="filter-search"><span>搜索</span><input id="obs-search" value="${esc(f.q)}" placeholder="模型、错误、会话 ID 或目录"></label><button data-daily="search">${icon('search')}筛选</button><button data-daily="reset-obs">重置</button></div><details class="range-detail"><summary>精确时间与关联条件${f.session?' · 已限定对话':''}${f.request?' · 已限定请求':''}${f.provider||f.providerUnknown?' · 已限定供应商':''}${f.modelUnknown?' · 模型未记录':''}</summary><div class="actions"><label>开始<input type="datetime-local" data-time="start" value="${dateValue(f.start||daily.obs?.range?.start)}"></label><label>结束<input type="datetime-local" data-time="end" value="${dateValue(f.end||daily.obs?.range?.end)}"></label><button data-daily="exact-range">应用时间</button></div><small>摘要和页签跳转共用此时间范围；趋势按 UTC 日分组，时间显示使用本机时区。</small></details>`;
}
function renderObservation() {
  const data=daily.obs, s=data?.summary;
  $('#content').innerHTML=dailyHeading('OBSERVABILITY','观测','已记录调用的局部视图。原生会话与 Hub 网关共用索引；未知信息保留未知。',`<button data-daily="scan">${icon('refresh')}读取新记录</button>`)+collectionNotice(data?.status)+
    `<div class="obs-tabs" role="tablist">${[['overview','概览'],['requests','请求'],['conversations','对话'],['context','上下文']].map(([id,t])=>`<button role="tab" aria-selected="${daily.tab===id}" data-tab="${id}" class="${daily.tab===id?'active':''}">${t}</button>`).join('')}</div>`+observationFilters()+
    (daily.obsError?`<div class="notice warn">${esc(daily.obsError)}<button data-daily="obs-retry">重试</button></div>`:'')+
    (daily.obsLoading?`<div class="loading-inline"><span class="spinner"></span>正在读取统一索引…</div>`:'')+
    (!data?blank('正在连接本机索引','首次加载后会显示采集状态。'):!s.calls?`<section class="card">${blank(data.status.settings.enabled?'当前范围没有已采集记录':'采集尚未开启或已停止',data.status.settings.enabled?'可调整时间范围，或查看来源读取状态。没有记录不能证明没有活动。':'在设置中开启只读原生采集后，Hub 才会索引实际调用。已有数据仍受保留期限约束。')}<div class="empty-actions"><a class="button-link" href="#settings">采集设置</a><button data-daily="reset-obs">重置筛选</button></div></section>`:
      daily.tab==='overview'?overviewHtml(data):daily.tab==='requests'?requestsHtml(data):daily.tab==='conversations'?conversationsHtml(data):contextHtml(data));
}
function metric(label,value,note,filter='',status='') {
  return `<button class="metric" data-daily="metric" data-filter="${filter}" data-value="${status}"><span>${label}</span><strong>${value}</strong><small>${note}</small></button>`;
}
function overviewHtml(data) {
  const s=data.summary;
  return `<div class="metric-grid">${metric('已记录调用',fmt(s.calls),`${s.succeeded} 次已完成 · ${s.interrupted} 次中断`)}${metric('失败',fmt(s.failed),'按每条请求最终结果统计','status','failed')}${metric('结果未确认',fmt(s.unknown),'存在用量，但缺少最终状态证据','status','unknown')}${metric('USD 参考成本',costFmt(s.cost),`${s.unpriced} 次无法计价；非实际扣费`)}</div>
    <section class="card token-card"><div class="card-heading"><div><h2>Token 与性能</h2><p>推理属于输出；新输入不含缓存，单次上下文另计。每项标明有记录的调用数。</p></div></div><div class="token-grid">${[['input','新输入（不含缓存）'],['output','输出'],['reasoning','推理'],['cacheRead','缓存读取'],['cacheWrite','缓存写入']].map(([k,t])=>`<div><span>${t}</span><strong>${tokenCell(s.tokens[k].value)}</strong><small>${s.tokens[k].samples} / ${s.calls} 次有数据</small></div>`).join('')}</div><div class="performance-row"><span>平均响应耗时 <b>${s.durationMs==null?'未采集':fmt(s.durationMs)+' ms'}</b><small> ${s.durationSamples} 个实测样本</small></span><span>首个内容等待 <b>${s.ttftMs==null?'未采集':fmt(s.ttftMs)+' ms'}</b><small> ${s.ttftMsSamples||0} 个样本；含推理 / 工具</small></span><span>输出速度 <b>${s.speed==null?'未可靠测得':fmt(s.speed)+' Token/s'}</b></span><span>网关重试 / 上游尝试 <b>${s.attempts==null?'未知':s.retried+' / '+s.attempts}</b><small> ${s.attemptSamples||0} 次请求有尝试数据</small></span></div></section>
    <div class="obs-columns"><section class="card"><div class="card-heading"><div><h2>调用趋势</h2><p>UTC 日 · 点击进入当天请求</p></div></div><div class="trend-chart">${data.trend.map(d=>`<button class="trend-column" data-daily="trend" data-date="${d.date}" title="${d.date} · ${d.calls} 次调用"><span>${d.calls}</span><i style="height:${Math.max(3,d.calls/Math.max(...data.trend.map(x=>x.calls))*100)}px"></i><small>${d.date.slice(5)}</small></button>`).join('')}</div></section><section class="card"><div class="card-heading"><div><h2>数据覆盖与额度</h2><p>只展示原生声明的数据。</p></div></div><div class="coverage-list">${(data.status.collector.sources||[]).map(x=>`<div><strong>${labels[x.agent]||'Hub 网关'}</strong><span>${x.agent==='gateway'?'已接入账本':x.exists?`${x.files} 个原生文件`:'未找到日志目录'}</span></div>`).join('')||'<p>尚未扫描来源。</p>'}</div><p class="card-footnote">订阅 Plan、余额与重置时间见下方原生快照；没有快照时不推断。</p>${quotaHtml(data.quotas||[])}</section></div>
    <div class="distribution-grid">${[['agent','按 Agent'],['provider','按有来源的供应商'],['model','按模型']].map(([dim,title])=>`<section class="card"><div class="card-heading"><h2>${title}</h2></div><div class="distribution-list">${data.distribution[dim].sort((a,b)=>b.calls-a.calls).map(x=>`<button data-daily="distribution" data-dimension="${dim}" data-value="${esc(x.name)}"><span title="${esc(x.name)}">${esc(labels[x.name]||x.name)}</span><b>${x.calls}</b><i style="--share:${x.calls/s.calls*100}%"></i></button>`).join('')}</div></section>`).join('')}</div>`;
}
function quotaHtml(quotas) {
  return quotas.length?quotas.map(q=>`<div class="quota-snapshot"><strong>${labels[q.agent]} · ${esc(q.plan||'Plan 未知')}</strong><small>历史快照 ${when(q.time)}，非实时额度</small>${['primary','secondary'].map(k=>q[k]?`<p>${k==='primary'?'主窗口':'次窗口'}：已使用 ${fmt(q[k].used_percent)}% · 重置 ${when(q[k].resets_at)}</p>`:'').join('')}${q.credits?.balance!=null?`<p>记录余额：${esc(q.credits.balance)}（单位由客户端记录，不能当作 USD）</p>`:''}</div>`).join(''):'<p class="card-footnote">没有可靠额度快照。</p>';
}
function requestsHtml(data) {
  return `<section class="card"><div class="card-heading"><div><h2>请求记录</h2><p>${data.total} 次已记录调用 · ${data.summary.attempts==null?'上游尝试数未知':data.summary.attempts+' 次已知上游尝试 / '+data.summary.attemptSamples+' 次请求有数据'} · 消息数另计</p></div></div><div class="obs-table-scroll"><table class="requests-table"><thead><tr><th>时间 / Agent</th><th>供应商 / 请求模型 → 返回模型</th><th>结果 / 耗时</th><th>输入 / 输出</th><th>缓存读 / 写</th><th>参考费用</th><th></th></tr></thead><tbody>${data.requests.map(r=>`<tr><td><span>${when(r.time)}</span><small>${labels[r.agent]}</small></td><td class="request-model"><span>${esc(r.provider||'供应商未知')} <small>${r.providerEvidence==='gateway-upstream'?'网关实际上游':'客户端记录'}</small></span><strong title="${esc(r.model||'')}">${esc(r.model||'请求模型未记录')}</strong><small title="${esc(r.servedModel||'')}">→ ${esc(r.servedModel||'上游返回模型未记录')}</small></td><td>${tag(statusNames[r.status],r.status==='failed'?'red':r.status==='succeeded'?'green':'')}<small>${r.durationMs==null?'耗时未采集':fmt(r.durationMs)+' ms'}</small>${r.error?`<p class="error-excerpt" title="${esc(r.error)}">${esc(r.error)}</p>`:''}</td><td class="mono">${tokenCell(r.usage.inputIncludesCache?(r.usage.input==null||r.usage.cacheRead==null?null:Math.max(0,r.usage.input-r.usage.cacheRead)):r.usage.input)} / ${tokenCell(r.usage.output)}</td><td class="mono">${tokenCell(r.usage.cacheRead)} / ${tokenCell(r.usage.cacheWrite)}</td><td>${costFmt(r.cost)}</td><td><button class="small" data-daily="request" data-id="${r.id}">详情 ${icon('arrow')}</button></td></tr>`).join('')}</tbody></table></div><div class="table-footer"><span>第 ${data.page} / ${data.pages} 页，共 ${data.total} 条</span><div class="actions"><button data-daily="obs-prev" ${data.page<=1?'disabled':''}>上一页</button><button data-daily="obs-next" ${data.page>=data.pages?'disabled':''}>下一页</button></div></div></section>`;
}
function conversationsHtml(data) {
  return `<section class="card"><div class="card-heading"><div><h2>对话</h2><p>按原生会话 ID 关联 · ${data.sessionTotal} 个对话 · ${data.unassociated} 次调用未关联</p></div></div><div class="conversation-list">${data.sessions.map(s=>`<article class="conversation-row"><div class="conversation-main"><span class="agent-symbol ${s.agent}">${({claude:'C',codex:'⎔',pi:'π',dsh:'D'})[s.agent]}</span><div><button class="text-button" data-daily="conversation" data-agent="${s.agent}" data-session="${esc(s.sessionId)}">${esc(s.title||s.sessionId)}</button><p class="path-line">${esc(s.cwd||'工作目录未记录')}</p><small>${labels[s.agent]} · 开始 ${when(s.startedAt)} · 最近 ${when(s.lastAt)}${s.parentSessionId?' · 存在原生父会话引用':''}</small></div></div><div class="conversation-count"><strong>${s.summary.calls} 次调用</strong><small>范围内输出 ${fmt(s.summary.tokens.output.value)} Token</small><small>${costFmt(s.summary.cost)} · ${s.summary.unpriced} 次未计价</small></div><div class="tags model-tags">${s.models.map(m=>tag(m||'模型未知')).join('')}</div></article>`).join('')||blank('这些调用未关联到对话','原生记录未声明可靠会话 ID，Hub 不按时间或文本强行归组。')}</div>${sessionPagination(data)}</section>`;
}
function contextHtml(data) {
  return `<div class="notice">${icon('info')}单次输入与会话累计 Token 分开。缓存属于输入的一部分；未知窗口不填默认值。缓存分项缺失的输入量为下界；构成缺失时不生成比例或健康分。</div>${data.sessions.map(s=>{
    const points=s.points, max=Math.max(...points.map(p=>p.input||0),1), compact=s.compactions||[];
    return `<section class="card context-session"><div class="card-heading"><div><h2>${esc(s.title||s.sessionId)}</h2><p>${labels[s.agent]} · 单次输入峰值 ${s.points.some(p=>p.precision==='lower-bound')?'至少 ':''}${fmt(s.peakInput)} Token · ${compact.length} 次原生压缩事件</p></div><button class="small" data-daily="conversation" data-agent="${s.agent}" data-session="${esc(s.sessionId)}">相关对话</button></div><div class="context-chart">${points.map((p,i)=>`<button data-daily="request" data-id="${p.id}" title="第 ${i+1} 次 · ${when(p.time)} · 输入 ${p.precision==='lower-bound'?'至少 ':''}${fmt(p.input)} · 窗口 ${fmt(p.window)} · ${p.fill==null?'占比未知':(p.fill*100).toFixed(1)+'%'}"><i style="height:${Math.max(3,(p.input||0)/max*106)}px"></i><small>${i+1}</small></button>`).join('')}</div><div class="context-caption"><span>逐次输入量（含可确认缓存）· ${s.pointCount>160?'最近 160 次 / 共 '+s.pointCount+' 次 · ':''}点击柱形进入请求</span><span>会话范围内累计输出 ${fmt(s.summary.tokens.output.value)} Token</span></div>${compact.length?`<div class="compaction-list">${compact.map(c=>`<span>压缩 · ${when(c.time)} · ${esc(c.source)}</span>`).join('')}</div>`:''}</section>`;
  }).join('')}${sessionPagination(data)}`;
}
function sessionPagination(data){return `<div class="table-footer"><span>对话 ${data.sessionPage} / ${data.sessionPages} 页 · 共 ${data.sessionTotal} 个</span><div class="actions"><button data-daily="session-prev" ${data.sessionPage<=1?'disabled':''}>上一页</button><button data-daily="session-next" ${data.sessionPage>=data.sessionPages?'disabled':''}>下一页</button></div></div>`;}
async function requestDetail(id) {
  const r=await api('GET',`/api/observation/requests/${id}`); daily.detail=r;
  showEditor('read-only','调用详情',`<div class="detail-title"><span>${labels[r.agent]} · ${when(r.time)}</span>${tag(statusNames[r.status])}</div><dl class="request-facts"><dt>原生调用 ID</dt><dd class="mono">${esc(r.nativeId)}</dd><dt>请求模型</dt><dd>${esc(r.model||'未记录')}</dd><dt>上游返回模型</dt><dd>${esc(r.servedModel||'未记录；不能用配置模型代替')}</dd><dt>供应商</dt><dd>${esc(r.provider||'未知')} · ${r.providerEvidence==='gateway-upstream'?'网关实际上游线路':r.provider?'客户端原生记录，不证明最终网络路由':'没有可靠来源'}</dd><dt>单次上下文输入</dt><dd>${r.usage.contextPrecision==='lower-bound'?'至少 ':''}${fmt(r.usage.context)} Token${r.window?` / ${r.windowEvidence==='gateway-catalog'?'模型目录窗口（非运行时声明）':'有效窗口（原生声明）'} ${fmt(r.window)} · ${r.usage.context==null?'占比未知':(r.usage.context/r.window*100).toFixed(1)+'%'+(r.usage.contextPrecision==='lower-bound'?'（下界；缺少缓存分项）':'')}`:' · 有效窗口未记录'}</dd><dt>输出 / 推理</dt><dd>${fmt(r.usage.output)} / ${fmt(r.usage.reasoning)}</dd><dt>缓存读 / 写</dt><dd>${fmt(r.usage.cacheRead)} / ${fmt(r.usage.cacheWrite)} · 计入上下文，不能再次加到占用中</dd><dt>费用</dt><dd>${costFmt(r.cost)} · ${esc(r.costNote||'缺少可适用价格或计价分项，不代表免费')}</dd><dt>响应耗时 / 首字等待</dt><dd>${r.durationMs==null?'未采集':fmt(r.durationMs)+' ms'} / ${r.firstTextMs==null?'首文字未测得':fmt(r.firstTextMs)+' ms（首文字）'}<br>首个内容（含推理 / 工具）：${r.ttftMs==null?'未测得':fmt(r.ttftMs)+' ms'} · 输出速度 ${r.outputSpeed==null?'未可靠测得':fmt(r.outputSpeed)+' Token/s'}</dd><dt>重试 / 备用线路</dt><dd>${r.attempts==null?'未采集；无法确认上游尝试次数':`${r.attempts} 次上游尝试 · ${r.retried?'发生重试，按最终结果计数':'未发生网关重试'}`}</dd>${r.attemptDetails?.length?`<dt>尝试明细</dt><dd>${r.attemptDetails.map(a=>`${esc(a.provider||'未知')} / ${esc(a.model||'未知')} · HTTP ${esc(a.status)}${a.handedOn?' · 转交后续尝试':''}${a.error?' · '+esc(a.error):''}`).join('<br>')}</dd>`:''}${r.error?`<dt>错误</dt><dd class="error-full">${esc(r.error)}</dd>`:''}<dt>证据</dt><dd>${esc(r.evidence)}</dd><dt>来源文件</dt><dd class="mono">${esc(r.source)}</dd></dl><div class="notice">${r.gaps.map(esc).join('<br>')}</div><h3>上下文构成</h3>${contextPartsHtml(r)}`, `<button type="button" data-action="close-editor">关闭</button>${r.sessionId?`<button type="button" data-daily="related-conversation" data-agent="${r.agent}" data-session="${esc(r.sessionId)}">对应对话</button><button type="button" data-daily="related-context" data-agent="${r.agent}" data-session="${esc(r.sessionId)}">查看上下文变化</button>`:'<span>会话未关联</span>'}`);
}
async function conversationDetail(agent,sid) {
  const data=await api('GET','/api/observation/session?'+obsParams({agent,session:sid,status:'',request:''}));
  const s=data.session;
  if(!s)throw new Error('当前筛选范围内没有此对话的记录');
  showEditor('read-only',s.title||'对话详情',`<dl><dt>原生会话</dt><dd class="mono">${esc(s.sessionId)}</dd><dt>项目目录</dt><dd>${esc(s.cwd||'未记录')}</dd><dt>主 / 子关系</dt><dd>${s.parentSessionId?`原生父会话引用：${esc(s.parentSessionId)}`:'未声明父会话；不推断关系'}</dd><dt>本次筛选内用量</dt><dd>${s.summary.calls} 次调用 · 输出 ${fmt(s.summary.tokens.output.value)} · ${costFmt(s.summary.cost)} · ${s.summary.unpriced} 次未计价</dd></dl><h3>相关调用</h3><div class="related-requests">${s.requests.map(r=>`<button type="button" data-daily="request" data-id="${r.id}">${when(r.time)} · ${esc(r.servedModel||r.model||'模型未知')} · ${statusNames[r.status]}</button>`).join('')}</div><h3>消息与工具事件</h3>${data.contentSource==='gateway-on-demand'?'<p class="notice">来自已开启的 Hub 网关录制；输入含重放历史，每条是请求快照，不作为新增消息计数。</p>':''}${data.contentSource==='native-on-demand'?'<p class="form-intro">从客户端已有原生日志按需读取；这次查看不会在 Hub 额外保存正文。用量以当前索引为准。</p>':''}${data.contentError?`<p class="notice warn">原生正文读取失败：${esc(data.contentError)}${data.contentSource==='retained'?'；正在显示尚未过期的留存副本。':''}</p>`:''}${data.messages.length?data.messages.map(m=>`<details class="message-event"><summary>${esc(m.role)} · ${when(m.time)}</summary><pre>${esc(m.body||'正文未留存')}</pre></details>`).join(''):blank('当前范围没有可读的原生正文','原始日志可能已删除、不可读或没有记录消息。额外正文留存默认关闭；开启也不能还原客户端未记录的内容。')}`,`<button type="button" data-action="close-editor">关闭</button><button type="button" data-daily="related-context" data-agent="${agent}" data-session="${esc(sid)}">查看上下文</button>`);
}
function gatewaySettings(g) {
  return `<section class="card gateway-settings"><div class="card-heading"><div><h2>Hub 本地网关</h2><p>使用仓库内网关的流式转发、用量账本和上下文分析。Hub 的模型库、预览、冲突保护、备份与恢复继续管理客户端配置。</p></div>${tag(g?.state==='running'?(g.enabled?'运行中 · 已应用接入配置':'运行中 · 待应用接入配置'):g?.state==='failed'?'启动失败':'尚未运行',g?.state==='running'?'green':'')}</div><p>${esc(g?.url||'http://127.0.0.1:8789')} · Agent Hub ${esc(g?.version||'hub-2')} · 随应用维护，无外部引擎</p>${g?.error?`<div class="notice warn">${esc(g.error)}</div>`:''}<p>自定义模型走网关；Codex 保留 ChatGPT 登录。pi 内置 OAuth 与 dsh 内置 DeepSeek 账号保持原生线路。原生与网关记录按响应 ID 合并，不按时间猜测。</p><div class="actions"><button data-daily="gateway-start">启动 / 恢复网关</button><button class="primary" data-daily="gateway-connect">预览接入已启用 Agent</button><button data-daily="gateway-disconnect">预览恢复直连</button></div><small>接入与恢复需要应用预览后生效。已有会话可能保留旧配置，请重启或新建会话。退出 Hub 会停止其网关；下次启动会自动恢复已启用的网关。</small></section>`;
}
function contextPartsHtml(r) {
  const p=r.contextParts;
  return p?`<p class="notice">分项为估算 · ${esc(p.method)}${p.counted?' 总量已由供应商用量校准。':' 总量尚未由供应商用量校准。'}${p.held?' 上游持有的前序响应内容不可完整拆分。':''}</p><table class="context-parts"><thead><tr><th>请求构成</th><th>估算 Token</th></tr></thead><tbody>${p.parts.map(x=>`<tr><td>${esc(({system:'系统指令',tools:'工具定义',history:'历史消息',results:'工具结果',memory:'文件 / 记忆',user:'用户输入',assistant:'助手历史'})[x.kind]||x.kind)}</td><td>${fmt(x.tokens)}</td></tr>`).join('')}</tbody></table>`:'<p class="form-intro">当前来源没有完整请求构成。原生日志无法精确拆分系统指令、工具定义、文件和记忆；没有填入推测比例。</p>';
}
function renderSettings() {
  const status=daily.obs?.status, settings=status?.settings;
  $('#content').innerHTML=dailyHeading('LOCAL SETTINGS','设置','常用操作使用表单；配置预览、冲突保护和恢复继续沿用原有事务。')+
    `<div class="settings-links"><a class="card" href="#providers">${icon('layers')}<strong>供应商与连接</strong><p>维护端点、密钥和按 Agent 覆盖的连接。</p>${icon('arrow')}</a><a class="card" href="#models">${icon('grid')}<strong>模型库</strong><p>按连接管理模型、窗口与参考单价。</p>${icon('arrow')}</a><a class="card" href="#sync">${icon('sync')}<strong>方案与高级同步</strong><p>保存组合、Claude 档位、预览及恢复。</p>${icon('arrow')}</a></div>
    <section class="card collection-settings"><div class="card-heading"><div><h2>本机观测采集</h2><p>采用 Magpie 的本机读取原则：默认自动索引用量，已有原生对话按需打开；额外正文副本默认关闭。网关接入另行预览并应用；服务退出停止采集与其网关。</p></div></div>${settings?`<label class="setting-row"><span><strong>采集调用元数据</strong><small>默认开启。读取模型、Token、状态、目录与会话标识；关闭暂停 Hub 自动导入，主动打开已有对话仍可读取原文件。网关账本随转发记录；预览恢复直连后可停止网关。</small></span><input type="checkbox" id="collection-enabled" ${settings.enabled?'checked':''}></label><label class="setting-row"><span><strong>额外留存对话正文</strong><small>默认关闭；无需开启也能按需查看客户端已有正文。副本最多保留 7 天、256 MiB，仅在本机；关闭会清除副本，不删除客户端原文件。已知密钥脱敏不能保证清除所有私人文字。</small></span><input type="checkbox" id="collection-content" ${settings.content?'checked':''}></label><label class="setting-row"><span><strong>用量索引保留期限</strong><small>默认不自动过期；额外正文副本独立限制为 7 天 / 256 MiB。不删除客户端原文件。</small></span><select id="collection-retention">${[0,7,30,90,365].map(n=>`<option value="${n}" ${settings.retentionDays===n?'selected':''}>${n?n+' 天':'不自动过期'}</option>`).join('')}</select></label><div class="table-footer"><span>${status.records} 条调用索引 · ${status.contentRecords} 条正文事件 · ${fmt((status.contentBytes||0)/1024/1024)} MiB / 256 MiB</span><div class="actions"><button data-daily="cleanup">清理过期记录</button><button class="primary" data-daily="save-collection">保存采集设置</button></div></div><div class="collection-path mono">${esc(status.path)}</div><div class="danger-zone"><div><strong>清空 Hub 观测记录</strong><p>同时停止采集，并设定清理水位；再次开启时不会重新导入清理之前的旧事件。</p></div><button class="danger" data-daily="clear-observation">清空记录</button></div>`:blank('正在读取设置',daily.obsError||'本机索引加载中…')}</section>`+collectionNotice(status)+
    `<section class="card service-settings"><strong>本地服务</strong><p>关闭浏览器窗口后，后台本地服务仍可继续索引。需要结束时可以在这里退出。</p><button data-daily="stop-service">退出本地服务</button></section><details class="diagnostics"><summary>数据口径与能力缺口</summary><p>采集来源：Claude Code JSONL、Codex rollout（CLI / 桌面共用）、pi JSONL、dsh 最新代 JSONL / zstd。实际历史扫描与隔离回归不等同于真实新客户端调用验证。</p><p>请求身份按原生 ID，不用时间猜测归属。网关记录网络耗时、首个内容等待、重试与估算构成；直连只有原生记录。缺少价格不等于免费。参考成本与实际扣费、订阅额度分别表达。</p></details>`;
}

const settingsWithGateway=renderSettings;
renderSettings=function(){settingsWithGateway();$('.settings-links')?.insertAdjacentHTML('afterend',gatewaySettings(daily.obs?.status?.gateway));};

document.addEventListener('submit',async event=>{
  if(event.target.id!=='editor-form'||state.editor?.type!=='quick-switch')return;
  event.preventDefault();event.stopImmediatePropagation();
  const values=new FormData($('#editor-form'));
  try{await busy('正在生成配置预览…',()=>previewQuick(values));}catch(e){$('#editor-error').textContent=e.message;$('#editor-error').hidden=false;}
},true);
document.addEventListener('change',async event=>{
  const el=event.target;
  if(el.dataset.instance){daily.chosen[el.dataset.instance]=el.value;renderAgents();}
  if(el.id==='quick-provider'){daily.quick.preview=null;quickModels();}
  if(el.closest('#editor-form')&&daily.quick?.preview&&state.editor?.type==='quick-switch'){
    daily.quick.preview=null;$('#quick-result').innerHTML='';$('#editor-footer').innerHTML='<button type="button" data-action="close-editor">取消</button><button type="submit" class="primary">预览切换</button>';
  }
  if(el.dataset.obsFilter){daily.filter[el.dataset.obsFilter]=el.value;daily.filter.page=1;daily.filter.sessionPage=1;if(el.dataset.obsFilter==='model')delete daily.filter.modelUnknown;if(el.dataset.obsFilter==='days'){daily.filter.start='';daily.filter.end='';}await loadObservation();}
});
document.addEventListener('keydown',event=>{if(event.target.id==='obs-search'&&event.key==='Enter')$('[data-daily="search"]').click();});
document.addEventListener('click',async event=>{
  const tab=event.target.closest('[data-tab]');
  if(tab){daily.tab=tab.dataset.tab;renderObservation();return;}
  const b=event.target.closest('[data-daily]');if(!b||b.disabled||state.busy)return;
  const a=b.dataset.daily;
  try{
    if(a==='detect')await loadInventory(true);
    else if(a==='gateway-start'){await busy('正在校验并启动网关…',()=>api('POST','/api/gateway/start',{}));await loadObservation();toast('网关已启动；接入客户端请应用配置预览。');}
    else if(a==='gateway-connect'||a==='gateway-disconnect'){
      const p=await busy('正在生成可恢复的网关配置预览…',()=>api('POST','/api/gateway/preview',{enabled:a==='gateway-connect'}));
      if(p){daily.gatewayPreview=p;showEditor('read-only','网关接入 / 恢复预览',`<p>以下文件通过现有事务写入，自动保留恢复点；凭据内容不会出现在预览里。</p>${p.files.map(f=>`<p class="path-line">${esc(f.action)} · ${esc(f.path)}</p>`).join('')}<div class="notice">${p.warnings.map(esc).join('<br>')}</div>`,`<button type="button" data-action="close-editor">稍后应用</button><button type="button" class="primary" data-daily="gateway-apply">应用配置并保存恢复点</button>`);}
    }
    else if(a==='gateway-apply'){
      const r=await busy('正在写入网关配置…',()=>api('POST','/api/apply',{planId:daily.gatewayPreview.planId}));
      if(r){closeEditor();await reload();await loadObservation();toast('配置已写入并保存恢复点；请新建会话或重启客户端。');}
    }
    else if(a==='agent-detail')agentDetail(b.dataset.agent);
    else if(a==='switch')openQuickSwitch(b.dataset.agent);
    else if(a==='check') {b.disabled=true;b.textContent='检查中…';const result=await api('POST','/api/agents/check',{instanceId:b.dataset.id});toast(result.message,result.updateState==='check-failed'?'error':'ok');await loadInventory();}
    else if(a==='update'){
      const e=daily.inventory.instances.find(e=>e.id===b.dataset.id);
      if(await confirmDialog('更新这个安装实例',`${labels[e.agent]} · ${e.version} → ${e.latestVersion}。只更新 ${e.path}；运行中的会话可能需要关闭和重启。`,[],'执行更新',false)){
        await api('POST','/api/agents/update',{instanceId:e.id});await loadInventory();
      }
    }
    else if(a==='quick-apply'){
      const result=await busy('正在写入并保存恢复点…',()=>api('POST','/api/apply',{planId:daily.quick.preview.planId}));
      if(!result)return;
      closeEditor();await reload();toast(((result.details||[]).map(x=>x.message).join('；')||'配置写入完成')+'；请启动新会话或重启客户端。',result.ok?'ok':'error');
    }
    else if(a==='scan'){daily.filter.end=Date.now()/1000;await api('POST','/api/observation/refresh',{});await loadObservation();}
    else if(a==='obs-retry')await loadObservation();
    else if(a==='search'){daily.filter.q=$('#obs-search').value;daily.filter.page=1;await loadObservation();}
    else if(a==='session-prev'||a==='session-next'){daily.filter.sessionPage=daily.obs.sessionPage+(a==='session-next'?1:-1);await loadObservation();}
    else if(a==='obs-prev'||a==='obs-next'){daily.filter.page=daily.obs.page+(a==='obs-next'?1:-1);await loadObservation();}
    else if(a==='reset-obs'){daily.filter={days:7,agent:'',model:'',provider:'',status:'',q:'',page:1,start:'',end:''};await loadObservation();}
    else if(a==='exact-range'){
      for(const k of ['start','end'])daily.filter[k]=new Date($(`[data-time="${k}"]`).value).getTime()/1000;
      daily.filter.page=1;await loadObservation();
    }
    else if(a==='metric'){daily.tab='requests';if(b.dataset.filter)daily.filter[b.dataset.filter]=b.dataset.value;daily.filter.page=1;await loadObservation();}
    else if(a==='distribution'){
      if(b.dataset.value==='未知'){daily.filter[b.dataset.dimension+'Unknown']='1';daily.filter[b.dataset.dimension]='';daily.filter.page=1;daily.tab='requests';await loadObservation();}
      else{delete daily.filter[b.dataset.dimension+'Unknown'];daily.filter[b.dataset.dimension]=b.dataset.value;daily.filter.page=1;daily.tab='requests';await loadObservation();}
    }
    else if(a==='trend'){daily.filter.start=Date.parse(b.dataset.date+'T00:00:00Z')/1000;daily.filter.end=daily.filter.start+86400-0.001;daily.filter.page=1;daily.tab='requests';await loadObservation();}
    else if(a==='request'){if($('#editor-dialog').open)closeEditor();await requestDetail(b.dataset.id);}
    else if(a==='conversation'||a==='related-conversation'){if($('#editor-dialog').open)closeEditor();await conversationDetail(b.dataset.agent,b.dataset.session);}
    else if(a==='related-context'){closeEditor();daily.filter.agent=b.dataset.agent;daily.filter.session=b.dataset.session;daily.filter.status='';daily.filter.request='';daily.filter.page=1;daily.tab='context';state.page='observe';history.replaceState(null,'','#observe');await loadObservation();renderChrome();}
    else if(a==='save-collection'){
      await api('PUT','/api/observation/settings',{enabled:$('#collection-enabled').checked,content:$('#collection-content').checked,retentionDays:Number($('#collection-retention').value)});await loadObservation();toast('采集设置已保存。');
    }
    else if(a==='cleanup'){await api('POST','/api/observation/cleanup',{});await loadObservation();toast('已清理过期索引。');}
    else if(a==='stop-service'){
      if(await confirmDialog('退出本地服务','停止本地 HTTP 服务与只读采集；保存的采集开关下次启动后继续生效。已接入客户端需等待服务重新启动，或先预览恢复直连。',[],'退出服务',false)){
        await api('POST','/api/shutdown',{});daily.obs=null;daily.inventory=null;state.loaded=false;
        $('#content').innerHTML=blank('本地服务已退出','下次双击 Launch GUI.vbs 或运行 hub.cmd gui 即可重新启动。');$('#save-state').textContent='服务已退出';
      }
    }
    else if(a==='clear-observation'){
      if(await confirmDialog('清空 Hub 观测记录','删除 Hub 已保存的调用索引与正文，同时停止采集。客户端原生日志保留。',[],'清空并停止',true)){
        await api('POST','/api/observation/cleanup',{clear:true});await loadObservation();toast('已清空并停止采集。');
      }
    }
  }catch(e){toast(e.message,'error');if(state.editor?.type==='quick-switch'){$('#editor-error').textContent=e.message;$('#editor-error').hidden=false;}}
});
setInterval(()=>{
  if(state.loaded&&state.page==='agents'&&daily.inventory?.jobs.some(j=>j.state==='running'))loadInventory();
  if(state.loaded&&['observe','settings'].includes(state.page)&&!daily.obsLoading&&daily.obs?.status?.collector?.state==='reading')loadObservation();
},2500);
setInterval(()=>{if(state.loaded&&state.page==='observe'&&!daily.obsLoading&&daily.obs?.status?.settings?.enabled)loadObservation();},30000);
