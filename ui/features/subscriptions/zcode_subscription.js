"use strict";

const zcodeSubscriptionView={data:null,error:"",loading:false,working:false,painted:"",activity:null,activityError:"",quota:null,quotaError:""};
const settingsBeforeZcodeSubscription=renderSettings;
renderSettings=function(){
  settingsBeforeZcodeSubscription();
  $('#codex-subscription-settings')?.insertAdjacentHTML('afterend','<section class="card workbuddy-settings" id="zcode-subscription-settings" aria-live="polite" tabindex="-1"></section>');
  paintZcodeSubscription();
  if(!zcodeSubscriptionView.data&&!zcodeSubscriptionView.loading&&!zcodeSubscriptionView.error)loadZcodeSubscription();
};

function paintZcodeSubscription(){
  const el=$('#zcode-subscription-settings');if(!el)return;
  const d=zcodeSubscriptionView.data;
  const label=d?.connected?(d.needsRefresh?'账号已变更 · 需刷新':d.signedIn?'已接入':'已接入 · 登录不可用'):d?.signedIn?'已登录 · 待读取模型':'尚未就绪';
  const html=`<div class="card-heading"><div><h2>ZCode 订阅共享</h2><p>把本机 ZCode 当前的订阅模型用于其他已启用的 Agent；使用时保持 Hub 运行。</p></div>${tag(label,d?.connected&&d?.signedIn&&!d?.needsRefresh?'green':'')}</div>
    ${zcodeSubscriptionView.error?`<p class="notice warn" role="alert">${esc(zcodeSubscriptionView.error)}</p>`:''}
    <p>${esc(d?.message||'正在检查 ZCode 的登录状态…')}${d?.connected?` · ${d.models} 个已保存模型`:''}</p>
    <div class="actions">${d?.signedIn?`<button class="primary" data-zcode-subscription="connect" ${zcodeSubscriptionView.working?'disabled':''}>${d.connected?'刷新模型并恢复服务':'读取模型并接入'}</button><button data-zcode-subscription="quota" ${zcodeSubscriptionView.working?'disabled':''}>查询额度</button>`:''}${d?.connected?`<button data-zcode-subscription="disconnect" ${zcodeSubscriptionView.working?'disabled':''}>停用共享</button>`:''}<button data-zcode-subscription="refresh" ${zcodeSubscriptionView.working?'disabled':''}>刷新状态</button></div>
    ${subscriptionMonitor(zcodeSubscriptionView)}
    <p class="card-footnote">Hub 使用 ZCode 当前选中的个人、团队或 Start Plan，不创建密钥。Codex 使用 ZCode 时需在 Codex 配置中设置 web_search = "disabled"；接入后请在 Agent 中选择模型并确认预览。</p>`;
  if(html===zcodeSubscriptionView.painted&&el.innerHTML)return;
  zcodeSubscriptionView.painted=html;el.innerHTML=html;
}

async function loadZcodeSubscription(){
  if(zcodeSubscriptionView.loading)return;
  zcodeSubscriptionView.loading=true;
  try{zcodeSubscriptionView.data=await api('GET','/api/zcode-subscription');zcodeSubscriptionView.error='';await loadSubscriptionActivity(zcodeSubscriptionView,'zcode-subscription');}
  catch(e){zcodeSubscriptionView.error=e.message;}
  finally{zcodeSubscriptionView.loading=false;paintZcodeSubscription();}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-zcode-subscription]');if(!button||button.disabled)return;
  const action=button.dataset.zcodeSubscription;
  if(action==='disconnect'&&!await confirmDialog('停用 ZCode 订阅共享？','不会退出 ZCode 登录。已经指向 Hub 的客户端需要重新选择模型并应用配置预览。',[],'停用共享',false))return;
  const focus=button===document.activeElement;zcodeSubscriptionView.working=true;paintZcodeSubscription();
  if(focus)$('#zcode-subscription-settings')?.focus({preventScroll:true});
  try{
    if(action==='refresh'){zcodeSubscriptionView.quota=null;zcodeSubscriptionView.quotaError='';await loadZcodeSubscription();}
    else if(action==='quota'){
      zcodeSubscriptionView.quota=null;zcodeSubscriptionView.quotaError='';
      try{zcodeSubscriptionView.quota=(await api('POST','/api/zcode-subscription/quota',{})).quota;}
      catch(e){zcodeSubscriptionView.quotaError=e.message;}
    }
    else{
      zcodeSubscriptionView.quota=null;zcodeSubscriptionView.quotaError='';
      zcodeSubscriptionView.data=await api('POST',`/api/zcode-subscription/${action}`,{});
      zcodeSubscriptionView.error='';await reload();await loadObservation();
      toast(action==='connect'?'ZCode 订阅已接入。请在 Agent 中选择模型并应用配置预览。':'已停用共享，ZCode 登录保持不变。');
    }
  }catch(e){zcodeSubscriptionView.error=e.message;}
  finally{const restore=focus&&document.activeElement===$('#zcode-subscription-settings');zcodeSubscriptionView.working=false;paintZcodeSubscription();if(restore)$('#zcode-subscription-settings')?.querySelector(`[data-zcode-subscription="${action}"]:not(:disabled)`)?.focus({preventScroll:true});}
});
