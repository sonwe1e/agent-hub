/* Dependency-free contract checks for ui/bridge.js (WebView2 WebMessage fetch rewrite).
   Stubs window.pywebview.api.request and asserts the bridged Response shape, error
   paths and AbortError propagation. Requires Node >= 18 for global Response/DOMException. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const realFetchCalls = [];
const windowObj = {
  fetch(input, init) { realFetchCalls.push({ input, init }); return Promise.resolve('REAL_FETCH_RESULT'); },
};
const listeners = new Map();
const document = { addEventListener: (type, cb) => listeners.set(type, cb) };
const bridgeSrc = fs.readFileSync(path.resolve(__dirname, '../ui/bridge.js'), 'utf8');
const context = vm.createContext({ window: windowObj, document, Response, DOMException, AbortController, URL, console });
vm.runInContext(bridgeSrc, context);

// Wave B：带记录型定时器的第二宿主（bridge 超时兜底检测的是 window.setTimeout）。
const loadBridgedWithTimers = () => {
  const scheduled = [], canceled = [];
  let nextId = 1;
  const win = {
    fetch: (input, init) => Promise.resolve('REAL_FETCH_RESULT'),
    setTimeout: (fn, ms) => { scheduled.push({ fn, ms, id: nextId }); return nextId++; },
    clearTimeout: id => canceled.push(id),
  };
  const ctx = vm.createContext({ window: win, document: { addEventListener() {} }, Response, DOMException, AbortController, URL, console });
  vm.runInContext(bridgeSrc, ctx);
  return { win, scheduled, canceled };
};

let passed = 0;
const test = async (name, task) => { await task(); passed++; console.log(`PASS ${name}`); };
const bridgeFetch = (input, init) => windowObj.fetch(input, init); // bridge 已覆写 window.fetch

(async () => {
  await test('bridged fetch returns a real Response with payload body and JSON content type', async () => {
    const calls = [];
    windowObj.pywebview = { api: { request: async (method, path, body) => { calls.push({ method, path, body }); return { status: 200, body: { ok: 1, list: [1, 2] } }; } } };
    const res = await bridgeFetch('/api/providers', { method: 'POST', body: JSON.stringify({ q: 1 }) });
    assert.ok(res instanceof Response, 'must resolve to a Response, not raw payload');
    assert.equal(res.status, 200);
    assert.equal(res.ok, true);
    assert.equal(res.headers.get('content-type'), 'application/json; charset=utf-8');
    assert.equal(JSON.stringify(await res.json()), JSON.stringify({ ok: 1, list: [1, 2] }));
    assert.equal(JSON.stringify(calls), JSON.stringify([{ method: 'POST', path: '/api/providers', body: { q: 1 } }]),
      'string JSON body is parsed before crossing the WebMessage boundary');
  });

  await test('error status keeps HTTP semantics (res.ok false) and body shape', async () => {
    windowObj.pywebview = { api: { request: async () => ({ status: 409, body: { error: '已有批量任务正在运行', bulk: { state: 'checking' } } }) } };
    const res = await bridgeFetch('/api/agents/update-all', { method: 'POST', body: '{}' });
    assert.equal(res.status, 409);
    assert.equal(res.ok, false);
    const data = await res.json();
    assert.equal(data.error, '已有批量任务正在运行');
    assert.equal(data.bulk.state, 'checking');
  });

  await test('non-JSON string body becomes null instead of failing silently elsewhere', async () => {
    const calls = [];
    windowObj.pywebview = { api: { request: async (m, p, body) => { calls.push(body); return { status: 200, body: {} }; } } };
    await bridgeFetch('/api/x', { method: 'POST', body: 'not-json' });
    assert.deepEqual(calls, [null], 'unparseable string body is coerced to null (documented bridge behavior)');
  });

  await test('pywebview rejection propagates as fetch rejection', async () => {
    windowObj.pywebview = { api: { request: async () => { throw new Error('pywebview down'); } } };
    await assert.rejects(bridgeFetch('/api/x'), /pywebview down/);
  });

  await test('aborting the signal rejects with AbortError while the request is in flight', async () => {
    windowObj.pywebview = { api: { request: () => new Promise(() => {}) } };
    const controller = new AbortController();
    const pending = bridgeFetch('/api/slow', { signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, e => e.name === 'AbortError');
  });

  await test('pre-aborted signal rejects immediately without reaching pywebview', async () => {
    let reached = false;
    windowObj.pywebview = { api: { request: async () => { reached = true; return { status: 200, body: {} }; } } };
    const controller = new AbortController();
    controller.abort();
    await assert.rejects(bridgeFetch('/api/x', { signal: controller.signal }), e => e.name === 'AbortError');
    assert.equal(reached, false);
  });

  await test('non-/api/ paths pass through to the real fetch untouched', async () => {
    windowObj.pywebview = { api: { request: async () => ({ status: 200, body: {} }) } };
    const before = realFetchCalls.length;
    const out = await bridgeFetch('/favicon.svg');
    assert.equal(out, 'REAL_FETCH_RESULT');
    assert.equal(realFetchCalls.length, before + 1);
  });

  await test('without pywebview (browser dev mode) /api/ paths also pass through', async () => {
    windowObj.pywebview = undefined;
    const before = realFetchCalls.length;
    const out = await bridgeFetch('/api/status');
    assert.equal(out, 'REAL_FETCH_RESULT');
    assert.equal(realFetchCalls.length, before + 1);
  });

  // ---- Wave B：桥自身 60s 超时兜底 ----
  await test('bridged fetch rejects with an actionable message when the 60s guard fires', async () => {
    const { win, scheduled } = loadBridgedWithTimers();
    win.pywebview = { api: { request: () => new Promise(() => {}) } }; // WebMessage 挂起
    const pending = win.fetch('/api/hang', { method: 'POST', body: '{}' });
    assert.equal(scheduled.length, 1, 'exactly one guard timer per request');
    assert.equal(scheduled[0].ms, 60000, 'guard duration is 60s (shorter than app.js 120s)');
    scheduled[0].fn(); // 模拟 60s 到期
    await assert.rejects(pending, e => /60 秒/.test(e.message) && /勿重复提交/.test(e.message),
      'timeout message must state the wait and warn against duplicate submits');
  });

  await test('a settling response cancels the guard timer', async () => {
    const { win, scheduled, canceled } = loadBridgedWithTimers();
    win.pywebview = { api: { request: async () => ({ status: 200, body: { ok: 1 } }) } };
    const res = await win.fetch('/api/x', { method: 'POST', body: '{}' });
    assert.equal(res.status, 200);
    assert.equal(scheduled.length, 1);
    assert.deepEqual(canceled, [scheduled[0].id], 'settle must clear the pending guard');
  });

  await test('abort wins over the pending guard; a late guard callback is a no-op', async () => {
    const { win, scheduled, canceled } = loadBridgedWithTimers();
    win.pywebview = { api: { request: () => new Promise(() => {}) } };
    const controller = new AbortController();
    const pending = win.fetch('/api/slow', { signal: controller.signal });
    controller.abort();
    await assert.rejects(pending, e => e.name === 'AbortError');
    assert.deepEqual(canceled, [scheduled[0].id], 'abort settlement clears the guard timer');
    scheduled[0].fn(); // 迟到的超时回调：settled 守卫必须吞掉，不得二次 reject
  });

  console.log(`PASS ${passed} bridge contract checks (pywebview stub; no WebView2 required)`);
})().catch(error => { console.error(error); process.exitCode = 1; });
