// Real browser acceptance path, against only isolated config and native-format fixtures.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..'),out=path.join(__dirname,'artifacts','daily');
fs.mkdirSync(out,{recursive:true});
const fixture=spawn(process.env.PYTHON||'python',['tests/daily_fixture_server.py'],{cwd:root,stdio:['ignore','pipe','pipe']});
let stderr='';fixture.stderr.on('data',d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{let stdout='';fixture.stdout.on('data',d=>{stdout+=d;const m=stdout.match(/READY (\d+)/);if(m)resolve(`http://127.0.0.1:${m[1]}`);});fixture.on('exit',code=>reject(new Error(`fixture ${code}: ${stderr}`)));});
let browser;const results=[];
const step=async(name,fn)=>{await fn();results.push(name);console.log('PASS '+name);};
(async()=>{try{
 const url=await ready;
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 // main 有一条 150ms 延迟 + 200ms 的入场动画（styles.css settle-in，fill-mode: both），
 // 延迟期间 main 的 opacity 是 0。截图必须等它结束，否则会拍到一张「内容全白」的图，
 // 无法作为验收证据。只等 main 自身的动画，不影响子元素里的无限动画（spinner 等）。dialog（#editor-dialog 详情视图等）与 ::backdrop 的 180ms 落位动画（dialog-settle / scrim-in）在 main 子树之外，必须一并等待，否则拍到半透明「鬼影」弹窗（2026-10-25 审查发现）；弹窗内没有无限动画，等待必然收敛。
 const settle=async()=>{await page.waitForFunction(()=>{const els=[document.querySelector('main'),...document.querySelectorAll('dialog[open]')];return els.every(el=>!!el&&el.getAnimations().every(a=>a.playState!=='running'));}).catch(()=>{});};
 const screenshot=async name=>{await settle();await page.screenshot({path:path.join(out,name+'.png'),fullPage:true});};
 const noOverflow=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
 await step('home has five aligned agents and distinct desktop instances',async()=>{
  await page.goto(url);await page.locator('.agent-table tbody tr').first().waitFor();await page.waitForFunction(()=>!!document.querySelector('.instance-picker'));
  // inventory 现在含 opencode（agent_inventory.AGENTS 共 5 项），表格与侧栏徽标同源。
  assert.equal(await page.locator('.agent-table tbody tr').count(),5);
  // 行数与侧栏徽标必须一致：这里显式对账，防止两边再次漂移。
  assert.equal((await page.locator('#nav-agent-count').textContent()).trim(),String(await page.locator('.agent-table tbody tr').count()));
  assert.equal(await page.locator('nav:not(#settings-nav) a').count(),5);
  assert.doesNotMatch(await page.locator('main').innerText(),/ZCode|mcode|omp|cc-switch/);
  await noOverflow();await screenshot('agents-desktop');
 });
 await step('quick model switch reuses preview and exact preview apply receipt',async()=>{
  // 「切换模型」已移入详情抽屉：先开详情，再从抽屉里点切换。
  await page.locator('[data-daily="agent-detail"][data-agent="pi"]').click();
  await page.locator('[data-daily="switch"][data-agent="pi"]').click();
  await page.locator('#quick-provider').selectOption('deepseek');await page.locator('#quick-model').selectOption('deepseek/model-1');
  await page.locator('#editor-form button[type="submit"]').click();await page.locator('[data-daily="quick-apply"]').waitFor();
  await screenshot('switch-preview');await page.locator('[data-daily="quick-apply"]').click();await page.locator('#editor-dialog').waitFor({state:'hidden'});
 });
 await step('OpenCode quick preview explains provider replacement',async()=>{
  await page.locator('[data-daily="agent-detail"][data-agent="opencode"]').click();
  await page.locator('[data-daily="switch"][data-agent="opencode"]').click();
  await page.locator('#quick-provider').selectOption('deepseek');await page.locator('#quick-model').selectOption('deepseek/model-1');
  await page.locator('#editor-form button[type="submit"]').click();await page.locator('[data-daily="quick-apply"]').waitFor();
  assert.match(await page.locator('#quick-result').innerText(),/替换.*provider/);
  await page.locator('#editor-footer [data-action="close-editor"]').click();
  await page.locator('#confirm-ok').click();
  await page.locator('#editor-dialog').waitFor({state:'hidden'});
 });
 await step('overview uses parsed native fixtures and can filter to a failed call',async()=>{
  await page.locator('nav a[href="#observe"]').click();await page.locator('.metric').first().waitFor();
  assert.match(await page.locator('.metric').first().innerText(),/6/);
  await screenshot('overview-desktop');
  await page.locator('.metric').nth(1).click();await page.locator('.requests-table tbody tr').first().waitFor();
  assert.equal(await page.locator('.requests-table tbody tr').count(),1);
  assert.match(await page.locator('.requests-table tbody').innerText(),/失败/);
  await screenshot('failed-requests');
 });
 await step('failed call links to native conversation and context with matching evidence',async()=>{
  await page.locator('[data-daily="request"]').first().click();await page.locator('#editor-dialog').waitFor({state:'visible'});
  assert.match(await page.locator('#editor-body').innerText(),/429/);assert.match(await page.locator('#editor-body').innerText(),/23,000/);
  await screenshot('request-detail');
  // The conversation link must preserve time/agent/model, but remove the
  // request-status facet so the explanation includes neighboring calls.
  await page.locator('[data-daily="related-conversation"]').click();
  await page.locator('.related-requests button').first().waitFor();
  assert.equal(await page.locator('.related-requests button').count(),3);
  assert.doesNotMatch(await page.locator('#editor-body').innerText(),/fixture-secret-do-not-use/);
  await screenshot('conversation-detail');
  await page.locator('[data-daily="related-context"]').click();await page.locator('.context-chart button').first().waitFor();
  assert.equal(await page.locator('.context-chart button').count(),3);
  assert.match(await page.locator('main').innerText(),/23,000/);await screenshot('context-desktop');
 });
 await step('long model and errors, narrow layout, settings and collection stop/clear',async()=>{
  await page.setViewportSize({width:760,height:1000});await noOverflow();await screenshot('context-narrow');
  await page.locator('[data-daily="reset-obs"]').click();await page.locator('[data-tab="conversations"]').click();
  await page.locator('.conversation-row').first().waitFor();await screenshot('conversations-narrow');
  await page.locator('[data-tab="requests"]').click();await page.locator('.requests-table tbody tr').first().waitFor();await noOverflow();await screenshot('requests-narrow');
  await page.locator('nav a[href="#settings"]').click();await page.locator('#collection-enabled').waitFor();await screenshot('settings-narrow');
  await page.locator('#collection-content').uncheck();await page.locator('[data-daily="save-collection"]').click();
  await page.waitForFunction(()=>document.querySelector('.collection-settings').textContent.includes('0 条正文事件'));
  await page.locator('nav a[href="#observe"]').click();await page.locator('[data-tab="conversations"]').click();
  await page.locator('[data-daily="conversation"][data-agent="pi"]').first().click();
  await page.locator('.message-event').first().waitFor();
  assert.match(await page.locator('#editor-body').innerText(),/从客户端已有原生日志按需读取/);
  assert.doesNotMatch(await page.locator('#editor-body').innerText(),/fixture-secret-do-not-use/);
  await screenshot('native-on-demand-default');
  await page.locator('#editor-footer [data-action="close-editor"]').click();
  await page.locator('nav a[href="#settings"]').click();await page.locator('#collection-enabled').waitFor();
  assert.match(await page.locator('.collection-settings').innerText(),/0 条正文事件/);
  await page.locator('[data-daily="clear-observation"]').click();await page.locator('#confirm-ok').click();
  await page.waitForFunction(()=>document.querySelector('.collection-settings').textContent.includes('0 条调用索引'));
  await page.locator('nav a[href="#observe"]').click();await page.locator('.obs-empty').first().waitFor();await screenshot('collection-empty');
  await page.setViewportSize({width:390,height:844});await noOverflow();await screenshot('mobile-empty');
  await page.locator('nav a[href="#agents"]').click();await page.locator('.agent-table').waitFor();await noOverflow();await screenshot('agents-mobile');
 });
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'browser-results.json'),JSON.stringify({passed:results,errors},null,2));
 console.log('PASS all daily UI acceptance checks');
}catch(e){console.error(e);if(browser){for(const p of browser.contexts().flatMap(c=>c.pages())){await p.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error((await p.locator('body').innerText()).slice(-7000));}}process.exitCode=1;}finally{if(browser)await browser.close();fixture.kill();}})();
