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
    const go = async hash => { await page.locator(`nav a[href="#${hash}"]`).click(); await main.locator('h1').filter({ hasText: ({ models: '模型库', providers: '供应商', sync: '平台同步' })[hash] }).waitFor(); };
    const confirm = async label => { await page.locator('#confirm-dialog').waitFor({ state: 'visible' }); await page.locator('#confirm-ok').click(); await page.waitForFunction(() => !document.body.classList.contains('busy')); };

    await step('initial sync workspace and model library layout', async () => {
      await page.goto(url);
      await main.locator('h1').filter({ hasText: '平台同步' }).waitFor();
      await go('models');
      await main.locator('h1').filter({ hasText: '模型库' }).waitFor();
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
      assert.deepEqual(await dialog.locator('[name=targets]:checked').evaluateAll(inputs => inputs.map(input => input.value)), ['pi', 'dsh', 'claude', 'codex']);
      assert.equal(await dialog.locator('[name=allTargets]').isChecked(), false);
      await dialog.locator('[name=id]').fill('new-provider');
      await dialog.locator('[name=displayName]').fill('New Provider');
      await dialog.locator('[name=baseUrl]').fill('https://example.com/v1');
      await dialog.locator('[name=apiKey]').fill('test-only-key');
      await submit.click();
      await waitSaved();
      assert.equal(Object.keys(await config()).length, 5);
      assert.deepEqual((await config())['new-provider'].targets, ['pi', 'dsh', 'claude', 'codex']);
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
    await step('sync draft protection and linked alias targets', async () => {
      await go('sync');
      for (const checkbox of await main.locator('.target-check:not(:disabled)').all()) await checkbox.uncheck();
      await main.locator('.target-check[data-target="pi"]').check();
      await main.locator('.target-check[data-target="dsh"]').check();
      assert.equal(await main.locator('.target-check[data-target="dsh-desktop"]').isChecked(), true);
      assert.equal(await main.locator('.target-check[data-target="dsh-desktop"]').isDisabled(), true);
      await page.locator('nav a[href="#models"]').click();
      await page.locator('#confirm-dialog').waitFor({ state: 'visible' });
      await page.locator('#confirm-cancel').click();
      assert.equal(await main.locator('h1').textContent(), '平台同步');
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
      assert.match(await main.locator('.result-row.error').textContent(), /omp/);
      await main.locator('.target-check[data-target="omp"]').uncheck();
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
    await step('keyboard save, Escape and runtime errors', async () => {
      await go('models');
      await main.locator('[data-action="add-model"]').first().click();
      await dialog.locator('[name=id]').fill('keyboard/model');
      await page.keyboard.press('Control+s');
      await waitSaved();
      await main.locator('[data-action="add-model"]').first().click();
      await page.keyboard.press('Escape');
      await dialog.waitFor({ state: 'hidden' });
      assert.deepEqual(errors, []);
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
