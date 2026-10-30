/* Run with PLAYWRIGHT_MODULE pointing to a cached playwright package, or npm install --prefix tests playwright. */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const root = path.resolve(__dirname, '..');
const artifacts = path.join(__dirname, 'artifacts');
fs.mkdirSync(artifacts, { recursive: true });
const fixture = spawn(process.env.PYTHON || 'python', ['tests/fixture_server.py'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
let fixtureError = '';
fixture.stderr.on('data', data => { fixtureError += data.toString(); });
const ready = new Promise((resolve, reject) => {
  let output = '';
  fixture.stdout.on('data', data => { output += data.toString(); const match = output.match(/READY (\d+)/); if (match) resolve(`http://127.0.0.1:${match[1]}`); });
  fixture.on('exit', code => reject(new Error(`Fixture exited (${code}): ${fixtureError}`)));
  fixture.on('error', reject);
});
let browser;
const passed = [];
const step = async (name, task) => { await task(); passed.push(name); console.log(`PASS ${name}`); };
(async () => {
  try {
    const url = await ready;
    browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1040 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('console', message => { if (message.type() === 'error' && !message.text().includes('500 (Internal Server Error)') && !message.text().includes('400 (Bad Request)')) errors.push(message.text()); });
    const main = page.locator('main');
    const dialog = page.locator('#editor-dialog');
    const submit = dialog.locator('button[type=submit]');
    const waitSaved = async () => { await dialog.waitFor({ state: 'hidden' }); await page.waitForFunction(() => !document.body.classList.contains('busy')); };
    const config = async () => (await (await page.request.get(`${url}/api/providers`)).json()).providers;
    // 旧「模型库」与「平台同步」已合并为「模型与方案」一页：#sync 只是 #models 的别名。
    const TITLES = { models: '模型与方案', sync: '模型与方案', providers: '供应商' };
    // 同步区块（平台勾选 / 精细覆盖 / 切换方案 / 结果 / 诊断）现在收在 <details> 里，默认收起。
    // 打开它是「用户展开高级」的等价动作；state.advancedOpen 会跨重绘保留，所以调一次即可。
    const openAdvanced = async () => {
      const drawer = main.locator('#models-advanced');
      await drawer.waitFor({ state: 'attached' });
      await drawer.evaluate(el => { el.open = true; });
    };
    const go = async hash => {
      await page.evaluate(hash => location.hash = hash, hash);
      await main.locator('h1').filter({ hasText: TITLES[hash] }).waitFor();
      if (hash === 'models' || hash === 'sync') await openAdvanced();
    };
    const confirm = async label => { await page.locator('#confirm-dialog').waitFor({ state: 'visible' }); await page.locator('#confirm-ok').click(); await page.waitForFunction(() => !document.body.classList.contains('busy')); };

    await step('initial sync workspace and model library layout', async () => {
      await page.goto(url);
      // main 有 150ms 延迟 + 200ms 的 settle-in 入场动画（styles.css:569，fill-mode: both），
      // 延迟窗口内 main 的 opacity 为 0，会让截图拍到空白页。先等它结束再断言与截图。
      await page.waitForFunction(() => { const m = document.querySelector('main'); return !!m && m.getAnimations().every(a => a.playState !== 'running'); }).catch(() => {});
      await main.locator('h1').filter({ hasText: 'Agent' }).waitFor();
      await go('models');
      await main.locator('h1').filter({ hasText: '模型与方案' }).waitFor();
      assert.equal(await page.locator('#nav-model-count').textContent(), '28');
      assert.equal(await page.locator('#model-rows tr').count(), 20);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(artifacts, 'models-desktop.png'), fullPage: true });
    });
    await step('search, provider and capability filtering', async () => {
      await page.locator('#model-search').fill('openrouter/model-2');
      assert.equal(await page.locator('#model-rows tr').count(), 1);
      await page.locator('#model-search').fill('');
      await page.locator('[data-action="filter-provider"][data-pid="deepseek"]').click();
      assert.equal(await page.locator('.model-check').count(), 7);
      await page.locator('#capability-filter').selectOption('image');
      assert.equal(await page.locator('.model-check').count(), 3);
      await page.locator('#capability-filter').selectOption('all');
    });
    await step('add model with provider selection', async () => {
      await main.locator('[data-action="add-model"]').first().click();
      assert.equal(await dialog.locator('[name=contextWindow]').inputValue(), '');
      assert.equal(await dialog.locator('[name=maxTokens]').inputValue(), '');
      assert.equal(await dialog.locator('[name=reasoning]').inputValue(), '');
      await dialog.locator('[name=id]').fill('vendor/my-new-model');
      await dialog.locator('[name=name]').fill('My New Model');
      await dialog.locator('[name=contextWindow]').fill('256000');
      await dialog.locator('[name=input][value=image]').check();
      await page.screenshot({ path: path.join(artifacts, 'model-editor.png') });
      await submit.click();
      await waitSaved();
      assert.equal((await config()).deepseek.models.find(m => m.id === 'vendor/my-new-model').contextWindow, 256000);
    });
    await step('edit encoded slash ID and preserve advanced fields', async () => {
      await main.locator('[data-action="edit-model"][data-mid="deepseek/model-1"]').click();
      await dialog.locator('[name=name]').fill('Updated model');
      await submit.click();
      await waitSaved();
      const model = (await config()).deepseek.models.find(m => m.id === 'deepseek/model-1');
      assert.equal(model.name, 'Updated model');
      assert.equal(model.cost.input, 0.5);
      assert.equal(model.compat.custom, true);
    });
    await step('reference prices are editable without treating unknown as free', async () => {
      await main.locator('[data-action="open-model-detail"][data-mid="deepseek/model-1"]').click();
      await main.locator('#model-detail summary').filter({hasText:'参考单价'}).click();
      await main.locator('#detail-cost-input').fill('0.75');
      await main.locator('#detail-cost-output').fill('0');
      await page.setViewportSize({width:390,height:844});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await page.screenshot({ path: path.join(artifacts, 'model-price-mobile.png'), fullPage: true });
      await page.setViewportSize({width:1440,height:1040});
      await main.locator('[data-action="save-model-detail"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const model = (await config()).deepseek.models.find(m => m.id === 'deepseek/model-1');
      assert.equal(model.cost.input, 0.75);
      assert.equal(model.cost.output, 0);
      assert.equal(model.cost.cacheRead, undefined);
      assert.equal(model.compat.custom, true);
      await main.locator('#model-detail [data-action="close-detail"]').click();
    });
    await step('editing price and full form preserve unknown capabilities', async () => {
      const row = main.locator('[data-action="open-model-detail"][data-mid="deepseek/model-7"]');
      assert.match(await row.textContent(), /未知/);
      await row.click();
      assert.equal(await main.locator('#detail-context').inputValue(), '');
      assert.equal(await main.locator('#detail-maxtokens').inputValue(), '');
      assert.equal(await main.locator('#detail-reasoning').inputValue(), '');
      assert.equal(await main.locator('.detail-input:checked').count(), 0);
      await main.locator('#model-detail summary').filter({hasText:'参考单价'}).click();
      await main.locator('#detail-cost-output').fill('0.2');
      await main.locator('[data-action="save-model-detail"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      let model = (await config()).deepseek.models.find(m => m.id === 'deepseek/model-7');
      assert.equal(model.cost.output, 0.2);
      for (const field of ['contextWindow', 'maxTokens', 'input', 'reasoning']) assert.equal(model[field], undefined);
      await main.locator('#model-detail [data-action="close-detail"]').click();
      await main.locator('[data-action="edit-model"][data-mid="deepseek/model-7"]').click();
      assert.equal(await dialog.locator('[name=contextWindow]').inputValue(), '');
      assert.equal(await dialog.locator('[name=maxTokens]').inputValue(), '');
      assert.equal(await dialog.locator('[name=reasoning]').inputValue(), '');
      await dialog.locator('[name=name]').fill('Unknown capabilities');
      await submit.click();
      await waitSaved();
      model = (await config()).deepseek.models.find(m => m.id === 'deepseek/model-7');
      for (const field of ['contextWindow', 'maxTokens', 'input', 'reasoning']) assert.equal(model[field], undefined);
    });
    await step('duplicate validation and unsaved-change confirmation', async () => {
      await main.locator('[data-action="edit-model"][data-mid="deepseek/model-1"]').click();
      await dialog.locator('[name=id]').fill('deepseek/model-2');
      await submit.click();
      await page.locator('#editor-error').waitFor({ state: 'visible' });
      assert.match(await page.locator('#editor-error').textContent(), /已存在/);
      await dialog.locator('[data-action="close-editor"]').first().click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-cancel').click();
      assert.equal(await dialog.isVisible(), true);
      await dialog.locator('[data-action="close-editor"]').first().click();
      await confirm();
      await dialog.waitFor({ state: 'hidden' });
    });
    await step('add and configure supplier without changing its models', async () => {
      await main.locator('[data-action="add-provider"]').click();
      assert.deepEqual(await dialog.locator('[name=targets]:checked').evaluateAll(inputs => inputs.map(input => input.value)), ['claude', 'codex', 'pi', 'dsh', 'opencode']);
      assert.equal(await dialog.locator('[name=allTargets]').isChecked(), false);
      await dialog.locator('[name=id]').fill('new-provider');
      await dialog.locator('[name=displayName]').fill('New Provider');
      await dialog.locator('[name=baseUrl]').fill('https://example.com/v1');
      await dialog.locator('[name=apiKey]').fill('test-only-key');
      await submit.click();
      await waitSaved();
      assert.equal(Object.keys(await config()).length, 5);
      assert.deepEqual((await config())['new-provider'].targets, ['claude', 'codex', 'pi', 'dsh', 'opencode']);
      await go('providers');
      await main.locator('[data-action="edit-provider"][data-pid="deepseek"]').click();
      await dialog.locator('[name=displayName]').fill('DeepSeek Updated');
      await submit.click();
      await waitSaved();
      assert.equal((await config()).deepseek.models.length, 8);
      await page.screenshot({ path: path.join(artifacts, 'providers-desktop.png'), fullPage: true });
    });
    await step('Claude-only endpoint is configured as a Claude connection', async () => {
      await main.locator('[data-action="add-provider"]').first().click();
      await dialog.locator('[name=id]').fill('claude-plan');
      await dialog.locator('[name=displayName]').fill('Claude Plan');
      // 「按 Agent 覆盖」默认展开，每个 Agent 的连接是一个可折叠小节
      await dialog.locator('summary').filter({ hasText: 'Claude Code 连接' }).click();
      await dialog.locator('[name="agent-claude-baseUrl"]').fill('https://example.com/anthropic');
      await dialog.locator('[name="agent-claude-apiKey"]').fill('test-only-key');
      await submit.click();
      await waitSaved();
      const plan = (await config())['claude-plan'];
      assert.equal(plan.baseUrl, '');
      assert.equal(plan.agents.claude.baseUrl, 'https://example.com/anthropic');
      assert.equal(plan.agents.claude.apiKey, 'test-only-key');
      // 只有 Claude 连接就绪：pi / dsh / codex 不会写入这家供应商
      const status = await (await page.request.get(`${url}/api/status`)).json();
      assert.ok(status.agentCapable.claude.includes('claude-plan'));
      assert.ok(!status.agentCapable.pi.includes('claude-plan'));
      assert.ok(!status.agentCapable.codex.includes('claude-plan'));
      assert.equal(status.connections['claude-plan'].claude.ready, true);
    });
    await step('discovery explicit selection and non-destructive import', async () => {
      await main.locator('[data-action="discover"][data-pid="deepseek"]').click();
      await dialog.waitFor({ state: 'visible' });
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(await dialog.locator('.discover-check:checked').count(), 2);
      assert.equal(await dialog.locator('.discover-check:disabled').count(), 1);
      await dialog.locator('#discover-all').uncheck();
      assert.equal(await submit.isDisabled(), true);
      await dialog.locator('.discover-check[value="1"]').check();
      await page.screenshot({ path: path.join(artifacts, 'model-discovery.png') });
      await submit.click();
      await waitSaved();
      const models = (await config()).deepseek.models;
      assert.equal(models.length, 9);
      assert.equal(models.some(m => m.id === 'vendor/another-new'), false);
      assert.equal(models.find(m => m.id === 'deepseek/model-1').name, 'Updated model');
      const imported = models.find(m => m.id === 'vendor/new-model');
      for (const field of ['contextWindow', 'maxTokens', 'reasoning']) assert.equal(imported[field], undefined);
      assert.deepEqual(imported.input, ['text', 'image']);
    });
    await step('single model deletion confirmation', async () => {
      await main.locator('[data-action="provider-models"][data-pid="deepseek"]').click();
      await main.locator('[data-action="delete-model"][data-mid="vendor/my-new-model"]').click();
      await confirm();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal((await config()).deepseek.models.some(m => m.id === 'vendor/my-new-model'), false);
    });
    await step('visible selection deletes without extra acknowledgement', async () => {
      await main.locator('.model-check').first().check();
      await main.locator('[data-action="bulk-delete"]').click();
      assert.equal(await page.locator('#confirm-ack').isHidden(), true);
      assert.equal(await page.locator('#confirm-ok').isDisabled(), false);
      await page.locator('#confirm-cancel').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'hidden' });
      await main.locator('.model-check').first().uncheck();
    });
    await step('cross-provider bulk deletion requires hidden-selection acknowledgement', async () => {
      await main.locator('.model-check').first().check();
      await main.locator('[data-action="filter-provider"][data-pid="openrouter"]').click();
      await main.locator('.model-check').first().check();
      assert.match(await main.locator('#bulk-bar').textContent(), /已选择 2/);
      await main.locator('[data-action="bulk-delete"]').click();
      assert.equal(await page.locator('#confirm-list > div').count(), 2);
      // 屏幕外的那 1 个模型必须被显式确认，否则不能提交
      assert.match(await page.locator('#confirm-ack-label').textContent(), /看不到的 1 个模型/);
      assert.equal(await page.locator('#confirm-ok').isDisabled(), true);
      await page.screenshot({ path: path.join(artifacts, 'bulk-delete-ack.png') });
      await page.locator('#confirm-ack-box').check();
      assert.equal(await page.locator('#confirm-ok').isDisabled(), false);
      await confirm();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal((await config()).deepseek.models.length, 7);
      assert.equal((await config()).openrouter.models.length, 6);
    });
    await step('legacy agents absent from all daily selection controls',async()=>{
      await go('sync');
      assert.equal(await main.locator('[data-target="zcode"], [data-target="omp"], [data-target="mcode"]').count(),0);
    });
    await step('sync draft protection and linked alias targets', async () => {
      await go('sync');
      for (const checkbox of await main.locator('.target-check:not(:disabled)').all()) await checkbox.uncheck();
      await main.locator('.target-check[data-target="pi"]').check();
      await main.locator('.target-check[data-target="dsh"]').check();
      assert.equal(await main.locator('.target-check[data-target="dsh-desktop"]').count(), 0);
      // #sync 已别名到 #models：同页导航不再「离开」，不该弹确认，草稿要原样留着。
      await page.locator('nav a[href="#models"]').click();
      assert.equal(await page.locator('#confirm-dialog').isVisible(), false);
      assert.equal(await main.locator('.target-check[data-target="pi"]').isChecked(), true);
      // 真正的离开保护：改过 target 后跳到另一个页面必须弹确认；取消后草稿不丢。
      await page.locator('nav a[href="#agents"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-cancel').click();
      assert.equal(await main.locator('h1').textContent(), '模型与方案');
      assert.equal(await main.locator('.target-check[data-target="pi"]').isChecked(), true);
      assert.equal(await main.locator('.target-check[data-target="dsh"]').isChecked(), true);
      await main.locator('[data-action="save-targets"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await main.locator('[data-action="apply-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(await main.locator('.result-row').count(), 2);
      const status = await (await page.request.get(`${url}/api/status`)).json();
      assert.equal(status.targets['dsh-desktop'].enabled, true);
      // All currently enabled independent targets were selected, so no pending config remains.
      assert.equal(status.pendingSync, false);
    });
    await step('per-platform failures remain visible and successful retry clears pending', async () => {
      for (const checkbox of await main.locator('.target-check:not(:disabled)').all()) await checkbox.check();
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await main.locator('[data-action="apply-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(await main.locator('.result-row.error').count(), 1);
      assert.match(await main.locator('.result-row.error').textContent(), /Claude Code/);
      await main.locator('.target-check[data-target="claude"]').uncheck();
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await main.locator('[data-action="apply-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(await main.locator('.result-row.error').count(), 0);
      const status = await (await page.request.get(`${url}/api/status`)).json();
      assert.equal(status.pendingSync, false);
      await page.screenshot({ path: path.join(artifacts, 'sync-desktop.png'), fullPage: true });
    });
    await step('detect-and-sync explains overwrite direction', async () => {
      await main.locator('[data-action="detect-sync"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      assert.match(await page.locator('#confirm-description').textContent(), /不是从客户端导入/);
      await page.locator('#confirm-cancel').click();
    });
    await step('Codex mode requires opt-in and third-party overrides remain editable', async () => {
      for (const checkbox of await main.locator('.target-check:not(:disabled)').all()) await checkbox.uncheck();
      await main.locator('.target-check[data-target="pi"]').check();
      await main.locator('.target-check[data-target="codex"]').check();
      assert.equal(await main.locator('#codex-mode').inputValue(), 'official');
      assert.equal(await main.locator('.ms-client[data-client="codex"].readonly').count(), 0);
      await main.locator('#codex-mode').selectOption('third-party');
      await main.locator('[data-action="run-sync"]').click();
      assert.equal(await main.locator('#sync-preview').isVisible(), false);
      assert.equal((await (await page.request.get(`${url}/api/status`)).json()).targets.codex.mode, 'official');
      await main.locator('#codex-optin').check();
      await main.locator('[data-action="ms-override"][data-target="codex"]').click();
      await main.locator('#ms-provider-clients-codex').selectOption('openrouter');
      await main.locator('#ms-model-clients-codex').selectOption('openrouter/model-3');
      await main.locator('[data-ms-effort][data-target="codex"]').selectOption('high');
      await main.locator('[data-action="save-targets"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const modelsync = await (await page.request.get(`${url}/api/modelsync`)).json();
      assert.equal(modelsync.modelSync.overrides.codex.provider, 'openrouter');
      assert.equal(modelsync.modelSync.overrides.codex.model, 'openrouter/model-3');
      assert.equal(modelsync.modelSync.overrides.codex.reasoningEffort, 'high');
      assert.equal((await (await page.request.get(`${url}/api/status`)).json()).targets.codex.mode, 'third-party');
      await page.screenshot({ path: path.join(artifacts, 'codex-third-party.png'), fullPage: true });
    });
    await step('preview cancel, draft invalidation and one-shot apply', async () => {
      const before = (await (await page.request.get(`${url}/api/status`)).json()).lastTransaction;
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.match(await main.locator('#sync-preview').textContent(), /尚未写入客户端/);
      assert.doesNotMatch(await main.locator('#sync-preview').textContent(), /test-not-a-real-key/);
      assert.equal((await (await page.request.get(`${url}/api/status`)).json()).lastTransaction, before);
      await page.screenshot({ path: path.join(artifacts, 'sync-preview.png'), fullPage: true });
      await main.locator('[data-action="cancel-preview"]').click();
      assert.equal(await main.locator('#sync-preview').isVisible(), false);
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await main.locator('.target-check[data-target="pi"]').uncheck();
      assert.equal(await main.locator('#sync-preview').isVisible(), false);
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      let requests = 0;
      await page.route('**/api/apply', async route => {
        requests++;
        await new Promise(resolve => setTimeout(resolve, 200));
        await route.continue();
      });
      await main.locator('[data-action="apply-preview"]').click();
      assert.equal(await main.locator('[data-action="apply-preview"]').isDisabled(), true);
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await page.unroute('**/api/apply');
      assert.equal(requests, 1);
      assert.equal(await main.locator('#sync-preview').isVisible(), false);
      assert.equal(await main.locator('.result-row').count(), 1);
    });
    await step('stale preview failure requires new preview and official mode keeps override', async () => {
      await main.locator('[data-action="run-sync"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await page.route('**/api/apply', route => route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: '预览已过期，请重新预览' }) }));
      await main.locator('[data-action="apply-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.match(await main.locator('#sync-preview').textContent(), /预览已过期/);
      assert.equal(await main.locator('[data-action="apply-preview"]').count(), 0);
      await page.unroute('**/api/apply');
      await main.locator('#codex-mode').selectOption('official');
      await main.locator('[data-action="save-targets"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const status = await (await page.request.get(`${url}/api/status`)).json();
      assert.equal(status.targets.codex.mode, 'official');
      const modelsync = await (await page.request.get(`${url}/api/modelsync`)).json();
      assert.equal(modelsync.modelSync.overrides.codex.model, 'openrouter/model-3');
      assert.match(await main.locator('.ms-client[data-client="codex"]').textContent(), /恢复.*官方 model/);
    });
    await step('supplier deletion and empty state', async () => {
      await go('providers');
      await main.locator('[data-action="edit-provider"][data-pid="new-provider"]').click();
      await dialog.locator('[data-action="delete-provider"]').click();
      await confirm();
      await waitSaved();
      assert.equal((await config())['new-provider'], undefined);
    });
    await step('in-flight form locking and recoverable API failure', async () => {
      await go('models');
      await main.locator('[data-action="add-model"]').first().click();
      await dialog.locator('[name=id]').fill('network/failure-model');
      let release;
      let writes = 0;
      const gate = new Promise(resolve => { release = resolve; });
      await page.route('**/api/providers/*/models', async route => {
        writes++;
        await gate;
        await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'Temporary test failure' }) });
      });
      await submit.click();
      await page.waitForFunction(() => document.body.classList.contains('busy'));
      assert.equal(await dialog.locator('[name=id]').isDisabled(), true);
      await page.keyboard.press('Control+s');
      release();
      await page.locator('#editor-error').waitFor({ state: 'visible' });
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(writes, 1);
      assert.equal(await dialog.locator('[name=id]').isDisabled(), false);
      assert.equal(await dialog.locator('[name=id]').inputValue(), 'network/failure-model');
      await page.unroute('**/api/providers/*/models');
      await dialog.locator('[data-action="close-editor"]').first().click();
      await confirm();
      await dialog.waitFor({ state: 'hidden' });
    });
    await step('successful save with failed refresh is not treated as failed write', async () => {
      await main.locator('[data-action="add-model"]').first().click();
      await dialog.locator('[name=id]').fill('saved/refresh-failure');
      await page.route('**/api/providers', route => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'Refresh test failure' }) }));
      await submit.click();
      await waitSaved();
      assert.equal(Object.values(await config()).some(p => p.models.some(m => m.id === 'saved/refresh-failure')), true);
      assert.match(await page.locator('#toasts').textContent(), /列表刷新失败/);
      await page.unroute('**/api/providers');
      await page.locator('#refresh').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
    });
    await step('mobile layouts and dialog fit', async () => {
      await page.setViewportSize({ width: 390, height: 844 });
      await go('models');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(artifacts, 'models-mobile.png'), fullPage: true });
      await main.locator('[data-action="add-model"]').first().click();
      const bounds = await dialog.boundingBox();
      assert(bounds.width <= 390);
      await page.screenshot({ path: path.join(artifacts, 'model-editor-mobile.png') });
      await dialog.locator('[data-action="close-editor"]').first().click();
      await go('sync');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
      await page.screenshot({ path: path.join(artifacts, 'sync-mobile.png'), fullPage: true });
    });
    await step('switch plan: one choice, explicit scope, preview, apply, restore', async () => {
      await go('sync');
      await main.locator('.target-check[data-target="pi"]').check();
      await main.locator('.target-check[data-target="claude"]').check();
      await main.locator('[data-action="save-targets"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const card = main.locator('#plan-card');
      await card.locator('#plan-provider').selectOption('deepseek');
      const options = await card.locator('#plan-model option').evaluateAll(nodes => nodes.map(n => n.value));
      assert.ok(options.includes('deepseek/model-2'), `plan model options: ${JSON.stringify(options)}`);
      await card.locator('#plan-model').selectOption('deepseek/model-2');
      // 只检查不写入：必须一个文件都不写
      const before = (await (await page.request.get(`${url}/api/status`)).json()).lastSync;
      await card.locator('[data-action="plan-analyze"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const report = await card.locator('#plan-report').textContent();
      assert.match(report, /切换清单/);
      assert.match(report, /Claude Code/);
      assert.doesNotMatch(report, /test-not-a-real-key/);
      assert.equal((await (await page.request.get(`${url}/api/status`)).json()).lastSync, before);
      // 一次选中连接就绪的 Agent
      await card.locator('[data-action="plan-pick-usable"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const chosen = await card.locator('input[data-plan-pick]:checked').count();
      assert.ok(chosen >= 2, `expected several agents picked, got ${chosen}`);
      // 未勾选的 Agent 用「保留客户端当前值」表达，不写它
      const dsh = card.locator('[data-plan-target="dsh"]');
      if (await dsh.locator('input[data-plan-pick]').isChecked()) {
        await dsh.locator('input[data-plan-pick]').uncheck();
      }
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await card.locator('select[data-plan-policy="dsh"]').selectOption('keep');
      assert.equal(await card.locator('[data-plan-target="dsh"].on').count(), 0);
      // 预览 -> 确认应用
      await card.locator('[data-action="plan-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.match(await card.locator('#plan-result').textContent(), /将写入|尚未写入客户端|不写入/);
      await card.locator('[data-action="plan-confirm-apply"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-ok').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const status = await (await page.request.get(`${url}/api/status`)).json();
      assert.ok(status.lastTransaction, 'apply should record a transaction');
      assert.ok(status.lastResults.length >= 1);
      const sync = await (await page.request.get(`${url}/api/modelsync`)).json();
      assert.equal(sync.modelSync.overrides.dsh, undefined);   // keep -> 不写 dsh
      assert.equal(sync.modelSync.overrides.claude.provider, 'deepseek');
      await page.screenshot({ path: path.join(artifacts, 'switch-plan.png'), fullPage: true });
      // 结果区说明还要客户端动手，不宣称会话已经换模；也不回传配置正文或 JSON
      const resultText = await card.locator('#plan-result').textContent();
      assert.match(resultText, /重启/);
      assert.doesNotMatch(resultText, /\{"ok"|planId|ANTHROPIC_AUTH_TOKEN|test-not-a-real-key/);
      assert.match(resultText, /已写入|将写入/);
      // 恢复这次切换前的配置（结果区里的按钮）
      await card.locator('[data-action="restore-sync"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-ok').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.match(await page.locator('#toasts').textContent(), /已恢复/);
    });
    await step('switch plan: save, reuse and delete a named plan', async () => {
      await go('sync');
      const card = main.locator('#plan-card');
      await card.locator('#plan-provider').selectOption('deepseek');
      await card.locator('#plan-model').selectOption('deepseek/model-3');
      await card.locator('[data-action="plan-pick-usable"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await card.locator('#plan-name').fill('省钱模式');
      await card.locator('[data-action="plan-save"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const saved = await (await page.request.get(`${url}/api/switch-plans`)).json();
      assert.equal(saved.store.plans.length, 1);
      assert.equal(saved.store.plans[0].name, '省钱模式');
      assert.equal(saved.store.default, saved.store.plans[0].id);
      assert.equal(saved.store.plans[0].stale, false);
      // 重新载入并再次应用，不需要重新逐项选择
      await card.locator('#plan-select').selectOption(saved.store.plans[0].id);
      await card.locator('[data-action="plan-load"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.equal(await card.locator('#plan-name').inputValue(), '省钱模式');
      await card.locator('[data-action="plan-preview"]').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      await card.locator('[data-action="plan-confirm-apply"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-ok').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      assert.match(await card.locator('#plan-result').textContent(), /将写入|写入/);
      // 删除方案时不碰客户端配置
      const modelsyncBefore = await (await page.request.get(`${url}/api/modelsync`)).json();
      await card.locator('[data-action="plan-delete"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-ok').click();
      await page.waitForFunction(() => !document.body.classList.contains('busy'));
      const after = await (await page.request.get(`${url}/api/switch-plans`)).json();
      assert.equal(after.store.plans.length, 0);
      assert.equal(after.store.default, null);
      const modelsyncAfter = await (await page.request.get(`${url}/api/modelsync`)).json();
      assert.deepEqual(modelsyncAfter.modelSync, modelsyncBefore.modelSync);
    });
    await step('keyboard save, Escape and runtime errors', async () => {
      await go('models');
      await main.locator('[data-action="add-model"]').first().click();
      await dialog.locator('[name=id]').fill('keyboard/model');
      await page.keyboard.press('Control+s');
      await waitSaved();
      const minimal = Object.values(await config()).flatMap(p => p.models || []).find(m => m.id === 'keyboard/model');
      assert.ok(minimal);
      for (const field of ['contextWindow', 'maxTokens', 'input', 'reasoning']) assert.equal(minimal[field], undefined);
      await main.locator('[data-action="add-model"]').first().click();
      await page.keyboard.press('Escape');
      await dialog.waitFor({ state: 'hidden' });
      assert.deepEqual(errors, []);
    });
    await step('full editor keeps an agent-specific model on its connection', async () => {
      const added = await page.request.post(`${url}/api/providers/deepseek/models`, {data: {
        id: 'only-claude/model', name: 'Only Claude', agent: 'claude',
        contextWindow: 32000, maxTokens: 4000, input: ['text'], reasoning: false,
      }});
      assert.equal(added.status(), 200);
      await page.reload();
      await go('models');
      await page.locator('#model-search').fill('only-claude/model');
      const row = main.locator('[data-action="open-model-detail"][data-agent="claude"][data-mid="only-claude/model"]');
      await row.locator('[data-action="edit-model"]').click();
      await dialog.locator('[name=name]').fill('Claude connection updated');
      await submit.click();
      await waitSaved();
      const provider = (await config()).deepseek;
      assert.equal(provider.agents.claude.models.find(m => m.id === 'only-claude/model').name, 'Claude connection updated');
      assert.equal(provider.models.some(m => m.id === 'only-claude/model'), false);
    });
    console.log(`Browser regression: ${passed.length} passed; screenshots: tests/artifacts/`);
  } catch (error) {
    console.error(`FAIL after ${passed.length} checks: ${error.stack}`);
    if (browser) {
      const pages = browser.contexts().flatMap(c => c.pages());
      if (pages[0]) await pages[0].screenshot({ path: path.join(artifacts, 'failure.png'), fullPage: true }).catch(() => {});
    }
    process.exitCode = 1;
  } finally {
    if (browser) await browser.close();
    fixture.kill();
  }
})();
