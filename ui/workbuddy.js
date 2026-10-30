"use strict";

const workbuddyView={data:null,error:"",loading:false,timer:null,loginId:null,connecting:false,painted:""};
const settingsBeforeWorkbuddy=renderSettings;
renderSettings=function(){
  settingsBeforeWorkbuddy();
  $('.gateway-settings')?.insertAdjacentHTML('afterend','<section class="card workbuddy-settings" id="workbuddy-settings" aria-live="polite"></section>');
  paintWorkbuddy();
  if(!workbuddyView.data&&!workbuddyView.loading&&!workbuddyView.error)loadWorkbuddy();
  else if(workbuddyView.data?.login?.status==='pending'&&!workbuddyView.loading&&!workbuddyView.timer)workbuddyView.timer=setTimeout(loadWorkbuddy,1500);
};
const providersBeforeWorkbuddy=renderProviders;
renderProviders=function(){
  providersBeforeWorkbuddy();
  $('.filter-bar')?.insertAdjacentHTML('beforebegin','<div class="notice"><div><strong>WorkBuddy AI 海外版订阅反代</strong><p>在设置中登录，即可把账号模型用于已启用的 Agent。</p><a href="#settings">前往登录与模型刷新</a></div></div>');
};
// 登录轮询只在设置页存活：切页即停（先例：workbench.js 的 stopBulkWatch）。
// 回到设置页且登录仍在 pending 时，由上面的 renderSettings 包装恢复轮询。
function stopWorkbuddyPoll(){clearTimeout(workbuddyView.timer);workbuddyView.timer=null;}
const pageBeforeWorkbuddy=renderPage;
renderPage=function(){
  if(workbuddyView.timer&&state.page!=='settings')stopWorkbuddyPoll();
  pageBeforeWorkbuddy();
};

function paintWorkbuddy(){
  const el=$('#workbuddy-settings');if(!el)return;
  const d=workbuddyView.data,pending=d?.login?.status==='pending';
  let link='';
  if(pending){try{const u=new URL(d.login.url);if(u.protocol==='https:')link=u.href;}catch{}}
  const html=`<div class="card-heading"><div><h2>WorkBuddy AI（海外版）</h2><p>将 WorkBuddy 海外版订阅作为供应商，供已启用的 Agent 选择；使用时保持 Hub 运行。</p></div>${tag(d?.connected?'已接入':d?.signedIn?'已登录 · 待读取模型':'尚未登录',d?.connected?'green':'')}</div>
    ${workbuddyView.error?`<p class="notice warn" role="alert">${esc(workbuddyView.error)}</p>`:''}
    ${d?.signedIn?`<p>账号：${esc(d.account)}${d.enterprise?' · 企业账号':''} · ${d.models} 个已保存模型${d.expired?' · Token 已到期，将在使用时尝试刷新':''}</p>`:'<p>使用 WorkBuddy 官方浏览器登录页面，完成后自动接入。无需填写 API Key。</p>'}
    ${pending?`<p class="notice">等待浏览器完成登录；页面在 ${new Date(d.login.expiresAt*1000).toLocaleTimeString('zh-CN',{hour12:false})} 前有效。<a href="${esc(link)}" target="_blank" rel="noopener noreferrer" class="workbuddy-login-link">打开 WorkBuddy 登录页面</a></p>`:''}
    ${d?.login?.status==='failed'?`<p class="notice warn">${esc(d.login.error)}</p>`:''}
    <div class="actions"><button data-workbuddy="login" ${pending||workbuddyView.loading?'disabled':''}>${d?.signedIn?'重新登录 / 切换账号':'登录 WorkBuddy 海外版'}</button>${pending?'<button data-workbuddy="cancel">取消本次登录</button>':''}${d?.signedIn?`<button class="primary" data-workbuddy="connect" ${workbuddyView.connecting?'disabled':''}>${workbuddyView.connecting?'正在读取模型…':d.connected?'刷新模型并恢复服务':'读取模型并接入'}</button><button data-workbuddy="logout">退出此账号</button>`:''}<button data-workbuddy="refresh">刷新状态</button></div>
    <p class="card-footnote">接入后到 Agent 行内选择 WorkBuddy AI 和模型，再应用配置预览。OAuth 凭据仅保存在本机私有数据目录，临近到期自动刷新。退出账号会停用此供应商；客户端切换仍通过预览生效。</p>`;
  // 登录 pending 期的 1.5s 轮询与设置页的周期性 renderSettings 都会走到这里：
  // 数据没变且画布还在（innerHTML 非空）就不重绘，避免整段换新抢走按钮焦点/悬停态
  //（先例：workbench loadObservation 的 unchanged 守卫）。整页重渲染会插入全新的
  // 空 section，靠 innerHTML 判空识别并强制重画。
  if(html===workbuddyView.painted&&el.innerHTML)return;
  workbuddyView.painted=html;
  el.innerHTML=html;
}

async function loadWorkbuddy(){
  if(workbuddyView.loading)return;
  workbuddyView.loading=true;
  try{
    workbuddyView.data=await api('GET','/api/workbuddy');workbuddyView.error='';
    const login=workbuddyView.data.login;
    if(login?.status==='succeeded'&&workbuddyView.loginId===login.id){
      workbuddyView.loginId=null;
      await connectWorkbuddy();
    }
  }catch(e){workbuddyView.error=e.message;}
  finally{
    workbuddyView.loading=false;paintWorkbuddy();stopWorkbuddyPoll();
    if(workbuddyView.data?.login?.status==='pending'&&state.page==='settings')workbuddyView.timer=setTimeout(loadWorkbuddy,1500);
  }
}

async function connectWorkbuddy(){
  if(workbuddyView.connecting)return;
  workbuddyView.connecting=true;paintWorkbuddy();
  try{
    const result=await api('POST','/api/workbuddy/connect',{});
    workbuddyView.data=result;workbuddyView.error='';
    await reload();await loadObservation();
    toast('WorkBuddy AI 已接入。请在 Agent 中选择模型并应用配置预览。');
  }finally{workbuddyView.connecting=false;paintWorkbuddy();}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-workbuddy]');if(!button||button.disabled)return;
  button.disabled=true;
  try{
    const action=button.dataset.workbuddy;
    if(action==='login'){
      const login=await api('POST','/api/workbuddy/login',{});
      workbuddyView.loginId=login.id;
      workbuddyView.data={...(workbuddyView.data||{}),login};workbuddyView.error='';
      paintWorkbuddy();await loadWorkbuddy();
    }else if(action==='connect')await connectWorkbuddy();
    else if(action==='cancel'){
      await api('DELETE','/api/workbuddy/login');workbuddyView.loginId=null;await loadWorkbuddy();
    }else if(action==='logout'){
      workbuddyView.data=await api('POST','/api/workbuddy/logout',{});workbuddyView.loginId=null;
      stopWorkbuddyPoll();await reload();paintWorkbuddy();toast('已移除本机 WorkBuddy 登录凭据，并停用此供应商。');
    }else await loadWorkbuddy();
  }catch(e){workbuddyView.error=e.message;paintWorkbuddy();}
  finally{button.disabled=false;}
});
