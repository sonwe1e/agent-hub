"use strict";

// Optional batch UI over the existing per-item preview/apply API. Importing or
// selecting a skill does not itself write a client; each reviewed plan retains
// its normal conflict, source fingerprint, recovery and transaction checks.
const skillsBulk = {selected:new Set(), entries:[], running:false, generation:0, ownsDialog:false};
const skillsBulkBaseLibrary = renderLibrary;
const skillsBulkBasePreview = showWorkPreview;
const skillsBulkBaseEditor = showEditor;

// Keep batch actions visible even when many per-client differences are listed.
// The class is switched for every editor so ordinary dialogs keep their layout.
const skillsBulkStyle=document.createElement("style");
skillsBulkStyle.textContent=`
  #editor-dialog.skills-bulk-dialog[open] { display:flex; flex-direction:column; overflow:hidden; }
  #editor-dialog.skills-bulk-dialog .dialog-header { flex:none; }
  #editor-dialog.skills-bulk-dialog #editor-form { display:flex; flex-direction:column; flex:1 1 auto; min-height:0; overflow:hidden; }
  #editor-dialog.skills-bulk-dialog #editor-body { flex:1 1 auto; min-height:0; overflow:auto; }
  #editor-dialog.skills-bulk-dialog #editor-footer { flex:none; flex-wrap:wrap; }
`;
document.head.appendChild(skillsBulkStyle);
showEditor = function(type,title,body,footer,context={}) {
  $("#editor-dialog").classList.toggle("skills-bulk-dialog",context.kind==="skills-bulk");
  return skillsBulkBaseEditor(type,title,body,footer,context);
};

renderLibrary = function() {
  skillsBulkBaseLibrary();
  const items=(workFeatures.library?.items||[]).filter(x=>x.kind==="skill");
  const valid=new Set(items.map(x=>x.id));
  for (const id of skillsBulk.selected) if(!valid.has(id)) skillsBulk.selected.delete(id);
  const heading=$("#content .page-heading");
  if (!heading || !items.length) return;
  heading.insertAdjacentHTML("afterend", '<section class="card feature-section" id="skills-bulk-toolbar" aria-label="批量技能操作"><div class="actions"><button type="button" data-skill-bulk="toggle-skills">全选技能</button><button type="button" class="primary" data-skill-bulk="preview" disabled>预览所选技能</button><span data-skills-count aria-live="polite"></span></div><p>选择已管理的技能，查看每端差异后，可统一选择需要覆盖的冲突；不会修改同步模式。</p></section>');
  for (const button of $$('#content [data-feature="edit-item"]')) {
    const item=items.find(x=>x.id===button.dataset.id);
    if(!item) continue;
    const node=button.closest(".feature-row")?.querySelector(".feature-row-main");
    if(node) node.insertAdjacentHTML("afterbegin", '<label class="check-label"><input type="checkbox" data-skill-bulk-item="'+esc(item.id)+'" '+(skillsBulk.selected.has(item.id)?'checked':'')+'><span>选择 '+esc(item.name)+'</span></label>');
  }
  skillsBulkUpdateSelection();
};

function skillsBulkUpdateSelection() {
  const checkboxes=$$('#content [data-skill-bulk-item]');
  const count=$("[data-skills-count]"), toggle=$('[data-skill-bulk="toggle-skills"]');
  const preview=$('[data-skill-bulk="preview"]');
  if(count) count.textContent="已选择 "+skillsBulk.selected.size+" / "+checkboxes.length+" 项技能";
  if(toggle) toggle.textContent=checkboxes.length && checkboxes.every(x=>x.checked)?"取消全选":"全选技能";
  if(preview) preview.disabled=skillsBulk.selected.size===0 || skillsBulk.running;
}

showWorkPreview = function(plan) {
  skillsBulkBasePreview(plan);
  const boxes=$$('#editor-body [name="work-force"]');
  if(boxes.length<1) return;
  const first=$("#editor-body .feature-list");
  if(first) first.insertAdjacentHTML("beforebegin", '<div class="actions"><button type="button" data-skill-bulk="toggle-single-conflicts">全选可覆盖冲突</button><span>不包含待恢复事务。</span></div>');
};
function skillsBulkUpdateSingleForce() {
  const button=$('[data-skill-bulk="toggle-single-conflicts"]');
  const boxes=$$('#editor-body [name="work-force"]');
  if(button) button.textContent=boxes.length && boxes.every(x=>x.checked)?"取消全选冲突":"全选可覆盖冲突";
}

