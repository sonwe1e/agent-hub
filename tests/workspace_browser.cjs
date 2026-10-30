// End-to-end UI paths against a temporary HOME and mocked inventory.
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||"playwright");
const {spawn}=require("node:child_process");
const path=require("node:path"), fs=require("node:fs"), assert=require("node:assert/strict");
const root=path.resolve(__dirname,".."), out=path.join(__dirname,"artifacts","workspace");
fs.mkdirSync(out,{recursive:true});
const fixture=spawn(process.env.PYTHON||"python",["tests/workspace_fixture_server.py"],{cwd:root,stdio:["ignore","pipe","pipe"]});
let stderr=""; fixture.stderr.on("data",d=>stderr+=d);
const ready=new Promise((resolve,reject)=>{
  let output=""; fixture.stdout.on("data",d=>{output+=d; const m=output.match(/READY (\d+)/); if(m) resolve(`http://127.0.0.1:${m[1]}`);});
  fixture.on("exit",code=>reject(new Error(`fixture ${code}: ${stderr}`)));
});
let browser; const errors=[];
async function step(name,fn){await fn();console.log("PASS "+name);}
(async()=>{try{
  const url=await ready;
  browser=await chromium.launch({headless:true,...(process.env.BROWSER_EXECUTABLE?{executablePath:process.env.BROWSER_EXECUTABLE}:{})});
  const page=await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
  page.on("pageerror",e=>errors.push(e.message));
  const snap=async name=>{await page.waitForTimeout(650);await page.screenshot({path:path.join(out,name+".png"),fullPage:true});};
  const noOverflow=async()=>assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await step("home displays actionable workspace summaries",async()=>{
    await page.goto(url);await page.locator(".work-summary").waitFor();
    await page.waitForFunction(()=>document.querySelector(".work-summary")?.innerText.includes("已管理"));
    assert.match(await page.locator(".work-summary").innerText(),/继续工作|技能与指令|订阅额度/);
    await noOverflow();await snap("home");
  });
  await step("discover, register, preview and publish a full skill",async()=>{
    await page.locator('a[href="#library"]').first().click();
    await page.locator('[data-feature="discover"]').click();
    const candidate=page.locator(".feature-row").filter({hasText:"fixture-skill"});
    await candidate.locator('[data-feature="select-candidate"]').click();
    assert.match(await page.locator("#work-item-source").inputValue(),/fixture-skill/);
    await page.locator('[data-feature="save-item"]').click();
    await page.locator("#editor-dialog").waitFor({state:"hidden"});
    await page.locator('[data-feature="preview"]').click();
    await page.locator('[data-feature="apply-preview"]').waitFor();
    assert.match(await page.locator("#editor-body").innerText(),/共享技能目录/);
    assert.match(await page.locator("#editor-body").innerText(),/guide.md/);
    await snap("publish-preview");
    await page.locator('[data-feature="apply-preview"]').click();
    await page.locator("#editor-dialog").waitFor({state:"hidden"});
    await page.waitForFunction(()=>document.querySelector("#content")?.innerText.includes("与发布版一致"));
    assert.match(await page.locator("#content").innerText(),/与发布版一致/);
    await snap("library");
  });
  await step("explicit per-agent instruction replacement is previewed",async()=>{
    await page.locator('[data-feature="discover"]').click();
    const candidate=page.locator(".feature-row").filter({hasText:"AGENTS.md"}).first();
    await candidate.locator('[data-feature="select-candidate"]').click();
    await page.locator("#work-item-name").fill("fixture-instructions");
    await page.locator("#work-overrides summary").click();
    await page.locator('[data-feature="override-add"][data-agent="pi"]').click();
    const pair=page.locator('[data-override-rows="pi"] .work-override-row');
    await pair.locator(".work-replace-from").fill("--agent codex");
    await pair.locator(".work-replace-to").fill("--agent pi");
    await page.locator('[data-feature="save-item"]').click();
    await page.locator("#editor-dialog").waitFor({state:"hidden"});
    const row=page.locator("#content .feature-row").filter({hasText:"fixture-instructions"});
    await row.locator('[data-feature="preview"]').click();
    await page.locator('[data-feature="apply-preview"]').waitFor();
    assert.match(await page.locator("#editor-body").innerText(),/--agent pi/);
    await page.locator('#editor-dialog [data-action="close-editor"]').last().click();
  });
  await step("handoff stays editable and does not execute by default",async()=>{
    await page.locator('a[href="#sessions"]').first().click();
    await page.locator('[data-feature="handoff"]').first().waitFor();
    const named=page.locator("#content .feature-row").filter({hasText:"调查失败请求并保留证据"});
    assert.match(await named.innerText(),/用户消息节选/);
    assert.doesNotMatch(await named.innerText(),/fixture-secret-do-not-use/);
    assert.equal(await named.locator('[data-feature="resume-preview"]').isEnabled(),true);
    const gateway=page.locator('[data-feature="resume-preview"][data-session="fixture-gateway-session"]').locator('..').locator('..');
    assert.match(await gateway.innerText(),/仅有网关调用记录，未关联原生会话/);
    assert.equal(await gateway.locator('[data-feature="resume-preview"]').isDisabled(),true);
    assert.equal(await gateway.locator('[data-feature="handoff"]').isEnabled(),true);
    const checkGatewayRows = async()=>{
      const meta=await gateway.locator('.feature-row-main > small').boundingBox();
      const notice=await gateway.locator('.feature-row-main p > small').boundingBox();
      assert.ok(meta && notice && notice.y>=meta.y+meta.height,'gateway notice has its own line');
      await noOverflow();
    };
    await checkGatewayRows();
    await snap("sessions");
    await page.setViewportSize({width:700,height:900});
    await checkGatewayRows();
    await snap("sessions-narrow");
    await page.setViewportSize({width:1440,height:1000});
    await page.locator('[data-feature="handoff"]').first().click();
    await page.locator('[data-feature="draft"]').click();
    await page.locator("#work-handoff-text").waitFor();
    assert.equal(await page.locator("#work-handoff-start").isChecked(),false);
    assert.match(await page.locator("#editor-body").innerText(),/未发送|未经|不等于/);
    await page.locator('[data-feature="summary-select"]').click();
    await page.locator('[data-feature="summary-preview"]').waitFor();
    await page.locator('[data-feature="summary-preview"]').click();
    await page.locator('[data-feature="summary-generate"]').waitFor();
    assert.match(await page.locator("#editor-body").innerText(),/将发送的材料/);
    assert.match(await page.locator("#editor-body").innerText(),/接收方/);
    await snap("summary-preview");
    await page.locator('[data-feature="draft-back"]').click();
    await page.locator("#editor-dialog [data-action=close-editor]").last().click();
    await page.locator("#editor-dialog").waitFor({state:"hidden"});
  });
  await step("manual names work for gateway and native sessions with cancel, errors and refresh",async()=>{
    const requests=[];
    let failNext=false, heldRead=null, gatewayCaptured=false;
    await page.route('**/api/sessions/title',async route=>{
      const body=route.request().postDataJSON(); requests.push(body);
      if(failNext) {failNext=false;return route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({error:'模拟保存失败'})});}
      return route.continue();
    });
    await page.route('**/api/sessions/recent',async route=>{
      const response=await route.fetch(), data=await response.json();
      for(const row of data.sessions||[]) if(gatewayCaptured && row.sessionId==='fixture-gateway-session' && row.titleSource!=='manual') {row.title='网关测试请求节选';row.titleSource='gateway-user-message';}
      if(heldRead) {const held=heldRead;heldRead=null;held.observed();await held.release;}
      await route.fulfill({response,body:JSON.stringify(data)});
    });
    const gatewaySelector='[data-feature="session-rename"][data-session="fixture-gateway-session"]';
    const gatewayRow=()=>page.locator(gatewaySelector).locator('..').locator('..');
    await page.locator(gatewaySelector).click();
    assert.equal(await page.locator('#work-session-title').inputValue(),'');
    assert.match(await page.locator('#editor-body').innerText(),/只保存在本机|留空/);
    await snap('session-title');
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    assert.equal(requests.length,0);

    await page.locator(gatewaySelector).click();
    await page.locator('#work-session-title').fill('尚未保存的名称');
    await page.locator('#editor-footer [data-action="close-editor"]').click();
    await page.locator('#confirm-ok').click();
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    assert.equal(requests.length,0);

    await page.locator(gatewaySelector).click();
    await page.locator('#work-session-title').fill('a'.repeat(61));
    await page.locator('[data-feature="save-session-title"]').click();
    assert.match(await page.locator('#editor-error').innerText(),/不能超过 60 字/);
    assert.equal(requests.length,0);

    failNext=true;
    await page.locator('#work-session-title').fill('修复测试会话');
    await page.locator('[data-feature="save-session-title"]').click();
    await page.waitForFunction(()=>{const e=document.querySelector('#editor-error');return !e.hidden&&e.textContent.includes('模拟保存失败');});
    assert.match(await page.locator('#editor-error').innerText(),/模拟保存失败/);
    assert.equal(await page.locator('#work-session-title').inputValue(),'修复测试会话');
    assert.equal(await page.locator('#editor-dialog').isVisible(),true);
    assert.deepEqual(Object.keys(requests[0]).sort(),['agent','sessionId','title']);
    assert.deepEqual(requests[0],{agent:'pi',sessionId:'fixture-gateway-session',title:'修复测试会话'});
    await page.locator('#work-session-title').press('Enter');
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('[data-feature="session-rename"][data-session="fixture-gateway-session"]')?.closest('.feature-row')?.innerText.includes('修复测试会话'));
    assert.match(await gatewayRow().innerText(),/修复测试会话[\s\S]*手工命名/);
    assert.match(await gatewayRow().innerText(),/仅有网关调用记录，未关联原生会话/);
    assert.equal(await gatewayRow().locator('[data-feature="resume-preview"]').isDisabled(),true);

    await page.locator(gatewaySelector).click();
    assert.equal(await page.locator('#work-session-title').inputValue(),'修复测试会话');
    await page.locator('#work-session-title').fill('');
    await page.locator('[data-feature="save-session-title"]').click();
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('[data-feature="session-rename"][data-session="fixture-gateway-session"]')?.closest('.feature-row')?.innerText.includes('未命名会话'));
    assert.equal(requests.at(-1).title,'');
    assert.doesNotMatch(await gatewayRow().innerText(),/手工命名/);

    gatewayCaptured=true;
    await page.evaluate(()=>loadWorkFeatures(true));
    assert.match(await gatewayRow().innerText(),/网关测试请求节选[\s\S]*网关请求消息节选/);
    assert.equal(await gatewayRow().locator('[data-feature="resume-preview"]').isDisabled(),true);
    await page.locator(gatewaySelector).click();
    assert.equal(await page.locator('#work-session-title').inputValue(),'');
    assert.match(await page.locator('#editor-body').innerText(),/当前显示：网关测试请求节选（网关请求消息节选）/);
    assert.match(await page.locator('#editor-body').innerText(),/留空会清除手工名称，重新显示可用的原有标题或网关请求消息节选/);
    await page.locator('#work-session-title').fill('手动名称优先');
    await page.locator('[data-feature="save-session-title"]').click();
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('[data-feature="session-rename"][data-session="fixture-gateway-session"]')?.closest('.feature-row')?.innerText.includes('手动名称优先'));
    assert.match(await gatewayRow().innerText(),/手动名称优先[\s\S]*手工命名/);
    await page.locator(gatewaySelector).click();
    await page.locator('#work-session-title').fill('');
    await page.locator('[data-feature="save-session-title"]').click();
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    await page.waitForFunction(()=>document.querySelector('[data-feature="session-rename"][data-session="fixture-gateway-session"]')?.closest('.feature-row')?.innerText.includes('网关测试请求节选'));
    assert.match(await gatewayRow().innerText(),/网关请求消息节选/);
    gatewayCaptured=false;
    await page.evaluate(()=>loadWorkFeatures(true));

    const native=page.locator('#content .feature-row').filter({hasText:'调查失败请求并保留证据'});
    await native.locator('[data-feature="session-rename"]').click();
    assert.equal(await page.locator('#work-session-title').inputValue(),'');
    assert.match(await page.locator('#editor-body').innerText(),/当前显示：调查失败请求/);
    assert.doesNotMatch(await page.locator('#editor-body').innerText(),/fixture-secret-do-not-use/);
    await page.locator('#editor-footer [data-action="close-editor"]').click();

    // A list refresh already in flight captured an older view. It must finish
    // before the post-save refresh so it cannot overwrite the saved name.
    let observed,release;
    const seen=new Promise(resolve=>observed=resolve), resume=new Promise(resolve=>release=resolve);
    heldRead={observed,release:resume};
    await page.evaluate(()=>{void loadWorkFeatures(true);});
    await seen;
    await page.locator(gatewaySelector).click();
    await page.locator('#work-session-title').fill('并发刷新测试');
    await page.locator('[data-feature="save-session-title"]').click();
    await page.locator('#editor-dialog').waitFor({state:'hidden'});
    release();
    await page.waitForFunction(()=>document.querySelector('[data-feature="session-rename"][data-session="fixture-gateway-session"]')?.closest('.feature-row')?.innerText.includes('并发刷新测试'));
    assert.match(await gatewayRow().innerText(),/手工命名/);
    await page.unroute('**/api/sessions/title');
    await page.unroute('**/api/sessions/recent');
  });
  await step("quota settings and responsive layout",async()=>{
    await page.locator('a[href="#settings"]').first().click();
    await page.locator("#quota-monitor").waitFor();
    assert.match(await page.locator("#quota-monitor").innerText(),/未知|未接入/);
    await snap("quota-settings");
    await page.locator("#work-quota-threshold").fill("33");
    await page.locator("#quota-monitor h2").click();
    await page.evaluate(()=>loadWorkFeatures(true));
    assert.equal(await page.locator("#work-quota-threshold").inputValue(),"33");
    await page.setViewportSize({width:700,height:900});await noOverflow();
    await page.locator('a[href="#library"]').first().click();await noOverflow();await snap("library-narrow");
  });
  await step("a freshly blocked recovery cannot be forced from stale UI state",async()=>{
    await page.evaluate(()=>showWorkPreview({itemId:"stale",hasConflicts:true,results:[
      {agent:"codex",status:"conflict",blockedByRecovery:true,detail:"此目标有未完成事务"}
    ]}));
    assert.equal(await page.locator('[name="work-force"]').count(),0);
    assert.equal(await page.locator('[data-feature="repreview"]').count(),0);
    assert.match(await page.locator("#editor-body").innerText(),/未完成事务/);
    await page.locator('#editor-dialog [data-action="close-editor"]').last().click();
  });
  assert.deepEqual(errors,[]);
  console.log("PASS workspace browser acceptance");
}catch(e){console.error(e);process.exitCode=1;}finally{if(browser)await browser.close();fixture.kill();}})();
