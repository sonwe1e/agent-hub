"use strict";

// Shared account evidence: local indexed calls and explicitly refreshed upstream
// snapshots are kept separate because neither proves the other's completeness.
function subscriptionMonitor(view){
  const activity=view.activity, quota=view.quota;
  const last=activity?.last;
  const event=view.activityError?'本机记录读取失败，请刷新状态':last?`${last.status==='succeeded'?'成功':last.status==='interrupted'?'中断':last.status==='failed'?'失败':'状态未知'} · ${esc(when(last.time))}${last.agent?` · ${esc(last.agent)}`:''}${last.model?` · ${esc(last.model)}`:''}`:view.loading?'正在读取本机记录…':'暂无可关联的本机调用记录';
  const brief=row=>row?`${esc(when(row.time))}${row.agent?` · ${esc(row.agent)}`:''}${row.model?` · ${esc(row.model)}`:''}`:'暂无记录';
  const history=activity?`<p><strong>最近成功</strong> ${brief(activity.lastSuccess)}</p><p><strong>最近失败或中断</strong> ${brief(activity.lastError)}</p>${activity.lastError?.error?`<p class="subscription-alert">${esc(activity.lastError.error)}</p>`:''}`:'';
  const coverage=activity&&!activity.collectionEnabled?'采集已暂停，线路记录可能不完整；历史记录不区分之前的账号。':'来自本机线路观测索引，可能有采集延迟；历史记录不区分之前的账号。';
  let quotaText='<p class="subscription-empty">尚未查询上游额度；未知不代表没有额度。</p>';
  if(view.quotaError)quotaText=`<p class="subscription-alert" role="alert">${esc(view.quotaError)}</p>`;
  else if(quota){
    const rows=(quota.windows||[]).map(w=>{
      let label=w.name||'额度窗口';
      if(w.windowSeconds===18000)label='5 小时窗口';
      else if(w.windowSeconds===604800)label='每周窗口';
      const used=typeof w.usedPercent==='number'&&Number.isFinite(w.usedPercent)?`已使用 ${fmt(w.usedPercent)}%`:'已使用比例未知';
      const amount=typeof w.used==='number'&&typeof w.total==='number'?` · ${fmt(w.used)} / ${fmt(w.total)}`:'';
      const deadline=w.resetsAt?` · 重置 ${esc(when(w.resetsAt))}`:w.expiresAt?` · 到期 ${esc(when(w.expiresAt))}`:'';
      return `<div><strong>${esc(label)}</strong><span>${used}${amount}${deadline}</span></div>`;
    }).join('');
    quotaText=`<p class="subscription-source">${esc(quota.source||'上游额度')} · 查询于 ${esc(when(quota.checkedAt))}${quota.plan?` · ${esc(quota.plan)}`:''} · 仅对应查询时的账号</p>${rows?`<div class="subscription-windows">${rows}</div>`:'<p class="subscription-empty">上游没有返回可显示的额度窗口。</p>'}`;
  }
  return `<section class="subscription-monitor" aria-label="账号状态与用量"><h3>账号状态与用量</h3><p><strong>最近调用</strong> ${event}</p>${history}<p class="subscription-source">${esc(coverage)}</p>${quotaText}</section>`;
}

async function loadSubscriptionActivity(view,provider){
  try{view.activity=await api('GET',`/api/subscriptions/activity?provider=${encodeURIComponent(provider)}`);view.activityError='';}
  catch(e){view.activity=null;view.activityError=e.message;}
}
