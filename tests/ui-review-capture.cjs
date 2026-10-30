// 设计评审捕获：对 daily fixture 渲染的界面做 1440×1000 全页截图。
// 用法：node tests/ui-review-capture.cjs <url> <outdir>
// url 由调用方启动 tests/daily_fixture_server.py 后从其 READY 行取得；
// 本脚本只通过 HTTP 与夹具对话，不派生 Python 子进程（沙箱下管道受限）。
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const path = require('node:path'), fs = require('node:fs');
const [, , url, outArg] = process.argv;
if (!url || !outArg) { console.error('usage: node ui-review-capture.cjs <url> <outdir>'); process.exit(2); }
const out = path.resolve(outArg);
fs.mkdirSync(out, { recursive: true });
// main 有 150ms 延迟 + 200ms 入场动画（styles.css settle-in，fill-mode: both），
// 延迟期间 opacity 为 0，截图必须等它结束，否则拍到「内容全白」。dialog 与 ::backdrop 的落位动画在 main 子树之外，同样要等，否则拍到鬼影弹窗（同 daily_browser，2026-10-25 修复）。
const settle = page => page.waitForFunction(() => {
  const els = [document.querySelector('main'), ...document.querySelectorAll('dialog[open]')];
  return els.every(el => !!el && el.getAnimations().every(a => a.playState !== 'running'));
}).catch(() => {});
const shot = async (page, name) => { await settle(page); await page.screenshot({ path: path.join(out, name + '.png'), fullPage: true }); console.log('SHOT ' + name); };
const go = async (page, hash) => { await page.evaluate(h => { location.hash = h; }, hash); };
(async () => {
  let browser;
  try {
    browser = await chromium.launch({ headless: true, ...(process.env.BROWSER_EXECUTABLE ? { executablePath: process.env.BROWSER_EXECUTABLE } : {}) });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(url);
    await page.locator('.agent-table tbody tr').first().waitFor();
    await shot(page, 'agents-1440');
    await go(page, '#models');
    await page.locator('#model-rows tr').first().waitFor();
    await shot(page, 'models-1440');
    await page.locator('#model-rows tr[data-action="open-model-detail"]').first().click();
    await page.locator('#model-detail .detail-foot').waitFor();
    await shot(page, 'models-detail-open-1440');
    await go(page, '#providers');
    await page.locator('.provider-card').first().waitFor();
    await shot(page, 'providers-1440');
    await go(page, '#settings');
    await page.locator('#collection-enabled').waitFor();
    await shot(page, 'settings-1440');
    await go(page, '#observe');
    await page.locator('.metric').first().waitFor();
    await shot(page, 'observe-overview-1440');
    await page.locator('[data-tab="requests"]').click();
    await page.locator('.requests-table tbody tr').first().waitFor();
    await shot(page, 'observe-requests-1440');
    await page.locator('[data-tab="conversations"]').click();
    await page.locator('.conversation-row').first().waitFor();
    await shot(page, 'observe-conversations-1440');
    await page.locator('[data-tab="context"]').click();
    await page.locator('.context-session').first().waitFor();
    await shot(page, 'observe-contexts-1440');
    if (errors.length) { console.error('PAGE ERRORS:\n' + errors.join('\n')); process.exitCode = 1; }
    console.log('CAPTURE-OK ' + out);
  } catch (e) {
    console.error(e);
    if (browser) { for (const p of browser.contexts().flatMap(c => c.pages())) await p.screenshot({ path: path.join(out, 'failure.png'), fullPage: true }).catch(() => {}); }
    process.exitCode = 1;
  } finally { if (browser) await browser.close(); }
})();