function skillsBulkDiff(row) {
  const d=row.diff||{};
  return [d.text||"",d.added?.length?"新增："+d.added.join("、"):"",d.removed?.length?"移除："+d.removed.join("、"):"",d.changed?.length?"修改："+d.changed.join("、"):"",...Object.values(d.textFiles||{})].filter(Boolean).join("\n");
}
function skillsBulkCanForce(entry, row) {
  const pending=workFeatures.library?.items?.find(x=>x.id===entry.id)?.targetPending||{};
  return !entry.outcome && (row.status==="conflict"||row.forced) && !row.blockedByRecovery && !pending[row.agent];
}
function skillsBulkRow(entry) {
  if(entry.error) return '<section class="feature-section" data-skills-entry="'+esc(entry.id)+'"><h3>'+esc(entry.name)+'</h3><p class="notice warn">预览失败：'+esc(entry.error)+'</p></section>';
  const plan=entry.plan||entry.previousPlan, result=plan?.results||[];
  const statuses=result.map(row=>{
    const diff=skillsBulkDiff(row), label=workStatusText[row.status]||row.status||"未知";
    const canChoose=skillsBulkCanForce(entry,row);
    return '<div class="feature-row"><div class="feature-row-main"><strong>'+esc(workAgentNames[row.agent]||row.agent)+' · '+esc(label)+(row.forced?' · 已选择覆盖':'')+'</strong><small class="feature-path">'+esc(row.path||"")+'</small>'+(row.detail?'<small>'+esc(row.detail)+'</small>':'')+(diff?'<details><summary>查看差异</summary><pre class="feature-diff">'+esc(diff)+'</pre></details>':'')+(canChoose?'<label class="check-label"><input type="checkbox" data-skills-force="'+esc(entry.id)+'" data-agent="'+esc(row.agent)+'" data-was-forced="'+(row.forced?'true':'false')+'" '+(row.forced?'checked':'')+'><span>覆盖此端的已有修改</span></label>':'')+'</div></div>';
  }).join("");
  const notes=(plan?.warnings||[]).map(w=>'<p class="notice warn">'+esc(w)+'</p>').join("");
  const completion=entry.outcome?'<p class="notice '+(entry.outcome.ok?'':'warn')+'">'+esc(entry.outcome.message)+'</p>':'';
  return '<section class="feature-section" data-skills-entry="'+esc(entry.id)+'"><h3>'+esc(entry.name)+'</h3><p>来源：'+esc(entry.source)+' · 使用当前主来源</p>'+completion+notes+'<div class="feature-list">'+statuses+'</div></section>';
}
function skillsBulkRenderPreview() {
  const entries=skillsBulk.entries, count=entries.filter(e=>e.plan&&!e.plan.hasConflicts&&!e.outcome).length;
  const blocked=entries.filter(e=>e.error||e.plan?.hasConflicts).length;
  const hasForce=entries.some(e=>(e.plan?.results||[]).some(r=>skillsBulkCanForce(e,r)));
  const body='<p>本次使用各技能的当前主来源。每项单独写入并保存恢复点；未完成的事务不可覆盖。复核后才会修改客户端文件。</p>'+(blocked?'<p class="notice warn">'+esc(blocked)+' 项尚有冲突或无法预览；可以先处理其余已就绪项。</p>':'')+(hasForce?'<div class="actions"><button type="button" data-skill-bulk="toggle-conflicts">全选可覆盖冲突</button><span data-skills-force-count aria-live="polite"></span></div>':'')+entries.map(skillsBulkRow).join("");
  const footer='<button type="button" data-action="close-editor">关闭</button>'+(hasForce?'<button type="button" data-skill-bulk="repreview">按覆盖选择重新预览</button>':'')+(count?'<button type="button" class="primary" data-skill-bulk="apply">确认并应用 '+count+' 项</button>':'');
  skillsBulk.ownsDialog=true;
  showEditor("read-only","批量发布技能",body,footer,{kind:"skills-bulk"});
  skillsBulkUpdateForce();
}
function skillsBulkUpdateForce() {
  const boxes=$$('#editor-body [data-skills-force]');
  const count=$('[data-skills-force-count]'), button=$('[data-skill-bulk="toggle-conflicts"]');
  if(count) count.textContent="已选 "+boxes.filter(x=>x.checked).length+" / "+boxes.length+" 处可覆盖冲突";
  if(button) button.textContent=boxes.length && boxes.every(x=>x.checked)?"取消全选冲突":"全选可覆盖冲突";
  const dirty=boxes.some(x=>x.checked !== (x.dataset.wasForced==="true"));
  const apply=$('[data-skill-bulk="apply"]');
  if(apply) {
    apply.disabled=dirty;
    apply.title=dirty?"覆盖选择已变更，请先重新预览":"";
  }
}
function skillsBulkReadForce() {
  const map={};
  for(const checkbox of $$('#editor-body [data-skills-force]:checked')) {
    const id=checkbox.dataset.skillsForce;
    (map[id] ||= []).push(checkbox.dataset.agent);
  }
  return map;
}
async function skillsBulkPreviewAll() {
  const items=(workFeatures.library?.items||[]).filter(x=>x.kind==="skill" && skillsBulk.selected.has(x.id));
  if(!items.length) throw new Error("请先选择要发布的技能。");
  const generation=++skillsBulk.generation;
  skillsBulk.entries=items.map(x=>({id:x.id,name:x.name,source:x.source,plan:null,error:null,outcome:null}));
  skillsBulk.ownsDialog=true;
  showEditor("read-only","批量发布技能",'<p>正在逐项生成预览；不会修改客户端文件。</p>','<button type="button" data-action="close-editor">关闭</button>',{kind:"skills-bulk"});
  for(const entry of skillsBulk.entries) {
    if(!skillsBulkCurrent(generation)) break;
    try {
      const plan=await api("POST","/api/capabilities/"+encodeURIComponent(entry.id)+"/preview",{});
      if(!skillsBulkCurrent(generation)) { await skillsBulkDiscard(plan.planId); break; }
      entry.plan=plan;
    }
    catch(e) { entry.error=e.message; }
  }
  if(skillsBulkCurrent(generation)) skillsBulkRenderPreview();
}
async function skillsBulkRepreview() {
  const generation=skillsBulk.generation;
  const selection=skillsBulkReadForce();
  for(const checkbox of $$('#editor-body [data-skills-force]')) checkbox.disabled=true;
  const toggle=$('[data-skill-bulk="toggle-conflicts"]');
  if(toggle) toggle.disabled=true;
  for(const entry of skillsBulk.entries) {
    if(!entry.plan || entry.outcome) continue;
    if(!skillsBulkCurrent(generation)) break;
    const old=entry.plan;
    try {
      const next=await api("POST","/api/capabilities/"+encodeURIComponent(entry.id)+"/preview",{forceTargets:selection[entry.id]||[]});
      if(!skillsBulkCurrent(generation)) { await skillsBulkDiscard(next.planId); break; }
      entry.plan=next;
    } catch(e) { entry.plan=null; entry.error=e.message; }
    finally { await skillsBulkDiscard(old.planId); }
  }
  if(skillsBulkCurrent(generation)) skillsBulkRenderPreview();
}
async function skillsBulkApply() {
  if($$('#editor-body [data-skills-force]').some(x=>x.checked !== (x.dataset.wasForced==="true")))
    throw new Error("覆盖选择已变更，请先重新预览。");
  const generation=skillsBulk.generation;
  const ready=skillsBulk.entries.filter(e=>e.plan&&!e.plan.hasConflicts&&!e.outcome);
  if(!ready.length) throw new Error("没有可应用的预览，请先处理冲突。");
  const forced=ready.reduce((n,e)=>n+(e.plan.results||[]).filter(r=>r.forced).length,0);
  const pending=skillsBulk.entries.length-ready.length;
  const description="将逐项应用已审阅的当前主来源，覆盖 "+forced+" 个已选冲突目标；每项保留独立恢复点。"+(pending?"另有 "+pending+" 项未就绪，不会应用。":"");
  if(!await confirmDialog("应用批量发布？",description,ready.map(e=>e.name),"确认应用",false)) return;
  if(!skillsBulkCurrent(generation)) return;
  state.busy=true;
  const progress=document.createElement("p");
  progress.className="notice";
  progress.setAttribute("role","status");
  $("#editor-body").prepend(progress);
  $("#editor-dialog").setAttribute("aria-busy","true");
  try {
    for(const [index,entry] of ready.entries()) {
      progress.textContent="正在应用 "+(index+1)+" / "+ready.length+"："+entry.name;
      const plan=entry.plan;
      try {
        const receipt=await api("POST","/api/capabilities/apply",{planId:plan.planId});
        entry.outcome={ok:!receipt.partial,message:receipt.partial?"部分目标已发布；请查看各端状态。":"已发布，恢复点："+(receipt.transactionId||"已记录")};
      } catch(e) {
        const uncertain=/请求超时|无法连接本地服务/.test(e.message);
        entry.outcome={ok:false,message:uncertain?"结果待确认："+e.message+" 请查看记录和目标状态后再重新预览。":"应用失败："+e.message};
      }
      // A plan may be consumed even when the request fails: never retry it as if
      // untouched. A fresh explicit preview is required for another attempt.
      entry.previousPlan=plan; entry.plan=null;
      await skillsBulkDiscard(plan.planId);
    }
    await loadWorkFeatures(true);
  } finally { state.busy=false; $("#editor-dialog").removeAttribute("aria-busy"); }
  if(skillsBulkCurrent(generation)) skillsBulkRenderPreview();
}

