// Four production observation views over scrubbed REAL client records.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const {spawn}=require('node:child_process');
const path=require('node:path'),fs=require('node:fs'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const report=path.resolve(process.env.LIVE_OBSERVATION_REPORT||path.join(__dirname,'artifacts/live-report-20261017'));
const expected=JSON.parse(fs.readFileSync(path.join(report,'observation.json'),'utf8'));
const out=path.join(report,'screenshots');fs.mkdirSync(out,{recursive:true});
const fixture=spawn(process.env.PYTHON||'python',['tests/live_capture_server.py'],{
 cwd:root,env:{...process.env,LIVE_OBSERVATION_REPORT:report},stdio:['ignore','pipe','pipe']});
let stderr='';fixture.stderr.on('data',d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{let stdout='';fixture.stdout.on('data',d=>{stdout+=d;const m=stdout.match(/READY (\d+)/);if(m)resolve(`http://127.0.0.1:${m[1]}`);});fixture.on('exit',code=>reject(new Error(`capture server ${code}: ${stderr}`)));});
let browser;const passed=[];
const step=async(name,fn)=>{await fn();passed.push(name);console.log('PASS '+name);};
(async()=>{try{
 const url=await ready;
 browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
 const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const shot=async name=>page.screenshot({path:path.join(out,name+'.png'),fullPage:true});
 await step('real overview matches collector, contains all four agents, keeps unconfirmed outcomes',async()=>{
  await page.goto(url+'/#observe');await page.locator('.metric').first().waitFor();
  assert.match(await page.locator('.metric').first().innerText(),new RegExp(String(expected.total)));
  for(const label of ['Claude Code','Codex','pi','dsh'])assert.match(await page.locator('main').innerText(),new RegExp(label));
  if(expected.summary.unknown)assert.match(await page.locator('main').innerText(),/结果未确认/);
  await shot('live-overview');
 });
 const failed=expected.requests.find(r=>r.status==='failed');
 await step('real failure links overview to exact request and native error',async()=>{
  await page.locator('.metric').nth(1).click();await page.locator('.requests-table tbody tr').first().waitFor();
  assert.equal(await page.locator('.requests-table tbody tr').count(),expected.summary.failed);
  await shot('live-failed-requests');
  await page.locator(`[data-daily="request"][data-id="${failed.id}"]`).click();
  await page.locator('#editor-dialog').waitFor({state:'visible'});
  const text=await page.locator('#editor-body').innerText();
  assert.ok(failed.error);assert.ok(text.includes(failed.error));assert.match(text,/初始化值/);assert.match(text,/无法计价/);
  assert.match(text,new RegExp(failed.nativeId));await shot('live-request-detail');
 });
 await step('request links to same native session and unknown context remains unknown',async()=>{
  await page.locator('[data-daily="related-conversation"]').click();await page.locator('.related-requests button').first().waitFor();
  assert.match(await page.locator('#editor-body').innerText(),new RegExp(failed.sessionId));
  const session=expected.sessions.find(s=>s.agent===failed.agent&&s.sessionId===failed.sessionId);
  assert.equal(await page.locator('.related-requests button').count(),session.requests.length);
  for(const event of await page.locator('.message-event').all())await event.locator('summary').click();
  const body=await page.locator('#editor-body').innerText();
  assert.ok(body.includes(failed.error)||body.includes(JSON.stringify(failed.error).slice(1,-1)));
  await shot('live-failed-conversation');
  await page.locator('[data-daily="related-context"]').click();await page.locator('.context-chart button').first().waitFor();
  assert.equal(await page.locator('.context-chart button').count(),session.points.length);await shot('live-failed-context');
 });
 const gatewayRecord=expected.requests.find(r=>r.sourceType==='gateway+native'&&r.contextParts&&r.providerEvidence==='gateway-upstream'&&r.provider);
 if(gatewayRecord)await step('actual gateway timings and estimated context parts link to native tool conversation',async()=>{
  await page.locator('[data-daily="reset-obs"]').click();
  await page.locator('[data-tab="requests"]').click();await page.locator('.requests-table').waitFor();
  await page.locator(`[data-daily="request"][data-id="${gatewayRecord.id}"]`).click();
  await page.locator('#editor-dialog').waitFor({state:'visible'});
  const text=await page.locator('#editor-body').innerText();
  assert.match(text,/网关实际上游线路/);assert.match(text,/分项为估算/);assert.match(text,/ASCII/);
  assert.ok(gatewayRecord.ttftMs>0);assert.ok(gatewayRecord.contextParts.parts.length);
  await shot('gateway-request-context-parts');
  await page.setViewportSize({width:760,height:1000});
  await page.locator('.context-parts').scrollIntoViewIfNeeded();
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await shot('gateway-context-parts-narrow');
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-daily="related-conversation"]').click();await page.locator('.message-event').first().waitFor();
  assert.match(await page.locator('#editor-body').innerText(),new RegExp(gatewayRecord.sessionId));await shot('gateway-native-conversation');
  await page.locator('[data-daily="related-context"]').click();await page.locator('.context-chart button').first().waitFor();
  assert.ok((await page.locator('.context-chart button').evaluateAll(ns=>ns.map(n=>n.dataset.id))).includes(gatewayRecord.id));await shot('gateway-context-linked');
 });
 await step('DeepSeek account tool trace and two measured context points retain request ids',async()=>{
  await page.locator('[data-daily="reset-obs"]').click();
  await page.locator('[data-tab="requests"]').click();await page.locator('.requests-table').waitFor();
  // Open by native request id through the normal production detail action.
  const dsh=expected.requests.find(r=>r.agent==='dsh'&&r.provider==='deepseek-account');
  const option=page.locator('[data-obs-filter="agent"]');
  await option.selectOption('dsh');await page.locator(`[data-daily="request"][data-id="${dsh.id}"]`).waitFor();
  await page.locator(`[data-daily="request"][data-id="${dsh.id}"]`).click();
  await page.locator('[data-daily="related-conversation"]').click();await page.locator('.message-event').first().waitFor();
  const tools=page.locator('.message-event').filter({hasText:'tool/'});
  for(const detail of await tools.all())await detail.locator('summary').click();
  assert.match(await page.locator('#editor-body').innerText(),/toolCallId/);
  assert.match(await page.locator('#editor-body').innerText(),/HUB_READ_/);await shot('live-dsh-tool-conversation');
  await page.locator('[data-daily="related-context"]').click();await page.locator('.context-chart button').first().waitFor();
  assert.equal(await page.locator('.context-chart button').count(),2);
  const session=expected.sessions.find(s=>s.agent==='dsh'&&s.sessionId===dsh.sessionId);
  const ids=await page.locator('.context-chart button').evaluateAll(nodes=>nodes.map(n=>n.dataset.id));
  assert.deepEqual(ids.sort(),session.points.map(p=>p.id).sort());await shot('live-dsh-context');
  await page.setViewportSize({width:760,height:1000});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await shot('live-context-narrow');
 });
 if(gatewayRecord)await step('gateway settings start owned engine, preview and apply connection, then restore direct routes',async()=>{
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('nav a[href="#settings"]').click();await page.locator('[data-daily="gateway-start"]').waitFor();
  await page.locator('[data-daily="gateway-start"]').click();
  await page.waitForFunction(()=>document.querySelector('.gateway-settings').textContent.includes('运行中'));
  await shot('gateway-settings-running');
  await page.locator('[data-daily="gateway-connect"]').click();await page.locator('[data-daily="gateway-apply"]').waitFor();
  await shot('gateway-connection-preview');
  await page.locator('[data-daily="gateway-apply"]').click();await page.locator('#editor-dialog').waitFor({state:'hidden'});
  const connected=await (await page.request.get(url+'/api/gateway')).json();assert.equal(connected.enabled,true);assert.equal(connected.state,'running');
  await page.locator('[data-daily="gateway-disconnect"]').click();await page.locator('[data-daily="gateway-apply"]').waitFor();
  await page.locator('[data-daily="gateway-apply"]').click();await page.locator('#editor-dialog').waitFor({state:'hidden'});
  const disconnected=await (await page.request.get(url+'/api/gateway')).json();assert.equal(disconnected.enabled,false);assert.equal(disconnected.state,'stopped');
  await page.setViewportSize({width:760,height:1000});assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);await shot('gateway-settings-narrow');
 });
 assert.deepEqual(errors,[]);fs.writeFileSync(path.join(report,'browser-results.json'),JSON.stringify({passed,errors},null,2));
 console.log('PASS real-capture browser verification');
}catch(e){console.error(e);if(browser){for(const p of browser.contexts().flatMap(c=>c.pages())){await p.screenshot({path:path.join(out,'failure.png'),fullPage:true});console.error((await p.locator('body').innerText()).slice(-4500));}}process.exitCode=1;}
finally{if(browser)await browser.close();fixture.kill();}})();
