/* Dependency-free UI logic checks. Browser visual/layout checks remain in browser.cjs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map();
const listeners = new Map();
const node = key => {
  if (!nodes.has(key)) nodes.set(key, { innerHTML: '', textContent: '', hidden: false, disabled: false,
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, scrollIntoView() {}, focus() {}, append() {}, remove() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } } });
  return nodes.get(key);
};
const document = { querySelector: node, querySelectorAll: () => [], body: node('body'),
  createElement: () => node('created'), addEventListener: (type, callback) => listeners.set(type, callback) };
const context = vm.createContext({ document, window: { addEventListener() {} }, location: { hash: '#sync' },
  AbortController, setTimeout: () => 0, clearTimeout() {}, console });
let source = fs.readFileSync(path.resolve(__dirname, '../ui/app.js'), 'utf8');
source = source.slice(0, source.lastIndexOf('state.page = PAGES[location.hash.slice(1)]'));
vm.runInContext(source, context);
const run = text => vm.runInContext(text, context);
run(`state.providers = {gateway: {name: 'Gateway', enabled: true, models: [{id:'gpt-test'}]}};
state.status = {targets:{codex:{enabled:true,supported:true,mode:'official'},zcode:{enabled:true,supported:true,mode:'official'},pi:{enabled:true,supported:true}},anthropicCapable:['gateway'],agentCapable:{pi:['gateway'],dsh:['gateway'],zcode:['gateway'],claude:['gateway'],codex:['gateway']},connections:{gateway:{pi:{ready:true,overridden:false,modelCount:1},dsh:{ready:true,overridden:false,modelCount:1},zcode:{ready:true,overridden:false,modelCount:1},claude:{ready:true,overridden:false,modelCount:1},codex:{ready:true,overridden:false,modelCount:1}}},lastResults:[]};
state.targetDraft={codex:true,zcode:true,pi:true};
state.modelSync={modelSync:{default:{provider:'gateway',model:'gpt-test'}},resolved:{codex:{provider:'gateway',model:'gpt-test'}},options:{},optionsByTarget:{}};
ensureModelSyncDraft();`);
let passed = 0;
const test = async (name, task) => { await task(); passed++; console.log(`PASS ${name}`); };
(async () => {
  await test('new supplier targets are explicit and focus on core agents', () => {
    assert.equal(run('DEFAULT_PROVIDER_TARGETS.join(",")'), 'claude,codex,pi,dsh');
    const selected = { has: () => false, getAll: () => ['claude','codex','pi','dsh'] };
    context.selected = selected;
    assert.equal(run('providerTargetsFromForm(selected).join(",")'), 'claude,codex,pi,dsh');
    const all = { has: () => true, getAll: () => [] };
    context.all = all;
    assert.equal(run('providerTargetsFromForm(all).length'), 0);
    const empty = { has: () => false, getAll: () => [] };
    context.empty = empty;
    assert.throws(() => run('providerTargetsFromForm(empty)'), /至少选择一个同步平台/);
  });
  await test('official Codex is writable and restoration is explained', () => {
    const html = run(`clientRow('codex','Codex')`);
    assert.match(html, /恢复.*官方 model/);
    assert.doesNotMatch(html, /只登记|ms-client readonly|data-action="ms-override"/);
  });
  await test('daily selectors contain exactly the four supported agents', () => {
    assert.equal(run(`AGENT_OVERRIDES.map(x=>x[0]).join(',')`),'claude,codex,pi,dsh');
    assert.equal(run(`IMPORT_CLIENTS.map(x=>x[0]).includes('zcode')`),false);
  });
  await test('ZCode import list renders candidates and empty state', () => {
    const html = run(`importListHtml([{id:'atria',name:'Atria',baseUrl:'https://api.atria.example/v1',api:'openai-completions',modelCount:2,models:[]}])`);
    assert.match(html, /import-check/);
    assert.match(html, /Atria/);
    assert.match(html, /2 个模型/);
    assert.match(run(`importListHtml([])`), /没有 Hub 尚未管理/);
  });
  await test('third-party opt-in is explicit and override is editable', () => {
    run(`state.clientMode.codex='third-party';state.clientOptIn.codex=false`);
    const html = run(`clientRow('codex','Codex')`);
    assert.match(html, /id="codex-optin" type="checkbox"\s*\//);
    assert.match(html, /data-action="ms-override"/);
  });
  await test('Codex uses its own effort vocabulary and supports incomplete draft editing', () => {
    run(`setModelEntry('clients','codex','',{provider:'gateway',model:''})`);
    const html = run(`clientRow('codex','Codex')`);
    assert.match(html, /id="ms-provider-clients-codex"/);
    assert.match(html, /value="none"/);
    assert.match(html, /供应商默认（移除覆盖）/);
    assert.doesNotMatch(html, /value="off"/);
    // Codex 线路放开 max / ultra
    assert.match(html, /value="max"/);
    assert.match(html, /value="ultra"/);
  });
  await test('preview escapes paths and has distinct confirmation', () => {
    run(`state.preview={planId:'plan',targets:['codex'],files:[{path:'<script>bad</script>',action:'update'}],warnings:['<img>']};renderPreview()`);
    assert.match(node('#sync-preview').innerHTML, /&lt;script&gt;/);
    assert.doesNotMatch(node('#sync-preview').innerHTML, /<script>/);
    assert.match(node('#sync-preview').innerHTML, /尚未写入客户端|确认应用预览/);
  });
  await test('mode changes invalidate preview and clear consent', () => {
    listeners.get('change')({target:{id:'codex-mode',value:'official',dataset:{},classList:{contains:()=>false}}});
    assert.equal(run('state.preview'), null);
    assert.equal(run('state.clientOptIn.codex'), false);
    assert.equal(node('#sync-preview').hidden, true);

  });
  await test('platform selection changes invalidate preview', () => {
    run(`state.preview={planId:'plan'}`);
    listeners.get('change')({target:{id:'',checked:false,dataset:{target:'pi'},classList:{contains:x=>x==='target-check'}}});
    assert.equal(run('state.preview'), null);
    assert.equal(run('state.targetDraft.pi'), false);
  });
  await test('unsaved model-only overrides protect refresh and navigation', () => {
    run(`state.loaded=true; state.targetDraft.pi=true; state.clientMode.codex='official'; state.clientMode.zcode='official';`);
    assert.equal(run('targetDirty()'), true);
    run(`state.modelSyncDraft=null;state.modelSyncDraftKey='';ensureModelSyncDraft()`);
    assert.equal(run('targetDirty()'), false);
  });
  run(`var requests=[];var messages=[];busy=async(label,task)=>task();reload=async()=>{};saveTargets=async()=>{};
    toast=(message)=>messages.push(message);
    api=async(method,path,body)=>{requests.push({method,path,body});return {planId:'test-plan',targets:['codex'],files:[],warnings:[]}};`);
  await test('third-party preview blocked until explicit consent', async () => {
    run(`state.clientMode.codex='third-party';state.clientOptIn.codex=false`);
    await run(`runSync()`);
    assert.equal(run('requests.length'), 0);
    assert.match(run('messages.at(-1)'), /先勾选/);
  });
  await test('generating preview never invokes apply', async () => {
    run(`state.clientMode.zcode='official';state.clientOptIn.codex=true`);
    await run(`runSync()`);
    assert.equal(run('requests.length'), 1);
    assert.equal(run('requests[0].path'), '/api/preview');
    assert.equal(run('state.preview.planId'), 'test-plan');
    assert.equal(node('#sync-preview').hidden, false);
  });
  await test('cancel removes apply capability', () => {
    run('invalidatePreview()');
    assert.equal(run('state.preview'), null);
    assert.equal(node('#sync-preview').hidden, true);
  });
  await test('apply consumes plan and cannot be repeated without new preview', async () => {
    run(`state.preview={planId:'test-plan'}; requests=[];
      api=async(method,path,body)=>{requests.push({path,body});return {ok:true,details:[{target:'codex',ok:true}]}};`);
    await run('applyPreview()');
    await run('applyPreview()');
    assert.equal(run('requests.length'), 1);
    assert.equal(run('requests[0].path'), '/api/apply');
    assert.equal(run('state.preview'), null);
  });
  await test('stale plan error remains visible and cannot be retried directly', async () => {
    run(`state.preview={planId:'stale'};api=async()=>{throw new Error('预览已过期，请重新预览')}`);
    await assert.rejects(run('applyPreview()'), /预览已过期/);
    assert.equal(run('state.preview'), null);
    assert.match(node('#sync-preview').innerHTML, /预览已过期/);
    assert.doesNotMatch(node('#sync-preview').innerHTML, /data-action="apply-preview"/);
  });
  await test('supplier choices only list providers whose connection is ready', () => {
    run(`state.providers = {
      gateway: {name:'Gateway',enabled:true,baseUrl:'https://example.com/v1',models:[{id:'gpt-test'}],
        agents:{claude:{baseUrl:'https://example.com/anthropic',api:'anthropic-messages',models:[{id:'claude-test'}]},
                codex:{baseUrl:'https://example.com/v1',api:'openai-responses',models:[{id:'gpt-test'}]}}},
      claudeOnly: {name:'Claude Plan',enabled:true,baseUrl:'',anthropicBaseUrl:'https://example.com/anthropic',targets:['claude'],models:[{id:'claude-test'}]},
      chatOnly: {name:'Chat Plan',enabled:true,baseUrl:'https://example.com/v1',api:'openai-completions',models:[{id:'chat-test'}]}
    };
    state.status.agentCapable = {pi:['gateway','chatOnly'],dsh:['gateway','chatOnly'],claude:['gateway','claudeOnly'],codex:['gateway']};`);
    const claude = run(`providerSelect('clients','claude','','')`);
    assert.match(claude, /value="claudeOnly"/);
    assert.doesNotMatch(claude, /value="chatOnly"/);
    const codex = run(`providerSelect('clients','codex','','')`);
    assert.match(codex, /value="gateway"/);
    assert.doesNotMatch(codex, /value="claudeOnly"|value="chatOnly"/);
  });
  await test('switching global supplier clears a model absent from the new supplier', () => {
    run(`state.modelSync={modelSync:{default:{provider:'gateway',model:'gpt-test'}},resolved:{},options:{},optionsByTarget:{}};
      state.modelSyncDraft=null;state.modelSyncDraftKey='';ensureModelSyncDraft()`);
    listeners.get('change')({target:{id:'ms-default-provider',value:'chatOnly',dataset:{msProvider:'',scope:'default',target:'default'},classList:{contains:()=>false}}});
    assert.equal(run('state.modelSyncDraft.default.provider'), 'chatOnly');
    assert.equal(run('state.modelSyncDraft.default.model'), undefined);
    assert.equal(run(`modelChoices('chatOnly').includes('gpt-test')`), false);
  });
  // ---- 切换方案（switchPlans）：一次选择、明确范围、预览不写入 ----
  const seedPlans = () => run(`
    state.plans = {
      store: {version:1, default:'work', plans:[
        {id:'work', name:'工作模式', description:'', targets:['claude','codex'],
         base:{provider:'gateway', model:'gpt-test'}, agents:{},
         fallback:{routes:[],require:{},allowCost:{}}, stale:false, refs:[]},
        {id:'stale', name:'旧方案', description:'', targets:['claude'],
         base:{provider:'gone', model:'ghost'},
         agents:{}, fallback:{routes:[],require:{},allowCost:{}}, stale:true,
         refs:[{where:'base',target:null,provider:'gone',model:'ghost',state:'provider-missing',valid:false}]}
      ]},
      targets: {
        claude:{connections:{gateway:{ready:true,enabled:true,api:'anthropic-messages',models:['claude-test']}, claudeOnly:{ready:true,enabled:true,api:'anthropic-messages',models:['claude-test']}}},
        codex:{connections:{gateway:{ready:true,enabled:true,api:'openai-responses',models:['gpt-test']}}},
        pi:{connections:{gateway:{ready:true,enabled:true,api:'openai-completions',models:['gpt-test','pi-only-test']}, chatOnly:{ready:true,enabled:true,api:'openai-completions',models:['chat-test']}}},
        dsh:{connections:{gateway:{ready:true,enabled:true,api:'openai-completions',models:['gpt-test']}}},
        zcode:{connections:{gateway:{ready:true,enabled:true,api:'openai-chat-completions',models:['gpt-test']}}}
      }
    };
    state.plansError=''; state.planLoaded=null; state.planLoadedPayload=null;
    state.planDraft={base:{provider:'',model:'',reasoningEffort:''},targets:[],agents:{}};
    state.planTargets=new Set(); state.planOverrides={}; state.planName=''; state.planDescription='';
    state.planAnalysis=null; state.planPreviewResult=null; state.planBusy=false;`);
  await test('plan card offers one model choice plus an explicit agent scope', () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test',reasoningEffort:''}`);
    // gateway 的 Claude 连接上只有 claude-test，所以 gpt-test 不覆盖 Claude Code
    assert.equal(run(`planTargetsForModel('gateway','gpt-test').join(',')`), 'codex,pi,dsh');
    assert.equal(run(`planTargetsForModel('gateway','claude-test').join(',')`), 'claude');
    // 只有 pi 的连接上有这个模型：可用范围必须按连接算，不能按供应商算
    assert.equal(run(`planTargetsForModel('gateway','pi-only-test').join(',')`), 'pi');
    const html = run('planCard()');
    assert.match(html, /id="plan-card"/);
    assert.match(html, /id="plan-provider"/);
    assert.match(html, /id="plan-model"/);
    assert.match(html, /id="plan-effort"/);
    assert.match(html, /data-action="plan-pick-usable"/);
    assert.match(html, /data-action="plan-preview"/);
    assert.match(html, /data-action="plan-apply"/);
    assert.match(html, /保留客户端当前值/);
    assert.match(html, /跟随本次选择/);
  });
  await test('plan card marks per-agent availability and never guesses a model', () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test',reasoningEffort:''};
      state.planTargets=new Set(['claude','pi']);`);
    const html = run('planCard()');
    assert.match(html, /data-plan-target="claude"/);
    assert.match(html, /data-plan-target="dsh"/);
    assert.match(html, /连接就绪、模型在该连接上/);
    // 未勾选的 Agent 显示"未参与本次切换"，不会被顺手改动
    assert.match(html, /未参与本次切换/);
    // claude 的连接上有 gpt-test 吗？没有 -> 必须标出来而不是静默换模型
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test'};
      state.planTargets=new Set(['codex','dsh'])`);
    const ok = run('planCard()');
    assert.match(ok, /dsh（含桌面版）/);
    run(`state.planDraft.base={provider:'gateway',model:'chat-test'}`);
    const mismatch = run('planCard()');
    assert.match(mismatch, /claude/);
  });
  await test('plan card surfaces stale references instead of substituting', () => {
    seedPlans();
    run(`loadPlanIntoDraft(state.plans.store.plans.find(p=>p.id==='stale'))`);
    const html = run('planCard()');
    assert.match(html, /引用已失效/);
    assert.match(html, /不会自动换成别的模型/);
    assert.equal(run(`state.planDraft.base.provider`), 'gone');
    assert.equal(run(`state.planDraft.base.model`), 'ghost');
  });
  await test('plan policy switch selects keep / follow / fixed semantics', () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test'};
      state.planTargets=new Set(['claude','pi','dsh']);`);
    const pick = (name, value) => listeners.get('change')({ target: { id: '', value, dataset: { planPolicy: name }, classList: { contains: () => false } } });
    pick('dsh', 'keep');
    assert.equal(run(`state.planOverrides.dsh.policy`), 'keep');
    pick('pi', 'fixed');
    assert.equal(run(`state.planOverrides.pi.policy`), 'fixed');
    // 单独指定时预填该 Agent 连接上就绪的供应商与它的第一个模型
    assert.equal(run(`state.planOverrides.pi.provider`), 'gateway');
    assert.ok(run(`['gpt-test','chat-test'].includes(state.planOverrides.pi.model)`));
    pick('pi', 'follow');
    assert.equal(run(`state.planOverrides.pi`), undefined);
    const payload = run('planPayload()');
    assert.equal(payload.targets.join(','), 'claude,pi,dsh');
    assert.equal(payload.base.model, 'gpt-test');
    assert.equal(payload.agents.dsh.policy, 'keep');
  });
  await test('plan payload never sends an empty provider or model', () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'',reasoningEffort:''};
      state.planTargets=new Set(['pi'])`);
    const payload = run('planPayload()');
    assert.equal(payload.base, undefined);
    assert.equal(payload.agents, undefined);
  });
  await test('plan preview reports what would be written without applying', async () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test'}; state.planTargets=new Set(['codex']);
      requests=[]; messages=[];
      api=async(method,path,body)=>{requests.push({method,path,body});
        return {ok:true, writable:true, planId:'switch-plan', targets:['codex'],
          files:[{path:'C:/x/.codex/config.toml',action:'update'}], warnings:[],
          analysis:{details:[{target:'codex',label:'Codex',status:'ok',provider:'gateway',model:'gpt-test',
            effort:{support:'applied',requested:'ultra'},tiers:[],needsClientAction:'写入的是 config.toml；正在运行的 Codex 会话与项目级配置可能覆盖它，需要重启确认。'}],
            errors:[],warnings:[],issues:[],summary:{ready:['codex'],blocked:[],skipped:[],errorCount:0,warningCount:0},
            disclaimer:'「可写入」只表示 Hub 会成功写出原生配置，不代表正在运行的会话已经换模。'}};};`);
    await run('planPreview()');
    assert.equal(run('requests.length'), 1);
    assert.equal(run('requests[0].path'), '/api/switch-plans/preview');
    assert.ok(run(`JSON.stringify(requests).indexOf('/api/switch-plans/apply') === -1`));
    const html = run('planCard()');
    assert.match(html, /确认应用并写入客户端/);
    assert.match(html, /尚未写入客户端|不写入/);
    assert.match(html, /重启/);
    assert.match(html, /不代表正在运行的会话已经换模/);
  });
  await test('failed plan analysis cannot be applied', async () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'chat-test'}; state.planTargets=new Set(['codex']);
      requests=[];
      api=async(method,path,body)=>{requests.push({path});
        return {ok:false, writable:false, analysis:{details:[{target:'codex',label:'Codex',status:'error',
            provider:'gateway',model:'chat-test',reason:'gateway 的 Codex 连接模型库里没有 chat-test',
            effort:{support:'applied',requested:null},tiers:[],needsClientAction:''}],
          errors:[{code:'model-not-in-connection',severity:'error',target:'codex',message:'gateway 的 Codex 连接模型库里没有 chat-test'}],
          warnings:[],issues:[],summary:{ready:[],blocked:['codex'],skipped:[],errorCount:1,warningCount:0},disclaimer:'x'}};};`);
    await assert.rejects(run('planPreview()'), /不兼容|没有写入/);
    const html = run('planCard()');
    assert.match(html, /不兼容/);
    assert.doesNotMatch(html, /确认应用并写入客户端/);
  });
  await test('saving a plan stores the payload and update keeps the same id', async () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test'}; state.planTargets=new Set(['claude','codex']);
      state.planName='工作模式'; state.planDescription=''; requests=[];
      api=async(method,path,body)=>{requests.push({method,path,body});
        return {ok:true, store:{version:1, default:'work', plans:[
          {id:'work',name:'工作模式',targets:['claude','codex'],base:{provider:'gateway',model:'gpt-test'},
           agents:{},fallback:{routes:[],require:{},allowCost:{}},stale:false,refs:[]}]}};};`);
    await run('savePlan()');
    assert.equal(run('requests[0].method'), 'PUT');
    assert.equal(run('requests[0].path'), '/api/switch-plans');
    assert.equal(run(`requests[0].body.switchPlans.plans.work.base.model`), 'gpt-test');
    assert.equal(run('state.planLoaded.id'), 'work');
    // 保存的 payload 不带展示字段（stale / refs / updatedAt）
    assert.equal(run(`'stale' in requests[0].body.switchPlans.plans.work`), false);
    assert.equal(run(`'refs' in requests[0].body.switchPlans.plans.work`), false);
  });
  await test('plan changes invalidate any pending preview', () => {
    seedPlans();
    run(`state.planDraft.base={provider:'gateway',model:'gpt-test'}; state.planTargets=new Set(['codex']);
      state.planPreviewResult={ok:true,planId:'stale-plan',targets:['codex'],files:[],warnings:[],
        analysis:{details:[],errors:[],warnings:[],issues:[],summary:{ready:[],blocked:[],skipped:[],errorCount:0,warningCount:0},disclaimer:''}};`);
    listeners.get('change')({ target: { id: '', value: 'ultra', dataset: { planEffort: 'codex' }, classList: { contains: () => false } } });
    assert.equal(run('state.planPreviewResult'), null);
    const html = run('planCard()');
    assert.doesNotMatch(html, /确认应用并写入客户端/);
  });
  console.log(`PASS ${passed} UI contract checks (DOM stub; no browser layout assertion)`);
})().catch(error => { console.error(error); process.exitCode=1; });
