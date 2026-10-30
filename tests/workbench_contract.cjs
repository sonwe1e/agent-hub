/* Dependency-free contract checks for ui/features/workbench/workbench.js.
   Loads app.js (truncated before init) and workbench.js into one VM,
   mirroring index.html script order. DOM stub only; no browser, no server. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const nodes = new Map(), listeners = new Map(), qaLists = new Map();
const timeouts = [], clearedTimeouts = [], intervals = [], historyWrites = [];

const makeNode = key => ({
  key, innerHTML: '', textContent: '', hidden: false, disabled: false, open: false, value: '',
  dataset: {}, _clicks: 0, focused: false, style: {},
  addEventListener() {}, setAttribute() {}, removeAttribute() {}, scrollIntoView() {},
  focus() { this.focused = true; }, setSelectionRange(start, end) { this._sel = [start, end]; },
  click() { this._clicks++; }, append() {}, remove() {}, insertAdjacentHTML() {},
  querySelector(sel) { const k = this.key + ' ' + sel; if (!nodes.has(k)) nodes.set(k, makeNode(k)); return nodes.get(k); },
  querySelectorAll() { return []; },
  showModal() { this.open = true; }, close() { this.open = false; },
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
});
const node = key => { if (!nodes.has(key)) nodes.set(key, makeNode(key)); return nodes.get(key); };
const document = {
  querySelector: node, querySelectorAll: sel => { const v = qaLists.get(sel); return typeof v === 'function' ? v() : (v || []); },
  body: node('body'), createElement: () => node('created'), activeElement: null,
  addEventListener: (type, cb) => { const arr = listeners.get(type) || []; arr.push(cb); listeners.set(type, arr); },
};
const windowObj = { addEventListener() {}, scrollY: 0, _scrolled: null, scrollTo(x, y) { this._scrolled = [x, y]; } };
const context = vm.createContext({
  document, window: windowObj, location: { hash: '#agents' },
  history: { replaceState: (s, t, url) => historyWrites.push(url) },
  AbortController, URL, URLSearchParams, console,
  FormData: class { entries() { return []; } },
  setTimeout: (fn, ms) => { timeouts.push({ fn, ms }); return timeouts.length; },
  clearTimeout: id => { clearedTimeouts.push(id); },
  setInterval: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
  clearInterval: () => {},
});

const read = f => fs.readFileSync(path.resolve(__dirname, '..', 'ui', f), 'utf8');
let appSrc = read('app.js');
appSrc = appSrc.slice(0, appSrc.lastIndexOf('state.page = PAGES[location.hash.slice(1)]'));
vm.runInContext(appSrc, context);
vm.runInContext(read('features/workbench/workbench.js'), context);
const run = code => vm.runInContext(code, context);

// #content 写入计数：轮询守卫的判定依据（innerHTML 不应被无变化的 poll 重写）。
const contentEl = node('#content');
let contentWrites = 0, contentBuf = contentEl.innerHTML;
Object.defineProperty(contentEl, 'innerHTML', {
  get: () => contentBuf, set: v => { contentBuf = v; contentWrites++; }, configurable: true,
});

// 最小观测载荷：满足 renderObservation → overviewHtml 的全部字段。
const obsPayload = calls => ({
  summary: { calls, succeeded: calls, interrupted: 0, failed: 0, unknown: 0, cost: 0, unpriced: 0,
    tokens: { input: { value: 10, samples: 1 }, output: { value: 5, samples: 1 }, reasoning: { value: 0, samples: 1 },
      cacheRead: { value: 0, samples: 1 }, cacheWrite: { value: 0, samples: 1 } },
    durationMs: 12, durationSamples: 1, ttftMs: 3, ttftMsSamples: 1, speed: 5, attempts: 1, retried: 0, attemptSamples: 1 },
  trend: [{ date: '2026-10-21', calls }],
  distribution: { agent: [{ name: 'claude', calls }], provider: [{ name: 'gateway', calls }], model: [{ name: 'm1', calls }] },
  status: { settings: { enabled: true }, collector: { sources: [] } },
  quotas: [], models: [], range: { start: 1, end: 2 },
});

let passed = 0;
const test = async (name, task) => { await task(); passed++; console.log(`PASS ${name}`); };
const clickTarget = daily => ({
  disabled: false, dataset: { daily },
  closest(sel) {
    if (sel === '[data-tab]') return this.dataset.tab ? this : null;
    if (sel === '[data-daily]') return this.dataset.daily ? this : null;
    if (sel === '[data-action]') return this.dataset.action ? this : null;
    return null;
  },
});
const dispatchClick = async daily => {
  const ev = { target: clickTarget(daily), preventDefault() {} };
  await Promise.all((listeners.get('click') || []).map(cb => cb(ev)));
};
const dispatchTab = async tab => {
  const ev = { target: { dataset: { tab }, closest(sel) { return sel === '[data-tab]' ? this : null; } }, preventDefault() {} };
  await Promise.all((listeners.get('click') || []).map(cb => cb(ev)));
};

(async () => {
  await test('agent detail reports a selected retired route instead of hiding it', () => {
    run(`state.modelSync={resolved:{},retiredSelections:{pi:'pi 的模型选择使用已停用的 WorkBuddy 订阅'}};
      state.status={targets:{pi:{files:[]}}};daily.inventory={instances:[],jobs:[]};`);
    const html=run(`agentDetailBody('pi')`);
    assert.match(html,/已停用的 WorkBuddy/);
    assert.match(html,/切换模型/);
  });
  // ---- a) loadObservation 轮询守卫 ----
  await test('poll with unchanged observation data does not rewrite #content', async () => {
    context.__obsPayload = obsPayload(1);
    run(`state.page='observe';state.loaded=true;daily.tab='overview';daily.obs=null;daily.obsError='';
      daily.filter={days:7,agent:"",model:"",provider:"",status:"",q:"",page:1,start:"",end:""};
      api=async()=>JSON.parse(JSON.stringify(__obsPayload));`);
    await run('loadObservation()');
    const afterFirst = contentWrites;
    assert.ok(afterFirst >= 1, 'initial load must render');
    assert.match(contentBuf, /已记录调用/);
    await run('loadObservation(true)');
    assert.equal(contentWrites, afterFirst, 'unchanged poll must not re-render');
    context.__obsPayload = obsPayload(2);
    await run('loadObservation(true)');
    assert.equal(contentWrites, afterFirst + 1, 'changed data must re-render');
  });

  await test('poll re-renders identical data when the previous round errored', async () => {
    const before = contentWrites;
    run(`daily.obsError='模拟读取失败';`);
    await run('loadObservation(true)');
    assert.equal(contentWrites, before + 1, 'success after error must repaint (skip-guard requires !obsError)');
  });

  // ---- b) snapshotObserveInputs / restoreObserveInputs 往返 ----
  await test('observe input snapshot/restore roundtrips value, focus, selection, open state and scroll', async () => {
    const searchInput = { tagName: 'INPUT', id: 'obs-search', value: '正在输入', dataset: {},
      selectionStart: 2, selectionEnd: 4, focused: false, _sel: null,
      focus() { this.focused = true; }, setSelectionRange(s, e) { this._sel = [s, e]; }, closest: () => ({}) };
    const daysSelect = { tagName: 'SELECT', dataset: { obsFilter: 'days' }, value: '30' };
    const startInput = { tagName: 'INPUT', dataset: { time: 'start' }, value: '2026-10-01T08:00' };
    qaLists.set('#content input,#content select', [searchInput, daysSelect, startInput]);
    nodes.set('#obs-search', searchInput);
    nodes.set('[data-obs-filter="days"]', daysSelect);
    nodes.set('[data-time="start"]', startInput);
    document.activeElement = searchInput;
    const rangeDetail = node('.range-detail'); rangeDetail.open = true;
    windowObj.scrollY = 777;

    const snap = run('snapshotObserveInputs()');
    assert.equal(snap.inputs.length, 3);
    assert.equal(JSON.stringify(snap.inputs.map(i => i.sel)),
      JSON.stringify(['#obs-search', '[data-obs-filter="days"]', '[data-time="start"]']));
    assert.equal(snap.activeSel, '#obs-search');
    assert.equal(JSON.stringify(snap.activeInput), JSON.stringify({ start: 2, end: 4 }));
    assert.equal(snap.rangeOpen, true);
    assert.equal(snap.scrollY, 777);

    searchInput.value = '被改掉'; daysSelect.value = '7'; rangeDetail.open = false;
    document.activeElement = null; searchInput.focused = false;
    context.snap = snap;
    run('restoreObserveInputs(snap)');
    assert.equal(searchInput.value, '正在输入');
    assert.equal(daysSelect.value, '30');
    assert.ok(searchInput.focused, 'focus must return to the active input');
    assert.deepEqual(searchInput._sel, [2, 4], 'caret selection must be restored');
    assert.equal(rangeDetail.open, true, 'range details open state must be restored');
    assert.deepEqual(windowObj._scrolled, [0, 777], 'scroll position must be restored');
  });

  // ---- c) update-all：先确认，取消不发 POST ----
  await test('update-all confirms first and cancel issues no POST', async () => {
    run(`state.page='agents';state.loaded=true;
      daily.inventory={instances:[{id:'npm-claude',agent:'claude',canUpdate:true,version:'1.0.0',latestVersion:'2.0.0',path:'C:\\\\npm\\\\claude',updateState:'available'}],jobs:[],
        bulk:{state:'idle',checked:0,total:0,step:'',updated:[],failed:[],skipped:[],startedAt:0,finishedAt:0}};
      var __posts=[];var __confirmOk=false;var __confirmTitle='';
      api=async(method,path,body)=>{__posts.push({method,path,body});
        return {ok:true,bulk:{state:'checking',checked:0,total:1,step:'x',updated:[],failed:[],skipped:[],startedAt:1,finishedAt:0}};};
      confirmDialog=async(title)=>{__confirmTitle=title;return __confirmOk;};`);
    await dispatchClick('update-all');
    assert.match(run('__confirmTitle'), /更新全部可更新实例/);
    assert.equal(run('__posts.length'), 0, 'cancel must not POST');
    run(`__confirmOk=true;__posts=[];`);
    await dispatchClick('update-all');
    assert.equal(run('__posts.length'), 1);
    assert.equal(run('__posts[0].path'), '/api/agents/update-all');
    assert.equal(run('__posts[0].method'), 'POST');
  });

  // ---- d) 设置页读取失败给出路 ----
  await test('settings page surfaces obsError with a retry entry', async () => {
    run(`state.page='settings';daily.obs=null;daily.obsError='无法连接本机索引';renderSettings();`);
    assert.match(contentBuf, /读取失败/);
    assert.match(contentBuf, /data-daily="obs-retry"/);
    assert.match(contentBuf, /重试读取/);
  });

  // ---- e2) obs-search Enter 的输入法守卫 ----
  await test('obs-search Enter respects isComposing (IME confirm must not trigger search)', async () => {
    const searchBtn = node('[data-daily="search"]');
    const keydown = isComposing => (listeners.get('keydown') || []).forEach(cb =>
      cb({ target: { id: 'obs-search' }, key: 'Enter', isComposing }));
    keydown(true);
    assert.equal(searchBtn._clicks, 0, 'composing Enter must be ignored');
    keydown(false);
    assert.equal(searchBtn._clicks, 1, 'plain Enter must click search');
  });

  // ---- Wave B：对话详情消息分批 ----
  await test('conversation detail batches messages, keeps expanded details, and resets on reopen', async () => {
    const messages = Array.from({ length: 120 }, (_, i) => ({ role: i % 2 ? 'assistant' : 'user', time: 1700000000 + i, body: `m${i}` }));
    context.__conv = { contentSource: 'native-on-demand',
      session: { sessionId: 's1', title: '会话', cwd: 'C:\\w', summary: { calls: 3, tokens: { output: { value: 10 } }, cost: 0, unpriced: 0 }, requests: [] },
      messages };
    run(`state.page='observe';daily.tab='requests';
      api=async()=>JSON.parse(JSON.stringify(__conv));daily.messageBatch=0;`);
    // editor-body 每次重写即视为 DOM 换代：换一批全新（未展开）的 message-event 节点。
    const bodyEl = node('#editor-body');
    let bodyHtml = bodyEl.innerHTML; let currentBatch = [];
    const countEvents = () => (bodyHtml.match(/<details class="message-event"/g) || []).length;
    Object.defineProperty(bodyEl, 'innerHTML', {
      get: () => bodyHtml,
      set: v => { bodyHtml = v; currentBatch = Array.from({ length: countEvents() }, () => ({ open: false })); },
      configurable: true,
    });
    qaLists.set('#editor-body .message-event', () => currentBatch);
    await run(`conversationDetail('claude','s1')`);
    assert.equal(countEvents(), 50, 'first batch shows 50');
    assert.match(bodyHtml, /显示剩余 70 条/);
    currentBatch[0].open = true; currentBatch[2].open = true;
    await dispatchClick('messages-more');
    assert.equal(run('daily.messageBatch'), 1);
    assert.equal(countEvents(), 100, 'one more batch appended');
    assert.match(bodyHtml, /显示剩余 20 条/);
    assert.equal(currentBatch[0].open, true, 'expanded detail kept across batches');
    assert.equal(currentBatch[2].open, true);
    assert.equal(currentBatch[1].open, false, 'others stay collapsed');
    assert.ok(node('[data-daily="messages-more"]').focused, 'button keeps focus after batch append');
    await run(`conversationDetail('claude','s1')`); // 重新打开：批次归零
    assert.equal(countEvents(), 50, 'reopen resets to the first batch');
    assert.match(bodyHtml, /显示剩余 70 条/);
  });

  // ---- Wave B：手动路径只还原焦点与搜索草稿 ----
  await test('manual re-render restores focus and obs-search draft but never other control values', async () => {
    const search = { tagName: 'INPUT', id: 'obs-search', dataset: {}, selectionStart: 1, selectionEnd: 2,
      focused: false, _sel: null, focus() { this.focused = true; }, setSelectionRange(s, e) { this._sel = [s, e]; }, closest: () => ({}) };
    const days = { tagName: 'SELECT', dataset: { obsFilter: 'days' } };
    const statusSel = { tagName: 'SELECT', dataset: { obsFilter: 'status' } };
    let daysVal = '7', daysWrites = 0, statusVal = 'failed', statusWrites = 0, searchVal = '未提交草稿';
    Object.defineProperty(days, 'value', { get: () => daysVal, set: v => { daysWrites++; daysVal = v; }, configurable: true });
    Object.defineProperty(statusSel, 'value', { get: () => statusVal, set: v => { statusWrites++; statusVal = v; }, configurable: true });
    Object.defineProperty(search, 'value', { get: () => searchVal, set: v => { searchVal = v; }, configurable: true });
    qaLists.set('#content input,#content select', [search, days, statusSel]);
    nodes.set('#obs-search', search);
    nodes.set('[data-obs-filter="days"]', days);
    nodes.set('[data-obs-filter="status"]', statusSel);
    document.activeElement = search;
    windowObj.scrollY = 0;
    run(`daily.filter.q='';`); // 已提交筛选词为空：模板渲染空搜索框，草稿只能靠还原保住
    await dispatchTab('requests');
    assert.ok(search.focused, 'focus returns to the active search box after a tab switch');
    assert.equal(searchVal, '未提交草稿', 'unsubmitted draft survives manual re-render');
    assert.deepEqual(search._sel, [1, 2], 'caret selection restored');
    assert.equal(daysWrites, 0, 'manual path must not restore other control values');
    assert.equal(statusWrites, 0, 'restoring old values would silently undo submitted filters');
  });

  console.log(`PASS ${passed} workbench contract checks (DOM stub; no browser layout assertion)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
