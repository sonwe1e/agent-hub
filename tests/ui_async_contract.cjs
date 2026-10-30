/* Synthetic DOM checks with the real busy guard; not browser/layout verification. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const nodes = new Map(), listeners = new Map(), historyWrites = [];
const node = key => {
  if (!nodes.has(key)) nodes.set(key, {
    innerHTML: '', textContent: '', hidden: false, disabled: false,
    addEventListener() {}, setAttribute() {}, removeAttribute() {}, scrollIntoView() {}, focus() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  });
  return nodes.get(key);
};
const document = { querySelector: node, querySelectorAll: () => [], body: node('body'),
  createElement: () => node('created'), addEventListener: (name, fn) => listeners.set(name, fn) };
const context = vm.createContext({ document, window: { addEventListener() {} },
  location: { hash: '#models' }, history: { replaceState: (_state, _title, url) => historyWrites.push(url) },
  AbortController, setTimeout: () => 0, clearTimeout() {}, console });
let source = fs.readFileSync(path.resolve(__dirname, '../ui/app.js'), 'utf8');
source = source.slice(0, source.lastIndexOf('state.page = PAGES[location.hash.slice(1)]'));
vm.runInContext(source, context);
const run = code => vm.runInContext(code, context);
const click = action => listeners.get('click')({ target: { closest: () => ({ disabled: false, dataset: { action } }) } });
const ticks = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
// 载体页用 models（旧独立 sync 页已并入 #models、死分支已删）；断言本质是
// 「busy 期间导航被拦下并回写当前 hash」，与具体是哪两页无关。
run(`state.page='models';state.loaded=true;state.targetDraft={codex:true};
  state.status={lastResults:[]};var requests=[];var pendingResolve;
  renderChrome=()=>{};renderPage=()=>{};reload=async()=>{};saveTargets=async()=>{};toast=()=>{};
  api=async(method,path,body)=>{requests.push({method,path,body});return new Promise(resolve=>pendingResolve=resolve)};`);
(async () => {
  const first = run('runSync()');
  await ticks();
  assert.equal(run('state.busy'), true);
  await run('runSync()');
  assert.equal(run('requests.length'), 1, 'Repeated preview must not start a second request');
  await run(`navigate('agents')`);
  assert.equal(run('state.page'), 'models');
  assert.equal(historyWrites.at(-1), '#models', 'Busy navigation must restore current history hash');
  run(`pendingResolve({planId:'synthetic-preview',targets:['codex'],files:[],warnings:['external edit warning']})`);
  await first;
  assert.equal(run('state.busy'), false);
  assert.equal(run('state.preview.planId'), 'synthetic-preview');
  assert.match(node('#sync-preview').innerHTML, /external edit warning/);
  await click('cancel-preview');
  assert.equal(run('state.preview'), null);
  assert.equal(node('#sync-preview').hidden, true);
  await run('applyPreview()');
  assert.equal(run('requests.length'), 1, 'Cancelled preview must not be applied');
  console.log('PASS asynchronous preview repeat, navigation, warning, and cancellation');

  run(`state.preview={planId:'synthetic-apply'};requests=[]`);
  const applying = run('applyPreview()');
  await ticks();
  await run('applyPreview()');
  assert.equal(run('requests.length'), 1, 'Repeated apply must not start a second request');
  assert.equal(run('requests[0].path'), '/api/apply');
  run(`pendingResolve({ok:true,details:[{target:'codex',ok:true}]})`);
  await applying;
  assert.equal(run('state.preview'), null);
  assert.equal(run('state.busy'), false);
  await run('applyPreview()');
  assert.equal(run('requests.length'), 1, 'Completed preview must not be reused');
  console.log('PASS asynchronous apply deduplication and plan consumption');
})().catch(error => { console.error(error); process.exitCode = 1; });