function skillsBulkCurrent(generation) {
  return generation===skillsBulk.generation && skillsBulk.ownsDialog &&
    $("#editor-dialog").open && state.editor?.kind==="skills-bulk";
}
async function skillsBulkDiscard(planId) {
  if(!planId) return;
  try { await api("POST","/api/capabilities/discard",{planId}); } catch(_) { /* Plan expires automatically. */ }
}
$("#editor-dialog").addEventListener("close",()=>{
  $("#editor-dialog").classList.remove("skills-bulk-dialog");
  if(!skillsBulk.ownsDialog) return;
  skillsBulk.ownsDialog=false; skillsBulk.generation++;
  for(const entry of skillsBulk.entries) if(entry.plan) {
    const id=entry.plan.planId; entry.plan=null; void skillsBulkDiscard(id);
  }
});

document.addEventListener("change",event=>{
  const checkbox=event.target;
  if(checkbox.matches?.("[data-skill-bulk-item]")) {
    if(checkbox.checked) skillsBulk.selected.add(checkbox.dataset.skillBulkItem); else skillsBulk.selected.delete(checkbox.dataset.skillBulkItem);
    skillsBulkUpdateSelection();
  }
  if(checkbox.matches?.("[data-skills-force]")) skillsBulkUpdateForce();
  if(checkbox.matches?.('[name="work-force"]')) skillsBulkUpdateSingleForce();
});
document.addEventListener("click",async event=>{
  const button=event.target.closest("[data-skill-bulk]");
  if(!button || button.disabled || state.busy || skillsBulk.running) return;
  const action=button.dataset.skillBulk;
  if(action==="toggle-skills") {
    const boxes=$$('#content [data-skill-bulk-item]'), select=boxes.some(x=>!x.checked);
    for(const checkbox of boxes) { checkbox.checked=select; if(select)skillsBulk.selected.add(checkbox.dataset.skillBulkItem); else skillsBulk.selected.delete(checkbox.dataset.skillBulkItem); }
    skillsBulkUpdateSelection(); return;
  }
  if(action==="toggle-single-conflicts") {
    const boxes=$$('#editor-body [name="work-force"]'); const select=boxes.some(x=>!x.checked);
    for(const checkbox of boxes) checkbox.checked=select;
    skillsBulkUpdateSingleForce(); return;
  }
  if(action==="toggle-conflicts") {
    const boxes=$$('#editor-body [data-skills-force]'), select=boxes.some(x=>!x.checked);
    for(const checkbox of boxes)checkbox.checked=select;
    skillsBulkUpdateForce(); return;
  }
  skillsBulk.running=true; button.disabled=true;
  try {
    if(action==="preview") await skillsBulkPreviewAll();
    else if(action==="repreview") await skillsBulkRepreview();
    else if(action==="apply") await skillsBulkApply();
  } catch(e) { workError(e); }
  finally { skillsBulk.running=false; if(button.isConnected)button.disabled=false; skillsBulkUpdateSelection(); }
});
