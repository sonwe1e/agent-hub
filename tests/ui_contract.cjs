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
    assert.equal(run('DEFAULT_PROVIDER_TARGETS.join(",")'), 'pi,dsh,zcode,claude,codex');
    const selected = { has: () => false, getAll: () => ['pi', 'dsh', 'zcode', 'claude', 'codex'] };
    context.selected = selected;
    assert.equal(run('providerTargetsFromForm(selected).join(",")'), 'pi,dsh,zcode,claude,codex');
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
  await test('ZCode has a model sync row and connection override', () => {
    const html = run(`clientRow('zcode','ZCode')`);
    assert.match(html, /data-client="zcode"/);
    assert.match(html, /ZCode/);
    assert.match(run(`AGENT_OVERRIDES.map(x=>x[0]).join(',')`), /zcode/);
  });
  await test('ZCode official mode keeps catalog but explains no default takeover', () => {
    const html = run(`clientRow('zcode','ZCode')`);
    assert.match(html, /id="zcode-mode"/);
    assert.match(html, /官方模式/);
    assert.match(html, /个人供应商目录/);
    assert.match(html, /credentials\.json.*永不触碰/);
    assert.doesNotMatch(html, /id="zcode-optin"/);
  });
  await test('ZCode third-party mode requires its own opt-in', () => {
    run(`state.clientMode.zcode='third-party'`);
    const html = run(`clientRow('zcode','ZCode')`);
    assert.match(html, /id="zcode-optin" type="checkbox"\s*\//);
    assert.match(html, /data-action="ms-override"/);
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
    listeners.get('change')({target:{id:'zcode-mode',value:'third-party',dataset:{},classList:{contains:()=>false}}});
    assert.equal(run('state.clientMode.zcode'), 'third-party');
    assert.equal(run('state.clientOptIn.zcode'), false);
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
  await test('ZCode third-party preview blocked until its own consent', async () => {
    run(`state.clientMode.codex='official';state.clientMode.zcode='third-party';state.clientOptIn.zcode=false`);
    await run(`runSync()`);
    assert.equal(run('requests.length'), 0);
    assert.match(run('messages.at(-1)'), /先勾选 ZCode/);
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
  console.log(`PASS ${passed} UI contract checks (DOM stub; no browser layout assertion)`);
})().catch(error => { console.error(error); process.exitCode=1; });
