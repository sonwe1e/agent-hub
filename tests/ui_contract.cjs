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
state.status = {targets:{codex:{enabled:true,supported:true,mode:'official'},pi:{enabled:true,supported:true}},anthropicCapable:[],lastResults:[]};
state.targetDraft={codex:true,pi:true};
state.modelSync={modelSync:{default:{provider:'gateway',model:'gpt-test'}},resolved:{codex:{provider:'gateway',model:'gpt-test'}},options:{}};
ensureModelSyncDraft();`);
let passed = 0;
const test = async (name, task) => { await task(); passed++; console.log(`PASS ${name}`); };
(async () => {
  await test('official Codex is writable and restoration is explained', () => {
    const html = run(`clientRow('codex','Codex')`);
    assert.match(html, /恢复.*官方 model/);
    assert.doesNotMatch(html, /只登记|ms-client readonly|data-action="ms-override"/);
  });
  await test('third-party opt-in is explicit and override is editable', () => {
    run(`state.codexMode='third-party';state.codexOptIn=false`);
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
    assert.doesNotMatch(html, /value="off"|value="max"/);
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
    assert.equal(run('state.codexOptIn'), false);
    assert.equal(node('#sync-preview').hidden, true);
  });
  await test('platform selection changes invalidate preview', () => {
    run(`state.preview={planId:'plan'}`);
    listeners.get('change')({target:{id:'',checked:false,dataset:{target:'pi'},classList:{contains:x=>x==='target-check'}}});
    assert.equal(run('state.preview'), null);
    assert.equal(run('state.targetDraft.pi'), false);
  });
  await test('unsaved model-only overrides protect refresh and navigation', () => {
    run(`state.loaded=true; state.targetDraft.pi=true; state.codexMode='official';`);
    assert.equal(run('targetDirty()'), true);
    run(`state.modelSyncDraft=null;state.modelSyncDraftKey='';ensureModelSyncDraft()`);
    assert.equal(run('targetDirty()'), false);
  });
  run(`var requests=[];var messages=[];busy=async(label,task)=>task();reload=async()=>{};saveTargets=async()=>{};
    toast=(message)=>messages.push(message);
    api=async(method,path,body)=>{requests.push({method,path,body});return {planId:'test-plan',targets:['codex'],files:[],warnings:[]}};`);
  await test('third-party preview blocked until explicit consent', async () => {
    run(`state.codexMode='third-party';state.codexOptIn=false`);
    await run(`runSync()`);
    assert.equal(run('requests.length'), 0);
    assert.match(run('messages.at(-1)'), /先勾选/);
  });
  await test('generating preview never invokes apply', async () => {
    run(`state.codexOptIn=true`);
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
  console.log(`PASS ${passed} UI contract checks (DOM stub; no browser layout assertion)`);
})().catch(error => { console.error(error); process.exitCode=1; });
