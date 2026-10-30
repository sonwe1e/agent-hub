"use strict";

const codexSubscriptionView={data:null,error:"",loading:false,working:false,painted:"",activity:null,activityError:"",quota:null,quotaError:""};
const settingsBeforeCodexSubscription=renderSettings;
renderSettings=function(){
  settingsBeforeCodexSubscription();
  $('.gateway-settings')?.insertAdjacentHTML('afterend','<section class="card workbuddy-settings" id="codex-subscription-settings" aria-live="polite" tabindex="-1"></section>');
  paintCodexSubscription();
  if(!codexSubscriptionView.data&&!codexSubscriptionView.loading&&!codexSubscriptionView.error)loadCodexSubscription();
};

function paintCodexSubscription(){
  const el=$('#codex-subscription-settings');if(!el)return;
  const d=codexSubscriptionView.data;
  const label=d?.connected?(d.expired?'已接入 · 登录已过期':'已接入'):d?.signedIn?'已登录 · 待读取模型':'尚未登录';
  const html=`<div class="card-heading"><div><h2>Codex／ChatGPT 订阅共享</h2><p>把本机 Codex 的 ChatGPT 模型用于其他已启用的 Agent；使用时保持 Hub 运行。</p></div>${tag(label,d?.connected&&!d?.expired?'green':'')}</div>
    ${codexSubscriptionView.error?`<p class="notice warn" role="alert">${esc(codexSubscriptionView.error)}</p>`:''}
    <p>${esc(d?.message||'正在检查 Codex 的登录状态…')}${d?.connected?` · ${d.models} 个已保存模型`:''}</p>
    <div class="actions">${d?.signedIn?`<button class="primary" data-codex-subscription="connect" ${codexSubscriptionView.working||d.expired?'disabled':''}>${d.connected?'刷新模型并恢复服务':'读取模型并接入'}</button><button data-codex-subscription="quota" ${codexSubscriptionView.working||d.expired?'disabled':''}>查询额度</button>`:''}${d?.connected?`<button data-codex-subscription="disconnect" ${codexSubscriptionView.working?'disabled':''}>停用共享</button>`:''}<button data-codex-subscription="refresh" ${codexSubscriptionView.working?'disabled':''}>刷新状态</button></div>
    ${subscriptionMonitor(codexSubscriptionView)}
    <p class="card-footnote">Hub 按请求读取 Codex 当前的登录，不复制或刷新其登录凭据；过期后请先在 Codex 续期。此方式复用 Codex 登录，不是 OpenAI 独立应用授权流程。接入只保存 Hub 供应商，客户端需要选择模型并确认预览。</p>`;
  if(html===codexSubscriptionView.painted&&el.innerHTML)return;
  codexSubscriptionView.painted=html;el.innerHTML=html;
}

async function loadCodexSubscription(){
  if(codexSubscriptionView.loading)return;
  codexSubscriptionView.loading=true;
  try{codexSubscriptionView.data=await api('GET','/api/codex-subscription');codexSubscriptionView.error='';await loadSubscriptionActivity(codexSubscriptionView,'codex-chatgpt');}
  catch(e){codexSubscriptionView.error=e.message;}
  finally{codexSubscriptionView.loading=false;paintCodexSubscription();}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-codex-subscription]');if(!button||button.disabled)return;
  const action=button.dataset.codexSubscription;
  if(action==='disconnect'&&!await confirmDialog('停用 Codex 订阅共享？','不会退出 Codex 登录。已经指向 Hub 的客户端需要重新选择模型并应用配置预览。',[],'停用共享',false))return;
  const focus=button===document.activeElement;codexSubscriptionView.working=true;paintCodexSubscription();
  if(focus)$('#codex-subscription-settings')?.focus({preventScroll:true});
  try{
    if(action==='refresh'){codexSubscriptionView.quota=null;codexSubscriptionView.quotaError='';await loadCodexSubscription();}
    else if(action==='quota'){
      codexSubscriptionView.quota=null;codexSubscriptionView.quotaError='';
      try{codexSubscriptionView.quota=(await api('POST','/api/codex-subscription/quota',{})).quota;}
      catch(e){codexSubscriptionView.quotaError=e.message;}
    }
    else{
      codexSubscriptionView.quota=null;codexSubscriptionView.quotaError='';
      codexSubscriptionView.data=await api('POST',`/api/codex-subscription/${action}`,{});
      codexSubscriptionView.error='';await reload();await loadObservation();
      toast(action==='connect'?'Codex 订阅已接入。请在 Agent 中选择模型并应用配置预览。':'已停用共享，Codex 登录保持不变。');
    }
  }catch(e){codexSubscriptionView.error=e.message;}
  finally{const restore=focus&&document.activeElement===$('#codex-subscription-settings');codexSubscriptionView.working=false;paintCodexSubscription();if(restore)$('#codex-subscription-settings')?.querySelector(`[data-codex-subscription="${action}"]:not(:disabled)`)?.focus({preventScroll:true});}
});
